"""Assist tools: ask about the watched companies and followed people.

Home Assistant builds the "Assist" API a conversation agent uses from every
loaded integration's ``llm`` platform. This one offers a handful of
questions that are answered from what is already on hand — the snapshots,
the change log, the ratings, the map — so a question never costs a request
to the register. The tools only appear once a config entry is loaded and at
least one of its entities is exposed to the assistant, the same gate the
core calendar and to-do tools use. The "Companies House" API in
``services.py`` (the raw read actions, one per register resource) is
separate and untouched.

Every answer is plain data with plain-English strings ready to be spoken:
dates as "8 Sep 2026", money as "£1.2m". Company and person names are
matched forgivingly (case, punctuation, "Ltd" for "Limited", a part of the
name); when more than one matches, the candidates come back instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
import re
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.homeassistant.exposed_entities import (
    async_should_expose,
)
from homeassistant.components.llm import LLMTools
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.llm import LLM_API_ASSIST, LLMContext, Tool, ToolInput
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType
import voluptuous as vol

from .accounts import (
    METRIC_NAMES,
    METRICS,
    describe_figures,
    format_change,
    format_figure,
    year_payload,
    year_summary,
)
from .charges import charges_overview
from .connections import build_connections, trim_for_llm
from .const import DOMAIN
from .digest import (
    DEADLINE_HORIZON_DAYS,
    _company_card,
    _company_link,
    _control_words,
    _document_link,
    _normalise_person,
    _person_card,
    _pretty_date,
    _role,
    _sentence,
    is_finished,
)
from .enumerations import COMPANY_STATUS_DETAIL, COMPANY_TYPE
from .gazette import notice_link
from .models import JsonDict, display_name
from .risk import BAND_AMBER, BAND_RANK, BAND_RED

if TYPE_CHECKING:
    from .coordinator import CompaniesHouseConfigEntry, CompanyRuntime, OfficerRuntime

PROMPT = (
    "The companies_house__ tools answer questions about the UK companies being "
    "watched and the people being followed from data already gathered from "
    "Companies House: status, filing deadlines, risk ratings, recent changes and "
    "filings, accounts figures, officers, owners, charges, strike-off countdowns "
    "and the connections between them. Use them rather than guessing, and name "
    "the company or person the way the user did; the tool copes with partial "
    "names and asks back when several match."
)

# How far back the company answer and the person answer look for changes.
RECENT_DAYS = 30
PERSON_RECENT_DAYS = 90
DEFAULT_CHANGE_DAYS = 7
DEFAULT_FILING_DAYS = 90
MAX_DAYS = 365
CHANGE_LIMIT = 30
FILING_LIMIT = 25
ROLE_LIMIT = 25
CHARGE_LIMIT = 10
CANDIDATE_LIMIT = 8

_ABBREVIATIONS = {"limited": "ltd", "incorporated": "inc", "and": "&"}
_WORD_RE = re.compile(r"[^a-z0-9&]+")


# ---------------------------------------------------------------- matching


def _normalise(text: str) -> str:
    """Casefold a name, drop punctuation and spell "Limited" as "ltd"."""
    words = _WORD_RE.sub(" ", text.casefold().replace("&", " & ")).split()
    return " ".join(_ABBREVIATIONS.get(w, w) for w in words)


def _match_score(query: str, names: Iterable[str]) -> int:
    """Say how well a query fits any of a thing's names: 3 exact, 2 part, 1 words.

    "Example Trading Ltd" is the same as "EXAMPLE TRADING LIMITED"; "example
    trading" is part of it; "trading example" has every word in it.
    """
    wanted = _normalise(query)
    if not wanted:
        return 0
    words = wanted.split()
    best = 0
    for name in names:
        have = _normalise(name)
        if not have:
            continue
        if have == wanted:
            return 3
        if wanted in have:
            best = max(best, 2)
        elif all(w in have.split() for w in words):
            best = max(best, 1)
    return best


class NoAnswerError(Exception):
    """A question that cannot be answered, with the reason in plain English."""

    def __init__(self, error: str, **extra: Any) -> None:
        """Keep the message and anything else worth returning, such as candidates."""
        super().__init__(error)
        self.error = error
        self.extra = extra


def _resolve[T](
    query: str, candidates: list[tuple[T, list[str]]], *, what: str, plural: str
) -> T:
    """Return the one candidate a name fits best, or say why there is none.

    The best score wins; several at the same score is a real ambiguity and
    the caller gets their names to ask back with.
    """
    scored = [
        (_match_score(query, names), item, names[0]) for item, names in candidates
    ]
    best = max((s for s, _, _ in scored), default=0)
    if best == 0:
        raise NoAnswerError(f"No {what} matches '{query}'.")
    matches = [(item, name) for score, item, name in scored if score == best]
    if len(matches) > 1:
        names = sorted({name for _, name in matches})
        raise NoAnswerError(
            f"Several {plural} match '{query}': {_some(names)}. Which one?",
            candidates=names,
        )
    return matches[0][0]


def _some(names: list[str], limit: int = CANDIDATE_LIMIT) -> str:
    """List the first few names and count the rest."""
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def _loaded_entries(hass: HomeAssistant) -> list[CompaniesHouseConfigEntry]:
    """Return every loaded account."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


