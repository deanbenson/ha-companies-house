"""Sensors for companies, officers and the service device."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_APPOINTMENTS_CAP,
    ATTR_LIST_CAP,
    COMPANY_STATUS_DETAIL_OPTIONS,
    COMPANY_STATUS_OPTIONS,
    DEADLINE_TYPE_OPTIONS,
    JURISDICTION_OPTIONS,
    MAX_STATE_LENGTH,
    Dataset,
    OfficerDataset,
    Tier,
)
from .coordinator import (
    AccountCoordinator,
    CompaniesHouseConfigEntry,
    CompanyRuntime,
    OfficerRuntime,
    signal_new_company,
    signal_new_officer,
    signal_tier,
)
from .entity import CompanyEntity, OfficerEntity, ServiceEntity
from .enumerations import (
    COMPANY_SUBTYPE,
    COMPANY_TYPE,
    OFFICER_ROLE,
    REGISTER_TYPES,
    SIC_DESCRIPTIONS,
)
from .gazette import countdown_attributes
from .models import Appointment, CompanyProfile
from .scheduler import days_until, probe_interval

PARALLEL_UPDATES = 0

type Attrs = Mapping[str, Any]


def _today() -> date:
    return dt_util.now().date()


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _truncate(value: str | None) -> str | None:
    if value is None:
        return None
    return (
        value if len(value) <= MAX_STATE_LENGTH else value[: MAX_STATE_LENGTH - 1] + "…"
    )


# ---------------------------------------------------------------- companies


@dataclass(frozen=True, kw_only=True)
class CompanySensorDescription(SensorEntityDescription):
    """Describes a company sensor and which dataset it reads."""

    dataset: Dataset = Dataset.PROFILE
    value_fn: Callable[[CompanyRuntime], StateType | date]
    attrs_fn: Callable[[CompanyRuntime], Attrs] | None = None


def _profile(company: CompanyRuntime) -> CompanyProfile:
    assert company.profile.data is not None
    return company.profile.data


def _next_deadline(company: CompanyRuntime) -> tuple[date, str] | None:
    return _profile(company).next_deadline


def _sic_attrs(company: CompanyRuntime) -> Attrs:
    codes = _profile(company).sic_codes[:ATTR_LIST_CAP]
    return {"codes": {code: SIC_DESCRIPTIONS.get(code) for code in codes}}


def _address_attrs(company: CompanyRuntime) -> Attrs:
    address = _profile(company).registered_office_address
    if address is None:
        return {}
    return {k: v for k, v in address.to_storage().items() if v}


def _tier_attrs(company: CompanyRuntime) -> Attrs:
    interval, when = probe_interval(
        company.tier, dt_util.utcnow(), company.probe.multiplier
    )
    return {
        "reason": company.tier_reason,
        "probe_interval_minutes": int(interval.total_seconds() // 60),
        "period": when,
        "next_probe": company.probe.next_run.isoformat()
        if company.probe.next_run
        else None,
        "last_probe": company.probe.fetched_at.isoformat()
        if company.probe.fetched_at
        else None,
        "close_watch": company.close_watch,
    }


def _last_filing_attrs(company: CompanyRuntime) -> Attrs:
    data = company.probe.data
    if data is None or not data.items:
        return {}
    item = data.items[0]
    return {
        "category": item.category,
        "type": item.type,
        "transaction_id": item.transaction_id,
        "document_id": item.document_id,
        "paper_filed": item.paper_filed,
        "pages": item.pages,
    }


def _filings_last_12_months(company: CompanyRuntime) -> int:
    cutoff = (_today() - date.resolution * 365).isoformat()
    return sum(
        1 for f in company.state.recent_filings if (f.get("date") or "") >= cutoff
    )


def _company_age(company: CompanyRuntime) -> float | None:
    created = _profile(company).date_of_creation
    if created is None:
        return None
    end = _profile(company).date_of_cessation or _today()
    return round((end - created).days / 365.25, 1)


def _officers(company: CompanyRuntime) -> Any:
    assert company.officers is not None
    return company.officers.data


def _psc(company: CompanyRuntime) -> Any:
    assert company.psc is not None
    return company.psc.data


def _charges(company: CompanyRuntime) -> Any:
    assert company.charges is not None
    return company.charges.data


def _count_roles(company: CompanyRuntime, roles: tuple[str, ...]) -> int:
    return sum(
        1 for o in _officers(company).items if o.is_active and o.officer_role in roles
    )


def _strike_off_attrs(company: CompanyRuntime) -> Attrs:
    return countdown_attributes(
        company.strike_off_countdown(_today()), company.company_number
    )


COMPANY_SENSORS: tuple[CompanySensorDescription, ...] = (
    # Default on
    CompanySensorDescription(
        key="next_deadline",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: d[0] if (d := _next_deadline(c)) else None,
        attrs_fn=lambda c: {
            "deadline_type": d[1] if (d := _next_deadline(c)) else None
        },
    ),
    CompanySensorDescription(
        key="next_deadline_type",
        device_class=SensorDeviceClass.ENUM,
        options=DEADLINE_TYPE_OPTIONS,
        value_fn=lambda c: d[1] if (d := _next_deadline(c)) else None,
    ),
    CompanySensorDescription(
        key="days_to_next_deadline",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=lambda c: days_until(
            d[0] if (d := _next_deadline(c)) else None, _today()
        ),
    ),
    CompanySensorDescription(
        key="accounts_next_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).accounts.next_due,
    ),
    CompanySensorDescription(
        key="confirmation_statement_next_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).confirmation_statement.next_due,
    ),
    CompanySensorDescription(
        key="company_status",
        device_class=SensorDeviceClass.ENUM,
        options=COMPANY_STATUS_OPTIONS,
        value_fn=lambda c: (
            s if (s := _profile(c).company_status) in COMPANY_STATUS_OPTIONS else None
        ),
        attrs_fn=lambda c: {"detail": _profile(c).company_status_detail},
    ),
    # Only meaningful while a strike-off is live; unknown otherwise.
    CompanySensorDescription(
        key="strike_off_earliest_on",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: (
            d.earliest_on if (d := c.strike_off_countdown(_today())) else None
        ),
        attrs_fn=_strike_off_attrs,
    ),
    CompanySensorDescription(
        key="days_to_object",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=lambda c: (
            d.days_to_object if (d := c.strike_off_countdown(_today())) else None
        ),
        attrs_fn=_strike_off_attrs,
    ),
    CompanySensorDescription(
        key="last_filing_date",
        dataset=Dataset.FILINGS,
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: (
            c.probe.data.items[0].date if c.probe.data and c.probe.data.items else None
        ),
    ),
    CompanySensorDescription(
        key="last_filing_description",
        dataset=Dataset.FILINGS,
        value_fn=lambda c: (
            _truncate(c.probe.data.items[0].rendered_description)
            if c.probe.data and c.probe.data.items
            else None
        ),
        attrs_fn=_last_filing_attrs,
    ),
    CompanySensorDescription(
        key="officers_active",
        dataset=Dataset.OFFICERS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: _officers(c).active_count,
        attrs_fn=lambda c: {
            "officers": [
                {
                    "name": o.name,
                    "role": o.officer_role,
                    "appointed_on": _iso(o.appointed_on or o.appointed_before),
                }
                for o in _officers(c).items
                if o.is_active
            ][:ATTR_LIST_CAP]
        },
    ),
    CompanySensorDescription(
        key="psc_active",
        dataset=Dataset.PSC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: _psc(c).active_count,
        attrs_fn=lambda c: {
            "persons": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "natures_of_control": p.natures_of_control,
                }
                for p in _psc(c).items
                if not p.ceased
            ][:ATTR_LIST_CAP]
        },
    ),
    CompanySensorDescription(
        key="charges_outstanding",
        dataset=Dataset.CHARGES,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: _charges(c).outstanding_count,
        attrs_fn=lambda c: {
            "charges": [
                {
                    "charge_code": ch.charge_code,
                    "status": ch.status,
                    "created_on": _iso(ch.created_on),
                    "persons_entitled": ch.persons_entitled,
                }
                for ch in _charges(c).items
                if ch.is_outstanding
            ][:ATTR_LIST_CAP]
        },
    ),
    # Diagnostic
    CompanySensorDescription(
        key="company_name",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _profile(c).company_name,
    ),
    CompanySensorDescription(
        key="company_status_detail",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=COMPANY_STATUS_DETAIL_OPTIONS,
        value_fn=lambda c: (
            s
            if (s := _profile(c).company_status_detail) in COMPANY_STATUS_DETAIL_OPTIONS
            else None
        ),
    ),
    CompanySensorDescription(
        key="company_type",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=sorted(COMPANY_TYPE),
        value_fn=lambda c: t if (t := _profile(c).type) in COMPANY_TYPE else None,
        attrs_fn=lambda c: {"description": COMPANY_TYPE.get(_profile(c).type or "")},
    ),
    CompanySensorDescription(
        key="company_subtype",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=sorted(COMPANY_SUBTYPE),
        value_fn=lambda c: t if (t := _profile(c).subtype) in COMPANY_SUBTYPE else None,
    ),
    CompanySensorDescription(
        key="jurisdiction",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=JURISDICTION_OPTIONS,
        value_fn=lambda c: (
            j if (j := _profile(c).jurisdiction) in JURISDICTION_OPTIONS else None
        ),
    ),
    CompanySensorDescription(
        key="date_of_creation",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).date_of_creation,
    ),
    CompanySensorDescription(
        key="date_of_cessation",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).date_of_cessation,
    ),
    CompanySensorDescription(
        key="registered_office_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (
            _truncate(a.one_line())
            if (a := _profile(c).registered_office_address)
            else None
        ),
        attrs_fn=_address_attrs,
    ),
    CompanySensorDescription(
        key="polling_tier",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=[t.value for t in Tier],
        value_fn=lambda c: c.tier.value,
        attrs_fn=_tier_attrs,
    ),
    # Off by default
    CompanySensorDescription(
        key="company_age",
        native_unit_of_measurement=UnitOfTime.YEARS,
        suggested_display_precision=1,
        value_fn=_company_age,
    ),
    CompanySensorDescription(
        key="accounts_next_made_up_to",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).accounts.next_period_end,
    ),
    CompanySensorDescription(
        key="accounts_last_made_up_to",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).accounts.last_made_up_to,
    ),
    CompanySensorDescription(
        key="accounts_next_period_start",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).accounts.next_period_start,
    ),
    CompanySensorDescription(
        key="accounting_reference_date",
        value_fn=lambda c: (
            f"{a.reference_day:02d}/{a.reference_month:02d}"
            if (a := _profile(c).accounts).reference_day and a.reference_month
            else None
        ),
    ),
    CompanySensorDescription(
        key="last_accounts_type",
        value_fn=lambda c: _profile(c).accounts.last_type,
    ),
    CompanySensorDescription(
        key="confirmation_statement_next_made_up_to",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).confirmation_statement.next_made_up_to,
    ),
    CompanySensorDescription(
        key="confirmation_statement_last_made_up_to",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: _profile(c).confirmation_statement.last_made_up_to,
    ),
    CompanySensorDescription(
        key="days_to_accounts_due",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=lambda c: days_until(_profile(c).accounts.next_due, _today()),
    ),
    CompanySensorDescription(
        key="days_to_confirmation_statement_due",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=lambda c: days_until(
            _profile(c).confirmation_statement.next_due, _today()
        ),
    ),
    CompanySensorDescription(
        key="sic_codes",
        value_fn=lambda c: len(_profile(c).sic_codes),
        attrs_fn=_sic_attrs,
    ),
    CompanySensorDescription(
        key="primary_sic_description",
        value_fn=lambda c: (
            _truncate(SIC_DESCRIPTIONS.get(codes[0], codes[0]))
            if (codes := _profile(c).sic_codes)
            else None
        ),
    ),
    CompanySensorDescription(
        key="officers_total",
        dataset=Dataset.OFFICERS,
        value_fn=lambda c: _officers(c).total_results,
    ),
    CompanySensorDescription(
        key="officers_resigned",
        dataset=Dataset.OFFICERS,
        value_fn=lambda c: _officers(c).resigned_count,
    ),
    CompanySensorDescription(
        key="directors_active",
        dataset=Dataset.OFFICERS,
        value_fn=lambda c: _count_roles(
            c,
            (
                "director",
                "corporate-director",
                "nominee-director",
                "corporate-nominee-director",
            ),
        ),
    ),
    CompanySensorDescription(
        key="secretaries_active",
        dataset=Dataset.OFFICERS,
        value_fn=lambda c: _count_roles(
            c,
            (
                "secretary",
                "corporate-secretary",
                "nominee-secretary",
                "corporate-nominee-secretary",
            ),
        ),
    ),
    CompanySensorDescription(
        key="llp_members_active",
        dataset=Dataset.OFFICERS,
        value_fn=lambda c: _count_roles(
            c,
            (
                "llp-member",
                "llp-designated-member",
                "corporate-llp-member",
                "corporate-llp-designated-member",
            ),
        ),
    ),
    CompanySensorDescription(
        key="psc_total",
        dataset=Dataset.PSC,
        value_fn=lambda c: _psc(c).total_results,
    ),
    CompanySensorDescription(
        key="psc_statements",
        dataset=Dataset.PSC,
        value_fn=lambda c: _psc(c).statements_active_count,
        attrs_fn=lambda c: {
            "statements": [
                s.statement for s in _psc(c).statements if s.ceased_on is None
            ][:ATTR_LIST_CAP]
        },
    ),
    CompanySensorDescription(
        key="charges_total",
        dataset=Dataset.CHARGES,
        value_fn=lambda c: _charges(c).total_count,
    ),
    CompanySensorDescription(
        key="charges_part_satisfied",
        dataset=Dataset.CHARGES,
        value_fn=lambda c: _charges(c).part_satisfied_count,
    ),
    CompanySensorDescription(
        key="charges_satisfied",
        dataset=Dataset.CHARGES,
        value_fn=lambda c: _charges(c).satisfied_count,
    ),
    CompanySensorDescription(
        key="filings_total",
        dataset=Dataset.FILINGS,
        value_fn=lambda c: c.probe.data.total_count if c.probe.data else None,
    ),
    CompanySensorDescription(
        key="filings_last_12_months",
        dataset=Dataset.FILINGS,
        value_fn=_filings_last_12_months,
    ),
    CompanySensorDescription(
        key="days_since_last_filing",
        dataset=Dataset.FILINGS,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        suggested_display_precision=0,
        value_fn=lambda c: (
            -d
            if (d := days_until(c.state.newest_filing_date, _today())) is not None
            else None
        ),
    ),
    CompanySensorDescription(
        key="last_filing_category",
        dataset=Dataset.FILINGS,
        value_fn=lambda c: (
            c.probe.data.items[0].category
            if c.probe.data and c.probe.data.items
            else None
        ),
    ),
    CompanySensorDescription(
        key="previous_names_count",
        value_fn=lambda c: len(_profile(c).previous_company_names),
        attrs_fn=lambda c: {
            "names": [
                {
                    "name": n.name,
                    "from": _iso(n.effective_from),
                    "until": _iso(n.ceased_on),
                }
                for n in _profile(c).previous_company_names
            ][:ATTR_LIST_CAP]
        },
    ),
    CompanySensorDescription(
        key="uk_establishments_count",
        dataset=Dataset.STRUCTURE,
        value_fn=lambda c: (
            len(c.structure.data.uk_establishments)
            if c.structure and c.structure.data
            else None
        ),
        attrs_fn=lambda c: {
            "establishments": [
                e.to_storage() for e in c.structure.data.uk_establishments
            ][:ATTR_LIST_CAP]
            if c.structure and c.structure.data
            else []
        },
    ),
    CompanySensorDescription(
        key="insolvency_cases_count",
        dataset=Dataset.INSOLVENCY,
        value_fn=lambda c: (
            len(c.insolvency.data.cases) if c.insolvency and c.insolvency.data else None
        ),
        attrs_fn=lambda c: (
            {
                "status": c.insolvency.data.status,
                "cases": [
                    {
                        "number": case.number,
                        "type": case.type,
                        "dates": {k: v.isoformat() for k, v in case.dates.items()},
                        "practitioners": [p.name for p in case.practitioners],
                    }
                    for case in c.insolvency.data.cases
                ][:ATTR_LIST_CAP],
            }
            if c.insolvency and c.insolvency.data
            else {}
        ),
    ),
    CompanySensorDescription(
        key="registers_held",
        dataset=Dataset.STRUCTURE,
        value_fn=lambda c: (
            len(c.structure.data.registers)
            if c.structure and c.structure.data
            else None
        ),
        attrs_fn=lambda c: (
            {
                "registers": {
                    REGISTER_TYPES.get(k, k): v
                    for k, v in c.structure.data.registers.items()
                }
            }
            if c.structure and c.structure.data
            else {}
        ),
    ),
)


class CompanySensor(CompanyEntity[Any], SensorEntity):
    """A company sensor."""

    entity_description: CompanySensorDescription

    @property
    def native_value(self) -> StateType | date:
        """Return the state."""
        return self.entity_description.value_fn(self.company)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.company))


class PollingTierSensor(CompanySensor):
    """The tier sensor also listens for schedule changes."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to tier changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_tier(self.company.subentry.subentry_id),
                self._tier_changed,
            )
        )

    @callback
    def _tier_changed(self) -> None:
        self.async_write_ha_state()


