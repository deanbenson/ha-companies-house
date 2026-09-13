"""Coordinators: one per dataset, driven by the filing history probe.

The probe coordinator owns the adaptive schedule (spec 6.4) and, on a hit,
asks only the coordinators the filing category implicates to refresh. Every
coordinator seeds from its stored snapshot so a reload or restart costs no
requests while the snapshot is fresh, detects changes against the previous
copy, and hands change events to the company or officer runtime, which
fans them out to the event entities and the bus.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CompaniesHouseAuthError,
    CompaniesHouseBudgetError,
    CompaniesHouseClient,
    CompaniesHouseConnectionError,
    CompaniesHouseNotFoundError,
    CompaniesHouseRateLimitError,
    Priority,
    RateLimitStatus,
)
from .const import (
    CONF_CADENCE_MULTIPLIER,
    DEFAULT_CADENCE_MULTIPLIER,
    DOMAIN,
    EVENT_COMPANIES_HOUSE,
    FILING_CATEGORY_EVENT_TYPE,
    FILING_CATEGORY_REFRESH,
    FILING_EVENT_TYPES,
    FILING_UNKNOWN_REFRESH,
    FINISHED_STATUSES,
    LOGGER,
    STATUS_DETAIL_STRIKE_OFF,
    Dataset,
    OfficerDataset,
    Tier,
)
from .models import (
    AppointmentList,
    ChargeList,
    CompanyProfile,
    DateOfBirth,
    DisqualificationMatch,
    DisqualificationResult,
    FilingHistory,
    FilingHistoryItem,
    Insolvency,
    JsonDict,
    OfficerList,
    PscData,
    StorableModel,
    Structure,
    parse_date,
)
from .repairs import (
    async_clear_rate_limited,
    async_clear_subentry_issue,
    async_raise_rate_limited,
    async_raise_subentry_issue,
)
from .scheduler import (
    appointments_next_run,
    compute_tier,
    disqualification_next_run,
    jitter,
    probe_interval,
    profile_interval,
    reconciliation_due,
    structure_due,
)
from .store import ChangeStore, CompanyState, DatasetSnapshot, OfficerState

if TYPE_CHECKING:
    from . import CompaniesHouseRuntimeData

type CompaniesHouseConfigEntry = ConfigEntry[CompaniesHouseRuntimeData]

PROBE_SEED_ITEMS = 25
PROBE_CATCH_UP_MAX = 25
ACCOUNT_INTERVAL = timedelta(seconds=60)
STRIKE_OFF_NOTICE_DESCRIPTIONS = frozenset(
    {
        "gazette-notice-voluntary",
        "gazette-notice-compulsory",
        "gazette-notice-compulsary",
    }
)
STRIKE_OFF_DISCONTINUED_DESCRIPTIONS = frozenset(
    {
        "gazette-filings-brought-up-to-date",
        "dissolution-voluntary-strike-off-discontinued",
        "dissolution-voluntary-strike-off-suspended",
        "dissolved-compulsory-strike-off-suspended",
        "dissolution-withdrawal-application-strike-off-company",
        "dissolution-withdrawal-application-strike-off-limited-liability-partnership",
    }
)


def signal_changes(subentry_id: str) -> str:
    """Dispatcher signal carrying change events for a subentry."""
    return f"{DOMAIN}_changes_{subentry_id}"


def signal_tier(subentry_id: str) -> str:
    """Dispatcher signal fired when a company's tier or schedule changes."""
    return f"{DOMAIN}_tier_{subentry_id}"


@dataclass(frozen=True)
class ChangeEvent:
    """A change detected in the register."""

    kind: str
    event_type: str
    payload: dict[str, Any]


class _Runtime:
    """Shared dispatch and buffering for company and officer runtimes."""

    def __init__(self, hass: HomeAssistant, subentry: ConfigSubentry) -> None:
        self.hass = hass
        self.subentry = subentry
        self.ready = False
        self._buffer: list[ChangeEvent] = []

    def _bus_context(self) -> dict[str, Any]:
        raise NotImplementedError

    @callback
    def dispatch(self, event: ChangeEvent) -> None:
        """Fan a change out to the event entities and the bus.

        Events raised before the platforms are loaded are buffered so a
        change detected during setup still reaches its entity.
        """
        if not self.ready:
            self._buffer.append(event)
            return
        async_dispatcher_send(
            self.hass, signal_changes(self.subentry.subentry_id), event
        )
        self.hass.bus.async_fire(
            EVENT_COMPANIES_HOUSE,
            {
                **self._bus_context(),
                "kind": event.kind,
                "event_type": event.event_type,
                **event.payload,
            },
        )

    @callback
    def mark_ready(self) -> None:
        """Release buffered events once entities exist."""
        self.ready = True
        buffered, self._buffer = self._buffer, []
        for event in buffered:
            self.dispatch(event)


