"""Tests for the actions and the LLM API."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import async_capture_events
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
import yaml

from custom_components.companies_house.const import (
    API_BASE,
    DOCUMENT_API_BASE,
    DOMAIN,
    EVENT_DOCUMENT_DOWNLOADED,
)
from custom_components.companies_house.services import (
    ACTIONS,
    action_fields,
    normalise_company_number,
    sanitise_filename,
)

from .conftest import load_fixture, mock_company

SERVICES_YAML = (
    Path(__file__).parents[1]
    / "custom_components"
    / "companies_house"
    / "services.yaml"
)
STRINGS = (
    Path(__file__).parents[1] / "custom_components" / "companies_house" / "strings.json"
)


def test_services_yaml_and_strings_match_schemas() -> None:
    """Every action and field is documented in services.yaml and strings.json."""
    documented = yaml.safe_load(SERVICES_YAML.read_text())
    import json

    strings = json.loads(STRINGS.read_text())["services"]
    assert set(documented) == {a.name for a in ACTIONS}
    assert set(strings) == {a.name for a in ACTIONS}
    for action in ACTIONS:
        fields = set(action_fields(action))
        assert set(documented[action.name]["fields"]) == fields, action.name
        assert set(strings[action.name]["fields"]) == fields, action.name


def test_normalise_company_number() -> None:
    """Company numbers are upper cased and zero padded; junk is refused."""
    assert normalise_company_number(" 1234567 ") == "01234567"
    assert normalise_company_number("sc123456") == "SC123456"
    assert normalise_company_number("oc 12345") == "OC012345"
    with pytest.raises(ServiceValidationError):
        normalise_company_number("not-a-number")
    with pytest.raises(ServiceValidationError):
        normalise_company_number("123")


def test_sanitise_filename() -> None:
    """Unsafe characters go, whitespace collapses, length is capped."""
    name = sanitise_filename('2026-03-11 - Companies House - A/B: "C"  (X)', ".pdf")
    assert name == "2026-03-11 - Companies House - AB C (X).pdf"
    long = sanitise_filename("x" * 300, ".pdf")
    assert len(long) == 200
    assert long.endswith(".pdf")


async def test_read_actions_return_responses(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Every read action reaches its endpoint and returns the payload."""
    await setup_entry()
    n = "12345678"
    psc = f"{API_BASE}/company/{n}/persons-with-significant-control"
    routes: dict[str, tuple[str, dict[str, Any]]] = {
        "search_companies": (
            f"{API_BASE}/search/companies?q=example",
            {"query": "example", "items_per_page": 5},
        ),
        "advanced_search": (
            f"{API_BASE}/advanced-search/companies?company_status=active%2Cdissolved",
            {"company_status": ["active", "dissolved"], "location": "Reading"},
        ),
        "alphabetical_search": (
            f"{API_BASE}/alphabetical-search/companies?q=a",
            {"query": "a"},
        ),
        "dissolved_search": (
            f"{API_BASE}/dissolved-search/companies?q=a",
            {"query": "a", "search_type": "alphabetical"},
        ),
        "search_officers": (f"{API_BASE}/search/officers?q=smith", {"query": "smith"}),
        "search_disqualified_officers": (
            f"{API_BASE}/search/disqualified-officers?q=smith",
            {"query": "smith"},
        ),
        "search_all": (f"{API_BASE}/search?q=smith", {"query": "smith"}),
        "get_company": (f"{API_BASE}/company/{n}", {"company_number": "12345678"}),
        "get_registered_office": (
            f"{API_BASE}/company/{n}/registered-office-address",
            {"company_number": n},
        ),
        "get_officers": (
            f"{API_BASE}/company/{n}/officers?register_view=true",
            {"company_number": n, "register_view": True, "order_by": "surname"},
        ),
        "get_officer_appointment": (
            f"{API_BASE}/company/{n}/appointments/a1",
            {"company_number": n, "appointment_id": "a1"},
        ),
        "get_officer_appointments": (
            f"{API_BASE}/officers/o1/appointments",
            {"officer_id": "o1"},
        ),
        "get_disqualification": (
            f"{API_BASE}/disqualified-officers/natural/o1",
            {"officer_id": "o1"},
        ),
        "get_charges": (f"{API_BASE}/company/{n}/charges", {"company_number": n}),
        "get_charge": (
            f"{API_BASE}/company/{n}/charges/c1",
            {"company_number": n, "charge_id": "c1"},
        ),
        "get_psc": (psc, {"company_number": n}),
        "get_psc_detail": (
            f"{psc}/individual/p1",
            {"company_number": n, "kind": "individual", "notification_id": "p1"},
        ),
        "get_psc_statements": (f"{psc}-statements", {"company_number": n}),
        "get_insolvency": (f"{API_BASE}/company/{n}/insolvency", {"company_number": n}),
        "get_exemptions": (f"{API_BASE}/company/{n}/exemptions", {"company_number": n}),
        "get_registers": (f"{API_BASE}/company/{n}/registers", {"company_number": n}),
        "get_uk_establishments": (
            f"{API_BASE}/company/{n}/uk-establishments",
            {"company_number": n},
        ),
        "get_document_metadata": (
            f"{DOCUMENT_API_BASE}/document/d1",
            {"document_id": "d1"},
        ),
    }
    for name, (url, _) in routes.items():
        aioclient_mock.get(url, json={"ok": name})
    for name, (_, data) in routes.items():
        response = await hass.services.async_call(
            DOMAIN, name, data, blocking=True, return_response=True
        )
        assert response == {"ok": name}, name


