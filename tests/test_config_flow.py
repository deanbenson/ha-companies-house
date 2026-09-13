"""Tests for the config, options and subentry flows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.config_flow import key_unique_id
from custom_components.companies_house.const import (
    API_BASE,
    CONF_API_KEY,
    CONF_CADENCE_MULTIPLIER,
    CONF_CLOSE_WATCH,
    CONF_COMPANY_NAME,
    CONF_COMPANY_NUMBER,
    CONF_DATASETS,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    CONF_DOCUMENT_DIRECTORY,
    CONF_DUE_SOON_DAYS,
    CONF_LABEL,
    CONF_MAX_PAGES,
    CONF_OFFICER_ID,
    CONF_OFFICER_NAME,
    CONF_POSTCODE,
    CONF_QUERY,
    CONF_SELECTION,
    DOMAIN,
    SUBENTRY_TYPE_COMPANY,
    SUBENTRY_TYPE_OFFICER,
)

from .conftest import TEST_API_KEY, load_fixture, mock_search

VALIDATE_URL = f"{API_BASE}/search/companies?q=test&items_per_page=1"


async def test_user_flow_creates_entry(
    hass: HomeAssistant, validate_ok: AiohttpClientMocker
) -> None:
    """A working key creates the account entry with a hashed unique id."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: f" {TEST_API_KEY} "}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Companies House"
    assert result["data"] == {CONF_API_KEY: TEST_API_KEY}
    assert result["result"].unique_id == key_unique_id(TEST_API_KEY)
    assert TEST_API_KEY not in result["result"].unique_id


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, "invalid_auth"), (429, "rate_limited"), (500, "cannot_connect")],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int, error: str
) -> None:
    """Each failure maps to its error and the form can be resubmitted."""
    aioclient_mock.get(VALIDATE_URL, status=status)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: TEST_API_KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    aioclient_mock.clear_requests()
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("search/validate"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: TEST_API_KEY}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_rejects_duplicate_key(
    hass: HomeAssistant, validate_ok: AiohttpClientMocker
) -> None:
    """The same key cannot be added twice."""
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_API_KEY: TEST_API_KEY},
        unique_id=key_unique_id(TEST_API_KEY),
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: TEST_API_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Reauth replaces the key and unique id, and reloads."""
    entry = await setup_entry()
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    aioclient_mock.clear_requests()
    aioclient_mock.get(VALIDATE_URL, status=401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("search/validate"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new-key"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_API_KEY] == "new-key"
    assert entry.unique_id == key_unique_id("new-key")


async def test_reconfigure_flow_rejects_key_of_other_entry(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Reconfigure cannot switch to a key another entry already uses."""
    entry = await setup_entry()
    MockConfigEntry(
        domain=DOMAIN, data={CONF_API_KEY: "other"}, unique_id=key_unique_id("other")
    ).add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "other"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Re-entering the same key is fine and reloads.
    result = await entry.start_reconfigure_flow(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(VALIDATE_URL, json=load_fixture("search/validate"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: TEST_API_KEY}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"


async def test_options_flow(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Options save, show the projection, and refuse a configuration over budget."""
    entry = await setup_entry(["12345678"])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert float(result["description_placeholders"]["projected"]) < 5
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DUE_SOON_DAYS: 14,
            CONF_DOCUMENT_DIRECTORY: "/media/ch",
            CONF_MAX_PAGES: 5,
            CONF_CADENCE_MULTIPLIER: 2.0,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_DUE_SOON_DAYS] == 14
    assert entry.options[CONF_CADENCE_MULTIPLIER] == 2.0
    state = hass.states.get("binary_sensor.example_trading_limited_accounts_due_soon")
    assert state is not None
    assert state.attributes["threshold_days"] == 14


async def test_options_flow_refuses_over_budget(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projection above the cap is refused with an error."""
    entry = await setup_entry(["12345678"])
    monkeypatch.setattr(
        "custom_components.companies_house.config_flow.projected_requests_per_window",
        lambda *args, **kwargs: 999.0,
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_DUE_SOON_DAYS: 30,
            CONF_DOCUMENT_DIRECTORY: "/media/ch",
            CONF_MAX_PAGES: 10,
            CONF_CADENCE_MULTIPLIER: 1.0,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "budget_exceeded"}


async def test_options_projection_when_not_loaded(hass: HomeAssistant) -> None:
    """The projection falls back to counting subentries when the entry is not loaded."""
    from .conftest import company_subentry, make_entry, officer_subentry

    entry = make_entry([company_subentry("12345678"), officer_subentry()])
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert float(result["description_placeholders"]["projected"]) > 0


# ---------------------------------------------------------------- company subentry


async def _start_subentry(
    hass: HomeAssistant, entry: MockConfigEntry, kind: str
) -> dict[str, Any]:
    result: dict[str, Any] = await hass.config_entries.subentries.async_init(
        (entry.entry_id, kind), context={"source": config_entries.SOURCE_USER}
    )
    return result


async def test_company_subentry_search_and_add(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Search, pick from labelled results, confirm datasets, create the subentry."""
    entry = await setup_entry()
    mock_search(aioclient_mock)
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_COMPANY)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "example", CONF_POSTCODE: ""}
    )
    assert result["step_id"] == "select"
    options = result["data_schema"].schema[CONF_SELECTION].config["options"]
    assert (
        options[0]["label"]
        == "EXAMPLE TRADING LIMITED (12345678) - active - incorporated 2015"
    )
    assert options[1]["label"].startswith(
        "EXAMPLE TRADING (NORTH) LIMITED (87654321) - dissolved"
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "12345678"}
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"] == {
        "company": "EXAMPLE TRADING LIMITED",
        "number": "12345678",
    }
    from .conftest import mock_company

    mock_company(aioclient_mock, "12345678")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_DATASETS: ["officers", "charges"],
            CONF_CLOSE_WATCH: True,
            CONF_LABEL: "Contractor",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EXAMPLE TRADING LIMITED (Contractor)"
    assert result["unique_id"] == "12345678"
    subentry = next(iter(entry.subentries.values()))
    assert subentry.data == {
        CONF_COMPANY_NUMBER: "12345678",
        CONF_COMPANY_NAME: "EXAMPLE TRADING LIMITED",
        CONF_DATASETS: ["officers", "charges"],
        CONF_CLOSE_WATCH: True,
        CONF_LABEL: "Contractor",
    }
    # The entry reloaded and set the company up with only the chosen datasets.
    company = entry.runtime_data.companies[subentry.subentry_id]
    assert company.close_watch
    assert company.psc is None
    assert company.officers is not None
    assert hass.states.get("sensor.example_trading_limited_officers_active") is not None
    assert (
        hass.states.get(
            "sensor.example_trading_limited_people_with_significant_control"
        )
        is None
    )


async def test_company_subentry_postcode_search_and_errors(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A postcode uses the advanced search; empty and failing searches show errors."""
    entry = await setup_entry()
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_COMPANY)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "", CONF_POSTCODE: ""}
    )
    assert result["errors"] == {"base": "no_results"}
    aioclient_mock.get(f"{API_BASE}/advanced-search/companies", status=500)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "", CONF_POSTCODE: "RG1 2AB"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API_BASE}/advanced-search/companies",
        json={
            "items": [
                {
                    "company_name": "EXAMPLE TRADING LIMITED",
                    "company_number": "12345678",
                    "company_status": "active",
                    "date_of_creation": "2015-03-12",
                },
                {"nope": 1},
            ]
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "example", CONF_POSTCODE: "RG1 2AB"}
    )
    assert result["step_id"] == "select"
    call = aioclient_mock.mock_calls[-1]
    assert call[1].query["location"] == "RG1 2AB"
    assert call[1].query["company_name_includes"] == "example"