class CompaniesHouseCoordinator[DataT: StorableModel](DataUpdateCoordinator[DataT]):
    """Base coordinator with snapshot seeding and adaptive scheduling."""

    model: type[DataT]
    snapshot_key: str
    # Coordinators driven by the probe (officers, PSC, charges, insolvency,
    # structure) never schedule themselves: the probe refreshes them on a
    # filing hit and at the weekly and monthly slots. Only the probe and the
    # profile keep their own timers.
    driven_by_probe = False
    # The base class types ``data`` as never None; it is None until the first
    # successful fetch, and the entities rely on that.
    data: DataT | None  # type: ignore[assignment]

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        client: CompaniesHouseClient,
        store: ChangeStore,
        snapshots: dict[str, DatasetSnapshot],
        *,
        name: str,
    ) -> None:
        """Initialise without a fixed update interval; scheduling is adaptive."""
        super().__init__(
            hass, LOGGER, config_entry=entry, name=name, update_interval=None
        )
        self.client = client
        self.store = store
        self.snapshots = snapshots
        self.fetched_at: datetime | None = None
        self.next_run: datetime | None = None
        self.last_reason = "not run"
        self._first_run = True
        self._force_fetch = False

    # -- scheduling -------------------------------------------------------

    @property
    def multiplier(self) -> float:
        """Return the cadence multiplier from the options."""
        assert self.config_entry is not None
        value = self.config_entry.options.get(
            CONF_CADENCE_MULTIPLIER, DEFAULT_CADENCE_MULTIPLIER
        )
        return float(value)

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Return the nominal interval and the reason. Subclasses decide."""
        raise NotImplementedError

    def _delay(self, now: datetime) -> tuple[timedelta, str]:
        """Return the delay until the next run, honouring a retry-after."""
        if self._retry_after is not None:
            delay, self._retry_after = timedelta(seconds=self._retry_after), None
            return delay, "retry after"
        interval, reason = self.interval(now)
        interval = jitter(interval)
        if self.fetched_at is not None:
            return max(self.fetched_at + interval - now, timedelta(minutes=1)), reason
        return interval, reason

    @callback
    def _schedule_refresh(self) -> None:
        """Schedule the next refresh with async_call_later instead of a fixed interval."""
        if self.driven_by_probe or (
            self.config_entry and self.config_entry.pref_disable_polling
        ):
            return
        self._async_unsub_refresh()
        now = dt_util.utcnow()
        delay, reason = self._delay(now)
        self.next_run = now + delay
        LOGGER.debug(
            "Scheduling %s: reason=%s, next run %s (in %s)",
            self.name,
            reason,
            self.next_run.isoformat(timespec="seconds"),
            delay,
        )
        self._unsub_refresh = async_call_later(
            self.hass, delay, self._handle_refresh_interval
        )

    @callback
    def reschedule(self) -> None:
        """Recompute the next run, for example after the tier changed."""
        if self._listeners:
            self._schedule_refresh()

    async def async_refresh_now(self, *, on_demand: bool = True) -> None:
        """Refresh immediately, using the on demand reserve by default."""
        self._force_fetch = True
        self._on_demand = on_demand
        try:
            await self.async_refresh()
        finally:
            self._force_fetch = False
            self._on_demand = False

    _on_demand = False

    # -- fetching ---------------------------------------------------------

    async def _async_setup(self) -> None:
        """Seed from the stored snapshot."""
        self.load_snapshot()

    @callback
    def load_snapshot(self) -> None:
        """Seed ``data`` from the stored snapshot, if there is one."""
        snapshot = self.snapshots.get(self.snapshot_key)
        if snapshot is not None and snapshot.data is not None:
            try:
                self.data = self.model.from_storage(snapshot.data)
            except TypeError, ValueError, KeyError:
                LOGGER.debug("Discarding unreadable %s snapshot", self.name)
                return
            self.fetched_at = snapshot.fetched_at

    def _is_fresh(self, now: datetime) -> bool:
        if self.fetched_at is None:
            return False
        interval, _ = self.interval(now)
        return now - self.fetched_at < interval

    async def _fetch(self, priority: Priority) -> DataT:
        raise NotImplementedError

    def _detect_changes(self, previous: DataT, current: DataT) -> None:
        """Compare two copies and dispatch events. Subclasses implement."""

    def _on_not_found(self) -> None:
        """Handle the resource no longer existing. Subclasses may raise repairs."""

    async def _async_update_data(self) -> DataT:
        now = dt_util.utcnow()
        first, self._first_run = self._first_run, False
        if (
            first
            and not self._force_fetch
            and self.data is not None
            and self._is_fresh(now)
        ):
            self.last_reason = "snapshot fresh"
            return self.data
        priority = (
            Priority.ON_DEMAND
            if self.data is None or self._on_demand
            else Priority.SCHEDULED
        )
        try:
            current = await self._fetch(priority)
        except CompaniesHouseAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except CompaniesHouseBudgetError as err:
            if self.data is None:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="budget_exhausted",
                    retry_after=err.retry_after,
                ) from err
            self._retry_after = err.retry_after
            self.last_reason = f"deferred ({err.reason})"
            LOGGER.debug("%s deferred: %s", self.name, err)
            return self.data
        except CompaniesHouseRateLimitError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="rate_limited",
                retry_after=err.retry_after,
            ) from err
        except CompaniesHouseNotFoundError as err:
            self._on_not_found()
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="not_found"
            ) from err
        except CompaniesHouseConnectionError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        previous = self.data
        self.fetched_at = now
        self.last_reason = "fetched"
        if previous is not None:
            self._detect_changes(previous, current)
        self.snapshots[self.snapshot_key] = DatasetSnapshot(
            fetched_at=now, data=current.to_storage()
        )
        self.store.save()
        return current


# ---------------------------------------------------------------- companies


class CompanyRuntime(_Runtime):
    """Everything set up for one monitored company."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        subentry: ConfigSubentry,
        client: CompaniesHouseClient,
        store: ChangeStore,
        *,
        company_number: str,
        close_watch: bool,
        datasets: set[Dataset],
    ) -> None:
        """Create the coordinators for a company."""
        super().__init__(hass, subentry)
        self.entry = entry
        self.company_number = company_number
        self.close_watch = close_watch
        self.datasets = datasets
        self.state: CompanyState = store.company(company_number)
        self.tier = Tier.NORMAL
        self.tier_reason = "not evaluated"
        self.probe = ProbeCoordinator(hass, entry, client, store, self)
        self.profile = ProfileCoordinator(hass, entry, client, store, self)
        self.officers = (
            OfficersCoordinator(hass, entry, client, store, self)
            if Dataset.OFFICERS in datasets
            else None
        )
        self.psc = (
            PscCoordinator(hass, entry, client, store, self)
            if Dataset.PSC in datasets
            else None
        )
        self.charges = (
            ChargesCoordinator(hass, entry, client, store, self)
            if Dataset.CHARGES in datasets
            else None
        )
        self.insolvency = (
            InsolvencyCoordinator(hass, entry, client, store, self)
            if Dataset.INSOLVENCY in datasets
            else None
        )
        self.structure = (
            StructureCoordinator(hass, entry, client, store, self)
            if Dataset.STRUCTURE in datasets
            else None
        )

    @property
    def coordinators(self) -> dict[Dataset, CompaniesHouseCoordinator[Any]]:
        """Return the coordinators that exist for this company."""
        optional = {
            Dataset.OFFICERS: self.officers,
            Dataset.PSC: self.psc,
            Dataset.CHARGES: self.charges,
            Dataset.INSOLVENCY: self.insolvency,
            Dataset.STRUCTURE: self.structure,
        }
        result: dict[Dataset, CompaniesHouseCoordinator[Any]] = {
            Dataset.FILINGS: self.probe,
            Dataset.PROFILE: self.profile,
        }
        result.update({d: c for d, c in optional.items() if c is not None})
        return result

    @property
    def company_name(self) -> str:
        """Return the current company name, or the number until known."""
        if self.profile.data is not None and self.profile.data.company_name:
            return self.profile.data.company_name
        return self.company_number

    def _bus_context(self) -> dict[str, Any]:
        return {
            "company_number": self.company_number,
            "company_name": self.company_name,
        }

    async def async_first_refresh(self) -> None:
        """Refresh every coordinator once at setup, tolerating failures.

        Snapshots are loaded first so a fresh one costs no request, and so
        changes since the last run are detected rather than replayed.
        """
        for coordinator in self.coordinators.values():
            coordinator.load_snapshot()
        for coordinator in self.coordinators.values():
            await coordinator.async_refresh()
        self.recompute_tier()

    @callback
    def recompute_tier(self) -> None:
        """Re-evaluate the adaptive tier and reschedule the probe if it changed."""
        decision = compute_tier(
            self.profile.data,
            close_watch=self.close_watch,
            last_filing_date=self.state.newest_filing_date,
            today=dt_util.now().date(),
        )
        changed = decision.tier is not self.tier
        self.tier, self.tier_reason = decision.tier, decision.reason
        if changed:
            LOGGER.debug(
                "%s (%s) tier -> %s: %s",
                self.company_name,
                self.company_number,
                self.tier,
                self.tier_reason,
            )
            self.probe.reschedule()
            self.profile.reschedule()
            async_dispatcher_send(self.hass, signal_tier(self.subentry.subentry_id))

    async def async_refresh_datasets(
        self, datasets: Iterable[Dataset], *, reason: str, on_demand: bool = False
    ) -> None:
        """Refresh a set of datasets now."""
        wanted = [d for d in datasets if d in self.coordinators]
        LOGGER.debug(
            "%s: refreshing %s (%s)",
            self.company_number,
            [d.value for d in wanted],
            reason,
        )
        for dataset in wanted:
            await self.coordinators[dataset].async_refresh_now(on_demand=on_demand)
        if Dataset.PROFILE in wanted:
            self.recompute_tier()

    @property
    def strike_off_proposed(self) -> bool:
        """Return True from the status detail or a first gazette notice."""
        profile = self.profile.data
        if profile is not None and profile.company_status in FINISHED_STATUSES:
            return False
        if (
            profile is not None
            and profile.company_status_detail == STATUS_DETAIL_STRIKE_OFF
        ):
            return True
        return self.state.strike_off_notice_on is not None

    async def async_shutdown(self) -> None:
        """Stop every coordinator."""
        for coordinator in self.coordinators.values():
            await coordinator.async_shutdown()