async def test_filing_actions_render_descriptions(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Filing history and filing responses carry rendered descriptions."""
    await setup_entry()
    history = load_fixture("company_active/filing_history")
    aioclient_mock.get(f"{API_BASE}/company/12345678/filing-history", json=history)
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/filing-history/t1", json=history["items"][0]
    )
    aioclient_mock.get(f"{API_BASE}/company/00000000/filing-history", status=404)
    response = await hass.services.async_call(
        DOMAIN,
        "get_filing_history",
        {"company_number": "12345678", "category": "accounts", "items_per_page": 5},
        blocking=True,
        return_response=True,
    )
    assert response["items"][0]["rendered_description"].startswith(
        "Confirmation statement made on"
    )
    assert aioclient_mock.mock_calls[-1][1].query["category"] == "accounts"
    response = await hass.services.async_call(
        DOMAIN,
        "get_filing",
        {"company_number": "12345678", "transaction_id": "t1"},
        blocking=True,
        return_response=True,
    )
    assert "rendered_description" in response
    response = await hass.services.async_call(
        DOMAIN,
        "get_filing_history",
        {"company_number": "00000000"},
        blocking=True,
        return_response=True,
    )
    assert response == {"items": [], "total_count": 0}


async def test_action_errors(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Bad input raises a validation error; API failures a translated HA error."""
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            "get_company",
            {"company_number": "12345678"},
            blocking=True,
            return_response=True,
        )
    assert excinfo.value.translation_key == "no_entry"
    entry = await setup_entry()
    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            "get_company",
            {"company_number": "12345678", "config_entry_id": "nope"},
            blocking=True,
            return_response=True,
        )
    assert excinfo.value.translation_key == "entry_not_loaded"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "get_company",
            {"company_number": "bad!"},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "advanced_search",
            {"incorporated_from": "yesterday"},
            blocking=True,
            return_response=True,
        )
    aioclient_mock.get(f"{API_BASE}/company/12345678", status=500)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "get_company",
            {"company_number": "12345678", "config_entry_id": entry.entry_id},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "api_error"


