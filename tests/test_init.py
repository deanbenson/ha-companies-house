"""Test the Companies House integration setup."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.const import API_BASE, DOMAIN


async def test_setup_and_unload_without_subentries(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An account with nothing monitored validates the key, sets up and unloads."""
    entry = await setup_entry()
    assert entry.state is ConfigEntryState.LOADED
    assert aioclient_mock.call_count == 1
    device_registry = dr.async_get(hass)
    service = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert service is not None
    assert service.name == "Companies House"
    assert hass.states.get("sensor.companies_house_companies_monitored").state == "0"
    assert hass.states.get("calendar.companies_house_all_deadlines") is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_with_companies_and_officer(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Companies and officers get devices, owned by their subentries, via the service device."""
    entry = await setup_entry(["12345678", "23456789"], officers=True)
    assert entry.state is ConfigEntryState.LOADED
    device_registry = dr.async_get(hass)
    company = device_registry.async_get_device_by_identifier(
        (DOMAIN, "company_12345678"), entry.entry_id
    )
    assert company is not None
    assert company.name == "EXAMPLE TRADING LIMITED"
    assert company.model == "Private limited company"
    assert company.serial_number == "12345678"
    assert company.config_entries_subentries == {entry.entry_id: {"sub_12345678"}}
    service = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert service is not None
    assert company.via_device_id == service.id
    officer = device_registry.async_get_device_by_identifier(
        (DOMAIN, "officer_officer-jane"), entry.entry_id
    )
    assert officer is not None
    assert officer.name == "Jane Elizabeth SMITH"
    assert officer.config_entries_subentries == {entry.entry_id: {"sub_officer"}}
    assert hass.states.get("sensor.companies_house_companies_monitored").state == "2"
    assert hass.states.get("sensor.companies_house_officers_monitored").state == "1"
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "active"
    )
    assert (
        hass.states.get("sensor.old_ventures_limited_company_status").state
        == "dissolved"
    )
    assert (
        hass.states.get("sensor.jane_elizabeth_smith_appointments_active").state == "18"
    )


async def test_remove_subentry_removes_only_its_device(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Deleting a subentry removes exactly its device and entities."""
    entry = await setup_entry(["12345678", "23456789"])
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    before = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    removed_entities = er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    )
    removed_entities = [
        e for e in removed_entities if e.config_subentry_id == "sub_23456789"
    ]
    assert removed_entities

    hass.config_entries.async_remove_subentry(entry, "sub_23456789")
    await hass.async_block_till_done()

    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, "company_23456789"), entry.entry_id
        )
        is None
    )
    assert (
        device_registry.async_get_device_by_identifier(
            (DOMAIN, "company_12345678"), entry.entry_id
        )
        is not None
    )
    after = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(after) == before - len(removed_entities)
    assert all(e.config_subentry_id != "sub_23456789" for e in after)
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_without_subentries_key_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """With nothing monitored the key is tested; failures map to auth or not ready."""
    from .conftest import make_entry

    aioclient_mock.get(f"{API_BASE}/search/companies", status=401)
    entry = make_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API_BASE}/search/companies", status=500)
    other = MockConfigEntry(
        domain=DOMAIN, data={"api_key": "k2"}, unique_id="other", entry_id="entry2"
    )
    other.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    assert other.state is ConfigEntryState.SETUP_RETRY


async def test_officer_auth_failure_is_raised(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 on an officer coordinator also puts the entry into auth failure."""
    from .conftest import make_entry, officer_subentry

    aioclient_mock.get(f"{API_BASE}/officers/officer-jane/appointments", status=401)
    aioclient_mock.get(f"{API_BASE}/search/disqualified-officers", status=401)
    entry = make_entry([officer_subentry()])
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_store_pruned_and_removed(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    hass_storage: dict[str, Any],
) -> None:
    """State for removed subentries is pruned on reload; removal deletes the store."""
    entry = await setup_entry(["12345678", "23456789"])
    store = entry.runtime_data.store
    assert set(store.companies) == {"12345678", "23456789"}
    hass.config_entries.async_remove_subentry(entry, "sub_23456789")
    await hass.async_block_till_done()
    assert set(entry.runtime_data.store.companies) == {"12345678"}
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


async def test_remove_device_only_when_stale(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """Live devices cannot be removed; a stale device can."""
    from custom_components.companies_house import async_remove_config_entry_device

    entry = await setup_entry(["12345678"], officers=True)
    registry = dr.async_get(hass)
    company = registry.async_get_device_by_identifier(
        (DOMAIN, "company_12345678"), entry.entry_id
    )
    officer = registry.async_get_device_by_identifier(
        (DOMAIN, "officer_officer-jane"), entry.entry_id
    )
    service = registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert company is not None
    assert officer is not None
    assert service is not None
    assert not await async_remove_config_entry_device(hass, entry, company)
    assert not await async_remove_config_entry_device(hass, entry, officer)
    assert not await async_remove_config_entry_device(hass, entry, service)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "company_00000000")}
    )
    assert await async_remove_config_entry_device(hass, entry, stale)


async def test_migrate_entry(hass: HomeAssistant) -> None:
    """Version 1 entries need no migration; a future version is refused."""
    from custom_components.companies_house import async_migrate_entry

    from .conftest import make_entry

    entry = make_entry()
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    future = MockConfigEntry(
        domain=DOMAIN, data={"api_key": "k"}, version=2, entry_id="e2"
    )
    future.add_to_hass(hass)
    assert not await async_migrate_entry(hass, future)
