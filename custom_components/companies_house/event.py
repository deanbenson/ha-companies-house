"""Event entities: one per kind of change on each company and officer."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    APPOINTMENT_EVENT_TYPES,
    CHARGE_CHANGE_EVENT_TYPES,
    FILING_EVENT_TYPES,
    OFFICER_CHANGE_EVENT_TYPES,
    PROFILE_CHANGE_EVENT_TYPES,
    PSC_CHANGE_EVENT_TYPES,
    STATUS_CHANGE_EVENT_TYPES,
    Dataset,
    OfficerDataset,
)
from .coordinator import (
    ChangeEvent,
    CompaniesHouseConfigEntry,
    CompanyRuntime,
    OfficerRuntime,
    signal_changes,
    signal_new_company,
    signal_new_officer,
)
from .entity import CompanyEntity, OfficerEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CompanyEventDescription(EventEntityDescription):
    """Describes a company event entity."""

    kind: str
    dataset: Dataset


COMPANY_EVENTS: tuple[CompanyEventDescription, ...] = (
    CompanyEventDescription(
        key="filing",
        kind="filing",
        dataset=Dataset.FILINGS,
        event_types=FILING_EVENT_TYPES,
    ),
    CompanyEventDescription(
        key="officer_change",
        kind="officer",
        dataset=Dataset.OFFICERS,
        event_types=OFFICER_CHANGE_EVENT_TYPES,
    ),
    CompanyEventDescription(
        key="psc_change",
        kind="psc",
        dataset=Dataset.PSC,
        event_types=PSC_CHANGE_EVENT_TYPES,
    ),
    CompanyEventDescription(
        key="charge_change",
        kind="charge",
        dataset=Dataset.CHARGES,
        event_types=CHARGE_CHANGE_EVENT_TYPES,
    ),
    CompanyEventDescription(
        key="status_change",
        kind="status",
        dataset=Dataset.PROFILE,
        event_types=STATUS_CHANGE_EVENT_TYPES,
    ),
    CompanyEventDescription(
        key="profile_change",
        kind="profile",
        dataset=Dataset.PROFILE,
        event_types=PROFILE_CHANGE_EVENT_TYPES,
    ),
)


class _ChangeEventEntity(EventEntity):
    """Common dispatcher wiring."""

    kind: str
    subentry_id: str

    async def async_added_to_hass(self) -> None:
        """Subscribe to change events for this subentry."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_changes(self.subentry_id), self._on_change
            )
        )

    @callback
    def _on_change(self, event: ChangeEvent) -> None:
        if event.kind != self.kind or event.event_type not in self.event_types:
            return
        self._trigger_event(event.event_type, event.payload)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Event entities stay available; they carry the last event, not live data."""
        return True


class CompanyEventEntity(CompanyEntity[object], _ChangeEventEntity):  # type: ignore[type-var]
    """A company event entity."""

    entity_description: CompanyEventDescription

    def __init__(
        self, company: CompanyRuntime, description: CompanyEventDescription
    ) -> None:
        """Bind to the company."""
        super().__init__(
            company, company.coordinators[description.dataset], description
        )
        self.kind = description.kind
        self.subentry_id = company.subentry.subentry_id

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator updates do not change an event entity."""


class OfficerEventEntity(OfficerEntity[object], _ChangeEventEntity):  # type: ignore[type-var]
    """The officer appointment event entity."""

    def __init__(self, officer: OfficerRuntime) -> None:
        """Bind to the officer."""
        super().__init__(
            officer,
            officer.coordinators[OfficerDataset.APPOINTMENTS],
            EventEntityDescription(
                key="appointment", event_types=APPOINTMENT_EVENT_TYPES
            ),
        )
        self.kind = "appointment"
        self.subentry_id = officer.subentry.subentry_id

    @callback
    def _handle_coordinator_update(self) -> None:
        """Coordinator updates do not change an event entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the event entities, and add more as companies and officers are added."""
    runtime = entry.runtime_data

    @callback
    def _add_company(company: CompanyRuntime) -> None:
        async_add_entities(
            (
                CompanyEventEntity(company, description)
                for description in COMPANY_EVENTS
                if description.dataset in company.coordinators
            ),
            config_subentry_id=company.subentry.subentry_id,
        )

    @callback
    def _add_officer(officer: OfficerRuntime) -> None:
        async_add_entities(
            [OfficerEventEntity(officer)],
            config_subentry_id=officer.subentry.subentry_id,
        )

    for company in runtime.companies.values():
        _add_company(company)
    for officer in runtime.officers.values():
        _add_officer(officer)
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_company(entry.entry_id), _add_company)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_officer(entry.entry_id), _add_officer)
    )