def _companies(hass: HomeAssistant) -> list[CompanyRuntime]:
    """Return every watched company whose profile has been read."""
    return [
        company
        for entry in _loaded_entries(hass)
        for company in entry.runtime_data.companies.values()
        if company.profile.data is not None
    ]


def _people(hass: HomeAssistant) -> list[OfficerRuntime]:
    """Return every followed person."""
    return [
        officer
        for entry in _loaded_entries(hass)
        for officer in entry.runtime_data.officers.values()
    ]


def _company_names(company: CompanyRuntime) -> list[str]:
    """Return the names a company can be asked for by: its name, label and number."""
    return [company.company_name, company.label, company.company_number]


def find_company(hass: HomeAssistant, query: str) -> CompanyRuntime:
    """Return the watched company a name or number refers to."""
    candidates = [(c, _company_names(c)) for c in _companies(hass)]
    if not candidates:
        raise NoAnswerError("No companies are being watched yet.")
    return _resolve(
        query, candidates, what="watched company", plural="watched companies"
    )


def _person_names(officer: OfficerRuntime) -> list[str]:
    """Return the names a followed person can be asked for by."""
    names = [officer.officer_name, officer.configured_name, officer.register_name]
    names.append(display_name(officer.register_name))
    return [n for n in names if n]


# ---------------------------------------------------------------- words


def _pretty(value: date | str | None) -> str:
    """Write a date as people do, or nothing when there is none."""
    return _pretty_date(value) if value else ""


def _when(value: Any) -> str:
    """Write the day a logged change happened."""
    parsed = dt_util.parse_datetime(str(value or ""))
    return _pretty(dt_util.as_local(parsed).date()) if parsed else ""


def _due_words(days: int) -> str:
    """Say how a deadline stands: "overdue by 12 days", "due in 30 days"."""
    if days < 0:
        return f"overdue by {-days} day{'' if days == -1 else 's'}"
    if days == 0:
        return "due today"
    return f"due in {days} day{'' if days == 1 else 's'}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _list_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _change_row(change: JsonDict, *, who: str) -> JsonDict:
    """Return one logged change the way an assistant reads it out."""
    return {
        "when": _when(change.get("at")),
        "who": who,
        "what": change.get("title"),
        "detail": change.get("message"),
        "link": change.get("link"),
    }


# ---------------------------------------------------------------- answers


def _deadline_rows(company: CompanyRuntime, today: date) -> list[JsonDict]:
    """Return a company's filing deadlines and the last day to object to a strike-off."""
    profile = company.profile.data
    assert profile is not None
    rows: list[JsonDict] = []
    if is_finished(profile.company_status):
        return rows
    for what, due, overdue in (
        ("annual accounts", profile.accounts.next_due, profile.accounts.next_overdue),
        (
            "confirmation statement",
            profile.confirmation_statement.next_due,
            profile.confirmation_statement.overdue,
        ),
    ):
        if due is None:
            continue
        days = (due - today).days
        rows.append(
            {
                "what": what,
                "company": company.company_name,
                "number": company.company_number,
                "date": _pretty(due),
                "days": days,
                "overdue": overdue or days < 0,
                "status": _due_words(days),
                "link": _company_link(company.company_number),
            }
        )
    countdown = company.strike_off_countdown(today)
    if countdown is not None and countdown.objection_deadline is not None:
        rows.append(
            {
                "what": "object to strike-off",
                "company": company.company_name,
                "number": company.company_number,
                "date": _pretty(countdown.objection_deadline),
                "days": countdown.days_to_object,
                "overdue": False,
                "status": countdown.objection_phrase(),
                "link": notice_link(company.company_number, countdown.transaction_id),
            }
        )
    return rows


