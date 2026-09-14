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
    """Return the strike-off status events; the rating a strike-off moves is a separate story."""
    return [
        e.data
        for e in events
        if e.data["kind"] == "status" and e.data["event_type"] != "risk-changed"
    ]


def _risk_events(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.data["event_type"] == "risk-changed"]


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
    # The notice alone turns the rating red, with the countdown's own words.
    (rated,) = _risk_events(events)
    assert rated["new_band"] == "red"
    assert "strike-off proposed (voluntary) on 14 Sep 2026" in rated["reasons"]
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
    # The strike-off, then the red rating it earned (new this week too).
    attention, rated = card["attention"]
    assert rated == {
        "issue": "Risk rating red",
        "new": True,
        "since": None,
        "short": "Risk rating red",
        "links": [],
        "kind": "risk",
    }
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
    attention, rated = card["attention"]
    # Still red: a suspended strike-off is held off, not dropped. The band
    # did not move this period, so it is still open rather than new.
    assert rated["issue"] == "Risk rating red"
    assert rated["new"] is False
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
    # says "proposal to strike off" for a while, but the filing is what
    # counts: the sensor goes off and says why, and nothing is counted down.
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
    assert company.state.strike_off_discontinued_on == date(2026, 10, 1)
    assert not company.strike_off_proposed
    assert company.strike_off_countdown() is None
    binary = hass.states.get(PROPOSED)
    assert binary.state == "off"
    assert binary.attributes["status_detail"] == "active-proposal-to-strike-off"
    assert binary.attributes["discontinued_on"] == "2026-10-01"
    assert binary.attributes["gazette_notice_on"] is None
    assert hass.states.get(EARLIEST).state == "unknown"
    assert hass.states.get(DAYS).state == "unknown"
    assert hass.states.get(DAYS).attributes["caveat"] is None
    assert hass.states.get(DAYS).attributes["link"] is None
    assert not any(
        e.summary in ("Objection deadline", "Earliest strike-off")
        for e in company_events(company, prefix=False)
    )
    # The report has the "dropped" row and no live problem contradicting it.
    digest = build_digest(entry, days=7)
    (card,) = [c for c in digest["companies"] if c["number"] == ACTIVE]
    assert card["strike_off"] is None
    assert not any("Strike-off" in a["issue"] for a in card["attention"])
    assert "strike-off-discontinued" in {c["event_type"] for c in card["changes"]}
    assert not any("Strike-off" in a["issue"] for a in digest["needs_attention"])
    assert "strike-off dropped" in render_text(digest, title="T")
    assert "notice date unknown" not in render_html(digest, title="T")

    # Then the profile clears too: the same news, so nothing more is said.
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
    assert _status_events(events) == []
    assert company.state.strike_off_discontinued_on is None
    binary = hass.states.get(PROPOSED)
    assert binary.state == "off"
    assert binary.attributes["discontinued_on"] is None
    assert hass.states.get(DAYS).attributes["caveat"] is None


