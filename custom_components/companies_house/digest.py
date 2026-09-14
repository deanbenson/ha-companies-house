"""Plain-English change descriptions, and the report (digest) builder.

Everything here works from what is already in memory and in the store, so
building a report costs no API requests.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from html import escape
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .charges import charge_filing_link, charges_overview
from .const import FIND_AND_UPDATE_BASE, FINISHED_STATUSES
from .enumerations import COMPANY_STATUS, COMPANY_STATUS_DETAIL
from .gazette import notice_link
from .models import JsonDict, display_name
from .risk import BAND_AMBER, BAND_RANK, BAND_RED, band_worsened
from .scheduler import days_until

if TYPE_CHECKING:
    from .coordinator import CompaniesHouseConfigEntry, CompanyRuntime, OfficerRuntime

# Google's favicon service: a real image for any site, PNG or JPEG (email
# clients show those), a plain globe when the site has none.
LOGO_URL = (
    "https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
    "&fallback_opts=TYPE,SIZE,URL&url=https://{domain}&size=128"
)
DEADLINE_HORIZON_DAYS = 30

# ---------------------------------------------------------------- describing


def logo_for(website: str) -> str:
    """Return a logo image address for a website, or empty when there is none."""
    domain = (
        (website or "")
        .strip()
        .removeprefix("https://")
        .removeprefix("http://")
        .split("/")[0]
        .removeprefix("www.")
    )
    return LOGO_URL.format(domain=domain) if domain else ""


def _company_link(number: str) -> str:
    return f"{FIND_AND_UPDATE_BASE}/company/{number}"


def _officer_link(officer_id: str) -> str:
    return f"{FIND_AND_UPDATE_BASE}/officers/{officer_id}/appointments"


def _document_link(number: str, transaction_id: str | None) -> str:
    if not transaction_id:
        return _company_link(number) + "/filing-history"
    return (
        f"{_company_link(number)}/filing-history/{transaction_id}"
        "/document?format=pdf&download=0"
    )


# The pages on the register worth a click for a company: (label, path).
_COMPANY_PAGES = (
    ("Filing history", "/filing-history"),
    ("Officers", "/officers"),
    ("People with control", "/persons-with-significant-control"),
    ("Charges", "/charges"),
)


def company_pages(number: str, *, insolvency: bool = False) -> list[JsonDict]:
    """Return the register's pages for a company, for a row of quick links."""
    pages = [
        {"text": text, "href": _company_link(number) + path}
        for text, path in _COMPANY_PAGES
    ]
    if insolvency:
        pages.append(
            {"text": "Insolvency", "href": _company_link(number) + "/insolvency"}
        )
    return pages


def _charge_pdf_known(event_type: str, payload: JsonDict) -> bool:
    """Whether a charge change opens a filing's PDF rather than the charges page."""
    if event_type in ("satisfied", "part-satisfied"):
        return bool(payload.get("satisfied_transaction_id"))
    return bool(payload.get("created_transaction_id"))


def _more_links(
    kind: str, payload: JsonDict, number: str, event_type: str = ""
) -> list[JsonDict]:
    """Return further register pages worth a click for one change."""
    company_number = number or str(payload.get("company_number") or "")
    if not company_number:
        return []
    company = _company_link(company_number)
    links: list[JsonDict] = []
    if kind == "filing":
        links.append({"text": "Filing history", "href": company + "/filing-history"})
    elif kind == "officer":
        links.append({"text": "Officers", "href": company + "/officers"})
    elif kind == "psc":
        links.append(
            {
                "text": "People with control",
                "href": company + "/persons-with-significant-control",
            }
        )
    elif kind == "charge" and _charge_pdf_known(event_type, payload):
        # The change itself opens the filing's PDF; this is the page behind it.
        links.append({"text": "Charges", "href": company + "/charges"})
    elif kind == "status":
        links.append(
            {
                "text": "Gazette notices",
                "href": company + "/filing-history?category=gazette",
            }
        )
    elif kind == "appointment":
        links.append({"text": "Officers", "href": company + "/officers"})
    if not number:
        # A person's change: the company page itself is the second link.
        links.insert(0, {"text": "Company", "href": company})
    return links


def _on(value: Any) -> str:
    """Return a date as people write it, or say it is unknown."""
    return _pretty_date(value) if value else "an unknown date"


def _status_word(status: str | None) -> str:
    if not status:
        return "unknown"
    return str(COMPANY_STATUS.get(status, status)).lower()


def _role(role: str | None) -> str:
    return (role or "officer").replace("-", " ")


def _sentence(text: str) -> str:
    """End a sentence with a full stop, unless it already trails off with "…"."""
    return text if text.endswith(("…", ".")) else f"{text}."


def _describe_charge(
    event_type: str, p: JsonDict, *, subject: str, number: str
) -> tuple[str, str, str]:
    """Describe a charge change: who lent, secured on what, created or satisfied.

    The link opens the filing's PDF (the MR01 that created the charge, the
    MR04 that satisfied it) when the register gave its id, else the charges
    page. A satisfaction the register has not yet listed a filing for links
    to the charges page rather than to the deed that created the charge.
    """
    who = (
        str(p.get("lender") or "")
        or ", ".join(str(x) for x in p.get("persons_entitled") or [])
        or "a lender"
    )
    security = str(p.get("security") or "")
    detail = f" — {security}" if security else ""
    created = (
        f" (created {_pretty_date(p['created_on'])})" if p.get("created_on") else ""
    )
    creation_pdf = (
        charge_filing_link(number, p.get("created_transaction_id")) if number else ""
    )
    satisfaction_pdf = (
        charge_filing_link(number, p.get("satisfied_transaction_id")) if number else ""
    )
    if event_type == "created":
        return (
            f"{subject}: new charge registered",
            _sentence(
                f"A charge in favour of {who} was registered"
                + (
                    f" on {_pretty_date(p['created_on'])}"
                    if p.get("created_on")
                    else ""
                )
                + detail
            ),
            creation_pdf,
        )
    if event_type == "acquired":
        return (
            f"{subject}: charge acquired with property",
            _sentence(
                f"Property acquired on {_on(p.get('acquired_on'))} came with a "
                f"charge in favour of {who}{created}{detail}"
            ),
            creation_pdf,
        )
    if event_type == "satisfied":
        return (
            f"{subject}: charge satisfied",
            f"The charge in favour of {who}{created} was satisfied in full"
            + (
                f" on {_pretty_date(p['satisfied_on'])}"
                if p.get("satisfied_on")
                else ""
            )
            + ".",
            satisfaction_pdf,
        )
    return (
        f"{subject}: charge part satisfied",
        _sentence(
            f"The charge in favour of {who}{created} has been partly satisfied{detail}"
        ),
        satisfaction_pdf,
    )


