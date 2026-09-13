"""Repair issues and their fix flows.

Issues raised:

* ``rate_limited`` when 429s persist past three windows; the fix raises the
  cadence multiplier by one.
* ``company_dissolved_<number>`` when a monitored company is dissolved or
  removed; the fix stops monitoring it.
* ``company_not_found_<number>`` when the profile returns 404; same fix.
* ``officer_not_found_<id>`` when the appointments return 404; same fix.

A rejected API key is handled by Home Assistant's own reauthentication
repair, started by the coordinators raising ``ConfigEntryAuthFailed``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .const import (
    CONF_CADENCE_MULTIPLIER,
    DEFAULT_CADENCE_MULTIPLIER,
    DOMAIN,
    MAX_CADENCE_MULTIPLIER,
)

ISSUE_RATE_LIMITED = "rate_limited"


@callback
def async_raise_rate_limited(hass: HomeAssistant, entry_id: str) -> None:
    """Raise the persistent throttling issue."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_RATE_LIMITED}_{entry_id}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_RATE_LIMITED,
        data={"entry_id": entry_id},
    )


@callback
def async_clear_rate_limited(hass: HomeAssistant, entry_id: str) -> None:
    """Clear the throttling issue."""
    ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_RATE_LIMITED}_{entry_id}")


@callback
def async_raise_subentry_issue(
    hass: HomeAssistant,
    *,
    key: str,
    entry_id: str,
    subentry_id: str,
    placeholders: dict[str, str],
) -> None:
    """Raise an issue whose fix removes a subentry."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{key}_{placeholders.get('number') or placeholders.get('officer_id')}",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders=placeholders,
        data={"entry_id": entry_id, "subentry_id": subentry_id, **placeholders},
    )


@callback
def async_clear_subentry_issue(hass: HomeAssistant, key: str, identifier: str) -> None:
    """Clear a subentry issue."""
    ir.async_delete_issue(hass, DOMAIN, f"{key}_{identifier}")


class RemoveSubentryRepairFlow(RepairsFlow):
    """Confirm, then stop monitoring the company or officer."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Keep the issue data."""
        self._data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Go straight to confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Remove the subentry on confirmation."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(
                str(self._data["entry_id"])
            )
            subentry_id = str(self._data["subentry_id"])
            if entry is not None and subentry_id in entry.subentries:
                self.hass.config_entries.async_remove_subentry(entry, subentry_id)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={k: str(v) for k, v in self._data.items()},
        )


class RateLimitRepairFlow(RepairsFlow):
    """Confirm, then slow the cadence down by one."""

    def __init__(self, data: dict[str, Any]) -> None:
        """Keep the issue data."""
        self._data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Go straight to confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        """Raise the multiplier on confirmation."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(
                str(self._data["entry_id"])
            )
            if entry is not None:
                current = float(
                    entry.options.get(
                        CONF_CADENCE_MULTIPLIER, DEFAULT_CADENCE_MULTIPLIER
                    )
                )
                self.hass.config_entries.async_update_entry(
                    entry,
                    options={
                        **entry.options,
                        CONF_CADENCE_MULTIPLIER: min(
                            current + 1, MAX_CADENCE_MULTIPLIER
                        ),
                    },
                )
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Return the fix flow for an issue."""
    payload: dict[str, Any] = dict(data or {})
    if issue_id.startswith(ISSUE_RATE_LIMITED):
        return RateLimitRepairFlow(payload)
    return RemoveSubentryRepairFlow(payload)
