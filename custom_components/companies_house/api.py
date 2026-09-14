"""Async client for the Companies House public data and document APIs.

Auth is HTTP Basic with the API key as the username and an empty password.
Every request passes through a shared :class:`RateLimiter` which enforces the
published 600 per 5 minute window, a 2 request per second ceiling, and the
80 percent budget guard for scheduled work described in the spec.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import math
import re
import time
from typing import Any

import aiohttp
from multidict import CIMultiDictProxy
from yarl import URL

from .const import (
    API_BASE,
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    BUDGET_SCHEDULED_FRACTION,
    DEFAULT_MAX_PAGES,
    DOCUMENT_API_BASE,
    HEADER_RATE_LIMIT,
    HEADER_RATE_REMAIN,
    HEADER_RATE_RESET,
    HEADER_RATE_WINDOW,
    ITEMS_PER_PAGE,
    RATE_LIMIT_DEFAULT,
    RATE_MAX_PER_SECOND,
    RATE_WINDOW_DEFAULT,
    REQUEST_TIMEOUT,
    THROTTLE_REPAIR_WINDOWS,
)
from .models import (
    AppointmentList,
    ChargeList,
    CompanyProfile,
    FilingHistory,
    Insolvency,
    JsonDict,
    OfficerList,
    PscData,
    Structure,
)


class CompaniesHouseError(Exception):
    """Base error for the client."""


class CompaniesHouseAuthError(CompaniesHouseError):
    """The API key was rejected."""


class CompaniesHouseNotFoundError(CompaniesHouseError):
    """The resource does not exist."""


class CompaniesHouseUnsupportedFormatError(CompaniesHouseNotFoundError):
    """The document exists but not in the format asked for (HTTP 406).

    Accounts filed on paper have no structured (iXBRL) version, so asking
    for one is answered this way. It is a normal outcome, not a failure.
    """


class CompaniesHouseConnectionError(CompaniesHouseError):
    """The API could not be reached or returned a server error."""


class CompaniesHouseRateLimitError(CompaniesHouseError):
    """The API returned 429 or the whole window is spent."""

    def __init__(self, retry_after: float) -> None:
        """Store how long to wait."""
        super().__init__(f"Rate limited, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class CompaniesHouseBudgetError(CompaniesHouseError):
    """Scheduled work has spent its share of the window; defer to the next."""

    def __init__(self, retry_after: float, reason: str) -> None:
        """Store when to try again."""
        super().__init__(
            f"Scheduled budget exhausted ({reason}), retry after {retry_after:.0f}s"
        )
        self.retry_after = retry_after
        self.reason = reason


class Priority(StrEnum):
    """Who is asking for the request."""

    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"


@dataclass(frozen=True)
class RateLimitStatus:
    """A snapshot of the limiter for diagnostics."""

    limit: int
    window_seconds: int
    used: int
    remaining: int
    percent_used: float
    reset_at: datetime | None
    blocked_until: datetime | None
    throttled_since: datetime | None
    from_headers: bool


_WINDOW_RE = re.compile(r"^\s*(\d+)\s*([smh])\s*$")


def parse_window(value: str | None) -> float | None:
    """Parse ``X-Ratelimit-Window`` such as ``5m`` into seconds."""
    if not value:
        return None
    match = _WINDOW_RE.match(value)
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600}[unit]


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _utc(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=UTC) if ts is not None else None


class RateLimiter:
    """Token bucket with a scheduled-work budget guard.

    The limiter keeps a local sliding-window count of requests and trusts the
    ``X-Ratelimit-*`` headers over it whenever they are fresh.
    """

    def __init__(
        self,
        *,
        limit: int = RATE_LIMIT_DEFAULT,
        window: float = RATE_WINDOW_DEFAULT.total_seconds(),
        max_per_second: float | None = None,
        scheduled_fraction: float = BUDGET_SCHEDULED_FRACTION,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        """Initialise the limiter."""
        self.limit = limit
        self.window = window
        # Resolved at call time so tests can switch pacing off.
        self.max_per_second = (
            RATE_MAX_PER_SECOND if max_per_second is None else max_per_second
        )
        self.scheduled_fraction = scheduled_fraction
        # Looked up lazily so a patched ``time.time`` (tests) is honoured.
        self._clock = clock or (lambda: time.time())  # noqa: PLW0108
        self._sleep = sleep or (lambda seconds: asyncio.sleep(seconds))  # noqa: PLW0108
        self._lock = asyncio.Lock()
        self._history: deque[float] = deque()
        self._last_request: float | None = None
        self._server_remaining: int | None = None
        self._server_reset: float | None = None
        self._requests_since_server = 0
        self._blocked_until: float | None = None
        self._backoff = BACKOFF_INITIAL.total_seconds()
        self._throttled_since: float | None = None
        self.throttle_count = 0

    # -- accounting -------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        if self._server_reset is not None and now >= self._server_reset:
            # The server window rolled; anything before it no longer counts.
            cutoff = max(cutoff, self._server_reset)
            self._server_remaining = None
            self._server_reset = None
            self._requests_since_server = 0
        while self._history and self._history[0] < cutoff:
            self._history.popleft()

    def _server_fresh(self, now: float) -> bool:
        return (
            self._server_remaining is not None
            and self._server_reset is not None
            and now < self._server_reset
        )

    def remaining(self, now: float | None = None) -> int:
        """Return the requests left in the current window."""
        now = self._clock() if now is None else now
        self._prune(now)
        local = self.limit - len(self._history)
        if self._server_fresh(now):
            assert self._server_remaining is not None
            return max(0, self._server_remaining - self._requests_since_server)
        return max(0, local)

    def used(self, now: float | None = None) -> int:
        """Return the requests spent in the current window."""
        now = self._clock() if now is None else now
        return self.limit - self.remaining(now)

    def next_reset(self, now: float | None = None) -> float:
        """Return the unix timestamp at which capacity next returns."""
        now = self._clock() if now is None else now
        self._prune(now)
        if self._server_reset is not None and now < self._server_reset:
            return self._server_reset
        if self._history:
            return self._history[0] + self.window
        return now

    @property
    def scheduled_reserve(self) -> int:
        """Return the requests kept back for on demand work."""
        return math.ceil(self.limit * (1 - self.scheduled_fraction))

    def scheduled_allowed(self, now: float | None = None) -> bool:
        """Return True if scheduled work may spend a request right now."""
        now = self._clock() if now is None else now
        if self._blocked_until is not None and now < self._blocked_until:
            return False
        return self.remaining(now) > self.scheduled_reserve

    def persistently_throttled(self, now: float | None = None) -> bool:
        """Return True once 429s have persisted past three windows."""
        now = self._clock() if now is None else now
        return (
            self._throttled_since is not None
            and now - self._throttled_since >= THROTTLE_REPAIR_WINDOWS * self.window
        )

    def status(self) -> RateLimitStatus:
        """Return a snapshot for diagnostics."""
        now = self._clock()
        remaining = self.remaining(now)
        used = self.limit - remaining
        return RateLimitStatus(
            limit=self.limit,
            window_seconds=int(self.window),
            used=used,
            remaining=remaining,
            percent_used=round(100 * used / self.limit, 1) if self.limit else 0.0,
            reset_at=_utc(self.next_reset(now))
            if self._history or self._server_reset
            else None,
            blocked_until=_utc(self._blocked_until)
            if self._blocked_until and now < self._blocked_until
            else None,
            throttled_since=_utc(self._throttled_since),
            from_headers=self._server_fresh(now),
        )

    # -- feedback from responses -----------------------------------------

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Absorb the ``X-Ratelimit-*`` headers. Never fails on a missing one."""
        limit = _to_int(headers.get(HEADER_RATE_LIMIT))
        remain = _to_int(headers.get(HEADER_RATE_REMAIN))
        reset = _to_int(headers.get(HEADER_RATE_RESET))
        window = parse_window(headers.get(HEADER_RATE_WINDOW))
        if limit and limit > 0:
            self.limit = limit
        if window and window > 0:
            self.window = window
        if remain is not None and reset is not None and reset > 0:
            self._server_remaining = max(0, remain)
            self._server_reset = float(reset)
            self._requests_since_server = 0

    def note_success(self) -> None:
        """Reset throttling state after a successful response."""
        self._backoff = BACKOFF_INITIAL.total_seconds()
        self._blocked_until = None
        self._throttled_since = None
        self.throttle_count = 0

    def note_rate_limited(self, retry_after: float | None) -> float:
        """Record a 429 and return how long requests are blocked for."""
        now = self._clock()
        self.throttle_count += 1
        if self._throttled_since is None:
            self._throttled_since = now
        if retry_after is not None and retry_after > 0:
            delay = retry_after
        else:
            delay = self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX.total_seconds())
        self._blocked_until = now + delay
        return delay

    # -- acquiring --------------------------------------------------------

    async def acquire(self, priority: Priority = Priority.SCHEDULED) -> None:
        """Wait for, or refuse, permission to send one request."""
        async with self._lock:
            while True:
                now = self._clock()
                if self._blocked_until is not None and now < self._blocked_until:
                    wait = self._blocked_until - now
                    if priority is Priority.SCHEDULED:
                        raise CompaniesHouseBudgetError(wait, "throttled")
                    if wait > self.window:
                        raise CompaniesHouseRateLimitError(wait)
                    await self._sleep(wait)
                    continue
                remaining = self.remaining(now)
                if (
                    priority is Priority.SCHEDULED
                    and remaining <= self.scheduled_reserve
                ):
                    raise CompaniesHouseBudgetError(
                        max(1.0, self.next_reset(now) - now), "budget"
                    )
                if remaining <= 0:
                    await self._sleep(max(1.0, self.next_reset(now) - now))
                    continue
                gap = 1 / self.max_per_second if self.max_per_second > 0 else 0
                if (
                    self._last_request is not None
                    and (elapsed := now - self._last_request) < gap
                ):
                    await self._sleep(gap - elapsed)
                    continue
                self._history.append(now)
                self._requests_since_server += 1
                self._last_request = now
                return