class _CompanyCoordinator[DataT: StorableModel](CompaniesHouseCoordinator[DataT]):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        client: CompaniesHouseClient,
        store: ChangeStore,
        company: CompanyRuntime,
    ) -> None:
        super().__init__(
            hass,
            entry,
            client,
            store,
            company.state.snapshots,
            name=f"{DOMAIN} {company.company_number} {self.snapshot_key}",
        )
        self.company = company

    @property
    def company_number(self) -> str:
        """Return the company number."""
        return self.company.company_number


class ProbeCoordinator(_CompanyCoordinator[FilingHistory]):
    """Probe the filing history and drive the other coordinators."""

    model = FilingHistory
    snapshot_key = Dataset.FILINGS.value

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialise with nothing pending."""
        super().__init__(*args, **kwargs)
        self._pending_refresh: set[Dataset] = set()
        self._pending_reason = "filing"

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Adaptive probe interval from the company's tier."""
        interval, when = probe_interval(self.company.tier, now, self.multiplier)
        return interval, f"{self.company.tier.value}, {when}"

    async def _fetch(self, priority: Priority) -> FilingHistory:
        state = self.company.state
        number = self.company_number
        seeding = state.filings_total_count is None
        history = await self.client.get_filing_history(
            number, items_per_page=PROBE_SEED_ITEMS if seeding else 1, priority=priority
        )
        newest = history.items[0] if history.items else None
        now = dt_util.utcnow()
        if seeding:
            # First ever look: remember what is there and fire nothing (8.4).
            state.remember_filings([item.transaction_id for item in history.items])
            state.recent_filings = [
                {
                    "transaction_id": i.transaction_id,
                    "date": i.date.isoformat() if i.date else None,
                    "category": i.category,
                }
                for i in history.items
            ]
            self._record_newest(history, newest)
            state.last_reconciled = now
            state.last_structure = now
            self.last_reason = "seeded"
            return history

        changed = history.total_count != state.filings_total_count or (
            newest is not None and newest.transaction_id != state.newest_transaction_id
        )
        new_items: list[FilingHistoryItem] = []
        if changed:
            delta = history.total_count - (state.filings_total_count or 0)
            if delta > 1:
                page = await self.client.get_filing_history(
                    number,
                    items_per_page=min(delta, PROBE_CATCH_UP_MAX),
                    priority=priority,
                )
                candidates = page.items
            else:
                candidates = history.items
            new_items = [
                item
                for item in candidates
                if item.transaction_id not in state.seen_transaction_ids
            ]
            self._record_newest(history, newest)
            state.remember_filings([item.transaction_id for item in candidates])
            for item in reversed(new_items):
                state.recent_filings.insert(
                    0,
                    {
                        "transaction_id": item.transaction_id,
                        "date": item.date.isoformat() if item.date else None,
                        "category": item.category,
                    },
                )
            del state.recent_filings[100:]
            for item in reversed(new_items):  # oldest first
                self._fire_filing(item)
            refresh: set[Dataset] = set()
            for item in new_items:
                refresh.update(
                    FILING_CATEGORY_REFRESH.get(
                        item.category or "", FILING_UNKNOWN_REFRESH
                    )
                )
            if changed and not new_items:
                refresh.add(Dataset.PROFILE)
            self._pending_refresh |= refresh
        if reconciliation_due(number, now, state.last_reconciled):
            self._pending_refresh |= set(Dataset) - {Dataset.FILINGS, Dataset.STRUCTURE}
            self._pending_reason = "weekly reconciliation"
            state.last_reconciled = now
        if structure_due(number, now, state.last_structure):
            self._pending_refresh.add(Dataset.STRUCTURE)
            state.last_structure = now
        return history

    def _record_newest(
        self, history: FilingHistory, newest: FilingHistoryItem | None
    ) -> None:
        state = self.company.state
        state.filings_total_count = history.total_count
        if newest is not None:
            state.newest_transaction_id = newest.transaction_id
            state.newest_filing_date = newest.date

    def _fire_filing(self, item: FilingHistoryItem) -> None:
        category = item.category or "other"
        event_type = FILING_CATEGORY_EVENT_TYPE.get(category, category)
        if event_type not in FILING_EVENT_TYPES:
            event_type = "other"
        self.company.dispatch(
            ChangeEvent(
                "filing",
                event_type,
                {
                    "transaction_id": item.transaction_id,
                    "date": item.date.isoformat() if item.date else None,
                    "description": item.description,
                    "rendered_description": item.rendered_description,
                    "category": item.category,
                    "subcategory": item.subcategory,
                    "type": item.type,
                    "barcode": item.barcode,
                    "document_id": item.document_id,
                    "paper_filed": item.paper_filed,
                    "pages": item.pages,
                },
            )
        )
        state = self.company.state
        if item.description in STRIKE_OFF_NOTICE_DESCRIPTIONS:
            state.strike_off_notice_on = item.date
            self.company.dispatch(
                ChangeEvent(
                    "status",
                    "strike-off-proposed",
                    {
                        "old_status": None,
                        "new_status": None,
                        "detail": item.rendered_description,
                    },
                )
            )
        elif item.description in STRIKE_OFF_DISCONTINUED_DESCRIPTIONS:
            state.strike_off_notice_on = None
            self.company.dispatch(
                ChangeEvent(
                    "status",
                    "strike-off-discontinued",
                    {
                        "old_status": None,
                        "new_status": None,
                        "detail": item.rendered_description,
                    },
                )
            )

    async def _async_update_data(self) -> FilingHistory:
        self._pending_refresh = set()
        self._pending_reason = "filing"
        history = await super()._async_update_data()
        if self._pending_refresh:
            pending, self._pending_refresh = self._pending_refresh, set()
            assert self.config_entry is not None
            self.config_entry.async_create_background_task(
                self.hass,
                self.company.async_refresh_datasets(
                    sorted(pending), reason=self._pending_reason
                ),
                name=f"{self.name} dispatch",
            )
        return history


