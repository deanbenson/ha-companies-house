"""Who sits with whom, who owns what: the connections map.

Everything here is built from what is already in memory, so drawing the map
costs no API requests. The graph joins the officer lists and PSC lists of
every watched company with the appointment lists of every followed person,
then a list of rules picks out the connections worth knowing about (shared
boards, ownership chains, a director who also runs a company in
liquidation, and so on).

Identity is the hard part. A followed person is one node however many
register records they have; other officers are keyed by their register
record; a PSC has no record id, so an individual PSC is joined to an officer
by surname, first forename and month and year of birth (the same test the
disqualification check uses), and a corporate PSC or corporate officer by
its UK company number.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later, async_track_utc_time_change
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .connections_shell import SHELL_HTML
from .const import (
    DOMAIN,
    EVENT_COMPANIES_HOUSE,
    FINISHED_STATUSES,
    INSOLVENT_STATUSES,
    LOGGER,
)
from .coordinator import (
    _TITLES,
    _normalise_name,
    signal_new_company,
    signal_new_officer,
)
from .digest import (
    _company_link,
    _company_weight,
    _control_words,
    _officer_link,
    _pretty_date,
    _role,
    _status_phrase,
    logo_for,
)
from .enumerations import OFFICER_ROLE
from .models import DateOfBirth, JsonDict, display_name, stable_hash

if TYPE_CHECKING:
    from .coordinator import CompaniesHouseConfigEntry, CompanyRuntime, OfficerRuntime

# Which change events can alter the map, and how long to wait for a burst of
# them (a weekly reconciliation touches every company) to settle.
REBUILD_KINDS = frozenset(
    {"officer", "psc", "appointment", "status", "profile", "charge"}
)
REBUILD_DEBOUNCE = timedelta(seconds=60)
# "New this week" is measured in whole days, so the map is also rebuilt once a
# day, just after midnight UTC, for the window to roll on when nothing happens.
DAY_ROLLOVER_MINUTE = 1
# The first write after setup waits a little so the files never slow setup.
INITIAL_WRITE_DELAY = timedelta(seconds=30)
DEFAULT_DAYS = 7
SHELL_FILENAME = "connections.html"
DATA_FILENAME = "connections.json"
# How many lines the sensor carries; the files and the action carry them all.
SENSOR_LINES_CAP = 20
CHAIN_LINES_CAP = 10

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
# Whole words that name a UK register. "Wales" on its own is fine; "New South
# Wales" is not. "Companies Act" is deliberately absent: Ireland and the Isle
# of Man have one too.
_UK_PLACE_RE = re.compile(
    r"\b(?:england|scotland|northern ireland|united kingdom|great britain"
    r"|companies house|uk)\b|(?<!south )\bwales\b"
)
# Letters whose names start with a vowel sound, for "an LLP member".
_AN_LETTERS = "AEFHILMNORSX"
_COMPANY_NUMBER_RE = re.compile(r"^([A-Z]{0,2})(\d{5,8})$")
_DIRECTOR_ROLES = frozenset({"director", "llp-designated-member", "llp-member"})
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.casefold()).strip("-") or "unnamed"


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _company_number(value: str | None) -> str | None:
    """Return a normalised UK company number, or None when it is not one.

    Registration numbers in PSC and corporate officer records come as typed
    by the filer: unpadded, lower case, with spaces. The same rule as the
    actions' ``normalise_company_number``, minus the error.
    """
    if not value:
        return None
    match = _COMPANY_NUMBER_RE.match(value.strip().upper().replace(" ", ""))
    if not match:
        return None
    prefix, digits = match.groups()
    return prefix + digits.zfill(8 - len(prefix))


def _uk_registration(identification: dict[str, str]) -> str | None:
    """Return the company number a corporate PSC or officer is registered under.

    Only when the record says it is a UK company: overseas numbers look the
    same but mean nothing on this register.
    """
    number = _company_number(identification.get("registration_number"))
    if number is None:
        return None
    if identification.get("identification_type", "").startswith("uk-"):
        return number
    # A place or country that is given but names somewhere else is decisive,
    # whatever the legal authority says; only a blank one falls back to it.
    where = " ".join(
        identification.get(k, "") for k in ("place_registered", "country_registered")
    ).strip()
    if not where:
        where = identification.get("legal_authority", "")
    return number if _UK_PLACE_RE.search(where.casefold()) else None


def _share_band(natures: Iterable[str]) -> str | None:
    for nature in natures:
        if nature.startswith("ownership-of-shares-"):
            band = nature.removeprefix("ownership-of-shares-").split("-percent")[0]
            return band.replace("-to-", "-")
    return None


def _company_name_key(name: str) -> str:
    """Casefold a company name so ``Example Trading Ltd`` matches the register."""
    words = re.sub(r"[^a-z0-9 ]", " ", name.casefold()).split()
    replaced = [
        {"limited": "ltd", "public": "plc", "company": "", "the": ""}.get(w, w)
        for w in words
    ]
    return " ".join(w for w in replaced if w)


def _person_label(name: str) -> str:
    """Show a person's name without the title a PSC record carries."""
    words = name.split()
    while words and words[0].rstrip(".").casefold() in _TITLES:
        words.pop(0)
    return display_name(" ".join(words) or name)


def _list_names(names: list[str], *, cap: int = 3) -> str:
    """Join names as people write them: "A", "A and B", "A, B and C"."""
    shown = names[:cap]
    more = len(names) - len(shown)
    if more > 0:
        shown = [*shown, f"{more} more"]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + " and " + shown[-1]


# ---------------------------------------------------------------- the graph


@dataclass
class _Line:
    rule: str
    severity: str
    text: str
    node_ids: list[str]
    edge_ids: list[str]
    link: str
    links: list[JsonDict]
    weight: int = 1

    def to_dict(self) -> JsonDict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "text": self.text,
            "node_ids": self.node_ids,
            "edge_ids": self.edge_ids,
            "link": self.link,
            "links": self.links,
        }


