"""Tests for diagnostics and system health."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
    get_diagnostics_for_device,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.companies_house.const import DOMAIN
from custom_components.companies_house.system_health import system_health_info

from .conftest import TEST_API_KEY


async def test_config_entry_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[..., Any],
) -> None:
    """The entry diagnostics redact the key and dates of birth."""
    entry = await setup_entry(["12345678", "34567890"], officers=True)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["entry"]["data"]["api_key"] == "**REDACTED**"
    assert TEST_API_KEY not in str(diagnostics)
    officer_subentry = next(
        s for s in diagnostics["entry"]["subentries"] if s["subentry_type"] == "officer"
    )
    assert officer_subentry["data"]["date_of_birth_year"] == "**REDACTED**"
    assert diagnostics["rate_limit"]["limit"] == 600
    assert diagnostics["client"]["max_pages"] == 10
    companies = {c["company_number"]: c for c in diagnostics["companies"]}
    assert companies["12345678"]["tier"] == "quiet"
    assert companies["34567890"]["tier"] == "deadline"
    assert companies["12345678"]["coordinators"]["profile"]["last_reason"] == "fetched"
    assert companies["12345678"]["state"]["filings_total_count"] == 6
    assert "data" not in companies["12345678"]
    assert diagnostics["officers"][0]["officer_id"] == "officer-jane"


async def test_device_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[..., Any],
) -> None:
    """Device diagnostics carry the snapshot data, redacted."""
    entry = await setup_entry(["12345678"], officers=True)
    registry = dr.async_get(hass)
    company = registry.async_get_device_by_identifier(
        (DOMAIN, "company_12345678"), entry.entry_id
    )
    assert company is not None
    diagnostics = await get_diagnostics_for_device(hass, hass_client, entry, company)
    assert diagnostics["company_number"] == "12345678"
    assert diagnostics["data"]["profile"]["company_name"] == "EXAMPLE TRADING LIMITED"
    assert (
        diagnostics["data"]["officers"]["items"][0]["date_of_birth"] == "**REDACTED**"
    )
    officer = registry.async_get_device_by_identifier(
        (DOMAIN, "officer_officer-jane"), entry.entry_id
    )
    assert officer is not None
    diagnostics = await get_diagnostics_for_device(hass, hass_client, entry, officer)
    assert diagnostics["officer_id"] == "officer-jane"
    assert diagnostics["data"]["appointments"]["date_of_birth"] == "**REDACTED**"
    assert (
        diagnostics["data"]["appointments"]["items"][0]["company_number"] == "10000001"
    )
    service = registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert service is not None
    diagnostics = await get_diagnostics_for_device(hass, hass_client, entry, service)
    assert "companies" in diagnostics


async def test_system_health(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """System health reports reachability and the remaining budget."""
    assert await async_setup_component(hass, "system_health", {})
    with patch(
        "custom_components.companies_house.system_health.system_health.async_check_can_reach_url",
        new=Mock(return_value="ok"),
    ):
        info = await system_health_info(hass)
        assert info == {"can_reach_server": "ok"}
        await setup_entry(["12345678"])
        info = await system_health_info(hass)
    assert info["companies_monitored"] == 1
    assert info["officers_monitored"] == 0
    assert info["requests_remaining"] < 600
    assert info["requests_used"] > 0
    assert info["window_resets_at"] is not None