class ProfileCoordinator(_CompanyCoordinator[CompanyProfile]):
    """The company profile: deadlines, status, name, address."""

    model = CompanyProfile
    snapshot_key = Dataset.PROFILE.value

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Daily, 6 hourly within 14 days of a deadline, monthly once dissolved."""
        return profile_interval(
            self.data, self.company.tier, dt_util.as_local(now).date(), self.multiplier
        )

    async def _fetch(self, priority: Priority) -> CompanyProfile:
        return await self.client.get_company(self.company_number, priority=priority)

    def _on_not_found(self) -> None:
        self.company.state.not_found = True
        async_raise_subentry_issue(
            self.hass,
            key="company_not_found",
            entry_id=self.company.entry.entry_id,
            subentry_id=self.company.subentry.subentry_id,
            placeholders={
                "number": self.company_number,
                "company": self.company.company_name,
            },
        )

    def _detect_changes(
        self, previous: CompanyProfile, current: CompanyProfile
    ) -> None:
        dispatch = self.company.dispatch
        if previous.company_status != current.company_status:
            payload = {
                "old_status": previous.company_status,
                "new_status": current.company_status,
                "detail": current.company_status_detail,
            }
            dispatch(ChangeEvent("status", "status-changed", payload))
            if current.company_status in FINISHED_STATUSES:
                dispatch(ChangeEvent("status", "dissolved", payload))
                self.company.state.strike_off_notice_on = None
        old_strike = previous.company_status_detail == STATUS_DETAIL_STRIKE_OFF
        new_strike = current.company_status_detail == STATUS_DETAIL_STRIKE_OFF
        if new_strike and not old_strike:
            dispatch(
                ChangeEvent(
                    "status",
                    "strike-off-proposed",
                    {
                        "old_status": previous.company_status,
                        "new_status": current.company_status,
                        "detail": current.company_status_detail,
                    },
                )
            )
        elif old_strike and not new_strike:
            self.company.state.strike_off_notice_on = None
            dispatch(
                ChangeEvent(
                    "status",
                    "strike-off-discontinued",
                    {
                        "old_status": previous.company_status,
                        "new_status": current.company_status,
                        "detail": current.company_status_detail,
                    },
                )
            )
        if previous.company_name != current.company_name:
            dispatch(
                ChangeEvent(
                    "profile",
                    "name-changed",
                    {
                        "old_value": previous.company_name,
                        "new_value": current.company_name,
                    },
                )
            )
        old_addr = (
            previous.registered_office_address.one_line()
            if previous.registered_office_address
            else None
        )
        new_addr = (
            current.registered_office_address.one_line()
            if current.registered_office_address
            else None
        )
        if old_addr != new_addr:
            dispatch(
                ChangeEvent(
                    "profile",
                    "address-changed",
                    {"old_value": old_addr, "new_value": new_addr},
                )
            )
        if previous.sic_codes != current.sic_codes:
            dispatch(
                ChangeEvent(
                    "profile",
                    "sic-changed",
                    {"old_value": previous.sic_codes, "new_value": current.sic_codes},
                )
            )
        old_ard = (previous.accounts.reference_day, previous.accounts.reference_month)
        new_ard = (current.accounts.reference_day, current.accounts.reference_month)
        if old_ard != new_ard:
            dispatch(
                ChangeEvent(
                    "profile",
                    "accounting-reference-date-changed",
                    {
                        "old_value": f"{old_ard[0]}/{old_ard[1]}"
                        if old_ard[0]
                        else None,
                        "new_value": f"{new_ard[0]}/{new_ard[1]}"
                        if new_ard[0]
                        else None,
                    },
                )
            )

    @callback
    def _async_refresh_finished(self) -> None:
        """Re-evaluate the tier once the new profile is in place, and manage issues."""
        self.company.recompute_tier()
        if not self.last_update_success or self.data is None:
            return
        if self.company.state.not_found:
            self.company.state.not_found = False
            async_clear_subentry_issue(
                self.hass, "company_not_found", self.company_number
            )
        if self.data.company_status in FINISHED_STATUSES:
            async_raise_subentry_issue(
                self.hass,
                key="company_dissolved",
                entry_id=self.company.entry.entry_id,
                subentry_id=self.company.subentry.subentry_id,
                placeholders={
                    "number": self.company_number,
                    "company": self.data.company_name,
                },
            )
        else:
            async_clear_subentry_issue(
                self.hass, "company_dissolved", self.company_number
            )


def _officer_payload(officer: Any) -> dict[str, Any]:
    return {
        "name": officer.name,
        "role": officer.officer_role,
        "appointed_on": (officer.appointed_on or officer.appointed_before).isoformat()
        if (officer.appointed_on or officer.appointed_before)
        else None,
        "resigned_on": officer.resigned_on.isoformat() if officer.resigned_on else None,
        "officer_id": officer.officer_id,
        "appointment_id": officer.appointment_id,
    }


class OfficersCoordinator(_CompanyCoordinator[OfficerList]):
    """Officer appointments; refreshed on officer filings and weekly."""

    model = OfficerList
    snapshot_key = Dataset.OFFICERS.value
    driven_by_probe = True

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Only the probe refreshes this; the nominal interval is the reconciliation."""
        return timedelta(days=7) * self.multiplier, "weekly reconciliation"

    async def _fetch(self, priority: Priority) -> OfficerList:
        return await self.client.get_officers(self.company_number, priority=priority)

    def _detect_changes(self, previous: OfficerList, current: OfficerList) -> None:
        before = {o.appointment_id or o.name: o for o in previous.items}
        for officer in current.items:
            key = officer.appointment_id or officer.name
            old = before.get(key)
            if old is None:
                self.company.dispatch(
                    ChangeEvent("officer", "appointed", _officer_payload(officer))
                )
            elif old.resigned_on is None and officer.resigned_on is not None:
                self.company.dispatch(
                    ChangeEvent("officer", "resigned", _officer_payload(officer))
                )
            elif old.details_hash != officer.details_hash:
                self.company.dispatch(
                    ChangeEvent("officer", "details-changed", _officer_payload(officer))
                )


