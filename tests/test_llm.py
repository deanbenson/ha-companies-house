"""Tests for the Assist tools: answered from memory, never from the API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.accounts import AccountsYear
from custom_components.companies_house.coordinator import (
    ChangeEvent,
    CompanyRuntime,
    OfficerRuntime,
)
from custom_components.companies_house.ixbrl import Figure
from custom_components.companies_house.llm import (
    PROMPT,
    TOOLS,
    NoAnswerError,
    _due_words,
    _match_score,
    _normalise,
    async_get_tools,
    find_company,
)
from custom_components.companies_house.models import AccountsInfo

from .conftest import COMPANY_FIXTURES
from .test_accounts import mock_accounts, run_backfill

NOW = "2026-09-15T09:00:00+00:00"
ALL_COMPANIES = list(COMPANY_FIXTURES)
EXPOSED = "sensor.example_trading_limited_risk_rating"
CONTEXT = llm.LLMContext(
    platform="test",
    context=None,
    language="en",
    assistant="conversation",
    device_id=None,
)


async def ask(hass: HomeAssistant, tool_name: str, **args: Any) -> dict[str, Any]:
    """Call one tool the way an assistant does and return its answer."""
    tools = async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST)
    assert tools is not None
    tool = next(t for t in tools.tools if t.name == f"companies_house__{tool_name}")
    result = await tool.async_call(
        hass, llm.ToolInput(tool_name=tool.name, tool_args=args), CONTEXT
    )
    return dict(result)


def _company(entry: MockConfigEntry, number: str) -> CompanyRuntime:
    company: CompanyRuntime = entry.runtime_data.companies[f"sub_{number}"]
    return company


def _officer(entry: MockConfigEntry) -> OfficerRuntime:
    officer: OfficerRuntime = entry.runtime_data.officers["sub_officer"]
    return officer


@pytest.fixture
async def entry(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """Set up every fixture company and the officer, and expose one entity."""
    freezer.move_to(NOW)
    assert await async_setup_component(hass, "homeassistant", {})
    entry: MockConfigEntry = await setup_entry(
        ALL_COMPANIES, officers=True, close_watch={"12345678"}
    )
    async_expose_entity(hass, "conversation", EXPOSED, True)
    return entry


# ---------------------------------------------------------------- gate


async def test_tools_only_for_assist_with_something_exposed(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """No entry, nothing exposed or another API: no tools. Otherwise all seven."""
    freezer.move_to(NOW)
    assert await async_setup_component(hass, "homeassistant", {})
    assert async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST) is None
    entry = await setup_entry(["12345678"])
    assert async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST) is None
    async_expose_entity(hass, "conversation", EXPOSED, True)
    assert async_get_tools(hass, CONTEXT, "companies_house") is None
    tools = async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST)
    assert tools is not None
    assert tools.prompt == PROMPT
    assert [t.name for t in tools.tools] == [
        f"companies_house__{n}"
        for n in (
            "what_changed",
            "company",
            "deadlines",
            "filings",
            "person",
            "connections",
            "risk",
        )
    ]
    # Every parameter carries a description for the model.
    for tool in tools.tools:
        for marker in tool.parameters.schema:
            assert marker.description, f"{tool.name} {marker}"
    # Exposed to another assistant only: nothing for this one.
    async_expose_entity(hass, "conversation", EXPOSED, False)
    async_expose_entity(hass, "cloud.alexa", EXPOSED, True)
    assert async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST) is None
    async_expose_entity(hass, "conversation", EXPOSED, True)
    assert async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST) is not None
    # Unloading the entry takes the tools away again.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST) is None


async def test_assist_api_picks_up_the_platform(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Home Assistant's own Assist API finds llm.py and merges the prompt."""
    assert await async_setup_component(hass, "llm", {})
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, CONTEXT)
    names = {t.name for t in api.tools}
    assert {
        f"companies_house__{t.name.partition('__')[2]}"
        for t in api.tools
        if t.name.startswith("companies_house__")
    } == {tool.name for tool in TOOLS}
    assert "companies_house__company" in names
    assert PROMPT in api.api_prompt
    # (APIInstance.async_call_tool imports the conversation component, whose
    # requirements the test venv does not carry; the tool itself is the same.)
    tool = next(t for t in api.tools if t.name == "companies_house__company")
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name=tool.name, tool_args={"name": "Example Trading Ltd"}),
        api.llm_context,
    )
    assert result["success"] is True
    assert result["result"]["number"] == "12345678"