class _Graph:
    """Nodes and edges plus the identity indexes used while building."""

    def __init__(self, since_day: date) -> None:
        self.since_day = since_day
        self.nodes: dict[str, JsonDict] = {}
        self.edges: dict[str, JsonDict] = {}
        self.weights: dict[str, int] = {}
        self.record_to_node: dict[str, str] = {}
        self._by_dob_key: dict[tuple[str, str, int, int], str] = {}
        self._by_name_key: dict[tuple[str, str], str] = {}
        self._node_dob: dict[str, tuple[str, str, int, int]] = {}
        self._company_names: dict[str, str] = {}

    # -- nodes ------------------------------------------------------------

    def add_node(self, node_id: str, **fields: Any) -> JsonDict:
        node = self.nodes.get(node_id)
        if node is None:
            node = {
                "id": node_id,
                "type": "entity",
                "label": "",
                "status": "external",
                "company_status": None,
                "flags": [],
                "link": "",
                "degree": 0,
                "number": None,
                "meta": {},
            }
            self.nodes[node_id] = node
        for key, value in fields.items():
            if key == "flags":
                for flag in value:
                    if flag not in node["flags"]:
                        node["flags"].append(flag)
            elif key == "meta":
                node["meta"].update(value)
            elif value is not None and (value != "" or not node[key]):
                node[key] = value
        return node

    def company_node(
        self,
        number: str,
        *,
        name: str | None = None,
        status: str | None = None,
        company_status: str | None = None,
    ) -> JsonDict:
        """Return the node for a company number, creating an external one.

        A watched company (``status`` given) sets its own name and status;
        a mention of it elsewhere (a corporate PSC, an appointment) never
        overwrites those.
        """
        node_id = f"company:{number}"
        node = self.nodes.get(node_id)
        if node is not None and node["status"] != "external" and status is None:
            if name:
                self._company_names[_company_name_key(name)] = node_id
            return node
        node = self.add_node(
            node_id,
            type="company",
            label=name or "",
            number=number,
            link=_company_link(number),
            company_status=company_status,
        )
        if status:
            node["status"] = status
        if not node["label"]:
            node["label"] = number
        if name:
            self._company_names[_company_name_key(name)] = node_id
        self.flag_status(node)
        return node

    def flag_status(self, node: JsonDict) -> None:
        """Flag a company node from its status: insolvent, dissolved."""
        status = node.get("company_status")
        if status in INSOLVENT_STATUSES:
            self.add_node(node["id"], flags=["insolvent"])
        if status in FINISHED_STATUSES:
            self.add_node(node["id"], flags=["dissolved"])

    def entity_node(self, name: str, *, flag: str) -> JsonDict:
        """Return the node for something outside the register: a lender, a foreign parent."""
        return self.add_node(
            f"entity:{_slug(name)}", type="entity", label=name, flags=[flag]
        )

    def person_node(
        self,
        name: str,
        date_of_birth: DateOfBirth | None,
        *,
        officer_id: str | None = None,
        followed: bool = False,
        display: str | None = None,
    ) -> JsonDict:
        """Return the node for a person, joining records of the same human.

        Precedence: a register record already seen; then surname, first
        forename and month and year of birth; then the name alone, but only
        to join a record that has no id of its own (a person with
        significant control) and only when one side has no date of birth to
        check. Two register records are never joined by name alone: that is
        how a secretary, who has no date of birth on the register, would be
        mistaken for a director who shares their name.
        """
        name_key = _normalise_name(name)
        dob_key = (
            (*name_key, date_of_birth.month, date_of_birth.year)
            if date_of_birth is not None
            and date_of_birth.month is not None
            and date_of_birth.year is not None
            and name_key[0]
            else None
        )
        node_id = None
        if officer_id and officer_id in self.record_to_node:
            node_id = self.record_to_node[officer_id]
        elif dob_key is not None and dob_key in self._by_dob_key:
            node_id = self._by_dob_key[dob_key]
        elif name_key[0] and name_key in self._by_name_key:
            candidate = self._by_name_key[name_key]
            two_records = bool(
                officer_id and self.nodes[candidate]["meta"].get("officer_ids")
            )
            if not two_records and (dob_key is None or candidate not in self._node_dob):
                node_id = candidate
        if node_id is None:
            base = f"person:{officer_id}" if officer_id else f"person:{_slug(name)}"
            node_id = base
            suffix = 2
            while node_id in self.nodes:
                node_id = f"{base}-{suffix}"
                suffix += 1
        node = self.add_node(
            node_id,
            type="person",
            label=display or _person_label(name),
            link=_officer_link(officer_id) if officer_id else "",
        )
        if followed:
            node["status"] = "followed"
        if officer_id:
            self.record_to_node[officer_id] = node_id
            ids = node["meta"].setdefault("officer_ids", [])
            if officer_id not in ids:
                ids.append(officer_id)
        if dob_key is not None:
            self._by_dob_key[dob_key] = node_id
            self._node_dob[node_id] = dob_key
            node["meta"]["date_of_birth"] = f"{dob_key[2]:02d}/{dob_key[3]}"
        if name_key[0]:
            self._by_name_key.setdefault(name_key, node_id)
        return node

    def add_name_alias(self, name: str, node_id: str) -> None:
        """Let another spelling of a person's name find their node."""
        key = _normalise_name(name)
        if key[0]:
            self._by_name_key.setdefault(key, node_id)

    def find_person(self, name: str, officer_id: str | None = None) -> str | None:
        """Look a person up without creating anything (for the change log)."""
        if officer_id and officer_id in self.record_to_node:
            return self.record_to_node[officer_id]
        return self._by_name_key.get(_normalise_name(name))

    def find_by_company_name(self, name: str) -> str | None:
        return self._company_names.get(_company_name_key(name))

    # -- edges ------------------------------------------------------------

    def add_edge(
        self,
        edge_id: str,
        source: str,
        target: str,
        kind: str,
        *,
        active: bool,
        since: date | None,
        until: date | None,
        link: str,
        role: str | None = None,
        control: list[str] | None = None,
    ) -> JsonDict:
        existing = self.edges.get(edge_id)
        if existing is not None:
            return existing
        edge = {
            "id": edge_id,
            "source": source,
            "target": target,
            "kind": kind,
            "role": role,
            "role_label": OFFICER_ROLE.get(role or "", _role(role).capitalize())
            if kind == "officer"
            else None,
            "control": list(control or []),
            "control_label": _control_words(control or []) if control else None,
            "share_band": _share_band(control or []) if control else None,
            "active": active,
            "since": _iso(since),
            "until": _iso(until),
            "new": bool(
                (since is not None and since >= self.since_day)
                or (until is not None and until >= self.since_day)
            ),
            "link": link,
        }
        self.edges[edge_id] = edge
        return edge

    # -- lookups used by the rules ---------------------------------------

    def edges_of(self, node_id: str, *, kind: str | None = None) -> list[JsonDict]:
        return [
            e
            for e in self.edges.values()
            if e["active"]
            and (kind is None or e["kind"] == kind)
            and node_id in (e["source"], e["target"])
        ]

    def is_watched(self, node_id: str) -> bool:
        node = self.nodes.get(node_id)
        return bool(
            node and node["type"] == "company" and node["status"] in ("own", "watched")
        )

    def weight(self, node_ids: Iterable[str]) -> int:
        return max((self.weights.get(n, 1) for n in node_ids), default=1)

    def label(self, node_id: str) -> str:
        return str(self.nodes[node_id]["label"])

    def link(self, node_id: str) -> str:
        return str(self.nodes[node_id]["link"])

    def line(
        self,
        rule: str,
        severity: str,
        text: str,
        node_ids: list[str],
        edge_ids: list[str] | None = None,
    ) -> _Line:
        """Build a line with a link per node involved."""
        links = [
            {"text": self.label(n), "href": self.link(n)}
            for n in dict.fromkeys(node_ids)
            if self.link(n)
        ][:4]
        primary = next(
            (self.link(n) for n in node_ids if self.is_watched(n) and self.link(n)),
            links[0]["href"] if links else "",
        )
        return _Line(
            rule=rule,
            severity=severity,
            text=text,
            node_ids=list(dict.fromkeys(node_ids)),
            edge_ids=list(edge_ids or []),
            link=primary,
            links=links,
            weight=self.weight(node_ids),
        )