def _accounts_answer(company: CompanyRuntime) -> JsonDict | None:
    """Return the newest figures read from the accounts, each with its change."""
    history = company.accounts.data
    if history is None:
        return None
    year = history.latest_read or history.latest
    if year is None:
        return None
    summary = year_summary(year)
    figures: list[JsonDict] = []
    undisclosed: list[str] = []
    for metric in METRICS:
        figure = summary["figures"][metric]
        if figure["value"] is None:
            undisclosed.append(METRIC_NAMES[metric].lower())
            continue
        figures.append(
            {
                "name": METRIC_NAMES[metric],
                "value": format_figure(metric, figure["value"]),
                "prior": format_figure(metric, figure["prior"])
                if figure["prior"] is not None
                else "",
                "change": format_change(figure["change_percent"]),
                "change_percent": figure["change_percent"],
            }
        )
    return {
        "made_up_to": _pretty(year.made_up_to),
        "filed_on": _pretty(year.filed_on),
        "type": year.type_words,
        "status": summary["status_words"],
        "figures": figures,
        "not_disclosed": undisclosed,
        "flags": summary["flags"],
        "text": describe_figures(year_payload(year)),
        "years_read": len(history.series()),
        "link": _document_link(company.company_number, year.transaction_id),
    }


def _officer_rows(company: CompanyRuntime) -> list[JsonDict]:
    """Return the current officers, newest appointment first."""
    data = company.officers.data if company.officers else None
    if data is None:
        return []
    current = sorted(
        (o for o in data.items if o.is_active),
        key=lambda o: o.appointed_on or o.appointed_before or date.min,
        reverse=True,
    )
    return [
        {
            "name": display_name(o.name),
            "role": _role(o.officer_role),
            "since": _pretty(o.appointed_on or o.appointed_before),
            "occupation": o.occupation or "",
            "nationality": o.nationality or "",
        }
        for o in current[:ROLE_LIMIT]
    ]


def _psc_rows(company: CompanyRuntime) -> list[JsonDict]:
    """Return who controls the company now."""
    data = company.psc.data if company.psc else None
    if data is None:
        return []
    return [
        {
            "name": display_name(p.name),
            "controls": _control_words(p.natures_of_control),
            "since": _pretty(p.notified_on),
            "corporate": bool(p.kind and "corporate" in p.kind),
            "sanctioned": p.is_sanctioned,
        }
        for p in data.items
        if not p.ceased and p.ceased_on is None
    ]


def _charges_answer(company: CompanyRuntime) -> JsonDict | None:
    """Return the charges: how many, who lent, and the newest few."""
    overview = charges_overview(company)
    if overview is None:
        return None
    return {
        "outstanding": overview["outstanding"],
        "total": overview["total"],
        "satisfied": overview["satisfied"],
        "lenders": overview["lenders"],
        "text": (
            f"{_plural(overview['outstanding'], 'outstanding charge')}"
            + (
                f" in favour of {_list_words(overview['lenders'])}"
                if overview["lenders"]
                else ""
            )
            if overview["outstanding"]
            else "No outstanding charges"
        ),
        "charges": [
            {
                "lender": c["lender"],
                "status": str(c["status"] or "").replace("-", " "),
                "created_on": _pretty(c["created_on"]),
                "satisfied_on": _pretty(c["satisfied_on"]),
                "kind": c["kind"],
                "secured": c["secured"],
                "link": c["link"],
            }
            for c in overview["charges"][:CHARGE_LIMIT]
        ],
        "link": overview["link"],
    }


def _reason_words(reason: str) -> str:
    """Drop the band from a one-line rating reason: "Amber: sole director" → "sole director"."""
    _, sep, rest = reason.partition(": ")
    return rest if sep else reason


def _company_summary(
    company: CompanyRuntime, card: JsonDict, deadlines: list[JsonDict]
) -> str:
    """Write the one-paragraph answer: status, trouble, rating, next deadline, figures."""
    profile = company.profile.data
    assert profile is not None
    opening = f"{company.company_name} ({company.company_number}) is {card['status']}"
    if profile.date_of_cessation and is_finished(profile.company_status):
        opening += f" since {_pretty(profile.date_of_cessation)}"
    elif profile.date_of_creation:
        opening += f", incorporated {_pretty(profile.date_of_creation)}"
    sentences = [f"{opening}."]
    if card["strike_off"]:
        sentences.append(f"{card['strike_off']['summary']}.")
    trouble = [
        a["issue"]
        for a in card["attention"]
        if a.get("kind") != "risk" and not a["issue"].startswith("Strike-off")
    ]
    if trouble:
        lowered = [trouble[0], *(t[0].lower() + t[1:] for t in trouble[1:])]
        sentences.append(f"{_list_words(lowered)}.")
    risk = card["risk"]
    if risk and risk["band"]:
        sentences.append(
            f"Risk rating {risk['band']}: {_reason_words(risk['reason'])}."
        )
    upcoming = [d for d in deadlines if d["days"] is not None and d["days"] >= 0]
    if upcoming:
        soonest = min(upcoming, key=lambda d: d["days"])
        sentences.append(
            f"Next deadline: {soonest['what']} {soonest['status']} ({soonest['date']})."
        )
    accounts = card["accounts"]
    if accounts and any(v is not None for v in accounts["figures"].values()):
        figures = describe_figures(
            {
                "figures": accounts["figures"],
                "changes": accounts["changes"],
                "flags": accounts["flags"],
                "accounts_type_words": accounts["accounts_type"],
            }
        )
        sentences.append(
            f"Latest accounts to {_pretty(accounts['made_up_to'])}: {figures}"
        )
    return " ".join(sentences)