async def test_company_subentry_already_configured_and_auth_abort(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A monitored company is refused; a rejected key aborts the flow."""
    entry = await setup_entry(["12345678"])
    mock_search(aioclient_mock)
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_COMPANY)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "example", CONF_POSTCODE: ""}
    )
    options = result["data_schema"].schema[CONF_SELECTION].config["options"]
    assert options[0]["label"].endswith("incorporated 2015 (already watching)")
    assert "(already" not in options[1]["label"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "12345678"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/search/companies", status=401)
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_COMPANY)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "example", CONF_POSTCODE: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


async def test_company_subentry_reconfigure_settings(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """The reconfigure menu leads to the settings form which updates the subentry."""
    entry = await setup_entry(["12345678"])
    subentry = entry.subentries["sub_12345678"]
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": subentry.subentry_id,
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["settings", "track_officer"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {CONF_DATASETS: ["psc"], CONF_CLOSE_WATCH: True, CONF_LABEL: "Ours"},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert subentry.data[CONF_DATASETS] == ["psc"]
    assert subentry.data[CONF_CLOSE_WATCH] is True
    assert subentry.title == "EXAMPLE TRADING LIMITED (Ours)"
    company = entry.runtime_data.companies["sub_12345678"]
    assert company.close_watch
    assert company.officers is None


async def test_track_officer_shortcut(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """One click on a current officer creates the officer subentry."""
    entry = await setup_entry(["12345678"])
    subentry = entry.subentries["sub_12345678"]
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": subentry.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "track_officer"}
    )
    assert result["step_id"] == "track_officer"
    options = result["data_schema"].schema[CONF_SELECTION].config["options"]
    labels = [o["label"] for o in options]
    assert "SMITH, Jane Elizabeth - director - born 06/1978" in labels
    assert all("BROWN" not in label for label in labels)  # resigned
    from .conftest import mock_officer

    mock_officer(aioclient_mock)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "officer-jane"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "officer_added"
    officer = next(
        s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_OFFICER
    )
    assert officer.unique_id == "officer-jane"
    assert officer.data == {
        CONF_OFFICER_ID: "officer-jane",
        CONF_OFFICER_NAME: "SMITH, Jane Elizabeth",
        CONF_DATE_OF_BIRTH_MONTH: 6,
        CONF_DATE_OF_BIRTH_YEAR: 1978,
    }
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_active") is not None
    )

    # Doing it again aborts because the officer is already tracked.
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": subentry.subentry_id,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "track_officer"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "officer-jane"}
    )
    assert result["reason"] == "officer_already_configured"


async def test_track_officer_when_not_loaded_and_no_officers(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Without a loaded runtime the officers are fetched; none aborts; failure errors."""
    from .conftest import company_subentry, make_entry

    entry = make_entry([company_subentry("12345678", subentry_id="sub_12345678")])
    entry.add_to_hass(hass)
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/officers", json={"items": [], "total_results": 0}
    )
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": "sub_12345678",
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "track_officer"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_officers"
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/12345678/officers", status=500)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": "sub_12345678",
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "track_officer"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------- officer subentry


