"""Switches: per company and person, how they are reported and alerted on."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_IN_WEEKLY_REPORT,
    CONF_NOTIFY_INSTANTLY,
    CONF_WATCH_COMPANIES,
    Dataset,
    OfficerDataset,
)
from .coordinator import (
    CompaniesHouseConfigEntry,
    CompanyRuntime,
    OfficerRuntime,
    signal_new_company,
    signal_new_officer,
)
from .entity import CompanyEntity, OfficerEntity

PARALLEL_UPDATES = 0

# Each switch is one setting saved on the company's or person's subentry, so it
# survives restarts and can equally be changed from their settings dialog.
NOTIFY_INSTANTLY = SwitchEntityDescription(
    key=CONF_NOTIFY_INSTANTLY, entity_category=EntityCategory.CONFIG
)
IN_WEEKLY_REPORT = SwitchEntityDescription(
    key=CONF_IN_WEEKLY_REPORT, entity_category=EntityCategory.CONFIG
)
WATCH_COMPANIES = SwitchEntityDescription(
    key=CONF_WATCH_COMPANIES, entity_category=EntityCategory.CONFIG
)


class _SettingSwitch(SwitchEntity):
    """A switch backed by one boolean in a subentry's data."""

    entity_description: SwitchEntityDescription
    runtime: CompanyRuntime | OfficerRuntime

    @property
    def is_on(self) -> bool:
        """Return the setting as the runtime currently holds it."""
        return bool(getattr(self.runtime, self.entity_description.key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the setting off."""
        await self._async_set(False)

    async def _async_set(self, value: bool) -> None:
        setattr(self.runtime, self.entity_description.key, value)
        self.async_write_ha_state()
        self.hass.config_entries.async_update_subentry(
            self.runtime.entry,
            self.runtime.subentry,
            data={**self.runtime.subentry.data, self.entity_description.key: value},
        )
        if value and self.entity_description.key == CONF_WATCH_COMPANIES:
            assert isinstance(self.runtime, OfficerRuntime)
            await self.runtime.async_watch_companies()


class CompanySettingSwitch(CompanyEntity[Any], _SettingSwitch):
    """Notify instantly / in weekly report, on a company."""

    def __init__(
        self, company: CompanyRuntime, description: SwitchEntityDescription
    ) -> None:
        """Bind to the profile, which every company has."""
        super().__init__(company, company.coordinators[Dataset.PROFILE], description)
        self.runtime = company


class OfficerSettingSwitch(OfficerEntity[Any], _SettingSwitch):
    """Notify instantly / in weekly report / watch their companies, on a person."""

    def __init__(
        self, officer: OfficerRuntime, description: SwitchEntityDescription
    ) -> None:
        """Bind to the appointments."""
        super().__init__(
            officer, officer.coordinators[OfficerDataset.APPOINTMENTS], description
        )
        self.runtime = officer


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches, and add more as companies and officers are added."""
    runtime = entry.runtime_data

    @callback
    def _add_company(company: CompanyRuntime) -> None:
        async_add_entities(
            [
                CompanySettingSwitch(company, NOTIFY_INSTANTLY),
                CompanySettingSwitch(company, IN_WEEKLY_REPORT),
            ],
            config_subentry_id=company.subentry.subentry_id,
        )

    @callback
    def _add_officer(officer: OfficerRuntime) -> None:
        async_add_entities(
            [
                OfficerSettingSwitch(officer, WATCH_COMPANIES),
                OfficerSettingSwitch(officer, NOTIFY_INSTANTLY),
                OfficerSettingSwitch(officer, IN_WEEKLY_REPORT),
            ],
            config_subentry_id=officer.subentry.subentry_id,
        )

    for company in runtime.companies.values():
        _add_company(company)
    for officer in runtime.officers.values():
        _add_officer(officer)
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_company(entry.entry_id), _add_company)
    )
    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_officer(entry.entry_id), _add_officer)
    )
