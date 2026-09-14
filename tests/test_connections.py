"""Tests for the connections map: the graph, the rules, the action and the sensor."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.companies_house.connections import (
    INITIAL_WRITE_DELAY,
    REBUILD_DEBOUNCE,
    _company_name_key,
    _company_number,
    _is_subchain,
    _list_names,
    _person_label,
    _share_band,
    _uk_registration,
    build_connections,
    build_graph,
    fingerprint,
    trim_for_llm,
)
from custom_components.companies_house.const import DOMAIN, EVENT_COMPANIES_HOUSE
from custom_components.companies_house.digest import render_html, render_text
from custom_components.companies_house.models import (
    Address,
    Appointment,
    AppointmentList,
    Charge,
    ChargeList,
    CompanyProfile,
    DateOfBirth,
    DisqualificationResult,
    Officer,
    OfficerList,
    Psc,
    PscData,
)

from .conftest import (
    COMPANY_FIXTURES,
    company_subentry,
    load_fixture,
    mock_company,
)
from .test_digest import DIGEST

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
TODAY = date(2026, 9, 14)
OLD = date(2015, 3, 12)
ALEX = DateOfBirth(month=3, year=1978)
LAURA = DateOfBirth(month=2, year=1975)


# ---------------------------------------------------------------- hand-built runtimes


def officer(
    name: str,
    *,
    officer_id: str | None = None,
    appointment_id: str | None = None,
    role: str = "director",
    appointed_on: date | None = OLD,
    resigned_on: date | None = None,
    dob: DateOfBirth | None = None,
    identification: dict[str, str] | None = None,
    occupation: str | None = None,
) -> Officer:
    """Return a company-side officer record."""
    return Officer(
        appointment_id=appointment_id,
        officer_id=officer_id,
        name=name,
        officer_role=role,
        appointed_on=appointed_on,
        resigned_on=resigned_on,
        date_of_birth=dob,
        identification=identification or {},
        occupation=occupation,
    )


def psc(
    name: str,
    *,
    notification_id: str | None = None,
    kind: str = "individual-person-with-significant-control",
    control: tuple[str, ...] = ("ownership-of-shares-75-to-100-percent",),
    notified_on: date | None = OLD,
    ceased_on: date | None = None,
    dob: DateOfBirth | None = None,
    identification: dict[str, str] | None = None,
    sanctioned: bool = False,
) -> Psc:
    """Return a PSC record."""
    return Psc(
        notification_id=notification_id,
        name=name,
        kind=kind,
        natures_of_control=list(control),
        notified_on=notified_on,
        ceased_on=ceased_on,
        ceased=ceased_on is not None,
        date_of_birth=dob,
        identification=identification or {},
        is_sanctioned=sanctioned,
    )


def company(
    number: str,
    name: str,
    *,
    officers: list[Officer] | None = None,
    pscs: list[Psc] | None = None,
    charges: list[Charge] | None = None,
    status: str = "active",
    detail: str | None = None,
    close_watch: bool = False,
    notify: bool = False,
    address: str | None = "1 Group Way",
    changes: list[dict[str, Any]] | None = None,
    in_report: bool = True,
    datasets: bool = True,
) -> Any:
    """Return something that quacks like a CompanyRuntime, without Home Assistant."""
    profile = CompanyProfile(
        company_number=number,
        company_name=name,
        company_status=status,
        company_status_detail=detail,
        registered_office_address=Address(address_line_1=address, locality="Town")
        if address
        else None,
    )
    # Register ids are unique per appointment and per notification, so a
    # record without one gets an id of its own here.
    officers = [
        replace(o, appointment_id=o.appointment_id or f"appt-{number}-{i}")
        for i, o in enumerate(officers or [])
    ]
    pscs = [
        replace(p, notification_id=p.notification_id or f"psc-{number}-{i}")
        for i, p in enumerate(pscs or [])
    ]
    return SimpleNamespace(
        company_number=number,
        company_name=name,
        close_watch=close_watch,
        notify_instantly=notify,
        label="",
        website="",
        in_weekly_report=in_report,
        strike_off_proposed=detail == "active-proposal-to-strike-off",
        profile=SimpleNamespace(data=profile),
        officers=SimpleNamespace(data=OfficerList(items=officers))
        if datasets
        else None,
        psc=SimpleNamespace(data=PscData(items=pscs)) if datasets else None,
        charges=SimpleNamespace(data=ChargeList(items=charges or []))
        if datasets
        else None,
        state=SimpleNamespace(changes=changes or []),
    )


def person(
    officer_id: str,
    name: str,
    *,
    officer_ids: list[str] | None = None,
    dob: DateOfBirth | None = None,
    appointments: list[Appointment] | None = None,
    disqualified: bool = False,
    notify: bool = False,
    configured_name: str = "",
) -> Any:
    """Return something that quacks like an OfficerRuntime."""
    return SimpleNamespace(
        officer_id=officer_id,
        officer_ids=[officer_id, *(officer_ids or [])],
        register_name=name,
        configured_name=configured_name,
        officer_name=configured_name or name,
        known_date_of_birth=dob,
        notify_instantly=notify,
        disqualification=SimpleNamespace(
            data=DisqualificationResult(disqualified=disqualified)
        ),
        appointments=SimpleNamespace(
            data=AppointmentList(
                officer_id=officer_id, name=name, items=appointments or []
            )
        ),
        state=SimpleNamespace(changes=[]),
    )


def appointment(
    number: str,
    name: str,
    *,
    status: str = "active",
    role: str = "director",
    appointed_on: date | None = OLD,
    resigned_on: date | None = None,
    appointment_id: str | None = None,
) -> Appointment:
    """Return a person-side appointment record."""
    return Appointment(
        appointment_id=appointment_id or f"app-{number}",
        company_number=number,
        company_name=name,
        company_status=status,
        officer_role=role,
        appointed_on=appointed_on,
        resigned_on=resigned_on,
    )


def graph(*runtimes: Any, **kwargs: Any) -> dict[str, Any]:
    """Build the map from hand-built companies (numbers) and people (ids)."""
    companies = [r for r in runtimes if hasattr(r, "company_number")]
    people = [r for r in runtimes if hasattr(r, "officer_id")]
    return build_graph(companies, people, **{"now": NOW, **kwargs})


def texts(out: dict[str, Any], rule: str | None = None) -> list[str]:
    """Return the interesting lines' text, optionally for one rule."""
    return [x["text"] for x in out["interesting"] if rule is None or x["rule"] == rule]