def company_answer(company: CompanyRuntime, now: datetime) -> JsonDict:
    """Return everything known about one company, ready to be read out."""
    # Only companies whose profile has been read are offered up for matching.
    profile = company.profile.data
    assert profile is not None
    today = dt_util.as_local(now).date()
    since = now - timedelta(days=RECENT_DAYS)
    card = _company_card(company, since, today)
    deadlines = _deadline_rows(company, today)
    strike_off = card["strike_off"]
    risk = company.risk
    return {
        "name": company.company_name,
        "number": company.company_number,
        "label": company.label,
        "status": card["status"],
        "status_detail": COMPANY_STATUS_DETAIL.get(
            profile.company_status_detail or "", profile.company_status_detail or ""
        ),
        "type": COMPANY_TYPE.get(profile.type or "", profile.type or ""),
        "incorporated": _pretty(profile.date_of_creation),
        "registered_office": profile.registered_office_address.one_line()
        if profile.registered_office_address
        else "",
        "close_watch": company.close_watch,
        "summary": _company_summary(company, card, deadlines),
        "attention": [a["issue"] for a in card["attention"]],
        "deadlines": deadlines,
        "strike_off": {
            "summary": strike_off["summary"],
            "kind": strike_off["kind"],
            "notice_on": _pretty(strike_off["notice_on"]),
            "suspended_on": _pretty(strike_off["suspended_on"]),
            "earliest_on": _pretty(strike_off["earliest_on"]),
            "objection_deadline": _pretty(strike_off["objection_deadline"]),
            "days_to_object": strike_off["days_to_object"],
            "caveat": strike_off["caveat"],
            "link": strike_off["link"],
        }
        if strike_off
        else None,
        "risk": {
            "band": risk.band or "unknown",
            "score": risk.score,
            "reason": risk.reason,
            "reasons": list(risk.reasons),
            "data_age_days": risk.data_age_days,
        }
        if risk is not None
        else None,
        "accounts": _accounts_answer(company),
        "officers": _officer_rows(company),
        "people_with_control": _psc_rows(company),
        "charges": _charges_answer(company),
        "recent_changes": [
            _change_row(c, who=company.company_name)
            for c in sorted(card["changes"], key=lambda c: str(c["at"]), reverse=True)[
                :CHANGE_LIMIT
            ]
        ],
        "recent_days": RECENT_DAYS,
        "links": [
            {"text": "Company", "href": card["link"]},
            *card["pages"],
        ],
    }


def what_changed_answer(
    hass: HomeAssistant, *, days: int, company: CompanyRuntime | None, now: datetime
) -> JsonDict:
    """Return what changed at the watched companies and followed people, newest first."""
    today = dt_util.as_local(now).date()
    since = now - timedelta(days=days)
    companies = [company] if company else _companies(hass)
    people = [] if company else [p for p in _people(hass) if p.appointments.data]
    dated: list[tuple[str, JsonDict]] = []
    changed_companies = 0
    changed_people = 0
    for item in companies:
        card = _company_card(item, since, today)
        changed_companies += bool(card["changes"])
        dated.extend(
            (str(c["at"]), _change_row(c, who=item.company_name))
            for c in card["changes"]
        )
    for person in people:
        card = _person_card(person, since)
        changed_people += bool(card["changes"])
        dated.extend(
            (str(c["at"]), _change_row(c, who=person.officer_name))
            for c in card["changes"]
        )
    dated.sort(key=lambda pair: pair[0], reverse=True)
    period = f"the last {_plural(days, 'day')}"
    if not dated:
        where = (
            company.company_name
            if company
            else "the watched companies and followed people"
        )
        text = f"Nothing changed at {where} in {period}."
    elif company:
        text = f"{_plural(len(dated), 'change')} at {company.company_name} in {period}."
    else:
        subjects = [
            words
            for count, singular, plural in (
                (changed_companies, "company", "companies"),
                (changed_people, "person", "people"),
            )
            if count
            for words in [f"{count} {singular if count == 1 else plural}"]
        ]
        text = (
            f"{_plural(len(dated), 'change')} at {_list_words(subjects)} in {period}."
        )
    return {
        "days": days,
        "since": _pretty(dt_util.as_local(since).date()),
        "count": len(dated),
        "summary": text,
        "changes": [row for _, row in dated[:CHANGE_LIMIT]],
    }


