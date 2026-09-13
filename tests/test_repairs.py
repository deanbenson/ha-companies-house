"""Tests for the repair issues and their fix flows."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from http import HTTPStatus
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    TestClient,
)

from custom_components.companies_house.const import (
    API_BASE,
    CONF_CADENCE_MULTIPLIER,
    DOMAIN,
)
from custom_components.companies_house.repairs import (
    RateLimitRepairFlow,
    RemoveSubentryRepairFlow,
    async_create_fix_flow,
)

from .conftest import load_fixture, mock_company


async def start_repair_fix_flow(
    client: TestClient, handler: str, issue_id: str
) -> dict[str, Any]:
    """Start a fix flow through the repairs API."""
    resp = await client.post(
        "/api/repairs/issues/fix", json={"handler": handler, "issue_id": issue_id}
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    result: dict[str, Any] = await resp.json()
    return result


async def process_repair_fix_flow(
    client: TestClient, flow_id: str, json: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Advance a fix flow through the repairs API."""
    resp = await client.post(f"/api/repairs/issues/fix/{flow_id}", json=json)
    assert resp.status == HTTPStatus.OK
    result: dict[str, Any] = await resp.json()
    return result


async def _fix(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, issue_id: str
) -> None:
    client = await hass_client()
    result = await start_repair_fix_flow(client, DOMAIN, issue_id)
    assert result["step_id"] == "confirm"
    result = await process_repair_fix_flow(client, result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_dissolved_company_issue_and_fix(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[..., Any],
) -> None:
    """A dissolved company raises an issue whose fix removes the subentry."""
    entry = await setup_entry(["12345678", "23456789"])
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, "company_dissolved_23456789")
    assert issue is not None
    assert issue.translation_placeholders == {
        "number": "23456789",
        "company": "OLD VENTURES LIMITED",
    }
    assert registry.async_get_issue(DOMAIN, "company_dissolved_12345678") is None
    await _fix(hass, hass_client, "company_dissolved_23456789")
    assert "sub_23456789" not in entry.subentries
    assert registry.async_get_issue(DOMAIN, "company_dissolved_23456789") is None
    assert (
        dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, "company_23456789"), entry.entry_id
        )
        is None
    )
    assert "sub_12345678" in entry.subentries


async def test_not_found_issues_clear_on_recovery(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """404s raise issues; a later success clears them; the fix removes the subentry."""
    entry = await setup_entry(["12345678"], officers=True)
    company = entry.runtime_data.companies["sub_12345678"]
    officer = entry.runtime_data.officers["sub_officer"]
    registry = ir.async_get(hass)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/company/12345678", status=404)
    aioclient_mock.get(f"{API_BASE}/officers/officer-jane/appointments", status=404)
    await company.profile.async_refresh()
    await officer.appointments.async_refresh()
    assert registry.async_get_issue(DOMAIN, "company_not_found_12345678") is not None
    assert (
        registry.async_get_issue(DOMAIN, "officer_not_found_officer-jane") is not None
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, "12345678")
    await company.profile.async_refresh()
    assert registry.async_get_issue(DOMAIN, "company_not_found_12345678") is None
    assert not company.state.not_found
    await _fix(hass, hass_client, "officer_not_found_officer-jane")
    assert "sub_officer" not in entry.subentries


async def test_fix_flows_tolerate_missing_entries(hass: HomeAssistant) -> None:
    """The fix flows do nothing harmful when the entry or subentry is gone."""
    flow = RemoveSubentryRepairFlow({"entry_id": "gone", "subentry_id": "gone"})
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] == "form"
    result = await flow.async_step_confirm({})
    assert result["type"] == "create_entry"
    rate = RateLimitRepairFlow({"entry_id": "gone"})
    rate.hass = hass
    result = await rate.async_step_init()
    assert result["type"] == "form"
    result = await rate.async_step_confirm({})
    assert result["type"] == "create_entry"
    assert isinstance(
        await async_create_fix_flow(hass, "rate_limited_x", None), RateLimitRepairFlow
    )
    assert isinstance(
        await async_create_fix_flow(hass, "company_dissolved_1", {"entry_id": "e"}),
        RemoveSubentryRepairFlow,
    )


async def test_rate_limit_issue_and_fix(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_entry: Callable[..., Any],
    freezer: FrozenDateTimeFactory,
) -> None:
    """Persistent throttling raises an issue; the fix raises the cadence multiplier."""
    entry = await setup_entry(["12345678"])
    limiter = entry.runtime_data.client.limiter
    registry = ir.async_get(hass)
    limiter.note_rate_limited(None)
    freezer.tick(timedelta(minutes=16))
    limiter.note_rate_limited(None)
    await entry.runtime_data.account.async_refresh()
    issue_id = f"rate_limited_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None
    await _fix(hass, hass_client, issue_id)
    assert entry.options[CONF_CADENCE_MULTIPLIER] == 2.0
    # Once throttling stops the issue is cleared on the next account refresh.
    entry.runtime_data.client.limiter.note_success()
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    await entry.runtime_data.account.async_refresh()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_restored_company_clears_dissolved_issue(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A company restored to the register clears its dissolved issue."""
    entry = await setup_entry(["23456789"])
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, "company_dissolved_23456789") is not None
    profile = load_fixture("company_dissolved/profile")
    profile["company_status"] = "active"
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, "23456789", overrides={"profile": profile})
    await entry.runtime_data.companies["sub_23456789"].profile.async_refresh()
    assert registry.async_get_issue(DOMAIN, "company_dissolved_23456789") is None
