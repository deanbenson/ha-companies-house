"""Diagnostics for the config entry and its devices."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_API_KEY,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    DOMAIN,
)
from .coordinator import (
    CompaniesHouseConfigEntry,
    CompaniesHouseCoordinator,
    CompanyRuntime,
    OfficerRuntime,
)

TO_REDACT = {
    CONF_API_KEY,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    "date_of_birth",
    "address",
    "principal_office_address",
    "usual_residential_address",
    "unique_id",
}


def _coordinator_info(coordinator: CompaniesHouseCoordinator[Any]) -> dict[str, Any]:
    return {
        "last_update_success": coordinator.last_update_success,
        "last_exception": str(coordinator.last_exception)
        if coordinator.last_exception
        else None,
        "fetched_at": coordinator.fetched_at.isoformat()
        if coordinator.fetched_at
        else None,
        "next_run": coordinator.next_run.isoformat() if coordinator.next_run else None,
        "last_reason": coordinator.last_reason,
        "failing_since": coordinator.failing_since.isoformat()
        if coordinator.failing_since
        else None,
        "has_data": coordinator.data is not None,
    }


def _company_info(company: CompanyRuntime, *, include_data: bool) -> dict[str, Any]:
    info: dict[str, Any] = {
        "company_number": company.company_number,
        "company_name": company.company_name,
        "close_watch": company.close_watch,
        "datasets": sorted(d.value for d in company.datasets),
        "tier": company.tier.value,
        "tier_reason": company.tier_reason,
        "strike_off_proposed": company.strike_off_proposed,
        "risk": company.risk.as_dict() if company.risk is not None else None,
        "state": {
            "filings_total_count": company.state.filings_total_count,
            "newest_transaction_id": company.state.newest_transaction_id,
            "newest_filing_date": company.state.newest_filing_date.isoformat()
            if company.state.newest_filing_date
            else None,
            "seen_transaction_ids": len(company.state.seen_transaction_ids),
            "recent_filings": len(company.state.recent_filings),
            "last_reconciled": company.state.last_reconciled.isoformat()
            if company.state.last_reconciled
            else None,
            "last_structure": company.state.last_structure.isoformat()
            if company.state.last_structure
            else None,
            "not_found": company.state.not_found,
        },
        "coordinators": {
            d.value: _coordinator_info(c) for d, c in company.coordinators.items()
        },
    }
    if include_data:
        info["data"] = {
            d.value: c.data.to_storage() if c.data is not None else None
            for d, c in company.coordinators.items()
        }
    return info


def _officer_info(officer: OfficerRuntime, *, include_data: bool) -> dict[str, Any]:
    info: dict[str, Any] = {
        "officer_id": officer.officer_id,
        "officer_name": officer.officer_name,
        "not_found": officer.state.not_found,
        "coordinators": {
            d.value: _coordinator_info(c) for d, c in officer.coordinators.items()
        },
    }
    if include_data:
        info["data"] = {
            d.value: c.data.to_storage() if c.data is not None else None
            for d, c in officer.coordinators.items()
        }
    return info


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the account."""
    runtime = entry.runtime_data
    status = runtime.client.limiter.status()
    data = {
        "entry": {
            "data": dict(entry.data),
            "options": dict(entry.options),
            "version": f"{entry.version}.{entry.minor_version}",
            "subentries": [s.as_dict() for s in entry.subentries.values()],
        },
        "rate_limit": {
            **{
                k: v.isoformat() if hasattr(v, "isoformat") else v
                for k, v in asdict(status).items()
            },
            "throttle_count": runtime.client.limiter.throttle_count,
            "persistently_throttled": runtime.client.limiter.persistently_throttled(),
        },
        "client": {
            "last_success": runtime.client.last_success.isoformat()
            if runtime.client.last_success
            else None,
            "last_error": runtime.client.last_error,
            "max_pages": runtime.client.max_pages,
        },
        "companies": [
            _company_info(c, include_data=False) for c in runtime.companies.values()
        ],
        "officers": [
            _officer_info(o, include_data=False) for o in runtime.officers.values()
        ],
    }
    return async_redact_data(data, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for one company or officer device."""
    runtime = entry.runtime_data
    identifiers = {i[1] for i in device.identifiers if i[0] == DOMAIN}
    for company in runtime.companies.values():
        if f"company_{company.company_number}" in identifiers:
            return async_redact_data(
                _company_info(company, include_data=True), TO_REDACT
            )
    for officer in runtime.officers.values():
        if f"officer_{officer.officer_id}" in identifiers:
            return async_redact_data(
                _officer_info(officer, include_data=True), TO_REDACT
            )
    return await async_get_config_entry_diagnostics(hass, entry)