async def test_profile_flags_strike_off_before_any_notice_is_seen(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Status detail alone announces the strike-off, once the filings are checked.

    The probe is asked to look for the notice first; when there is none the
    announcement has no clock but is still in plain English, and the notice
    fills the clock in quietly when it arrives.
    """
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
    # The profile, then the filing history, looked at now for the notice.
    assert [str(call[1].path) for call in aioclient_mock.mock_calls] == [
        f"/company/{ACTIVE}",
        f"/company/{ACTIVE}/filing-history",
    ]
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert proposed["detail"] == "active-proposal-to-strike-off"
    assert "notice_on" not in proposed
    assert company.state.strike_off_notice_on is None
    assert company.state.strike_off_awaiting_notice is True
    assert hass.states.get(PROPOSED).state == "on"
    assert hass.states.get(EARLIEST).state == "unknown"
    assert hass.states.get(DAYS).state == "unknown"
    assert hass.states.get(DAYS).attributes["caveat"] == CAVEAT_UNKNOWN
    digest = build_digest(entry, days=7)
    (card,) = digest["companies"]
    attention, rated = card["attention"]
    assert rated["issue"] == "Risk rating red"
    assert attention["issue"] == "Strike-off proposed — notice date unknown"
    assert attention["new"] is True
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
    # Never the register's key: plain English in the alert and the report.
    _, message, link = describe_change(
        "status", "strike-off-proposed", proposed, subject="X", number=ACTIVE
    )
    assert message == (
        "Companies House has proposed to strike the company off; the Gazette "
        "notice has not appeared in the filings yet."
    )
    assert link.endswith(f"/company/{ACTIVE}")
    assert "active-proposal-to-strike-off" not in render_text(digest, title="T")
    (info,) = (await async_get_config_entry_diagnostics(hass, entry))["companies"]
    assert info["strike_off_awaiting_notice"] is True

    # The notice arrives: the filing is announced, the proposal is not said
    # again, and the countdown fills in.
    events.clear()
    history = _new_filing(
        load_fixture("company_active/filing_history"),
        category="gazette",
        type="GAZ1",
        description="gazette-notice-compulsory",
        description_values={},
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["event_type"] for e in events] == ["gazette"]
    assert company.state.strike_off_notice_on == date(2026, 9, 14)
    assert company.state.strike_off_kind == "compulsory"
    assert company.state.strike_off_awaiting_notice is False
    assert hass.states.get(DAYS).state == "46"
    assert hass.states.get(EARLIEST).state == "2026-11-14"
    assert hass.states.get(DAYS).attributes["caveat"] == CAVEAT_TWO_MONTHS


async def test_profile_flip_finds_the_notice_the_probe_has_not_seen(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The profile flips first; the probe is asked, finds the notice and announces once."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry, ACTIVE)
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    history = _new_filing(
        load_fixture("company_active/filing_history"),
        category="gazette",
        type="GAZ1",
        description="gazette-notice-compulsory",
        description_values={},
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["event_type"] for e in events] == [
        "gazette",
        "strike-off-proposed",
        "risk-changed",
    ]
    (proposed,) = _status_events(events)
    assert proposed["detail"] == "First Gazette notice for compulsory strike-off"
    assert proposed["notice_on"] == "2026-09-14"
    assert proposed["earliest_on"] == "2026-11-14"
    assert proposed["transaction_id"] == "NEWTRANSACTION0001"
    assert company.state.strike_off_awaiting_notice is False
    assert hass.states.get(DAYS).state == "46"


async def test_profile_flip_announces_a_notice_kept_from_the_first_look(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A notice seeded before the register said so is announced with its clock."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    profile = load_fixture("company_strike_off/profile")
    profile["company_status_detail"] = None
    mock_company(aioclient_mock, STRIKE_OFF, overrides={"profile": profile})
    entry = await setup_entry([STRIKE_OFF])
    company = _company(entry, STRIKE_OFF)
    assert events == []
    assert company.state.strike_off_notice_on is None
    assert not company.strike_off_proposed

    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, STRIKE_OFF, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 1  # the profile; no probe needed
    (proposed,) = _status_events(events)
    assert [e.data["event_type"] for e in events] == ["strike-off-proposed"]
    assert proposed["detail"] == "First Gazette notice for compulsory strike-off"
    assert proposed["old_status"] == proposed["new_status"] == "active"
    assert proposed["notice_on"] == "2026-08-25"
    assert proposed["earliest_on"] == "2026-10-25"
    assert proposed["objection_deadline"] == "2026-10-11"
    assert proposed["transaction_id"] == "MzUwMDAwMDAwMDAwMDAwMDAx"
    assert company.state.strike_off_kind == "compulsory"
    _, message, link = describe_change(
        "status", "strike-off-proposed", proposed, subject="X", number=STRIKE_OFF
    )
    assert message == (
        "First Gazette notice for compulsory strike-off on 25 Aug 2026. The "
        "company can be struck off from 25 Oct 2026; object by 11 Oct 2026."
    )
    assert link == notice_link(STRIKE_OFF, "MzUwMDAwMDAwMDAwMDAwMDAx")
    assert (
        hass.states.get(
            "sensor.dormant_holdings_limited_days_to_object_to_strike_off"
        ).state
        == "26"
    )


async def test_profile_flip_after_a_discontinuation_says_nothing(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The register flips to "proposed" while the kept filings say discontinued."""
    freezer.move_to("2026-09-14T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry()
    history = load_fixture("company_suspended_strike_off/filing_history")
    history["items"] = history["items"][2:]  # DISS40 above the 2023 notice
    history["total_count"] = len(history["items"])
    profile = load_fixture("company_suspended_strike_off/profile")
    profile["company_status_detail"] = None
    company = await _add_company(
        hass,
        entry,
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history, "profile": profile},
    )
    assert company.state.strike_off_discontinued_on is None
    events.clear()
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history, "profile": profile},
    )
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert _status_events(events) == []
    assert company.state.strike_off_discontinued_on == date(2023, 8, 15)
    assert company.state.strike_off_awaiting_notice is False
    assert not company.strike_off_proposed
    # A fresh notice ends the stale discontinuation and is announced as usual.
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0009",
        category="gazette",
        type="GAZ1",
        description="gazette-notice-compulsory",
        description_values={},
        date="2026-09-14",
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert proposed["notice_on"] == "2026-09-14"
    assert company.state.strike_off_discontinued_on is None
    assert company.strike_off_proposed