def _trouble_phrase(node: JsonDict) -> str:
    """Say what is wrong with a company: "in liquidation", "facing strike-off"."""
    if "strike_off" in node["flags"]:
        return "facing strike-off"
    return _status_phrase(node.get("company_status"))


# ---------------------------------------------------------------- building


def _add_company(graph: _Graph, company: CompanyRuntime) -> None:
    profile = company.profile.data
    assert profile is not None
    number = company.company_number
    node = graph.company_node(
        number,
        name=company.company_name,
        status="own" if company.close_watch else "watched",
        company_status=profile.company_status,
    )
    flags = []
    if company.close_watch:
        flags.append("close_watch")
    if company.notify_instantly:
        flags.append("notify_instantly")
    if company.strike_off_proposed:
        flags.append("strike_off")
    graph.add_node(
        node["id"],
        flags=flags,
        meta={
            "label": company.label,
            "website": company.website,
            "logo": logo_for(company.website),
            "registered_office": profile.registered_office_address.one_line()
            if profile.registered_office_address
            else None,
        },
    )
    graph.weights[node["id"]] = _company_weight(company)
    company_link = _company_link(number)
    # The register counts a role at a dissolved company as inactive, and so
    # does the map; a holding there is no more current.
    finished = profile.company_status in FINISHED_STATUSES

    officers = company.officers.data if company.officers is not None else None
    # Rules that compare the board with the owners need to know whether the
    # board is known at all (the officers dataset can be switched off).
    graph.add_node(node["id"], meta={"officers_known": officers is not None})
    for officer in officers.items if officers else []:
        corporate = bool(officer.identification) or (
            officer.officer_role or ""
        ).startswith("corporate-")
        if corporate:
            registration = _uk_registration(officer.identification)
            other = (
                graph.company_node(registration, name=officer.name)
                if registration
                else graph.entity_node(officer.name, flag="corporate")
            )
            source = other["id"]
        else:
            source = graph.person_node(
                officer.name, officer.date_of_birth, officer_id=officer.officer_id
            )["id"]
            if officer.occupation:
                graph.add_node(source, meta={"occupation": officer.occupation})
        graph.add_edge(
            officer.appointment_id or f"officer:{number}:{_slug(officer.name)}",
            source,
            node["id"],
            "officer",
            active=officer.is_active and not finished,
            since=officer.appointed_on or officer.appointed_before,
            until=officer.resigned_on,
            link=company_link + "/officers",
            role=officer.officer_role,
        )

    psc_data = company.psc.data if company.psc is not None else None
    for psc in psc_data.items if psc_data else []:
        kind = psc.kind or ""
        if "super-secure" in kind:
            continue
        if "corporate-entity" in kind:
            registration = _uk_registration(psc.identification)
            holder = (
                graph.company_node(registration, name=psc.name)
                if registration
                else graph.entity_node(psc.name, flag="corporate")
            )
        elif "legal-person" in kind:
            holder = graph.entity_node(psc.name, flag="legal_person")
        else:
            holder = graph.person_node(psc.name, psc.date_of_birth)
        if psc.is_sanctioned:
            graph.add_node(holder["id"], flags=["sanctioned"])
        graph.add_edge(
            psc.notification_id or f"psc:{number}:{_slug(psc.name)}",
            holder["id"],
            node["id"],
            "psc",
            active=not psc.ceased and not finished,
            since=psc.notified_on,
            until=psc.ceased_on,
            link=company_link + "/persons-with-significant-control",
            control=psc.natures_of_control,
        )


def _add_charges(graph: _Graph, company: CompanyRuntime) -> None:
    """Add lender edges. Runs last, so a lender that is watched or followed is found."""
    charges = company.charges.data if company.charges is not None else None
    number = company.company_number
    for charge in charges.items if charges else []:
        for index, lender in enumerate(charge.persons_entitled):
            lender_id = graph.find_by_company_name(lender) or graph.find_person(lender)
            if lender_id is None:
                lender_id = graph.entity_node(lender, flag="lender")["id"]
            graph.add_edge(
                f"charge:{charge.charge_id or charge.charge_code}:{index}",
                lender_id,
                f"company:{number}",
                "charge",
                active=charge.is_outstanding,
                since=charge.created_on or charge.acquired_on,
                until=charge.satisfied_on,
                link=_company_link(number) + "/charges",
            )


def _add_person(graph: _Graph, officer: OfficerRuntime) -> None:
    node = graph.person_node(
        officer.register_name,
        officer.known_date_of_birth,
        officer_id=officer.officer_id,
        followed=True,
        display=display_name(officer.officer_name),
    )
    for record in officer.officer_ids:
        graph.record_to_node[record] = node["id"]
        ids = node["meta"].setdefault("officer_ids", [])
        if record not in ids:
            ids.append(record)
    if officer.configured_name:
        # The name chosen in settings may differ from the register's.
        graph.add_name_alias(officer.configured_name, node["id"])
    flags = ["notify_instantly"] if officer.notify_instantly else []
    disqualification = officer.disqualification.data
    if disqualification is not None and disqualification.disqualified:
        flags.append("disqualified")
    graph.add_node(node["id"], flags=flags)
    graph.weights[node["id"]] = 2 if officer.notify_instantly else 1
    data = officer.appointments.data
    for appointment in data.items if data else []:
        number = appointment.company_number
        if not number:
            continue
        company = graph.company_node(
            number,
            name=appointment.company_name,
            company_status=appointment.company_status,
        )
        graph.add_edge(
            appointment.appointment_id or f"officer:{number}:{node['id']}",
            node["id"],
            company["id"],
            "officer",
            active=appointment.is_active,
            since=appointment.appointed_on or appointment.appointed_before,
            until=appointment.resigned_on,
            link=_company_link(number) + "/officers",
            role=appointment.officer_role,
        )