def describe_change(
    kind: str, event_type: str, payload: JsonDict, *, subject: str, number: str = ""
) -> tuple[str, str, str]:
    """Return (title, message, link) for a change, in plain English.

    ``subject`` is the company or person the change belongs to. The link is
    the most useful page on the Companies House website for it.
    """
    p = payload
    link = _company_link(number) if number else ""
    name = display_name(str(p.get("name") or ""))
    company = str(p.get("company_name") or "")

    if kind == "filing":
        what = str(p.get("rendered_description") or p.get("description") or "filing")
        titles = {
            "accounts": "Accounts filed",
            "confirmation-statement": "Confirmation statement filed",
            "officers": "Officer change filed",
            "persons-with-significant-control": "Ownership change filed",
            "address": "Address change filed",
            "capital": "Share capital change filed",
            "charges": "Charge filed",
            "mortgage": "Mortgage filed",
            "change-of-name": "Name change filed",
            "resolution": "Resolution filed",
            "incorporation": "Incorporation filed",
            "gazette": "Gazette notice",
        }
        title = f"{subject}: {titles.get(event_type, 'New filing')}"
        return title, what, _document_link(number, p.get("transaction_id"))

    if kind == "status":
        if event_type == "strike-off-proposed":
            detail = str(p.get("detail") or "").rstrip(".")
            if not detail or detail in COMPANY_STATUS_DETAIL:
                # Seen in the register's status detail, not in a Gazette
                # notice filing: never echo the register's key.
                detail = "Companies House has proposed to strike the company off"
                if not p.get("earliest_on"):
                    detail += "; the Gazette notice has not appeared in the filings yet"
            if p.get("earliest_on"):
                # The probe saw the Gazette notice itself, so the clock is known.
                detail += (
                    f" on {_pretty_date(p.get('notice_on'))}. The company can be "
                    f"struck off from {_pretty_date(p['earliest_on'])}; object "
                    f"by {_pretty_date(p.get('objection_deadline'))}"
                )
            return (
                f"{subject}: strike-off proposed",
                detail + ".",
                notice_link(number, str(p["transaction_id"]))
                if number and p.get("transaction_id")
                else link,
            )
        if event_type == "strike-off-suspended":
            detail = str(p.get("detail") or "The strike-off has been suspended").rstrip(
                "."
            )
            if p.get("earliest_on"):
                detail += (
                    f" on {_pretty_date(p.get('suspended_on'))}. The company "
                    f"cannot be struck off before {_pretty_date(p['earliest_on'])}"
                )
            return (
                f"{subject}: strike-off suspended",
                detail + ".",
                notice_link(number, None) if number else link,
            )
        if event_type == "strike-off-discontinued":
            return (
                f"{subject}: strike-off dropped",
                "The proposal to strike the company off has been discontinued.",
                link,
            )
        if event_type == "dissolved":
            return f"{subject}: dissolved", "The company has been dissolved.", link
        if event_type == "risk-changed":
            band = str(p.get("new_band") or "unknown")
            was = str(p.get("old_band") or "unknown")
            return (
                f"{subject}: risk now {band}",
                str(p.get("reason") or f"{band.capitalize()}: was {was}."),
                link,
            )
        return (
            f"{subject}: now {_status_word(p.get('new_status'))}",
            (
                f"Status changed from {_status_word(p.get('old_status'))} "
                f"to {_status_word(p.get('new_status'))}."
            ),
            link,
        )

    if kind == "profile":
        old, new = p.get("old_value"), p.get("new_value")
        if event_type == "name-changed":
            return f"{subject}: renamed", f"Now called {new}. Was {old}.", link
        if event_type == "address-changed":
            return f"{subject}: registered office moved", f"Now at {new}.", link
        if event_type == "sic-changed":
            return (
                f"{subject}: business activity changed",
                f"SIC codes now {', '.join(map(str, new or []))}.",
                link,
            )
        return (
            f"{subject}: year end changed",
            f"Accounting reference date now {new}. Was {old}.",
            link,
        )

    if kind == "officer":
        role = _role(p.get("role"))
        if event_type == "appointed":
            return (
                f"{subject}: new {role}",
                f"{name} was appointed {role} on {_on(p.get('appointed_on'))}.",
                link + "/officers",
            )
        if event_type == "resigned":
            return (
                f"{subject}: {role} resigned",
                f"{name} resigned as {role} on {_on(p.get('resigned_on'))}.",
                link + "/officers",
            )
        return (
            f"{subject}: officer details changed",
            f"Details changed for {name} ({role}).",
            link + "/officers",
        )

    if kind == "psc":
        control = ", ".join(
            str(n).replace("-", " ") for n in p.get("natures_of_control") or []
        )
        if event_type == "notified":
            return (
                f"{subject}: new controlling person",
                f"{name} now has significant control"
                + (f": {control}." if control else "."),
                link + "/persons-with-significant-control",
            )
        if event_type == "ceased":
            return (
                f"{subject}: controlling person ceased",
                f"{name} no longer has significant control.",
                link + "/persons-with-significant-control",
            )
        if event_type == "statement-added":
            return (
                f"{subject}: ownership statement added",
                str(p.get("statement") or "A PSC statement was added."),
                link + "/persons-with-significant-control",
            )
        return (
            f"{subject}: controlling person details changed",
            f"Details changed for {name}.",
            link + "/persons-with-significant-control",
        )

    if kind == "charge":
        return _describe_charge(event_type, p, subject=subject, number=number)

    if kind == "insolvency":
        return (
            f"{subject}: insolvency update",
            str(p.get("description") or p.get("type") or "An insolvency case changed."),
            link + "/insolvency",
        )

    if kind == "appointment":
        role = _role(p.get("role"))
        where = company or str(p.get("company_number") or "a company")
        target = (
            _company_link(str(p.get("company_number")))
            if p.get("company_number")
            else ""
        )
        if event_type == "appointed":
            return (
                f"{subject}: new role",
                f"Appointed {role} at {where} on {_on(p.get('appointed_on'))}.",
                target,
            )
        if event_type == "resigned":
            return (
                f"{subject}: stepped down",
                f"Resigned as {role} at {where} on {_on(p.get('resigned_on'))}.",
                target,
            )
        if event_type == "company-status-changed":
            return (
                f"{subject}: a company changed status",
                (
                    f"{where} is now {_status_word(p.get('company_status'))}"
                    f" (was {_status_word(p.get('old_status'))})."
                ),
                target,
            )
        if event_type == "disqualified":
            return (
                f"{subject}: disqualified as a director",
                f"Disqualified until {_on(p.get('disqualified_until'))}"
                + (f": {p.get('reason')}" if p.get("reason") else "")
                + ".",
                "",
            )
        if event_type == "new-record":
            return (
                f"{subject}: new register record found",
                "Companies House opened another record for this person; it is now followed too.",
                _officer_link(str(p.get("officer_id") or "")),
            )
        if event_type == "company-now-watched":
            return (
                f"{subject}: now watching {where}",
                f"{where} was added because {subject} holds a role there.",
                target,
            )

    return f"{subject}: {event_type.replace('-', ' ')}", "", link


# ---------------------------------------------------------------- collecting

# How much a kind of change matters, before the company's own weight.
_KIND_WEIGHT: dict[tuple[str, str], int] = {
    ("status", "strike-off-proposed"): 10,
    ("status", "dissolved"): 10,
    ("status", "status-changed"): 9,
    ("status", "risk-changed"): 9,
    ("status", "strike-off-suspended"): 8,
    ("status", "strike-off-discontinued"): 6,
    ("filing", "gazette"): 9,
    ("charge", "created"): 8,
    ("charge", "acquired"): 8,
    ("filing", "mortgage"): 8,
    ("filing", "charges"): 8,
    ("psc", "notified"): 7,
    ("psc", "ceased"): 7,
    ("psc", "statement-added"): 5,
    ("psc", "details-changed"): 3,
    ("officer", "appointed"): 6,
    ("officer", "resigned"): 6,
    ("officer", "details-changed"): 2,
    ("filing", "accounts"): 5,
    ("filing", "capital"): 5,
    ("filing", "change-of-name"): 5,
    ("filing", "resolution"): 4,
    ("filing", "incorporation"): 5,
    ("profile", "name-changed"): 5,
    ("profile", "address-changed"): 3,
    ("profile", "sic-changed"): 3,
    ("profile", "accounting-reference-date-changed"): 3,
    ("filing", "officers"): 3,
    ("filing", "persons-with-significant-control"): 3,
    ("filing", "address"): 2,
    ("charge", "satisfied"): 3,
    ("charge", "part-satisfied"): 2,
    ("filing", "confirmation-statement"): 1,
    ("appointment", "appointed"): 6,
    ("appointment", "resigned"): 6,
    ("appointment", "disqualified"): 10,
    ("appointment", "company-status-changed"): 6,
    ("appointment", "company-now-watched"): 4,
    ("appointment", "new-record"): 3,
}
_ONGOING_STATUSES = ("liquidation", "administration", "receivership")
NEW_COMPANY_DAYS = 90
# How many connection lines the report shows; the map itself shows them all.
CONNECTION_LINES = 8
ATTACH_LIMIT = 8
ATTACH_BYTES_LIMIT = 20_000_000


