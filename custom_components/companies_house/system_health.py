"""System health for Companies House."""

from __future__ import annotations

from typing import Any

from homeassistant.components import system_health
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import API_BASE, DOMAIN


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    """Register the system health callback."""
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Report API reachability and the remaining rate budget."""
    info: dict[str, Any] = {
        "can_reach_server": system_health.async_check_can_reach_url(hass, API_BASE),
    }
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        runtime = entry.runtime_data
        status = runtime.client.limiter.status()
        info.update(
            {
                "requests_remaining": status.remaining,
                "requests_used": status.used,
                "budget_used_percent": status.percent_used,
                "window_resets_at": status.reset_at.isoformat()
                if status.reset_at
                else None,
                "companies_monitored": len(runtime.companies),
                "officers_monitored": len(runtime.officers),
                "last_error": runtime.client.last_error,
            }
        )
        break
    return info
