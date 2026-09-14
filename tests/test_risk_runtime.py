"""Tests for the risk rating inside Home Assistant: sensor, store, events, people."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.const import DOMAIN, EVENT_COMPANIES_HOUSE
from custom_components.companies_house.coordinator import (
    ChangeEvent,
    CompanyRuntime,
    signal_changes,
    signal_risk,
)
from custom_components.companies_house.risk import BASIS, SCORING_VERSION

from .conftest import load_fixture, mock_company, mock_officer

ACTIVE = "12345678"
SENSOR = "sensor.example_trading_limited_risk_rating"


def _company(entry: Any, number: str = ACTIVE) -> CompanyRuntime:
    company: CompanyRuntime = entry.runtime_data.companies[f"sub_{number}"]
    return company


async def test_sensor_state_attributes_and_store(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    hass_storage: dict[str, Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """The rating is a sensor with the workings as attributes, remembered on disk."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE, "34567890"])
    company = _company(entry)
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "green"
    attrs = state.attributes
    assert attrs["score"] == 2
    assert attrs["reason"] == "Green: 1 outstanding charge"
    assert attrs["reasons"] == ["1 outstanding charge"]
    assert attrs["overrides"] == []
    assert attrs["coverage"]["officers"] == "2026-09-15T09:00:00+00:00"
    assert attrs["data_age_days"] == 0
    assert attrs["computed_at"] == "2026-09-15T09:00:00+00:00"
    assert attrs["scoring_version"] == SCORING_VERSION
    assert attrs["basis"] == BASIS
    assert attrs["info"] == {"accounts_disclosure": "minimal"}
    red = hass.states.get("sensor.sunset_retail_limited_risk_rating")
    assert red is not None
    assert red.state == "red"
    assert red.attributes["overrides"] == ["R2"]
    # The first rating is remembered but never announced.
    assert company.state.risk_band == "green"
    assert company.state.risk_score == 2
    assert company.risk is not None
    assert company.risk.band == "green"
    assert [e for e in events if e.data["event_type"] == "risk-changed"] == []

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["companies"]
    assert stored[ACTIVE]["risk_band"] == "green"
    assert stored[ACTIVE]["risk_score"] == 2
    assert stored["34567890"]["risk_band"] == "red"
    assert stored["34567890"]["risk_score"] == 100

    # A restart rates again from the snapshots and announces nothing.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(SENSOR).state == "green"
    assert [e for e in events if e.data["event_type"] == "risk-changed"] == []


async def test_old_store_files_without_a_band_are_tolerated(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    hass_storage: dict[str, Any],
) -> None:
    """A store written before the rating existed loads, and rates without an event."""
    from custom_components.companies_house.store import CompanyState

    state = CompanyState.from_dict({"risk_band": 3, "risk_score": "high"})
    assert state.risk_band is None
    assert state.risk_score is None
    state = CompanyState.from_dict({"risk_band": "amber", "risk_score": True})
    assert state.risk_band == "amber"
    assert state.risk_score is None
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]["companies"][ACTIVE]
    del stored["risk_band"]
    del stored["risk_score"]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    company = _company(entry)
    assert company.state.risk_band == "green"
    assert events == []


async def test_score_changes_are_remembered_without_an_event(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A move within the band updates the stored score but fires nothing."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert company.state.risk_score == 2
    charges = load_fixture("company_active/charges")
    charges["items"][1]["status"] = "outstanding"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"charges": charges})
    await company.charges.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "green"
    assert hass.states.get(SENSOR).attributes["score"] == 2  # still 1 to 2 charges
    assert company.state.risk_score == 2
    profile = load_fixture("company_active/profile")
    profile["registered_office_is_in_dispute"] = True
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": profile})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "amber"
    assert company.state.risk_score == 10
    assert [e.data["event_type"] for e in events if e.data["kind"] == "status"] == [
        "risk-changed"
    ]
    # Back to green: announced again, because the band moved.
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "green"
    assert [e.data["new_band"] for e in events if e.data["kind"] == "status"] == [
        "amber",
        "green",
    ]