# ---------------------------------------------------------------- officers


@dataclass(frozen=True, kw_only=True)
class OfficerSensorDescription(SensorEntityDescription):
    """Describes an officer sensor."""

    dataset: OfficerDataset = OfficerDataset.APPOINTMENTS
    value_fn: Callable[[OfficerRuntime], StateType | date]
    attrs_fn: Callable[[OfficerRuntime], Attrs] | None = None


def _appointments(officer: OfficerRuntime) -> Any:
    assert officer.appointments.data is not None
    return officer.appointments.data


def _active_appointments(officer: OfficerRuntime) -> int:
    """Prefer the register's own count; it excludes dissolved companies."""
    data = _appointments(officer)
    return data.active_count if data.active_count is not None else len(data.active)


def _resigned_appointments(officer: OfficerRuntime) -> int:
    data = _appointments(officer)
    return (
        data.resigned_count if data.resigned_count is not None else len(data.resigned)
    )


def _appointment_date(a: Appointment) -> date | None:
    return a.appointed_on or a.appointed_before


def _sorted_appointments(officer: OfficerRuntime) -> list[Appointment]:
    return sorted(
        _appointments(officer).items,
        key=lambda a: _appointment_date(a) or date.min,
        reverse=True,
    )


def _most_recent(officer: OfficerRuntime) -> Appointment | None:
    ordered = _sorted_appointments(officer)
    return ordered[0] if ordered else None


