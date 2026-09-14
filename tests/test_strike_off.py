"""Tests for the strike-off countdown: probe, profile, back-fill, entities, report."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from types import MappingProxyType
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.calendar import company_events
from custom_components.companies_house.const import DOMAIN, EVENT_COMPANIES_HOUSE
from custom_components.companies_house.coordinator import CompanyRuntime
from custom_components.companies_house.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.companies_house.digest import (
    build_digest,
    describe_change,
    render_html,
    render_text,
)
from custom_components.companies_house.gazette import (
    CAVEAT_SUSPENDED,
    CAVEAT_TWO_MONTHS,
    CAVEAT_UNKNOWN,
    notice_link,
)
from custom_components.companies_house.store import CompanyState

from .conftest import company_subentry, load_fixture, mock_company
from .test_coordinator import _new_filing

ACTIVE = "12345678"
STRIKE_OFF = "45678901"
SUSPENDED = "10916685"
EARLIEST = "sensor.example_trading_limited_earliest_strike_off_date"
DAYS = "sensor.example_trading_limited_days_to_object_to_strike_off"
PROPOSED = "binary_sensor.example_trading_limited_proposed_strike_off"


def _company(entry: Any, number: str) -> CompanyRuntime:
    company: CompanyRuntime = entry.runtime_data.companies[f"sub_{number}"]
    return company


def _status_events(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.data["kind"] == "status"]


async def _add_company(
    hass: HomeAssistant,
    entry: Any,
    aioclient_mock: AiohttpClientMocker,
    number: str,
    *,
    fixture: str,
    overrides: dict[str, Any] | None = None,
) -> CompanyRuntime:
    """Add a company from a fixture directory that is not in the standard set."""
    mock_company(aioclient_mock, number, fixture=fixture, overrides=overrides)
    name = load_fixture(f"{fixture}/profile")["company_name"]
    data = company_subentry(number, name=name, datasets=[], subentry_id=f"sub_{number}")
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(dict(data["data"])),
            subentry_type=data["subentry_type"],
            title=data["title"],
            unique_id=data["unique_id"],
            subentry_id=data["subentry_id"],  # type: ignore[typeddict-item]
        ),
    )
    await hass.async_block_till_done()
    return _company(entry, number)


async def test_notice_suspension_and_discontinuation(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A Gazette notice starts the clock, a suspension holds it, DISS40 stops it."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry, ACTIVE)
    assert company.strike_off_countdown() is None
    assert hass.states.get(EARLIEST).state == "unknown"
    assert hass.states.get(DAYS).state == "unknown"
    assert hass.states.get(DAYS).attributes["caveat"] is None
    assert hass.states.get(PROPOSED).attributes["link"] is None

    # A first Gazette notice (voluntary), dated 14 September.
    history = _new_filing(
        load_fixture("company_active/filing_history"),
        category="gazette",
        type="GAZ1",
        description="gazette-notice-voluntary",
        description_values={},
        paper_filed=True,
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert proposed["detail"] == "First Gazette notice for voluntary strike-off"
    assert proposed["strike_off_kind"] == "voluntary"
    assert proposed["notice_on"] == "2026-09-14"
    assert proposed["earliest_on"] == "2026-11-14"
    assert proposed["objection_deadline"] == "2026-10-31"
    assert proposed["suspended_on"] is None
    assert proposed["transaction_id"] == "NEWTRANSACTION0001"
    assert "caveat" not in proposed
    assert proposed["kind"] == "status"  # the change kind is not overwritten
    state = company.state
    assert state.strike_off_notice_on == date(2026, 9, 14)
    assert state.strike_off_kind == "voluntary"
    assert state.strike_off_transaction_id == "NEWTRANSACTION0001"
    assert state.strike_off_suspended_on is None
    countdown = company.strike_off_countdown()
    assert countdown is not None
    assert countdown.days_to_object == 46
    assert hass.states.get(EARLIEST).state == "2026-11-14"
    assert hass.states.get(DAYS).state == "46"
    attrs = hass.states.get(DAYS).attributes
    assert attrs["kind"] == "voluntary"
    assert attrs["notice_on"] == "2026-09-14"
    assert attrs["objection_deadline"] == "2026-10-31"
    assert attrs["earliest_on"] == "2026-11-14"
    assert attrs["suspended_on"] is None
    assert attrs["transaction_id"] == "NEWTRANSACTION0001"
    assert attrs["link"] == notice_link(ACTIVE, "NEWTRANSACTION0001")
    assert attrs["caveat"] == CAVEAT_TWO_MONTHS
    binary = hass.states.get(PROPOSED)
    assert binary.state == "on"
    assert binary.attributes["gazette_notice_on"] == "2026-09-14"
    assert binary.attributes["objection_deadline"] == "2026-10-31"
    assert binary.attributes["link"] == notice_link(ACTIVE, "NEWTRANSACTION0001")
    calendar = {
        (e.summary, e.start.isoformat()) for e in company_events(company, prefix=False)
    }
    assert ("Objection deadline", "2026-10-31") in calendar
    assert ("Earliest strike-off", "2026-11-14") in calendar
    uids = [e.uid for e in company_events(company, prefix=False)]
    assert f"{ACTIVE}-objection_deadline-2026-10-31" in uids
    assert f"{ACTIVE}-strike_off_earliest-2026-11-14" in uids

    # The alert text carries the clock and links to the notice PDF.
    title, message, link = describe_change(
        "status", "strike-off-proposed", proposed, subject="X", number=ACTIVE
    )
    assert title == "X: strike-off proposed"
    assert message == (
        "First Gazette notice for voluntary strike-off on 14 Sep 2026. The company "
        "can be struck off from 14 Nov 2026; object by 31 Oct 2026."
    )
    assert link == notice_link(ACTIVE, "NEWTRANSACTION0001")

    # The profile catching up with the same news fires nothing more.
    events.clear()
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert _status_events(events) == []
    assert company.state.strike_off_notice_on == date(2026, 9, 14)

    # The report: a new problem with the countdown and the notice link, and
    # the objection deadline is not yet inside the 30 day horizon.
    digest = build_digest(entry, days=7)
    (card,) = [c for c in digest["companies"] if c["number"] == ACTIVE]
    assert card["strike_off"]["summary"] == (
        "Strike-off proposed (voluntary) — 46 days to object"
    )
    assert card["strike_off"]["link"] == notice_link(ACTIVE, "NEWTRANSACTION0001")
    (attention,) = card["attention"]
    assert attention["issue"] == "Strike-off proposed (voluntary) — 46 days to object"
    assert attention["short"] == "Strike-off proposed"
    assert attention["new"] is True
    assert attention["since"] == "2026-09-14"
    assert attention["links"] == [
        {"text": "Gazette notice", "href": notice_link(ACTIVE, "NEWTRANSACTION0001")}
    ]
    assert digest["needs_attention"][0]["links"] == attention["links"]
    assert [d["what"] for d in digest["deadlines"]] == []
    html = render_html(digest, title="T")
    assert "Strike-off proposed (voluntary) — 46 days to object" in html
    assert ">Gazette notice</a>" in html
    assert "Strike-off proposed</span>" in html  # the short badge on the card
    assert "46 days to object" in render_text(digest, title="T")

    # Sixteen days on, the objection deadline is due within 30 days.
    freezer.move_to("2026-10-01T09:00:00+00:00")
    digest = build_digest(entry, days=7)
    (deadline,) = [d for d in digest["deadlines"] if d["number"] == ACTIVE]
    assert deadline["what"] == "object to strike-off"
    assert deadline["date"] == "2026-10-31"
    assert deadline["days"] == 30
    assert deadline["document"] == notice_link(ACTIVE, "NEWTRANSACTION0001")
    html = render_html(digest, title="T")
    assert "object to strike-off" in html
    assert (
        f'<a href="{notice_link(ACTIVE, "NEWTRANSACTION0001").replace("&", "&amp;")}" '
        'style="color:#1d4ed8;text-decoration:none">Gazette notice</a>'
    ) in html
    assert "31 Oct 2026 EXAMPLE TRADING LIMITED: object to strike-off (in 30 days)" in (
        render_text(digest, title="T")
    )

    # A successful objection: the strike-off is suspended for six months.
    events.clear()
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0002",
        category="dissolution",
        type="DISS16(SOAS)",
        description="dissolved-compulsory-strike-off-suspended",
        description_values={},
        date="2026-10-01",
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    (suspended,) = _status_events(events)
    assert suspended["event_type"] == "strike-off-suspended"
    assert suspended["suspended_on"] == "2026-10-01"
    assert suspended["earliest_on"] == "2027-04-01"
    assert suspended["objection_deadline"] == "2027-03-18"
    assert suspended["notice_on"] == "2026-09-14"
    assert suspended["strike_off_kind"] == "voluntary"
    assert company.state.strike_off_suspended_on == date(2026, 10, 1)
    assert company.state.strike_off_notice_on == date(2026, 9, 14)
    assert hass.states.get(EARLIEST).state == "2027-04-01"
    assert hass.states.get(DAYS).state == "168"
    assert hass.states.get(DAYS).attributes["suspended_on"] == "2026-10-01"
    assert hass.states.get(DAYS).attributes["caveat"] == CAVEAT_SUSPENDED
    assert hass.states.get(PROPOSED).state == "on"
    title, message, link = describe_change(
        "status", "strike-off-suspended", suspended, subject="X", number=ACTIVE
    )
    assert title == "X: strike-off suspended"
    assert message == (
        "Compulsory strike-off action has been suspended on 1 Oct 2026. The "
        "company cannot be struck off before 1 Apr 2027."
    )
    assert link == notice_link(ACTIVE, None)
    digest = build_digest(entry, days=7)
    (card,) = [c for c in digest["companies"] if c["number"] == ACTIVE]
    (attention,) = card["attention"]
    assert attention["issue"] == (
        "Strike-off suspended (voluntary) — earliest strike-off 1 Apr 2027"
    )
    assert attention["short"] == "Strike-off suspended"
    assert attention["new"] is True
    assert attention["since"] == "2026-10-01"

    # A fresh notice restarts the clock and forgets the suspension.
    events.clear()
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0003",
        category="gazette",
        type="GAZ1",
        description="gazette-notice-compulsory",
        description_values={},
        date="2026-10-01",
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert proposed["strike_off_kind"] == "compulsory"
    assert proposed["suspended_on"] is None
    assert proposed["earliest_on"] == "2026-12-01"
    assert company.state.strike_off_suspended_on is None
    assert company.state.strike_off_transaction_id == "NEWTRANSACTION0003"
    assert hass.states.get(EARLIEST).state == "2026-12-01"

    # Filings brought up to date: the strike-off is dropped. The profile still
    # says "proposal to strike off" for a while, so the sensor stays on but
    # the countdown is honestly unknown rather than stale.
    events.clear()
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0004",
        category="gazette",
        type="DISS40",
        description="gazette-filings-brought-up-to-date",
        description_values={},
        date="2026-10-01",
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    (discontinued,) = _status_events(events)
    assert discontinued["event_type"] == "strike-off-discontinued"
    assert company.state.strike_off_notice_on is None
    assert company.state.strike_off_kind is None
    assert company.state.strike_off_transaction_id is None
    assert hass.states.get(PROPOSED).state == "on"
    assert hass.states.get(EARLIEST).state == "unknown"
    assert hass.states.get(DAYS).state == "unknown"
    assert hass.states.get(DAYS).attributes["caveat"] == CAVEAT_UNKNOWN
    assert hass.states.get(DAYS).attributes["link"] == notice_link(ACTIVE, None)
    assert not any(
        e.summary in ("Objection deadline", "Earliest strike-off")
        for e in company_events(company, prefix=False)
    )

    # Then the profile clears too.
    events.clear()
    profile["company_status_detail"] = None
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    (discontinued,) = _status_events(events)
    assert discontinued["event_type"] == "strike-off-discontinued"
    assert hass.states.get(PROPOSED).state == "off"
    assert hass.states.get(DAYS).attributes["caveat"] is None


async def test_profile_flags_strike_off_before_any_notice_is_seen(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Status detail alone announces the strike-off but cannot count it down."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry, ACTIVE)
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert "notice_on" not in proposed
    assert company.state.strike_off_notice_on is None
    assert hass.states.get(PROPOSED).state == "on"
    assert hass.states.get(EARLIEST).state == "unknown"
    assert hass.states.get(DAYS).state == "unknown"
    assert hass.states.get(DAYS).attributes["caveat"] == CAVEAT_UNKNOWN
    digest = build_digest(entry, days=7)
    (card,) = digest["companies"]
    (attention,) = card["attention"]
    assert attention["issue"] == "Strike-off proposed — notice date unknown"
    assert attention["links"] == [
        {"text": "Gazette notices", "href": notice_link(ACTIVE, None)}
    ]
    assert card["strike_off"]["earliest_on"] is None
    html = render_html(digest, title="T")
    assert "notice date unknown" in html
    # The Gazette list link is offered once per list, not twice.
    assert html.count(f'{notice_link(ACTIVE, None)}" ') == html.count(
        "Gazette notices</a>"
    )
    _, message, link = describe_change(
        "status", "strike-off-proposed", proposed, subject="X", number=ACTIVE
    )
    assert message == "active-proposal-to-strike-off."
    assert link.endswith(f"/company/{ACTIVE}")


async def test_backfill_from_the_seeded_filings(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A company already under a notice when added gets its countdown silently."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([STRIKE_OFF])
    assert events == []
    company = _company(entry, STRIKE_OFF)
    state = company.state
    assert state.strike_off_notice_on == date(2026, 8, 25)
    assert state.strike_off_kind == "compulsory"
    assert state.strike_off_transaction_id == "MzUwMDAwMDAwMDAwMDAwMDAx"
    assert state.strike_off_suspended_on is None
    days = hass.states.get(
        "sensor.dormant_holdings_limited_days_to_object_to_strike_off"
    )
    assert days.state == "26"
    assert days.attributes["earliest_on"] == "2026-10-25"
    assert (
        hass.states.get(
            "sensor.dormant_holdings_limited_earliest_strike_off_date"
        ).state
        == "2026-10-25"
    )
    calendar = hass.states.get("calendar.dormant_holdings_limited_deadlines")
    assert calendar.attributes["message"] == "Objection deadline"
    assert calendar.attributes["start_time"] == "2026-10-11 00:00:00"

    # It is persisted, and comes back after a restart without a request.
    await entry.runtime_data.store.async_save()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["companies"][STRIKE_OFF]
    assert stored["strike_off_notice_on"] == "2026-08-25"
    assert stored["strike_off_kind"] == "compulsory"
    assert stored["strike_off_suspended_on"] is None
    assert stored["strike_off_transaction_id"] == "MzUwMDAwMDAwMDAwMDAwMDAx"
    restored = CompanyState.from_dict(stored)
    assert restored.strike_off_notice_on == date(2026, 8, 25)
    assert restored.strike_off_kind == "compulsory"
    assert restored.strike_off_transaction_id == "MzUwMDAwMDAwMDAwMDAwMDAx"

    # The report: a known problem with the deadline in the table.
    digest = build_digest(entry, days=7)
    assert digest["needs_attention"] == []
    (still_open,) = digest["still_open"]
    assert still_open["issues"][0] == (
        "Strike-off proposed (compulsory) — 26 days to object"
    )
    assert still_open["links"] == [
        {
            "text": "Gazette notice",
            "href": notice_link(STRIKE_OFF, "MzUwMDAwMDAwMDAwMDAwMDAx"),
        }
    ]
    assert [(d["what"], d["date"], d["days"]) for d in digest["deadlines"]] == [
        ("object to strike-off", "2026-10-11", 26)
    ]
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    (info,) = diagnostics["companies"]
    assert info["strike_off"]["earliest_on"] == "2026-10-25"
    assert info["strike_off"]["days_to_object"] == 26


async def test_backfill_recovers_a_suspended_notice(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two notices, the first discontinued, the second suspended: the live case."""
    freezer.move_to("2026-09-14T09:00:00+00:00")
    entry = await setup_entry()
    company = await _add_company(
        hass, entry, aioclient_mock, SUSPENDED, fixture="company_suspended_strike_off"
    )
    state = company.state
    assert state.strike_off_notice_on == date(2024, 7, 30)
    assert state.strike_off_kind == "compulsory"
    assert state.strike_off_transaction_id == "MzQwNjAwMDAwMDAwMDAwMDA1"
    assert state.strike_off_suspended_on == date(2024, 8, 7)
    countdown = company.strike_off_countdown()
    assert countdown is not None
    assert countdown.earliest_on == date(2025, 2, 7)
    assert countdown.attention_line() == (
        "Strike-off suspended (compulsory) — earliest strike-off 7 Feb 2025"
    )
    days = hass.states.get("sensor.truvai_example_limited_days_to_object_to_strike_off")
    assert days.state == "-598"
    assert days.attributes["suspended_on"] == "2024-08-07"
    assert days.attributes["caveat"] == CAVEAT_SUSPENDED
    assert days.attributes["link"] == notice_link(SUSPENDED, "MzQwNjAwMDAwMDAwMDAwMDA1")
    assert (
        hass.states.get(
            "binary_sensor.truvai_example_limited_proposed_strike_off"
        ).state
        == "on"
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    (info,) = diagnostics["companies"]
    assert info["strike_off"]["suspended_on"] == "2024-08-07"


async def test_backfill_leaves_a_discontinued_notice_alone(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A notice followed by DISS40 is spent: the status detail alone is not trusted."""
    freezer.move_to("2026-09-14T09:00:00+00:00")
    entry = await setup_entry()
    history = load_fixture("company_suspended_strike_off/filing_history")
    history["items"] = history["items"][2:]  # DISS40 above the 2023 notice
    history["total_count"] = len(history["items"])
    company = await _add_company(
        hass,
        entry,
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history},
    )
    assert company.state.strike_off_notice_on is None
    assert company.strike_off_proposed
    countdown = company.strike_off_countdown()
    assert countdown is not None
    assert countdown.earliest_on is None
    days = hass.states.get("sensor.truvai_example_limited_days_to_object_to_strike_off")
    assert days.state == "unknown"
    assert days.attributes["caveat"] == CAVEAT_UNKNOWN
    (info,) = (await async_get_config_entry_diagnostics(hass, entry))["companies"]
    assert info["strike_off"]["notice_on"] is None
    assert info["strike_off"]["earliest_on"] is None

    # No notice in the kept filings at all: nothing to recover either.
    history["items"] = [i for i in history["items"] if i["type"] != "GAZ1"]
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert company.state.strike_off_notice_on is None


def test_company_state_round_trip_tolerates_old_files() -> None:
    """The new fields serialise, and a store written before them still loads."""
    state = CompanyState(
        strike_off_notice_on=date(2026, 8, 25),
        strike_off_kind="voluntary",
        strike_off_suspended_on=date(2026, 9, 1),
        strike_off_transaction_id="tx1",
    )
    data = state.to_dict()
    assert data["strike_off_suspended_on"] == "2026-09-01"
    again = CompanyState.from_dict(data)
    assert again.strike_off_notice_on == date(2026, 8, 25)
    assert again.strike_off_kind == "voluntary"
    assert again.strike_off_suspended_on == date(2026, 9, 1)
    assert again.strike_off_transaction_id == "tx1"
    old = CompanyState.from_dict({"strike_off_notice_on": "2026-08-25"})
    assert old.strike_off_notice_on == date(2026, 8, 25)
    assert old.strike_off_kind is None
    assert old.strike_off_suspended_on is None
    assert old.strike_off_transaction_id is None
    junk = CompanyState.from_dict(
        {"strike_off_kind": 7, "strike_off_transaction_id": "", "not_found": True}
    )
    assert junk.strike_off_kind is None
    assert junk.strike_off_transaction_id is None
    assert junk.not_found is False


def test_describe_suspension_without_a_clock() -> None:
    """A suspension seen with no notice date still reads well."""
    title, message, link = describe_change(
        "status",
        "strike-off-suspended",
        {"detail": "Voluntary strike-off action has been suspended"},
        subject="X",
    )
    assert title == "X: strike-off suspended"
    assert message == "Voluntary strike-off action has been suspended."
    assert link == ""
    _, message, _ = describe_change("status", "strike-off-suspended", {}, subject="X")
    assert message == "The strike-off has been suspended."
