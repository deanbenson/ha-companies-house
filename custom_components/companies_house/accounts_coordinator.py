"""Reading filed accounts from the register, one company at a time.

The :class:`AccountsCoordinator` lists a company's accounts filings, fetches
the structured (iXBRL) version of each one it has not read yet, and parses
it in an executor thread. Nothing here runs on a timer of its own: the
entry-level :class:`AccountsBackfill` queue reads companies one at a time
after setup, a probe hit on a new accounts filing reads that one, and the
``read_accounts`` action reads on request.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .accounts import (
    BACKFILL_YEARS,
    HISTORY_CAP,
    METRIC_NAMES,
    METRICS,
    MONEY_METRICS,
    AccountsHistory,
    AccountsYear,
    accounts_type_from_description,
    year_payload,
)
from .api import (
    CompaniesHouseBudgetError,
    CompaniesHouseConnectionError,
    CompaniesHouseNotFoundError,
    Priority,
)
from .const import DOMAIN, LOGGER, Dataset
from .coordinator import (
    ChangeEvent,
    CompaniesHouseConfigEntry,
    CompanyRuntime,
    _CompanyCoordinator,
)
from .ixbrl import MAX_BYTES, Figure, IxbrlError, looks_like_ixbrl, parse_accounts
from .models import FilingHistoryItem

# The filing history page listed: accounts filings only, newest first.
ACCOUNTS_HISTORY_ITEMS = 20
ACCOUNTS_FILING_TYPES = frozenset({"AA", "AAMD"})
XHTML = "application/xhtml+xml"
# The back-fill queue: wait for the first probes to settle, then one company
# at a time with a pause between them so the reads never crowd the probes.
BACKFILL_START_DELAY = timedelta(seconds=60)
BACKFILL_GAP = timedelta(seconds=20)
# After a failed read (not a spent budget) wait this long before trying again.
BACKFILL_RETRY_GAP = timedelta(minutes=15)
# How many times a company whose documents keep failing is tried per request.
# A spent budget is not a failure: the queue simply waits for the window.
BACKFILL_MAX_ATTEMPTS = 3
# Accounts filed within this long are announced when read; older ones are
# only remembered under their filing date.
ANNOUNCE_WINDOW = timedelta(days=45)
# The register's made-up date and the year end written in the accounts
# themselves can differ by a few days; a comparative column this close to a
# year's made-up date is about that year.
COMPARATIVE_TOLERANCE = timedelta(days=31)
# When the comparative column's date is unknown (accounts read before it was
# recorded), a year this close to the next one is taken to be the year before.
COMPARATIVE_MAX_GAP = timedelta(days=548)


def _is_accounts(item: FilingHistoryItem) -> bool:
    return item.type in ACCOUNTS_FILING_TYPES or (item.description or "").startswith(
        "accounts"
    )


def _made_up_to(item: FilingHistoryItem) -> date | None:
    return item.made_up_date or item.action_date


def merge_filings(
    known: AccountsHistory, items: list[FilingHistoryItem], today: date
) -> list[AccountsYear]:
    """Combine the register's accounts filings with the years already read.

    One year per made-up date, the newest filing for it winning (an amended
    set supersedes the original). Years already read are kept as they are;
    new ones start pending, or as ``no_ixbrl`` straight away when the
    filing was on paper. Only the last few years are read, and the list is
    capped, newest first.
    """
    by_id = {y.transaction_id: y for y in known.years}
    cutoff = date(today.year - BACKFILL_YEARS, today.month, 1)
    newest_per_date: dict[date, FilingHistoryItem] = {}
    for item in items:
        made_up_to = _made_up_to(item)
        if not _is_accounts(item) or made_up_to is None or made_up_to < cutoff:
            continue
        current = newest_per_date.get(made_up_to)
        if current is None or (item.date or date.min) > (current.date or date.min):
            newest_per_date[made_up_to] = item
    years: dict[date, AccountsYear] = {}
    for made_up_to, item in newest_per_date.items():
        existing = by_id.get(item.transaction_id)
        if existing is not None:
            years[made_up_to] = existing
            continue
        accounts_type, amended = accounts_type_from_description(item.description)
        years[made_up_to] = AccountsYear(
            transaction_id=item.transaction_id,
            made_up_to=made_up_to,
            filed_on=item.date,
            accounts_type=accounts_type,
            amended=amended,
            paper_filed=item.paper_filed,
            document_id=item.document_id,
            status="no_ixbrl"
            if item.paper_filed or not item.document_id
            else "pending",
            source="none",
        )
    # Older years already read are kept until the cap pushes them out.
    for year in known.years:
        years.setdefault(year.made_up_to, year)
    ordered = sorted(years.values(), key=lambda y: y.made_up_to, reverse=True)
    return ordered[:HISTORY_CAP]


def _is_year_before(newer: AccountsYear, year: AccountsYear) -> bool:
    """Return True when ``newer``'s comparative column is about ``year``."""
    if newer.prior_period_end is not None:
        return abs(newer.prior_period_end - year.made_up_to) <= COMPARATIVE_TOLERANCE
    return newer.made_up_to - year.made_up_to <= COMPARATIVE_MAX_GAP