def _current(officer: OfficerRuntime) -> Appointment | None:
    """Return the most recent active appointment, else the most recent of any."""
    active = [a for a in _sorted_appointments(officer) if a.is_active]
    return active[0] if active else _most_recent(officer)


def _current_companies(officer: OfficerRuntime) -> str:
    """Name the companies the person currently holds a role at, newest first."""
    names = [a.company_name for a in _sorted_appointments(officer) if a.is_active]
    return _truncate(", ".join(n for n in names if n)) or "None"


def _current_company_attrs(officer: OfficerRuntime) -> Attrs:
    """List the current companies, flagging the ones already watched here."""
    watched = {c.company_number for c in officer.entry.runtime_data.companies.values()}
    return {
        "companies": [
            {
                "company_number": a.company_number,
                "company_name": a.company_name,
                "role": a.officer_role,
                "appointed_on": _iso(_appointment_date(a)),
                "watched": a.company_number in watched,
            }
            for a in _sorted_appointments(officer)
            if a.is_active
        ][:ATTR_APPOINTMENTS_CAP]
    }


def _record_attrs(officer: OfficerRuntime) -> Attrs:
    """List the records followed as this person, and any lookalikes found."""
    search = officer.records.data
    return {
        "records": list(officer.officer_ids),
        "checked_on": _iso(search.checked_on) if search else None,
        "possible_matches": [
            {
                "officer_id": m.officer_id,
                "name": m.title,
                "date_of_birth": m.date_of_birth.display() if m.date_of_birth else None,
                "address": m.address_snippet,
                "appointments": m.appointment_count,
            }
            for m in (search.possible_matches if search else [])
        ],
    }


