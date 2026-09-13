"""Actions. Registered in async_setup so they exist without a config entry."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions. Filled in with the action set."""
