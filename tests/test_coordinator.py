"""Tests for the probe, change detection, scheduling and persistence."""

from __future__ import annotations

from collections.abc import Callable
import copy
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.api import CompaniesHouseBudgetError
from custom_components.companies_house.const import (
    API_BASE,
    DOMAIN,
    EVENT_COMPANIES_HOUSE,
    Dataset,
    Tier,
)
from custom_components.companies_house.coordinator import (
    CompanyRuntime,
    match_disqualification,
)
from custom_components.companies_house.models import DateOfBirth
from custom_components.companies_house.scheduler import LONDON

from .conftest import load_fixture, mock_company, mock_officer

ACTIVE = "12345678"


def _company(entry: Any, number: str = ACTIVE) -> CompanyRuntime:
    company: CompanyRuntime = entry.runtime_data.companies[f"sub_{number}"]
    return company


def _new_filing(history: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a copy of the history with one extra newest filing."""
    history = copy.deepcopy(history)
    item = {
        "transaction_id": "NEWTRANSACTION0001",
        "category": "officers",
        "type": "TM01",
        "date": "2026-09-14",
        "description": "termination-director-company-with-name-termination-date",
        "description_values": {
            "officer_name": "Ms Priya Patel",
            "termination_date": "2026-09-12",
        },
        "pages": 1,
        "paper_filed": False,
        "barcode": "XNEW0001",
        "links": {
            "self": f"/company/{ACTIVE}/filing-history/NEWTRANSACTION0001",
            "document_metadata": "https://frontend-doc-api/document/doc-new-1",
        },
    }
    item.update(overrides)
    history["items"].insert(0, item)
    history["total_count"] += 1
    return history


async def test_first_setup_seeds_silently_then_one_filing_fires_one_event(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Setup fires nothing; a new filing fires exactly one event of the right type."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    await hass.async_block_till_done()
    assert events == []
    company = _company(entry)
    assert company.state.filings_total_count == 6
    assert company.state.newest_transaction_id == "MzQwMDAwMDAwMDAwMDAwMDAx"
    assert len(company.state.seen_transaction_ids) == 6
    filing_entity = hass.states.get("event.example_trading_limited_filing")
    assert filing_entity is not None
    assert filing_entity.state == "unknown"

    # One new officers filing appears; the officers list is unchanged.
    history = _new_filing(load_fixture("company_active/filing_history"))
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    before = aioclient_mock.call_count
    await company.probe.async_refresh()
    await hass.async_block_till_done()

    filing_events = [e for e in events if e.data["kind"] == "filing"]
    assert len(filing_events) == 1
    event: Event = filing_events[0]
    assert event.data["event_type"] == "officers"
    assert event.data["company_number"] == ACTIVE
    assert event.data["company_name"] == "EXAMPLE TRADING LIMITED"
    assert event.data["transaction_id"] == "NEWTRANSACTION0001"
    assert event.data["rendered_description"] == (
        "Termination of appointment of Ms Priya Patel as a director on 12 September 2026"
    )
    assert event.data["document_id"] == "doc-new-1"
    filing_entity = hass.states.get("event.example_trading_limited_filing")
    assert filing_entity is not None
    assert filing_entity.attributes["event_type"] == "officers"
    assert filing_entity.attributes["transaction_id"] == "NEWTRANSACTION0001"
    # Probe (1) plus the profile and officers refreshes it implicated, nothing else.
    urls = [call[1].path for call in aioclient_mock.mock_calls[before:]]
    assert any(u.endswith("/filing-history") for u in urls)
    assert any(u.endswith(f"/company/{ACTIVE}") for u in urls)
    assert any("/officers" in u for u in urls)
    assert not any("/charges" in u for u in urls)
    assert not any("significant-control" in u for u in urls)
    assert company.state.filings_total_count == 7
    assert (
        hass.states.get("sensor.example_trading_limited_last_filing_date").state
        == "2026-09-14"
    )


async def test_unchanged_probe_issues_no_further_requests(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A probe with the same total_count and newest id costs exactly one request."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    before = aioclient_mock.call_count
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == before + 1
    assert aioclient_mock.mock_calls[-1][1].path.endswith("/filing-history")


async def test_catch_up_fetches_only_missing_filings(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Several new filings are fetched in one page and fired oldest first."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    history = _new_filing(load_fixture("company_active/filing_history"))
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0002",
        category="mortgage",
        type="MR04",
        description="mortgage-satisfy-with-charge-number",
        description_values={"charge_number": "123456780001"},
        date="2026-09-15",
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    filing_events = [e.data for e in events if e.data["kind"] == "filing"]
    assert [e["transaction_id"] for e in filing_events] == [
        "NEWTRANSACTION0001",
        "NEWTRANSACTION0002",
    ]
    assert filing_events[1]["event_type"] == "mortgage"
    urls = [call[1].path for call in aioclient_mock.mock_calls]
    assert any("/charges" in u for u in urls)  # implicated by the mortgage filing
    assert any("/officers" in u for u in urls)
    assert company.state.recent_filings[0]["transaction_id"] == "NEWTRANSACTION0002"
    # The snapshot keeps the seeded history behind the new page, newest first.
    assert company.probe.data is not None
    kept = [item.transaction_id for item in company.probe.data.items]
    assert kept[:2] == ["NEWTRANSACTION0002", "NEWTRANSACTION0001"]
    assert len(kept) == len(load_fixture("company_active/filing_history")["items"]) + 2
    assert len(set(kept)) == len(kept)


async def test_officer_and_charge_changes_fire_events(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Dataset diffs produce typed events with the documented payloads."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    officers = load_fixture("company_active/officers")
    officers["items"][1]["resigned_on"] = "2026-09-12"  # Priya resigns
    officers["items"][0]["occupation"] = "Engineer"  # Jane's details change
    officers["items"].append(
        {
            **officers["items"][0],
            "name": "NEW, Person",
            "links": {
                "self": f"/company/{ACTIVE}/appointments/appt-new",
                "officer": {"appointments": "/officers/officer-new/appointments"},
            },
        }
    )
    charges = load_fixture("company_active/charges")
    charges["items"][0]["status"] = "fully-satisfied"
    charges["items"].append(
        {
            **charges["items"][1],
            "links": {"self": f"/company/{ACTIVE}/charges/chg-3"},
            "status": "outstanding",
            "acquired_on": "2026-09-01",
        }
    )
    psc = load_fixture("company_active/psc")
    psc["items"][0]["ceased_on"] = "2026-09-10"
    psc["items"].append(
        {
            **psc["items"][0],
            "name": "Mr New Owner",
            "links": {
                "self": f"/company/{ACTIVE}/persons-with-significant-control/individual/psc-new"
            },
        }
    )
    psc.pop("ceased_on", None)
    statements = {
        "items": [
            {
                "statement": "no-individual-or-entity-with-signficant-control",
                "notified_on": "2026-09-10",
                "links": {
                    "self": f"/company/{ACTIVE}/persons-with-significant-control-statements/st-1"
                },
            }
        ],
        "active_count": 1,
        "total_results": 1,
    }
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        ACTIVE,
        overrides={
            "officers": officers,
            "charges": charges,
            "psc": psc,
            "psc_statements": statements,
        },
    )
    await company.async_refresh_datasets(
        [Dataset.OFFICERS, Dataset.CHARGES, Dataset.PSC], reason="test"
    )
    await hass.async_block_till_done()
    kinds = sorted((e.data["kind"], e.data["event_type"]) for e in events)
    assert kinds == [
        ("charge", "acquired"),
        ("charge", "satisfied"),
        ("officer", "appointed"),
        ("officer", "details-changed"),
        ("officer", "resigned"),
        ("psc", "ceased"),
        ("psc", "notified"),
        ("psc", "statement-added"),
        ("status", "risk-changed"),
    ]
    # A resignation and a change of control move the rating from green to
    # amber, once, after the refreshes have settled.
    rated = next(e.data for e in events if e.data["event_type"] == "risk-changed")
    assert (rated["old_band"], rated["new_band"]) == ("green", "amber")
    assert "1 director resigned in the last year" in rated["reasons"]
    assert rated["reason"].startswith("Amber: ")
    assert company.state.risk_band == "amber"
    assert company.state.risk_score == rated["score"]
    # Every change is remembered, newest first, for the report.
    logged = company.state.changes
    assert len(logged) == 9
    assert {(c["kind"], c["event_type"]) for c in logged} == set(kinds)
    assert logged[0]["at"]
    assert logged[0]["payload"]
    assert len(entry.runtime_data.store.company(ACTIVE).changes) == 9
    # Charge events say who lent, what secures it and which filing to open,
    # and the payload's own words never clobber the event's kind.
    satisfied = next(e.data for e in events if e.data["event_type"] == "satisfied")
    assert satisfied["kind"] == "charge"
    assert satisfied["charge_code"] == "123456780002"
    assert satisfied["lender"] == "HSBC UK Bank Plc"
    assert satisfied["persons_entitled"] == ["HSBC UK Bank Plc"]
    assert satisfied["charge_kind"] == (
        "fixed and floating charge over all the company's assets"
    )
    assert satisfied["security"].startswith(
        "fixed and floating charge over all the company's assets; secures all monies"
    )
    assert satisfied["created_transaction_id"] == "MzM0MDAwMDAwMGFkaXF6a2N4"
    assert satisfied["satisfied_transaction_id"] is None
    assert satisfied["transaction_ids"] == ["MzM0MDAwMDAwMGFkaXF6a2N4"]
    acquired = next(e.data for e in events if e.data["event_type"] == "acquired")
    assert acquired["kind"] == "charge"
    assert acquired["acquired_on"] == "2026-09-01"
    assert acquired["lender"] == "Lloyds Bank Plc"
    assert acquired["particulars"].startswith("The freehold property known as Unit 4")
    assert acquired["satisfied_transaction_id"] == "MzMxMDAwMDAwMGFkaXF6a2N4"
    resigned = next(e.data for e in events if e.data["event_type"] == "resigned")
    assert resigned["name"] == "PATEL, Priya"
    assert resigned["resigned_on"] == "2026-09-12"
    assert resigned["officer_id"] == "officer-priya"
    state = hass.states.get("event.example_trading_limited_officer_change")
    assert state is not None
    assert state.attributes["event_type"] in (
        "appointed",
        "resigned",
        "details-changed",
    )
    assert (
        hass.states.get("sensor.example_trading_limited_officers_active").state == "3"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_outstanding_charges").state
        == "1"
    )


async def test_notify_instantly_raises_alerts_in_plain_english(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With notify instantly on, each change also fires a ready-made alert."""
    from custom_components.companies_house.const import EVENT_COMPANIES_HOUSE_ALERT

    alerts = async_capture_events(hass, EVENT_COMPANIES_HOUSE_ALERT)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    officers = load_fixture("company_active/officers")
    officers["items"][1]["resigned_on"] = "2026-09-12"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"officers": officers})
    await company.async_refresh_datasets([Dataset.OFFICERS], reason="test")
    await hass.async_block_till_done()
    assert alerts == []  # off by default

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": "switch.example_trading_limited_notify_instantly"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.subentries["sub_12345678"].data["notify_instantly"] is True
    officers["items"][0]["resigned_on"] = "2026-09-13"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"officers": officers})
    await company.async_refresh_datasets([Dataset.OFFICERS], reason="test")
    await hass.async_block_till_done()
    # The resignation itself, then the rating it pushed from amber (a sole
    # director, after the first resignation) to red: no directors left in
    # office, on top of the all-assets debenture the charges fixture carries.
    assert len(alerts) == 2
    data = alerts[0].data
    assert data["company_number"] == ACTIVE
    assert data["kind"] == "officer"
    assert data["event_type"] == "resigned"
    assert data["title"] == "EXAMPLE TRADING LIMITED: director resigned"
    assert data["message"].startswith(
        "Jane Elizabeth Smith resigned as director on 13 Sep 2026"
    )
    assert data["link"].endswith(f"/company/{ACTIVE}/officers")
    rated = alerts[1].data
    assert rated["event_type"] == "risk-changed"
    assert rated["title"] == "EXAMPLE TRADING LIMITED: risk now red"
    assert rated["message"].startswith("Red: no directors in office")
    assert rated["link"].endswith(f"/company/{ACTIVE}")