def deadlines_answer(hass: HomeAssistant, *, days: int, now: datetime) -> JsonDict:
    """Return every deadline due inside the horizon, overdue ones first."""
    today = dt_util.as_local(now).date()
    rows = [
        row
        for company in _companies(hass)
        for row in _deadline_rows(company, today)
        if row["days"] is not None and row["days"] <= days
    ]
    rows.sort(key=lambda r: (r["days"], r["company"]))
    overdue = [r for r in rows if r["overdue"]]
    if not rows:
        text = (
            f"Nothing is due in the next {_plural(days, 'day')} and nothing is overdue."
        )
    else:
        text = f"{_plural(len(rows), 'deadline')} in the next {_plural(days, 'day')}"
        if overdue:
            text += f", {len(overdue)} already overdue"
        text += ": " + "; ".join(
            f"{r['company']} {r['what']} {r['status']}" for r in rows[:8]
        )
        text += "."
    return {
        "days": days,
        "count": len(rows),
        "overdue": len(overdue),
        "summary": text,
        "deadlines": rows,
    }


def filings_answer(company: CompanyRuntime, *, days: int, now: datetime) -> JsonDict:
    """Return the filings the register dates inside the period, newest first."""
    today = dt_util.as_local(now).date()
    since = today - timedelta(days=days)
    data = company.probe.data
    items = [i for i in (data.items if data else []) if i.date and i.date >= since]
    items.sort(key=lambda i: i.date or date.min, reverse=True)
    rows = [
        {
            "date": _pretty(item.date),
            "description": item.rendered_description or item.description or "",
            "category": (item.category or "other").replace("-", " "),
            "type": item.type or "",
            "pages": item.pages,
            "link": _document_link(company.company_number, item.transaction_id)
            if item.document_id
            else _company_link(company.company_number) + "/filing-history",
        }
        for item in items[:FILING_LIMIT]
    ]
    period = f"the last {_plural(days, 'day')}"
    if not rows:
        text = f"{company.company_name} filed nothing in {period}."
    else:
        text = (
            f"{company.company_name} filed {_plural(len(items), 'document')} in {period}: "
            + "; ".join(f"{r['description']} on {r['date']}" for r in rows[:5])
        )
        text += "."
    return {
        "company": company.company_name,
        "number": company.company_number,
        "days": days,
        "count": len(items),
        "summary": text,
        "filings": rows,
        "link": _company_link(company.company_number) + "/filing-history",
    }


def _followed_person_answer(officer: OfficerRuntime, now: datetime) -> JsonDict:
    """Return a followed person's roles, records, disqualification and changes."""
    since = now - timedelta(days=PERSON_RECENT_DAYS)
    card = _person_card(officer, since)
    data = officer.appointments.data
    former = data.resigned if data else []
    disqualification = officer.disqualification.data
    if disqualification is None:
        disqualified: JsonDict = {"checked": False, "text": "Not checked yet"}
    elif disqualification.disqualified:
        disqualified = {
            "checked": True,
            "disqualified": True,
            "from": _pretty(disqualification.disqualified_from),
            "until": _pretty(disqualification.disqualified_until),
            "reason": disqualification.reason or "",
            "text": f"Disqualified until {_pretty(disqualification.disqualified_until) or 'an unknown date'}",
        }
    else:
        matches = len(disqualification.possible_matches)
        disqualified = {
            "checked": True,
            "disqualified": False,
            "possible_matches": matches,
            "text": "Not on the disqualified directors register"
            + (f" ({_plural(matches, 'namesake')} on it)" if matches else ""),
        }
    roles = card["companies"]
    text = f"{officer.officer_name} holds {_plural(len(roles), 'current role')}"
    if roles:
        text += ": " + "; ".join(
            f"{r['role']} at {r['name']}" for r in roles[:ROLE_LIMIT]
        )
    text += f". {disqualified['text']}."
    if card["changes"]:
        text += f" {_plural(len(card['changes']), 'change')} in the last {PERSON_RECENT_DAYS} days."
    return {
        "name": officer.officer_name,
        "followed": True,
        "records": officer.officer_ids,
        "date_of_birth": officer.known_date_of_birth.display()
        if officer.known_date_of_birth
        else "",
        "summary": text,
        "roles": [
            {
                "company": r["name"],
                "number": r["number"],
                "role": r["role"],
                "since": _pretty(r["appointed_on"]),
                "link": r["link"],
            }
            for r in roles[:ROLE_LIMIT]
        ],
        "former_roles": len(former),
        "disqualification": disqualified,
        "recent_changes": [
            _change_row(c, who=officer.officer_name)
            for c in sorted(card["changes"], key=lambda c: str(c["at"]), reverse=True)[
                :CHANGE_LIMIT
            ]
        ],
        "recent_days": PERSON_RECENT_DAYS,
        "link": card["link"],
    }