def _psc_payload(psc: Any) -> dict[str, Any]:
    return {
        "name": psc.name,
        "psc_kind": psc.kind,
        "natures_of_control": list(psc.natures_of_control),
        "notified_on": psc.notified_on.isoformat() if psc.notified_on else None,
        "ceased_on": psc.ceased_on.isoformat() if psc.ceased_on else None,
        "notification_id": psc.notification_id,
    }


class PscCoordinator(_CompanyCoordinator[PscData]):
    """Persons with significant control and PSC statements."""

    model = PscData
    snapshot_key = Dataset.PSC.value
    driven_by_probe = True

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Only the probe refreshes this; the nominal interval is the reconciliation."""
        return timedelta(days=7) * self.multiplier, "weekly reconciliation"

    async def _fetch(self, priority: Priority) -> PscData:
        return await self.client.get_psc(self.company_number, priority=priority)

    def _detect_changes(self, previous: PscData, current: PscData) -> None:
        before = {p.notification_id or p.name: p for p in previous.items}
        for psc in current.items:
            old = before.get(psc.notification_id or psc.name)
            if old is None:
                self.company.dispatch(ChangeEvent("psc", "notified", _psc_payload(psc)))
            elif not old.ceased and psc.ceased:
                self.company.dispatch(ChangeEvent("psc", "ceased", _psc_payload(psc)))
            elif old.details_hash != psc.details_hash:
                self.company.dispatch(
                    ChangeEvent("psc", "details-changed", _psc_payload(psc))
                )
        old_statements = {s.statement_id for s in previous.statements}
        for statement in current.statements:
            if statement.statement_id not in old_statements:
                self.company.dispatch(
                    ChangeEvent(
                        "psc",
                        "statement-added",
                        {
                            "name": statement.linked_psc_name,
                            "psc_kind": "statement",
                            "natures_of_control": [],
                            "notified_on": statement.notified_on.isoformat()
                            if statement.notified_on
                            else None,
                            "ceased_on": statement.ceased_on.isoformat()
                            if statement.ceased_on
                            else None,
                            "statement": statement.statement,
                        },
                    )
                )


def _charge_payload(charge: Any) -> dict[str, Any]:
    return {
        "charge_code": charge.charge_code,
        "charge_id": charge.charge_id,
        "persons_entitled": list(charge.persons_entitled),
        "created_on": charge.created_on.isoformat() if charge.created_on else None,
        "delivered_on": charge.delivered_on.isoformat()
        if charge.delivered_on
        else None,
        "satisfied_on": charge.satisfied_on.isoformat()
        if charge.satisfied_on
        else None,
        "status": charge.status,
    }


class ChargesCoordinator(_CompanyCoordinator[ChargeList]):
    """Registered charges."""

    model = ChargeList
    snapshot_key = Dataset.CHARGES.value
    driven_by_probe = True

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Only the probe refreshes this; the nominal interval is the reconciliation."""
        return timedelta(days=7) * self.multiplier, "weekly reconciliation"

    async def _fetch(self, priority: Priority) -> ChargeList:
        return await self.client.get_charges(self.company_number, priority=priority)

    def _detect_changes(self, previous: ChargeList, current: ChargeList) -> None:
        before = {c.charge_id or c.charge_code: c for c in previous.items}
        for charge in current.items:
            old = before.get(charge.charge_id or charge.charge_code)
            if old is None:
                event_type = "acquired" if charge.acquired_on else "created"
                self.company.dispatch(
                    ChangeEvent("charge", event_type, _charge_payload(charge))
                )
            elif old.status != charge.status:
                if charge.status in ("fully-satisfied", "satisfied"):
                    self.company.dispatch(
                        ChangeEvent("charge", "satisfied", _charge_payload(charge))
                    )
                elif charge.status == "part-satisfied":
                    self.company.dispatch(
                        ChangeEvent("charge", "part-satisfied", _charge_payload(charge))
                    )


