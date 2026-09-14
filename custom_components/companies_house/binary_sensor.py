"""Binary sensors for companies and officers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DUE_SOON_DAYS,
    DEFAULT_DUE_SOON_DAYS,
    INSOLVENT_STATUSES,
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
from .gazette import countdown_attributes
from .models import CompanyProfile
from .scheduler import days_until

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class CompanyBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a company binary sensor."""

    dataset: Dataset = Dataset.PROFILE
    is_on_fn: Callable[[CompanyRuntime], bool | None]
    attrs_fn: Callable[[CompanyRuntime], Mapping[str, Any]] | None = None


def _profile(company: CompanyRuntime) -> CompanyProfile:
    assert company.profile.data is not None
    return company.profile.data


def _due_soon_days(company: CompanyRuntime) -> int:
    return int(company.entry.options.get(CONF_DUE_SOON_DAYS, DEFAULT_DUE_SOON_DAYS))


def _due_soon(company: CompanyRuntime, kind: str) -> bool | None:
    profile = _profile(company)
    due = (
        profile.accounts.next_due
        if kind == "accounts"
        else profile.confirmation_statement.next_due
    )
    days = days_until(due, dt_util.now().date())
    if days is None:
        return None
    return 0 <= days <= _due_soon_days(company)


def _accounts_overdue(company: CompanyRuntime) -> bool:
    profile = _profile(company)
    days = days_until(profile.accounts.next_due, dt_util.now().date())
    return profile.accounts.next_overdue or (days is not None and days < 0)


def _confirmation_overdue(company: CompanyRuntime) -> bool:
    profile = _profile(company)
    days = days_until(profile.confirmation_statement.next_due, dt_util.now().date())
    return profile.confirmation_statement.overdue or (days is not None and days < 0)


COMPANY_BINARY_SENSORS: tuple[CompanyBinarySensorDescription, ...] = (
    CompanyBinarySensorDescription(
        key="accounts_overdue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_accounts_overdue,
        attrs_fn=lambda c: {
            "due_on": (d := _profile(c).accounts.next_due) and d.isoformat()
        },
    ),
    CompanyBinarySensorDescription(
        key="confirmation_statement_overdue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_confirmation_overdue,
        attrs_fn=lambda c: {
            "due_on": (d := _profile(c).confirmation_statement.next_due)
            and d.isoformat()
        },
    ),
    CompanyBinarySensorDescription(
        key="accounts_due_soon",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: _due_soon(c, "accounts"),
        attrs_fn=lambda c: {"threshold_days": _due_soon_days(c)},
    ),
    CompanyBinarySensorDescription(
        key="confirmation_statement_due_soon",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: _due_soon(c, "confirmation_statement"),
        attrs_fn=lambda c: {"threshold_days": _due_soon_days(c)},
    ),
    CompanyBinarySensorDescription(
        key="proposed_strike_off",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: c.strike_off_proposed,
        attrs_fn=lambda c: {
            "status_detail": _profile(c).company_status_detail,
            "gazette_notice_on": (d := c.state.strike_off_notice_on) and d.isoformat(),
            # Set when a filing ended the strike-off before the register's
            # status detail caught up: the sensor is off although the detail
            # still says "proposal to strike off".
            "discontinued_on": (d := c.state.strike_off_discontinued_on)
            and d.isoformat(),
            **countdown_attributes(
                c.strike_off_countdown(dt_util.now().date()), c.company_number
            ),
        },
    ),
    CompanyBinarySensorDescription(
        key="insolvent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: _profile(c).company_status in INSOLVENT_STATUSES,
        attrs_fn=lambda c: {"status": _profile(c).company_status},
    ),
    CompanyBinarySensorDescription(
        key="registered_office_in_dispute",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: _profile(c).registered_office_is_in_dispute,
    ),
    CompanyBinarySensorDescription(
        key="undeliverable_registered_office",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda c: _profile(c).undeliverable_registered_office_address,
    ),
    CompanyBinarySensorDescription(
        key="is_active",
        is_on_fn=lambda c: _profile(c).company_status == "active",
    ),
    CompanyBinarySensorDescription(
        key="can_file",
        is_on_fn=lambda c: _profile(c).can_file,
    ),
    CompanyBinarySensorDescription(
        key="has_outstanding_charges",
        dataset=Dataset.CHARGES,
        is_on_fn=lambda c: bool(
            c.charges and c.charges.data and c.charges.data.outstanding_count
        ),
    ),
    CompanyBinarySensorDescription(
        key="has_insolvency_history",
        is_on_fn=lambda c: _profile(c).has_insolvency_history,
    ),
    CompanyBinarySensorDescription(
        key="has_super_secure_officers",
        is_on_fn=lambda c: bool(_profile(c).super_secure_managing_officer_count),
    ),
    CompanyBinarySensorDescription(
        key="has_exemptions",
        dataset=Dataset.STRUCTURE,
        is_on_fn=lambda c: bool(
            c.structure and c.structure.data and c.structure.data.exemptions
        ),
        attrs_fn=lambda c: (
            {"exemptions": c.structure.data.exemptions}
            if c.structure and c.structure.data
            else {}
        ),
    ),
)