def fill_comparatives(years: list[AccountsYear]) -> list[AccountsYear]:
    """Borrow the year-before column for years with no structured accounts.

    Each year's figures come from its own accounts. Only when a year has
    none (paper accounts, or accounts that could not be read) does the next
    year's comparative column stand in, marked as such, and only when that
    column really is about this year: a gap in the register's list (a
    missing year) must not put one year's figures under another's date.
    """
    out: list[AccountsYear] = []
    for index, year in enumerate(years):
        if year.status in ("ok", "pending") or index == 0:
            out.append(year)
            continue
        newer = years[index - 1]
        if not newer.is_read or not _is_year_before(newer, year):
            out.append(year)
            continue
        figures = {
            metric: Figure(
                value=figure.prior,
                status="ok" if figure.prior is not None else "not_disclosed",
                concept=figure.concept,
            )
            for metric, figure in newer.figures.items()
        }
        if not any(f.value is not None for f in figures.values()):
            out.append(year)
            continue
        out.append(_with(year, figures=figures, source="comparative"))
    return out


def _with(year: AccountsYear, **changes: Any) -> AccountsYear:
    """Return a copy of a year with some fields replaced."""
    return replace(year, **changes)


def _unread(year: AccountsYear, status: str = "pending") -> AccountsYear:
    """Return a copy of a year with nothing read (an earlier read forgotten)."""
    return replace(
        year, status=status, source="none", figures={}, error=None, reread=False
    )