def _register_person_answer(hass: HomeAssistant, query: str) -> JsonDict:
    """Return the roles a person not followed holds at the watched companies.

    Every officer and owner at every watched company is looked at; the
    ones whose name fits are pooled under one name (titles aside, "Mrs
    Sarah White" the owner is "Sarah White" the director), so "Priya"
    finds Priya Patel wherever she sits.
    """
    names: dict[str, str] = {}
    roles: dict[str, list[JsonDict]] = {}

    def add(name: str, register_name: str, row: JsonDict) -> None:
        if _match_score(query, [name, register_name]) == 0:
            return
        key = _normalise_person(name)
        names.setdefault(key, name)
        roles.setdefault(key, []).append(row)

    for company in _companies(hass):
        link = _company_link(company.company_number)
        officers = (
            company.officers.data.items
            if company.officers and company.officers.data
            else []
        )
        for officer in officers:
            add(
                display_name(officer.name),
                officer.name,
                {
                    "company": company.company_name,
                    "number": company.company_number,
                    "role": _role(officer.officer_role),
                    "since": _pretty(officer.appointed_on or officer.appointed_before),
                    "until": _pretty(officer.resigned_on),
                    "current": officer.is_active,
                    "link": link + "/officers",
                },
            )
        pscs = company.psc.data.items if company.psc and company.psc.data else []
        for psc in pscs:
            add(
                display_name(psc.name),
                psc.name,
                {
                    "company": company.company_name,
                    "number": company.company_number,
                    "role": "person with significant control",
                    "controls": _control_words(psc.natures_of_control),
                    "since": _pretty(psc.notified_on),
                    "until": _pretty(psc.ceased_on),
                    "current": not psc.ceased and psc.ceased_on is None,
                    "link": link + "/persons-with-significant-control",
                },
            )
    if not roles:
        followed = sorted(o.officer_name for o in _people(hass))
        hint = f" The followed people are {_list_words(followed)}." if followed else ""
        raise NoAnswerError(
            f"Nobody called '{query}' is followed or holds a role at a watched company.{hint}"
        )
    if len(roles) > 1:
        found = sorted(names.values())
        raise NoAnswerError(
            f"Several people match '{query}': {_some(found)}. Which one?",
            candidates=found,
        )
    key, rows = next(iter(roles.items()))
    name = names[key]
    current = [r for r in rows if r["current"]]
    text = f"{name} is not followed. "
    if current:
        text += (
            f"{_plural(len(current), 'current role')} at the watched companies: "
            + "; ".join(f"{r['role']} at {r['company']}" for r in current[:ROLE_LIMIT])
            + "."
        )
    else:
        text += "No current role at the watched companies, only former ones."
    return {
        "name": name,
        "followed": False,
        "summary": text,
        "roles": rows[:ROLE_LIMIT],
        "former_roles": len(rows) - len(current),
    }


def person_answer(hass: HomeAssistant, query: str, now: datetime) -> JsonDict:
    """Return what is known about a person: followed first, then the register's officers."""
    followed = [(o, _person_names(o)) for o in _people(hass)]
    if followed and any(_match_score(query, names) for _, names in followed):
        officer = _resolve(
            query, followed, what="followed person", plural="followed people"
        )
        if officer.appointments.data is None:
            raise NoAnswerError(
                f"{officer.officer_name} has not been read from the register yet."
            )
        return _followed_person_answer(officer, now)
    return _register_person_answer(hass, query)


def _graph(hass: HomeAssistant) -> JsonDict:
    """Return the current map: the one kept on the service device, else built now."""
    entries = _loaded_entries(hass)
    if not entries:
        raise NoAnswerError("No companies are being watched yet.")
    coordinator = entries[0].runtime_data.connections
    if coordinator is not None and coordinator.data is not None:
        return coordinator.data.graph
    return build_connections(entries[0], include_resigned=True, include_external=True)