async def test_officer_subentry_search_and_add(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Search, pick by date of birth, resolve the officer id from the link."""
    entry = await setup_entry()
    mock_search(aioclient_mock)
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_OFFICER)
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "jane smith"}
    )
    assert result["step_id"] == "select"
    options = result["data_schema"].schema[CONF_SELECTION].config["options"]
    assert (
        options[0]["label"] == "Jane Elizabeth SMITH - born 06/1978 - 23 appointments"
    )
    assert options[1]["label"] == "Jane SMITH - born 02/1981 - 1 appointment"
    from .conftest import mock_officer

    mock_officer(aioclient_mock)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "officer-jane"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Jane Elizabeth SMITH"
    assert result["unique_id"] == "officer-jane"
    assert result["data"][CONF_DATE_OF_BIRTH_YEAR] == 1978
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_active") is not None
    )

    # Someone already followed is marked as such, and picking them is refused.
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_OFFICER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "jane smith"}
    )
    options = result["data_schema"].schema[CONF_SELECTION].config["options"]
    assert options[0]["label"].endswith("23 appointments (already following)")
    assert "(already" not in options[1]["label"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_SELECTION: "officer-jane"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_officer_subentry_errors(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """No results, connection failure and rejected key are handled."""
    entry = await setup_entry()
    aioclient_mock.get(
        f"{API_BASE}/search/officers", json={"items": [{"title": "No link"}]}
    )
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_OFFICER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "nobody"}
    )
    assert result["errors"] == {"base": "no_results"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/search/officers", status=500)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "nobody"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/search/officers", status=401)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "nobody"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


async def test_officer_subentry_reconfigure(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """The display name can be changed."""
    entry = await setup_entry(officers=True)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_OFFICER),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": "sub_officer",
        },
    )
    assert result["step_id"] == "reconfigure"
    assert result["description_placeholders"] == {"officer_id": "officer-jane"}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_OFFICER_NAME: "Jane Smith (our director)"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries["sub_officer"].title == "Jane Smith (our director)"
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_OFFICER),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": "sub_officer",
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_OFFICER_NAME: "  "}
    )
    assert (
        entry.subentries["sub_officer"].data[CONF_OFFICER_NAME]
        == "Jane Smith (our director)"
    )


async def test_search_404_means_no_results_and_input_is_kept(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The API answers a search with no hits with a 404; the form keeps what was typed."""
    entry = await setup_entry()
    aioclient_mock.get(f"{API_BASE}/advanced-search/companies", status=404)
    aioclient_mock.get(f"{API_BASE}/search/officers", status=404)
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_COMPANY)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "nothing", CONF_POSTCODE: "ZZ99 9ZZ"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_results"}
    suggested = {
        getattr(k, "schema", k): k.description["suggested_value"]
        for k in result["data_schema"].schema
    }
    assert suggested == {CONF_QUERY: "nothing", CONF_POSTCODE: "ZZ99 9ZZ"}
    result = await _start_subentry(hass, entry, SUBENTRY_TYPE_OFFICER)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_QUERY: "nobody at all"}
    )
    assert result["errors"] == {"base": "no_results"}


async def test_track_officer_404_means_none(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 404 from the officer list means the company has no officers."""
    from .conftest import company_subentry, make_entry

    entry = make_entry([company_subentry("12345678", subentry_id="sub_12345678")])
    entry.add_to_hass(hass)
    aioclient_mock.get(f"{API_BASE}/company/12345678/officers", status=404)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMPANY),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": "sub_12345678",
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": "track_officer"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_officers"