def node(out: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Return one node by id."""
    return next(n for n in out["nodes"] if n["id"] == node_id)


# ---------------------------------------------------------------- helpers


def test_small_helpers() -> None:
    """Numbers, registrations, share bands, name keys and English lists behave."""
    assert _company_number("9876543") == "09876543"
    assert _company_number(" sc 12345 ") == "SC012345"
    assert _company_number("not a number") is None
    assert _company_number(None) is None
    assert (
        _uk_registration(
            {
                "identification_type": "uk-limited-company",
                "registration_number": "1234567",
            }
        )
        == "01234567"
    )
    assert (
        _uk_registration(
            {"place_registered": "England And Wales", "registration_number": "09876543"}
        )
        == "09876543"
    )
    assert (
        _uk_registration(
            {"legal_authority": "Companies Act 2006", "registration_number": "12345"}
        )
        == "00012345"
    )
    assert (
        _uk_registration({"country_registered": "Delaware", "registration_number": "5"})
        is None
    )
    assert _uk_registration({"place_registered": "England"}) is None
    assert _share_band(["voting-rights-50-to-75-percent"]) is None
    assert _share_band(["ownership-of-shares-25-to-50-percent"]) == "25-50"
    assert _company_name_key("The Example Trading Ltd.") == _company_name_key(
        "EXAMPLE TRADING LIMITED"
    )
    assert _person_label("Mr. Robert Taylor") == "Robert Taylor"
    assert _person_label("TAYLOR, Robert") == "Robert Taylor"
    assert _person_label("Dr") == "Dr"
    assert _list_names([]) == ""
    assert _list_names(["A"]) == "A"
    assert _list_names(["A", "B"]) == "A and B"
    assert _list_names(["A", "B", "C", "D", "E"]) == "A, B, C and 2 more"
    assert _is_subchain(["b", "c"], ["a", "b", "c", "d"])
    assert not _is_subchain(["a", "c"], ["a", "b", "c"])


# ---------------------------------------------------------------- identity


def test_people_are_joined_by_record_then_name_and_birth() -> None:
    """One human, several records: record id wins, then name plus birth month."""
    alex = person(
        "rec-1",
        "Alex MORGAN",
        officer_ids=["rec-2"],
        dob=ALEX,
        appointments=[appointment("30000001", "OUTSIDE LTD")],
        configured_name="Alex (main)",
    )
    out = graph(
        company(
            "10000001",
            "ALPHA LTD",
            officers=[
                officer("MORGAN, Alex", officer_id="rec-1", dob=ALEX),
                # A third record nobody follows, same name and birth month.
                officer(
                    "MORGAN, Alex James",
                    officer_id="rec-3",
                    dob=ALEX,
                    appointment_id="appt-3",
                ),
                # Same name, different birth month: someone else.
                officer(
                    "MORGAN, Alex",
                    officer_id="rec-9",
                    dob=DateOfBirth(month=1, year=1990),
                    appointment_id="appt-9",
                ),
            ],
            pscs=[
                psc("Mr Alex Morgan", dob=ALEX),
                # No birth date on this record: joined by name alone.
                psc("Mr Alex Morgan", notification_id="psc-nodob"),
            ],
        ),
        company(
            "10000002",
            "BETA LTD",
            officers=[
                officer(
                    "MORGAN, Alex",
                    officer_id="rec-2",
                    dob=ALEX,
                    appointment_id="appt-b",
                )
            ],
        ),
        alex,
    )
    ids = {n["id"] for n in out["nodes"] if n["type"] == "person"}
    assert ids == {"person:rec-1", "person:rec-9"}
    me = node(out, "person:rec-1")
    assert me["status"] == "followed"
    assert me["label"] == "Alex (main)"
    assert sorted(me["meta"]["officer_ids"]) == ["rec-1", "rec-2", "rec-3"]
    assert me["meta"]["date_of_birth"] == "03/1978"
    assert me["degree"] == 6
    other = node(out, "person:rec-9")
    assert other["status"] == "external"
    assert other["degree"] == 1
    assert node(out, "company:30000001")["status"] == "external"
    assert "external" in node(out, "company:30000001")["flags"]


def test_corporate_records_become_company_or_entity_nodes() -> None:
    """A UK corporate PSC or officer joins its company; anything else is an entity."""
    out = graph(
        company(
            "10000001",
            "SUB LTD",
            officers=[
                officer(
                    "ACME SECRETARIES LIMITED",
                    officer_id="sec",
                    role="corporate-secretary",
                    identification={
                        "identification_type": "uk-limited-company",
                        "registration_number": "11111111",
                    },
                ),
                officer(
                    "OVERSEAS NOMINEES INC",
                    officer_id="nom",
                    role="corporate-director",
                    identification={
                        "identification_type": "other-corporate-body-or-firm",
                        "registration_number": "555",
                        "place_registered": "Delaware",
                    },
                ),
            ],
            pscs=[
                psc(
                    "Parent Holdings Limited",
                    kind="corporate-entity-person-with-significant-control",
                    identification={
                        "place_registered": "England And Wales",
                        "registration_number": "9876543",
                    },
                ),
                psc(
                    "Cayman Parent Ltd",
                    kind="corporate-entity-person-with-significant-control",
                    identification={
                        "country_registered": "Cayman Islands",
                        "registration_number": "CI-1",
                    },
                ),
                psc("The Crown", kind="legal-person-person-with-significant-control"),
                psc("Hidden", kind="super-secure-person-with-significant-control"),
            ],
        ),
        company("09876543", "PARENT HOLDINGS LIMITED"),
    )
    assert node(out, "company:11111111")["type"] == "company"
    assert node(out, "company:11111111")["label"] == "ACME SECRETARIES LIMITED"
    assert node(out, "entity:overseas-nominees-inc")["flags"] == ["corporate"]
    parent = node(out, "company:09876543")
    assert parent["status"] == "watched"
    assert parent["label"] == "PARENT HOLDINGS LIMITED"  # the profile's name wins
    assert node(out, "entity:cayman-parent-ltd")["flags"] == ["corporate"]
    assert node(out, "entity:the-crown")["flags"] == ["legal_person"]
    assert not any(n["label"] == "Hidden" for n in out["nodes"])
    kinds = {e["id"]: e["kind"] for e in out["edges"]}
    assert kinds["appt-10000001-0"] == "officer"
    assert kinds["psc-10000001-0"] == "psc"
    edge = next(e for e in out["edges"] if e["id"] == "appt-10000001-0")
    assert edge["role_label"] == "Secretary"
    assert edge["link"].endswith("/company/10000001/officers")
    # Only the overseas parent is called out; the UK one is watched.
    assert texts(out, "unwatched-parent") == [
        (
            "Cayman Parent Ltd controls SUB LTD (75 to 100% of the shares) and is not on "
            "the UK register, so it cannot be watched"
        )
    ]


def test_edges_from_both_sides_are_one_edge() -> None:
    """A followed person's role at a watched company appears once, with its status."""
    jane = person(
        "jane",
        "Jane SMITH",
        dob=ALEX,
        appointments=[
            appointment("10000001", "ALPHA LTD", appointment_id="appt-1"),
            appointment("30000001", "OUTSIDE LTD", status="liquidation"),
        ],
    )
    out = graph(
        company(
            "10000001",
            "ALPHA LTD",
            officers=[
                officer(
                    "SMITH, Jane", officer_id="jane", dob=ALEX, appointment_id="appt-1"
                )
            ],
        ),
        jane,
    )
    assert [e["id"] for e in out["edges"]] == ["app-30000001", "appt-1"]
    assert node(out, "company:30000001")["company_status"] == "liquidation"
    assert "insolvent" in node(out, "company:30000001")["flags"]
    assert out["summary"]["live_connections"] == 1
    assert out["summary"]["followed_people"] == 1
    assert out["summary"]["external_companies"] == 1


# ---------------------------------------------------------------- rules


def test_shared_board_hub_and_simultaneous_moves() -> None:
    """Shared boards, hub people and same-day appointments are spotted and ranked."""
    simon = officer("REED, Simon", officer_id="simon", dob=ALEX)
    recent = date(2026, 9, 10)
    companies = [
        company(
            "10000001",
            "ORBIT BIDCO LTD",
            close_watch=True,
            officers=[
                officer(
                    "REED, Simon", officer_id="simon", dob=ALEX, appointed_on=recent
                ),
                officer(
                    "COLE, Nicholas", officer_id="nick", dob=LAURA, appointed_on=recent
                ),
            ],
        ),
        company(
            "10000002",
            "ORBIT MIDCO LTD",
            officers=[
                officer(
                    "REED, Simon", officer_id="simon", dob=ALEX, appointed_on=recent
                ),
                officer(
                    "COLE, Nicholas", officer_id="nick", dob=LAURA, appointed_on=recent
                ),
            ],
        ),
        company("10000003", "ORBIT TOPCO LTD", officers=[simon]),
        company("10000004", "ORBIT HOLDCO LTD", officers=[simon]),
        company(
            "10000005",
            "ORBIT FINCO LTD",
            officers=[
                simon,
                officer(
                    "COLE, Nicholas",
                    officer_id="nick",
                    dob=LAURA,
                    appointed_on=date(2016, 1, 1),
                ),
            ],
        ),
    ]
    out = graph(*companies, days=7)
    assert (
        "Nicholas Cole and Simon Reed both sit on the boards of ORBIT BIDCO LTD and "
        "ORBIT MIDCO LTD — all joined ORBIT BIDCO LTD on 10 Sep 2026"
    ) in texts(out, "shared-board")
    assert len(texts(out, "shared-board")) == 3
    # Your own company's lines come first; the pair that joined long ago says so.
    assert texts(out, "shared-board")[0].startswith(
        "Nicholas Cole and Simon Reed both sit on the boards of ORBIT BIDCO LTD and "
        "ORBIT FINCO LTD"
    )
    assert texts(out, "shared-board")[2] == (
        "Nicholas Cole and Simon Reed both sit on the boards of ORBIT FINCO LTD and "
        "ORBIT MIDCO LTD"
    )
    assert texts(out, "hub-person") == [
        (
            "Simon Reed holds roles at 5 watched companies: ORBIT BIDCO LTD, ORBIT "
            "FINCO LTD, ORBIT HOLDCO LTD, ORBIT MIDCO LTD and 1 more"
        ),
        (
            "Nicholas Cole holds roles at 3 watched companies: ORBIT BIDCO LTD, ORBIT "
            "FINCO LTD and ORBIT MIDCO LTD"
        ),
    ]
    severities = {x["text"]: x["severity"] for x in out["interesting"]}
    assert severities[texts(out, "hub-person")[0]] == "medium"
    assert severities[texts(out, "hub-person")[1]] == "low"
    assert texts(out, "simultaneous-moves") == [
        (
            "Nicholas Cole and Simon Reed both joined ORBIT BIDCO LTD and ORBIT MIDCO "
            "LTD on 10 Sep 2026"
        )
    ]
    # Ordering: severity first, then the close-watch company's lines.
    first = out["interesting"][0]
    assert first["severity"] == "medium"
    assert first["link"].endswith("/company/10000001")
    assert first["links"][0] == {
        "text": "ORBIT BIDCO LTD",
        "href": "https://find-and-update.company-information.service.gov.uk/company/10000001",
    }
    assert out["summary"]["new_connections"] == 4
    assert {e["new"] for e in out["edges"] if e["target"] == "company:10000003"} == {
        False
    }
    # Outside the period nothing is new and nobody moved together.
    old = graph(*companies, now=NOW + timedelta(days=60))
    assert texts(old, "simultaneous-moves") == []
    assert old["summary"]["new_connections"] == 0


def test_ownership_hub_chain_and_ultimate_owner() -> None:
    """Who owns what: hubs, chains through holding companies, the person at the top."""
    holdco_psc = psc(
        "Holdco Capital Limited",
        kind="corporate-entity-person-with-significant-control",
        identification={
            "place_registered": "Scotland",
            "registration_number": "sc 000001",
        },
        control=(
            "ownership-of-shares-75-to-100-percent",
            "voting-rights-75-to-100-percent",
        ),
    )
    out = graph(
        company(
            "SC000001",
            "HOLDCO CAPITAL LIMITED",
            pscs=[
                psc("Mr Alex Morgan", dob=ALEX),
                psc(
                    "Big Fund LP",
                    kind="corporate-entity-person-with-significant-control",
                    identification={
                        "place_registered": "England",
                        "registration_number": "LP000001",
                    },
                    control=("ownership-of-shares-25-to-50-percent",),
                ),
            ],
        ),
        company("10000001", "PORTFOLIO ONE LTD", pscs=[holdco_psc]),
        company("10000002", "PORTFOLIO TWO LTD", pscs=[holdco_psc]),
    )
    assert texts(out, "ownership-hub") == [
        (
            "HOLDCO CAPITAL LIMITED controls 2 watched companies (75 to 100% of the "
            "shares, 75 to 100% of the votes in each)"
        )
    ]
    assert texts(out, "ultimate-owner") == [
        (
            "Alex Morgan ultimately controls PORTFOLIO ONE LTD through HOLDCO CAPITAL "
            "LIMITED"
        ),
        (
            "Alex Morgan ultimately controls PORTFOLIO TWO LTD through HOLDCO CAPITAL "
            "LIMITED"
        ),
    ]
    assert texts(out, "ownership-chain") == [
        "Big Fund LP → HOLDCO CAPITAL LIMITED → PORTFOLIO ONE LTD",
        "Big Fund LP → HOLDCO CAPITAL LIMITED → PORTFOLIO TWO LTD",
    ]
    assert texts(out, "unwatched-parent") == [
        (
            "Big Fund LP (LP000001) controls HOLDCO CAPITAL LIMITED (25 to 50% of the "
            "shares) but is not watched"
        )
    ]
    chain = next(x for x in out["interesting"] if x["rule"] == "ultimate-owner")
    assert chain["node_ids"] == [
        "person:mr-alex-morgan",
        "company:SC000001",
        "company:10000001",
    ]
    assert len(chain["edge_ids"]) == 2
    assert chain["link"].endswith("/company/SC000001")
    # Owners with several holdings that differ are spelled out one by one.
    mixed = graph(
        company("10000001", "ONE LTD", pscs=[psc("Mr Alex Morgan", dob=ALEX)]),
        company(
            "10000002",
            "TWO LTD",
            pscs=[
                psc(
                    "Mr Alex Morgan",
                    dob=ALEX,
                    control=("significant-influence-or-control",),
                )
            ],
        ),
    )
    assert texts(mixed, "ownership-hub") == [
        (
            "Alex Morgan controls 2 watched companies (ONE LTD: 75 to 100% of the shares; "
            "TWO LTD: significant influence or control)"
        )
    ]


def test_risk_next_door_disqualified_and_sanctioned() -> None:
    """The high-severity rules: trouble one step away, a ban, a sanction."""
    laura = person(
        "laura",
        "Laura PRICE",
        dob=LAURA,
        disqualified=True,
        appointments=[
            appointment("10000001", "VELVET HOG SALTBURN LTD", appointment_id="a1"),
            appointment("30000001", "THE VELVET HOG LTD", status="liquidation"),
            appointment("30000002", "GONE LTD", status="dissolved"),
            appointment("30000003", "PAST LTD", status="liquidation", resigned_on=OLD),
        ],
    )
    out = graph(
        company(
            "10000001",
            "VELVET HOG SALTBURN LTD",
            officers=[
                officer(
                    "PRICE, Laura", officer_id="laura", dob=LAURA, appointment_id="a1"
                ),
                officer("GREEN, Michael", officer_id="mike", dob=ALEX),
            ],
            pscs=[psc("Mr Oleg Sanctionov", sanctioned=True)],
        ),
        company(
            "10000002",
            "DORMANT HOLDINGS LTD",
            detail="active-proposal-to-strike-off",
            officers=[
                officer(
                    "GREEN, Michael", officer_id="mike", dob=ALEX, appointment_id="a2"
                )
            ],
        ),
        laura,
    )
    assert texts(out, "risk-next-door") == [
        (
            "Laura Price, a director of VELVET HOG SALTBURN LTD, also runs THE VELVET "
            "HOG LTD, which is in liquidation"
        ),
        (
            "Michael Green, a director of VELVET HOG SALTBURN LTD, also runs DORMANT "
            "HOLDINGS LTD, which is facing strike-off"
        ),
    ]
    assert texts(out, "disqualified") == [
        (
            "Laura Price is disqualified as a director but still holds 3 active roles, at "
            "GONE LTD, THE VELVET HOG LTD and VELVET HOG SALTBURN LTD"
        )
    ]
    assert texts(out, "sanctioned") == [
        (
            "Oleg Sanctionov, who controls VELVET HOG SALTBURN LTD (75 to 100% of the "
            "shares), is on the UK sanctions list"
        )
    ]
    assert "sanctioned" in node(out, "person:mr-oleg-sanctionov")["flags"]
    assert "disqualified" in node(out, "person:laura")["flags"]
    assert "strike_off" in node(out, "company:10000002")["flags"]
    assert [x["severity"] for x in out["interesting"]][:4] == ["high"] * 4
    assert out["summary"]["high"] == 4


def test_new_link_and_link_broken() -> None:
    """A new role that bridges two groups, and a resignation that cuts one."""
    recent = date(2026, 9, 12)
    changes = [
        {
            "at": "2026-09-13T10:00:00+00:00",
            "kind": "officer",
            "event_type": "resigned",
            "payload": {"name": "PATEL, Priya", "officer_id": "priya"},
        },
        {
            "at": "2026-09-13T10:00:00+00:00",
            "kind": "psc",
            "event_type": "ceased",
            "payload": {"name": "Ms Priya Patel"},
        },
        {
            "at": "2026-09-13T10:00:00+00:00",
            "kind": "officer",
            "event_type": "resigned",
            "payload": {"name": "NOBODY, Ann", "officer_id": "ann"},
        },
        {
            "at": "2026-01-01T10:00:00+00:00",
            "kind": "officer",
            "event_type": "resigned",
            "payload": {"name": "OLD, News", "officer_id": "old"},
        },
        {"at": "bad", "kind": "officer", "event_type": "resigned", "payload": {}},
        {
            "at": "2026-09-13T10:00:00+00:00",
            "kind": "officer",
            "event_type": "appointed",
            "payload": {"name": "SMITH, Jane"},
        },
    ]
    out = graph(
        company(
            "10000001",
            "ALPHA LTD",
            officers=[
                officer("SMITH, Jane", officer_id="jane", dob=ALEX),
                officer(
                    "PATEL, Priya", officer_id="priya", dob=LAURA, resigned_on=recent
                ),
            ],
            changes=changes,
        ),
        company(
            "10000002",
            "BETA LTD",
            officers=[
                officer(
                    "SMITH, Jane",
                    officer_id="jane",
                    dob=ALEX,
                    appointed_on=recent,
                    appointment_id="b1",
                ),
                officer(
                    "PATEL, Priya", officer_id="priya", dob=LAURA, appointment_id="b2"
                ),
            ],
        ),
        company(
            "10000003",
            "GAMMA LTD",
            officers=[
                officer(
                    "BROWN, Tom",
                    officer_id="tom",
                    dob=ALEX,
                    appointed_on=recent,
                    appointment_id="g1",
                )
            ],
        ),
    )
    assert texts(out, "new-link") == ["Jane Smith now links BETA LTD to ALPHA LTD"]
    assert texts(out, "link-broken") == [
        "ALPHA LTD and BETA LTD are no longer connected through Priya Patel"
    ]


def test_office_unwatched_connected_owner_and_lender_rules() -> None:
    """The low-severity rules: shared office, unwatched hub, owners, lenders."""
    alex = person(
        "alex",
        "Alex MORGAN",
        dob=ALEX,
        appointments=[appointment("30000001", "TECHNOVA LTD")],
    )
    mark = person(
        "mark",
        "Mark TAYLOR",
        dob=LAURA,
        appointments=[
            appointment("30000001", "TECHNOVA LTD", appointment_id="m1"),
            appointment(
                "30000009", "GONE LTD", status="dissolved", appointment_id="m2"
            ),
        ],
    )
    hilary = person(
        "hilary",
        "Hilary TAYLOR",
        appointments=[appointment("30000009", "GONE LTD", appointment_id="h2")],
    )
    out = graph(
        company(
            "10000001",
            "TAYLOR PROPERTY LTD",
            officers=[officer("TAYLOR, Mark", officer_id="mark", dob=LAURA)],
            pscs=[psc("Mr Mark Taylor", dob=LAURA), psc("Mrs Hilary Taylor")],
            charges=[
                Charge(
                    charge_id="c1",
                    status="outstanding",
                    created_on=OLD,
                    persons_entitled=["Taylor Group Investments Ltd", "Alex Morgan"],
                ),
                Charge(
                    charge_id="c2",
                    status="fully-satisfied",
                    created_on=OLD,
                    satisfied_on=OLD,
                    persons_entitled=["Taylor Group Investments Ltd"],
                ),
                Charge(
                    charge_id="c3",
                    status="outstanding",
                    persons_entitled=["HSBC UK Bank Plc"],
                ),
            ],
        ),
        company(
            "10000002",
            "TAYLOR GROUP INVESTMENTS LIMITED",
            officers=[
                officer(
                    "TAYLOR, Mark", officer_id="mark", dob=LAURA, appointment_id="l2"
                )
            ],
        ),
        company("10000003", "ELSEWHERE LTD", address="9 Other Road"),
        company("10000004", "OLD LTD", status="dissolved"),
        company("10000005", "NO ADDRESS LTD", address=None, datasets=False),
        alex,
        mark,
        hilary,
    )
    assert texts(out, "shared-office") == [
        (
            "2 watched companies share the registered office at 1 Group Way, Town: TAYLOR "
            "GROUP INVESTMENTS LIMITED and TAYLOR PROPERTY LTD"
        )
    ]
    assert texts(out, "unwatched-connected") == [
        (
            "TECHNOVA LTD is not watched but Alex Morgan and Mark Taylor, whom you follow, "
            "both hold roles there"
        )
    ]
    assert texts(out, "owner-not-on-board") == [
        (
            "Hilary Taylor controls TAYLOR PROPERTY LTD (75 to 100% of the shares) but "
            "holds no role there"
        )
    ]
    assert texts(out, "director-not-owner") == []
    assert texts(out, "intra-group-lender") == [
        (
            "TAYLOR PROPERTY LTD has a charge in favour of Alex Morgan (created 12 Mar "
            "2015)"
        ),
        (
            "TAYLOR PROPERTY LTD has a charge in favour of TAYLOR GROUP INVESTMENTS "
            "LIMITED (created 12 Mar 2015)"
        ),
    ]
    lenders = {e["source"] for e in out["edges"] if e["kind"] == "charge"}
    assert lenders == {"company:10000002", "person:alex", "entity:hsbc-uk-bank-plc"}
    assert "lender" in node(out, "entity:hsbc-uk-bank-plc")["flags"]
    # Dissolved companies are drawn but say nothing.
    assert "dissolved" in node(out, "company:10000004")["flags"]
    assert not any("OLD LTD" in t for t in texts(out))


def test_sole_director_who_is_not_an_owner() -> None:
    """The only director of a company owned by someone else is worth a line."""
    out = graph(
        company(
            "10000001",
            "FRONT LTD",
            officers=[
                officer("PATEL, Priya", officer_id="priya", dob=LAURA),
                officer("SEC, Sam", officer_id="sam", role="secretary"),
            ],
            pscs=[psc("Mr Silent Partner", dob=ALEX)],
        ),
        # Owned by a company: no line, the director naturally is not a PSC.
        company(
            "10000002",
            "SUB LTD",
            officers=[
                officer(
                    "PATEL, Priya", officer_id="priya", dob=LAURA, appointment_id="s1"
                )
            ],
            pscs=[
                psc(
                    "Parent Ltd",
                    kind="corporate-entity-person-with-significant-control",
                    identification={
                        "place_registered": "England",
                        "registration_number": "1",
                    },
                )
            ],
        ),
    )
    assert texts(out, "director-not-owner") == [
        (
            "Priya Patel is the only director of FRONT LTD but not a person with "
            "significant control; Silent Partner is"
        )
    ]


def test_reciprocal_control() -> None:
    """A's director owns B while B's director owns A."""
    out = graph(
        company(
            "10000001",
            "ALPHA LTD",
            officers=[officer("ONE, Ann", officer_id="ann", dob=ALEX)],
            pscs=[psc("Mr Bob Two", dob=LAURA)],
        ),
        company(
            "10000002",
            "BETA LTD",
            officers=[officer("TWO, Bob", officer_id="bob", dob=LAURA)],
            pscs=[psc("Ms Ann One", dob=ALEX)],
        ),
        # One person running and owning both is a hub, not a loop.
        company(
            "10000003",
            "GAMMA LTD",
            officers=[officer("THREE, Cy", officer_id="cy", dob=ALEX)],
            pscs=[psc("Mr Cy Three", dob=ALEX)],
        ),
        company(
            "10000004",
            "DELTA LTD",
            officers=[
                officer("THREE, Cy", officer_id="cy", dob=ALEX, appointment_id="d1")
            ],
            pscs=[psc("Mr Cy Three", dob=ALEX, notification_id="d2")],
        ),
    )
    assert texts(out, "reciprocal-control") == [
        (
            "ALPHA LTD and BETA LTD control each other: Ann One runs ALPHA LTD and owns "
            "BETA LTD; Bob Two runs BETA LTD and owns ALPHA LTD"
        )
    ]
    assert out["summary"]["components"] == 2


def test_include_flags_trim_the_drawing_not_the_rules() -> None:
    """Resigned roles and unwatched companies can be left out of the drawing."""
    jane = person(
        "jane",
        "Jane SMITH",
        dob=ALEX,
        appointments=[
            appointment("30000001", "OUTSIDE LTD", status="liquidation"),
            appointment("30000002", "GONE LTD", resigned_on=OLD),
        ],
    )
    runtimes = [
        company(
            "10000001",
            "ALPHA LTD",
            officers=[
                officer("SMITH, Jane", officer_id="jane", dob=ALEX),
                officer("BROWN, Tom", officer_id="tom", dob=LAURA, resigned_on=OLD),
            ],
            charges=[
                Charge(
                    charge_id="c1", status="fully-satisfied", persons_entitled=["Bank"]
                )
            ],
        ),
        jane,
    ]
    full = graph(*runtimes, include_resigned=True, include_external=True)
    assert {e["id"] for e in full["edges"]} == {
        "app-30000001",
        "app-30000002",
        "appt-10000001-0",
        "appt-10000001-1",
        "charge:c1:0",
    }
    assert {n["id"] for n in full["nodes"]} >= {
        "person:tom",
        "entity:bank",
        "company:30000002",
    }

    slim = graph(*runtimes, include_resigned=False, include_external=False)
    assert {e["id"] for e in slim["edges"]} == {"appt-10000001-0"}
    assert {n["id"] for n in slim["nodes"]} == {"company:10000001", "person:jane"}
    # The rules still see the unwatched company in liquidation.
    assert texts(slim, "risk-next-door") == [
        (
            "Jane Smith, a director of ALPHA LTD, also runs OUTSIDE LTD, which is in "
            "liquidation"
        )
    ]
    assert slim["summary"]["nodes"] == 2
    assert slim["summary"]["edges"] == 1
    assert slim["summary"]["entities"] == 0
    assert slim["summary"]["other_people"] == 0


def test_fingerprint_and_trim() -> None:
    """The fingerprint ignores the clock; the assistant gets lines, not a drawing."""
    runtimes = [
        company(
            "10000001",
            "ALPHA LTD",
            officers=[officer("SMITH, Jane", officer_id="jane")],
        )
    ]
    one = build_graph(runtimes, [], now=NOW)
    two = build_graph(runtimes, [], now=NOW + timedelta(hours=1))
    assert one["generated_at"] != two["generated_at"]
    assert fingerprint(one) == fingerprint(two)
    three = build_graph(
        [
            company(
                "10000001",
                "ALPHA LTD",
                officers=[officer("SMITH, Jane", officer_id="jane"), officer("X, Y")],
            )
        ],
        [],
        now=NOW,
    )
    assert fingerprint(one) != fingerprint(three)
    trimmed = trim_for_llm(one)
    assert set(trimmed) == {"generated_at", "period_days", "summary", "interesting"}
    assert trimmed["summary"]["nodes"] == 2


def test_edge_cases_in_identity_and_chains() -> None:
    """Odd records: namesakes without ids, empty numbers, long chains, quiet bans."""
    grandparent = psc(
        "Top Co Ltd",
        kind="corporate-entity-person-with-significant-control",
        identification={
            "place_registered": "England",
            "registration_number": "40000001",
        },
    )
    parent = psc(
        "Mid Co Ltd",
        kind="corporate-entity-person-with-significant-control",
        identification={
            "place_registered": "England",
            "registration_number": "40000002",
        },
    )
    child = psc(
        "Low Co Ltd",
        kind="corporate-entity-person-with-significant-control",
        identification={
            "place_registered": "England",
            "registration_number": "40000003",
        },
    )
    banned = person("ban", "Banned PERSON", disqualified=True, officer_ids=["ban-2"])
    blank = person(
        "blank",
        "Blank ROLE",
        notify=True,
        appointments=[
            appointment("", "NOWHERE LTD"),
            appointment("40000002", None, resigned_on=OLD, appointment_id="gone"),
            appointment("40000009", None, appointment_id="nameless"),
        ],
    )
    out = graph(
        company(
            "40000002",
            "MID CO LTD",
            notify=True,
            pscs=[grandparent],
            officers=[
                # Two different people, same name, no record ids: two nodes.
                officer("SAME, Name", dob=ALEX),
                officer("SAME, Name", dob=LAURA),
            ],
            changes=[
                {
                    "at": "2026-09-13T10:00:00+00:00",
                    "kind": "officer",
                    "event_type": "resigned",
                    "payload": {"name": "SAME, Name"},
                },
                {
                    "at": "2026-09-13T10:00:00+00:00",
                    "kind": "officer",
                    "event_type": "resigned",
                    "payload": {"name": "Blank ROLE", "officer_id": "blank"},
                },
            ],
        ),
        company("40000003", "LOW CO LTD", pscs=[parent]),
        company("40000004", "BOTTOM LTD", pscs=[child]),
        banned,
        blank,
    )
    ids = sorted(n["id"] for n in out["nodes"] if n["type"] == "person")
    assert ids == [
        "person:ban",
        "person:blank",
        "person:same-name",
        "person:same-name-2",
    ]
    assert "notify_instantly" in node(out, "company:40000002")["flags"]
    assert "notify_instantly" in node(out, "person:blank")["flags"]
    assert node(out, "person:blank")["degree"] == 1  # the nameless company only
    assert node(out, "person:ban")["meta"]["officer_ids"] == ["ban", "ban-2"]
    assert node(out, "company:40000002")["label"] == "MID CO LTD"
    assert node(out, "company:40000009")["label"] == "40000009"
    # Only the whole chain is reported, not its inner parts.
    assert texts(out, "ownership-chain") == [
        "Top Co Ltd → MID CO LTD → LOW CO LTD → BOTTOM LTD"
    ]
    assert texts(out, "disqualified") == []
    assert texts(out, "link-broken") == []
    # A weighted line: the notify-instantly company outranks the others.
    parents = [x for x in out["interesting"] if x["rule"] == "unwatched-parent"]
    assert parents[0]["text"].startswith("Top Co Ltd (40000001) controls MID CO LTD")


def test_companies_without_profile_or_datasets_are_skipped() -> None:
    """A company not yet fetched, or watched without officers or PSCs, is harmless."""
    unfetched = company("10000009", "PENDING LTD")
    unfetched.profile.data = None
    out = graph(unfetched, company("10000001", "BARE LTD", datasets=False))
    assert [n["id"] for n in out["nodes"]] == ["company:10000001"]
    assert out["edges"] == []
    assert out["interesting"] == []
    assert out["summary"]["components"] == 1


# ---------------------------------------------------------------- the report


def test_digest_renders_connections() -> None:
    """The report gets a Connections section, in HTML and in text."""
    digest = {
        **DIGEST,
        "connections": [
            {
                "rule": "risk-next-door",
                "severity": "high",
                "text": "Laura Price, a director of X, also runs Y, which is in liquidation",
                "link": "https://example.invalid/company/1",
                "links": [{"text": "X", "href": "https://example.invalid/company/1"}],
            },
            {
                "rule": "shared-office",
                "severity": "low",
                "text": "2 watched companies share the registered office at Z",
                "link": "",
                "links": [],
            },
        ],
    }
    html = render_html(digest, title="T")
    assert "Connections" in html
    assert "Who sits with whom and who owns what" in html
    assert "background:#b91c1c" in html
    assert "background:#9ca3af" in html
    assert 'href="https://example.invalid/company/1"' in html
    text = render_text(digest, title="T")
    assert "Connections:\n  - Laura Price, a director of X" in text
    assert "Connections" not in render_html(DIGEST, title="T")
    assert "Connections:" not in render_text(DIGEST, title="T")


# ---------------------------------------------------------------- with Home Assistant


def _www(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("www", DOMAIN))


async def test_connections_action_builds_the_map_and_saves_it(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """The action returns the graph from the fixtures and, with save, writes the page."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    await setup_entry(list(COMPANY_FIXTURES), officers=True, close_watch={"12345678"})
    for name in ("connections.html", "connections.json"):
        (_www(hass) / name).unlink(missing_ok=True)
    response = await hass.services.async_call(
        DOMAIN, "connections", {}, blocking=True, return_response=True
    )
    assert response is not None
    assert response["period_days"] == 7
    summary = response["summary"]
    assert summary["watched_companies"] == 6
    assert summary["followed_people"] == 1
    assert summary["live_connections"] == 2
    nodes = {n["id"]: n for n in response["nodes"]}
    assert nodes["company:12345678"]["status"] == "own"
    assert nodes["company:12345678"]["meta"]["registered_office"].startswith("47 High")
    assert nodes["company:34567890"]["flags"] == ["insolvent"]
    assert nodes["company:45678901"]["flags"] == ["strike_off"]
    assert nodes["company:23456789"]["flags"] == ["dissolved"]
    assert nodes["person:officer-jane"]["status"] == "followed"
    assert nodes["person:officer-jane"]["label"] == "Jane Elizabeth Smith"
    assert nodes["person:officer-jane"]["degree"] == 22
    assert nodes["company:11111111"]["label"] == "ACME SECRETARIES LIMITED"
    assert nodes["company:09876543"]["status"] == "external"
    # Jane's PSC record joined her officer record; the LLP owner joined too.
    assert "person:mr-robert-taylor" not in nodes
    assert "person:mrs-sarah-white" not in nodes
    edges = {e["id"]: e for e in response["edges"]}
    assert edges["psc-jane"]["source"] == "person:officer-jane"
    assert edges["psc-jane"]["share_band"] == "75-100"
    assert edges["appt-4"]["source"] == "company:11111111"
    assert "appt-3" not in edges  # resigned, left out by default
    assert "app-21" not in edges
    assert "charge:chg-2:0" not in edges  # satisfied
    assert edges["charge:chg-1:0"]["source"] == "entity:hsbc-uk-bank-plc"
    assert [x["rule"] for x in response["interesting"]] == [
        "unwatched-parent",
        "shared-office",
    ]
    assert "path" not in response
    assert not (_www(hass) / "connections.json").exists()

    response = await hass.services.async_call(
        DOMAIN,
        "connections",
        {"save": True, "include_resigned": True, "days": 30},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    edges = {e["id"]: e for e in response["edges"]}
    assert "appt-3" in edges
    assert edges["appt-3"]["active"] is False
    assert Path(response["path"]) == _www(hass) / "connections.html"
    assert response["url"].endswith("/local/companies_house/connections.html")
    shell = (_www(hass) / "connections.html").read_text(encoding="utf-8")
    assert "fetch('connections.json', { cache: 'no-store' })" in shell
    assert "<script src" not in shell
    assert "http" not in shell.replace("http://www.w3.org/2000/svg", "")
    saved = json.loads((_www(hass) / "connections.json").read_text(encoding="utf-8"))
    assert saved == {k: v for k, v in response.items() if k not in ("path", "url")}


async def test_connections_action_reports_a_write_failure(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """A folder that cannot be written is a clear error, not a traceback."""
    await setup_entry(["12345678"])
    with (
        patch(
            "custom_components.companies_house.services.async_write_files",
            side_effect=OSError("read-only"),
        ),
        pytest.raises(HomeAssistantError) as excinfo,
    ):
        await hass.services.async_call(
            DOMAIN, "connections", {"save": True}, blocking=True, return_response=True
        )
    assert excinfo.value.translation_key == "document_write_failed"


async def test_llm_tool_gets_a_trimmed_map(
    hass: HomeAssistant, setup_entry: Callable[..., Any]
) -> None:
    """An assistant asking for the map gets the counts and the lines only."""
    await setup_entry(list(COMPANY_FIXTURES), officers=True)
    context = llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="conversation",
        device_id=None,
    )
    api = next(a for a in llm.async_get_apis(hass) if a.id == DOMAIN)
    instance = await api.async_get_api_instance(context)
    assert "who sits with whom" in instance.api_prompt
    tool = next(t for t in instance.tools if t.name == "companies_house_connections")
    result = await tool.async_call(
        hass,
        llm.ToolInput(tool_name="companies_house_connections", tool_args={}),
        context,
    )
    assert set(result["result"]) == {
        "generated_at",
        "period_days",
        "summary",
        "interesting",
    }
    assert result["result"]["summary"]["watched_companies"] == 6
    assert result["result"]["interesting"][0]["severity"] == "medium"
    assert set(result["result"]["interesting"][0]) == {"severity", "text", "link"}


async def test_sensor_and_debounced_rebuild(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The sensor has a state at setup; changes rebuild the map once, later, on disk."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(["12345678", "56789012"], officers=True)
    for name in ("connections.html", "connections.json"):
        (_www(hass) / name).unlink(missing_ok=True)
    coordinator = entry.runtime_data.connections
    assert coordinator is not None
    state = hass.states.get("sensor.companies_house_connections")
    assert state is not None
    assert state.state == "2"
    assert state.attributes["watched_companies"] == 2
    assert state.attributes["written_at"] is None
    assert state.attributes["url"].endswith("/local/companies_house/connections.html")
    assert state.attributes["interesting"][0].startswith("medium: Parent Holdings")
    assert coordinator.pending_reason == "setup"

    # Shortly after setup the files are written, once.
    freezer.tick(INITIAL_WRITE_DELAY + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.pending_reason is None
    assert (_www(hass) / "connections.json").exists()
    state = hass.states.get("sensor.companies_house_connections")
    assert state is not None
    written = state.attributes["written_at"]
    assert written is not None
    assert Path(state.attributes["path"]) == _www(hass) / "connections.html"

    # A filing event is not about the map; nothing is armed.
    hass.bus.async_fire(
        EVENT_COMPANIES_HOUSE, {"kind": "filing", "event_type": "accounts"}
    )
    await hass.async_block_till_done()
    assert coordinator.pending_reason is None

    # An officer change arms one timer; the second event rides along.
    company = entry.runtime_data.companies["sub_12345678"]
    officers = load_fixture("company_active/officers")
    officers["items"].append(
        {
            **officers["items"][1],
            "name": "NEWMAN, Nina",
            "appointed_on": "2026-09-14",
            "date_of_birth": {"month": 1, "year": 1990},
            "links": {
                "officer": {"appointments": "/officers/officer-nina/appointments"},
                "self": "/company/12345678/appointments/appt-nina",
            },
        }
    )
    aioclient_mock.clear_requests()
    mock_company(aioclient_mock, "12345678", overrides={"officers": officers})
    from custom_components.companies_house.const import Dataset

    await company.async_refresh_datasets([Dataset.OFFICERS], reason="test")
    await hass.async_block_till_done()
    assert coordinator.pending_reason == "officer appointed"
    hass.bus.async_fire(EVENT_COMPANIES_HOUSE, {"kind": "psc", "event_type": "ceased"})
    await hass.async_block_till_done()
    assert coordinator.pending_reason == "officer appointed"
    assert (
        hass.states.get("sensor.companies_house_connections").attributes["edges"] == 32
    )

    freezer.tick(REBUILD_DEBOUNCE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.companies_house_connections")
    assert state is not None
    assert state.attributes["edges"] == 33
    assert state.attributes["new_connections"] == 1
    assert state.attributes["written_at"] > written
    saved = json.loads((_www(hass) / "connections.json").read_text(encoding="utf-8"))
    assert "appt-nina" in {e["id"] for e in saved["edges"]}
    assert "person:officer-nina" in {n["id"] for n in saved["nodes"]}

    # Nothing changed: the timer fires but the files are left alone.
    written = state.attributes["written_at"]
    coordinator.request_rebuild("test")
    freezer.tick(REBUILD_DEBOUNCE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.companies_house_connections").attributes["written_at"]
        == written
    )

    # A manual refresh always rewrites; a failed write keeps the old path.
    with patch(
        "custom_components.companies_house.connections.async_write_files",
        side_effect=OSError("disk full"),
    ):
        await coordinator.async_refresh()
    state = hass.states.get("sensor.companies_house_connections")
    assert state is not None
    assert state.attributes["written_at"] == written
    assert Path(state.attributes["path"]) == _www(hass) / "connections.html"
    await coordinator.async_refresh()
    assert (
        hass.states.get("sensor.companies_house_connections").attributes["written_at"]
        > written
    )

    # Unloading drops the pending timer and the listeners.
    coordinator.request_rebuild("test")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert coordinator.pending_reason == "test"
    freezer.tick(REBUILD_DEBOUNCE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.companies_house_connections").state == "unavailable"


async def test_adding_a_company_rebuilds_the_map(
    hass: HomeAssistant,
    setup_entry: Callable[..., Any],
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A company added in settings reaches the map without a reload."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(["12345678"])
    coordinator = entry.runtime_data.connections
    assert coordinator is not None
    freezer.tick(INITIAL_WRITE_DELAY + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert coordinator.pending_reason is None
    mock_company(aioclient_mock, "56789012")
    data = company_subentry("56789012", subentry_id="sub_56789012")
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(dict(data["data"])),
            subentry_type=data["subentry_type"],
            title=data["title"],
            unique_id=data["unique_id"],
            subentry_id="sub_56789012",
        ),
    )
    await hass.async_block_till_done()
    assert coordinator.pending_reason == "new company or person"
    freezer.tick(REBUILD_DEBOUNCE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.companies_house_connections")
    assert state is not None
    assert state.attributes["watched_companies"] == 2
    assert state.attributes["interesting"][0].startswith("medium: Parent Holdings")

    # Removing it is only ever noticed through the settings listener.
    hass.config_entries.async_remove_subentry(entry, "sub_56789012")
    await hass.async_block_till_done()
    assert coordinator.pending_reason == "settings changed"
    freezer.tick(REBUILD_DEBOUNCE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert (
        hass.states.get("sensor.companies_house_connections").attributes[
            "watched_companies"
        ]
        == 1
    )


async def test_digest_action_carries_connections(
    hass: HomeAssistant, setup_entry: Callable[..., Any], freezer: FrozenDateTimeFactory
) -> None:
    """The report lists the connection lines, leaving out companies opted out."""
    freezer.move_to("2026-09-15T09:00:00+00:00")
    entry = await setup_entry(["12345678", "56789012"], officers=True)
    response = await hass.services.async_call(
        DOMAIN, "digest", {"days": 7}, blocking=True, return_response=True
    )
    assert response is not None
    assert response["summary"]["connections"] == 2
    assert [c["rule"] for c in response["connections"]] == [
        "unwatched-parent",
        "shared-office",
    ]
    assert (
        response["connections"][0]["links"][0]["text"] == "SUBSIDIARY SERVICES LIMITED"
    )
    assert "Connections" in response["html"]
    assert "Connections:" in response["text"]

    entry.runtime_data.companies["sub_56789012"].in_weekly_report = False
    response = await hass.services.async_call(
        DOMAIN, "digest", {"days": 7}, blocking=True, return_response=True
    )
    assert response is not None
    assert response["connections"] == []
    assert response["summary"]["connections"] == 0
    assert "Connections" not in response["html"]
    assert build_connections(entry)["summary"]["watched_companies"] == 2
