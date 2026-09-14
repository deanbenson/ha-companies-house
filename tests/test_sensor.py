"""Targeted sensor and binary sensor tests beyond the snapshots."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.models import CompanyProfile

from .conftest import load_fixture, mock_company


async def test_profile_edge_values(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Missing address, creation date and filings leave sensors unknown, not broken."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    profile = load_fixture("company_active/profile")
    profile.pop("registered_office_address")
    profile.pop("date_of_creation")
    profile["company_status"] = "made-up-status"
    profile["type"] = "made-up-type"
    profile["jurisdiction"] = "mars"
    history = {"items": [], "total_count": 0}
    aioclient_mock.clear_requests()
    entry = await setup_entry()
    from .conftest import company_subentry

    mock_company(
        aioclient_mock,
        "12345678",
        overrides={"profile": profile, "filing_history": history},
    )
    hass.config_entries.async_add_subentry(
        entry, _subentry_obj(company_subentry("12345678", subentry_id="sub_12345678"))
    )
    await hass.async_block_till_done()
    company = entry.runtime_data.companies["sub_12345678"]
    assert company.profile.data is not None
    assert (
        hass.states.get(
            "sensor.example_trading_limited_registered_office_address"
        ).state
        == "unknown"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_company_status").state
        == "unknown"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_company_type").state
        == "unknown"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_jurisdiction").state
        == "unknown"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_last_filing_date").state
        == "unknown"
    )
    assert (
        hass.states.get("sensor.example_trading_limited_last_filing").state == "unknown"
    )
    attrs = hass.states.get("sensor.example_trading_limited_last_filing").attributes
    assert "transaction_id" not in attrs
    from custom_components.companies_house.sensor import _company_age

    assert _company_age(company) is None
    company.profile.data = CompanyProfile.from_api(
        {**profile, "date_of_creation": "2020-01-01", "date_of_cessation": "2022-01-01"}
    )
    assert _company_age(company) == 2.0


def _subentry_obj(data: Any) -> Any:
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    return ConfigSubentry(
        data=MappingProxyType(dict(data["data"])),
        subentry_type=data["subentry_type"],
        title=data["title"],
        unique_id=data["unique_id"],
        subentry_id=data["subentry_id"],
    )


async def test_long_registered_office_is_truncated(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A registered office over 255 characters fits the state limit."""
    profile = load_fixture("company_active/profile")
    profile["registered_office_address"]["address_line_1"] = "X" * 300
    entry = await setup_entry()
    from .conftest import company_subentry

    mock_company(aioclient_mock, "12345678", overrides={"profile": profile})
    hass.config_entries.async_add_subentry(
        entry, _subentry_obj(company_subentry("12345678", subentry_id="sub_12345678"))
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.example_trading_limited_registered_office_address")
    assert state is not None
    assert len(state.state) == 255
    assert state.state.endswith("…")


async def test_charge_sensors_list_the_charges(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Charge counts carry the charges themselves, newest first, out of the recorder."""
    await setup_entry(["12345678"])
    outstanding = hass.states.get("sensor.example_trading_limited_outstanding_charges")
    assert outstanding is not None
    assert outstanding.state == "1"
    assert outstanding.attributes["lenders"] == ["HSBC UK Bank Plc"]
    (row,) = outstanding.attributes["charges"]
    assert row == {
        "charge_code": "123456780002",
        "lender": "HSBC UK Bank Plc",
        "status": "outstanding",
        "created_on": "2022-05-06",
        "satisfied_on": None,
        "secured": "All monies due",
        "particulars": "",
        "kind": "fixed and floating charge over all the company's assets",
        "link": (
            "https://find-and-update.company-information.service.gov.uk/company/"
            "12345678/filing-history/MzM0MDAwMDAwMGFkaXF6a2N4/document?format=pdf&download=0"
        ),
    }
    total = hass.states.get("sensor.example_trading_limited_charges_total")
    assert total is not None
    assert total.state == "2"
    assert "lenders" not in total.attributes
    rows = total.attributes["charges"]
    assert [r["status"] for r in rows] == ["outstanding", "fully-satisfied"]
    assert rows[1]["lender"] == "Lloyds Bank Plc"
    assert rows[1]["satisfied_on"] == "2021-11-30"
    assert rows[1]["kind"] == "fixed and floating charge"
    assert rows[1]["particulars"].startswith("The freehold property known as Unit 4")
    assert rows[1]["particulars"].endswith("…")
    # The satisfied charge links to the filing that satisfied it (the MR04).
    assert "/MzMxMDAwMDAwMGFkaXF6a2N4/document" in rows[1]["link"]
    # The lists never reach the recorder; the counts and everything else do.
    for state in (outstanding, total):
        assert state.state_info is not None
        assert {"charges", "lenders"} <= state.state_info["unrecorded_attributes"]
    plain = hass.states.get("sensor.example_trading_limited_charges_satisfied")
    assert plain is not None
    assert plain.state_info is not None
    assert "charges" not in plain.state_info["unrecorded_attributes"]