async def test_refresh_action(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Refresh targets a device or the whole entry and uses the on demand budget."""
    entry = await setup_entry(["12345678", "23456789"], officers=True)
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "company_12345678"), entry.entry_id
    )
    assert device is not None
    before = aioclient_mock.call_count
    await hass.services.async_call(
        DOMAIN,
        "refresh",
        {"device_id": device.id, "datasets": ["profile"]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == before + 1
    assert aioclient_mock.mock_calls[-1][1].path == "/company/12345678"
    before = aioclient_mock.call_count
    await hass.services.async_call(
        DOMAIN, "refresh", {"config_entry_id": entry.entry_id}, blocking=True
    )
    await hass.async_block_till_done()
    assert aioclient_mock.call_count > before + 10
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "refresh", {"device_id": ["unknown"]}, blocking=True
        )


async def test_download_document(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """Documents are named from the filing, never overwritten, and an event fires."""
    events = async_capture_events(hass, EVENT_DOCUMENT_DOWNLOADED)
    entry = await setup_entry(
        ["12345678"], options={"document_directory": str(tmp_path)}
    )
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    hass.config.media_dirs["local"] = str(tmp_path)
    metadata = load_fixture("document/metadata")
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-1", json=metadata)
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-1/content",
        status=302,
        headers={"Location": "https://s3.example.invalid/doc"},
    )
    aioclient_mock.get("https://s3.example.invalid/doc", content=b"%PDF-1.4 one")
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/filing-history/MzQwMDAwMDAwMDAwMDAwMDAx",
        json=load_fixture("company_active/filing_history")["items"][0],
    )
    response = await hass.services.async_call(
        DOMAIN,
        "download_document",
        {
            "document_id": "doc-1",
            "company_number": "12345678",
            "transaction_id": "MzQwMDAwMDAwMDAwMDAwMDAx",
        },
        blocking=True,
        return_response=True,
    )
    assert response is not None
    path = Path(response["path"])
    assert path.parent == tmp_path / "12345678 EXAMPLE TRADING LIMITED"
    assert path.name == (
        "2026-03-14 - Companies House - Confirmation statement made on 11 March 2026 "
        "with no updates (EXAMPLE TRADING LIMITED).pdf"
    )
    assert path.read_bytes() == b"%PDF-1.4 one"  # noqa: ASYNC240
    assert response["already_existed"] is False
    # Under a media folder the file gets a media-source id, so an AI task or an
    # email action can take it as an attachment.
    assert response["media_content_id"] == (
        "media-source://media_source/local/12345678 EXAMPLE TRADING LIMITED/"
        + path.name
    )
    assert len(events) == 1
    assert events[0].data["path"] == str(path)

    # Same content again returns the existing file.
    response = await hass.services.async_call(
        DOMAIN,
        "download_document",
        {
            "document_id": "doc-1",
            "company_number": "12345678",
            "transaction_id": "MzQwMDAwMDAwMDAwMDAwMDAx",
        },
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert response["already_existed"] is True
    assert Path(response["path"]) == path

    # Different content with the same name gets a (2) suffix.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-1", json=metadata)
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-1/content", content=b"%PDF-1.4 two"
    )
    response = await hass.services.async_call(
        DOMAIN,
        "download_document",
        {
            "document_id": "doc-1",
            "company_number": "12345678",
            "description": "Confirmation statement made on 11 March 2026 with no updates",
            "filing_date": "2026-03-14",
        },
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert Path(response["path"]).name.endswith("(EXAMPLE TRADING LIMITED) (2).pdf")

    # Without company context the metadata names the file and an unmonitored company is looked up.
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-2",
        json={**metadata, "company_number": "23456789"},
    )
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-2/content", content=b"three")
    aioclient_mock.get(
        f"{API_BASE}/company/23456789", json=load_fixture("company_dissolved/profile")
    )
    response = await hass.services.async_call(
        DOMAIN,
        "download_document",
        {"document_id": "doc-2", "filename": "custom name"},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert (
        Path(response["path"])
        == tmp_path / "23456789 OLD VENTURES LIMITED" / "custom name.pdf"
    )
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-3",
        json={"category": "accounts", "created_at": "2025-01-02T10:00:00Z"},
    )
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-3/content", content=b"four")
    response = await hass.services.async_call(
        DOMAIN,
        "download_document",
        {"document_id": "doc-3"},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert (
        Path(response["path"])
        == tmp_path
        / "documents"
        / "2025-01-02 - Companies House - Accounts (doc-3).pdf"
    )

    # A directory outside the allowlist is refused.
    hass.config.allowlist_external_dirs.discard(str(tmp_path))
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-3", json={"category": "accounts"}
    )
    with pytest.raises(ServiceValidationError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            "download_document",
            {"document_id": "doc-3"},
            blocking=True,
            return_response=True,
        )
    assert excinfo.value.translation_key == "path_not_allowed"
    assert entry.state.recoverable


async def test_digest_action_reports_the_week(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The report ranks changes, splits new problems from old, and can attach PDFs."""
    from custom_components.companies_house.const import Dataset

    freezer.move_to("2026-09-14 12:00:00+00:00")
    # A dissolved company rides along: rated red, counted, but never listed.
    entry = await setup_entry(
        ["12345678", "34567890", "23456789"],
        officers=True,
        close_watch={"12345678"},
        options={"document_directory": str(tmp_path)},
    )
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    hass.config.media_dirs["local"] = str(tmp_path)
    company = entry.runtime_data.companies["sub_12345678"]
    officers = load_fixture("company_active/officers")
    officers["items"][1]["resigned_on"] = "2026-09-12"
    filings = load_fixture("company_active/filing_history")
    filings["items"].insert(
        0,
        {
            **filings["items"][0],
            "transaction_id": "tx-accounts",
            "date": "2026-09-13",
            "category": "accounts",
            "type": "AA",
            "description": "accounts-with-accounts-type-micro-entity",
            "description_values": {"made_up_date": "2026-03-31"},
            "links": {
                "self": "/company/12345678/filing-history/tx-accounts",
                "document_metadata": f"{DOCUMENT_API_BASE}/document/doc-accounts",
            },
        },
    )
    filings["total_count"] += 1
    aioclient_mock.clear_requests()
    mock_company(
        aioclient_mock,
        "12345678",
        overrides={"officers": officers, "filing_history": filings},
    )
    await company.async_refresh_datasets(
        [Dataset.OFFICERS, Dataset.FILINGS], reason="test"
    )
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        "digest",
        {"days": 7, "summary": "A quiet week, one resignation."},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert response["days"] == 7
    assert response["summary"]["companies"] == 3
    assert response["summary"]["people"] == 1
    # The resignation, the accounts filing, and the rating the resignation
    # pushed from green to amber.
    assert response["summary"]["changes"] == 3
    assert response["summary"]["by_kind"] == {"filing": 1, "officer": 1, "status": 1}
    assert response["summary"]["new_issues"] == 1
    assert response["summary"]["still_open"] == 1
    assert response["summary"]["risk"] == {
        "red": 2,
        "amber": 1,
        "green": 0,
        "unknown": 0,
    }
    (changed,) = response["companies"]
    assert changed["name"] == "EXAMPLE TRADING LIMITED"
    assert changed["initials"] == "ET"
    assert changed["weight"] == 3  # close watch: your own company
    assert changed["risk"]["band"] == "amber"
    assert changed["risk"]["reason"].startswith("Amber: ")
    # The rating change and the resignation outrank the routine filing; all
    # are scored by weight.
    assert [c["event_type"] for c in changed["changes"]] == [
        "risk-changed",
        "resigned",
        "accounts",
    ]
    assert [c["score"] for c in changed["changes"]] == [27, 18, 15]
    assert changed["changes"][0]["title"] == "EXAMPLE TRADING LIMITED: risk now amber"
    # Every company offers the register's pages; every change offers more links.
    assert [p["text"] for p in changed["pages"]] == [
        "Filing history",
        "Officers",
        "People with control",
        "Charges",
    ]
    assert changed["pages"][0]["href"] == changed["link"] + "/filing-history"
    assert changed["changes"][1]["links"] == [
        {"text": "Officers", "href": changed["link"] + "/officers"}
    ]
    assert changed["changes"][2]["links"][0]["text"] == "Filing history"
    assert response["top"][1]["title"] == "EXAMPLE TRADING LIMITED: director resigned"
    assert response["top"][1]["subject"] == "EXAMPLE TRADING LIMITED"
    # The rating got worse this week: that needs attention. The liquidation
    # started before this week: still open, and rated red all along.
    assert response["needs_attention"] == [
        {
            "company": "EXAMPLE TRADING LIMITED",
            "number": "12345678",
            "link": changed["link"],
            "issues": ["Risk rating amber"],
        }
    ]
    assert response["still_open"] == [
        {
            "company": "SUNSET RETAIL LIMITED",
            "number": "34567890",
            "link": changed["link"].replace("12345678", "34567890"),
            "issues": [
                "In liquidation",
                "Confirmation statement overdue",
                "Risk rating red",
            ],
        }
    ]
    # The risk list: red first, then amber, each with the one-line reason.
    # The dissolved company is red for being dissolved, which is not news,
    # so it is counted above but listed nowhere.
    assert [(r["band"], r["company"]) for r in response["risk"]] == [
        ("red", "SUNSET RETAIL LIMITED"),
        ("amber", "EXAMPLE TRADING LIMITED"),
    ]
    assert response["risk"][0]["reason"].startswith(
        "Red: being wound up (solvent liquidation) since 1 Jul 2026"
    )
    assert entry.runtime_data.companies["sub_23456789"].risk.band == "red"
    assert response["quiet_companies"] == [
        "OLD VENTURES LIMITED",
        "SUNSET RETAIL LIMITED",
    ]
    assert response["new_companies"] == []
    assert response["attachments"] == []
    html = response["html"]
    assert "A quiet week, one resignation." in html
    assert "Worth a look" in html
    assert "Still open" in html
    assert "director resigned" in html
    assert "risk: 2 red, 1 amber" in html
    assert ">amber</span>" in html  # the pill in the company card heading
    assert "<script" not in html
    text = response["text"]
    assert "Still open (known before this week):" in text
    assert "director resigned" in text
    assert "  - RED SUNSET RETAIL LIMITED — being wound up" in text
    assert "url" not in response

    # With attach on, the accounts PDF is fetched and offered as an attachment.
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-accounts",
        json=load_fixture("document/metadata"),
    )
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-accounts/content",
        status=302,
        headers={"Location": "https://s3.example.invalid/accounts"},
    )
    aioclient_mock.get("https://s3.example.invalid/accounts", content=b"%PDF-1.4 acc")
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/filing-history/tx-accounts",
        json=filings["items"][0],
    )
    response = await hass.services.async_call(
        DOMAIN,
        "digest",
        {"days": 7, "save": True, "attach": True},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    (attachment,) = response["attachments"]
    assert response["email_attachments"] == [
        {
            "media_source": {
                "media_content_id": attachment["media_content_id"],
                "media_content_type": "application/pdf",
            },
            "filename": attachment["filename"],
        }
    ]
    assert attachment["media_content_type"] == "application/pdf"
    assert attachment["filename"].startswith("2026-09-13 - Companies House - ")
    assert attachment["media_content_id"].startswith(
        "media-source://media_source/local/"
    )
    saved = Path(response["path"])
    assert saved.parent == Path(hass.config.path("www", DOMAIN))
    assert saved.name.startswith("report-")
    assert saved.read_text(encoding="utf-8") == response["html"]  # noqa: ASYNC240
    assert response["url"].endswith(f"/local/{DOMAIN}/{saved.name}")
    latest = saved.parent / "report-latest.html"
    assert latest.read_text(encoding="utf-8") == response["html"]
    assert response["latest_url"].endswith(f"/local/{DOMAIN}/report-latest.html")

    # The register's own dates fill in what the change log never saw: with the
    # log wiped, this week's filing and a new role still make the report, while
    # the company-officer resignation (only ever a log entry) does not.
    officer = next(iter(entry.runtime_data.officers.values()))
    appointments = load_fixture("officer_many/appointments")
    appointments["items"][0]["appointed_on"] = "2026-09-10"
    aioclient_mock.get(
        f"{API_BASE}/officers/{officer.officer_id}/appointments",
        json=appointments,
    )
    await officer.appointments.async_refresh()
    await hass.async_block_till_done()
    company.state.changes = []
    officer.state.changes = []
    response = await hass.services.async_call(
        DOMAIN, "digest", {"days": 7}, blocking=True, return_response=True
    )
    assert response is not None
    assert response["summary"]["by_kind"] == {"filing": 1, "appointment": 1}
    (changed,) = response["companies"]
    (filed,) = changed["changes"]
    assert filed["event_type"] == "accounts"
    assert filed["at"].startswith("2026-09-13T00:00:00")
    assert filed["transaction_id"] == "tx-accounts"
    (person,) = response["people"]
    (role,) = person["changes"]
    assert role["event_type"] == "appointed"
    assert [x["text"] for x in role["links"]] == ["Company", "Officers"]
    assert person["companies"][0]["link"].endswith("/company/10000001")
    assert role["at"].startswith("2026-09-10T00:00:00")
    assert "new role" in role["title"]

    # A company opted out of the report is left out entirely.
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.example_trading_limited_in_weekly_report"},
        blocking=True,
    )
    await hass.async_block_till_done()
    response = await hass.services.async_call(
        DOMAIN, "digest", {"days": 7}, blocking=True, return_response=True
    )
    assert response is not None
    assert response["summary"]["companies"] == 2
    assert response["companies"] == []
    assert "EXAMPLE TRADING LIMITED" not in response["html"]
    assert "new role" in response["html"]


