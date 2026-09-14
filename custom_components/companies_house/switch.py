"""Switches for officers: whether their companies are watched automatically."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_WATCH_COMPANIES, OfficerDataset
from .coordinator import CompaniesHouseConfigEntry, OfficerRuntime, signal_new_officer
from .entity import OfficerEntity

PARALLEL_UPDATES = 0

WATCH_COMPANIES = SwitchEntityDescription(
    key="watch_companies", entity_category=EntityCategory.CONFIG
)


class WatchCompaniesSwitch(OfficerEntity[Any], SwitchEntity):
    """Watch every company the person currently holds a role at.

    The setting lives on the person's subentry, so it survives restarts and
    can equally be changed from the person's settings. Turning it on adds
    their current companies straight away; later ones are added as the
    register shows them.
    """

    entity_description: SwitchEntityDescription

    def __init__(self, officer: OfficerRuntime) -> None:
        """Bind to the appointments, which is what the switch acts on."""
        super().__init__(
            officer, officer.coordinators[OfficerDataset.APPOINTMENTS], WATCH_COMPANIES
        )

    @property
    def is_on(self) -> bool:
        """Return whether the person's companies are added automatically."""
        return self.officer.watch_companies

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start watching their companies."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop adding their companies. Ones already watched stay watched."""
        await self._async_set(False)

    async def _async_set(self, value: bool) -> None:
        self.officer.watch_companies = value
        self.async_write_ha_state()
        self.hass.config_entries.async_update_subentry(
            self.officer.entry,
            self.officer.subentry,
            data={**self.officer.subentry.data, CONF_WATCH_COMPANIES: value},
        )
        if value:
            await self.officer.async_watch_companies()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches, and add more as officers are added."""
    runtime = entry.runtime_data

    @callback
    def _add_officer(officer: OfficerRuntime) -> None:
        async_add_entities(
            [WatchCompaniesSwitch(officer)],
            config_subentry_id=officer.subentry.subentry_id,
        )

    for officer in runtime.officers.values():
        _add_officer(officer)
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_officer(entry.entry_id), _add_officer)
    )