def test_describe_change_covers_every_kind() -> None:
    """Every change kind has a plain-English title and a useful link."""
    from custom_components.companies_house.const import (
        ACCOUNTS_CHANGE_EVENT_TYPES,
        APPOINTMENT_EVENT_TYPES,
        CHARGE_CHANGE_EVENT_TYPES,
        FILING_EVENT_TYPES,
        OFFICER_CHANGE_EVENT_TYPES,
        PROFILE_CHANGE_EVENT_TYPES,
        PSC_CHANGE_EVENT_TYPES,
        STATUS_CHANGE_EVENT_TYPES,
    )
    from custom_components.companies_house.digest import describe_change

    payload = {
        "name": "SMITH, Jane",
        "role": "director",
        "appointed_on": "2026-01-01",
        "resigned_on": "2026-02-01",
        "old_status": "active",
        "new_status": "liquidation",
        "company_status": "dissolved",
        "old_value": "OLD LTD",
        "new_value": "NEW LTD",
        "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
        "persons_entitled": ["Big Bank plc"],
        "charge_code": "0123",
        "created_on": "2026-03-01",
        "transaction_id": "tx1",
        "rendered_description": "Confirmation statement made on 1 January 2026",
        "company_name": "OTHER LTD",
        "company_number": "99999999",
        "officer_id": "abc",
        "disqualified_until": "2031-01-01",
        "reason": "Misconduct",
    }
    kinds = {
        "filing": FILING_EVENT_TYPES,
        "officer": OFFICER_CHANGE_EVENT_TYPES,
        "psc": PSC_CHANGE_EVENT_TYPES,
        "charge": CHARGE_CHANGE_EVENT_TYPES,
        "status": STATUS_CHANGE_EVENT_TYPES,
        "profile": PROFILE_CHANGE_EVENT_TYPES,
        "insolvency": ["case-added"],
        "appointment": APPOINTMENT_EVENT_TYPES,
        "accounts": ACCOUNTS_CHANGE_EVENT_TYPES,
    }
    for kind, types in kinds.items():
        for event_type in types:
            title, message, link = describe_change(
                kind, event_type, payload, subject="ACME LTD", number="12345678"
            )
            assert title.startswith("ACME LTD: "), (kind, event_type)
            assert isinstance(message, str)
            assert isinstance(link, str)
    title, message, link = describe_change(
        "filing", "accounts", payload, subject="ACME LTD", number="12345678"
    )
    assert title == "ACME LTD: Accounts filed"
    assert message == "Confirmation statement made on 1 January 2026"
    assert link.endswith(
        "/company/12345678/filing-history/tx1/document?format=pdf&download=0"
    )
    title, _, _ = describe_change("status", "strike-off-proposed", {}, subject="X")
    assert title == "X: strike-off proposed"
    title, message, _ = describe_change("weird", "thing-happened", {}, subject="X")
    assert (title, message) == ("X: thing happened", "")


