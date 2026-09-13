"""The Companies House integration."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import LOGGER

PLATFORMS: list[Platform] = []


@dataclass
class CompaniesHouseRuntimeData:
    """Runtime data stored on the config entry."""

    companies: dict[str, object] = field(default_factory=dict)
    officers: dict[str, object] = field(default_factory=dict)


type CompaniesHouseConfigEntry = ConfigEntry[CompaniesHouseRuntimeData]


async def async_setup_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> bool:
    """Set up Companies House from a config entry."""
    entry.runtime_data = CompaniesHouseRuntimeData()
    LOGGER.debug("Set up Companies House entry %s", entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
