"""Base entity classes for companies, officers and the service device."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVELOPER_HUB_URL, DOMAIN, FIND_AND_UPDATE_BASE, MANUFACTURER
from .coordinator import (
    AccountCoordinator,
    CompaniesHouseCoordinator,
    CompanyRuntime,
    OfficerRuntime,
)
from .enumerations import COMPANY_SUBTYPE, COMPANY_TYPE, OFFICER_ROLE


def company_device_info(company: CompanyRuntime) -> DeviceInfo:
    """Return the device info for a company, owned by its subentry."""
    profile = company.profile.data
    return DeviceInfo(
        identifiers={(DOMAIN, f"company_{company.company_number}")},
        name=company.company_name,
        manufacturer=MANUFACTURER,
        model=COMPANY_TYPE.get(profile.type, profile.type)
        if profile and profile.type
        else None,
        model_id=COMPANY_SUBTYPE.get(profile.subtype, profile.subtype)
        if profile and profile.subtype
        else None,
        serial_number=company.company_number,
        via_device_id=company.entry.runtime_data.service_device_id,
        configuration_url=f"{FIND_AND_UPDATE_BASE}/company/{company.company_number}",
    )


def officer_device_info(officer: OfficerRuntime) -> DeviceInfo:
    """Return the device info for an officer, owned by its subentry."""
    appointments = officer.appointments.data
    role = None
    if appointments is not None:
        active = appointments.active or appointments.items
        if active and active[0].officer_role:
            role = OFFICER_ROLE.get(active[0].officer_role, active[0].officer_role)
    return DeviceInfo(
        identifiers={(DOMAIN, f"officer_{officer.officer_id}")},
        name=officer.officer_name,
        manufacturer=MANUFACTURER,
        model=role,
        serial_number=officer.officer_id,
        via_device_id=officer.entry.runtime_data.service_device_id,
        configuration_url=f"{FIND_AND_UPDATE_BASE}/officers/{officer.officer_id}/appointments",
    )


def service_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the device info of the account's service device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or MANUFACTURER,
        manufacturer=MANUFACTURER,
        model="Public Data API",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=DEVELOPER_HUB_URL,
    )


class CompanyEntity[CoordinatorT: CompaniesHouseCoordinator[Any]](
    CoordinatorEntity[CoordinatorT]
):
    """An entity on a company device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        company: CompanyRuntime,
        coordinator: CoordinatorT,
        description: EntityDescription,
    ) -> None:
        """Bind to the company's coordinator for the description's dataset."""
        super().__init__(coordinator)
        self.company = company
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{DOMAIN}_{company.company_number}_{description.key}"
        self._attr_device_info = company_device_info(company)

    @property
    def available(self) -> bool:
        """Unavailable until the dataset has been fetched at least once."""
        return super().available and self.coordinator.data is not None


class OfficerEntity[CoordinatorT: CompaniesHouseCoordinator[Any]](
    CoordinatorEntity[CoordinatorT]
):
    """An entity on an officer device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        officer: OfficerRuntime,
        coordinator: CoordinatorT,
        description: EntityDescription,
    ) -> None:
        """Bind to the officer's coordinator."""
        super().__init__(coordinator)
        self.officer = officer
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = (
            f"{DOMAIN}_officer_{officer.officer_id}_{description.key}"
        )
        self._attr_device_info = officer_device_info(officer)

    @property
    def available(self) -> bool:
        """Unavailable until the dataset has been fetched at least once."""
        return super().available and self.coordinator.data is not None


class ServiceEntity(CoordinatorEntity[AccountCoordinator]):
    """An entity on the service device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: AccountCoordinator,
        description: EntityDescription,
    ) -> None:
        """Bind to the account coordinator."""
        super().__init__(coordinator)
        self.entry = entry
        self.entity_description = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{description.key}"
        self._attr_device_info = service_device_info(entry)