def _appointment_attrs(officer: OfficerRuntime) -> Attrs:
    return {
        "appointments": [
            {
                "company_number": a.company_number,
                "company_name": a.company_name,
                "role": a.officer_role,
                "appointed_on": _iso(_appointment_date(a)),
                "resigned_on": _iso(a.resigned_on),
                "company_status": a.company_status,
            }
            for a in _sorted_appointments(officer)
        ][:ATTR_APPOINTMENTS_CAP]
    }


OFFICER_SENSORS: tuple[OfficerSensorDescription, ...] = (
    OfficerSensorDescription(
        key="current_companies",
        value_fn=_current_companies,
        attrs_fn=_current_company_attrs,
    ),
    OfficerSensorDescription(
        key="appointments_active",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_active_appointments,
        attrs_fn=_appointment_attrs,
    ),
    OfficerSensorDescription(
        key="appointments_total",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda o: _appointments(o).total_results,
    ),
    OfficerSensorDescription(
        key="most_recent_company",
        value_fn=lambda o: (
            _truncate(a.company_name) if (a := _most_recent(o)) else None
        ),
        attrs_fn=lambda o: (
            {
                "company_number": a.company_number,
                "role": a.officer_role,
                "appointed_on": _iso(_appointment_date(a)),
                "company_status": a.company_status,
            }
            if (a := _most_recent(o))
            else {}
        ),
    ),
    OfficerSensorDescription(
        key="last_appointment_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda o: _appointment_date(a) if (a := _most_recent(o)) else None,
    ),
    OfficerSensorDescription(
        key="appointments_resigned",
        value_fn=_resigned_appointments,
    ),
    OfficerSensorDescription(
        key="first_appointment_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda o: min(
            (d for a in _appointments(o).items if (d := _appointment_date(a))),
            default=None,
        ),
    ),
    OfficerSensorDescription(
        key="nationality",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: a.nationality if (a := _current(o)) else None,
    ),
    OfficerSensorDescription(
        key="country_of_residence",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: a.country_of_residence if (a := _current(o)) else None,
    ),
    OfficerSensorDescription(
        key="occupation",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: a.occupation if (a := _current(o)) else None,
    ),
    OfficerSensorDescription(
        key="date_of_birth",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: (
            d.display() if (d := _appointments(o).date_of_birth) else None
        ),
    ),
    OfficerSensorDescription(
        key="register_records",
        dataset=OfficerDataset.RECORDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: len(o.officer_ids),
        attrs_fn=_record_attrs,
    ),
    OfficerSensorDescription(
        key="officer_role",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda o: a.officer_role if (a := _current(o)) else None,
        attrs_fn=lambda o: (
            {"description": OFFICER_ROLE.get(a.officer_role or "")}
            if (a := _current(o))
            else {}
        ),
    ),
)