def connections_answer(hass: HomeAssistant, name: str | None) -> JsonDict:
    """Return the connections worth knowing about, for everyone or for one name."""
    graph = _graph(hass)
    if not name:
        trimmed = trim_for_llm(graph)
        summary = graph["summary"]
        lines = trimmed["interesting"]
        text = (
            (
                f"{_plural(summary['live_connections'], 'live connection')} between "
                f"{_plural(summary['watched_companies'], 'watched company')} and "
                f"{_plural(summary['followed_people'], 'followed person')}"
            )
            .replace("companys", "companies")
            .replace("persons", "people")
        )
        if lines:
            text += f"; {_plural(len(lines), 'thing')} worth knowing: " + " ".join(
                _sentence(str(x["text"])) for x in lines[:5]
            )
        else:
            text += "; nothing stands out."
        return {**trimmed, "summary_text": text}
    nodes = [n for n in graph["nodes"] if n["type"] in ("company", "person")]
    node = _resolve(
        name,
        [(n, [n["label"], n.get("number") or ""]) for n in nodes],
        what="company or person on the map",
        plural="companies and people on the map",
    )
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    links: list[JsonDict] = []
    for edge in graph["edges"]:
        if node["id"] not in (edge["source"], edge["target"]):
            continue
        other = edge["target"] if edge["source"] == node["id"] else edge["source"]
        links.append(
            {
                "with": labels.get(other, other),
                "how": edge.get("role_label")
                or edge.get("control_label")
                or edge["kind"],
                "since": _pretty(edge.get("since")),
                "until": _pretty(edge.get("until")),
                "current": edge["active"],
                "new": edge.get("new", False),
                "link": edge.get("link"),
            }
        )
    links.sort(key=lambda x: (not x["current"], str(x["with"])))
    interesting = [
        {"severity": x["severity"], "text": x["text"], "link": x["link"]}
        for x in graph["interesting"]
        if node["id"] in x["node_ids"]
    ]
    current = [x for x in links if x["current"]]
    text = f"{node['label']} has {_plural(len(current), 'current connection')}"
    if current:
        text += ": " + "; ".join(f"{x['how']} — {x['with']}" for x in current[:10])
    text += "."
    if interesting:
        text += " " + " ".join(_sentence(str(x["text"])) for x in interesting[:5])
    return {
        "name": node["label"],
        "type": node["type"],
        "status": node["status"],
        "summary": text,
        "connections": links,
        "interesting": interesting,
        "link": node.get("link"),
    }


def risk_answer(hass: HomeAssistant) -> JsonDict:
    """Return the red and amber companies, worst first, with the counts."""
    counts = {"red": 0, "amber": 0, "green": 0, "unknown": 0}
    flagged: list[JsonDict] = []
    for company in _companies(hass):
        risk = company.risk
        band = risk.band if risk and risk.band else "unknown"
        counts[band] += 1
        if risk is None or band not in (BAND_AMBER, BAND_RED):
            continue
        profile = company.profile.data
        if profile is not None and is_finished(profile.company_status):
            continue
        flagged.append(
            {
                "company": company.company_name,
                "number": company.company_number,
                "band": band,
                "score": risk.score,
                "reason": risk.reason,
                "reasons": list(risk.reasons),
                "link": _company_link(company.company_number),
            }
        )
    flagged.sort(
        key=lambda r: (-BAND_RANK.get(r["band"], 0), -r["score"], r["company"])
    )
    text = (
        f"{counts['red']} red, {counts['amber']} amber, {counts['green']} green"
        + (f", {counts['unknown']} not rated" if counts["unknown"] else "")
        + "."
    )
    if flagged:
        text += " " + " ".join(
            f"{r['company']} is {r['band']}: {_reason_words(r['reason'])}."
            for r in flagged[:8]
        )
    return {"counts": counts, "summary": text, "companies": flagged}


# ---------------------------------------------------------------- tools


_DAYS = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DAYS))
_COMPANY_DESCRIPTION = (
    "The company's name (or part of it), its label, or its company number."
)


class _CompaniesHouseTool(Tool):
    """A question answered from what is already on hand, never from the API."""

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> JsonObjectType:
        """Validate the arguments and answer, or say why there is no answer."""
        try:
            args = self.parameters(tool_input.tool_args)
        except vol.Invalid as err:
            return {"success": False, "error": f"Bad request: {err}"}
        try:
            result = self._answer(hass, args, dt_util.utcnow())
        except NoAnswerError as err:
            return {"success": False, "error": err.error, **err.extra}
        return {"success": True, "result": result}

    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        """Answer the question. Subclasses implement."""
        raise NotImplementedError