async def test_tools_answer_nothing_once_the_entry_is_gone(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A tool handed out before an unload says there is nothing to answer from."""
    tools = async_get_tools(hass, CONTEXT, llm.LLM_API_ASSIST)
    assert tools is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    for name, args in (
        ("company", {"name": "example"}),
        ("connections", {}),
        ("filings", {"company": "example"}),
    ):
        tool = next(t for t in tools.tools if t.name == f"companies_house__{name}")
        result = await tool.async_call(
            hass, llm.ToolInput(tool_name=tool.name, tool_args=args), CONTEXT
        )
        assert result == {
            "success": False,
            "error": "No companies are being watched yet.",
        }


# ---------------------------------------------------------------- matching


@pytest.mark.parametrize(
    ("query", "names", "score"),
    [
        ("Example Trading Ltd", ["EXAMPLE TRADING LIMITED"], 3),
        ("example trading", ["EXAMPLE TRADING LIMITED"], 2),
        ("trading example", ["EXAMPLE TRADING LIMITED"], 1),
        ("12345678", ["EXAMPLE TRADING LIMITED", "", "12345678"], 3),
        ("Smith & Sons", ["SMITH AND SONS LIMITED"], 2),
        ("nothing", ["EXAMPLE TRADING LIMITED"], 0),
        ("", ["EXAMPLE TRADING LIMITED"], 0),
        ("example", [""], 0),
    ],
)
def test_match_score(query: str, names: list[str], score: int) -> None:
    """Names match exactly, as a part, or by their words, whatever the spelling."""
    assert _match_score(query, names) == score


def test_normalise() -> None:
    """Punctuation goes, case goes, "Limited" becomes "ltd"."""
    assert _normalise("J.P. Morgan (U.K.) Limited") == "j p morgan u k ltd"
    assert _normalise("Marks & Spencer") == "marks & spencer"


async def test_find_company(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A name, a part of it or the number finds the company; several ask back."""
    assert find_company(hass, "12345678").company_number == "12345678"
    assert find_company(hass, "dormant holdings").company_number == "45678901"
    assert find_company(hass, "HISTORIC PARTNERS LLP").company_number == "OC123456"
    with pytest.raises(NoAnswerError) as excinfo:
        find_company(hass, "limited")
    assert excinfo.value.extra == {
        "candidates": [
            "DORMANT HOLDINGS LIMITED",
            "EXAMPLE TRADING LIMITED",
            "OLD VENTURES LIMITED",
            "SUBSIDIARY SERVICES LIMITED",
            "SUNSET RETAIL LIMITED",
        ]
    }
    assert str(excinfo.value).startswith("Several watched companies match 'limited'")
    with pytest.raises(NoAnswerError, match="No watched company matches 'acme'"):
        find_company(hass, "acme")


# ---------------------------------------------------------------- company


async def test_company_answer(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The company answer has status, deadlines, rating, people, charges and links."""
    result = await ask(hass, "company", name="example trading")
    assert result["success"] is True
    answer = result["result"]
    assert answer["name"] == "EXAMPLE TRADING LIMITED"
    assert answer["number"] == "12345678"
    assert answer["status"] == "active"
    assert answer["type"] == "Private limited company"
    assert answer["incorporated"] == "12 Mar 2015"
    assert answer["registered_office"] == "47 High Street, Reading, RG1 2AB, England"
    assert answer["close_watch"] is True
    assert answer["summary"].startswith(
        "EXAMPLE TRADING LIMITED (12345678) is active, incorporated 12 Mar 2015. "
        "Risk rating green: "
    )
    assert (
        "Next deadline: annual accounts due in 107 days (31 Dec 2026)."
        in answer["summary"]
    )
    assert answer["attention"] == []
    assert [d["what"] for d in answer["deadlines"]] == [
        "annual accounts",
        "confirmation statement",
    ]
    assert answer["deadlines"][0]["date"] == "31 Dec 2026"
    assert answer["deadlines"][0]["status"] == "due in 107 days"
    assert answer["strike_off"] is None
    assert answer["risk"]["band"] == "green"
    assert answer["risk"]["reasons"]
    assert answer["accounts"] is None
    assert [o["name"] for o in answer["officers"]] == [
        "Priya Patel",
        "Jane Elizabeth Smith",
        "ACME SECRETARIES LIMITED",
    ]
    assert answer["officers"][2]["role"] == "corporate secretary"
    assert answer["officers"][0]["since"] == "1 Sep 2025"
    assert answer["people_with_control"] == [
        {
            "name": "Ms Jane Elizabeth Smith",
            "controls": "75 to 100% of the shares, 75 to 100% of the votes",
            "since": "6 Apr 2016",
            "corporate": False,
            "sanctioned": False,
        }
    ]
    charges = answer["charges"]
    assert charges["outstanding"] == 1
    assert charges["total"] == 2
    assert charges["text"] == "1 outstanding charge in favour of HSBC UK Bank Plc"
    assert charges["charges"][0]["created_on"] == "6 May 2022"
    assert charges["charges"][0]["kind"] == (
        "fixed and floating charge over all the company's assets"
    )
    assert charges["charges"][0]["link"].endswith("document?format=pdf&download=0")
    assert answer["recent_changes"] == []
    assert answer["recent_days"] == 30
    assert answer["links"][0] == {
        "text": "Company",
        "href": "https://find-and-update.company-information.service.gov.uk/company/12345678",
    }
    assert [x["text"] for x in answer["links"][1:]] == [
        "Filing history",
        "Officers",
        "People with control",
        "Charges",
    ]


async def test_company_answer_in_trouble(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A strike-off, overdue filings and a red rating all reach the summary."""
    result = await ask(hass, "company", name="Dormant Holdings")
    answer = result["result"]
    assert answer["status_detail"] == "Active proposal to strike off"
    assert answer["summary"] == (
        "DORMANT HOLDINGS LIMITED (45678901) is active, incorporated 12 Mar 2015. "
        "Strike-off proposed (compulsory) — 26 days to object. "
        "Accounts overdue and confirmation statement overdue. "
        "Risk rating red: strike-off proposed (compulsory) on 25 Aug 2026; "
        "accounts 7 months overdue; confirmation statement 7 months overdue; "
        "files dormant accounts (declares it is not trading) and 2 more. "
        "Next deadline: object to strike-off 26 days to object (11 Oct 2026)."
    )
    assert answer["attention"] == [
        "Strike-off proposed (compulsory) — 26 days to object",
        "Accounts overdue",
        "Confirmation statement overdue",
        "Risk rating red",
    ]
    assert answer["strike_off"]["kind"] == "compulsory"
    assert answer["strike_off"]["notice_on"] == "25 Aug 2026"
    assert answer["strike_off"]["earliest_on"] == "25 Oct 2026"
    assert answer["strike_off"]["objection_deadline"] == "11 Oct 2026"
    assert answer["strike_off"]["days_to_object"] == 26
    assert answer["strike_off"]["suspended_on"] == ""
    assert answer["strike_off"]["link"].endswith(
        "/filing-history/MzUwMDAwMDAwMDAwMDAwMDAx/document?format=pdf&download=0"
    )
    assert [(d["what"], d["status"]) for d in answer["deadlines"]] == [
        ("annual accounts", "overdue by 199 days"),
        ("confirmation statement", "overdue by 212 days"),
        ("object to strike-off", "26 days to object"),
    ]
    assert answer["deadlines"][0]["overdue"] is True
    assert answer["charges"]["text"] == "No outstanding charges"
    # A dissolved company: the date it went, no deadlines, no rating chatter.
    result = await ask(hass, "company", name="old ventures")
    answer = result["result"]
    assert answer["summary"].startswith(
        "OLD VENTURES LIMITED (23456789) is dissolved since 13 Aug 2024."
    )
    assert answer["deadlines"] == []


async def test_company_answer_with_accounts(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Once the accounts are read, the figures, changes and flags come back."""
    freezer.move_to(NOW)
    assert await async_setup_component(hass, "homeassistant", {})
    mock_accounts(aioclient_mock)
    await setup_entry(["12345678"])
    async_expose_entity(hass, "conversation", EXPOSED, True)
    await run_backfill(hass, freezer)
    result = await ask(hass, "company", name="example")
    accounts = result["result"]["accounts"]
    assert accounts["made_up_to"] == "31 Dec 2025"
    assert accounts["type"] == "full accounts"
    assert accounts["status"] == "read"
    assert accounts["years_read"] == 3
    by_name = {f["name"]: f for f in accounts["figures"]}
    assert by_name["Turnover"] == {
        "name": "Turnover",
        "value": "£13.8k",
        "prior": "£16.6k",
        "change": "down 17 %",
        "change_percent": -17.0,
    }
    assert accounts["link"].endswith(
        "/filing-history/tx-2025/document?format=pdf&download=0"
    )
    assert accounts["text"].startswith("Turnover £13.8k (down 17 %)")
    assert (
        "Latest accounts to 31 Dec 2025: Turnover £13.8k (down 17 %)"
        in (result["result"]["summary"])
    )
    # The accounts/read change is in the recent changes too.
    assert any("accounts" in c["what"] for c in result["result"]["recent_changes"])


async def test_company_answer_names(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Ambiguous and unknown names come back as errors, with the candidates."""
    result = await ask(hass, "company", name="limited")
    assert result["success"] is False
    assert result["error"].startswith("Several watched companies match 'limited': ")
    assert len(result["candidates"]) == 5
    result = await ask(hass, "company", name="acme")
    assert result == {"success": False, "error": "No watched company matches 'acme'."}
    result = await ask(hass, "company")
    assert result["success"] is False
    assert result["error"].startswith("Bad request: required key not provided")
    result = await ask(hass, "company", name="example", extra=1)
    assert result["success"] is False
    assert result["error"].startswith("Bad request: ")
    assert "'extra'" in result["error"]


# ---------------------------------------------------------------- changes


async def test_what_changed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Changes from the log and from the register's own dates, newest first."""
    result = await ask(hass, "what_changed")
    assert result == {
        "success": True,
        "result": {
            "days": 7,
            "since": "8 Sep 2026",
            "count": 0,
            "summary": (
                "Nothing changed at the watched companies and followed people "
                "in the last 7 days."
            ),
            "changes": [],
        },
    }
    result = await ask(hass, "what_changed", days=365, company="example trading")
    answer = result["result"]
    assert answer["count"] == 2
    assert (
        answer["summary"]
        == "2 changes at EXAMPLE TRADING LIMITED in the last 365 days."
    )
    assert answer["changes"][0] == {
        "when": "14 Mar 2026",
        "who": "EXAMPLE TRADING LIMITED",
        "what": "EXAMPLE TRADING LIMITED: Confirmation statement filed",
        "detail": "Confirmation statement made on 11 March 2026 with no updates",
        "link": "https://find-and-update.company-information.service.gov.uk/company/12345678/filing-history/MzQwMDAwMDAwMDAwMDAwMDAx/document?format=pdf&download=0",
    }
    assert answer["changes"][1]["when"] == "18 Dec 2025"
    # A logged change at a person and at a company are both in, newest first.
    officer = _officer(entry)
    appointment = officer.appointments.data.items[0]
    officer.dispatch(
        ChangeEvent("appointment", "resigned", appointment.event_payload())
    )
    company = _company(entry, "34567890")
    company.dispatch(
        ChangeEvent(
            "status",
            "status-changed",
            {"old_status": "active", "new_status": "liquidation"},
        )
    )
    result = await ask(hass, "what_changed", days=30)
    answer = result["result"]
    assert answer["count"] == 3
    assert (
        answer["summary"]
        == "3 changes at 2 companies and 1 person in the last 30 days."
    )
    assert [c["who"] for c in answer["changes"]] == [
        "SUNSET RETAIL LIMITED",
        "Jane Elizabeth SMITH",
        "DORMANT HOLDINGS LIMITED",
    ]
    assert answer["changes"][0]["when"] == "15 Sep 2026"
    assert answer["changes"][2]["what"] == "DORMANT HOLDINGS LIMITED: Gazette notice"
    assert answer["changes"][2]["detail"] == (
        "First Gazette notice for compulsory strike-off"
    )
    result = await ask(hass, "what_changed", days=0)
    assert result["success"] is False
    assert result["error"].startswith("Bad request: ")
    result = await ask(hass, "what_changed", company="nobody")
    assert result == {"success": False, "error": "No watched company matches 'nobody'."}


async def test_deadlines(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Overdue first, then what is due, then the strike-off objection date."""
    result = await ask(hass, "deadlines")
    answer = result["result"]
    assert answer["days"] == 30
    assert answer["count"] == 5
    assert answer["overdue"] == 3
    assert [(d["company"], d["what"], d["status"]) for d in answer["deadlines"]] == [
        ("DORMANT HOLDINGS LIMITED", "confirmation statement", "overdue by 212 days"),
        ("DORMANT HOLDINGS LIMITED", "annual accounts", "overdue by 199 days"),
        ("SUNSET RETAIL LIMITED", "confirmation statement", "overdue by 92 days"),
        ("SUNSET RETAIL LIMITED", "annual accounts", "due in 15 days"),
        ("DORMANT HOLDINGS LIMITED", "object to strike-off", "26 days to object"),
    ]
    assert answer["summary"].startswith(
        "5 deadlines in the next 30 days, 3 already overdue: "
        "DORMANT HOLDINGS LIMITED confirmation statement overdue by 212 days; "
    )
    assert answer["deadlines"][-1]["link"].endswith(
        "/filing-history/MzUwMDAwMDAwMDAwMDAwMDAx/document?format=pdf&download=0"
    )
    result = await ask(hass, "deadlines", days=365)
    assert result["result"]["count"] == 11
    assert [d["days"] for d in result["result"]["deadlines"]] == sorted(
        d["days"] for d in result["result"]["deadlines"]
    )
    # With only a dissolved company on hand there is nothing due.
    for number in ALL_COMPANIES:
        if number != "23456789":
            _company(entry, number).profile.data = None
    result = await ask(hass, "deadlines", days=1)
    assert result["result"] == {
        "days": 1,
        "count": 0,
        "overdue": 0,
        "summary": "Nothing is due in the next 1 day and nothing is overdue.",
        "deadlines": [],
    }


async def test_filings(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Recent filings with links to the documents, newest first."""
    result = await ask(hass, "filings", company="dormant")
    answer = result["result"]
    assert answer["count"] == 1
    assert answer["summary"] == (
        "DORMANT HOLDINGS LIMITED filed 1 document in the last 90 days: "
        "First Gazette notice for compulsory strike-off on 25 Aug 2026."
    )
    assert answer["filings"] == [
        {
            "date": "25 Aug 2026",
            "description": "First Gazette notice for compulsory strike-off",
            "category": "gazette",
            "type": "GAZ1",
            "pages": 1,
            "link": "https://find-and-update.company-information.service.gov.uk/company/45678901/filing-history",
        }
    ]
    result = await ask(hass, "filings", company="example", days=365)
    answer = result["result"]
    assert answer["count"] == 2
    assert [f["date"] for f in answer["filings"]] == ["14 Mar 2026", "18 Dec 2025"]
    assert answer["filings"][1]["category"] == "accounts"
    assert answer["filings"][1]["link"].endswith("document?format=pdf&download=0")
    result = await ask(hass, "filings", company="example", days=30)
    assert result["result"]["summary"] == (
        "EXAMPLE TRADING LIMITED filed nothing in the last 30 days."
    )
    assert result["result"]["filings"] == []
    _company(entry, "12345678").probe.data = None
    result = await ask(hass, "filings", company="example")
    assert result["result"]["count"] == 0


# ---------------------------------------------------------------- people


async def test_person_followed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A followed person: roles, records, disqualification and changes."""
    result = await ask(hass, "person", name="jane")
    answer = result["result"]
    assert answer["name"] == "Jane Elizabeth SMITH"
    assert answer["followed"] is True
    assert answer["records"] == ["officer-jane"]
    assert answer["date_of_birth"] == "06/1978"
    assert len(answer["roles"]) == 18
    assert answer["roles"][0] == {
        "company": "PORTFOLIO COMPANY 11 LIMITED",
        "number": "10000011",
        "role": "director",
        "since": "15 Mar 2021",
        "link": "https://find-and-update.company-information.service.gov.uk/company/10000011",
    }
    assert answer["former_roles"] == 3
    assert answer["disqualification"] == {
        "checked": True,
        "disqualified": False,
        "possible_matches": 1,
        "text": "Not on the disqualified directors register (1 namesake on it)",
    }
    assert answer["summary"].startswith(
        "Jane Elizabeth SMITH holds 18 current roles: director at"
    )
    assert answer["summary"].endswith(
        "Not on the disqualified directors register (1 namesake on it)."
    )
    assert answer["recent_changes"] == []
    assert answer["link"].endswith("/officers/officer-jane/appointments")
    # "Jane Smith" fits the register's spelling too.
    assert (await ask(hass, "person", name="Jane Smith"))["result"]["followed"] is True

    officer = _officer(entry)
    officer.disqualification.data = replace(
        officer.disqualification.data,
        disqualified=True,
        disqualified_from=date(2025, 1, 1),
        disqualified_until=date(2030, 1, 1),
        reason="Unfit conduct",
    )
    appointment = officer.appointments.data.items[0]
    officer.dispatch(
        ChangeEvent("appointment", "resigned", appointment.event_payload())
    )
    result = await ask(hass, "person", name="jane")
    answer = result["result"]
    assert answer["disqualification"] == {
        "checked": True,
        "disqualified": True,
        "from": "1 Jan 2025",
        "until": "1 Jan 2030",
        "reason": "Unfit conduct",
        "text": "Disqualified until 1 Jan 2030",
    }
    assert answer["summary"].endswith(
        "Disqualified until 1 Jan 2030. 1 change in the last 90 days."
    )
    assert answer["recent_changes"][0]["when"] == "15 Sep 2026"
    assert answer["recent_changes"][0]["who"] == "Jane Elizabeth SMITH"

    officer.disqualification.data = None
    result = await ask(hass, "person", name="jane")
    assert result["result"]["disqualification"] == {
        "checked": False,
        "text": "Not checked yet",
    }
    officer.appointments.data = None
    result = await ask(hass, "person", name="jane")
    assert result == {
        "success": False,
        "error": "Jane Elizabeth SMITH has not been read from the register yet.",
    }


async def test_person_from_the_watched_companies(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Anyone else is found among the officers and owners of the watched companies."""
    result = await ask(hass, "person", name="priya")
    assert result["result"] == {
        "name": "Priya Patel",
        "followed": False,
        "summary": (
            "Priya Patel is not followed. 1 current role at the watched companies: "
            "director at EXAMPLE TRADING LIMITED."
        ),
        "roles": [
            {
                "company": "EXAMPLE TRADING LIMITED",
                "number": "12345678",
                "role": "director",
                "since": "1 Sep 2025",
                "until": "",
                "current": True,
                "link": "https://find-and-update.company-information.service.gov.uk/company/12345678/officers",
            }
        ],
        "former_roles": 0,
    }
    # A former officer.
    result = await ask(hass, "person", name="thomas brown")
    assert result["result"]["summary"] == (
        "Thomas Brown is not followed. No current role at the watched companies, "
        "only former ones."
    )
    assert result["result"]["roles"][0]["until"] == "31 Jan 2020"
    assert result["result"]["former_roles"] == 1
    # An owner who is also a director has both rows.
    result = await ask(hass, "person", name="sarah white")
    roles = result["result"]["roles"]
    assert [r["role"] for r in roles] == [
        "director",
        "person with significant control",
    ]
    assert roles[1]["controls"]
    # A corporate owner only on the PSC register.
    result = await ask(hass, "person", name="parent holdings")
    assert result["result"]["roles"][0]["role"] == "person with significant control"
    # Two Taylors at the LLP.
    result = await ask(hass, "person", name="taylor")
    assert result == {
        "success": False,
        "error": "Several people match 'taylor': Emma Taylor, Robert Taylor. Which one?",
        "candidates": ["Emma Taylor", "Robert Taylor"],
    }
    result = await ask(hass, "person", name="nobody")
    assert result == {
        "success": False,
        "error": (
            "Nobody called 'nobody' is followed or holds a role at a watched company. "
            "The followed people are Jane Elizabeth SMITH."
        ),
    }
    # Without officer data there is nobody to search.
    for number in ALL_COMPANIES:
        company = _company(entry, number)
        if company.officers:
            company.officers.data = None
        if company.psc:
            company.psc.data = None
    for officer in list(entry.runtime_data.officers.values()):
        entry.runtime_data.officers.pop("sub_officer")
        assert officer
    result = await ask(hass, "person", name="priya")
    assert result == {
        "success": False,
        "error": "Nobody called 'priya' is followed or holds a role at a watched company.",
    }


# ---------------------------------------------------------------- connections


async def test_connections(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The whole map summarised, or one name's connections and what stands out."""
    result = await ask(hass, "connections")
    answer = result["result"]
    assert answer["summary"]["watched_companies"] == 6
    assert answer["summary"]["followed_people"] == 1
    assert answer["summary_text"].startswith(
        "2 live connections between 6 watched companies and 1 followed person; "
        "2 things worth knowing: Parent Holdings Limited (09876543) controls "
    )
    assert answer["summary_text"].endswith("and SUNSET RETAIL LIMITED.")
    assert answer["interesting"]
    assert set(answer) == {
        "generated_at",
        "period_days",
        "summary",
        "interesting",
        "summary_text",
    }

    result = await ask(hass, "connections", name="example trading")
    answer = result["result"]
    assert answer["name"] == "EXAMPLE TRADING LIMITED"
    assert answer["type"] == "company"
    assert answer["status"] == "own"
    assert [(c["with"], c["how"], c["current"]) for c in answer["connections"]] == [
        ("ACME SECRETARIES LIMITED", "Secretary", True),
        ("HSBC UK Bank Plc", "charge", True),
        ("Jane Elizabeth Smith", "Director", True),
        (
            "Jane Elizabeth Smith",
            "75 to 100% of the shares, 75 to 100% of the votes",
            True,
        ),
        ("Priya Patel", "Director", True),
        ("Lloyds Bank Plc", "charge", False),
        ("Thomas Brown", "Director", False),
    ]
    assert answer["connections"][5]["until"] == "30 Nov 2021"
    assert answer["summary"].startswith(
        "EXAMPLE TRADING LIMITED has 5 current connections: Secretary — ACME SECRETARIES LIMITED; "
    )
    assert answer["interesting"]
    assert answer["link"].endswith("/company/12345678")

    result = await ask(hass, "connections", name="priya")
    answer = result["result"]
    assert answer["type"] == "person"
    assert answer["status"] == "external"
    assert (
        answer["summary"]
        == "Priya Patel has 1 current connection: Director — EXAMPLE TRADING LIMITED."
    )
    assert answer["interesting"] == []

    result = await ask(hass, "connections", name="nobody")
    assert result == {
        "success": False,
        "error": "No company or person on the map matches 'nobody'.",
    }
    result = await ask(hass, "connections", name="portfolio company")
    assert result["success"] is False
    assert result["error"].startswith(
        "Several companies and people on the map match 'portfolio company': "
        "PORTFOLIO COMPANY 01 LIMITED, "
    )
    assert result["error"].endswith(
        ", PORTFOLIO COMPANY 08 LIMITED and 15 more. Which one?"
    )
    assert len(result["candidates"]) == 23

    # Without the map kept on the service device, it is built on the spot.
    entry.runtime_data.connections = None
    result = await ask(hass, "connections", name="priya")
    assert (
        result["result"]["summary"]
        == "Priya Patel has 1 current connection: Director — EXAMPLE TRADING LIMITED."
    )
    # A map with nothing on it reads as such.
    for number in ALL_COMPANIES:
        _company(entry, number).profile.data = None
    result = await ask(hass, "connections")
    assert result["result"]["summary_text"] == (
        "0 live connections between 0 watched companies and 1 followed person; "
        "nothing stands out."
    )


# ---------------------------------------------------------------- risk


async def test_risk(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Red then amber, worst first; a dissolved company is counted but not listed."""
    result = await ask(hass, "risk")
    answer = result["result"]
    assert answer["counts"] == {"red": 3, "amber": 0, "green": 3, "unknown": 0}
    assert [(r["company"], r["band"]) for r in answer["companies"]] == [
        ("DORMANT HOLDINGS LIMITED", "red"),
        ("SUNSET RETAIL LIMITED", "red"),
    ]
    assert answer["summary"].startswith(
        "3 red, 0 amber, 3 green. DORMANT HOLDINGS LIMITED is red: strike-off proposed"
    )
    assert answer["companies"][0]["reasons"][0] == (
        "strike-off proposed (compulsory) on 25 Aug 2026"
    )
    assert answer["companies"][0]["link"].endswith("/company/45678901")
    # A company not yet rated counts as unknown.
    _company(entry, "12345678").risk = None
    result = await ask(hass, "risk")
    assert result["result"]["counts"] == {
        "red": 3,
        "amber": 0,
        "green": 2,
        "unknown": 1,
    }
    assert result["result"]["summary"].startswith(
        "3 red, 0 amber, 2 green, 1 not rated."
    )
    # Nothing red or amber: just the counts.
    for number in ("45678901", "34567890", "23456789"):
        _company(entry, number).profile.data = None
    result = await ask(hass, "risk")
    assert result["result"]["summary"] == "0 red, 0 amber, 2 green, 1 not rated."
    assert result["result"]["companies"] == []


# ---------------------------------------------------------------- edges


def test_due_words() -> None:
    """Deadlines read as people say them."""
    assert _due_words(-1) == "overdue by 1 day"
    assert _due_words(-12) == "overdue by 12 days"
    assert _due_words(0) == "due today"
    assert _due_words(1) == "due in 1 day"
    assert _due_words(30) == "due in 30 days"


async def test_company_answer_with_less_on_hand(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Datasets not read yet, no rating and no dates leave gaps, not errors."""
    company = _company(entry, "12345678")
    assert company.officers is not None
    assert company.psc is not None
    assert company.charges is not None
    company.officers.data = None
    company.psc.data = None
    company.charges.data = None
    company.accounts.data = None
    company.risk = None
    company.profile.data = replace(
        company.profile.data,
        date_of_creation=None,
        accounts=AccountsInfo(),
    )
    result = await ask(hass, "company", name="example")
    answer = result["result"]
    assert answer["summary"] == (
        "EXAMPLE TRADING LIMITED (12345678) is active. "
        "Next deadline: confirmation statement due in 191 days (25 Mar 2027)."
    )
    assert answer["incorporated"] == ""
    assert answer["officers"] == []
    assert answer["people_with_control"] == []
    assert answer["charges"] is None
    assert answer["accounts"] is None
    assert answer["risk"] is None
    assert [d["what"] for d in answer["deadlines"]] == ["confirmation statement"]
    # Accounts read but with a figure left out: it is listed as not disclosed.
    history = _company(entry, "45678901").accounts.data
    assert history is not None
    year = AccountsYear(
        transaction_id="tx-1",
        made_up_to=date(2025, 5, 31),
        filed_on=date(2025, 11, 12),
        accounts_type="dormant",
        source="ixbrl",
        status="ok",
        dormant=True,
        figures={"net_assets": Figure(value=Decimal(100), prior=Decimal(100))},
    )
    _company(entry, "45678901").accounts.data = replace(history, years=[year])
    result = await ask(hass, "company", name="dormant holdings")
    accounts = result["result"]["accounts"]
    assert accounts["type"] == "dormant company accounts"
    assert accounts["figures"] == [
        {
            "name": "Net assets",
            "value": "£100",
            "prior": "£100",
            "change": "unchanged",
            "change_percent": 0.0,
        }
    ]
    assert accounts["not_disclosed"] == [
        "turnover",
        "profit before tax",
        "profit after tax",
        "cash",
        "creditors due within a year",
        "employees",
    ]
    assert (
        "Latest accounts to 31 May 2025: Net assets £100 (unchanged)."
        in (result["result"]["summary"])
    )


async def test_deadlines_with_nothing_overdue(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """The summary leaves out the overdue count when there is none."""
    for number in ("45678901", "34567890"):
        _company(entry, number).profile.data = None
    result = await ask(hass, "deadlines", days=365)
    answer = result["result"]
    assert answer["overdue"] == 0
    assert answer["count"] == 6
    assert answer["summary"].startswith("6 deadlines in the next 365 days: ")


async def test_person_with_no_roles(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A followed person who holds nothing now still gets an answer."""
    officer = _officer(entry)
    assert officer.appointments.data is not None
    officer.appointments.data = replace(officer.appointments.data, items=[])
    result = await ask(hass, "person", name="jane")
    assert result["result"]["roles"] == []
    assert result["result"]["summary"].startswith(
        "Jane Elizabeth SMITH holds 0 current roles. Not on the disqualified"
    )


async def test_connections_of_someone_gone(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A former director is on the map with only past connections."""
    result = await ask(hass, "connections", name="thomas brown")
    answer = result["result"]
    assert answer["summary"] == "Thomas Brown has 0 current connections."
    assert [(c["with"], c["current"]) for c in answer["connections"]] == [
        ("EXAMPLE TRADING LIMITED", False)
    ]