class InsolvencyCoordinator(_CompanyCoordinator[Insolvency]):
    """Insolvency cases."""

    model = Insolvency
    snapshot_key = Dataset.INSOLVENCY.value
    driven_by_probe = True

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Only the probe refreshes this; the nominal interval is the reconciliation."""
        return timedelta(days=7) * self.multiplier, "weekly reconciliation"

    async def _fetch(self, priority: Priority) -> Insolvency:
        return await self.client.get_insolvency(self.company_number, priority=priority)


class StructureCoordinator(_CompanyCoordinator[Structure]):
    """Registers, exemptions and UK establishments, monthly."""

    model = Structure
    snapshot_key = Dataset.STRUCTURE.value
    driven_by_probe = True

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Monthly."""
        return timedelta(days=30) * self.multiplier, "monthly"

    async def _fetch(self, priority: Priority) -> Structure:
        return await self.client.get_structure(self.company_number, priority=priority)


# ---------------------------------------------------------------- officers


class OfficerRuntime(_Runtime):
    """Everything set up for one tracked officer."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        subentry: ConfigSubentry,
        client: CompaniesHouseClient,
        store: ChangeStore,
        *,
        officer_id: str,
        officer_name: str,
        date_of_birth: DateOfBirth | None,
    ) -> None:
        """Create the coordinators for an officer."""
        super().__init__(hass, subentry)
        self.entry = entry
        self.officer_id = officer_id
        self.configured_name = officer_name
        self.date_of_birth = date_of_birth
        self.state: OfficerState = store.officer(officer_id)
        self.appointments = AppointmentsCoordinator(hass, entry, client, store, self)
        self.disqualification = DisqualificationCoordinator(
            hass, entry, client, store, self
        )

    @property
    def coordinators(self) -> dict[OfficerDataset, CompaniesHouseCoordinator[Any]]:
        """Return the officer coordinators."""
        return {
            OfficerDataset.APPOINTMENTS: self.appointments,
            OfficerDataset.DISQUALIFICATION: self.disqualification,
        }

    @property
    def officer_name(self) -> str:
        """Return the name from the register, or the configured one."""
        if self.appointments.data is not None and self.appointments.data.name:
            return self.appointments.data.name
        return self.configured_name

    def _bus_context(self) -> dict[str, Any]:
        return {"officer_id": self.officer_id, "officer_name": self.officer_name}

    async def async_first_refresh(self) -> None:
        """Refresh both coordinators once at setup."""
        for coordinator in self.coordinators.values():
            coordinator.load_snapshot()
        for coordinator in self.coordinators.values():
            await coordinator.async_refresh()

    async def async_shutdown(self) -> None:
        """Stop both coordinators."""
        for coordinator in self.coordinators.values():
            await coordinator.async_shutdown()


class _OfficerCoordinator[DataT: StorableModel](CompaniesHouseCoordinator[DataT]):
    period: timedelta

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        client: CompaniesHouseClient,
        store: ChangeStore,
        officer: OfficerRuntime,
    ) -> None:
        super().__init__(
            hass,
            entry,
            client,
            store,
            officer.state.snapshots,
            name=f"{DOMAIN} officer {officer.officer_id} {self.snapshot_key}",
        )
        self.officer = officer

    def _slot(self, now: datetime) -> datetime:
        raise NotImplementedError

    def interval(self, now: datetime) -> tuple[timedelta, str]:
        """Nominal period, scaled by the multiplier."""
        return self.period * self.multiplier, "hash spread"

    def _delay(self, now: datetime) -> tuple[timedelta, str]:
        """Run at the officer's hash-spread slot rather than a plain interval."""
        if self._retry_after is not None:
            delay, self._retry_after = timedelta(seconds=self._retry_after), None
            return delay, "retry after"
        slot = self._slot(now)
        if self.multiplier > 1:
            slot = slot + (self.period * (self.multiplier - 1))
        return max(slot - now, timedelta(minutes=1)), "hash spread slot"