def test_new_companies_and_ownership_are_spotted() -> None:
    """A freshly incorporated watched company with a followed founder is highlighted."""
    from custom_components.companies_house.digest import _control_words, _new_companies

    companies = [
        {
            "name": "BRAND NEW LTD",
            "number": "17000001",
            "link": "x",
            "incorporated": "2026-09-01",
            "changes": [],
        },
        {
            "name": "OLD HAT LTD",
            "number": "10000001",
            "link": "y",
            "incorporated": "2015-01-01",
            "changes": [],
        },
        {
            "name": "ALSO NEW LTD",
            "number": "17000002",
            "link": "z",
            "incorporated": "2026-08-01",
            "changes": [],
        },
    ]
    people = [
        {
            "name": "Jane Smith",
            "companies": [
                {
                    "number": "17000001",
                    "role": "director",
                    "name": "BRAND NEW LTD",
                    "appointed_on": "2026-09-01",
                },
                {
                    "number": "17000002",
                    "role": "director",
                    "name": "ALSO NEW LTD",
                    "appointed_on": "2026-08-01",
                },
            ],
        }
    ]
    from datetime import UTC, date, datetime

    # ALSO NEW LTD is recent but nothing about it happened this week, so it is
    # left out: a new company is highlighted once, not for three months.
    found = _new_companies(
        companies, people, datetime(2026, 8, 30, tzinfo=UTC), date(2026, 9, 14)
    )
    assert [n["company"] for n in found] == ["BRAND NEW LTD"]
    assert found[0]["people"] == [
        {"name": "Jane Smith", "role": "director", "appointed_on": "2026-09-01"}
    ]
    assert (
        _control_words(
            ["ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent"]
        )
        == "75 to 100% of the shares, 75 to 100% of the votes"
    )
    assert _control_words(["right-to-appoint-and-remove-directors"]) == (
        "the right to appoint and remove directors"
    )