class CompanyBinarySensor(CompanyEntity[Any], BinarySensorEntity):
    """A company binary sensor."""

    entity_description: CompanyBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.is_on_fn(self.company)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.company))


@dataclass(frozen=True, kw_only=True)
class OfficerBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an officer binary sensor."""

    dataset: OfficerDataset = OfficerDataset.APPOINTMENTS
    is_on_fn: Callable[[OfficerRuntime], bool | None]
    attrs_fn: Callable[[OfficerRuntime], Mapping[str, Any]] | None = None


def _disqualification_attrs(officer: OfficerRuntime) -> Mapping[str, Any]:
    result = officer.disqualification.data
    if result is None:
        return {}
    return {
        "checked_on": result.checked_on.isoformat() if result.checked_on else None,
        "possible_matches": len(result.possible_matches),
        "possible_match_names": [m.title for m in result.possible_matches],
        "disqualified_from": result.disqualified_from.isoformat()
        if result.disqualified_from
        else None,
        "disqualified_until": result.disqualified_until.isoformat()
        if result.disqualified_until
        else None,
        "reason": result.reason,
        "company_names": result.company_names,
    }


OFFICER_BINARY_SENSORS: tuple[OfficerBinarySensorDescription, ...] = (
    OfficerBinarySensorDescription(
        key="disqualified",
        dataset=OfficerDataset.DISQUALIFICATION,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda o: bool(
            o.disqualification.data and o.disqualification.data.disqualified
        ),
        attrs_fn=_disqualification_attrs,
    ),
    OfficerBinarySensorDescription(
        key="has_active_appointments",
        is_on_fn=lambda o: bool(o.appointments.data and o.appointments.data.active),
    ),
)


class OfficerBinarySensor(OfficerEntity[Any], BinarySensorEntity):
    """An officer binary sensor."""

    entity_description: OfficerBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        return self.entity_description.is_on_fn(self.officer)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.officer))


def _company_binary_sensors(company: CompanyRuntime) -> list[CompanyBinarySensor]:
    coordinators = company.coordinators
    return [
        CompanyBinarySensor(company, coordinators[description.dataset], description)
        for description in COMPANY_BINARY_SENSORS
        if description.dataset in coordinators
    ]


def _officer_binary_sensors(officer: OfficerRuntime) -> list[OfficerBinarySensor]:
    return [
        OfficerBinarySensor(
            officer, officer.coordinators[description.dataset], description
        )
        for description in OFFICER_BINARY_SENSORS
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors, and add more as companies and officers are added."""
    runtime = entry.runtime_data

    @callback
    def _add_company(company: CompanyRuntime) -> None:
        async_add_entities(
            _company_binary_sensors(company),
            config_subentry_id=company.subentry.subentry_id,
        )

    @callback
    def _add_officer(officer: OfficerRuntime) -> None:
        async_add_entities(
            _officer_binary_sensors(officer),
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
