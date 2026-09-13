"""Shared fixtures for the Companies House tests."""

from __future__ import annotations

from collections.abc import Callable, Generator
import json
from pathlib import Path
import random
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigSubentryData, ConfigSubentryDataWithId
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from syrupy.assertion import SnapshotAssertion

from custom_components.companies_house.const import (
    API_BASE,
    CONF_API_KEY,
    CONF_CLOSE_WATCH,
    CONF_COMPANY_NAME,
    CONF_COMPANY_NUMBER,
    CONF_DATASETS,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    CONF_LABEL,
    CONF_OFFICER_ID,
    CONF_OFFICER_NAME,
    DOMAIN,
    OPTIONAL_DATASETS,
    SUBENTRY_TYPE_COMPANY,
    SUBENTRY_TYPE_OFFICER,
)

TEST_API_KEY = "test-api-key-0000000000000000000000"
FIXTURES = Path(__file__).parent / "fixtures"

COMPANY_FIXTURES: dict[str, str] = {
    "12345678": "company_active",
    "23456789": "company_dissolved",
    "34567890": "company_liquidation",
    "45678901": "company_strike_off",
    "OC123456": "company_llp",
    "56789012": "company_corporate_psc",
}
COMPANY_ENDPOINTS: dict[str, str] = {
    "profile": "",
    "filing_history": "/filing-history",
    "officers": "/officers",
    "psc": "/persons-with-significant-control",
    "psc_statements": "/persons-with-significant-control-statements",
    "charges": "/charges",
    "insolvency": "/insolvency",
    "registers": "/registers",
    "exemptions": "/exemptions",
    "uk_establishments": "/uk-establishments",
}
OFFICER_ID = "officer-jane"


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by relative path, e.g. ``company_active/profile``."""
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


class FakeClock:
    """A controllable clock whose sleep advances time instantly."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        """Start the clock."""
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Advance time instead of waiting."""
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    """Return a fake clock."""
    return FakeClock()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for every test."""
    return


@pytest.fixture(autouse=True)
def deterministic_jitter() -> Generator[None]:
    """Seed the jitter so scheduled times are reproducible in snapshots."""
    with patch(
        "custom_components.companies_house.scheduler.random.SystemRandom",
        lambda: random.Random(0),
    ):
        yield


@pytest.fixture(autouse=True)
def no_pacing() -> Generator[None]:
    """Switch off the 2 requests per second pacing so tests do not sleep."""
    with patch("custom_components.companies_house.api.RATE_MAX_PER_SECOND", 0):
        yield


def mock_company(
    aioclient_mock: AiohttpClientMocker,
    company_number: str,
    fixture: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> None:
    """Register every company endpoint from a fixture directory; 404 where absent."""
    fixture = fixture or COMPANY_FIXTURES[company_number]
    base = f"{API_BASE}/company/{company_number}"
    for key, path in COMPANY_ENDPOINTS.items():
        if overrides and key in overrides:
            payload = overrides[key]
            if payload is None:
                aioclient_mock.get(base + path, status=404)
            else:
                aioclient_mock.get(base + path, json=payload)
            continue
        file = FIXTURES / fixture / f"{key}.json"
        if file.exists():
            aioclient_mock.get(base + path, json=json.loads(file.read_text()))
        else:
            aioclient_mock.get(base + path, status=404)


def mock_officer(
    aioclient_mock: AiohttpClientMocker,
    officer_id: str = OFFICER_ID,
    *,
    appointments: dict[str, Any] | None = None,
    disqualified_search: dict[str, Any] | None = None,
) -> None:
    """Register the officer endpoints."""
    aioclient_mock.get(
        f"{API_BASE}/officers/{officer_id}/appointments",
        json=appointments or load_fixture("officer_many/appointments"),
    )
    aioclient_mock.get(
        f"{API_BASE}/search/disqualified-officers",
        json=disqualified_search
        if disqualified_search is not None
        else load_fixture("officer_many/disqualified_search_name_only"),
    )
    aioclient_mock.get(
        f"{API_BASE}/disqualified-officers/natural/dq-exact-jane",
        json=load_fixture("officer_many/disqualification_natural"),
    )


def mock_search(aioclient_mock: AiohttpClientMocker) -> None:
    """Register the search endpoints."""
    aioclient_mock.get(
        f"{API_BASE}/search/companies", json=load_fixture("search/companies")
    )
    aioclient_mock.get(
        f"{API_BASE}/search/officers", json=load_fixture("search/officers")
    )
    aioclient_mock.get(
        f"{API_BASE}/advanced-search/companies",
        json={"items": load_fixture("search/companies")["items"][:1], "hits": 1},
    )


def company_subentry(
    company_number: str,
    *,
    name: str | None = None,
    close_watch: bool = False,
    datasets: list[str] | None = None,
    label: str = "",
    subentry_id: str | None = None,
) -> ConfigSubentryData | ConfigSubentryDataWithId:
    """Return subentry data for a company."""
    name = (
        name
        or load_fixture(f"{COMPANY_FIXTURES[company_number]}/profile")["company_name"]
    )
    data: dict[str, Any] = {
        "data": {
            CONF_COMPANY_NUMBER: company_number,
            CONF_COMPANY_NAME: name,
            CONF_DATASETS: datasets
            if datasets is not None
            else [d.value for d in OPTIONAL_DATASETS],
            CONF_CLOSE_WATCH: close_watch,
            CONF_LABEL: label,
        },
        "subentry_type": SUBENTRY_TYPE_COMPANY,
        "title": name,
        "unique_id": company_number,
    }
    if subentry_id:
        data["subentry_id"] = subentry_id
    return data  # type: ignore[return-value]


def officer_subentry(
    officer_id: str = OFFICER_ID,
    name: str = "Jane Elizabeth SMITH",
    month: int = 6,
    year: int = 1978,
) -> ConfigSubentryData:
    """Return subentry data for an officer."""
    return {
        "data": {
            CONF_OFFICER_ID: officer_id,
            CONF_OFFICER_NAME: name,
            CONF_DATE_OF_BIRTH_MONTH: month,
            CONF_DATE_OF_BIRTH_YEAR: year,
        },
        "subentry_type": SUBENTRY_TYPE_OFFICER,
        "title": name,
        "unique_id": officer_id,
    }


def make_entry(
    subentries: list[ConfigSubentryData] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Return a config entry for the account with the given subentries."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Companies House",
        data={CONF_API_KEY: TEST_API_KEY},
        options=options or {},
        unique_id="hashed-key",
        version=1,
        minor_version=1,
        subentries_data=subentries or [],
        entry_id="entry1",
    )


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an account entry with no subentries."""
    return make_entry()