class OfficerSensor(OfficerEntity[Any], SensorEntity):
    """An officer sensor."""

    entity_description: OfficerSensorDescription

    @property
    def native_value(self) -> StateType | date:
        """Return the state."""
        return self.entity_description.value_fn(self.officer)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.officer))


# ---------------------------------------------------------------- service


@dataclass(frozen=True, kw_only=True)
class ServiceSensorDescription(SensorEntityDescription):
    """Describes a diagnostic sensor on the service device."""

    value_fn: Callable[[AccountCoordinator], StateType | datetime]
    attrs_fn: Callable[[AccountCoordinator], Attrs] | None = None


SERVICE_SENSORS: tuple[ServiceSensorDescription, ...] = (
    ServiceSensorDescription(
        key="requests_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.rate_limit.used,
        attrs_fn=lambda c: {
            "limit": c.data.rate_limit.limit,
            "window_seconds": c.data.rate_limit.window_seconds,
            "from_headers": c.data.rate_limit.from_headers,
        },
    ),
    ServiceSensorDescription(
        key="requests_remaining",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.rate_limit.remaining,
    ),
    ServiceSensorDescription(
        key="budget_used",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.rate_limit.percent_used,
        attrs_fn=lambda c: {
            "blocked_until": c.data.rate_limit.blocked_until,
            "throttled_since": c.data.rate_limit.throttled_since,
        },
    ),
    ServiceSensorDescription(
        key="window_resets_at",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.rate_limit.reset_at,
    ),
    ServiceSensorDescription(
        key="last_successful_update",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: c.data.last_success,
    ),
    ServiceSensorDescription(
        key="last_error",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: _truncate(c.data.last_error),
    ),
    ServiceSensorDescription(
        key="companies_monitored",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.companies_monitored,
    ),
    ServiceSensorDescription(
        key="officers_monitored",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data.officers_monitored,
    ),
    ServiceSensorDescription(
        key="next_scheduled_probe",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda c: p[0] if (p := c.next_probe()) else None,
        attrs_fn=lambda c: {"company": p[1] if (p := c.next_probe()) else None},
    ),
)