async def test_status_and_profile_changes(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Strike off, dissolution, name and address changes each fire once."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    profile = load_fixture("company_active/profile")
    profile["company_status_detail"] = "active-proposal-to-strike-off"
    profile["company_name"] = "EXAMPLE TRADING (RENAMED) LIMITED"
    profile["registered_office_address"]["postal_code"] = "RG1 9ZZ"
    profile["sic_codes"] = ["62012"]
    profile["accounts"]["accounting_reference_date"] = {"day": "30", "month": "06"}
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    types = sorted(e.data["event_type"] for e in events)
    assert types == [
        "accounting-reference-date-changed",
        "address-changed",
        "name-changed",
        "risk-changed",
        "sic-changed",
        "strike-off-proposed",
    ]
    assert hass.states.get("sensor.example_trading_limited_risk_rating").state == "red"
    assert (
        hass.states.get(
            "binary_sensor.example_trading_limited_proposed_strike_off"
        ).state
        == "on"
    )
    assert company.company_name == "EXAMPLE TRADING (RENAMED) LIMITED"
    assert company.strike_off_proposed

    events.clear()
    profile["company_status_detail"] = None
    profile["company_status"] = "dissolved"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    types = sorted(e.data["event_type"] for e in events)
    # Struck off is dissolved, not "strike-off dropped".
    assert types == ["dissolved", "status-changed"]
    assert company.tier is Tier.DISSOLVED
    assert not company.strike_off_proposed
    assert (
        hass.states.get("sensor.example_trading_limited_how_often_it_is_checked").state
        == "dissolved"
    )


async def test_gazette_filing_flags_strike_off(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A first gazette notice flips the sensor before the profile catches up."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    history = _new_filing(
        load_fixture("company_active/filing_history"),
        category="gazette",
        type="GAZ1",
        description="gazette-notice-compulsory",
        description_values={},
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["event_type"] for e in events] == [
        "gazette",
        "strike-off-proposed",
        "risk-changed",
    ]
    assert company.state.strike_off_notice_on is not None
    rating = hass.states.get("sensor.example_trading_limited_risk_rating")
    assert rating is not None
    assert rating.state == "red"
    assert rating.attributes["overrides"] == ["R4"]
    assert rating.attributes["reasons"][0].startswith(
        "strike-off proposed (compulsory) on "
    )
    assert (
        hass.states.get(
            "binary_sensor.example_trading_limited_proposed_strike_off"
        ).state
        == "on"
    )
    events.clear()
    history = _new_filing(
        history,
        transaction_id="NEWTRANSACTION0002",
        category="gazette",
        type="GAZ2",
        description="gazette-filings-brought-up-to-date",
        description_values={},
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    assert [e.data["event_type"] for e in events] == [
        "gazette",
        "strike-off-discontinued",
        "risk-changed",
    ]
    assert (
        hass.states.get("sensor.example_trading_limited_risk_rating").state == "green"
    )
    assert (
        hass.states.get(
            "binary_sensor.example_trading_limited_proposed_strike_off"
        ).state
        == "off"
    )


async def test_restart_does_not_replay_events(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> None:
    """Reloading with fresh snapshots costs no requests and fires nothing."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    first_calls = aioclient_mock.call_count
    assert first_calls > 5
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert ACTIVE in stored["companies"]
    assert set(stored["companies"][ACTIVE]["snapshots"]) >= {
        "profile",
        "filings",
        "officers",
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == first_calls
    assert events == []
    company = _company(entry)
    assert company.profile.last_reason == "snapshot fresh"
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "active"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_officers_active").state == "3"
    )


async def test_stale_snapshot_is_refetched_and_diffed(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a long outage the snapshot is stale, refetched, and changes are detected once."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.tick(timedelta(days=10))
    officers = load_fixture("company_active/officers")
    officers["items"][1]["resigned_on"] = "2026-09-20"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"officers": officers})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The resignation once, and the rating it moved (green to amber: a sole
    # director now, one resignation this year) since the band was stored.
    assert [e.data["event_type"] for e in events] == ["resigned", "risk-changed"]


async def test_reconciliation_refreshes_everything_weekly(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once the weekly slot passes, an unchanged probe still refreshes every dataset."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    freezer.tick(timedelta(days=12))
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    await company.probe.async_refresh()
    await hass.async_block_till_done()
    urls = {call[1].path.rsplit("/", 1)[-1] for call in aioclient_mock.mock_calls}
    assert {"filing-history", ACTIVE, "officers", "charges", "insolvency"} <= urls
    assert "persons-with-significant-control" in urls
    assert company.state.last_reconciled is not None
    assert company.state.last_reconciled > dt_util.utcnow() - timedelta(minutes=1)
    assert company.officers is not None
    assert company.officers.last_reason == "fetched"
    # Probe driven datasets never run their own timers; the probe is the scheduler.
    assert company.officers.next_run is None
    assert company.probe.next_run is not None
    assert company.profile.next_run is not None


async def test_budget_deferral_keeps_data(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """When the scheduled budget is spent the coordinator keeps its data and retries later."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    limiter = entry.runtime_data.client.limiter
    limiter.update_from_headers(
        {
            "X-Ratelimit-Remain": "10",
            "X-Ratelimit-Reset": str(int(dt_util.utcnow().timestamp()) + 200),
        }
    )
    assert not limiter.scheduled_allowed()
    await company.profile.async_refresh()
    assert company.profile.last_update_success
    assert company.profile.data is not None
    assert company.profile.last_reason.startswith("deferred")
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "active"
    )
    # A manual refresh uses the reserve.
    await company.profile.async_refresh_now(on_demand=True)
    assert company.profile.last_reason == "fetched"


async def test_errors_mark_unavailable_and_recover(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Connection errors, 429s and 404s are translated; recovery restores the state."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert company.profile.failing_since is None
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/{ACTIVE}", status=500)
    await company.profile.async_refresh()
    assert not company.profile.last_update_success
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "unavailable"
    )
    # The run of failures is dated from the first one, whatever follows.
    failing_since = company.profile.failing_since
    assert failing_since is not None
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API_BASE}/company/{ACTIVE}", status=429, headers={"Retry-After": "7"}
    )
    await company.profile.async_refresh()
    assert company.profile.next_run is not None
    assert company.profile.next_run - dt_util.utcnow() <= timedelta(seconds=8)
    assert company.profile.failing_since == failing_since
    # The limiter is blocked for those 7 seconds; scheduled work defers.
    await company.profile.async_refresh()
    assert company.profile.last_reason.startswith("deferred")
    entry.runtime_data.client.limiter.note_success()
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/{ACTIVE}", status=404)
    await company.profile.async_refresh()
    assert company.state.not_found
    assert company.profile.failing_since == failing_since
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    await company.profile.async_refresh()
    assert company.profile.last_update_success
    assert company.profile.failing_since is None
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "active"
    )


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """A 401 on any coordinator puts the entry into reauth."""
    aioclient_mock.get(f"{API_BASE}/company/{ACTIVE}", status=401)
    aioclient_mock.get(f"{API_BASE}/company/{ACTIVE}/filing-history", status=401)
    from .conftest import company_subentry, make_entry

    entry = make_entry([company_subentry(ACTIVE, subentry_id=f"sub_{ACTIVE}")])
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows
    assert flows[0]["context"]["source"] == "reauth"


