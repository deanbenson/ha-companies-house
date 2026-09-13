"""Shared fixtures for the Companies House tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.companies_house.const import CONF_API_KEY, DOMAIN

TEST_API_KEY = "test-api-key-0000000000000000000000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for every test."""
    return


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the account."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Companies House",
        data={CONF_API_KEY: TEST_API_KEY},
        unique_id="hashed-key",
        version=1,
        minor_version=1,
    )


FIXTURES = Path(__file__).parent / "fixtures"


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
