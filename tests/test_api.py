"""Tests for the API client, rate limiter and models."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from datetime import date

import aiohttp
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.api import (
    CompaniesHouseAuthError,
    CompaniesHouseBudgetError,
    CompaniesHouseClient,
    CompaniesHouseConnectionError,
    CompaniesHouseNotFoundError,
    CompaniesHouseRateLimitError,
    Priority,
    RateLimiter,
    parse_window,
)
from custom_components.companies_house.const import API_BASE, DOCUMENT_API_BASE
from custom_components.companies_house.enumerations import FILING_DESCRIPTIONS
from custom_components.companies_house.models import (
    Address,
    CompanyProfile,
    FilingHistory,
    Officer,
    OfficerList,
    format_date,
    officer_id_from_link,
    parse_date,
    render_filing_description,
)

from .conftest import TEST_API_KEY, FakeClock, load_fixture

WINDOW = 300.0


# ---------------------------------------------------------------- rate limiter


def test_parse_window() -> None:
    """Window header parsing tolerates junk."""
    assert parse_window("5m") == 300
    assert parse_window("30s") == 30
    assert parse_window("1h") == 3600
    assert parse_window(None) is None
    assert parse_window("soon") is None


def test_headers_missing_do_not_crash(fake_clock: FakeClock) -> None:
    """Absent or malformed rate limit headers are ignored."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    limiter.update_from_headers({})
    limiter.update_from_headers({"X-Ratelimit-Limit": "abc", "X-Ratelimit-Remain": ""})
    assert limiter.limit == 600
    assert limiter.remaining() == 600
    assert limiter.status().from_headers is False
    assert limiter.status().reset_at is None


def test_headers_trusted_over_local_counter(fake_clock: FakeClock) -> None:
    """Server headers win over the local count while they are fresh."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    limiter.update_from_headers(
        {
            "X-Ratelimit-Limit": "600",
            "X-Ratelimit-Remain": "100",
            "X-Ratelimit-Reset": str(int(fake_clock.now + 120)),
            "X-Ratelimit-Window": "5m",
        }
    )
    assert limiter.remaining() == 100
    assert limiter.status().from_headers is True
    assert limiter.status().reset_at is not None
    # Once the server window rolls we fall back to the local count.
    fake_clock.now += 121
    assert limiter.remaining() == 600
    assert limiter.status().from_headers is False


async def test_bucket_never_exceeds_limit(fake_clock: FakeClock) -> None:
    """700 queued requests never exceed 600 in any simulated 5 minute window."""
    limiter = RateLimiter(
        max_per_second=0, clock=fake_clock, sleep=fake_clock.sleep, window=WINDOW
    )
    stamps: list[float] = []
    for _ in range(700):
        await limiter.acquire(Priority.ON_DEMAND)
        stamps.append(fake_clock.now)
    for t in stamps:
        assert sum(1 for s in stamps if t - WINDOW < s <= t) <= 600
    assert len(stamps) == 700
    assert fake_clock.sleeps, "the 601st request had to wait for the window"


async def test_budget_guard_scheduled_stops_at_80_percent(
    fake_clock: FakeClock,
) -> None:
    """Scheduled work stops at 80 percent while on demand work still succeeds."""
    limiter = RateLimiter(
        max_per_second=0, clock=fake_clock, sleep=fake_clock.sleep, window=WINDOW
    )
    for _ in range(480):
        await limiter.acquire(Priority.SCHEDULED)
    assert limiter.scheduled_allowed() is False
    with pytest.raises(CompaniesHouseBudgetError) as excinfo:
        await limiter.acquire(Priority.SCHEDULED)
    assert excinfo.value.reason == "budget"
    assert 0 < excinfo.value.retry_after <= WINDOW
    # The reserve is available to an action call.
    await limiter.acquire(Priority.ON_DEMAND)
    assert limiter.used() == 481
    assert limiter.status().percent_used == pytest.approx(80.2)
    # After the window rolls scheduled work resumes.
    fake_clock.now += WINDOW + 1
    await limiter.acquire(Priority.SCHEDULED)


async def test_pacing_caps_at_two_per_second(fake_clock: FakeClock) -> None:
    """Sustained rate is capped at 2 requests per second."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    start = fake_clock.now
    for _ in range(5):
        await limiter.acquire(Priority.ON_DEMAND)
    assert fake_clock.now - start == pytest.approx(2.0)