async def test_download_document_write_failure(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    tmp_path: Path,
) -> None:
    """A filesystem error is reported as a translated error."""
    await setup_entry(options={"document_directory": str(tmp_path / "file")})
    (tmp_path / "file").write_text("not a directory")
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-9", json={"category": "accounts"}
    )
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-9/content", content=b"x")
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            "download_document",
            {"document_id": "doc-9"},
            blocking=True,
            return_response=True,
        )
    assert excinfo.value.translation_key == "document_write_failed"


async def test_llm_api_exposes_read_actions(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The read actions are available as LLM tools and call through."""
    await setup_entry(["12345678"])
    context = llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="conversation",
        device_id=None,
    )
    api = next(a for a in llm.async_get_apis(hass) if a.id == DOMAIN)
    instance = await api.async_get_api_instance(context)
    names = {tool.name for tool in instance.tools}
    assert "companies_house_get_company" in names
    assert "companies_house_search_companies" in names
    assert "companies_house_download_document" not in names
    assert "companies_house_refresh" not in names
    assert "EXAMPLE TRADING LIMITED (12345678)" in instance.api_prompt
    tool = next(t for t in instance.tools if t.name == "companies_house_get_company")
    assert "config_entry_id" not in {
        getattr(k, "schema", k) for k in tool.parameters.schema
    }
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/12345678", json={"company_name": "X"})
    result = await tool.async_call(
        hass,
        llm.ToolInput(
            tool_name="companies_house_get_company",
            tool_args={"company_number": "12345678"},
        ),
        context,
    )
    assert result == {"result": {"company_name": "X"}}


async def test_search_404_returns_empty_and_get_404_is_clear(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A search with no hits (a 404 from the API) is empty; a missing resource says so."""
    await setup_entry()
    aioclient_mock.get(f"{API_BASE}/advanced-search/companies", status=404)
    response = await hass.services.async_call(
        DOMAIN,
        "advanced_search",
        {"location": "ZZ99 9ZZ"},
        blocking=True,
        return_response=True,
    )
    assert response == {"items": [], "total_results": 0, "hits": 0}
    aioclient_mock.get(f"{API_BASE}/company/00000000", status=404)
    with pytest.raises(HomeAssistantError) as excinfo:
        await hass.services.async_call(
            DOMAIN,
            "get_company",
            {"company_number": "00000000"},
            blocking=True,
            return_response=True,
        )
    assert excinfo.value.translation_key == "resource_not_found"
    assert excinfo.value.translation_placeholders == {"resource": "/company/00000000"}