def _finish(graph: _Graph) -> None:
    """Fill in degrees and the external-company flags once every edge is in."""
    for node in graph.nodes.values():
        node["degree"] = 0
    for edge in graph.edges.values():
        if edge["active"]:
            graph.nodes[edge["source"]]["degree"] += 1
            graph.nodes[edge["target"]]["degree"] += 1
    for node in graph.nodes.values():
        if node["type"] == "company" and node["status"] == "external":
            graph.add_node(node["id"], flags=["external"])
        if node["type"] == "company" and node.get("company_status") is not None:
            graph.flag_status(node)


# ---------------------------------------------------------------- rules


def _people_at(graph: _Graph, company_id: str) -> dict[str, JsonDict]:
    """Return person node id -> officer edge for the active officers of a company.

    A person often holds two records at one company (director and
    secretary); the director one is the one kept, whatever order the
    register lists them in, so the lines never churn between refreshes.
    """
    board: dict[str, JsonDict] = {}
    edges = sorted(
        (
            e
            for e in graph.edges_of(company_id, kind="officer")
            if e["target"] == company_id
            and graph.nodes[e["source"]]["type"] == "person"
        ),
        key=lambda e: ((e.get("role") or "") not in _DIRECTOR_ROLES, e["id"]),
    )
    for edge in edges:
        board.setdefault(edge["source"], edge)
    return board


def _companies_of(graph: _Graph, person_id: str) -> dict[str, JsonDict]:
    """Return company node id -> officer edge for a person's active roles."""
    return {
        e["target"]: e
        for e in graph.edges_of(person_id, kind="officer")
        if e["source"] == person_id
    }


def _rule_shared_board(graph: _Graph, watched: list[str]) -> list[_Line]:
    boards = {c: _people_at(graph, c) for c in watched}
    out: list[_Line] = []
    for i, a in enumerate(watched):
        for b in watched[i + 1 :]:
            shared = sorted(set(boards[a]) & set(boards[b]), key=graph.label)
            if len(shared) < 2:
                continue
            names = [graph.label(n) for n in shared]
            word = "both" if len(names) == 2 else "all"
            text = (
                f"{_list_names(names)} {word} sit on the boards of "
                f"{graph.label(a)} and {graph.label(b)}"
            )
            joined = {boards[a][n]["since"] for n in shared}
            if len(joined) == 1 and None not in joined:
                text += (
                    f" — {word} joined {graph.label(a)} on {_pretty_date(joined.pop())}"
                )
            out.append(
                graph.line(
                    "shared-board",
                    "medium",
                    text,
                    [a, b, *shared],
                    [boards[a][n]["id"] for n in shared]
                    + [boards[b][n]["id"] for n in shared],
                )
            )
    return out