async def test_429_retry_after_honoured(fake_clock: FakeClock) -> None:
    """A 429 with Retry-After blocks for exactly that long."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    assert limiter.note_rate_limited(42) == 42
    with pytest.raises(CompaniesHouseBudgetError) as excinfo:
        await limiter.acquire(Priority.SCHEDULED)
    assert excinfo.value.reason == "throttled"
    assert excinfo.value.retry_after == pytest.approx(42)
    assert limiter.status().blocked_until is not None
    # On demand work waits it out.
    await limiter.acquire(Priority.ON_DEMAND)
    assert fake_clock.sleeps[0] == pytest.approx(42)


async def test_429_without_retry_after_backs_off_exponentially(
    fake_clock: FakeClock,
) -> None:
    """Without Retry-After, back off from 30 seconds and double."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep)
    assert limiter.note_rate_limited(None) == 30
    assert limiter.note_rate_limited(None) == 60
    assert limiter.note_rate_limited(None) == 120
    limiter.note_success()
    assert limiter.note_rate_limited(None) == 30
    # A very long block refuses on demand work rather than hanging.
    limiter.note_rate_limited(WINDOW * 2)
    with pytest.raises(CompaniesHouseRateLimitError):
        await limiter.acquire(Priority.ON_DEMAND)


def test_persistent_throttling(fake_clock: FakeClock) -> None:
    """Throttling past three windows is flagged for a repair issue."""
    limiter = RateLimiter(clock=fake_clock, sleep=fake_clock.sleep, window=WINDOW)
    limiter.note_rate_limited(None)
    assert limiter.persistently_throttled() is False
    fake_clock.now += 3 * WINDOW
    limiter.note_rate_limited(None)
    assert limiter.persistently_throttled() is True
    assert limiter.throttle_count == 2
    limiter.note_success()
    assert limiter.persistently_throttled() is False


# ---------------------------------------------------------------- client


@pytest.fixture
async def client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_clock: FakeClock
) -> AsyncGenerator[CompaniesHouseClient]:
    """Return a client bound to the mocked session with an instant limiter."""
    limiter = RateLimiter(max_per_second=0, clock=fake_clock, sleep=fake_clock.sleep)
    session = aioclient_mock.create_session(hass.loop)
    yield CompaniesHouseClient(session, TEST_API_KEY, limiter=limiter, max_pages=3)
    await session.close()