# ---------------------------------------------------------------- scheduling


@pytest.mark.parametrize(
    ("when", "expected_minutes", "tier"),
    [
        ("2027-03-16 10:00", 30, Tier.DEADLINE),  # Tuesday, CS due in 9 days
        ("2027-03-16 02:00", 180, Tier.DEADLINE),
        ("2026-09-15 10:00", 360, Tier.QUIET),  # no filing for 6 months, nothing due
        ("2026-09-15 02:00", 1440, Tier.QUIET),
        ("2026-08-31 10:00", 180, None),  # bank holiday behaves as out of hours
    ],
)
async def test_probe_cadence_follows_tier_and_clock(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    freezer: FrozenDateTimeFactory,
    when: str,
    expected_minutes: int,
    tier: Tier | None,
) -> None:
    """The probe is rescheduled with the interval the spec's table gives."""
    freezer.move_to(
        dt_util.parse_datetime(when.replace(" ", "T")).replace(tzinfo=LONDON)
    )  # type: ignore[union-attr]
    entry = await setup_entry([ACTIVE], close_watch={"x"})
    company = _company(entry)
    if tier is Tier.QUIET:
        # Snapshot of the newest filing is March 2026; quiet needs no deadline within 90 days.
        assert company.tier is Tier.QUIET
    elif tier is not None:
        assert company.tier is tier
    if when.startswith("2026-08-31"):
        # 31 Aug 2026 is a bank holiday: in the deadline tier the interval is the out of hours one.
        profile = load_fixture("company_active/profile")
        profile["confirmation_statement"]["next_due"] = "2026-09-10"
        company.profile.data = company.profile.model.from_api(profile)
        company.recompute_tier()
        assert company.tier is Tier.DEADLINE
    company.probe.reschedule()
    assert company.probe.next_run is not None
    delay = (company.probe.next_run - dt_util.utcnow()).total_seconds() / 60
    assert expected_minutes * 0.84 <= delay <= expected_minutes * 1.16