class CompaniesHouseClient:
    """Thin typed client. Raises the ``CompaniesHouse*`` errors above."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        *,
        limiter: RateLimiter | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        """Initialise the client with an injected session."""
        self._session = session
        # HTTP Basic: the key is the username, the password is empty (7.1).
        self._authorization = aiohttp.encode_basic_auth(api_key, "")
        self.limiter = limiter or RateLimiter()
        self.max_pages = max(1, max_pages)
        self.last_error: str | None = None
        self.last_success: datetime | None = None

    # -- transport --------------------------------------------------------

    async def _get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        priority: Priority = Priority.SCHEDULED,
        accept: str = "application/json",
        allow_redirects: bool = True,
    ) -> aiohttp.ClientResponse:
        await self.limiter.acquire(priority)
        try:
            resp = await self._session.get(
                url,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers={"Accept": accept, "Authorization": self._authorization},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT.total_seconds()),
                allow_redirects=allow_redirects,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            self.last_error = f"{type(err).__name__}: {err}"
            raise CompaniesHouseConnectionError(str(err)) from err
        self.limiter.update_from_headers(resp.headers)
        if resp.status == 429:
            retry_after = _retry_after(resp.headers)
            delay = self.limiter.note_rate_limited(retry_after)
            self.last_error = f"429 rate limited for {delay:.0f}s"
            resp.release()
            raise CompaniesHouseRateLimitError(delay)
        if resp.status in (401, 403):
            self.last_error = f"{resp.status} authentication failed"
            resp.release()
            raise CompaniesHouseAuthError(f"HTTP {resp.status}")
        if resp.status == 404:
            resp.release()
            raise CompaniesHouseNotFoundError(url)
        if resp.status == 406:
            resp.release()
            raise CompaniesHouseUnsupportedFormatError(f"{url} as {accept}")
        if resp.status >= 400:
            self.last_error = f"HTTP {resp.status}"
            resp.release()
            raise CompaniesHouseConnectionError(f"HTTP {resp.status}")
        self.limiter.note_success()
        self.last_success = datetime.now(tz=UTC)
        return resp

    async def request(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        priority: Priority = Priority.SCHEDULED,
        base: str = API_BASE,
    ) -> JsonDict:
        """GET a JSON resource."""
        resp = await self._get(f"{base}{path}", params=params, priority=priority)
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as err:
            self.last_error = f"Invalid JSON: {err}"
            raise CompaniesHouseConnectionError("Invalid JSON") from err
        finally:
            resp.release()
        return data if isinstance(data, dict) else {}

    async def request_optional(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        priority: Priority = Priority.SCHEDULED,
    ) -> JsonDict | None:
        """GET a JSON resource, returning None for a 404."""
        try:
            return await self.request(path, params=params, priority=priority)
        except CompaniesHouseNotFoundError:
            return None

    async def _paginated(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        priority: Priority = Priority.SCHEDULED,
        total_key: str = "total_results",
    ) -> tuple[JsonDict, list[JsonDict]]:
        """Fetch every page of a list up to ``max_pages``."""
        first = await self.request(
            path,
            params={
                **(params or {}),
                "items_per_page": ITEMS_PER_PAGE,
                "start_index": 0,
            },
            priority=priority,
        )
        items: list[JsonDict] = [
            i for i in first.get("items") or [] if isinstance(i, dict)
        ]
        total = first.get(total_key)
        page = 1
        while (
            isinstance(total, int)
            and len(items) < total
            and page < self.max_pages
            and items
        ):
            more = await self.request(
                path,
                params={
                    **(params or {}),
                    "items_per_page": ITEMS_PER_PAGE,
                    "start_index": len(items),
                },
                priority=priority,
            )
            new = [i for i in more.get("items") or [] if isinstance(i, dict)]
            if not new:
                break
            items.extend(new)
            page += 1
        return first, items

    # -- validation -------------------------------------------------------

    async def validate_key(self) -> None:
        """Make the cheapest possible authenticated call."""
        await self.request(
            "/search/companies",
            params={"q": "test", "items_per_page": 1},
            priority=Priority.ON_DEMAND,
        )

    # -- company resources ------------------------------------------------

    async def get_company(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> CompanyProfile:
        """Fetch the company profile."""
        return CompanyProfile.from_api(
            await self.request(f"/company/{company_number}", priority=priority)
        )

    async def get_registered_office(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch the registered office address."""
        return await self.request(
            f"/company/{company_number}/registered-office-address", priority=priority
        )

    async def get_filing_history(
        self,
        company_number: str,
        *,
        items_per_page: int = 1,
        start_index: int = 0,
        category: str | None = None,
        priority: Priority = Priority.SCHEDULED,
    ) -> FilingHistory:
        """Fetch a page of filing history, newest first."""
        data = await self.request_optional(
            f"/company/{company_number}/filing-history",
            params={
                "items_per_page": items_per_page,
                "start_index": start_index,
                "category": category,
            },
            priority=priority,
        )
        return FilingHistory.from_api(data or {})

    async def get_filing(
        self,
        company_number: str,
        transaction_id: str,
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch one filing history transaction."""
        return await self.request(
            f"/company/{company_number}/filing-history/{transaction_id}",
            priority=priority,
        )

    async def get_officers(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> OfficerList:
        """Fetch every officer appointment, active and resigned."""
        try:
            first, items = await self._paginated(
                f"/company/{company_number}/officers", priority=priority
            )
        except CompaniesHouseNotFoundError:
            return OfficerList()
        return OfficerList.from_api(first, items)

    async def get_officers_raw(
        self,
        company_number: str,
        *,
        register_view: bool | None = None,
        order_by: str | None = None,
        items_per_page: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch the officer list, unmodelled, for the action."""
        return await self.request(
            f"/company/{company_number}/officers",
            params={
                "register_view": "true" if register_view else None,
                "order_by": order_by,
                "items_per_page": items_per_page,
            },
            priority=priority,
        )

    async def get_officer_appointment(
        self,
        company_number: str,
        appointment_id: str,
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch one appointment on a company."""
        return await self.request(
            f"/company/{company_number}/appointments/{appointment_id}",
            priority=priority,
        )

    async def get_psc(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> PscData:
        """Fetch PSC notifications and statements."""
        try:
            first, items = await self._paginated(
                f"/company/{company_number}/persons-with-significant-control",
                priority=priority,
            )
        except CompaniesHouseNotFoundError:
            first, items = {}, []
        try:
            statements, statement_items = await self._paginated(
                f"/company/{company_number}/persons-with-significant-control-statements",
                priority=priority,
            )
        except CompaniesHouseNotFoundError:
            statements, statement_items = {}, []
        return PscData.from_api(first, statements, items, statement_items)

    async def get_psc_raw(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch the PSC list, unmodelled, for the action."""
        return await self.request(
            f"/company/{company_number}/persons-with-significant-control",
            priority=priority,
        )

    async def get_psc_statements_raw(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch the PSC statements, unmodelled, for the action."""
        return await self.request(
            f"/company/{company_number}/persons-with-significant-control-statements",
            priority=priority,
        )

    async def get_psc_detail(
        self,
        company_number: str,
        kind: str,
        notification_id: str,
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch one PSC notification of the given kind."""
        return await self.request(
            f"/company/{company_number}/persons-with-significant-control/{kind}/{notification_id}",
            priority=priority,
        )

    async def get_psc_statement(
        self,
        company_number: str,
        statement_id: str,
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch one PSC statement."""
        return await self.request(
            f"/company/{company_number}/persons-with-significant-control-statements/{statement_id}",
            priority=priority,
        )

    async def get_charges(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> ChargeList:
        """Fetch every charge."""
        try:
            first, items = await self._paginated(
                f"/company/{company_number}/charges",
                priority=priority,
                total_key="total_count",
            )
        except CompaniesHouseNotFoundError:
            return ChargeList()
        return ChargeList.from_api(first, items)

    async def get_charges_raw(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch the charge list, unmodelled, for the action."""
        return await self.request(
            f"/company/{company_number}/charges", priority=priority
        )

    async def get_charge(
        self,
        company_number: str,
        charge_id: str,
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Fetch one charge."""
        return await self.request(
            f"/company/{company_number}/charges/{charge_id}", priority=priority
        )

    async def get_insolvency(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> Insolvency:
        """Fetch insolvency history; empty when the company has none."""
        return Insolvency.from_api(
            await self.request_optional(
                f"/company/{company_number}/insolvency", priority=priority
            )
        )

    async def get_insolvency_raw(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch insolvency, unmodelled, for the action."""
        return await self.request(
            f"/company/{company_number}/insolvency", priority=priority
        )

    async def get_registers(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict | None:
        """Fetch the registers resource."""
        return await self.request_optional(
            f"/company/{company_number}/registers", priority=priority
        )

    async def get_exemptions(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict | None:
        """Fetch the exemptions resource."""
        return await self.request_optional(
            f"/company/{company_number}/exemptions", priority=priority
        )

    async def get_uk_establishments(
        self, company_number: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict | None:
        """Fetch the UK establishments of an overseas company."""
        return await self.request_optional(
            f"/company/{company_number}/uk-establishments", priority=priority
        )

    async def get_structure(
        self, company_number: str, *, priority: Priority = Priority.SCHEDULED
    ) -> Structure:
        """Fetch registers, exemptions and UK establishments."""
        return Structure.from_api(
            await self.get_registers(company_number, priority=priority),
            await self.get_exemptions(company_number, priority=priority),
            await self.get_uk_establishments(company_number, priority=priority),
        )

    # -- officer resources ------------------------------------------------

    async def get_officer_appointments(
        self, officer_id: str, *, priority: Priority = Priority.SCHEDULED
    ) -> AppointmentList:
        """Fetch every appointment held by an officer."""
        first, items = await self._paginated(
            f"/officers/{officer_id}/appointments", priority=priority
        )
        return AppointmentList.from_api(officer_id, first, items)

    async def get_officer_appointments_raw(
        self, officer_id: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch appointments, unmodelled, for the action."""
        return await self.request(
            f"/officers/{officer_id}/appointments", priority=priority
        )

    async def get_disqualification(
        self,
        officer_id: str,
        kind: str = "natural",
        *,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict | None:
        """Fetch a natural or corporate disqualification record."""
        return await self.request_optional(
            f"/disqualified-officers/{kind}/{officer_id}", priority=priority
        )

    # -- search -----------------------------------------------------------

    async def search_companies(
        self,
        query: str,
        *,
        items_per_page: int | None = None,
        start_index: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Search companies."""
        return await self.request(
            "/search/companies",
            params={
                "q": query,
                "items_per_page": items_per_page,
                "start_index": start_index,
            },
            priority=priority,
        )

    async def search_officers(
        self,
        query: str,
        *,
        items_per_page: int | None = None,
        start_index: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Search officers."""
        return await self.request(
            "/search/officers",
            params={
                "q": query,
                "items_per_page": items_per_page,
                "start_index": start_index,
            },
            priority=priority,
        )

    async def search_disqualified_officers(
        self,
        query: str,
        *,
        items_per_page: int | None = None,
        start_index: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Search disqualified officers."""
        return await self.request(
            "/search/disqualified-officers",
            params={
                "q": query,
                "items_per_page": items_per_page,
                "start_index": start_index,
            },
            priority=priority,
        )

    async def search_all(
        self,
        query: str,
        *,
        items_per_page: int | None = None,
        start_index: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Search across companies, officers and disqualified officers."""
        return await self.request(
            "/search",
            params={
                "q": query,
                "items_per_page": items_per_page,
                "start_index": start_index,
            },
            priority=priority,
        )

    async def advanced_search(
        self, params: Mapping[str, Any], *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Run the advanced company search; ``params`` pass through as documented."""
        return await self.request(
            "/advanced-search/companies", params=params, priority=priority
        )

    async def alphabetical_search(
        self,
        query: str,
        *,
        search_above: str | None = None,
        search_below: str | None = None,
        size: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Run the alphabetical company search."""
        return await self.request(
            "/alphabetical-search/companies",
            params={
                "q": query,
                "search_above": search_above,
                "search_below": search_below,
                "size": size,
            },
            priority=priority,
        )

    async def dissolved_search(
        self,
        query: str,
        *,
        search_type: str | None = None,
        start_index: int | None = None,
        size: int | None = None,
        priority: Priority = Priority.ON_DEMAND,
    ) -> JsonDict:
        """Run the dissolved company search."""
        return await self.request(
            "/dissolved-search/companies",
            params={
                "q": query,
                "search_type": search_type,
                "start_index": start_index,
                "size": size,
            },
            priority=priority,
        )

    # -- documents --------------------------------------------------------

    async def get_document_metadata(
        self, document_id: str, *, priority: Priority = Priority.ON_DEMAND
    ) -> JsonDict:
        """Fetch document metadata from the Document API."""
        return await self.request(
            f"/document/{document_id}", priority=priority, base=DOCUMENT_API_BASE
        )

    async def download_document(
        self,
        document_id: str,
        *,
        content_type: str = "application/pdf",
        priority: Priority = Priority.ON_DEMAND,
    ) -> bytes:
        """Download document content.

        The Document API answers with a redirect to short lived storage. The
        redirect is followed by hand so the API key is never sent to it.
        """
        resp = await self._get(
            f"{DOCUMENT_API_BASE}/document/{document_id}/content",
            priority=priority,
            accept=content_type,
            allow_redirects=False,
        )
        try:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise CompaniesHouseConnectionError("Redirect without location")
                target = URL(location)
                if not target.is_absolute():
                    target = URL(DOCUMENT_API_BASE).join(target)
                try:
                    async with self._session.get(
                        target,
                        headers={"Accept": content_type},
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as content:
                        if content.status >= 400:
                            raise CompaniesHouseConnectionError(
                                f"HTTP {content.status} fetching document"
                            )
                        return await content.read()
                except (aiohttp.ClientError, TimeoutError) as err:
                    raise CompaniesHouseConnectionError(str(err)) from err
            return await resp.read()
        finally:
            resp.release()


def _retry_after(headers: CIMultiDictProxy[str]) -> float | None:
    value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=UTC)
    except ValueError:
        return None
    return max(0.0, (when - datetime.now(tz=UTC)).total_seconds())


__all__ = [
    "CompaniesHouseAuthError",
    "CompaniesHouseBudgetError",
    "CompaniesHouseClient",
    "CompaniesHouseConnectionError",
    "CompaniesHouseError",
    "CompaniesHouseNotFoundError",
    "CompaniesHouseRateLimitError",
    "CompaniesHouseUnsupportedFormatError",
    "Priority",
    "RateLimitStatus",
    "RateLimiter",
    "parse_window",
]