class AccountsCoordinator(_CompanyCoordinator[AccountsHistory]):
    """The figures read from a company's filed accounts.

    Never scheduled on its own: the back-fill queue, a probe hit on an
    accounts filing and the actions ask it to read. A plain refresh (at
    setup, or the reconciliation) only seeds from the snapshot.
    """

    model = AccountsHistory
    snapshot_key = Dataset.ACCOUNTS.value
    driven_by_probe = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise with nothing to retry."""
        super().__init__(*args, **kwargs)
        # Seconds until the scheduled budget is back, set when a read stopped
        # early because it was spent; nothing else would get through either.
        self.retry_in: float | None = None
        # A document could not be fetched this time; worth trying again later.
        self.fetch_failed = False
        self.read_count = 0

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Accounts are read as they are filed; the nominal interval is a year."""
        return timedelta(days=365) * self.multiplier, "read as filed"

    @property
    def wants_reading(self) -> bool:
        """Return True while the register has accounts this has not read."""
        return self.data is None or not self.data.checked or bool(self.data.pending)

    @callback
    def mark_unread(self) -> None:
        """Ask for every readable year to be read again from the register.

        The figures on hand are kept until the new read replaces them: the
        rating scores them, and a read that stalls (a document out of reach,
        a spent budget) must not drop a band it will put back later.
        """
        if self.data is None:
            return
        self.data = AccountsHistory(
            years=[
                _with(y, reread=True) if y.document_id and not y.paper_filed else y
                for y in self.data.years
            ],
            checked=self.data.checked,
        )

    async def _async_update_data(self) -> AccountsHistory:
        if not self._force_fetch:
            # Setup and the weekly reconciliation only seed; reads are asked for.
            if self.data is None:
                self.last_reason = "waiting for the first read"
                return AccountsHistory()
            self.last_reason = "snapshot"
            return self.data
        return await super()._async_update_data()

    async def _fetch(self, priority: Priority) -> AccountsHistory:
        self.retry_in = None
        self.fetch_failed = False
        known = self.data or AccountsHistory()
        try:
            history = await self.client.get_filing_history(
                self.company_number,
                items_per_page=ACCOUNTS_HISTORY_ITEMS,
                category="accounts",
                priority=priority,
            )
        except CompaniesHouseBudgetError as err:
            self.retry_in = err.retry_after
            raise
        years = merge_filings(known, history.items, dt_util.now().date())
        # What was already read, by year end: news is a year later than any
        # of these, or a different filing (an amended set) for one of them.
        read_before = {y.made_up_to: y.transaction_id for y in known.years if y.is_read}
        newest_read = max(read_before, default=None)
        read_now: list[AccountsYear] = []
        for index, year in enumerate(years):
            if year.status != "pending" and not year.reread:
                continue
            try:
                years[index] = await self._read_year(year, priority)
            except CompaniesHouseBudgetError as err:
                self.retry_in = err.retry_after
                LOGGER.debug(
                    "%s: accounts read paused, budget spent (%s)",
                    self.company_number,
                    err.reason,
                )
                break
            except CompaniesHouseConnectionError as err:
                # This document is out of reach for now; the others may not be.
                years[index] = _with(year, error=str(err))
                self.fetch_failed = True
                LOGGER.debug(
                    "%s: accounts to %s could not be fetched: %s",
                    self.company_number,
                    year.made_up_to,
                    err,
                )
                continue
            if years[index].is_read:
                read_now.append(years[index])
        result = AccountsHistory(years=fill_comparatives(years), checked=True)
        if read_now:
            self.read_count += len(read_now)
            self._import_statistics(result)
            # One event for the newest accounts read, not one per back-filled
            # year: the older ones are history, not news.
            newest = max(read_now, key=lambda y: y.made_up_to)
            replaced = read_before.get(newest.made_up_to)
            if (
                newest_read is None
                or newest.made_up_to > newest_read
                or (replaced is not None and replaced != newest.transaction_id)
            ):
                event = ChangeEvent("accounts", "read", year_payload(newest))
                filed = newest.filed_on
                today = dt_util.now().date()
                if filed is not None and today - filed <= ANNOUNCE_WINDOW:
                    # Freshly filed: worth an alert and a place in this week's report.
                    self.company.dispatch(event)
                elif filed is not None:
                    # Old accounts read for the first time: history, not news.
                    self.company.record(event, at=dt_util.start_of_local_day(filed))
        return result

    @callback
    def _async_refresh_finished(self) -> None:
        """Re-rate the company, then hand an unfinished read back to the queue.

        The figures feed the risk rating, so a read that landed re-rates the
        company at once (a band can move on net liabilities alone). A read
        asked for by the probe or an action has no timer of its own, so when
        it stopped early (budget spent, a document out of reach, the listing
        failed) the queue takes it from here. The queue's own reads decide
        for themselves.
        """
        super()._async_refresh_finished()
        if not self._force_fetch:
            return
        queue = self.company.entry.runtime_data.accounts_backfill
        if queue.running is self.company:
            return
        if self.retry_in is not None:
            queue.enqueue(
                self.company,
                delay=max(timedelta(seconds=self.retry_in), BACKFILL_GAP),
                front=True,
            )
        elif self.fetch_failed or not self.last_update_success:
            queue.enqueue(self.company, delay=BACKFILL_RETRY_GAP)

    async def _read_year(self, year: AccountsYear, priority: Priority) -> AccountsYear:
        """Fetch and parse one set of accounts. Budget errors propagate."""
        document_id = year.document_id
        assert document_id is not None
        try:
            content = await self.client.download_document(
                document_id, content_type=XHTML, priority=priority
            )
        except CompaniesHouseNotFoundError:
            # 406: the document exists, but only as a PDF.
            return _unread(year, "no_ixbrl")
        # From here the answer replaces whatever an earlier read left.
        year = _unread(year)
        if len(content) > MAX_BYTES:
            return _with(
                year,
                status="parse_error",
                error=f"document too large ({len(content)} bytes)",
            )
        if not looks_like_ixbrl(content):
            # The register answered with something else, usually the PDF. The
            # metadata says for sure whether a structured version exists.
            try:
                metadata = await self.client.get_document_metadata(
                    document_id, priority=priority
                )
            except CompaniesHouseNotFoundError:
                return _with(year, status="no_ixbrl")
            resources = metadata.get("resources")
            if not isinstance(resources, dict) or XHTML not in resources:
                return _with(year, status="no_ixbrl")
            return _with(year, status="parse_error", error="not an iXBRL document")
        try:
            parsed = await self.hass.async_add_executor_job(parse_accounts, content)
        except IxbrlError as err:
            LOGGER.debug(
                "%s: accounts to %s could not be read: %s",
                self.company_number,
                year.made_up_to,
                err,
            )
            return _with(year, status="parse_error", error=str(err))
        return _with(
            year,
            status="ok",
            source="ixbrl",
            error=None,
            prior_period_end=parsed.prior_period_end,
            dormant=parsed.dormant,
            accounting_standard=parsed.accounting_standard,
            accounts_type_member=parsed.accounts_type_member,
            figures=dict(parsed.figures),
        )

    @callback
    def _import_statistics(self, history: AccountsHistory) -> None:
        """Write the five-year series to the recorder's long-term statistics."""
        if "recorder" not in self.hass.config.components:
            return
        async_import_accounts_statistics(
            self.hass, self.company.company_name, self.company_number, history
        )


def statistic_id(company_number: str, metric: str) -> str:
    """Return the external statistic id for one company and metric."""
    return f"{DOMAIN}:{company_number.lower()}_{metric}"


