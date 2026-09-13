"""Snapshot every entity for every fixture company and the officer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import snapshot_platform
from syrupy.assertion import SnapshotAssertion

from .conftest import COMPANY_FIXTURES

ALL_COMPANIES = list(COMPANY_FIXTURES)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "platform",
    [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.EVENT, Platform.CALENDAR],
)
async def test_entities(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    entity_registry: er.EntityRegistry,
    snapshot: SnapshotAssertion,
    freezer: FrozenDateTimeFactory,
    platform: Platform,
) -> None:
    """Every entity's registry entry, state and attributes match the snapshot."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    with patch("custom_components.companies_house.PLATFORMS", [platform]):
        entry = await setup_entry(
            ALL_COMPANIES, officers=True, close_watch={"12345678"}
        )
    await snapshot_platform(hass, entity_registry, snapshot, entry.entry_id)