class WhatChangedTool(_CompaniesHouseTool):
    """What changed lately, everywhere or at one company."""

    name = f"{DOMAIN}__what_changed"
    description = (
        "List what changed at the watched companies and followed people over the "
        "last few days: filings, new and resigned officers, ownership changes, "
        "charges, status changes, accounts read and risk rating moves. Give a "
        "company to see only its changes."
    )
    parameters = vol.Schema(
        {
            vol.Optional(
                "days",
                default=DEFAULT_CHANGE_DAYS,
                description="How many days back to look (default 7).",
            ): _DAYS,
            vol.Optional("company", description=_COMPANY_DESCRIPTION): cv.string,
        }
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        company = find_company(hass, args["company"]) if args.get("company") else None
        return what_changed_answer(hass, days=args["days"], company=company, now=now)


class CompanyTool(_CompaniesHouseTool):
    """Everything about one company."""

    name = f"{DOMAIN}__company"
    description = (
        "Everything on hand about one watched company: status, filing deadlines, "
        "risk rating with its reasons, latest accounts figures with the change on "
        "the year before, current officers, people with significant control, "
        "charges and lenders, any strike-off countdown, recent changes and links "
        "to the register."
    )
    parameters = vol.Schema(
        {vol.Required("name", description=_COMPANY_DESCRIPTION): cv.string}
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return company_answer(find_company(hass, args["name"]), now)


class DeadlinesTool(_CompaniesHouseTool):
    """What is due, and what is overdue."""

    name = f"{DOMAIN}__deadlines"
    description = (
        "List the filing deadlines at the watched companies: annual accounts and "
        "confirmation statements due inside the next N days, anything already "
        "overdue, and the last day to object to a proposed strike-off."
    )
    parameters = vol.Schema(
        {
            vol.Optional(
                "days",
                default=DEADLINE_HORIZON_DAYS,
                description="How many days ahead to look (default 30).",
            ): _DAYS,
        }
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return deadlines_answer(hass, days=args["days"], now=now)


class FilingsTool(_CompaniesHouseTool):
    """One company's recent filings."""

    name = f"{DOMAIN}__filings"
    description = (
        "List what one watched company filed at Companies House recently, newest "
        "first, with a link to each document."
    )
    parameters = vol.Schema(
        {
            vol.Required("company", description=_COMPANY_DESCRIPTION): cv.string,
            vol.Optional(
                "days",
                default=DEFAULT_FILING_DAYS,
                description="How many days back to look (default 90).",
            ): _DAYS,
        }
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return filings_answer(
            find_company(hass, args["company"]), days=args["days"], now=now
        )


class PersonTool(_CompaniesHouseTool):
    """One person: their roles, records and changes."""

    name = f"{DOMAIN}__person"
    description = (
        "What is known about a person: for a followed person their current roles, "
        "the register records followed, whether they are disqualified and recent "
        "changes; for anyone else, the roles they hold at the watched companies."
    )
    parameters = vol.Schema(
        {
            vol.Required(
                "name", description="The person's name, or part of it."
            ): cv.string
        }
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return person_answer(hass, args["name"], now)


class ConnectionsTool(_CompaniesHouseTool):
    """Who sits with whom, who owns what."""

    name = f"{DOMAIN}__connections"
    description = (
        "The connections between the watched companies and followed people: "
        "shared boards, ownership chains, common lenders and the ones worth "
        "knowing about. Give a company or person to see just their connections."
    )
    parameters = vol.Schema(
        {
            vol.Optional(
                "name",
                description="A company or person to centre on; leave out for the whole map.",
            ): cv.string
        }
    )

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return connections_answer(hass, args.get("name"))


class RiskTool(_CompaniesHouseTool):
    """The red and amber companies."""

    name = f"{DOMAIN}__risk"
    description = (
        "List the watched companies rated red or amber for counterparty risk, "
        "worst first, each with the reasons, plus how many are green."
    )
    parameters = vol.Schema({})

    @override
    def _answer(
        self, hass: HomeAssistant, args: dict[str, Any], now: datetime
    ) -> JsonDict:
        return risk_answer(hass)


TOOLS: tuple[type[_CompaniesHouseTool], ...] = (
    WhatChangedTool,
    CompanyTool,
    DeadlinesTool,
    FilingsTool,
    PersonTool,
    ConnectionsTool,
    RiskTool,
)


@callback
def _anything_exposed(
    hass: HomeAssistant, entries: list[CompaniesHouseConfigEntry], assistant: str
) -> bool:
    """Whether the user has exposed any of the integration's entities to the assistant."""
    registry = er.async_get(hass)
    return any(
        async_should_expose(hass, assistant, entity.entity_id)
        for entry in entries
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    )


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: LLMContext, api_id: str
) -> LLMTools | None:
    """Return the tools for the Assist API once something of ours is exposed."""
    if api_id != LLM_API_ASSIST:
        return None
    entries = _loaded_entries(hass)
    if not entries or not _anything_exposed(hass, entries, llm_context.assistant):
        return None
    return LLMTools(tools=[tool() for tool in TOOLS], prompt=PROMPT)


__all__ = ["PROMPT", "TOOLS", "async_get_tools", "find_company"]