def _rule_hub_person(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    for node in graph.nodes.values():
        if node["type"] != "person":
            continue
        roles = {
            c: e for c, e in _companies_of(graph, node["id"]).items() if c in watched
        }
        if len(roles) < 3:
            continue
        names = [graph.label(c) for c in sorted(roles, key=graph.label)]
        out.append(
            graph.line(
                "hub-person",
                "medium" if len(roles) >= 5 else "low",
                f"{node['label']} holds roles at {len(roles)} watched companies: "
                f"{_list_names(names, cap=4)}",
                [node["id"], *roles],
                [e["id"] for e in roles.values()],
            )
        )
    return out


def _rule_ownership_hub(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    for node in graph.nodes.values():
        held = {
            e["target"]: e
            for e in graph.edges_of(node["id"], kind="psc")
            if e["source"] == node["id"] and e["target"] in watched
        }
        if len(held) < 2:
            continue
        controls = {e["control_label"] or "significant control" for e in held.values()}
        if len(controls) == 1:
            detail = f"{controls.pop()} in each"
        else:
            detail = "; ".join(
                f"{graph.label(c)}: {e['control_label'] or 'significant control'}"
                for c, e in sorted(held.items(), key=lambda kv: graph.label(kv[0]))
            )
        out.append(
            graph.line(
                "ownership-hub",
                "medium",
                f"{node['label']} controls {len(held)} watched companies ({detail})",
                [node["id"], *held],
                [e["id"] for e in held.values()],
            )
        )
    return out


def _rule_ownership_chain(graph: _Graph, watched: list[str]) -> list[_Line]:
    """Chains of control two or more hops long, longest first, no sub-chains."""
    holders: dict[str, list[tuple[str, str]]] = {}
    for edge in graph.edges.values():
        if edge["active"] and edge["kind"] == "psc":
            holders.setdefault(edge["target"], []).append((edge["source"], edge["id"]))
    chains: list[tuple[list[str], list[str]]] = []

    def walk(path: list[str], edge_ids: list[str]) -> None:
        for holder, edge_id in holders.get(path[0], []):
            if holder in path or graph.nodes[holder]["type"] == "entity":
                continue
            longer = [holder, *path]
            if graph.nodes[holder]["type"] == "company":
                walk(longer, [edge_id, *edge_ids])
            if len(longer) >= 3:
                chains.append((longer, [edge_id, *edge_ids]))

    for bottom in watched:
        walk([bottom], [])
    chains.sort(key=lambda c: (-len(c[0]), c[0]))
    kept: list[tuple[list[str], list[str]]] = []
    for chain, edge_ids in chains:
        if any(_is_subchain(chain, longer) for longer, _ in kept):
            continue
        kept.append((chain, edge_ids))
    out: list[_Line] = []
    for chain, edge_ids in kept[:CHAIN_LINES_CAP]:
        top, bottom = chain[0], chain[-1]
        if graph.nodes[top]["type"] == "person":
            through = _list_names([graph.label(n) for n in chain[1:-1]])
            out.append(
                graph.line(
                    "ultimate-owner",
                    "medium",
                    f"{graph.label(top)} ultimately controls {graph.label(bottom)} "
                    f"through {through}",
                    chain,
                    edge_ids,
                )
            )
        else:
            out.append(
                graph.line(
                    "ownership-chain",
                    "medium",
                    " → ".join(graph.label(n) for n in chain),
                    chain,
                    edge_ids,
                )
            )
    return out


def _is_subchain(short: list[str], longer: list[str]) -> bool:
    n = len(short)
    return any(longer[i : i + n] == short for i in range(len(longer) - n + 1))


def _rule_risk_next_door(graph: _Graph, watched: list[str]) -> list[_Line]:
    troubled = _troubled(graph)
    out: list[_Line] = []
    for node in graph.nodes.values():
        if node["type"] != "person":
            continue
        roles = _companies_of(graph, node["id"])
        safe = [c for c in roles if c in watched and c not in troubled]
        if not safe:
            continue
        for trouble in sorted(roles, key=graph.label):
            if trouble not in troubled:
                continue
            company = safe[0]
            out.append(
                graph.line(
                    "risk-next-door",
                    "high",
                    f"{node['label']}, {_a_role(roles[company])} of "
                    f"{graph.label(company)}, also runs {graph.label(trouble)}, "
                    f"which is {_trouble_phrase(graph.nodes[trouble])}",
                    [company, trouble, node["id"]],
                    [roles[company]["id"], roles[trouble]["id"]],
                )
            )
    return out


def _troubled(graph: _Graph) -> set[str]:
    return {
        n["id"]
        for n in graph.nodes.values()
        if n["type"] == "company"
        and ("insolvent" in n["flags"] or "strike_off" in n["flags"])
    }


def _a_role(edge: JsonDict) -> str:
    """Say a role with its article: "a director", "an LLP member"."""
    words = str(edge.get("role_label") or "officer").split()
    role = " ".join(w if w.isupper() else w.lower() for w in words)
    first = words[0]
    vowel_sound = (
        first[0] in _AN_LETTERS if first.isupper() else first[0].lower() in "aeiou"
    )
    return ("an " if vowel_sound else "a ") + role


def _rule_disqualified(graph: _Graph) -> list[_Line]:
    out: list[_Line] = []
    for node in graph.nodes.values():
        if "disqualified" not in node["flags"]:
            continue
        roles = _companies_of(graph, node["id"])
        if not roles:
            continue
        names = [graph.label(c) for c in sorted(roles, key=graph.label)]
        out.append(
            graph.line(
                "disqualified",
                "high",
                f"{node['label']} is disqualified as a director but still holds "
                f"{len(roles)} active role{'s' if len(roles) > 1 else ''}, at "
                f"{_list_names(names)}",
                [node["id"], *roles],
                [e["id"] for e in roles.values()],
            )
        )
    return out


def _rule_sanctioned(graph: _Graph) -> list[_Line]:
    out: list[_Line] = []
    for node in graph.nodes.values():
        if "sanctioned" not in node["flags"]:
            continue
        for edge in graph.edges_of(node["id"], kind="psc"):
            company = edge["target"]
            out.append(
                graph.line(
                    "sanctioned",
                    "high",
                    f"{node['label']}, who controls {graph.label(company)} "
                    f"({edge['control_label'] or 'significant control'}), "
                    "is on the UK sanctions list",
                    [company, node["id"]],
                    [edge["id"]],
                )
            )
    return out


def _rule_simultaneous_moves(graph: _Graph, watched: list[str]) -> list[_Line]:
    """Two or more people appointed to the same companies on the same day."""
    by_day: dict[str, dict[str, set[str]]] = {}
    for edge in graph.edges.values():
        if (
            edge["kind"] != "officer"
            or not edge["active"]
            or not edge["since"]
            or edge["since"] < graph.since_day.isoformat()
            or edge["target"] not in watched
            or graph.nodes[edge["source"]]["type"] != "person"
        ):
            continue
        by_day.setdefault(edge["since"], {}).setdefault(edge["source"], set()).add(
            edge["target"]
        )
    out: list[_Line] = []
    for day, people in sorted(by_day.items()):
        groups: dict[frozenset[str], list[str]] = {}
        for person, companies in people.items():
            groups.setdefault(frozenset(companies), []).append(person)
        for joined, members in groups.items():
            if len(members) < 2:
                continue
            members.sort(key=graph.label)
            names = [graph.label(p) for p in members]
            where = [graph.label(c) for c in sorted(joined, key=graph.label)]
            out.append(
                graph.line(
                    "simultaneous-moves",
                    "medium",
                    f"{_list_names(names)} {'both' if len(names) == 2 else 'all'} "
                    f"joined {_list_names(where)} on {_pretty_date(day)}",
                    [*joined, *members],
                )
            )
    return out


class _Components:
    """Union-find over node ids."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, node: str) -> str:
        self.parent.setdefault(node, node)
        while self.parent[node] != node:
            self.parent[node] = self.parent[self.parent[node]]
            node = self.parent[node]
        return node

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)

    def members(self, node: str) -> set[str]:
        root = self.find(node)
        return {n for n in self.parent if self.find(n) == root}


def _rule_new_link(graph: _Graph) -> list[_Line]:
    """Spot a new role or holding that joins two previously separate groups."""
    components = _Components()
    fresh: list[JsonDict] = []
    for edge in sorted(graph.edges.values(), key=lambda e: e["id"]):
        if not edge["active"] or edge["kind"] == "charge":
            continue
        if edge["new"]:
            fresh.append(edge)
        else:
            components.union(edge["source"], edge["target"])
    out: list[_Line] = []
    for edge in fresh:
        source, target = edge["source"], edge["target"]
        if components.find(source) == components.find(target):
            continue
        others = sorted(
            (
                n
                for n in components.members(source)
                if graph.nodes[n]["type"] == "company" and n != target
            ),
            key=lambda n: (not graph.is_watched(n), graph.label(n)),
        )
        components.union(source, target)
        if not others:
            continue
        out.append(
            graph.line(
                "new-link",
                "low",
                f"{graph.label(source)} now links {graph.label(target)} to "
                f"{graph.label(others[0])}",
                [target, others[0], source],
                [edge["id"]],
            )
        )
    return out


def _rule_link_broken(
    graph: _Graph, companies: list[CompanyRuntime], since: datetime
) -> list[_Line]:
    """Spot a resignation or ceased holding this period that cut a company loose."""
    out: list[_Line] = []
    seen: set[tuple[str, str]] = set()
    for company in companies:
        company_id = f"company:{company.company_number}"
        for change in company.state.changes:
            at = dt_util.parse_datetime(str(change.get("at") or ""))
            if at is None or at < since:
                continue
            kind, event_type = change.get("kind"), change.get("event_type")
            if (kind, event_type) not in (("officer", "resigned"), ("psc", "ceased")):
                continue
            payload = change.get("payload") or {}
            person = graph.find_person(
                str(payload.get("name") or ""), payload.get("officer_id")
            )
            if person is None or (person, company_id) in seen:
                continue
            seen.add((person, company_id))
            still = {
                n
                for e in graph.edges_of(person)
                for n in (e["source"], e["target"])
                if n != person
            }
            if company_id in still:
                continue
            others = sorted((n for n in still if graph.is_watched(n)), key=graph.label)
            if not others:
                continue
            out.append(
                graph.line(
                    "link-broken",
                    "low",
                    f"{graph.label(company_id)} and {graph.label(others[0])} are no "
                    f"longer connected through {graph.label(person)}",
                    [company_id, others[0], person],
                )
            )
    return out


def _rule_shared_office(graph: _Graph, watched: list[str]) -> list[_Line]:
    offices: dict[str, list[str]] = {}
    for company in watched:
        office = graph.nodes[company]["meta"].get("registered_office")
        if office and "dissolved" not in graph.nodes[company]["flags"]:
            offices.setdefault(office.casefold(), []).append(company)
    out: list[_Line] = []
    for members in offices.values():
        if len(members) < 2:
            continue
        members.sort(key=graph.label)
        office = graph.nodes[members[0]]["meta"]["registered_office"]
        names = [graph.label(c) for c in members]
        out.append(
            graph.line(
                "shared-office",
                "low",
                f"{len(members)} watched companies share the registered office at "
                f"{office}: {_list_names(names, cap=5)}",
                members,
            )
        )
    return out


def _rule_unwatched_connected(graph: _Graph) -> list[_Line]:
    out: list[_Line] = []
    for node in graph.nodes.values():
        if node["type"] != "company" or node["status"] != "external":
            continue
        if "dissolved" in node["flags"]:
            continue
        followed = sorted(
            {
                p
                for p, e in _people_at(graph, node["id"]).items()
                if graph.nodes[p]["status"] == "followed"
            },
            key=graph.label,
        )
        if len(followed) < 2:
            continue
        names = [graph.label(p) for p in followed]
        out.append(
            graph.line(
                "unwatched-connected",
                "low",
                f"{node['label']} is not watched but {_list_names(names)}, whom you "
                "follow, both hold roles there"
                if len(names) == 2
                else f"{node['label']} is not watched but {len(names)} people you "
                "follow hold roles there",
                [node["id"], *followed],
            )
        )
    return out


def _rule_unwatched_parent(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    for company in watched:
        for edge in graph.edges_of(company, kind="psc"):
            holder = graph.nodes[edge["source"]]
            if edge["target"] != company or holder["type"] == "person":
                continue
            control = edge["control_label"] or "significant control"
            if holder["type"] == "company" and holder["status"] == "external":
                out.append(
                    graph.line(
                        "unwatched-parent",
                        "medium",
                        f"{holder['label']} ({holder['number']}) controls "
                        f"{graph.label(company)} ({control}) but is not watched",
                        [company, holder["id"]],
                        [edge["id"]],
                    )
                )
            elif holder["type"] == "entity" and "corporate" in holder["flags"]:
                out.append(
                    graph.line(
                        "unwatched-parent",
                        "low",
                        f"{holder['label']} controls {graph.label(company)} "
                        f"({control}) and is not on the UK register, so it cannot "
                        "be watched",
                        [company, holder["id"]],
                        [edge["id"]],
                    )
                )
    return out


def _rule_owner_not_on_board(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    for company in watched:
        if not graph.nodes[company]["meta"].get("officers_known"):
            continue  # No board to compare the owners with.
        board = _people_at(graph, company)
        owners = {
            e["source"]: e
            for e in graph.edges_of(company, kind="psc")
            if e["target"] == company and graph.nodes[e["source"]]["type"] == "person"
        }
        for owner, edge in sorted(owners.items(), key=lambda kv: graph.label(kv[0])):
            if owner in board:
                continue
            out.append(
                graph.line(
                    "owner-not-on-board",
                    "low",
                    f"{graph.label(owner)} controls {graph.label(company)} "
                    f"({edge['control_label'] or 'significant control'}) but holds "
                    "no role there",
                    [company, owner],
                    [edge["id"]],
                )
            )
        directors = [
            p for p, e in board.items() if (e.get("role") or "") in _DIRECTOR_ROLES
        ]
        if len(directors) == 1 and owners and directors[0] not in owners:
            director = directors[0]
            names = [graph.label(o) for o in sorted(owners, key=graph.label)]
            out.append(
                graph.line(
                    "director-not-owner",
                    "low",
                    f"{graph.label(director)} is the only director of "
                    f"{graph.label(company)} but not a person with significant "
                    f"control; {_list_names(names)} {'is' if len(names) == 1 else 'are'}",
                    [company, director, *owners],
                    [board[director]["id"]],
                )
            )
    return out


def _rule_intra_group_lender(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    for company in watched:
        for edge in graph.edges_of(company, kind="charge"):
            lender = graph.nodes[edge["source"]]
            if edge["target"] != company or lender["status"] == "external":
                continue
            when = f" (created {_pretty_date(edge['since'])})" if edge["since"] else ""
            out.append(
                graph.line(
                    "intra-group-lender",
                    "low",
                    f"{graph.label(company)} has a charge in favour of "
                    f"{lender['label']}{when}",
                    [company, lender["id"]],
                    [edge["id"]],
                )
            )
    return out


def _rule_reciprocal_control(graph: _Graph, watched: list[str]) -> list[_Line]:
    out: list[_Line] = []
    boards = {c: _people_at(graph, c) for c in watched}
    owners = {
        c: {
            e["source"]: e
            for e in graph.edges_of(c, kind="psc")
            if e["target"] == c and graph.nodes[e["source"]]["type"] == "person"
        }
        for c in watched
    }
    for i, a in enumerate(watched):
        for b in watched[i + 1 :]:
            runs_a_owns_b = sorted(set(boards[a]) & set(owners[b]), key=graph.label)
            runs_b_owns_a = sorted(set(boards[b]) & set(owners[a]), key=graph.label)
            pairs = [(x, y) for x in runs_a_owns_b for y in runs_b_owns_a if x != y]
            if not pairs:
                continue
            x, y = pairs[0]
            out.append(
                graph.line(
                    "reciprocal-control",
                    "medium",
                    f"{graph.label(a)} and {graph.label(b)} control each other: "
                    f"{graph.label(x)} runs {graph.label(a)} and owns "
                    f"{graph.label(b)}; {graph.label(y)} runs {graph.label(b)} and "
                    f"owns {graph.label(a)}",
                    [a, b, x, y],
                    [
                        boards[a][x]["id"],
                        owners[b][x]["id"],
                        boards[b][y]["id"],
                        owners[a][y]["id"],
                    ],
                )
            )
    return out


_RULE_ORDER = [
    "risk-next-door",
    "disqualified",
    "sanctioned",
    "reciprocal-control",
    "ultimate-owner",
    "ownership-chain",
    "ownership-hub",
    "unwatched-parent",
    "shared-board",
    "simultaneous-moves",
    "hub-person",
    "new-link",
    "link-broken",
    "unwatched-connected",
    "owner-not-on-board",
    "director-not-owner",
    "intra-group-lender",
    "shared-office",
]


def _interesting(
    graph: _Graph, companies: list[CompanyRuntime], since: datetime
) -> list[JsonDict]:
    # Dissolved companies are history: their boards and owners say nothing now.
    watched = sorted(
        (
            n["id"]
            for n in graph.nodes.values()
            if graph.is_watched(n["id"]) and "dissolved" not in n["flags"]
        ),
        key=graph.label,
    )
    lines = [
        *_rule_risk_next_door(graph, watched),
        *_rule_disqualified(graph),
        *_rule_sanctioned(graph),
        *_rule_reciprocal_control(graph, watched),
        *_rule_ownership_chain(graph, watched),
        *_rule_ownership_hub(graph, watched),
        *_rule_unwatched_parent(graph, watched),
        *_rule_shared_board(graph, watched),
        *_rule_simultaneous_moves(graph, watched),
        *_rule_hub_person(graph, watched),
        *_rule_new_link(graph),
        *_rule_link_broken(graph, companies, since),
        *_rule_unwatched_connected(graph),
        *_rule_owner_not_on_board(graph, watched),
        *_rule_intra_group_lender(graph, watched),
        *_rule_shared_office(graph, watched),
    ]
    lines.sort(
        key=lambda x: (
            _SEVERITY_RANK[x.severity],
            -x.weight,
            _RULE_ORDER.index(x.rule),
            x.text,
        )
    )
    return [x.to_dict() for x in lines]


# ---------------------------------------------------------------- public


def build_graph(
    companies: Iterable[CompanyRuntime],
    people: Iterable[OfficerRuntime],
    *,
    days: int = DEFAULT_DAYS,
    include_resigned: bool = False,
    include_external: bool = True,
    now: datetime | None = None,
) -> JsonDict:
    """Build the map from runtimes: nodes, edges and the lines worth knowing.

    ``include_resigned`` keeps resigned roles, ceased holdings and satisfied
    charges as history; ``include_external`` keeps companies nobody watches
    and outside entities (lenders, overseas parents). Both only trim what is
    drawn: the rules always see the whole picture.
    """
    now = now or dt_util.utcnow()
    since = now - timedelta(days=days)
    graph = _Graph(since.date())
    watched = [c for c in companies if c.profile.data is not None]
    for company in watched:
        _add_company(graph, company)
    for officer in people:
        _add_person(graph, officer)
    for company in watched:
        _add_charges(graph, company)
    _finish(graph)
    interesting = _interesting(graph, watched, since)

    edges = [
        e
        for e in graph.edges.values()
        if (include_resigned or e["active"])
        and (
            include_external
            or all(
                graph.nodes[n]["type"] == "person"
                or graph.nodes[n]["status"] != "external"
                for n in (e["source"], e["target"])
            )
        )
    ]
    touched = {n for e in edges for n in (e["source"], e["target"])}
    nodes = [
        n
        for n in graph.nodes.values()
        if n["id"] in touched
        or (n["type"] == "company" and n["status"] != "external")
        or (n["type"] == "person" and n["status"] == "followed")
    ]
    order = {"company": 0, "person": 1, "entity": 2}
    nodes.sort(key=lambda n: (order[n["type"]], n["label"], n["id"]))
    edges.sort(key=lambda e: e["id"])
    connected = {
        n["id"] for n in nodes if n["status"] in ("own", "watched", "followed")
    }
    components = _Components()
    for e in edges:
        if e["active"]:
            components.union(e["source"], e["target"])
    live = [e for e in edges if e["active"] and {e["source"], e["target"]} <= connected]
    summary = {
        "nodes": len(nodes),
        "edges": len(edges),
        "watched_companies": sum(1 for n in nodes if graph.is_watched(n["id"])),
        "followed_people": sum(1 for n in nodes if n["status"] == "followed"),
        "other_people": sum(
            1 for n in nodes if n["type"] == "person" and n["status"] != "followed"
        ),
        "external_companies": sum(
            1 for n in nodes if n["type"] == "company" and n["status"] == "external"
        ),
        "entities": sum(1 for n in nodes if n["type"] == "entity"),
        "live_connections": len(live),
        "new_connections": sum(1 for e in edges if e["active"] and e["new"]),
        "components": len({components.find(n["id"]) for n in nodes}),
        "interesting": len(interesting),
        "high": sum(1 for x in interesting if x["severity"] == "high"),
        "medium": sum(1 for x in interesting if x["severity"] == "medium"),
        "low": sum(1 for x in interesting if x["severity"] == "low"),
    }
    return {
        "generated_at": now.isoformat(),
        "period_days": days,
        "since": since.date().isoformat(),
        "summary": summary,
        "nodes": nodes,
        "edges": edges,
        "interesting": interesting,
    }


def build_connections(
    entry: CompaniesHouseConfigEntry,
    *,
    days: int = DEFAULT_DAYS,
    include_resigned: bool = False,
    include_external: bool = True,
    now: datetime | None = None,
) -> JsonDict:
    """Build the map for everything a config entry watches and follows."""
    runtime = entry.runtime_data
    return build_graph(
        runtime.companies.values(),
        runtime.officers.values(),
        days=days,
        include_resigned=include_resigned,
        include_external=include_external,
        now=now,
    )


def fingerprint(graph: JsonDict) -> str:
    """Return a stable hash of the map, ignoring when it was generated."""
    return stable_hash(graph["nodes"], graph["edges"], graph["interesting"])


def trim_for_llm(graph: JsonDict) -> JsonDict:
    """Return what an assistant needs: the counts and the lines, not the drawing."""
    return {
        "generated_at": graph["generated_at"],
        "period_days": graph["period_days"],
        "summary": graph["summary"],
        "interesting": [
            {"severity": x["severity"], "text": x["text"], "link": x["link"]}
            for x in graph["interesting"]
        ],
    }


def connections_url(hass: HomeAssistant) -> str:
    """Return the address of the map page under ``/local``."""
    base = (hass.config.external_url or hass.config.internal_url or "").rstrip("/")
    return f"{base}/local/{DOMAIN}/{SHELL_FILENAME}"


def _replace(path: Path, text: str) -> None:
    """Write a file in one step, so a page fetching it mid-write never sees half."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _write_files(folder: Path, data: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    shell = folder / SHELL_FILENAME
    if not shell.exists() or shell.read_text(encoding="utf-8") != SHELL_HTML:
        _replace(shell, SHELL_HTML)
    _replace(folder / DATA_FILENAME, data)
    return shell


async def async_write_files(hass: HomeAssistant, graph: JsonDict) -> str:
    """Write the page and its data under ``www/companies_house``; return the path.

    Raises ``OSError`` when the folder cannot be written.
    """
    folder = Path(hass.config.path("www", DOMAIN))
    path = await hass.async_add_executor_job(
        _write_files, folder, json.dumps(graph, separators=(",", ":"))
    )
    return str(path)


# ---------------------------------------------------------------- coordinator


@dataclass(frozen=True)
class ConnectionsData:
    """The current map, and where it was last written."""

    graph: JsonDict
    fingerprint: str
    path: str | None
    url: str
    written_at: datetime | None


class ConnectionsCoordinator(DataUpdateCoordinator[ConnectionsData]):
    """Keep the map current and its files on disk, without polling anything.

    A change event of a kind that can alter the map (a role, a holding, a
    charge, a status, a name, a person's appointments) or a company or
    person being added arms one timer; when it fires the map is rebuilt in
    memory and, only if it actually changed, written to
    ``www/companies_house`` in the executor. Setup builds it once straight
    away so the sensor has a state, and writes it shortly after. Once a day
    the map is rebuilt anyway, so what counted as new last week stops
    saying so.
    """

    # None until the first build, like the dataset coordinators.
    data: ConnectionsData | None  # type: ignore[assignment]

    def __init__(self, hass: HomeAssistant, entry: CompaniesHouseConfigEntry) -> None:
        """Initialise with no timer armed."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} connections",
            update_interval=None,
        )
        self.entry = entry
        self._timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self.pending_reason: str | None = None

    @callback
    def async_build(self) -> ConnectionsData:
        """Rebuild the map in memory now, keeping where the files were last written."""
        graph = build_connections(
            self.entry, days=DEFAULT_DAYS, include_resigned=True, include_external=True
        )
        previous = self.data
        data = ConnectionsData(
            graph=graph,
            fingerprint=fingerprint(graph),
            path=previous.path if previous else None,
            url=connections_url(self.hass),
            written_at=previous.written_at if previous else None,
        )
        self.async_set_updated_data(data)
        return data

    @callback
    def async_start(self) -> None:
        """Listen for changes and arm the first write."""

        @callback
        def _wanted(data: dict[str, Any]) -> bool:
            return data.get("kind") in REBUILD_KINDS

        @callback
        def _on_event(event: Event[dict[str, Any]]) -> None:
            self.request_rebuild(
                f"{event.data.get('kind')} {event.data.get('event_type')}"
            )

        @callback
        def _on_new(_runtime: Any) -> None:
            self.request_rebuild("new company or person")

        @callback
        def _on_day(_now: datetime) -> None:
            self.request_rebuild("day rolled over")

        self._unsubs = [
            self.hass.bus.async_listen(EVENT_COMPANIES_HOUSE, _on_event, _wanted),
            async_dispatcher_connect(
                self.hass, signal_new_company(self.entry.entry_id), _on_new
            ),
            async_dispatcher_connect(
                self.hass, signal_new_officer(self.entry.entry_id), _on_new
            ),
            async_track_utc_time_change(
                self.hass, _on_day, hour=0, minute=DAY_ROLLOVER_MINUTE, second=0
            ),
        ]
        self._arm(INITIAL_WRITE_DELAY, "setup")

    @callback
    def request_rebuild(self, reason: str) -> None:
        """Rebuild soon. A burst of events shares one timer."""
        self._arm(REBUILD_DEBOUNCE, reason)

    def _arm(self, delay: timedelta, reason: str) -> None:
        if self._timer is not None:
            return
        self.pending_reason = reason
        LOGGER.debug("Connections map rebuild in %s (%s)", delay, reason)
        self._timer = async_call_later(self.hass, delay, self._on_timer)

    async def _on_timer(self, _now: datetime) -> None:
        self._timer = None
        reason, self.pending_reason = self.pending_reason, None
        await self.async_rebuild(force=reason == "setup")

    async def async_rebuild(self, *, force: bool = False, strict: bool = False) -> bool:
        """Rebuild the map and write the files if it changed. Return whether it did.

        A write that fails is logged and the old path kept, unless ``strict``
        (the action asked for the write) in which case ``OSError`` is raised.
        """
        graph = build_connections(
            self.entry, days=DEFAULT_DAYS, include_resigned=True, include_external=True
        )
        digest = fingerprint(graph)
        previous = self.data
        if (
            not force
            and previous is not None
            and previous.fingerprint == digest
            and previous.written_at is not None
        ):
            LOGGER.debug("Connections map unchanged")
            return False
        path: str | None = None
        written_at = previous.written_at if previous else None
        try:
            path = await async_write_files(self.hass, graph)
            written_at = dt_util.utcnow()
        except OSError as err:
            if strict:
                raise
            LOGGER.warning("Could not write the connections map: %s", err)
            path = previous.path if previous else None
        self.async_set_updated_data(
            ConnectionsData(
                graph=graph,
                fingerprint=digest,
                path=path,
                url=connections_url(self.hass),
                written_at=written_at,
            )
        )
        return True

    async def _async_update_data(self) -> ConnectionsData:
        """Rebuild and rewrite unconditionally, for a manual refresh."""
        await self.async_rebuild(force=True)
        assert self.data is not None
        return self.data

    async def async_shutdown(self) -> None:
        """Stop listening and drop any pending rebuild."""
        if self._timer is not None:
            self._timer()
            self._timer = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        await super().async_shutdown()


__all__ = [
    "ConnectionsCoordinator",
    "ConnectionsData",
    "async_write_files",
    "build_connections",
    "build_graph",
    "connections_url",
    "fingerprint",
    "trim_for_llm",
]