async def test_company_gone_from_the_register_is_unknown(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A 404 on the profile makes the rating unknown, and it comes back with the company."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"profile": None})
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert company.state.not_found
    assert company.risk is not None
    assert company.risk.band is None
    assert company.risk.info == {"error": "not found"}
    assert company.state.risk_band == "green"  # the last real band is kept
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    await company.profile.async_refresh()
    await hass.async_block_till_done()
    assert not company.state.not_found
    assert hass.states.get(SENSOR).state == "green"
    assert [e for e in events if e.data["event_type"] == "risk-changed"] == []


async def test_sensor_rerenders_on_every_signal(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
) -> None:
    """Change events, re-ratings and other datasets all refresh the sensor."""
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    assert company.risk is not None
    assert hass.states.get(SENSOR).state == "green"
    subentry_id = company.subentry.subentry_id

    company.risk = replace(company.risk, band="amber")
    async_dispatcher_send(
        hass, signal_changes(subentry_id), ChangeEvent("officer", "resigned", {})
    )
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "amber"

    company.risk = replace(company.risk, band="red")
    async_dispatcher_send(hass, signal_risk(subentry_id))
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "red"

    company.risk = replace(company.risk, band="green")
    assert company.officers is not None
    company.officers.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "green"

    # A band outside the options, or no rating at all, shows as unknown.
    company.risk = replace(company.risk, band="purple")
    company.officers.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get(SENSOR).state == "unknown"
    company.risk = None
    company.officers.async_update_listeners()
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "unknown"
    assert "score" not in state.attributes


async def test_disqualified_follower_turns_their_company_red(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A followed director's disqualification re-rates the company they sit on."""
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE], officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    assert hass.states.get(SENSOR).state == "green"
    aioclient_mock.clear_requests()
    mock_officer(
        aioclient_mock,
        disqualified_search=load_fixture("officer_many/disqualified_search"),
    )
    await officer.disqualification.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "red"
    assert state.attributes["overrides"] == ["R7"]
    assert state.attributes["reasons"][0] == (
        "Jane Elizabeth SMITH is a disqualified director until 9 Jan 2031"
    )
    rated = [e.data for e in events if e.data["event_type"] == "risk-changed"]
    assert [(r["old_band"], r["new_band"]) for r in rated] == [("green", "red")]
    assert rated[0]["company_number"] == ACTIVE


async def test_followers_failed_companies_count_against_this_one(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A follower still serving at companies now in liquidation re-rates on their poll."""
    entry = await setup_entry([ACTIVE], officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    appointments = load_fixture("officer_many/appointments")
    for item in appointments["items"][:2]:
        item["appointed_to"]["company_status"] = "liquidation"
    aioclient_mock.clear_requests()
    mock_officer(aioclient_mock, appointments=appointments)
    await officer.appointments.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "amber"
    # Two liquidations (6 each), three dissolved companies (3), one charge (2).
    assert state.attributes["score"] == 17
    reasons = state.attributes["reasons"]
    assert reasons[0].startswith("Jane Elizabeth SMITH is a director of ")
    assert reasons[0].endswith(", now in liquidation")
    assert reasons[1].endswith(", now in liquidation")
    assert "Jane Elizabeth SMITH has been a director of 3 dissolved companies" in (
        reasons
    )


async def test_rerate_skips_companies_without_a_profile(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
) -> None:
    """The people-side re-rate leaves unrated companies alone and copes with no runtime."""
    entry = await setup_entry([ACTIVE], officers=True)
    officer = entry.runtime_data.officers["sub_officer"]
    company = _company(entry)
    company.profile.data = None
    company.risk = None
    officer.rerate_companies()
    assert company.risk is None
    runtime = entry.runtime_data
    del entry.runtime_data
    officer.rerate_companies()
    assert company._tracked_people() == []
    entry.runtime_data = runtime