async def test_basic_auth_header(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The key is sent as the Basic username with an empty password."""
    aioclient_mock.get(
        f"{API_BASE}/search/companies?items_per_page=1&q=test",
        json=load_fixture("search/validate"),
        headers={"X-Ratelimit-Limit": "600", "X-Ratelimit-Remain": "599"},
    )
    await client.validate_key()
    _, _, _, headers = aioclient_mock.mock_calls[0]
    expected = base64.b64encode(f"{TEST_API_KEY}:".encode()).decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["Accept"] == "application/json"
    assert client.last_success is not None


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, CompaniesHouseAuthError),
        (403, CompaniesHouseAuthError),
        (404, CompaniesHouseNotFoundError),
        (500, CompaniesHouseConnectionError),
        (429, CompaniesHouseRateLimitError),
    ],
)
async def test_status_mapping(
    client: CompaniesHouseClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[Exception],
) -> None:
    """HTTP statuses map to typed errors."""
    aioclient_mock.get(f"{API_BASE}/company/12345678", status=status)
    with pytest.raises(error):
        await client.get_company("12345678")
    if status != 404:
        assert client.last_error is not None


async def test_network_error(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Connection failures and bad JSON raise the connection error."""
    aioclient_mock.get(f"{API_BASE}/company/1", exc=aiohttp.ClientConnectionError())
    with pytest.raises(CompaniesHouseConnectionError):
        await client.get_company("1")
    aioclient_mock.get(f"{API_BASE}/company/2", text="not json")
    with pytest.raises(CompaniesHouseConnectionError):
        await client.get_company("2")
    aioclient_mock.get(f"{API_BASE}/company/3", exc=TimeoutError())
    with pytest.raises(CompaniesHouseConnectionError):
        await client.get_company("3")


async def test_429_feeds_limiter(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 429 response with Retry-After blocks the limiter for that long."""
    aioclient_mock.get(
        f"{API_BASE}/company/1", status=429, headers={"Retry-After": "17"}
    )
    with pytest.raises(CompaniesHouseRateLimitError) as excinfo:
        await client.get_company("1")
    assert excinfo.value.retry_after == 17
    assert client.limiter.scheduled_allowed() is False
    client.limiter.note_success()
    aioclient_mock.get(
        f"{API_BASE}/company/2",
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )
    with pytest.raises(CompaniesHouseRateLimitError) as excinfo:
        await client.get_company("2", priority=Priority.ON_DEMAND)
    # A date in the past means "now", which falls back to the backoff.
    assert excinfo.value.retry_after == 30
    client.limiter.note_success()
    aioclient_mock.get(
        f"{API_BASE}/company/3", status=429, headers={"Retry-After": "soon"}
    )
    with pytest.raises(CompaniesHouseRateLimitError) as excinfo:
        await client.get_company("3", priority=Priority.ON_DEMAND)
    assert excinfo.value.retry_after == 30


async def test_get_company_parses_profile(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The profile model reads the non deprecated fields and derives the flags."""
    aioclient_mock.get(
        f"{API_BASE}/company/12345678", json=load_fixture("company_active/profile")
    )
    profile = await client.get_company("12345678")
    assert profile.company_name == "EXAMPLE TRADING LIMITED"
    assert profile.accounts.next_due == date(2026, 12, 31)
    assert profile.accounts.last_made_up_to == date(2025, 3, 31)
    assert profile.accounts.reference_day == 31
    assert profile.accounts.reference_month == 3
    assert profile.confirmation_statement.next_due == date(2027, 3, 25)
    assert profile.has_charges is True  # from links
    assert profile.has_insolvency_history is False
    assert profile.next_deadline == (date(2026, 12, 31), "accounts")
    assert profile.registered_office_address is not None
    assert profile.registered_office_address.one_line() == (
        "47 High Street, Reading, RG1 2AB, England"
    )
    assert profile.previous_company_names[0].name == "EXAMPLE WIDGETS LIMITED"
    # Storage round trip preserves everything.
    assert CompanyProfile.from_storage(profile.to_storage()) == profile
    # A bare profile has no deadline.
    assert CompanyProfile.from_api({"company_number": "1"}).next_deadline is None


async def test_filing_history_probe(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A probe returns total_count and a rendered newest filing; 404 is empty."""
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/filing-history?items_per_page=1&start_index=0",
        json=load_fixture("company_active/filing_history"),
    )
    history = await client.get_filing_history("12345678")
    aioclient_mock.get(
        f"{API_BASE}/company/99/filing-history?items_per_page=1&start_index=0",
        status=404,
    )
    empty = await client.get_filing_history("99")
    assert history.total_count == 6
    newest = history.items[0]
    assert newest.transaction_id == "MzQwMDAwMDAwMDAwMDAwMDAx"
    assert newest.rendered_description == (
        "Confirmation statement made on 11 March 2026 with no updates"
    )
    assert newest.document_id == "doc-MzQwMDAwMDAwMDAwMDAwMDAx"
    assert empty.total_count == 0
    assert FilingHistory.from_storage(history.to_storage()) == history


async def test_officers_paginate(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Officer lists page with items_per_page and start_index."""
    fixture = load_fixture("company_active/officers")
    page1 = {**fixture, "items": fixture["items"][:2], "total_results": 4}
    page2 = {**fixture, "items": fixture["items"][2:], "total_results": 4}
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/officers?items_per_page=100&start_index=0",
        json=page1,
    )
    aioclient_mock.get(
        f"{API_BASE}/company/12345678/officers?items_per_page=100&start_index=2",
        json=page2,
    )
    officers = await client.get_officers("12345678")
    assert officers.total_results == 4
    assert len(officers.items) == 4
    jane = officers.items[0]
    assert jane.officer_id == "officer-jane"
    assert jane.appointment_id == "appt-1"
    assert jane.date_of_birth is not None
    assert jane.date_of_birth.display() == "06/1978"
    assert jane.is_active
    assert officers.items[2].resigned_on == date(2020, 1, 31)
    assert officers.items[3].identification["registration_number"] == "11111111"
    assert OfficerList.from_storage(officers.to_storage()) == officers


async def test_officers_max_pages(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Paging stops at max_pages and at an empty page."""
    item = load_fixture("company_active/officers")["items"][0]
    for start in (0, 1, 2):
        aioclient_mock.get(
            f"{API_BASE}/company/1/officers?items_per_page=100&start_index={start}",
            json={"items": [item], "total_results": 50},
        )
    officers = await client.get_officers("1")
    assert len(officers.items) == 3  # max_pages=3
    aioclient_mock.get(
        f"{API_BASE}/company/2/officers?items_per_page=100&start_index=0",
        json={"items": [item], "total_results": 50},
    )
    aioclient_mock.get(
        f"{API_BASE}/company/2/officers?items_per_page=100&start_index=1",
        json={"items": [], "total_results": 50},
    )
    officers = await client.get_officers("2")
    assert len(officers.items) == 1
    aioclient_mock.get(
        f"{API_BASE}/company/3/officers?items_per_page=100&start_index=0", status=404
    )
    assert (await client.get_officers("3")).items == []


async def test_psc_charges_insolvency_structure(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The remaining datasets parse, and 404s become empty resources."""
    n = "12345678"
    psc_url = f"{API_BASE}/company/{n}/persons-with-significant-control"
    aioclient_mock.get(
        f"{psc_url}?items_per_page=100&start_index=0",
        json=load_fixture("company_active/psc"),
    )
    aioclient_mock.get(
        f"{psc_url}-statements?items_per_page=100&start_index=0", status=404
    )
    psc = await client.get_psc(n)
    aioclient_mock.get(
        f"{API_BASE}/company/{n}/charges?items_per_page=100&start_index=0",
        json=load_fixture("company_active/charges"),
    )
    charges = await client.get_charges(n)
    aioclient_mock.get(f"{API_BASE}/company/{n}/insolvency", status=404)
    insolvency = await client.get_insolvency(n)
    aioclient_mock.get(
        f"{API_BASE}/company/34567890/insolvency",
        json=load_fixture("company_liquidation/insolvency"),
    )
    liquidation = await client.get_insolvency("34567890")
    aioclient_mock.get(
        f"{API_BASE}/company/{n}/registers",
        json=load_fixture("company_active/registers"),
    )
    aioclient_mock.get(
        f"{API_BASE}/company/{n}/exemptions",
        json=load_fixture("company_active/exemptions"),
    )
    aioclient_mock.get(f"{API_BASE}/company/{n}/uk-establishments", status=404)
    structure = await client.get_structure(n)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{psc_url}?items_per_page=100&start_index=0", status=404)
    aioclient_mock.get(
        f"{psc_url}-statements?items_per_page=100&start_index=0", status=404
    )
    no_psc = await client.get_psc(n)
    aioclient_mock.get(
        f"{API_BASE}/company/{n}/charges?items_per_page=100&start_index=0", status=404
    )
    no_charges = await client.get_charges(n)

    assert psc.active_count == 1
    assert psc.items[0].notification_id == "psc-jane"
    assert psc.items[0].natures_of_control[0] == "ownership-of-shares-75-to-100-percent"
    assert psc.statements == []
    assert charges.total_count == 2
    assert charges.outstanding_count == 1
    assert charges.items[0].charge_id == "chg-1"
    assert charges.items[0].persons_entitled == ["HSBC UK Bank Plc"]
    assert insolvency.cases == []
    assert liquidation.status == ["liquidation"]
    assert liquidation.cases[0].dates["wound-up-on"] == date(2026, 7, 1)
    assert liquidation.cases[0].practitioners[0].name == "Alex Carter"
    assert structure.registers == {
        "directors": "public-register",
        "secretaries": "registered-office",
    }
    assert structure.exemptions == ["psc-exempt-as-trading-on-regulated-market"]
    assert structure.uk_establishments == []
    assert no_psc.items == []
    assert no_charges.items == []


async def test_officer_appointments(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Appointments parse including status and resignations."""
    aioclient_mock.get(
        f"{API_BASE}/officers/officer-jane/appointments?items_per_page=100&start_index=0",
        json=load_fixture("officer_many/appointments"),
    )
    appointments = await client.get_officer_appointments("officer-jane")
    assert appointments.name == "Jane Elizabeth SMITH"
    assert appointments.total_results == 23
    assert len(appointments.active) == 20
    assert len(appointments.resigned) == 3
    assert appointments.date_of_birth is not None
    assert appointments.date_of_birth.year == 1978
    assert appointments.items[0].company_number == "10000001"
    assert appointments.items[0].key == "app-1"


async def test_search_and_raw_endpoints(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Every documented endpoint is reachable through the client."""
    n = "12345678"
    psc = f"{API_BASE}/company/{n}/persons-with-significant-control"
    calls = {
        f"{API_BASE}/search/companies?q=example": client.search_companies("example"),
        f"{API_BASE}/search/officers?items_per_page=5&q=smith": client.search_officers(
            "smith", items_per_page=5
        ),
        f"{API_BASE}/search/disqualified-officers?q=smith": (
            client.search_disqualified_officers("smith")
        ),
        f"{API_BASE}/search?q=x": client.search_all("x"),
        f"{API_BASE}/advanced-search/companies?company_name_includes=widget": (
            client.advanced_search({"company_name_includes": "widget"})
        ),
        f"{API_BASE}/alphabetical-search/companies?q=a": client.alphabetical_search(
            "a"
        ),
        f"{API_BASE}/dissolved-search/companies?q=a&search_type=alphabetical": (
            client.dissolved_search("a", search_type="alphabetical")
        ),
        f"{API_BASE}/company/{n}/registered-office-address": (
            client.get_registered_office(n)
        ),
        f"{API_BASE}/company/{n}/officers?register_view=true": client.get_officers_raw(
            n, register_view=True
        ),
        f"{API_BASE}/company/{n}/appointments/a1": client.get_officer_appointment(
            n, "a1"
        ),
        f"{API_BASE}/company/{n}/filing-history/t1": client.get_filing(n, "t1"),
        f"{API_BASE}/company/{n}/charges": client.get_charges_raw(n),
        f"{API_BASE}/company/{n}/charges/c1": client.get_charge(n, "c1"),
        psc: client.get_psc_raw(n),
        f"{psc}-statements": client.get_psc_statements_raw(n),
        f"{psc}/individual/p1": client.get_psc_detail(n, "individual", "p1"),
        f"{psc}-statements/s1": client.get_psc_statement(n, "s1"),
        f"{API_BASE}/company/{n}/insolvency": client.get_insolvency_raw(n),
        f"{API_BASE}/officers/o1/appointments": client.get_officer_appointments_raw(
            "o1"
        ),
        f"{API_BASE}/disqualified-officers/natural/o1": client.get_disqualification(
            "o1"
        ),
        f"{DOCUMENT_API_BASE}/document/d1": client.get_document_metadata("d1"),
    }
    for url in calls:
        aioclient_mock.get(url, json={"ok": url})
    for url, coro in calls.items():
        assert await coro == {"ok": url}


async def test_download_document_follows_redirect_without_auth(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """The document redirect is followed by hand so the key never reaches storage."""
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/d1/content",
        status=302,
        headers={"Location": "https://s3.example.invalid/blob?sig=1"},
    )
    aioclient_mock.get("https://s3.example.invalid/blob?sig=1", content=b"%PDF-1.4")
    assert await client.download_document("d1") == b"%PDF-1.4"
    storage_call = next(c for c in aioclient_mock.mock_calls if "s3" in str(c[1]))
    assert "Authorization" not in storage_call[3]
    api_call = next(c for c in aioclient_mock.mock_calls if "document-api" in str(c[1]))
    assert "Authorization" in api_call[3]
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/d2/content", status=302)
    with pytest.raises(CompaniesHouseConnectionError):
        await client.download_document("d2")
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/document/d3/content", content=b"direct")
    assert await client.download_document("d3") == b"direct"
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/d4/content",
        status=302,
        headers={"Location": "/relative"},
    )
    aioclient_mock.get(f"{DOCUMENT_API_BASE}/relative", status=500)
    with pytest.raises(CompaniesHouseConnectionError):
        await client.download_document("d4")
    aioclient_mock.get(
        f"{DOCUMENT_API_BASE}/document/d5/content",
        status=302,
        headers={"Location": "https://s3.example.invalid/gone"},
    )
    aioclient_mock.get(
        "https://s3.example.invalid/gone", exc=aiohttp.ClientConnectionError()
    )
    with pytest.raises(CompaniesHouseConnectionError):
        await client.download_document("d5")


async def test_concurrent_requests_serialise_through_limiter(
    client: CompaniesHouseClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Concurrent callers all get through and all count."""
    for i in range(5):
        aioclient_mock.get(
            f"{API_BASE}/company/{i}",
            json={"company_number": str(i), "company_name": "X"},
        )
    results = await asyncio.gather(*(client.get_company(str(i)) for i in range(5)))
    assert [r.company_number for r in results] == ["0", "1", "2", "3", "4"]
    assert client.limiter.used() == 5


# ---------------------------------------------------------------- models


def test_date_helpers() -> None:
    """Dates parse strictly and render the Companies House way."""
    assert parse_date("2026-10-31") == date(2026, 10, 31)
    assert parse_date("2026-13-01") is None
    assert parse_date("31/10/2026") is None
    assert parse_date(None) is None
    assert format_date(date(2026, 4, 5)) == "5 April 2026"
    assert format_date(None) is None
    assert officer_id_from_link("/officers/AbC123/appointments") == "AbC123"
    assert officer_id_from_link("/company/1/appointments/x") is None
    assert officer_id_from_link(None) is None


def test_render_filing_description() -> None:
    """Description keys interpolate values, format dates and drop markdown."""
    assert (
        render_filing_description(
            "appoint-person-director-company-with-name-date",
            {"officer_name": "Ms Priya Patel", "appointment_date": "2025-09-01"},
        )
        == "Appointment of Ms Priya Patel as a director on 1 September 2025"
    )
    assert (
        render_filing_description(
            "capital-allotment-shares",
            {"date": "2024-01-02", "capital": [{"currency": "GBP", "figure": "100"}]},
        )
        == "Statement of capital following an allotment of shares on 2 January 2024"
    )
    assert render_filing_description("legacy", {"description": "Old style text"}) == (
        "Old style text"
    )
    assert render_filing_description("some-unknown-key", None) == "Some unknown key"
    assert render_filing_description(None, None) == ""
    assert render_filing_description("confirmation-statement-with-no-updates", {}) == (
        "Confirmation statement made on with no updates"
    )
    assert render_filing_description(
        "change-person-director-company-with-change-date",
        {"officer_name": "X", "change_date": "2024-05-06", "extra": {"a": "b"}},
    ).endswith("on 6 May 2024")


def test_render_filing_description_value_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lists, capital figures and nested objects render sensibly."""
    monkeypatch.setitem(
        FILING_DESCRIPTIONS,
        "test-template",
        "Cap {capital} names {names} obj {obj} n {n}",
    )
    assert (
        render_filing_description(
            "test-template",
            {
                "capital": [{"currency": "GBP", "figure": "100"}, {"figure": "5"}],
                "names": ["A", "B"],
                "obj": {"x": "1", "y": None},
                "n": 3,
            },
        )
        == "Cap GBP 100, 5 names A, B obj 1 n 3"
    )


def test_officer_details_hash_and_address() -> None:
    """Hashes change with details and addresses render on one line."""
    base = load_fixture("company_active/officers")["items"][0]
    officer = Officer.from_api(base)
    changed = Officer.from_api({**base, "occupation": "Engineer"})
    assert officer.details_hash != changed.details_hash
    assert Officer.from_api(dict(base)).details_hash == officer.details_hash
    assert Address.from_api(None) is None
    assert Address.from_api({}) is None
    assert (
        Address.from_api(
            {"care_of": "Bob", "premises": "1", "address_line_1": "X Road"}
        ).one_line()
        == "c/o Bob, 1 X Road"
    )
