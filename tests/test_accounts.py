"""Tests for reading filed accounts: the queue, the coordinator, sensors, actions, report."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.accounts import (
    AccountsHistory,
    AccountsYear,
    Figure,
    accounts_type_from_description,
    changes_for,
    describe_figures,
    figure_attributes,
    flags_for,
    format_change,
    format_count,
    format_money,
    history_summary,
    percent_change,
    plain_number,
)
from custom_components.companies_house.accounts_coordinator import (
    BACKFILL_GAP,
    BACKFILL_RETRY_GAP,
    BACKFILL_START_DELAY,
    fill_comparatives,
    merge_filings,
    statistic_id,
)
from custom_components.companies_house.const import (
    API_BASE,
    DOCUMENT_API_BASE,
    DOMAIN,
    EVENT_COMPANIES_HOUSE,
    EVENT_COMPANIES_HOUSE_ALERT,
    Dataset,
)
from custom_components.companies_house.coordinator import CompanyRuntime
from custom_components.companies_house.digest import describe_change
from custom_components.companies_house.models import FilingHistoryItem

from .conftest import company_subentry, load_fixture, mock_company
from .test_sensor import _subentry_obj

ACTIVE = "12345678"
IXBRL = Path(__file__).parent / "fixtures" / "ixbrl"
S3 = "https://s3.example.invalid"
NOW = "2026-09-15T09:00:00+00:00"


def _ixbrl(name: str) -> bytes:
    return (IXBRL / f"{name}.html").read_bytes()


def _item(
    transaction_id: str,
    made_up_to: str,
    filed_on: str,
    *,
    document_id: str | None = None,
    accounts_type: str = "micro-entity",
    filing_type: str = "AA",
    paper_filed: bool = False,
    description: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "transaction_id": transaction_id,
        "category": "accounts",
        "type": filing_type,
        "date": filed_on,
        "description": description
        or (
            f"accounts-amended-with-accounts-type-{accounts_type}"
            if filing_type == "AAMD"
            else f"accounts-with-accounts-type-{accounts_type}"
        ),
        "description_values": {"made_up_date": made_up_to},
        "action_date": made_up_to,
        "paper_filed": paper_filed,
        "pages": 5,
        "links": {"self": f"/company/{ACTIVE}/filing-history/{transaction_id}"},
    }
    if document_id:
        item["links"]["document_metadata"] = (
            f"{DOCUMENT_API_BASE}/document/{document_id}"
        )
    return item


# The register's accounts filings for the active company, newest first.
FILINGS = [
    _item(
        "tx-2025",
        "2025-12-31",
        "2026-09-10",
        document_id="doc-2025",
        accounts_type="full",
    ),
    _item(
        "tx-2024",
        "2024-12-31",
        "2025-09-01",
        document_id="doc-2024",
        accounts_type="full",
    ),
    _item(
        "tx-2023a",
        "2023-12-31",
        "2024-10-01",
        document_id="doc-2023a",
        accounts_type="small",
        filing_type="AAMD",
    ),
    _item(
        "tx-2023",
        "2023-12-31",
        "2024-09-01",
        document_id="doc-2023",
        accounts_type="small",
    ),
    _item(
        "tx-2022", "2022-12-31", "2023-09-01", document_id="doc-2022", paper_filed=True
    ),
    _item("tx-2021", "2021-12-31", "2022-09-01", document_id="doc-2021"),
    _item("tx-2019", "2019-12-31", "2020-09-01", document_id="doc-2019"),
    _item(
        "tx-ard",
        "2025-12-31",
        "2025-02-01",
        filing_type="AA01",
        description="change-account-reference-date-company-current-extended",
    ),
]
# What the document API answers for each: bytes to serve (via a redirect),
# 406 for PDF-only accounts, or a PDF body with metadata.
DOCUMENTS: dict[str, Any] = {
    "doc-2025": _ixbrl("full_frs102"),
    "doc-2024": 406,
    "doc-2023a": _ixbrl("small_filleted"),
    "doc-2021": b"%PDF-1.4 scanned",
}


def mock_accounts(
    aioclient_mock: AiohttpClientMocker,
    number: str = ACTIVE,
    items: list[dict[str, Any]] | None = None,
    documents: dict[str, Any] | None = None,
    *,
    metadata_xhtml: bool = False,
) -> None:
    """Mock the accounts listing and the documents behind it.

    Must run before ``setup_entry`` so the listing beats the default 404.
    """
    items = FILINGS if items is None else items
    aioclient_mock.get(
        f"{API_BASE}/company/{number}/filing-history?category=accounts",
        json={"items": items, "total_count": len(items)},
    )
    for document_id, spec in (DOCUMENTS if documents is None else documents).items():
        content = f"{DOCUMENT_API_BASE}/document/{document_id}/content"
        if spec == 406:
            aioclient_mock.get(content, status=406)
            continue
        if isinstance(spec, Exception):
            aioclient_mock.get(content, exc=spec)
            continue
        if isinstance(spec, int):
            aioclient_mock.get(
                content, status=302, headers={"Location": f"{S3}/{document_id}"}
            )
            aioclient_mock.get(f"{S3}/{document_id}", status=spec)
            continue
        aioclient_mock.get(
            content, status=302, headers={"Location": f"{S3}/{document_id}"}
        )
        aioclient_mock.get(f"{S3}/{document_id}", content=spec)
        resources = {"application/pdf": {"content_length": 100}}
        if metadata_xhtml or b"inlineXBRL" in spec:
            resources["application/xhtml+xml"] = {"content_length": len(spec)}
        aioclient_mock.get(
            f"{DOCUMENT_API_BASE}/document/{document_id}",
            json={"resources": resources, "company_number": number},
        )


async def run_backfill(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, delay: timedelta | None = None
) -> None:
    """Let the queue's timer fire and the read finish."""
    freezer.tick((delay or BACKFILL_START_DELAY) + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def _company(entry: Any, number: str = ACTIVE) -> CompanyRuntime:
    company: CompanyRuntime = entry.runtime_data.companies[f"sub_{number}"]
    return company


def _document_calls(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [
        c[1].path
        for c in aioclient_mock.mock_calls
        if c[1].host.startswith("document-api") or c[1].host.startswith("s3")
    ]


# ---------------------------------------------------------------- back-fill


async def test_backfill_reads_five_years_after_setup(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Setup only seeds; a minute later the queue reads the last five years."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    coordinator = company.accounts
    assert coordinator.data is not None
    assert not coordinator.data.checked
    assert coordinator.wants_reading
    assert coordinator.last_reason == "waiting for the first read"
    assert entry.runtime_data.accounts_backfill.queued == [ACTIVE]
    assert not _document_calls(aioclient_mock)
    state = hass.states.get("sensor.example_trading_limited_latest_accounts")
    assert state is not None
    assert state.state == "unknown"
    assert state.attributes["status"] == "not read yet"

    await run_backfill(hass, freezer)
    history = coordinator.data
    assert history.checked
    assert not coordinator.wants_reading
    assert entry.runtime_data.accounts_backfill.queued == []
    assert coordinator.retry_in is None
    assert coordinator.read_count == 2
    assert [y.made_up_to.isoformat() for y in history.years] == [
        "2025-12-31",
        "2024-12-31",
        "2023-12-31",
        "2022-12-31",
        "2021-12-31",
    ]
    by_year = {y.made_up_to.year: y for y in history.years}
    assert by_year[2025].status == "ok"
    assert by_year[2025].source == "ixbrl"
    assert by_year[2025].accounts_type == "full"
    assert by_year[2025].value("turnover") == Decimal(13784)
    assert by_year[2025].figure("turnover").prior == Decimal(16600)
    assert by_year[2025].prior_period_end == date(2024, 12, 31)
    assert by_year[2025].accounting_standard == "SmallEntities"
    # PDF-only accounts (406) borrow the next year's comparative column.
    assert by_year[2024].status == "no_ixbrl"
    assert by_year[2024].source == "comparative"
    assert by_year[2024].value("turnover") == Decimal(16600)
    assert by_year[2024].figure("turnover").prior is None
    # The amended set superseded the original; only it was fetched.
    assert by_year[2023].transaction_id == "tx-2023a"
    assert by_year[2023].amended
    assert by_year[2023].status == "ok"
    assert by_year[2023].value("cash") == Decimal(11)
    # Paper accounts cost no request. They would take the next year's
    # comparative column too, but that document's year before is not 2022
    # (its own year end is 28 Feb 2026), so nothing is put under 2022.
    assert by_year[2023].prior_period_end == date(2025, 2, 28)
    assert by_year[2022].status == "no_ixbrl"
    assert by_year[2022].source == "none"
    assert not by_year[2022].has_figures
    # A PDF body when structured data was asked for: the metadata settles it.
    assert by_year[2021].status == "no_ixbrl"
    assert by_year[2021].source == "none"
    assert not by_year[2021].has_figures
    calls = _document_calls(aioclient_mock)
    assert "/document/doc-2023/content" not in calls
    assert "/document/doc-2019/content" not in calls
    assert "/document/doc-2022/content" not in calls
    assert "/document/doc-2021" in calls
    assert calls.count("/document/doc-2025/content") == 1

    read = [e for e in events if e.data["kind"] == "accounts"]
    assert len(read) == 1
    assert read[0].data["event_type"] == "read"
    assert read[0].data["made_up_to"] == "2025-12-31"
    assert read[0].data["figures"]["turnover"] == 13784
    assert read[0].data["figures"]["net_assets"] == -1026
    assert read[0].data["changes"]["turnover"] == -17.0
    assert read[0].data["flags"] == [
        "Net liabilities",
        "Creditors exceed cash and debtors",
    ]
    assert company.state.snapshots["accounts"].data is not None
    # The figures feed the risk rating: net liabilities and creditors beyond
    # the cash took the company from green to amber, logged after the read.
    assert [c["kind"] for c in company.state.changes[:2]] == ["status", "accounts"]
    rated = [e for e in events if e.data["event_type"] == "risk-changed"]
    assert [(e.data["old_band"], e.data["new_band"]) for e in rated] == [
        ("green", "amber")
    ]
    assert company.risk is not None
    assert company.risk.info["accounts_figures_at"] == "2025-12-31"


async def test_sensors_show_the_latest_figures(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The six sensors carry the newest figures, priors, changes and the series."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    await setup_entry([ACTIVE])
    await run_backfill(hass, freezer)
    prefix = "sensor.example_trading_limited_"
    latest = hass.states.get(prefix + "latest_accounts")
    assert latest is not None
    assert latest.state == "2025-12-31"
    attrs = latest.attributes
    assert attrs["status"] == "read"
    assert attrs["accounts_type"] == "full accounts"
    assert attrs["source"] == "ixbrl"
    assert attrs["filed_on"] == "2026-09-10"
    assert attrs["years_read"] == 2
    assert attrs["flags"] == ["Net liabilities", "Creditors exceed cash and debtors"]
    assert attrs["figures"]["turnover"] == {
        "value": 13784,
        "prior": 16600,
        "change_percent": -17.0,
        "status": "ok",
    }
    assert attrs["figures"]["cash"]["change_percent"] == 61.4
    assert attrs["statistics"]["turnover"] == "companies_house:12345678_turnover"
    assert attrs["document"].endswith(
        "/filing-history/tx-2025/document?format=pdf&download=0"
    )
    assert [row["made_up_to"] for row in attrs["series"]] == [
        "2023-12-31",
        "2024-12-31",
        "2025-12-31",
    ]
    assert attrs["series"][-1]["turnover"] == 13784
    assert attrs["series"][-2]["source"] == "comparative"
    assert hass.states.get(prefix + "turnover").state == "13784"
    assert (
        hass.states.get(prefix + "turnover").attributes["unit_of_measurement"] == "GBP"
    )
    assert hass.states.get(prefix + "profit_before_tax").state == "2643"
    assert hass.states.get(prefix + "cash").state == "13552"
    assert hass.states.get(prefix + "net_assets").state == "-1026"
    assert hass.states.get(prefix + "employees").state == "0"
    turnover_attrs = hass.states.get(prefix + "turnover").attributes
    assert turnover_attrs["made_up_to"] == "2025-12-31"
    assert turnover_attrs["prior"] == 16600
    assert turnover_attrs["change_percent"] == -17.0
    assert turnover_attrs["status"] == "ok"
    assert turnover_attrs["statistic_id"] == "companies_house:12345678_turnover"


async def test_micro_accounts_explain_why_figures_are_missing(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Turnover is unknown for micro-entity accounts, and the attributes say why."""
    freezer.move_to(NOW)
    items = [_item("tx-1", "2025-12-31", "2026-09-01", document_id="doc-1")]
    mock_accounts(
        aioclient_mock,
        items=items,
        documents={"doc-1": _ixbrl("micro_hidden_employees")},
    )
    await setup_entry([ACTIVE])
    await run_backfill(hass, freezer)
    prefix = "sensor.example_trading_limited_"
    assert hass.states.get(prefix + "turnover").state == "unknown"
    assert (
        hass.states.get(prefix + "turnover").attributes["status"]
        == "not disclosed (micro-entity accounts)"
    )
    assert hass.states.get(prefix + "net_assets").state == "100"
    assert hass.states.get(prefix + "employees").state == "0"
    latest = hass.states.get(prefix + "latest_accounts").attributes
    assert (
        latest["figures"]["cash"]["status"] == "not disclosed (micro-entity accounts)"
    )
    assert latest["flags"] == []


async def test_new_accounts_filing_is_read_when_the_probe_sees_it(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A probe hit on an accounts filing reads just that filing, and fires one event."""
    freezer.move_to(NOW)
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    old_items = FILINGS[1:]
    mock_accounts(aioclient_mock, items=old_items)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    await run_backfill(hass, freezer)
    assert company.accounts.data is not None
    assert company.accounts.data.latest.made_up_to == date(2024, 12, 31)
    # Accounts filed a year ago are history: remembered under their filing
    # date, announced to nobody.
    assert [e for e in events if e.data["kind"] == "accounts"] == []
    (old_read,) = [c for c in company.state.changes if c["kind"] == "accounts"]
    assert old_read["at"].startswith("2024-10-01T00:00:00")

    # The register now shows the new accounts, first in the filing history.
    history = load_fixture("company_active/filing_history")
    history["items"].insert(0, FILINGS[0])
    history["total_count"] += 1
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock)
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert company.accounts.data.latest.transaction_id == "tx-2025"
    assert company.accounts.data.latest.status == "ok"
    calls = _document_calls(aioclient_mock)
    assert calls.count("/document/doc-2025/content") == 1
    # Years already read were not fetched again.
    assert "/document/doc-2023a/content" not in calls
    kinds = [(e.data["kind"], e.data["event_type"]) for e in events]
    assert ("filing", "accounts") in kinds
    read = [e for e in events if e.data["kind"] == "accounts"]
    assert len(read) == 1
    assert read[-1].data["transaction_id"] == "tx-2025"


async def test_budget_spent_pauses_the_queue_and_resumes(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A spent scheduled budget keeps what was read and waits for the window."""
    freezer.move_to(NOW)
    items = [
        _item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a"),
        _item("tx-b", "2024-12-31", "2025-06-01", document_id="doc-b"),
    ]
    docs = {"doc-a": _ixbrl("full_frs102"), "doc-b": _ixbrl("small_filleted")}
    mock_accounts(aioclient_mock, items=items, documents=docs)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    limiter = entry.runtime_data.client.limiter
    queue = entry.runtime_data.accounts_backfill
    reset = int(dt_util.utcnow().timestamp()) + 200
    # Enough for the listing and one document, then the reserve is reached.
    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "122", "X-Ratelimit-Reset": str(reset)}
    )
    await run_backfill(hass, freezer)
    history = company.accounts.data
    assert history is not None
    assert history.checked
    assert [y.status for y in history.years] == ["ok", "pending"]
    assert company.accounts.retry_in is not None
    assert queue.queued == [ACTIVE]
    assert company.accounts.last_update_success
    # The window rolls over; the queue picks the same company up again.
    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "600", "X-Ratelimit-Reset": str(reset + 400)}
    )
    await run_backfill(hass, freezer, timedelta(seconds=201))
    assert [y.status for y in company.accounts.data.years] == ["ok", "ok"]
    assert queue.queued == []

    # A budget error on the listing itself is deferred the same way.
    company.accounts.mark_unread()
    assert company.accounts.wants_reading
    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "5", "X-Ratelimit-Reset": str(reset + 10_000)}
    )
    queue.enqueue(company, delay=timedelta(0))
    await run_backfill(hass, freezer, timedelta(0))
    assert company.accounts.last_reason.startswith("deferred")
    assert company.accounts.retry_in is not None
    assert queue.queued == [ACTIVE]


async def test_fetch_failures_are_retried_a_few_times_then_dropped(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A document that cannot be fetched stays pending and is retried three times."""
    freezer.move_to(NOW)
    items = [_item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a")]
    mock_accounts(aioclient_mock, items=items, documents={"doc-a": 500})
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    queue = entry.runtime_data.accounts_backfill
    await run_backfill(hass, freezer)
    year = company.accounts.data.years[0]
    assert year.status == "pending"
    assert "HTTP 500" in (year.error or "")
    assert company.accounts.fetch_failed
    assert company.accounts.retry_in is None
    assert queue.queued == [ACTIVE]
    await run_backfill(hass, freezer, timedelta(minutes=15))
    assert queue.queued == [ACTIVE]
    await run_backfill(hass, freezer, timedelta(minutes=15))
    assert queue.queued == []
    assert company.accounts.wants_reading
    assert _document_calls(aioclient_mock).count("/document/doc-a/content") == 3

    # A listing that fails outright is retried later too, then given up on.
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE, overrides={"accounts_filings": {"items": []}})
    aioclient_mock.clear_requests()
    aioclient_mock.get(
        f"{API_BASE}/company/{ACTIVE}/filing-history?category=accounts", status=500
    )
    queue.enqueue(company, delay=timedelta(0))
    for _ in range(3):
        assert queue.queued == [ACTIVE]
        await run_backfill(hass, freezer, timedelta(minutes=15))
    assert queue.queued == []
    assert not company.accounts.last_update_success


async def test_fetch_failure_lets_other_companies_go_first(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A company whose document fails moves behind the others in the queue."""
    freezer.move_to(NOW)
    items = [_item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a")]
    mock_accounts(aioclient_mock, items=items, documents={"doc-a": 503})
    mock_accounts(
        aioclient_mock,
        "OC123456",
        items=[_item("tx-llp", "2025-03-31", "2026-09-12", document_id="doc-llp")],
        documents={"doc-llp": _ixbrl("micro_doctype")},
    )
    entry = await setup_entry([ACTIVE, "OC123456"])
    queue = entry.runtime_data.accounts_backfill
    await run_backfill(hass, freezer)
    assert queue.queued == ["OC123456", ACTIVE]
    await run_backfill(hass, freezer, BACKFILL_GAP)
    assert queue.queued == [ACTIVE]
    assert _company(entry, "OC123456").accounts.data.latest.status == "ok"
    await run_backfill(hass, freezer, BACKFILL_GAP)
    assert queue.queued == [ACTIVE]
    await run_backfill(hass, freezer, timedelta(minutes=15))
    assert queue.queued == []
    assert _document_calls(aioclient_mock).count("/document/doc-a/content") == 3


async def test_budget_waits_are_never_given_up_on(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """However many windows the budget stays spent, the company keeps its place."""
    freezer.move_to(NOW)
    items = [_item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a")]
    mock_accounts(
        aioclient_mock, items=items, documents={"doc-a": _ixbrl("full_frs102")}
    )
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    limiter = entry.runtime_data.client.limiter
    queue = entry.runtime_data.accounts_backfill
    reset = int(dt_util.utcnow().timestamp())
    for attempt in range(1, 6):
        # Room for the listing only; the document hits the reserve every time.
        reset += 300
        limiter.update_from_headers(
            {"X-Ratelimit-Remain": "121", "X-Ratelimit-Reset": str(reset)}
        )
        await run_backfill(
            hass,
            freezer,
            BACKFILL_START_DELAY if attempt == 1 else timedelta(seconds=300),
        )
        assert company.accounts.retry_in is not None, attempt
        assert company.accounts.data.years[0].status == "pending", attempt
        assert queue.queued == [ACTIVE], attempt
    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "600", "X-Ratelimit-Reset": str(reset + 300)}
    )
    await run_backfill(hass, freezer, timedelta(seconds=300))
    assert company.accounts.data.years[0].status == "ok"
    assert queue.queued == []
    assert _document_calls(aioclient_mock).count("/document/doc-a/content") == 1


async def test_one_bad_document_does_not_hold_the_others_back(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A document the register cannot serve is skipped; the older years are still read."""
    freezer.move_to(NOW)
    items = [
        _item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a"),
        _item("tx-b", "2024-12-31", "2025-06-01", document_id="doc-b"),
        _item("tx-c", "2023-12-31", "2024-06-01", document_id="doc-c"),
    ]
    docs = {
        "doc-a": 500,
        "doc-b": _ixbrl("small_filleted"),
        "doc-c": _ixbrl("micro_doctype"),
    }
    mock_accounts(aioclient_mock, items=items, documents=docs)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    queue = entry.runtime_data.accounts_backfill
    await run_backfill(hass, freezer)
    statuses = {y.transaction_id: y.status for y in company.accounts.data.years}
    assert statuses == {"tx-a": "pending", "tx-b": "ok", "tx-c": "ok"}
    assert company.accounts.fetch_failed
    assert company.accounts.wants_reading
    assert queue.queued == [ACTIVE]
    # The years read are news even though the newest could not be fetched.
    assert company.state.changes[0]["payload"]["transaction_id"] == "tx-b"
    # The retries only ask for the document still missing.
    await run_backfill(hass, freezer, BACKFILL_RETRY_GAP)
    await run_backfill(hass, freezer, BACKFILL_RETRY_GAP)
    calls = _document_calls(aioclient_mock)
    assert calls.count("/document/doc-a/content") == 3
    assert calls.count("/document/doc-b/content") == 1
    assert calls.count("/document/doc-c/content") == 1
    assert queue.queued == []


async def test_probe_read_paused_on_budget_goes_back_to_the_queue(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """New accounts the probe saw are still read when the first try hit the reserve."""
    freezer.move_to(NOW)
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    mock_accounts(aioclient_mock, items=FILINGS[1:])
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    queue = entry.runtime_data.accounts_backfill
    limiter = entry.runtime_data.client.limiter
    await run_backfill(hass, freezer)
    assert queue.queued == []
    assert [e for e in events if e.data["kind"] == "accounts"] == []

    history = load_fixture("company_active/filing_history")
    history["items"].insert(0, FILINGS[0])
    history["total_count"] += 1
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock)
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    # Room for the probe and the listing; the document itself hits the reserve.
    reset = int(dt_util.utcnow().timestamp()) + 200
    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "122", "X-Ratelimit-Reset": str(reset)}
    )
    await company.probe.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    latest = company.accounts.data.latest
    assert latest.transaction_id == "tx-2025"
    assert latest.status == "pending"
    assert company.accounts.retry_in is not None
    assert company.accounts.last_reason == "fetched"
    assert hass.states.get("sensor.example_trading_limited_cash").state == "11"
    # Nobody ran the queue for this, so it took the company back itself.
    assert queue.queued == [ACTIVE]
    assert not _document_calls(aioclient_mock)

    limiter.update_from_headers(
        {"X-Ratelimit-Remain": "600", "X-Ratelimit-Reset": str(reset + 400)}
    )
    await run_backfill(hass, freezer, timedelta(seconds=201))
    assert company.accounts.data.latest.status == "ok"
    assert queue.queued == []
    assert hass.states.get("sensor.example_trading_limited_cash").state == "13552"
    read = [e for e in events if e.data["kind"] == "accounts"]
    assert len(read) == 1
    assert read[-1].data["transaction_id"] == "tx-2025"


async def test_probe_read_that_fails_is_retried_by_the_queue(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A document the register could not serve to the probe's read is tried again later."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock, items=FILINGS[1:])
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    queue = entry.runtime_data.accounts_backfill
    await run_backfill(hass, freezer)
    assert queue.queued == []

    history = load_fixture("company_active/filing_history")
    history["items"].insert(0, FILINGS[0])
    history["total_count"] += 1
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock, documents={**DOCUMENTS, "doc-2025": 503})
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await company.probe.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    latest = company.accounts.data.latest
    assert latest.status == "pending"
    assert "HTTP 503" in (latest.error or "")
    assert queue.queued == [ACTIVE]
    # Not yet: failures wait a while before the next go.
    await run_backfill(hass, freezer, BACKFILL_GAP)
    assert _document_calls(aioclient_mock).count("/document/doc-2025/content") == 1

    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock)
    mock_company(aioclient_mock, ACTIVE, overrides={"filing_history": history})
    await run_backfill(hass, freezer, BACKFILL_RETRY_GAP)
    assert company.accounts.data.latest.status == "ok"
    assert company.accounts.data.latest.error is None
    assert queue.queued == []

    # A read the action asked for that fails on the listing is queued too.
    company.accounts.mark_unread()
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    aioclient_mock.get(
        f"{API_BASE}/company/{ACTIVE}/filing-history?category=accounts", status=500
    )
    response = await hass.services.async_call(
        DOMAIN,
        "read_accounts",
        {"company_number": ACTIVE},
        blocking=True,
        return_response=True,
    )
    # The figures on hand stay until the new read lands.
    assert response["companies"][0]["latest"]["status"] == "ok"
    assert company.accounts.data.latest.reread
    assert company.accounts.wants_reading
    assert not company.accounts.last_update_success
    assert queue.queued == [ACTIVE]


async def test_forced_reread_keeps_the_figures_and_the_rating_until_it_lands(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A forced re-read that stalls neither drops the figures nor moves the band."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    queue = entry.runtime_data.accounts_backfill
    await run_backfill(hass, freezer)
    assert company.state.risk_band == "amber"
    assert company.risk is not None
    assert company.risk.info["accounts_figures_at"] == "2025-12-31"

    # The newest accounts cannot be fetched this time; the older ones can.
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock, documents={**DOCUMENTS, "doc-2025": 500})
    mock_company(aioclient_mock, ACTIVE)
    response = await hass.services.async_call(
        DOMAIN,
        "read_accounts",
        {"company_number": ACTIVE, "force": True},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert response["companies"][0]["latest"]["status"] == "ok"
    by_year = {y.made_up_to.year: y for y in company.accounts.data.years}
    assert by_year[2025].status == "ok"
    assert by_year[2025].reread
    assert "HTTP 500" in (by_year[2025].error or "")
    assert by_year[2025].value("net_assets") == Decimal(-1026)
    assert by_year[2023].status == "ok"
    assert not by_year[2023].reread
    assert company.accounts.wants_reading
    assert company.state.risk_band == "amber"
    assert company.risk.info["accounts_figures_at"] == "2025-12-31"
    assert queue.queued == [ACTIVE]
    # Same filing, same figures: nothing to announce.
    rated = [e for e in events if e.data["event_type"] == "risk-changed"]
    assert [(e.data["old_band"], e.data["new_band"]) for e in rated] == [
        ("green", "amber")
    ]
    assert len([e for e in events if e.data["kind"] == "accounts"]) == 1

    # The queue's retry reads it; still amber, still nothing new to say.
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock)
    mock_company(aioclient_mock, ACTIVE)
    await run_backfill(hass, freezer, BACKFILL_RETRY_GAP)
    latest = company.accounts.data.latest
    assert latest.status == "ok"
    assert not latest.reread
    assert latest.error is None
    assert not company.accounts.wants_reading
    assert queue.queued == []
    assert company.state.risk_band == "amber"
    assert len([e for e in events if e.data["event_type"] == "risk-changed"]) == 1
    assert len([e for e in events if e.data["kind"] == "accounts"]) == 1
    assert company.accounts.read_count == 4


async def test_amended_accounts_are_read_and_announced(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An amended set for a year already read replaces it and fires the read event."""
    freezer.move_to(NOW)
    events = async_capture_events(hass, EVENT_COMPANIES_HOUSE)
    original = _item(
        "tx-1", "2025-12-31", "2026-08-20", document_id="doc-1", accounts_type="full"
    )
    mock_accounts(
        aioclient_mock, items=[original], documents={"doc-1": _ixbrl("full_frs102")}
    )
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    await run_backfill(hass, freezer)
    assert company.accounts.data.latest.value("net_assets") == Decimal(-1026)
    assert len([e for e in events if e.data["kind"] == "accounts"]) == 1

    amended = _item(
        "tx-1a",
        "2025-12-31",
        "2026-09-01",
        document_id="doc-1a",
        accounts_type="full",
        filing_type="AAMD",
    )
    aioclient_mock.clear_requests()
    mock_accounts(
        aioclient_mock,
        items=[amended, original],
        documents={"doc-1a": _ixbrl("full_net_liabilities")},
    )
    mock_company(aioclient_mock, ACTIVE)
    await company.accounts.async_refresh_now(on_demand=True)
    years = company.accounts.data.years
    assert [y.transaction_id for y in years] == ["tx-1a"]
    assert years[0].amended
    assert years[0].value("net_assets") == Decimal(-48576)
    read = [e for e in events if e.data["kind"] == "accounts"]
    assert len(read) == 2
    assert read[-1].data["transaction_id"] == "tx-1a"
    assert read[-1].data["amended"] is True
    assert read[-1].data["figures"]["employees"] == 2
    # Reading again with nothing new is not news.
    await company.accounts.async_refresh_now(on_demand=True)
    assert len([e for e in events if e.data["kind"] == "accounts"]) == 2


async def test_unreadable_documents_are_marked_not_retried(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Malformed structured data, a PDF where iXBRL was promised, missing metadata."""
    freezer.move_to(NOW)
    items = [
        _item("tx-a", "2025-12-31", "2026-06-01", document_id="doc-a"),
        _item("tx-b", "2024-12-31", "2025-06-01", document_id="doc-b"),
        _item("tx-c", "2023-12-31", "2024-06-01", document_id="doc-c"),
        _item("tx-d", "2022-12-31", "2023-06-01", document_id="doc-d"),
    ]
    docs = {
        "doc-a": b"<html xmlns:ix='http://www.xbrl.org/2013/inlineXBRL'><body>inlineXBRL but broken",
        "doc-b": b"%PDF-1.4 but the metadata promises xhtml",
        "doc-c": b"%PDF-1.4 and no metadata at all",
        "doc-d": b"<" + b" " * (8 * 1024 * 1024),
    }
    mock_accounts(aioclient_mock, items=items, documents=docs, metadata_xhtml=True)
    aioclient_mock.clear_requests()
    mock_accounts(aioclient_mock, items=items, documents=docs, metadata_xhtml=True)
    entry = await setup_entry([ACTIVE])
    company = _company(entry)
    await run_backfill(hass, freezer)
    statuses = {
        y.transaction_id: (y.status, y.error) for y in company.accounts.data.years
    }
    assert statuses["tx-a"][0] == "parse_error"
    assert "malformed XML" in statuses["tx-a"][1]
    assert statuses["tx-b"] == ("parse_error", "not an iXBRL document")
    assert statuses["tx-d"][0] == "parse_error"
    assert "too large" in statuses["tx-d"][1]
    assert not company.accounts.wants_reading
    assert entry.runtime_data.accounts_backfill.queued == []


async def test_missing_metadata_means_no_structured_data(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A PDF body whose metadata cannot be found is simply not structured."""
    freezer.move_to(NOW)
    items = [_item("tx-c", "2023-12-31", "2024-06-01", document_id="doc-c")]
    aioclient_mock.get(
        f"{API_BASE}/company/{ACTIVE}/filing-history?category=accounts",
        json={"items": items, "total_count": 1},
    )
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/doc-c/content",
        status=302,
        headers={"Location": f"{S3}/doc-c"},
    )
    aioclient_mock.get(f"{S3}/doc-c", content=b"%PDF-1.4")
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/doc-c", status=404)
    entry = await setup_entry([ACTIVE])
    await run_backfill(hass, freezer)
    year = _company(entry).accounts.data.years[0]
    assert year.status == "no_ixbrl"
    assert year.source == "none"


async def test_restart_keeps_the_history_without_requests(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reload seeds from the snapshot; nothing is read again."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    entry = await setup_entry([ACTIVE])
    await run_backfill(hass, freezer)
    await entry.runtime_data.store.async_save()
    assert not _company(entry).accounts.wants_reading
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, ACTIVE)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    company = _company(entry)
    assert company.accounts.data is not None
    assert company.accounts.data.checked
    assert company.accounts.last_reason == "snapshot"
    assert company.accounts.data.latest_read.value("turnover") == Decimal(13784)
    assert entry.runtime_data.accounts_backfill.queued == []
    await run_backfill(hass, freezer)
    assert not any("category=accounts" in str(c[1]) for c in aioclient_mock.mock_calls)
    assert hass.states.get("sensor.example_trading_limited_turnover").state == "13784"
    # Reconciliation leaves accounts alone too.
    assert Dataset.ACCOUNTS in company.coordinators


async def test_queue_reads_companies_one_at_a_time(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Several companies are read in turn with a pause between them."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    mock_accounts(
        aioclient_mock,
        "OC123456",
        items=[_item("tx-llp", "2025-03-31", "2026-09-12", document_id="doc-llp")],
        documents={"doc-llp": _ixbrl("micro_doctype")},
    )
    entry = await setup_entry([ACTIVE, "OC123456", "23456789"])
    queue = entry.runtime_data.accounts_backfill
    assert queue.queued == [ACTIVE, "OC123456", "23456789"]
    await run_backfill(hass, freezer)
    assert queue.queued == ["OC123456", "23456789"]
    assert _company(entry).accounts.data.checked
    assert not _company(entry, "OC123456").accounts.data.checked
    await run_backfill(hass, freezer, BACKFILL_GAP)
    assert queue.queued == ["23456789"]
    llp = _company(entry, "OC123456").accounts.data
    assert llp.checked
    assert llp.latest.value("net_assets") == Decimal(100)
    assert (
        statistic_id("OC123456", "net_assets") == "companies_house:oc123456_net_assets"
    )
    await run_backfill(hass, freezer, BACKFILL_GAP)
    assert queue.queued == []
    # The dissolved company had no accounts filings listed at all.
    dissolved = _company(entry, "23456789").accounts.data
    assert dissolved.checked
    assert dissolved.years == []
    assert not _company(entry, "23456789").accounts.wants_reading
    state = hass.states.get("sensor.old_dissolved_company_limited_latest_accounts")
    if state is not None:
        assert state.attributes["status"] == "no accounts found"

    # Removing a company drops it from the queue; enqueueing twice is once.
    company = _company(entry)
    company.accounts.mark_unread()
    queue.enqueue(company, delay=timedelta(0))
    queue.enqueue(company, delay=timedelta(0))
    queue.enqueue(_company(entry, "OC123456"), delay=timedelta(0), front=True)
    queue.enqueue(company, delay=timedelta(0), front=True)
    assert queue.queued == [ACTIVE, "OC123456"]
    queue.discard(company)
    queue.discard(company)
    assert queue.queued == ["OC123456"]
    queue.discard(_company(entry, "OC123456"))
    assert queue.queued == []
    # The armed timer finds nothing to do.
    calls = aioclient_mock.call_count
    await run_backfill(hass, freezer, timedelta(0))
    assert aioclient_mock.call_count == calls
    queue.async_shutdown()
    assert queue.queued == []
    # Forgetting reads before anything was read is harmless.
    company.accounts.data = None
    company.accounts.mark_unread()
    assert company.accounts.data is None
    assert company.accounts.wants_reading


async def test_statistics_are_imported_for_every_year(
    recorder_mock: Any,
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With the recorder running, one row per financial year lands per metric."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        list_statistic_ids,
        statistics_during_period,
    )
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    mock_accounts(
        aioclient_mock,
        "OC123456",
        items=[_item("tx-llp", "2025-03-31", "2026-09-12", document_id="doc-llp")],
        documents={"doc-llp": _ixbrl("micro_doctype")},
    )
    entry = await setup_entry([ACTIVE, "OC123456"])
    await run_backfill(hass, freezer)
    await run_backfill(hass, freezer, BACKFILL_GAP)
    await async_wait_recording_done(hass)
    ids = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, None, None
    )
    by_id = {s["statistic_id"]: s for s in ids}
    # A figure never disclosed gets no series at all.
    assert "companies_house:oc123456_net_assets" in by_id
    assert "companies_house:oc123456_turnover" not in by_id
    assert _company(entry).accounts.interval(dt_util.utcnow())[1] == "read as filed"
    turnover = by_id["companies_house:12345678_turnover"]
    assert turnover["source"] == DOMAIN
    assert turnover["name"] == "EXAMPLE TRADING LIMITED turnover"
    assert turnover["statistics_unit_of_measurement"] == "GBP"
    assert turnover["has_mean"]
    assert not turnover["has_sum"]
    assert (
        by_id["companies_house:12345678_employees"]["statistics_unit_of_measurement"]
        is None
    )
    # Full accounts disclose profit after tax, so that series exists too.
    assert "companies_house:12345678_profit_after_tax" in by_id
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=dt_util.UTC),
        None,
        {"companies_house:12345678_turnover", "companies_house:12345678_net_assets"},
        "year",
        None,
        {"mean", "min", "max"},
    )
    turnover_rows = stats["companies_house:12345678_turnover"]
    assert [row["mean"] for row in turnover_rows] == [16600.0, 13784.0]
    net_rows = stats["companies_house:12345678_net_assets"]
    assert [row["mean"] for row in net_rows] == [11.0, -2865.0, -1026.0]
    # The rows sit at local midnight on the year end.
    first = dt_util.utc_from_timestamp(net_rows[0]["start"])
    assert dt_util.as_local(first).year == 2023


# ---------------------------------------------------------------- actions


async def test_accounts_action_returns_the_history(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The accounts action answers from memory, in plain data."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    await setup_entry([ACTIVE])
    await run_backfill(hass, freezer)
    before = aioclient_mock.call_count
    response = await hass.services.async_call(
        DOMAIN,
        "accounts",
        {"company_number": "12345678"},
        blocking=True,
        return_response=True,
    )
    assert aioclient_mock.call_count == before
    assert response["company_name"] == "EXAMPLE TRADING LIMITED"
    assert response["checked"]
    assert response["latest"]["made_up_to"] == "2025-12-31"
    assert response["latest"]["figures"]["turnover"]["text"] == "£13.8k"
    assert response["latest"]["figures"]["profit_after_tax"]["text"] == "£1.8k"
    assert response["latest"]["flags"] == [
        "Net liabilities",
        "Creditors exceed cash and debtors",
    ]
    assert response["latest"]["status_words"] == "read"
    assert len(response["years"]) == 5
    assert response["years"][1]["source"] == "comparative"
    assert response["years"][1]["status_words"] == "no structured data"
    # A year with no structured data says so, rather than "not disclosed".
    assert response["years"][4]["status"] == "no_ixbrl"
    assert response["years"][4]["figures"]["turnover"]["text"] == "no structured data"
    # A year that borrowed the comparative column carries its figures.
    assert response["years"][1]["figures"]["turnover"]["text"] == "£16.6k"
    assert response["years"][1]["figures"]["turnover"]["status"] == "ok"
    assert len(response["series"]) == 3
    with pytest.raises(ServiceValidationError, match="not being watched"):
        await hass.services.async_call(
            DOMAIN,
            "accounts",
            {"company_number": "99999999"},
            blocking=True,
            return_response=True,
        )


async def test_read_accounts_action(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """One company is read at once on demand; all companies are queued."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    entry = await setup_entry([ACTIVE, "OC123456"])
    queue = entry.runtime_data.accounts_backfill
    queue.async_shutdown()
    response = await hass.services.async_call(
        DOMAIN,
        "read_accounts",
        {"company_number": "12345678"},
        blocking=True,
        return_response=True,
    )
    assert response["read"] == 1
    assert response["companies"][0]["latest"]["made_up_to"] == "2025-12-31"
    company = _company(entry)
    assert company.accounts.read_count == 2
    fetched = _document_calls(aioclient_mock).count("/document/doc-2025/content")
    assert fetched == 1
    # Read again without force: nothing is fetched twice.
    await hass.services.async_call(
        DOMAIN,
        "read_accounts",
        {"company_number": "12345678"},
        blocking=True,
        return_response=True,
    )
    assert _document_calls(aioclient_mock).count("/document/doc-2025/content") == 1
    # With force everything readable is fetched again.
    await hass.services.async_call(
        DOMAIN,
        "read_accounts",
        {"company_number": "12345678", "force": True},
        blocking=True,
        return_response=True,
    )
    assert _document_calls(aioclient_mock).count("/document/doc-2025/content") == 2
    assert company.accounts.read_count == 4

    # Without a company number every company that still wants reading is queued.
    response = await hass.services.async_call(
        DOMAIN, "read_accounts", {}, blocking=True, return_response=True
    )
    assert response == {"queued": 1, "companies": ["OC123456"]}
    assert queue.queued == ["OC123456"]
    await run_backfill(hass, freezer, timedelta(0))
    assert queue.queued == []
    response = await hass.services.async_call(
        DOMAIN, "read_accounts", {"force": True}, blocking=True, return_response=True
    )
    assert response["queued"] == 2
    assert queue.queued == [ACTIVE, "OC123456"]


async def test_refresh_action_can_ask_for_accounts(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The refresh action's dataset list includes accounts."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    entry = await setup_entry([ACTIVE])
    entry.runtime_data.accounts_backfill.async_shutdown()
    await hass.services.async_call(
        DOMAIN,
        "refresh",
        {"config_entry_id": entry.entry_id, "datasets": ["accounts"]},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert _company(entry).accounts.data.checked
    assert hass.states.get("sensor.example_trading_limited_turnover").state == "13784"


# ---------------------------------------------------------------- report


async def test_report_has_an_accounts_section(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Accounts read this week get a table of figures, flags and links."""
    freezer.move_to(NOW)
    mock_accounts(aioclient_mock)
    mock_accounts(
        aioclient_mock,
        "OC123456",
        items=[_item("tx-llp", "2025-03-31", "2026-09-12", document_id="doc-llp")],
        documents={"doc-llp": _ixbrl("micro_hidden_employees")},
    )
    mock_accounts(
        aioclient_mock,
        "56789012",
        items=[
            _item(
                "tx-sub",
                "2026-02-28",
                "2026-09-11",
                document_id="doc-sub",
                accounts_type="small",
            )
        ],
        documents={"doc-sub": _ixbrl("small_filleted")},
    )
    await setup_entry([ACTIVE, "OC123456", "56789012"], close_watch={"OC123456"})
    await run_backfill(hass, freezer)
    await run_backfill(hass, freezer, BACKFILL_GAP)
    await run_backfill(hass, freezer, BACKFILL_GAP)
    response = await hass.services.async_call(
        DOMAIN, "digest", {"days": 7}, blocking=True, return_response=True
    )
    assert response["summary"]["accounts_read"] == 3
    read = response["accounts_read"]
    # The one with flags first, then the closely watched one.
    assert [a["number"] for a in read] == [ACTIVE, "OC123456", "56789012"]
    first = read[0]
    assert first["accounts_type"] == "full accounts"
    assert first["made_up_to"] == "2025-12-31"
    assert first["flags"] == ["Net liabilities", "Creditors exceed cash and debtors"]
    assert first["undisclosed"] is None
    rows = {r["metric"]: r for r in first["rows"]}
    assert rows["turnover"] == {
        "metric": "turnover",
        "name": "Turnover",
        "value": "£13.8k",
        "prior": "£16.6k",
        "change": "down 17 %",
        "change_percent": -17.0,
        "worse": True,
    }
    assert rows["cash"]["worse"] is False
    assert rows["creditors_within_one_year"]["worse"] is True
    assert first["document"].endswith("tx-2025/document?format=pdf&download=0")
    second = read[1]
    assert second["undisclosed"] == (
        "Turnover, profit and cash: not disclosed (micro-entity accounts)"
    )
    assert [r["metric"] for r in second["rows"]] == ["net_assets", "employees"]
    # Filleted accounts keep cash: the note names only what is missing, so
    # it never contradicts the table above it.
    third = read[2]
    assert third["undisclosed"] == (
        "Turnover and profit: not disclosed (small company accounts)"
    )
    assert {r["metric"]: r["value"] for r in third["rows"]}["cash"] == "£11"
    html = response["html"]
    assert "Accounts read this week" in html
    assert "£13.8k" in html
    assert "down 17 %" in html
    assert "Net liabilities" in html
    assert "Open accounts PDF" in html
    assert "micro-entity accounts" in html
    text = response["text"]
    assert "Accounts read this week:" in text
    assert "Turnover: £13.8k (last year £16.6k, down 17 %)" in text
    assert "Flags: Net liabilities, Creditors exceed cash and debtors" in text
    assert "Turnover, profit and cash: not disclosed (micro-entity accounts)" in text
    assert "Turnover and profit: not disclosed (small company accounts)" in text
    assert "Cash: £11 (last year £11, unchanged)" in text
    # The company cards carry the figures for other features.
    cards = {c["number"]: c for c in response["companies"]}
    assert cards[ACTIVE]["accounts"]["figures"]["turnover"] == 13784
    assert cards[ACTIVE]["accounts"]["changes"]["turnover"] == -17.0
    assert cards[ACTIVE]["accounts"]["flags"] == [
        "Net liabilities",
        "Creditors exceed cash and debtors",
    ]
    change = next(c for c in cards[ACTIVE]["changes"] if c["kind"] == "accounts")
    assert change["title"] == "EXAMPLE TRADING LIMITED: accounts read"
    assert change["message"].startswith(
        "Full accounts to 31 Dec 2025: Turnover £13.8k (down 17 %), "
        "profit before tax £2.6k (down 51.3 %), profit after tax £1.8k (down 66.1 %), "
        "cash £13.6k (up 61.4 %), net assets -£1k (up 64.2 %), "
        "creditors due within a year £186k (up 0.9 %), "
    )
    assert change["message"].endswith(
        "0 employees. Net liabilities. Creditors exceed cash and debtors."
    )
    assert change["links"][0]["text"] == "Accounts filed"
    assert change["score"] == 6


async def test_alerts_describe_the_accounts(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A company set to notify instantly raises an alert when its accounts are read."""
    freezer.move_to(NOW)
    items = [_item("tx-1", "2025-12-31", "2026-09-01", document_id="doc-1")]
    mock_accounts(
        aioclient_mock, items=items, documents={"doc-1": _ixbrl("dormant_aa02")}
    )
    alerts = async_capture_events(hass, EVENT_COMPANIES_HOUSE_ALERT)
    entry = await setup_entry([ACTIVE])
    _company(entry).notify_instantly = True
    await run_backfill(hass, freezer)
    assert len(alerts) == 1
    assert alerts[0].data["title"] == "EXAMPLE TRADING LIMITED: accounts read"
    assert alerts[0].data["message"] == (
        "Micro-entity accounts to 31 Dec 2025: Net assets £1 (unchanged), 0 employees."
    )
    assert alerts[0].data["dormant"] is True
    assert alerts[0].data["link"].endswith("tx-1/document?format=pdf&download=0")


async def test_added_company_joins_the_queue(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A company added later is queued and read without a reload."""
    freezer.move_to(NOW)
    entry = await setup_entry()
    mock_accounts(aioclient_mock)
    mock_company(aioclient_mock, ACTIVE)
    hass.config_entries.async_add_subentry(
        entry, _subentry_obj(company_subentry(ACTIVE, subentry_id=f"sub_{ACTIVE}"))
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.accounts_backfill.queued == [ACTIVE]
    await run_backfill(hass, freezer)
    assert hass.states.get("sensor.example_trading_limited_cash").state == "13552"


# ---------------------------------------------------------------- pure helpers


def _year(**changes: Any) -> AccountsYear:
    base: dict[str, Any] = {
        "transaction_id": "tx",
        "made_up_to": date(2025, 12, 31),
        "status": "ok",
        "source": "ixbrl",
        "figures": {
            "turnover": Figure(value=Decimal(1000), prior=Decimal(2000), status="ok"),
            "net_assets": Figure(value=Decimal(-5), prior=Decimal(10), status="ok"),
        },
    }
    base.update(changes)
    return AccountsYear(**base)


def test_formatting_helpers() -> None:
    """Money, counts and changes read the way people write them."""
    assert format_money(43_300_000) == "£43.3m"
    assert format_money(Decimal(2_000_000)) == "£2m"
    assert format_money(912_345) == "£912k"
    # Rounding never leaves a four-digit thousands figure behind.
    assert format_money(999_999) == "£1m"
    assert format_money(999_499) == "£999k"
    assert format_money(1_060_000) == "£1.1m"
    assert format_money(13_784) == "£13.8k"
    assert format_money(1000) == "£1k"
    assert format_money(-1026) == "-£1k"
    assert format_money(1260) == "£1.3k"
    assert format_money(100) == "£100"
    assert format_money(0) == "£0"
    assert format_money(None) == "not disclosed"
    assert format_count(Decimal("0.02")) == "0.02"
    assert format_count(3) == "3"
    assert format_count(None) == "not disclosed"
    assert format_change(None) == ""
    assert format_change(0) == "unchanged"
    assert format_change(12.5) == "up 12.5 %"
    assert format_change(-3) == "down 3 %"
    # A dormant company that starts trading: written out, never 2.5e+06.
    assert format_change(percent_change(Decimal(25_000), Decimal(1))) == (
        "up 2,499,900 %"
    )
    assert format_change(1000.0) == "up 1,000 %"
    assert format_change(-999.9) == "down 999.9 %"
    assert percent_change(Decimal(110), Decimal(100)) == 10.0
    assert percent_change(Decimal(50), Decimal(-100)) == 150.0
    assert percent_change(Decimal(1), Decimal(0)) is None
    assert percent_change(None, Decimal(1)) is None
    assert plain_number(None) is None
    assert plain_number(Decimal("12.50")) == 12.5
    assert plain_number(Decimal("12.00")) == 12
    assert accounts_type_from_description("accounts-with-accounts-type-full") == (
        "full",
        False,
    )
    assert accounts_type_from_description(
        "accounts-amended-with-accounts-type-micro-entity"
    ) == ("micro-entity", True)
    assert accounts_type_from_description("accounts-dormant-company") == (
        "dormant",
        False,
    )
    assert accounts_type_from_description("accounts-with-made-up-date") == (None, False)
    assert accounts_type_from_description(None) == (None, False)


def test_changes_skip_missing_figures() -> None:
    """Only figures present with a prior get a percentage."""
    assert changes_for({}) == {}
    assert changes_for({"turnover": Figure(value=Decimal(3), prior=Decimal(2))}) == {
        "turnover": 50.0
    }


def test_flags_need_their_inputs() -> None:
    """Each warning fires only on the figures it is about."""
    ok = Figure(value=Decimal(100), prior=Decimal(100), status="ok")
    assert flags_for({}) == []
    assert flags_for({"net_assets": Figure(value=Decimal(-1), status="ok")}) == [
        "Net liabilities"
    ]
    assert flags_for(
        {"cash": Figure(value=Decimal(50), prior=Decimal(100), status="ok")}
    ) == ["Cash halved"]
    assert (
        flags_for({"cash": Figure(value=Decimal(51), prior=Decimal(100), status="ok")})
        == []
    )
    assert flags_for(
        {"turnover": Figure(value=Decimal(80), prior=Decimal(100), status="ok")}
    ) == ["Turnover down 20 %+"]
    assert flags_for(
        {"net_assets": Figure(value=Decimal(75), prior=Decimal(100), status="ok")}
    ) == ["Net assets down 25 %+"]
    assert flags_for(
        {"net_assets": Figure(value=Decimal(-75), prior=Decimal(-100), status="ok")}
    ) == ["Net liabilities"]
    assert flags_for(
        {
            "creditors_within_one_year": Figure(value=Decimal(300), status="ok"),
            "cash": ok,
            "debtors": Figure(value=Decimal(100), status="ok"),
        }
    ) == ["Creditors exceed cash and debtors"]
    assert (
        flags_for(
            {
                "creditors_within_one_year": Figure(value=Decimal(300), status="ok"),
                "cash": ok,
            }
        )
        == []
    )
    assert flags_for(
        {"employees": Figure(value=Decimal(2), prior=Decimal(4), status="ok")}
    ) == ["Headcount halved"]
    assert (
        flags_for(
            {"employees": Figure(value=Decimal(1), prior=Decimal(2), status="ok")}
        )
        == []
    )
    assert (
        flags_for(
            {"employees": Figure(value=Decimal(0), prior=Decimal(1), status="ok")}
        )
        == []
    )


def test_year_and_history_summaries() -> None:
    """Summaries carry words for every state, and an empty history is honest."""
    assert history_summary(None) == {
        "checked": False,
        "latest": None,
        "years": [],
        "series": [],
    }
    empty = history_summary(AccountsHistory())
    assert empty["latest"] is None
    assert empty["checked"] is False
    year = _year(accounts_type="small", accounts_type_member="FilletedAccounts")
    assert year.type_words == "small company accounts"
    assert year.undisclosed_reason == "not disclosed (small company accounts)"
    assert _year(
        accounts_type=None, accounts_type_member="FilletedAccounts"
    ).type_words == ("filleted accounts")
    assert _year(
        accounts_type=None, accounts_type_member="AbridgedAccounts"
    ).type_words == ("abridged accounts")
    assert (
        _year(
            accounts_type=None, accounts_type_member="AbridgedAccounts"
        ).undisclosed_reason
        == "not disclosed (abridged accounts)"
    )
    assert (
        _year(accounts_type=None, dormant=True).type_words == "dormant company accounts"
    )
    assert _year(accounts_type=None).type_words == "accounts"
    assert _year(accounts_type="full").undisclosed_reason is None
    attrs = figure_attributes(_year(accounts_type="full"))
    assert attrs["turnover"]["change_percent"] == -50.0
    assert attrs["cash"]["status"] == "not disclosed"
    attrs = figure_attributes(year)
    assert attrs["cash"]["status"] == "not disclosed (small company accounts)"
    unit = _year(figures={"cash": Figure(status="unit_mismatch")})
    assert figure_attributes(unit)["cash"]["status"] == "unit mismatch"
    # Before the accounts were read nothing is "not disclosed" yet.
    paper = _year(accounts_type="full", status="no_ixbrl", source="none", figures={})
    assert figure_attributes(paper)["turnover"]["status"] == "no structured data"
    pending = _year(accounts_type="micro-entity", status="pending", figures={})
    assert figure_attributes(pending)["cash"]["status"] == "not read yet"
    assert figure_attributes(_year(status="parse_error", figures={}))["cash"][
        "status"
    ] == ("could not be read")
    borrowed = _year(
        accounts_type="full",
        status="no_ixbrl",
        source="comparative",
        figures={"turnover": Figure(value=Decimal(5), status="ok")},
    )
    assert figure_attributes(borrowed)["turnover"]["status"] == "ok"
    assert figure_attributes(borrowed)["cash"]["status"] == (
        "not in the following year's comparatives"
    )
    summary = history_summary(AccountsHistory(years=[year], checked=True))
    assert summary["latest"]["figures"]["turnover"]["text"] == "£1k"
    assert summary["latest"]["flags"] == [
        "Net liabilities",
        "Turnover down 20 %+",
        "Net assets down 25 %+",
    ]
    assert summary["series"] == [
        {
            "made_up_to": "2025-12-31",
            "source": "ixbrl",
            "turnover": 1000,
            "profit_before_tax": None,
            "profit_after_tax": None,
            "cash": None,
            "net_assets": -5,
            "creditors_within_one_year": None,
            "employees": None,
        }
    ]
    # A history whose newest year was not read still points at the newest with figures.
    unread = _year(
        transaction_id="new",
        made_up_to=date(2026, 12, 31),
        status="no_ixbrl",
        source="none",
        figures={},
    )
    history = AccountsHistory(years=[unread, year], checked=True)
    assert history.latest is unread
    assert history.latest_read is year
    assert history_summary(history)["latest"]["made_up_to"] == "2025-12-31"
    assert history.pending == []


def test_describe_figures_and_describe_change() -> None:
    """The sentence for an accounts event reads well with and without figures."""
    payload = {
        "figures": {"turnover": 1_500_000, "employees": 1},
        "changes": {"turnover": 8.0},
        "flags": [],
    }
    assert describe_figures(payload) == "Turnover £1.5m (up 8 %), 1 employee."
    assert describe_figures(
        {"figures": {}, "accounts_type_words": "micro-entity accounts"}
    ) == ("No headline figures are disclosed in micro-entity accounts.")
    assert (
        describe_figures({"figures": {}, "flags": ["Net liabilities"]})
        == "Net liabilities."
    )
    title, message, link = describe_change(
        "accounts",
        "read",
        {"made_up_to": "2025-12-31", "transaction_id": "t9", **payload},
        subject="ACME LTD",
        number="12345678",
    )
    assert title == "ACME LTD: accounts read"
    assert message == "Accounts to 31 Dec 2025: Turnover £1.5m (up 8 %), 1 employee."
    assert link.endswith(
        "/company/12345678/filing-history/t9/document?format=pdf&download=0"
    )


def _filing(**overrides: Any) -> FilingHistoryItem:
    return FilingHistoryItem.from_api(_item(**overrides))


def test_merge_filings_rules() -> None:
    """One year per made-up date, newest filing wins, old and odd filings ignored."""
    today = date(2026, 9, 15)
    items = [
        _filing(
            transaction_id="new",
            made_up_to="2025-12-31",
            filed_on="2026-09-10",
            document_id="d1",
        ),
        _filing(
            transaction_id="dup",
            made_up_to="2025-12-31",
            filed_on="2026-08-01",
            document_id="d0",
        ),
        _filing(
            transaction_id="paper",
            made_up_to="2024-12-31",
            filed_on="2025-09-01",
            paper_filed=True,
            document_id="d2",
        ),
        _filing(transaction_id="nodoc", made_up_to="2023-12-31", filed_on="2024-09-01"),
        _filing(
            transaction_id="ancient",
            made_up_to="2015-12-31",
            filed_on="2016-09-01",
            document_id="d3",
        ),
        _filing(
            transaction_id="ard",
            made_up_to="2025-12-31",
            filed_on="2025-01-01",
            filing_type="AA01",
            description="change-account-reference-date",
        ),
    ]
    nodate = FilingHistoryItem.from_api(
        {"transaction_id": "x", "type": "AA", "category": "accounts"}
    )
    known = AccountsHistory(
        years=[
            _year(transaction_id="dup", made_up_to=date(2025, 12, 31)),
            _year(transaction_id="old", made_up_to=date(2020, 12, 31)),
            _year(transaction_id="older", made_up_to=date(2019, 12, 31)),
            _year(transaction_id="oldest", made_up_to=date(2018, 12, 31)),
        ]
    )
    years = merge_filings(known, [*items, nodate], today)
    assert [y.transaction_id for y in years] == [
        "new",
        "paper",
        "nodoc",
        "old",
        "older",
        "oldest",
    ]
    assert [y.status for y in years] == [
        "pending",
        "no_ixbrl",
        "no_ixbrl",
        "ok",
        "ok",
        "ok",
    ]
    assert years[0].accounts_type == "micro-entity"
    assert years[0].filed_on == date(2026, 9, 10)
    # A year already read is kept as it is even when listed again.
    again = merge_filings(AccountsHistory(years=years[:1]), items[:1], today)
    assert again[0] is years[0]
    assert merge_filings(AccountsHistory(), [], today) == []


def test_fill_comparatives_rules() -> None:
    """Only a year with no data of its own borrows, and only from a year that was read."""
    read = _year(transaction_id="a", made_up_to=date(2025, 12, 31))
    paper = _year(
        transaction_id="b",
        made_up_to=date(2024, 12, 31),
        status="no_ixbrl",
        source="none",
        figures={},
    )
    pending = _year(
        transaction_id="c",
        made_up_to=date(2023, 12, 31),
        status="pending",
        source="none",
        figures={},
    )
    paper_too = _year(
        transaction_id="d",
        made_up_to=date(2022, 12, 31),
        status="no_ixbrl",
        source="none",
        figures={},
    )
    filled = fill_comparatives([read, paper, pending, paper_too])
    assert filled[0] is read
    assert filled[1].source == "comparative"
    assert filled[1].status == "no_ixbrl"
    assert filled[1].value("turnover") == Decimal(2000)
    assert filled[1].figure("turnover").prior is None
    assert filled[1].figure("cash").status == "not_disclosed"
    assert filled[2] is pending
    assert filled[3] is paper_too
    # Nothing to borrow when the newer year's priors are all empty.
    bare = _year(
        transaction_id="e", figures={"turnover": Figure(value=Decimal(1), status="ok")}
    )
    assert fill_comparatives([bare, paper])[1] is paper
    # The newest year never borrows.
    assert fill_comparatives([paper])[0] is paper
    # The comparative column must be about that year: with the 2024 accounts
    # missing from the list, 2023 does not get 2024's figures.
    dated = _year(transaction_id="a", prior_period_end=date(2024, 12, 31))
    assert fill_comparatives([dated, paper])[1].source == "comparative"
    assert fill_comparatives([dated, paper_too])[1] is paper_too
    # A few days between the register's date and the accounts' own is fine.
    close = _year(transaction_id="a", prior_period_end=date(2025, 1, 5))
    assert fill_comparatives([close, paper])[1].source == "comparative"
    far = _year(transaction_id="a", prior_period_end=date(2025, 2, 28))
    assert fill_comparatives([far, paper])[1] is paper
    # When the accounts were read before the date was kept, a year within
    # eighteen months passes for the year before; a bigger gap does not.
    assert fill_comparatives([read, paper_too])[1] is paper_too
    long_first_year = _year(
        transaction_id="f",
        made_up_to=date(2025, 12, 31),
        status="no_ixbrl",
        source="none",
        figures={},
    )
    assert (
        fill_comparatives(
            [_year(transaction_id="g", made_up_to=date(2027, 3, 31)), long_first_year]
        )[1].source
        == "comparative"
    )


def test_figures_round_trip_through_storage() -> None:
    """Decimal figures survive the store as strings."""
    history = AccountsHistory(
        years=[
            _year(
                figures={
                    "employees": Figure(
                        value=Decimal("2.5"), prior=Decimal(3), status="ok"
                    )
                }
            )
        ],
        checked=True,
    )
    stored = history.to_storage()
    assert stored["years"][0]["figures"]["employees"]["value"] == "2.5"
    back = AccountsHistory.from_storage(stored)
    assert back == history
    broken = AccountsHistory.from_storage(
        {
            "years": [
                {
                    "transaction_id": "t",
                    "made_up_to": "2025-12-31",
                    "figures": {"cash": {"value": "nope", "prior": True}},
                }
            ]
        }
    )
    assert broken.years[0].figure("cash").value is None
    assert broken.years[0].figure("cash").prior is None