class ServiceSensor(ServiceEntity, SensorEntity):
    """A diagnostic sensor on the service device."""

    entity_description: ServiceSensorDescription

    @property
    def native_value(self) -> StateType | datetime:
        """Return the state."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the attributes."""
        if self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.coordinator))


# ---------------------------------------------------------------- setup


def _company_sensors(company: CompanyRuntime) -> list[CompanySensor]:
    coordinators = company.coordinators
    entities: list[CompanySensor] = []
    for description in COMPANY_SENSORS:
        coordinator = coordinators.get(description.dataset)
        if coordinator is None:
            continue
        cls = PollingTierSensor if description.key == "polling_tier" else CompanySensor
        entities.append(cls(company, coordinator, description))
    return entities


def _officer_sensors(officer: OfficerRuntime) -> list[OfficerSensor]:
    return [
        OfficerSensor(officer, officer.coordinators[description.dataset], description)
        for description in OFFICER_SENSORS
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CompaniesHouseConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors, and add more as companies and officers are added."""
    runtime = entry.runtime_data
    async_add_entities(
        ServiceSensor(entry, runtime.account, description)
        for description in SERVICE_SENSORS
    )

    @callback
    def _add_company(company: CompanyRuntime) -> None:
        async_add_entities(
            _company_sensors(company), config_subentry_id=company.subentry.subentry_id
        )

    @callback
    def _add_officer(officer: OfficerRuntime) -> None:
        async_add_entities(
            _officer_sensors(officer), config_subentry_id=officer.subentry.subentry_id
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