def score_change(kind: str, event_type: str, *, weight: int) -> int:
    """Return how much a change is worth looking at: kind times company weight."""
    return _KIND_WEIGHT.get((kind, event_type), 2) * weight


def _company_weight(company: CompanyRuntime) -> int:
    """Your own (close watch) companies first, then the ones you want alerts from."""
    if company.close_watch:
        return 3
    if company.notify_instantly:
        return 2
    return 1


def _since(changes: Iterable[JsonDict], since: datetime) -> list[JsonDict]:
    out = []
    for change in changes:
        at = dt_util.parse_datetime(str(change.get("at") or ""))
        if at is not None and at >= since:
            out.append(change)
    return out


def _initials(name: str) -> str:
    words = [w for w in name.replace("(", " ").split() if w[0].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def _control_words(natures: Iterable[str]) -> str:
    """Turn natures of control into a short phrase: "75-100% of the shares"."""
    parts: list[str] = []
    for nature in natures:
        n = str(nature)
        if n.startswith("ownership-of-shares"):
            band = n.removeprefix("ownership-of-shares-").split("-percent")[0]
            parts.append(f"{band.replace('-to-', ' to ')}% of the shares")
        elif n.startswith("voting-rights"):
            band = n.removeprefix("voting-rights-").split("-percent")[0]
            parts.append(f"{band.replace('-to-', ' to ')}% of the votes")
        elif n.startswith("right-to-appoint-and-remove-directors"):
            parts.append("the right to appoint and remove directors")
        elif n.startswith("significant-influence-or-control"):
            parts.append("significant influence or control")
        else:
            parts.append(n.replace("-", " "))
    seen: list[str] = []
    for part in parts:
        if part not in seen:
            seen.append(part)
    return ", ".join(seen)


def _change_entry(
    change: JsonDict, *, subject: str, number: str, weight: int
) -> JsonDict:
    kind = str(change.get("kind", ""))
    event_type = str(change.get("event_type", ""))
    payload = dict(change.get("payload") or {})
    title, message, link = describe_change(
        kind, event_type, payload, subject=subject, number=number
    )
    if kind == "psc" and event_type == "notified" and payload.get("natures_of_control"):
        message = (
            f"{payload.get('name')} now controls "
            f"{_control_words(payload['natures_of_control'])}."
        )
    return {
        "at": change.get("at"),
        "kind": kind,
        "event_type": event_type,
        "title": title,
        "message": message,
        "link": link,
        "score": score_change(kind, event_type, weight=weight),
        "document_id": payload.get("document_id"),
        "transaction_id": payload.get("transaction_id"),
        "links": _more_links(kind, payload, number, event_type),
    }


def _midnight(day: date) -> str:
    return dt_util.start_of_local_day(day).isoformat()


def _register_filings(
    company: CompanyRuntime, since_day: date, logged: set[str]
) -> list[JsonDict]:
    """Filings the register dates inside the period that the log has not got.

    The change log only starts when a company is first watched, and it
    records when a filing was noticed rather than when it was made. The
    filing history itself says what was filed this period, so the report
    reads from that too and the two are merged by transaction id.
    """
    data = company.probe.data
    if data is None:
        return []
    return [
        {
            "at": _midnight(item.date),
            "kind": "filing",
            "event_type": item.event_type,
            "payload": item.event_payload(),
        }
        for item in data.items
        if item.date is not None
        and item.date >= since_day
        and item.transaction_id not in logged
    ]


def _register_appointments(
    officer: OfficerRuntime, since_day: date, logged: set[tuple[str, str]]
) -> list[JsonDict]:
    """Roles the register says started or ended this period, if not logged."""
    data = officer.appointments.data
    if data is None:
        return []
    out: list[JsonDict] = []
    for appointment in data.items:
        payload = appointment.event_payload()
        started = appointment.appointed_on
        ended = appointment.resigned_on
        for event_type, day in (("appointed", started), ("resigned", ended)):
            if (
                day is not None
                and day >= since_day
                and (appointment.company_number, event_type) not in logged
            ):
                out.append(
                    {
                        "at": _midnight(day),
                        "kind": "appointment",
                        "event_type": event_type,
                        "payload": payload,
                    }
                )
    return out


def _risk_worsened(logged: Iterable[JsonDict]) -> bool:
    """Whether a logged rating change in the period moved the band the wrong way."""
    return any(
        c.get("kind") == "status"
        and c.get("event_type") == "risk-changed"
        and band_worsened(
            (c.get("payload") or {}).get("old_band"),
            (c.get("payload") or {}).get("new_band"),
        )
        for c in logged
    )


def _attention(
    company: CompanyRuntime,
    changes: list[JsonDict],
    since: datetime,
    today: date,
    *,
    risk_worsened: bool = False,
) -> list[JsonDict]:
    """Return the company's open problems, each marked new or ongoing.

    New means it started inside the period: a strike-off or status change
    seen in the log, or a deadline that passed during the period. Anything
    older is ongoing and belongs in the report's "still open" line, not at
    the top.
    """
    profile = company.profile.data
    if profile is None:
        return []
    events = {(c["kind"], c["event_type"]) for c in changes}
    out: list[JsonDict] = []

    def add(
        issue: str,
        *,
        new: bool,
        since_date: date | None = None,
        short: str | None = None,
        links: list[JsonDict] | None = None,
        kind: str | None = None,
    ) -> None:
        out.append(
            {
                "issue": issue,
                "new": new,
                "since": _iso(since_date),
                "short": short or issue,
                "links": links or [],
                "kind": kind,
            }
        )

    countdown = company.strike_off_countdown(today)
    if countdown is not None:
        # Logged as a status change when noticed; a Gazette notice dated this
        # period says the same thing when read from the register.
        add(
            countdown.attention_line(),
            new=("status", "strike-off-proposed") in events
            or ("status", "strike-off-suspended") in events
            or ("filing", "gazette") in events,
            since_date=countdown.suspended_on or countdown.notice_on,
            short="Strike-off suspended"
            if countdown.suspended
            else "Strike-off proposed",
            links=[
                {
                    "text": "Gazette notice"
                    if countdown.transaction_id
                    else "Gazette notices",
                    "href": notice_link(
                        company.company_number, countdown.transaction_id
                    ),
                }
            ],
        )
    if profile.company_status in _ONGOING_STATUSES:
        add(
            f"In {_status_word(profile.company_status)}",
            new=("status", "status-changed") in events,
        )
    since_day = since.date()
    if profile.accounts.next_overdue and profile.accounts.next_due:
        due = profile.accounts.next_due
        add("Accounts overdue", new=since_day <= due <= today, since_date=due)
    if (
        profile.confirmation_statement.overdue
        and profile.confirmation_statement.next_due
    ):
        due = profile.confirmation_statement.next_due
        add(
            "Confirmation statement overdue",
            new=since_day <= due <= today,
            since_date=due,
        )
    risk = company.risk
    if (
        risk is not None
        and risk.band in (BAND_AMBER, BAND_RED)
        # A dissolved company is red for being dissolved, which the status
        # line already says; the report keeps finished companies quiet.
        and not is_finished(profile.company_status)
    ):
        # New when the band got worse this period. The reasons are in the
        # report's own Risk section, so the badge stays short.
        add(f"Risk rating {risk.band}", new=risk_worsened, kind="risk")
    return out


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _charges_meta(company: CompanyRuntime) -> JsonDict | None:
    """Return what the company card says about charges: how many, and to whom."""
    overview = charges_overview(company)
    if overview is None:
        return None
    return {
        "outstanding": overview["outstanding"],
        "total": overview["total"],
        "lenders": overview["lenders"],
        "link": overview["link"],
    }


def _company_card(company: CompanyRuntime, since: datetime, today: date) -> JsonDict:
    profile = company.profile.data
    website = (company.website or "").strip()
    deadline = profile.next_deadline if profile else None
    days = days_until(deadline[0], today) if deadline else None
    weight = _company_weight(company)
    logged = _since(company.state.changes, since)
    filed = _register_filings(
        company,
        since.date(),
        {
            str((c.get("payload") or {}).get("transaction_id") or "")
            for c in logged
            if c.get("kind") == "filing"
        },
    )
    changes = [
        _change_entry(
            c,
            subject=company.company_name,
            number=company.company_number,
            weight=weight,
        )
        for c in logged + filed
    ]
    changes.sort(key=lambda c: (-c["score"], str(c["at"])))
    countdown = company.strike_off_countdown(today)
    return {
        "name": company.company_name,
        "number": company.company_number,
        "status": _status_word(profile.company_status) if profile else "unknown",
        "label": company.label,
        "website": website,
        "logo": logo_for(website),
        "initials": _initials(company.company_name),
        "link": _company_link(company.company_number),
        "close_watch": company.close_watch,
        "pages": company_pages(
            company.company_number,
            insolvency=bool(
                profile
                and (
                    profile.has_insolvency_history
                    or profile.company_status in _ONGOING_STATUSES
                )
            ),
        ),
        "weight": weight,
        "incorporated": _iso(profile.date_of_creation) if profile else None,
        "next_deadline": {
            "what": deadline[1],
            "date": deadline[0].isoformat(),
            "days": days,
        }
        if deadline
        else None,
        # A live strike-off: the countdown, and the summary line the report
        # and the Assist tools use ("Strike-off proposed (compulsory) — 41
        # days to object").
        "strike_off": {
            **countdown.as_dict(),
            "summary": countdown.attention_line(),
            "link": notice_link(company.company_number, countdown.transaction_id),
        }
        if countdown is not None
        else None,
        "attention": _attention(
            company, changes, since, today, risk_worsened=_risk_worsened(logged)
        ),
        "charges": _charges_meta(company),
        "changes": changes,
        "score": max((c["score"] for c in changes), default=0),
        "risk": {
            "band": company.risk.band,
            "score": company.risk.score,
            "reason": company.risk.reason,
            "reasons": list(company.risk.reasons),
        }
        if company.risk is not None
        else None,
        "finished": bool(profile and is_finished(profile.company_status)),
    }


def _person_card(officer: OfficerRuntime, since: datetime) -> JsonDict:
    data = officer.appointments.data
    current = (
        sorted(
            (a for a in data.items if a.is_active),
            key=lambda a: a.appointed_on or a.appointed_before or date.min,
            reverse=True,
        )
        if data
        else []
    )
    weight = 2 if officer.notify_instantly else 1
    logged = _since(officer.state.changes, since)
    moved = _register_appointments(
        officer,
        since.date(),
        {
            (
                str((c.get("payload") or {}).get("company_number") or ""),
                str(c["event_type"]),
            )
            for c in logged
            if c.get("kind") == "appointment"
        },
    )
    changes = [
        _change_entry(c, subject=officer.officer_name, number="", weight=weight)
        for c in logged + moved
    ]
    changes.sort(key=lambda c: (-c["score"], str(c["at"])))
    return {
        "name": officer.officer_name,
        "officer_id": officer.officer_id,
        "records": len(officer.officer_ids),
        "initials": _initials(officer.officer_name),
        "link": _officer_link(officer.officer_id),
        "companies": [
            {
                "name": a.company_name,
                "number": a.company_number,
                "link": _company_link(a.company_number),
                "role": _role(a.officer_role),
                "appointed_on": _iso(a.appointed_on or a.appointed_before),
            }
            for a in current
        ],
        "changes": changes,
        "score": max((c["score"] for c in changes), default=0),
    }


def _normalise_person(name: str) -> str:
    """Casefold a name and drop titles and punctuation, for matching."""
    cleaned = name.replace(",", " ").replace(".", " ")
    words = [
        w.casefold()
        for w in cleaned.split()
        if w.casefold()
        not in {"mr", "mrs", "ms", "miss", "dr", "sir", "obe", "dl", "mbe"}
    ]
    return " ".join(sorted(words))


def _ownership(
    companies: Iterable[CompanyRuntime], people: Iterable[OfficerRuntime]
) -> list[JsonDict]:
    """Who controls what, across every watched company with PSC data."""
    followed = {_normalise_person(o.officer_name): o.officer_name for o in people}
    for o in people:
        followed[_normalise_person(o.register_name)] = o.officer_name
    watched = {c.company_number: c.company_name for c in companies}
    holders: dict[str, JsonDict] = {}
    for company in companies:
        if company.psc is None or company.psc.data is None:
            continue
        for psc in company.psc.data.items:
            if psc.ceased:
                continue
            key = _normalise_person(psc.name)
            holder = holders.setdefault(
                key,
                {
                    "name": followed.get(key, psc.name),
                    "followed": key in followed,
                    "kind": (psc.kind or "").replace(
                        "-person-with-significant-control", ""
                    ),
                    "holdings": [],
                },
            )
            holder["holdings"].append(
                {
                    "company": company.company_name,
                    "number": company.company_number,
                    "link": _company_link(company.company_number),
                    "control": _control_words(psc.natures_of_control),
                    "watched": company.company_number in watched,
                }
            )
    result = list(holders.values())
    result.sort(key=lambda h: (not h["followed"], -len(h["holdings"]), h["name"]))
    return result


def _new_companies(
    companies: list[JsonDict], people: list[JsonDict], since: datetime, today: date
) -> list[JsonDict]:
    """Watched companies incorporated recently, with the followed people at them."""
    cutoff = today - timedelta(days=NEW_COMPANY_DAYS)
    since_day = since.date()
    out: list[JsonDict] = []
    for card in companies:
        created = card.get("incorporated")
        if not created or date.fromisoformat(created) < cutoff:
            continue
        founders = [
            {
                "name": p["name"],
                "role": h["role"],
                "appointed_on": h.get("appointed_on"),
            }
            for p in people
            for h in p["companies"]
            if h["number"] == card["number"]
        ]
        # Only once: the week it was set up, first watched, or first joined.
        seen_this_period = (
            date.fromisoformat(created) >= since_day
            or any(c["event_type"] == "company-now-watched" for c in card["changes"])
            or any(
                f["appointed_on"] and date.fromisoformat(f["appointed_on"]) >= since_day
                for f in founders
            )
        )
        if not seen_this_period:
            continue
        out.append(
            {
                "company": card["name"],
                "number": card["number"],
                "link": card["link"],
                "incorporated": created,
                "new_this_period": seen_this_period,
                "people": founders,
            }
        )
    out.sort(key=lambda n: n["incorporated"], reverse=True)
    return out


def _has_issue(card: JsonDict, *, new: bool) -> bool:
    return any(a["new"] is new for a in card["attention"])


def _issue_row(card: JsonDict, *, new: bool) -> JsonDict:
    """Return a company's new (or ongoing) problems with the links worth a click."""
    issues = [a for a in card["attention"] if a["new"] is new]
    return {
        "company": card["name"],
        "number": card["number"],
        "link": card["link"],
        "issues": [a["issue"] for a in issues],
        "links": [link for a in issues for link in a.get("links") or []],
    }


def _objection_deadlines(companies: list[JsonDict]) -> list[JsonDict]:
    """Return the last days to object to a strike-off inside the horizon."""
    out: list[JsonDict] = []
    for card in companies:
        countdown = card.get("strike_off")
        if not countdown or countdown["days_to_object"] is None:
            continue
        if not 0 <= countdown["days_to_object"] <= DEADLINE_HORIZON_DAYS:
            continue
        out.append(
            {
                "what": "object to strike-off",
                "date": countdown["objection_deadline"],
                "days": countdown["days_to_object"],
                "company": card["name"],
                "number": card["number"],
                "link": card["link"],
                "document": countdown["link"],
            }
        )
    return out


def _risk_counts(companies: list[JsonDict]) -> dict[str, int]:
    """Count the rated companies by band: red, amber, green, unknown."""
    counts = {"red": 0, "amber": 0, "green": 0, "unknown": 0}
    for card in companies:
        risk = card.get("risk")
        band = risk["band"] if risk and risk.get("band") else "unknown"
        counts[band] = counts.get(band, 0) + 1
    return counts


def _risk_list(companies: list[JsonDict]) -> list[JsonDict]:
    """List the amber and red companies, worst first, with the one-line reason.

    Finished companies are left out: they are red for being dissolved, which
    is not news, and the counts in the summary still include them.
    """
    out = [
        {
            "company": card["name"],
            "number": card["number"],
            "link": card["link"],
            "band": card["risk"]["band"],
            "score": card["risk"]["score"],
            "reason": card["risk"]["reason"],
            "reasons": list(card["risk"]["reasons"]),
            "pages": card.get("pages") or [],
        }
        for card in companies
        if card.get("risk")
        and card["risk"]["band"] in (BAND_AMBER, BAND_RED)
        and not card.get("finished")
    ]
    out.sort(key=lambda r: (-BAND_RANK.get(r["band"], 0), -r["score"], r["company"]))
    return out


def build_digest(
    entry: CompaniesHouseConfigEntry, *, days: int, now: datetime | None = None
) -> JsonDict:
    """Gather everything a report needs, for the companies and people opted in."""
    now = now or dt_util.utcnow()
    today = dt_util.as_local(now).date()
    since = now - timedelta(days=days)
    runtime = entry.runtime_data
    all_companies = [
        c for c in runtime.companies.values() if c.profile.data is not None
    ]
    all_people = [
        o for o in runtime.officers.values() if o.appointments.data is not None
    ]
    companies = [
        _company_card(c, since, today) for c in all_companies if c.in_weekly_report
    ]
    people = [_person_card(o, since) for o in all_people if o.in_weekly_report]
    companies.sort(key=lambda c: (-c["score"], -c["weight"], c["name"]))
    people.sort(key=lambda p: (-p["score"], p["name"]))
    deadlines = sorted(
        [
            {
                **c["next_deadline"],
                "company": c["name"],
                "number": c["number"],
                "link": c["link"],
            }
            for c in companies
            if c["next_deadline"]
            and c["next_deadline"]["days"] is not None
            and 0 <= c["next_deadline"]["days"] <= DEADLINE_HORIZON_DAYS
        ]
        + _objection_deadlines(companies),
        key=lambda d: (d["date"], d["company"]),
    )
    new_issues = [_issue_row(c, new=True) for c in companies if _has_issue(c, new=True)]
    still_open = [
        _issue_row(c, new=False) for c in companies if _has_issue(c, new=False)
    ]
    changed_companies = [c for c in companies if c["changes"]]
    changed_people = [p for p in people if p["changes"]]
    all_changes = [
        {
            **change,
            "subject": c["name"],
            "number": c["number"],
            "logo": c["logo"],
            "initials": c["initials"],
            "subject_link": c["link"],
        }
        for c in changed_companies
        for change in c["changes"]
    ] + [
        {
            **change,
            "subject": p["name"],
            "number": "",
            "logo": "",
            "initials": p["initials"],
            "subject_link": p["link"],
        }
        for p in changed_people
        for change in p["changes"]
    ]
    all_changes.sort(key=lambda c: (-c["score"], str(c["at"])))
    by_kind: dict[str, int] = {}
    for change in all_changes:
        by_kind[change["kind"]] = by_kind.get(change["kind"], 0) + 1
    new_companies = _new_companies(companies, people, since, today)
    connections = _connections(entry, days=days, now=now)
    risk = _risk_list(companies)
    return {
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "days": days,
        "summary": {
            "companies": len(companies),
            "people": len(people),
            "changes": len(all_changes),
            "by_kind": by_kind,
            "companies_with_changes": len(changed_companies),
            "people_with_changes": len(changed_people),
            "new_issues": len(new_issues),
            "still_open": len(still_open),
            "deadlines_soon": len(deadlines),
            "new_companies": len(new_companies),
            "connections": len(connections),
            "risk": _risk_counts(companies),
        },
        "top": all_changes[:5],
        "needs_attention": new_issues,
        "still_open": still_open,
        "deadlines": deadlines,
        "new_companies": new_companies,
        "risk": risk,
        "companies": changed_companies,
        "people": changed_people,
        "ownership": _ownership(all_companies, all_people),
        "connections": connections,
        "quiet_companies": [c["name"] for c in companies if not c["changes"]],
    }


def _connections(
    entry: CompaniesHouseConfigEntry, *, days: int, now: datetime
) -> list[JsonDict]:
    """Return the connections worth knowing about, most serious first, capped.

    A company or person opted out of the report is left out of these lines
    too. A person is matched by any of their register records, since the
    node can carry a pooled record's id rather than the primary one.
    """
    # Imported here: the connections module reuses this module's helpers.
    from .connections import build_connections

    runtime = entry.runtime_data
    left_out = {
        f"company:{c.company_number}"
        for c in runtime.companies.values()
        if not c.in_weekly_report
    }
    quiet_records = {
        record
        for o in runtime.officers.values()
        if not o.in_weekly_report
        for record in o.officer_ids
    }
    graph = build_connections(entry, days=days, now=now)
    left_out.update(
        n["id"]
        for n in graph["nodes"]
        if n["type"] == "person"
        and not quiet_records.isdisjoint(n["meta"].get("officer_ids") or [])
    )
    lines = [
        line for line in graph["interesting"] if left_out.isdisjoint(line["node_ids"])
    ]
    return [
        {
            "rule": line["rule"],
            "severity": line["severity"],
            "text": line["text"],
            "link": line["link"],
            "links": line["links"],
        }
        for line in lines[:CONNECTION_LINES]
    ]


def attachable_documents(digest: JsonDict) -> list[JsonDict]:
    """Return the filings worth attaching: accounts anywhere, anything at your own.

    Returned in score order and capped, so an email stays a sensible size.
    """
    wanted: list[JsonDict] = []
    for card in digest["companies"]:
        for change in card["changes"]:
            if change["kind"] != "filing" or not change.get("document_id"):
                continue
            if change["event_type"] == "accounts" or card["close_watch"]:
                wanted.append({**change, "company_number": card["number"]})
    wanted.sort(key=lambda c: -c["score"])
    return wanted[:ATTACH_LIMIT]


# ---------------------------------------------------------------- rendering

_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
_MUTED = "color:#6b7280;"
_KIND_LABEL = {
    "filing": "filing",
    "officer": "director change",
    "psc": "ownership change",
    "charge": "charge",
    "status": "status change",
    "profile": "details change",
    "appointment": "role change",
}


def _e(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def _avatar(logo: str, initials: str, colour: str = "#1f4e79", size: int = 40) -> str:
    if logo:
        return (
            f'<img src="{_e(logo)}" width="{size}" height="{size}" alt="" '
            f'style="width:{size}px;height:{size}px;border-radius:8px;display:block;'
            'background:#fff;object-fit:contain">'
        )
    return (
        f'<div style="width:{size}px;height:{size}px;border-radius:8px;background:{colour};'
        f'color:#fff;text-align:center;line-height:{size}px;font-weight:600;{_FONT}">'
        f"{_e(initials)}</div>"
    )


def _when(value: Any) -> str:
    parsed = dt_util.parse_datetime(str(value or ""))
    if parsed is None:
        return ""
    return dt_util.as_local(parsed).strftime("%a %-d %b")


def _pretty_date(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).strftime("%-d %b %Y")
    except ValueError:
        return str(value or "")


def _due(days: int | None) -> tuple[str, str]:
    if days is None:
        return "", _MUTED
    if days < 0:
        return f"{-days} days overdue", "color:#b91c1c;font-weight:600;"
    if days == 0:
        return "due today", "color:#b45309;font-weight:600;"
    if days <= 7:
        return f"in {days} days", "color:#b45309;font-weight:600;"
    return f"in {days} days", _MUTED


def _section(title: str, body: str, *, icon: str = "", intro: str = "") -> str:
    lead = (
        f'<p style="margin:0 0 10px 0;font-size:13px;{_MUTED}{_FONT}">{_e(intro)}</p>'
        if intro
        else ""
    )
    return (
        '<tr><td style="padding:24px 24px 0 24px">'
        f'<h2 style="margin:0 0 10px 0;font-size:17px;{_FONT}color:#111827">'
        f"{icon}&nbsp; {_e(title)}</h2>{lead}{body}</td></tr>"
    )


def _link(text: str, href: str, colour: str = "#1d4ed8") -> str:
    if not href:
        return _e(text)
    return f'<a href="{_e(href)}" style="color:{colour};text-decoration:none">{_e(text)}</a>'


def _link_row(links: list[JsonDict], *, size: int = 12) -> str:
    """Render a quiet row of links: "Filing history · Officers · Charges"."""
    if not links:
        return ""
    return (
        f'<div style="font-size:{size}px;{_FONT}margin-top:3px">'
        + " · ".join(_link(x["text"], x["href"]) for x in links)
        + "</div>"
    )


def _change_links(change: JsonDict) -> list[JsonDict]:
    """Return the links a change offers: the document or page first, then more."""
    links: list[JsonDict] = []
    if change.get("link"):
        is_pdf = change.get("document_id") or "format=pdf" in change["link"]
        links.append({"text": "Open PDF" if is_pdf else "Open", "href": change["link"]})
    links.extend(change.get("links") or [])
    return links


def _change_rows(changes: list[JsonDict]) -> str:
    rows = []
    for change in changes:
        title = _link(
            change["title"].split(": ", 1)[-1], change.get("link") or "", "#111827"
        )
        rows.append(
            '<tr><td style="padding:6px 0;border-top:1px solid #f3f4f6;vertical-align:top;'
            f'width:72px;font-size:12px;{_MUTED}{_FONT}">{_e(_when(change.get("at")))}</td>'
            f'<td style="padding:6px 0 6px 8px;border-top:1px solid #f3f4f6;font-size:14px;{_FONT}">'
            f'<div style="font-weight:600;color:#111827">{title}</div>'
            f'<div style="{_MUTED}font-size:13px">{_e(change.get("message"))}</div>'
            f"{_link_row(_change_links(change))}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )


_PILL_COLOURS = {
    BAND_RED: "background:#fee2e2;color:#991b1b;",
    BAND_AMBER: "background:#fef3c7;color:#b45309;",
}


def _pill(band: str | None, *, margin: bool = True) -> str:
    """Render a small coloured band label; green and unknown show nothing."""
    colours = _PILL_COLOURS.get(band or "")
    if not colours:
        return ""
    return (
        f'<span style="display:inline-block;{colours}border-radius:999px;'
        "padding:2px 8px;font-size:11px;font-weight:600;text-transform:uppercase;"
        f"letter-spacing:.04em;{'margin-left:6px;' if margin else ''}"
        f'vertical-align:middle;{_FONT}">{_e(band)}</span>'
    )


def _badge(text: str, *, new: bool) -> str:
    colours = (
        "background:#fee2e2;color:#991b1b;"
        if new
        else "background:#f3f4f6;color:#6b7280;"
    )
    return (
        f'<span style="display:inline-block;{colours}border-radius:999px;'
        f'padding:2px 8px;font-size:12px;margin-left:6px;{_FONT}">{_e(text)}</span>'
    )


def _card(avatar: str, heading: str, meta: str, body: str) -> str:
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin-bottom:14px;border:1px solid #e5e7eb;border-radius:10px">'
        '<tr><td style="padding:12px 14px 4px 14px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:top;padding-right:10px">{avatar}</td>'
        f'<td style="vertical-align:top;{_FONT}"><div style="font-size:15px;font-weight:600;color:#111827">'
        f"{heading}</div>"
        f'<div style="font-size:12px;{_MUTED}">{meta}</div></td></tr></table>'
        f'</td></tr><tr><td style="padding:4px 14px 10px 14px">{body}</td></tr></table>'
    )


def _charges_line(charges: JsonDict | None) -> str:
    """Say "2 outstanding charges · Lloyds Bank plc, HSBC UK", or nothing."""
    if not charges or not charges.get("outstanding"):
        return ""
    line = _plural(charges["outstanding"], "outstanding charge")
    lenders = list(charges.get("lenders") or [])
    if lenders:
        named = ", ".join(lenders[:3])
        if len(lenders) > 3:
            named += f" and {len(lenders) - 3} more"
        line += f" · {named}"
    return line


def _company_block(card: JsonDict) -> str:
    deadline = card.get("next_deadline")
    meta = [_link(card["number"], card["link"], "#6b7280")]
    if card.get("website"):
        site = (
            card["website"]
            if card["website"].startswith("http")
            else "https://" + card["website"]
        )
        meta.append(_link("website", site))
    if card.get("label"):
        meta.append(_e(card["label"]))
    if deadline and deadline["days"] is not None and deadline["days"] >= 0:
        due_text, due_style = _due(deadline["days"])
        meta.append(
            f'<span style="{due_style}">{_e(deadline["what"].replace("_", " "))} {_e(due_text)}</span>'
        )
    if charges_line := _charges_line(card.get("charges")):
        meta.append(_link(charges_line, (card.get("charges") or {}).get("link", "")))
    badges = "".join(
        _badge(a.get("short") or a["issue"], new=a["new"])
        for a in card.get("attention") or []
        if a.get("kind") != "risk"  # the pill says it
    )
    risk = card.get("risk") or {}
    heading = (
        _link(card["name"], card["link"], "#111827") + _pill(risk.get("band")) + badges
    )
    return _card(
        _avatar(card.get("logo", ""), card["initials"]),
        heading,
        " · ".join(meta) + _link_row(card.get("pages") or []),
        _change_rows(card["changes"]),
    )


def _person_block(card: JsonDict) -> str:
    companies = ", ".join(
        _link(c["name"], c.get("link") or "", "#6b7280") for c in card["companies"][:6]
    )
    if len(card["companies"]) > 6:
        companies += f" and {len(card['companies']) - 6} more"
    return _card(
        _avatar("", card["initials"], "#0f766e"),
        _link(card["name"], card["link"], "#111827"),
        (companies or "No current companies")
        + _link_row([{"text": "All their appointments", "href": card["link"]}]),
        _change_rows(card["changes"]),
    )


def _top_block(top: list[JsonDict]) -> str:
    rows = []
    for change in top:
        what = change["title"].split(": ", 1)[-1]
        rows.append(
            '<tr><td style="padding:8px 0;border-top:1px solid #f3f4f6;vertical-align:top;width:32px">'
            f"{_avatar(change.get('logo', ''), change.get('initials', '?'), '#1f4e79' if change.get('number') else '#0f766e', 32)}</td>"
            f'<td style="padding:8px 0 8px 10px;border-top:1px solid #f3f4f6;{_FONT}">'
            f'<div style="font-size:14px;font-weight:600;color:#111827">{_link(change["subject"], change["subject_link"], "#111827")}'
            f'<span style="font-weight:400;{_MUTED}"> · {_e(what)}</span></div>'
            f'<div style="font-size:13px;{_MUTED}">{_e(change["message"])}'
            f' <span style="font-size:11px">· {_e(_when(change.get("at")))}</span></div>'
            f"{_link_row(_change_links(change))}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )


def _issues_list(items: list[JsonDict], colour: str) -> str:
    rows = []
    for a in items:
        pages: list[JsonDict] = []
        for page in [
            *(a.get("links") or []),
            {"text": "Filing history", "href": a["link"] + "/filing-history"},
            {
                "text": "Gazette notices",
                "href": a["link"] + "/filing-history?category=gazette",
            },
        ]:
            if page["href"] not in {p["href"] for p in pages}:
                pages.append(page)
        rows.append(
            f'<li style="margin:4px 0">{_link(a["company"], a["link"], colour)} — '
            f"{_e(', '.join(a['issues']))}{_link_row(pages)}</li>"
        )
    return (
        f'<ul style="margin:0;padding-left:18px;font-size:14px;{_FONT}">'
        + "".join(rows)
        + "</ul>"
    )


def _risk_rows(items: list[JsonDict]) -> str:
    rows = []
    for r in items:
        reason = r["reason"].split(": ", 1)[-1]
        rows.append(
            '<tr><td style="padding:6px 0;border-top:1px solid #f3f4f6;vertical-align:top;'
            f'white-space:nowrap">{_pill(r["band"], margin=False)}</td>'
            f'<td style="padding:6px 0 6px 8px;border-top:1px solid #f3f4f6;font-size:14px;{_FONT}">'
            f'<div style="font-weight:600;color:#111827">{_link(r["company"], r["link"], "#111827")}</div>'
            f'<div style="{_MUTED}font-size:13px">{_e(reason)}</div>'
            f"{_link_row(r.get('pages') or [])}</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )


def _deadline_rows(deadlines: list[JsonDict]) -> str:
    rows = []
    for d in deadlines:
        due_text, due_style = _due(d["days"])
        rows.append(
            f'<tr><td style="padding:5px 0;font-size:14px;{_FONT}">{_link(d["company"], d["link"], "#111827")}</td>'
            f'<td style="padding:5px 8px;font-size:13px;{_MUTED}{_FONT}">{_e(d["what"].replace("_", " "))}</td>'
            f'<td style="padding:5px 0;font-size:13px;{_FONT}white-space:nowrap">{_e(_pretty_date(d["date"]))}</td>'
            f'<td style="padding:5px 0 5px 8px;font-size:13px;{_FONT}{due_style}white-space:nowrap">{_e(due_text)}</td>'
            f'<td style="padding:5px 0 5px 8px;font-size:12px;{_FONT}white-space:nowrap">'
            + (
                _link("Gazette notice", d["document"])
                if d.get("document")
                else _link("Filing history", d["link"] + "/filing-history")
            )
            + "</td></tr>"
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )


def _new_company_rows(items: list[JsonDict]) -> str:
    rows = []
    for n in items:
        who = (
            ", ".join(f"{p['name']} ({p['role']})" for p in n["people"])
            or "no one you follow yet"
        )
        pages = [
            {"text": "Officers", "href": n["link"] + "/officers"},
            {"text": "Filing history", "href": n["link"] + "/filing-history"},
        ]
        rows.append(
            f'<li style="margin:4px 0"><strong>{_link(n["company"], n["link"], "#111827")}</strong> '
            f"— set up {_e(_pretty_date(n['incorporated']))} · {_e(who)}{_link_row(pages)}</li>"
        )
    return (
        f'<ul style="margin:0;padding-left:18px;font-size:14px;{_FONT}">'
        + "".join(rows)
        + "</ul>"
    )


def _ownership_rows(holders: list[JsonDict], limit: int = 12) -> str:
    rows = []
    for h in holders[:limit]:
        holdings = "; ".join(
            f"{_link(x['company'], x['link'], '#111827')} "
            f"({_link(x['control'], x['link'] + '/persons-with-significant-control', '#6b7280')})"
            for x in h["holdings"]
        )
        star = " ★" if h["followed"] else ""
        rows.append(
            f'<li style="margin:4px 0"><strong>{_e(h["name"])}</strong>{star} — {holdings}</li>'
        )
    more = (
        f'<p style="font-size:12px;{_MUTED}{_FONT}">and {len(holders) - limit} more</p>'
        if len(holders) > limit
        else ""
    )
    return (
        f'<ul style="margin:0;padding-left:18px;font-size:14px;{_FONT}">'
        + "".join(rows)
        + "</ul>"
        + more
    )


_SEVERITY_STYLE = {
    "high": "background:#b91c1c;",
    "medium": "background:#d97706;",
    "low": "background:#9ca3af;",
}


def _connection_rows(lines: list[JsonDict]) -> str:
    rows = []
    for line in lines:
        dot = (
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;'
            f"{_SEVERITY_STYLE.get(line['severity'], _SEVERITY_STYLE['low'])}"
            'margin-right:8px;vertical-align:middle"></span>'
        )
        rows.append(
            f'<li style="margin:4px 0;list-style:none">{dot}'
            f"{_link(line['text'], line.get('link') or '', '#111827')}"
            f"{_link_row(line.get('links') or [])}</li>"
        )
    return (
        f'<ul style="margin:0;padding:0;font-size:14px;{_FONT}">'
        + "".join(rows)
        + "</ul>"
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _stats_line(digest: JsonDict) -> str:
    s = digest["summary"]
    kinds = ", ".join(
        _plural(n, _KIND_LABEL.get(k, k))
        for k, n in sorted(s.get("by_kind", {}).items(), key=lambda kv: -kv[1])
    )
    risk = s.get("risk") or {}
    rated = ", ".join(
        f"{risk[band]} {band}" for band in ("red", "amber", "green") if risk.get(band)
    )
    return (
        _plural(s["changes"], "change")
        + (f" ({kinds})" if kinds else "")
        + f" · {s['companies']} companies and {s['people']} people watched"
        + (f" · risk: {rated}" if rated else "")
    )


def render_html(digest: JsonDict, *, title: str, summary: str | None = None) -> str:
    """Render the report as email-safe HTML with inline styles."""
    period = f"{_when(digest['since'])} to {_when(digest['generated_at'])}"
    parts: list[str] = []
    if summary:
        paragraphs = "".join(
            f'<p style="margin:0 0 10px 0;font-size:15px;line-height:1.5;{_FONT}color:#111827">{_e(line)}</p>'
            for line in summary.strip().splitlines()
            if line.strip()
        )
        parts.append(
            '<tr><td style="padding:24px 24px 0 24px"><div style="background:#eff6ff;border-radius:10px;padding:16px">'
            f"{paragraphs}</div></td></tr>"
        )
    if digest["needs_attention"]:
        parts.append(
            _section(
                "New this week: needs attention",
                _issues_list(digest["needs_attention"], "#991b1b"),
                icon="⚠️",
            )
        )
    if digest.get("top"):
        parts.append(
            _section(
                "Worth a look",
                _top_block(digest["top"]),
                icon="⭐",
                intro="The week's changes, most important first.",
            )
        )
    if digest.get("risk"):
        parts.append(
            _section(
                "Risk",
                _risk_rows(digest["risk"]),
                icon="🚦",
                intro="Companies rated amber or red from the register alone: "
                "filing compliance, status, board, ownership and charges. Not a "
                "credit check.",
            )
        )
    if digest.get("new_companies"):
        parts.append(
            _section(
                "New companies",
                _new_company_rows(digest["new_companies"]),
                icon="🆕",
                intro="Recently set up by people you follow.",
            )
        )
    if digest["deadlines"]:
        parts.append(
            _section(
                f"Due in the next {DEADLINE_HORIZON_DAYS} days",
                _deadline_rows(digest["deadlines"]),
                icon="📅",
            )
        )
    if digest["companies"]:
        parts.append(
            _section(
                "Companies",
                "".join(_company_block(c) for c in digest["companies"]),
                icon="🏢",
            )
        )
    if digest["people"]:
        parts.append(
            _section(
                "People", "".join(_person_block(p) for p in digest["people"]), icon="👥"
            )
        )
    if digest["summary"].get("by_kind", {}).get("psc") and digest.get("ownership"):
        parts.append(
            _section(
                "Who controls what",
                _ownership_rows(digest["ownership"]),
                icon="🧭",
                intro="Ownership changed this week. ★ marks people you follow.",
            )
        )
    if digest.get("connections"):
        parts.append(
            _section(
                "Connections",
                _connection_rows(digest["connections"]),
                icon="🕸️",
                intro="Who sits with whom and who owns what, across everything you watch.",
            )
        )
    if not digest["companies"] and not digest["people"]:
        parts.append(
            _section(
                "A quiet week",
                f'<p style="margin:0;font-size:14px;{_FONT}{_MUTED}">Nothing changed at any watched '
                "company or person.</p>",
                icon="😌",
            )
        )
    if digest.get("still_open"):
        parts.append(
            _section(
                "Still open",
                _issues_list(digest["still_open"], "#6b7280"),
                intro="Known problems from before this week. They stay here until they clear.",
            )
        )
    body = "".join(parts)
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{_e(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f3f4f6">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="640" cellpadding="0" cellspacing="0" '
        'style="max-width:640px;width:100%;background:#ffffff;border-radius:14px">'
        '<tr><td style="padding:28px 24px 0 24px">'
        f'<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;{_MUTED}{_FONT}">Companies House</div>'
        f'<h1 style="margin:4px 0 6px 0;font-size:24px;{_FONT}color:#111827">{_e(title)}</h1>'
        f'<div style="font-size:13px;{_MUTED}{_FONT}">{_e(period)} · {_e(_stats_line(digest))}</div></td></tr>'
        f"{body}"
        f'<tr><td style="padding:24px;font-size:12px;{_MUTED}{_FONT}">Links open the Companies House '
        "register; filed documents open as PDFs. Built by your Home Assistant Companies House "
        "integration.</td></tr></table></td></tr></table></body></html>"
    )


def render_text(digest: JsonDict, *, title: str, summary: str | None = None) -> str:
    """Render a plain-text version for the email text part and for phones."""
    lines = [title, ""]
    if summary:
        lines += [summary.strip(), ""]
    if digest["needs_attention"]:
        lines.append("New this week, needs attention:")
        lines += [
            f"  - {a['company']}: {', '.join(a['issues'])}"
            for a in digest["needs_attention"]
        ]
        lines.append("")
    if digest.get("top"):
        lines.append("Worth a look:")
        for c in digest["top"]:
            lines.append(
                f"  - {c['subject']}: {c['title'].split(': ', 1)[-1]} — {c['message']}"
            )
        lines.append("")
    if digest.get("risk"):
        lines.append("Risk (from the register alone, not a credit check):")
        lines += [
            f"  - {r['band'].upper()} {r['company']} — {r['reason'].split(': ', 1)[-1]}"
            for r in digest["risk"]
        ]
        lines.append("")
    if digest.get("new_companies"):
        lines.append("New companies:")
        for n in digest["new_companies"]:
            who = ", ".join(p["name"] for p in n["people"])
            lines.append(
                f"  - {n['company']} (set up {_pretty_date(n['incorporated'])}){': ' + who if who else ''}"
            )
        lines.append("")
    if digest["deadlines"]:
        lines.append(f"Due in the next {DEADLINE_HORIZON_DAYS} days:")
        for d in digest["deadlines"]:
            lines.append(
                f"  - {_pretty_date(d['date'])} {d['company']}: {d['what'].replace('_', ' ')} ({_due(d['days'])[0]})"
            )
        lines.append("")
    for card in digest["companies"]:
        lines.append(card["name"])
        if charges_line := _charges_line(card.get("charges")):
            lines.append(f"  {charges_line}")
        lines += [
            f"  - {_when(c['at'])}: {c['title'].split(': ', 1)[-1]} — {c['message']}"
            for c in card["changes"]
        ]
        lines.append("")
    for card in digest["people"]:
        lines.append(card["name"])
        lines += [
            f"  - {_when(c['at'])}: {c['title'].split(': ', 1)[-1]} — {c['message']}"
            for c in card["changes"]
        ]
        lines.append("")
    if digest.get("connections"):
        lines.append("Connections:")
        lines += [f"  - {c['text']}" for c in digest["connections"]]
        lines.append("")
    if not digest["companies"] and not digest["people"]:
        lines += ["Nothing changed at any watched company or person.", ""]
    if digest.get("still_open"):
        lines.append("Still open (known before this week):")
        lines += [
            f"  - {a['company']}: {', '.join(a['issues'])}"
            for a in digest["still_open"]
        ]
    return "\n".join(lines).rstrip() + "\n"


def is_finished(status: str | None) -> bool:
    """Whether a company status means it is no longer trading."""
    return status in FINISHED_STATUSES