async def test_close_watch_and_dissolved_cadence(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """Close watch probes every 15 minutes; a dissolved company monthly."""
    freezer.move_to("2026-09-15T09:00:00+00:00")  # 10:00 BST, Tuesday
    entry = await setup_entry([ACTIVE, "23456789"], close_watch={ACTIVE})
    watched = _company(entry)
    dissolved = _company(entry, "23456789")
    assert watched.tier is Tier.CLOSE_WATCH
    assert dissolved.tier is Tier.DISSOLVED
    watched.probe.reschedule()
    dissolved.probe.reschedule()
    assert watched.probe.next_run is not None
    assert dissolved.probe.next_run is not None
    now = dt_util.utcnow()
    assert (
        timedelta(minutes=12) <= watched.probe.next_run - now <= timedelta(minutes=18)
    )
    assert timedelta(days=25) <= dissolved.probe.next_run - now <= timedelta(days=35)
    assert (
        hass.states.get("sensor.example_trading_limited_how_often_it_is_checked").state
        == "close_watch"
    )
    attrs = hass.states.get(
        "sensor.example_trading_limited_how_often_it_is_checked"
    ).attributes
    assert attrs["probe_interval_minutes"] == 15
    assert attrs["period"] == "business hours"


async def test_timer_fires_probe(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The async_call_later timer actually runs the probe when its time comes."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert company.probe.next_run is not None
    before = aioclient_mock.call_count
    freezer.move_to(company.probe.next_run + timedelta(seconds=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    # The accounts back-fill timer fires too; only the probe counts here.
    probes = [
        c
        for c in aioclient_mock.mock_calls[before:]
        if c[1].path.endswith("/filing-history") and "category" not in c[1].query
    ]
    assert len(probes) == 1
    assert company.probe.next_run > dt_util.utcnow()


async def test_profile_cadence_near_deadline(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """Within 14 days of a deadline the profile refreshes every 6 hours."""
    freezer.move_to("2027-03-16T09:00:00+00:00")
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    company.profile.reschedule()
    assert company.profile.next_run is not None
    delay = company.profile.next_run - dt_util.utcnow()
    assert timedelta(hours=5) <= delay <= timedelta(hours=7)


async def test_cadence_multiplier_slows_everything(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """The options multiplier scales the probe interval."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(
        [ACTIVE], close_watch={ACTIVE}, options={"cadence_multiplier": 2.0}
    )
    company = _company(entry)
    company.probe.reschedule()
    assert company.probe.next_run is not None
    assert (
        timedelta(minutes=25)
        <= company.probe.next_run - dt_util.utcnow()
        <= timedelta(minutes=35)
    )


# ---------------------------------------------------------------- officers


async def test_officer_appointments_and_events(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Officer appointment changes fire the appointment event entity."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry(officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_total").state == "23"
    )
    assert (
        hass.states.get(
            "binary_sensor.jane_elizabeth_smith_currently_holds_appointments"
        ).state
        == "on"
    )
    assert (
        hass.states.get("binary_sensor.jane_elizabeth_smith_disqualified").state
        == "off"
    )
    attrs = hass.states.get(
        "binary_sensor.jane_elizabeth_smith_disqualified"
    ).attributes
    assert attrs["possible_matches"] == 1
    appointments = load_fixture("officer_many/appointments")
    appointments["active_count"] = 16
    appointments["resigned_count"] = 4
    appointments["items"][0]["resigned_on"] = "2026-09-01"
    appointments["items"][1]["appointed_to"]["company_status"] = "liquidation"
    appointments["items"].append(
        {
            **appointments["items"][2],
            "appointed_to": {
                "company_name": "BRAND NEW LIMITED",
                "company_number": "99999999",
                "company_status": "active",
            },
            "links": {"self": "/company/99999999/appointments/app-new"},
            "appointed_on": "2026-09-10",
        }
    )
    aioclient_mock.clear_requests()
    mock_officer(aioclient_mock, appointments=appointments)
    await officer.appointments.async_refresh()
    await hass.async_block_till_done()
    types = sorted(e.data["event_type"] for e in events)
    assert types == ["appointed", "company-status-changed", "resigned"]
    appointed = next(e.data for e in events if e.data["event_type"] == "appointed")
    assert appointed["company_name"] == "BRAND NEW LIMITED"
    assert appointed["officer_id"] == "officer-jane"
    state = hass.states.get("event.jane_elizabeth_smith_appointment")
    assert state is not None
    assert state.attributes["event_type"] in types
    # The register's own counts win over the derived ones when present.
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_active").state == "16"
    )
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_resigned").state
        == "4"
    )


async def test_disqualification_exact_match_sets_sensor(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Only surname, forename and month/year of birth together flip the sensor."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry(officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    assert (
        hass.states.get("binary_sensor.jane_elizabeth_smith_disqualified").state
        == "off"
    )
    aioclient_mock.clear_requests()
    mock_officer(
        aioclient_mock,
        disqualified_search=load_fixture("officer_many/disqualified_search"),
    )
    await officer.disqualification.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.jane_elizabeth_smith_disqualified")
    assert state is not None
    assert state.state == "on"
    assert state.attributes["disqualified_until"] == "2031-01-09"
    assert state.attributes["company_names"] == ["FAILED WIDGETS LIMITED"]
    assert state.attributes["possible_matches"] == 1  # the other Jane, born 1981
    assert [e.data["event_type"] for e in events] == ["disqualified"]


async def test_new_register_record_is_followed_automatically(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A record with the same name and date of birth is pooled in; a namesake is not."""
    from .conftest import appointments_at

    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry(officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    records = hass.states.get("sensor.jane_elizabeth_smith_register_records")
    assert records.state == "1"
    assert records.attributes["records"] == ["officer-jane"]
    # The other Jane SMITH has a different date of birth: listed, never added.
    assert [m["name"] for m in records.attributes["possible_matches"]] == ["Jane SMITH"]

    # The register opens a new record for her, with the same name and birth date.
    search = load_fixture("search/officers")
    search["items"].append(
        {
            **search["items"][0],
            "links": {"self": "/officers/officer-jane-2/appointments"},
            "appointment_count": 1,
        }
    )
    aioclient_mock.clear_requests()
    mock_officer(aioclient_mock, records_search=search)
    aioclient_mock.get(
        f"{API_BASE}/officers/officer-jane-2/appointments",
        json=appointments_at("34567890"),
    )
    await officer.records.async_refresh()
    await hass.async_block_till_done()
    assert entry.subentries["sub_officer"].data["officer_ids"] == [
        "officer-jane",
        "officer-jane-2",
    ]
    assert officer.officer_ids == ["officer-jane", "officer-jane-2"]
    records = hass.states.get("sensor.jane_elizabeth_smith_register_records")
    assert records.state == "2"
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_active").state == "19"
    )
    new_record = [e.data for e in events if e.data["event_type"] == "new-record"]
    assert new_record == [
        {
            "officer_id": "officer-jane-2",
            "officer_name": "Jane Elizabeth SMITH",
            "name": "Jane Elizabeth SMITH",
            "kind": "appointment",
            "event_type": "new-record",
        }
    ]
    # Checking again finds nothing new and raises nothing twice.
    await officer.records.async_refresh()
    await hass.async_block_till_done()
    assert len([e for e in events if e.data["event_type"] == "new-record"]) == 1

    # A record already followed as a separate person is never pooled in.
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    search["items"].append(
        {
            **search["items"][0],
            "links": {"self": "/officers/officer-jane-3/appointments"},
        }
    )
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {"officer_id": "officer-jane-3", "officer_name": "Jane (other)"}
            ),
            subentry_type="officer",
            title="Jane (other)",
            unique_id="officer-jane-3",
            subentry_id="sub_jane_3",
        ),
    )
    aioclient_mock.clear_requests()
    mock_officer(aioclient_mock, records_search=search)
    mock_officer(aioclient_mock, "officer-jane-3")
    await hass.async_block_till_done()
    await officer.records.async_refresh()
    await hass.async_block_till_done()
    assert officer.officer_ids == ["officer-jane", "officer-jane-2"]


def test_match_records_rules() -> None:
    """Records already followed are skipped; names must agree exactly."""
    from custom_components.companies_house.coordinator import match_records

    dob = DateOfBirth(month=6, year=1978)
    search = load_fixture("search/officers")
    found, possible = match_records("Jane Elizabeth SMITH", dob, ["x"], search)
    assert found == ["officer-jane"]
    assert [m.officer_id for m in possible] == ["officer-other-jane"]
    found, possible = match_records(
        "Jane Elizabeth SMITH", dob, ["officer-jane", "officer-other-jane"], search
    )
    assert (found, possible) == ([], [])
    # Without a date of birth to confirm with, a name match is only possible.
    found, possible = match_records("Jane Elizabeth SMITH", None, [], search)
    assert found == []
    assert [m.officer_id for m in possible] == ["officer-jane", "officer-other-jane"]
    assert match_records("Someone ELSE", dob, [], search) == ([], [])
    search["items"].append("junk")
    assert match_records("Jane Elizabeth SMITH", dob, ["officer-jane"], search)[0] == []


def test_match_disqualification_rules() -> None:
    """Name only never matches; name plus month and year does."""
    dob = DateOfBirth(month=6, year=1978)
    search = load_fixture("officer_many/disqualified_search")
    exact, candidates = match_disqualification("Jane Elizabeth SMITH", dob, search)
    assert exact is not None
    assert exact.disqualified_officer_id == "dq-exact-jane"
    assert len(candidates) == 1
    exact, candidates = match_disqualification("SMITH, Jane Elizabeth", dob, search)
    assert exact is not None
    # Name only.
    exact, candidates = match_disqualification(
        "Jane SMITH", DateOfBirth(month=1, year=1990), search
    )
    assert exact is None
    assert len(candidates) == 2
    # No date of birth known at all never matches.
    exact, _ = match_disqualification("Jane Elizabeth SMITH", None, search)
    assert exact is None
    # Different surname with the same date of birth never matches.
    exact, _ = match_disqualification("Jane Elizabeth JONES", dob, search)
    assert exact is None
    # Titles and punctuation are ignored; junk items are skipped.
    search["items"].append("junk")
    exact, _ = match_disqualification("Ms. Jane E. SMITH", dob, search)
    assert exact is not None
    assert match_disqualification("", dob, {"items": []}) == (None, [])


async def test_not_found_company_and_officer_flag_state(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    setup_entry: Callable[..., Any],
) -> None:
    """A 404 on a profile or an appointments list is remembered for repairs."""
    entry = await setup_entry([ACTIVE], officers=True)
    company = _company(entry)
    officer = entry.runtime_data.officers["sub_officer"]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/{ACTIVE}", status=404)
    aioclient_mock.get(f"{API_BASE}/officers/officer-jane/appointments", status=404)
    await company.profile.async_refresh()
    await officer.appointments.async_refresh()
    assert company.state.not_found
    assert officer.state.not_found


async def test_budget_error_on_first_fetch_fails_update(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """A coordinator with no data yet cannot defer; it fails and retries."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert company.structure is not None
    company.structure.data = None
    company.structure.fetched_at = None

    async def _boom(priority: Any) -> Any:
        raise CompaniesHouseBudgetError(30, "budget")

    company.structure._fetch = _boom  # type: ignore[method-assign]
    await company.structure.async_refresh()
    assert not company.structure.last_update_success
    assert company.structure.failing_since is not None