@callback
def async_import_accounts_statistics(
    hass: HomeAssistant,
    company_name: str,
    company_number: str,
    history: AccountsHistory,
) -> None:
    """Import one row per financial year for every metric with figures.

    The whole series is written each time; the recorder updates rows in
    place, so this is safe to repeat.
    """
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    for metric in METRICS:
        rows: list[StatisticData] = []
        for year in sorted(history.years, key=lambda y: y.made_up_to):
            value = year.value(metric)
            if value is None:
                continue
            start = dt_util.start_of_local_day(year.made_up_to)
            number = float(value)
            rows.append({"start": start, "mean": number, "min": number, "max": number})
        if not rows:
            continue
        metadata: StatisticMetaData = {
            "mean_type": StatisticMeanType.ARITHMETIC,
            "has_sum": False,
            "name": f"{company_name} {METRIC_NAMES[metric].lower()}",
            "source": DOMAIN,
            "statistic_id": statistic_id(company_number, metric),
            "unit_class": None,
            "unit_of_measurement": "GBP" if metric in MONEY_METRICS else None,
        }
        async_add_external_statistics(hass, metadata, rows)


class AccountsBackfill:
    """Reads accounts for the watched companies one at a time.

    Companies join the queue when they are set up and whenever the register
    has accounts they have not read. The queue starts a minute after setup,
    waits between companies, and when the scheduled budget is spent waits
    for it to come back rather than dipping into the on-demand reserve.
    """

    def __init__(self, hass: HomeAssistant, entry: CompaniesHouseConfigEntry) -> None:
        """Start empty."""
        self.hass = hass
        self.entry = entry
        self._queue: deque[CompanyRuntime] = deque()
        self._attempts: dict[str, int] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._task: asyncio.Task[None] | None = None
        self._job = HassJob(
            self._start, f"{DOMAIN} accounts back-fill", cancel_on_shutdown=True
        )
        self.running: CompanyRuntime | None = None

    @property
    def queued(self) -> list[str]:
        """Return the company numbers waiting, in order."""
        return [c.company_number for c in self._queue]

    @callback
    def enqueue(
        self,
        company: CompanyRuntime,
        *,
        delay: timedelta = BACKFILL_START_DELAY,
        front: bool = False,
    ) -> None:
        """Add a company to the queue and make sure the queue is running."""
        if company in self._queue:
            if front:
                self._queue.remove(company)
                self._queue.appendleft(company)
            return
        if front:
            self._queue.appendleft(company)
        else:
            self._queue.append(company)
        # A fresh request starts the failure count again.
        self._attempts[company.company_number] = 0
        self._arm(delay)

    @callback
    def discard(self, company: CompanyRuntime) -> None:
        """Forget a company that is no longer watched."""
        if company in self._queue:
            self._queue.remove(company)
        self._attempts.pop(company.company_number, None)

    @callback
    def _arm(self, delay: timedelta) -> None:
        if self._unsub is not None or self.running is not None:
            return
        self._unsub = async_call_later(self.hass, delay, self._job)

    @callback
    def _start(self, _now: datetime) -> None:
        self._unsub = None
        if not self._queue or self.running is not None:
            return
        # The task starts eagerly and may well finish (and re-arm the timer)
        # before this returns, so only a task still running is kept.
        task = self.entry.async_create_background_task(
            self.hass, self._run_one(), name=f"{DOMAIN} accounts back-fill"
        )
        if not task.done():
            self._task = task

    async def _run_one(self) -> None:
        company = self._queue.popleft()
        self.running = company
        coordinator = company.accounts
        try:
            await coordinator.async_refresh_now(on_demand=False)
        finally:
            self.running = None
            self._task = None
        number = company.company_number
        delay = BACKFILL_GAP
        if coordinator.retry_in is not None:
            # Budget spent: the same company again once the window rolls
            # over, however often that takes; waiting is not failing.
            self._queue.appendleft(company)
            delay = max(timedelta(seconds=coordinator.retry_in), BACKFILL_GAP)
        elif coordinator.fetch_failed or not coordinator.last_update_success:
            # Something could not be fetched: let the others go first, and
            # wait a while when there is nobody else.
            self._attempts[number] = self._attempts.get(number, 0) + 1
            if self._attempts[number] < BACKFILL_MAX_ATTEMPTS:
                self._queue.append(company)
                if len(self._queue) == 1:
                    delay = BACKFILL_RETRY_GAP
        else:
            self._attempts.pop(number, None)
        if self._queue:
            self._arm(delay)

    @callback
    def async_shutdown(self) -> None:
        """Stop the queue."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._queue.clear()


__all__ = [
    "BACKFILL_GAP",
    "BACKFILL_RETRY_GAP",
    "BACKFILL_START_DELAY",
    "AccountsBackfill",
    "AccountsCoordinator",
    "async_import_accounts_statistics",
    "fill_comparatives",
    "merge_filings",
    "statistic_id",
]