async def test_probe_discontinuation_after_the_profile_cleared_is_quiet(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Whichever side sees the end of a strike-off first announces it, once."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry, ACTIVE)
    history = _new_filing(
        load_fixture("company_active/filing_history"),
        category="gazette",
        type="GAZ1",
        description="gazette-notice-voluntary",
        description_values={},
    )
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert [e["event_type"] for e in _status_events(events)] == ["strike-off-proposed"]

    # The register clears first: announced, and the notice is forgotten.
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
    assert company.state.strike_off_notice_on is None
    assert hass.states.get(PROPOSED).state == "off"

    # The withdrawal filing then arrives: the filing itself is announced, the
    # end of the strike-off is not announced a second time.
    events.clear()
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0002",
        category="dissolution",
        type="DS02",
        description="dissolution-withdrawal-application-strike-off-company",
        description_values={},
    )
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={"filing_history": history, "profile": profile},
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["event_type"] for e in events] == ["dissolution"]
    assert company.state.strike_off_discontinued_on == date(2026, 9, 14)
    assert hass.states.get(PROPOSED).state == "off"
    assert hass.states.get(PROPOSED).attributes["discontinued_on"] == "2026-09-14"


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
        "Strike-off suspended (compulsory) — hold ended 7 Feb 2025, could be "
        "struck off any day now"
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
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
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
    assert events == []
    assert company.state.strike_off_notice_on is None
    assert company.state.strike_off_discontinued_on == date(2023, 8, 15)
    assert not company.strike_off_proposed
    assert company.strike_off_countdown() is None
    days = hass.states.get("sensor.truvai_example_limited_days_to_object_to_strike_off")
    assert days.state == "unknown"
    assert days.attributes["caveat"] is None
    binary = hass.states.get("binary_sensor.truvai_example_limited_proposed_strike_off")
    assert binary.state == "off"
    assert binary.attributes["status_detail"] == "active-proposal-to-strike-off"
    assert binary.attributes["discontinued_on"] == "2023-08-15"
    (info,) = (await async_get_config_entry_diagnostics(hass, entry))["companies"]
    assert info["strike_off_proposed"] is False
    assert info["strike_off"] is None
    assert info["strike_off_discontinued_on"] == "2023-08-15"
    digest = build_digest(entry, days=7)
    assert digest["needs_attention"] == []
    assert not any(
        "Strike-off" in issue for row in digest["still_open"] for issue in row["issues"]
    )
    assert "notice date unknown" not in render_text(digest, title="T")

    # The register catches up: nothing was ever live here, nothing is said.
    profile = load_fixture("company_suspended_strike_off/profile")
    profile["company_status_detail"] = None
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        SUSPENDED,
        fixture="company_suspended_strike_off",
        overrides={"filing_history": history, "profile": profile},
    )
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert _status_events(events) == []
    assert company.state.strike_off_discontinued_on is None
    assert (
        hass.states.get(
            "binary_sensor.truvai_example_limited_proposed_strike_off"
        ).attributes["discontinued_on"]
        is None
    )


