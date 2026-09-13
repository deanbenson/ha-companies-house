"""Tests for the per company and aggregate calendars."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.companies_house.calendar import company_events
from custom_components.companies_house.scheduler import LONDON


async def test_company_calendar_events(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """Each company calendar carries its deadlines as all-day events with stable uids."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(["12345678"])
    company = entry.runtime_data.companies["sub_12345678"]
    events = company_events(company, prefix=False)
    summaries = [(e.summary, e.start.isoformat()) for e in events]
    assert ("Accounts period end", "2026-03-31") in summaries
    assert ("Accounts due", "2026-12-31") in summaries
    assert ("Confirmation statement due", "2027-03-25") in summaries
    assert ("Accounting reference date", "2026-03-31") in summaries
    assert ("Accounting reference date", "2027-03-31") in summaries
    uids = [e.uid for e in events]
    assert "12345678-accounts_due-2026-12-31" in uids
    assert len(uids) == len(set(uids))
    assert all("12345678" in (e.description or "") for e in events)
    state = hass.states.get("calendar.example_trading_limited_deadlines")
    assert state is not None
    assert state.state == "off"
    assert state.attributes["message"] == "Accounts due"
    assert state.attributes["start_time"] == "2026-12-31 00:00:00"
    assert state.attributes["all_day"] is True


async def test_aggregate_calendar_and_range_queries(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """The aggregate calendar lists every company with prefixed titles."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    await setup_entry(["12345678", "34567890"])
    state = hass.states.get("calendar.companies_house_all_deadlines")
    assert state is not None
    assert state.attributes["message"] == "SUNSET RETAIL LIMITED - Accounts due"
    aggregate = hass.data["calendar"].get_entity(
        "calendar.companies_house_all_deadlines"
    )
    assert aggregate is not None
    events = await aggregate.async_get_events(
        hass, datetime(2026, 9, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    )
    assert [(e.start.isoformat(), e.summary) for e in events] == [
        ("2026-09-30", "SUNSET RETAIL LIMITED - Accounts due"),
        ("2026-12-31", "EXAMPLE TRADING LIMITED - Accounts due"),
        ("2026-12-31", "SUNSET RETAIL LIMITED - Accounting reference date"),
    ]
    assert events[0].start == date(2026, 9, 30)
    assert events[0].end == date(2026, 10, 1)
    single = hass.data["calendar"].get_entity(
        "calendar.example_trading_limited_deadlines"
    )
    assert single is not None
    events = await single.async_get_events(
        hass, datetime(2026, 12, 31, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    )
    assert [e.summary for e in events] == ["Accounts due"]
    # A range in BST around an end of October date does not shift the day.
    events = await single.async_get_events(
        hass,
        datetime(2026, 10, 30, 23, tzinfo=UTC),
        datetime(2026, 10, 31, 23, tzinfo=UTC),
    )
    assert events == []
    events = await single.async_get_events(
        hass,
        datetime(2026, 12, 30, 23, tzinfo=UTC),
        datetime(2026, 12, 31, 23, tzinfo=UTC),
    )
    assert [e.summary for e in events] == ["Accounts due"]


async def test_calendar_without_deadlines(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """A dissolved company with no deadlines has an empty calendar."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(["23456789"])
    company = entry.runtime_data.companies["sub_23456789"]
    events = company_events(company, prefix=True)
    assert [e.summary for e in events] == [
        "OLD VENTURES LIMITED - Accounting reference date",
        "OLD VENTURES LIMITED - Accounting reference date",
    ]
    state = hass.states.get("calendar.old_ventures_limited_deadlines")
    assert state is not None
    assert state.attributes["message"] == "Accounting reference date"
    company.profile.data = None
    assert company_events(company, prefix=False) == []
    assert datetime(2026, 9, 15, tzinfo=LONDON).date().isoformat() == "2026-09-15"