def _appointment_payload(appointment: Any) -> dict[str, Any]:
    return {
        "company_number": appointment.company_number,
        "company_name": appointment.company_name,
        "company_status": appointment.company_status,
        "role": appointment.officer_role,
        "appointed_on": (
            appointment.appointed_on or appointment.appointed_before
        ).isoformat()
        if (appointment.appointed_on or appointment.appointed_before)
        else None,
        "resigned_on": appointment.resigned_on.isoformat()
        if appointment.resigned_on
        else None,
    }


class AppointmentsCoordinator(_OfficerCoordinator[AppointmentList]):
    """An officer's appointments across every company, daily."""

    model = AppointmentList
    snapshot_key = OfficerDataset.APPOINTMENTS.value
    period = timedelta(days=1)

    def _slot(self, now: datetime) -> datetime:
        return appointments_next_run(self.officer.officer_id, now, self.fetched_at)

    async def _fetch(self, priority: Priority) -> AppointmentList:
        return await self.client.get_officer_appointments(
            self.officer.officer_id, priority=priority
        )

    def _on_not_found(self) -> None:
        self.officer.state.not_found = True
        async_raise_subentry_issue(
            self.hass,
            key="officer_not_found",
            entry_id=self.officer.entry.entry_id,
            subentry_id=self.officer.subentry.subentry_id,
            placeholders={
                "officer_id": self.officer.officer_id,
                "name": self.officer.officer_name,
            },
        )

    @callback
    def _async_refresh_finished(self) -> None:
        """Clear the not found issue once the officer resolves again."""
        if (
            self.last_update_success
            and self.data is not None
            and self.officer.state.not_found
        ):
            self.officer.state.not_found = False
            async_clear_subentry_issue(
                self.hass, "officer_not_found", self.officer.officer_id
            )

    def _detect_changes(
        self, previous: AppointmentList, current: AppointmentList
    ) -> None:
        before = {a.key: a for a in previous.items}
        for appointment in current.items:
            old = before.get(appointment.key)
            if old is None:
                self.officer.dispatch(
                    ChangeEvent(
                        "appointment", "appointed", _appointment_payload(appointment)
                    )
                )
            elif old.resigned_on is None and appointment.resigned_on is not None:
                self.officer.dispatch(
                    ChangeEvent(
                        "appointment", "resigned", _appointment_payload(appointment)
                    )
                )
            elif old.company_status != appointment.company_status:
                self.officer.dispatch(
                    ChangeEvent(
                        "appointment",
                        "company-status-changed",
                        {
                            **_appointment_payload(appointment),
                            "old_status": old.company_status,
                        },
                    )
                )


def _normalise_name(name: str) -> tuple[str, str]:
    """Split a register name into (surname, first forename), casefolded.

    Handles ``SURNAME, Forename Other`` and ``Forename Other SURNAME``. The
    register prints surnames in capitals, which is the tie breaker for the
    second form.
    """
    cleaned = " ".join(name.replace(".", " ").split())
    if "," in cleaned:
        surname, _, rest = cleaned.partition(",")
        first = rest.split()
        return surname.strip().casefold(), (first[0].casefold() if first else "")
    parts = [p for p in cleaned.split() if p.casefold() not in _TITLES]
    if not parts:
        return "", ""
    capitals = [p for p in parts if p.isupper() and len(p) > 1]
    surname = capitals[-1] if capitals else parts[-1]
    others = [p for p in parts if p != surname]
    return surname.casefold(), (others[0].casefold() if others else "")


_TITLES = frozenset(
    {"mr", "mrs", "ms", "miss", "dr", "sir", "dame", "lord", "lady", "prof"}
)


def match_disqualification(
    officer_name: str,
    date_of_birth: DateOfBirth | None,
    search: JsonDict,
) -> tuple[DisqualificationMatch | None, list[DisqualificationMatch]]:
    """Return the exact match, if any, and every candidate.

    Exact means surname, first forename and month and year of birth all agree.
    Anything weaker is only a possible match and never flips the sensor.
    """
    target_surname, target_forename = _normalise_name(officer_name)
    exact: DisqualificationMatch | None = None
    candidates: list[DisqualificationMatch] = []
    for item in search.get("items") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        dob = DateOfBirth.from_api(item.get("date_of_birth"))
        raw_links = item.get("links")
        links: JsonDict = raw_links if isinstance(raw_links, dict) else {}
        candidate_id = str(links.get("self", "")).rstrip("/").rsplit("/", 1)[-1] or None
        surname, forename = _normalise_name(title)
        names_match = (
            surname == target_surname and forename == target_forename and bool(surname)
        )
        dob_match = (
            date_of_birth is not None
            and dob is not None
            and date_of_birth.month == dob.month
            and date_of_birth.year == dob.year
        )
        match = DisqualificationMatch(
            title=title,
            date_of_birth=dob,
            address_snippet=item.get("address_snippet")
            if isinstance(item.get("address_snippet"), str)
            else None,
            disqualified_officer_id=candidate_id,
            exact=names_match and dob_match,
        )
        if match.exact and exact is None:
            exact = match
        else:
            candidates.append(match)
    return exact, candidates