async def test_notice_recorded_by_an_older_version_gets_its_filing_back(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A store with only the notice date is completed from the kept filings."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([STRIKE_OFF])
    calls = aioclient_mock.call_count
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["companies"][STRIKE_OFF]
    assert stored["strike_off_notice_on"] == "2026-08-25"
    for key in (
        "strike_off_kind",
        "strike_off_suspended_on",
        "strike_off_transaction_id",
        "strike_off_discontinued_on",
        "strike_off_awaiting_notice",
    ):
        del stored[key]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == calls  # the snapshots were fresh
    assert events == []
    company = _company(entry, STRIKE_OFF)
    state = company.state
    assert state.strike_off_notice_on == date(2026, 8, 25)
    assert state.strike_off_kind == "compulsory"
    assert state.strike_off_transaction_id == "MzUwMDAwMDAwMDAwMDAwMDAx"
    countdown = company.strike_off_countdown()
    assert countdown is not None
    assert countdown.attention_line() == (
        "Strike-off proposed (compulsory) — 26 days to object"
    )
    days = hass.states.get(
        "sensor.dormant_holdings_limited_days_to_object_to_strike_off"
    )
    assert days.attributes["kind"] == "compulsory"
    assert days.attributes["link"] == notice_link(
        STRIKE_OFF, "MzUwMDAwMDAwMDAwMDAwMDAx"
    )
    digest = build_digest(entry, days=7)
    (deadline,) = digest["deadlines"]
    assert deadline["document"] == notice_link(STRIKE_OFF, "MzUwMDAwMDAwMDAwMDAwMDAx")
    assert ">Gazette notice</a>" in render_html(digest, title="T")

    # A notice whose filing is no longer among those kept stays as it was.
    state.strike_off_notice_on = date(2026, 8, 1)
    state.strike_off_kind = None
    state.strike_off_transaction_id = None
    company.reconcile_strike_off()
    assert state.strike_off_notice_on == date(2026, 8, 1)
    assert state.strike_off_kind is None
    assert state.strike_off_transaction_id is None


async def test_profile_flip_forgets_a_discontinuation_no_filing_backs(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A remembered discontinuation older than every kept filing gives way to the register."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry, ACTIVE)
    company.state.strike_off_discontinued_on = date(2025, 1, 1)
    assert not company.strike_off_proposed
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    (proposed,) = _status_events(events)
    assert proposed["event_type"] == "strike-off-proposed"
    assert company.state.strike_off_discontinued_on is None
    assert company.state.strike_off_awaiting_notice is True
    assert company.strike_off_proposed
    assert hass.states.get(PROPOSED).state == "on"

    # With no filing history at all the filings cannot settle anything.
    company.probe.data = None
    assert company._settle_strike_off_from_filings(announce=proposed) is None
    assert _status_events(events) == [proposed]


def test_company_state_round_trip_tolerates_old_files() -> None:
    """The new fields serialise, and a store written before them still loads."""
    state = CompanyState(
        strike_off_notice_on=date(2026, 8, 25),
        strike_off_kind="voluntary",
        strike_off_suspended_on=date(2026, 9, 1),
        strike_off_transaction_id="tx1",
        strike_off_discontinued_on=date(2026, 9, 10),
        strike_off_awaiting_notice=True,
    )
    data = state.to_dict()
    assert data["strike_off_suspended_on"] == "2026-09-01"
    assert data["strike_off_discontinued_on"] == "2026-09-10"
    assert data["strike_off_awaiting_notice"] is True
    again = CompanyState.from_dict(data)
    assert again.strike_off_notice_on == date(2026, 8, 25)
    assert again.strike_off_kind == "voluntary"
    assert again.strike_off_suspended_on == date(2026, 9, 1)
    assert again.strike_off_transaction_id == "tx1"
    assert again.strike_off_discontinued_on == date(2026, 9, 10)
    assert again.strike_off_awaiting_notice is True
    old = CompanyState.from_dict({"strike_off_notice_on": "2026-08-25"})
    assert old.strike_off_notice_on == date(2026, 8, 25)
    assert old.strike_off_kind is None
    assert old.strike_off_suspended_on is None
    assert old.strike_off_transaction_id is None
    assert old.strike_off_discontinued_on is None
    assert old.strike_off_awaiting_notice is False
    junk = CompanyState.from_dict(
        {
            "strike_off_kind": 7,
            "strike_off_transaction_id": "",
            "strike_off_discontinued_on": "soon",
            "strike_off_awaiting_notice": "yes",
            "not_found": True,
        }
    )
    assert junk.strike_off_kind is None
    assert junk.strike_off_transaction_id is None
    assert junk.strike_off_discontinued_on is None
    assert junk.strike_off_awaiting_notice is False
    assert junk.not_found is False


def test_describe_proposal_never_echoes_the_register_key() -> None:
    """Old log entries carry the status detail key; the reader gets a sentence."""
    _, message, _ = describe_change(
        "status",
        "strike-off-proposed",
        {"detail": "active-proposal-to-strike-off"},
        subject="X",
    )
    assert message == (
        "Companies House has proposed to strike the company off; the Gazette "
        "notice has not appeared in the filings yet."
    )
    _, message, _ = describe_change("status", "strike-off-proposed", {}, subject="X")
    assert message == (
        "Companies House has proposed to strike the company off; the Gazette "
        "notice has not appeared in the filings yet."
    )
    # With a clock but the register's key as detail (not a filing description).
    _, message, _ = describe_change(
        "status",
        "strike-off-proposed",
        {
            "detail": "active-proposal-to-strike-off",
            "notice_on": "2026-08-25",
            "earliest_on": "2026-10-25",
            "objection_deadline": "2026-10-11",
        },
        subject="X",
    )
    assert message == (
        "Companies House has proposed to strike the company off on 25 Aug 2026. "
        "The company can be struck off from 25 Oct 2026; object by 11 Oct 2026."
    )


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