@pytest.fixture
def validate_ok(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Mock the key validation call."""
    aioclient_mock.get(
        f"{API_BASE}/search/companies?q=test&items_per_page=1",
        json=load_fixture("search/validate"),
        headers={
            "X-Ratelimit-Limit": "600",
            "X-Ratelimit-Remain": "599",
            "X-Ratelimit-Reset": "1900000000",
            "X-Ratelimit-Window": "5m",
        },
    )
    return aioclient_mock


@pytest.fixture
def setup_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> Callable[..., Any]:
    """Return a helper that mocks the API for the subentries and sets up the entry."""

    async def _setup(
        companies: list[str] | None = None,
        officers: bool = False,
        *,
        options: dict[str, Any] | None = None,
        close_watch: set[str] | None = None,
        datasets: dict[str, list[str]] | None = None,
    ) -> MockConfigEntry:
        subentries: list[ConfigSubentryData] = []
        for number in companies or []:
            mock_company(aioclient_mock, number)
            subentries.append(
                company_subentry(
                    number,
                    close_watch=number in (close_watch or set()),
                    datasets=(datasets or {}).get(number),
                    subentry_id=f"sub_{number}",
                )
            )
        if officers:
            mock_officer(aioclient_mock)
            subentries.append({**officer_subentry(), "subentry_id": "sub_officer"})  # type: ignore[typeddict-unknown-key]
        if not subentries:
            aioclient_mock.get(
                f"{API_BASE}/search/companies?q=test&items_per_page=1",
                json=load_fixture("search/validate"),
            )
        await hass.config.async_set_time_zone("Europe/London")
        entry = make_entry(subentries, options)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _setup


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Use the Home Assistant snapshot serialiser and the tests/snapshots folder."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable every entity, including those disabled by default, for snapshots."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        yield