class DisqualificationCoordinator(_OfficerCoordinator[DisqualificationResult]):
    """Weekly check of the disqualified directors register, matched honestly."""

    model = DisqualificationResult
    snapshot_key = OfficerDataset.DISQUALIFICATION.value
    period = timedelta(days=7)

    def _slot(self, now: datetime) -> datetime:
        return disqualification_next_run(self.officer.officer_id, now, self.fetched_at)

    async def _fetch(self, priority: Priority) -> DisqualificationResult:
        name = self.officer.officer_name
        dob = self.officer.date_of_birth
        appointments = self.officer.appointments.data
        if appointments is not None and appointments.date_of_birth is not None:
            dob = appointments.date_of_birth
        search = await self.client.search_disqualified_officers(name, priority=priority)
        exact, candidates = match_disqualification(name, dob, search)
        result = DisqualificationResult(
            checked_on=dt_util.now().date(),
            disqualified=exact is not None,
            possible_matches=candidates[:10],
        )
        if exact is not None and exact.disqualified_officer_id:
            detail = await self.client.get_disqualification(
                exact.disqualified_officer_id, "natural", priority=priority
            )
            disqualifications = (detail or {}).get("disqualifications") or []
            latest: JsonDict = (
                disqualifications[-1]
                if disqualifications and isinstance(disqualifications[-1], dict)
                else {}
            )
            raw_reason = latest.get("reason")
            reason: JsonDict = raw_reason if isinstance(raw_reason, dict) else {}
            result = DisqualificationResult(
                checked_on=result.checked_on,
                disqualified=True,
                possible_matches=result.possible_matches,
                disqualified_from=parse_date(latest.get("disqualified_from")),
                disqualified_until=parse_date(latest.get("disqualified_until")),
                reason=reason.get("description_identifier")
                if isinstance(reason.get("description_identifier"), str)
                else None,
                company_names=[str(n) for n in latest.get("company_names") or []],
            )
        return result

    def _detect_changes(
        self, previous: DisqualificationResult, current: DisqualificationResult
    ) -> None:
        if current.disqualified and not previous.disqualified:
            self.officer.dispatch(
                ChangeEvent(
                    "appointment",
                    "disqualified",
                    {
                        "disqualified_from": current.disqualified_from.isoformat()
                        if current.disqualified_from
                        else None,
                        "disqualified_until": current.disqualified_until.isoformat()
                        if current.disqualified_until
                        else None,
                        "reason": current.reason,
                        "company_names": current.company_names,
                    },
                )
            )


# ---------------------------------------------------------------- account


@dataclass(frozen=True)
class AccountStatus:
    """Diagnostics for the service device."""

    rate_limit: RateLimitStatus
    last_success: datetime | None
    last_error: str | None
    companies_monitored: int
    officers_monitored: int
    next_probe: datetime | None
    next_probe_company: str | None


class AccountCoordinator(DataUpdateCoordinator[AccountStatus]):
    """Snapshots the limiter every minute. Makes no requests of its own."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: CompaniesHouseConfigEntry,
        client: CompaniesHouseClient,
        companies: Callable[[], Iterable[CompanyRuntime]],
        officers: Callable[[], Iterable[OfficerRuntime]],
    ) -> None:
        """Initialise with accessors for the runtimes."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} account",
            update_interval=ACCOUNT_INTERVAL,
        )
        self.client = client
        self._companies = companies
        self._officers = officers

    def next_probe(self) -> tuple[datetime, str] | None:
        """Return the soonest scheduled probe and its company, live."""
        soonest: tuple[datetime, str] | None = None
        for company in self._companies():
            if company.probe.next_run is not None and (
                soonest is None or company.probe.next_run < soonest[0]
            ):
                soonest = (company.probe.next_run, company.company_name)
        return soonest

    async def _async_update_data(self) -> AccountStatus:
        assert self.config_entry is not None
        if self.client.limiter.persistently_throttled():
            async_raise_rate_limited(self.hass, self.config_entry.entry_id)
        else:
            async_clear_rate_limited(self.hass, self.config_entry.entry_id)
        companies = list(self._companies())
        next_probe = self.next_probe()
        return AccountStatus(
            rate_limit=self.client.limiter.status(),
            last_success=self.client.last_success,
            last_error=self.client.last_error,
            companies_monitored=len(companies),
            officers_monitored=len(list(self._officers())),
            next_probe=next_probe[0] if next_probe else None,
            next_probe_company=next_probe[1] if next_probe else None,
        )


__all__ = [
    "AccountCoordinator",
    "AccountStatus",
    "AppointmentsCoordinator",
    "ChangeEvent",
    "ChargesCoordinator",
    "CompaniesHouseConfigEntry",
    "CompaniesHouseCoordinator",
    "CompanyRuntime",
    "DisqualificationCoordinator",
    "InsolvencyCoordinator",
    "OfficerRuntime",
    "OfficersCoordinator",
    "ProbeCoordinator",
    "ProfileCoordinator",
    "PscCoordinator",
    "StructureCoordinator",
    "match_disqualification",
    "signal_changes",
    "signal_tier",
]
