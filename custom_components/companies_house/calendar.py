"""Calendars: one per company, plus an aggregate on the service device."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityDescription,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import Dataset
from .coordinator import CompaniesHouseConfigEntry, CompanyRuntime, ProfileCoordinator
from .entity import CompanyEntity, ServiceEntity
from .scheduler import to_london

PARALLEL_UPDATES = 0

DEADLINE_LABELS = {
    "accounts_due": "Accounts due",
    "confirmation_statement_due": "Confirmation statement due",
    "accounts_period_end": "Accounts period end",
    "accounting_reference_date": "Accounting reference date",
}


def company_events(
    company: CompanyRuntime, *, prefix: bool, years: int = 2
) -> list[CalendarEvent]:
    """Return the all-day deadline events for a company."""
    profile = company.profile.data
    if profile is None:
        return []
    name = company.company_name
    number = company.company_number

    def _event(kind: str, when: date | None) -> CalendarEvent | None:
        if when is None:
            return None
        label = DEADLINE_LABELS[kind]
        summary = f"{name} - {label}" if prefix else label
        return CalendarEvent(
            start=when,
            end=when + timedelta(days=1),
            summary=summary,
            description=f"{name} ({number}): {label.lower()} on {when.isoformat()}",
            uid=f"{number}-{kind}-{when.isoformat()}",
        )

    events = [
        _event("accounts_due", profile.accounts.next_due),
        _event("confirmation_statement_due", profile.confirmation_statement.next_due),
        _event("accounts_period_end", profile.accounts.next_period_end),
    ]
    ard = profile.accounts
    if ard.reference_day and ard.reference_month:
        today = dt_util.now().date()
        for year in range(today.year, today.year + years):
            try:
                events.append(
                    _event(
                        "accounting_reference_date",
                        date(year, ard.reference_month, ard.reference_day),
                    )
                )
            except ValueError:
                continue
    return sorted((e for e in events if e is not None), key=lambda e: e.start)


def _in_range(event: CalendarEvent, start: datetime, end: datetime) -> bool:
    event_start = to_london(
        datetime.combine(
            event.start, datetime.min.time(), tzinfo=to_london(start).tzinfo
        )
    )
    event_end = event_start + timedelta(days=1)
    return event_start < end and event_end > start


def _next_event(events: list[CalendarEvent]) -> CalendarEvent | None:
    today = dt_util.now().date()
    upcoming = [e for e in events if isinstance(e.start, date) and e.start >= today]
    return upcoming[0] if upcoming else None


class CompanyCalendar(CompanyEntity[ProfileCoordinator], CalendarEntity):
    """The deadlines of one company."""

    def __init__(self, company: CompanyRuntime) -> None:
        """Bind to the profile coordinator."""
        super().__init__(
            company, company.profile, CalendarEntityDescription(key="deadlines")
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next deadline."""
        return _next_event(company_events(self.company, prefix=False))

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return the deadlines in a range."""
        return [
            e
            for e in company_events(self.company, prefix=False)
            if _in_range(e, start_date, end_date)
        ]


class AggregateCalendar(ServiceEntity, CalendarEntity):
    """Every monitored company's deadlines, for the dashboard."""

    def __init__(self, entry: CompaniesHouseConfigEntry) -> None:
        """Bind to the account coordinator."""
        super().__init__(
            entry,
            entry.runtime_data.account,
            CalendarEntityDescription(key="all_deadlines"),
        )

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for company in self.entry.runtime_data.companies.values():
            events.extend(company_events(company, prefix=True))
        return sorted(events, key=lambda e: e.start)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next deadline across the portfolio."""
        return _next_event(self._events())

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Return every deadline in a range."""
        return [e for e in self._events() if _in_range(e, start_date, end_date)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the calendars."""
    runtime = entry.runtime_data
    async_add_entities([AggregateCalendar(entry)])
    for subentry_id, company in runtime.companies.items():
        if Dataset.PROFILE in company.coordinators:
            async_add_entities(
                [CompanyCalendar(company)], config_subentry_id=subentry_id
            )
