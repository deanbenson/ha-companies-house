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

from .const import FIND_AND_UPDATE_BASE, FINISHED_STATUSES
from .enumerations import COMPANY_STATUS
from .models import JsonDict
from .scheduler import days_until

if TYPE_CHECKING:
    from .coordinator import CompaniesHouseConfigEntry, CompanyRuntime, OfficerRuntime

LOGO_URL = "https://logo.clearbit.com/{domain}?size=96"
DEADLINE_HORIZON_DAYS = 30

# ---------------------------------------------------------------- describing


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


def _status_word(status: str | None) -> str:
    if not status:
        return "unknown"
    return str(COMPANY_STATUS.get(status, status)).lower()


def _role(role: str | None) -> str:
    return (role or "officer").replace("-", " ")


def describe_change(
    kind: str, event_type: str, payload: JsonDict, *, subject: str, number: str = ""
) -> tuple[str, str, str]:
    """Return (title, message, link) for a change, in plain English.

    ``subject`` is the company or person the change belongs to. The link is
    the most useful page on the Companies House website for it.
    """
    p = payload
    link = _company_link(number) if number else ""
    name = str(p.get("name") or "")
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
            return (
                f"{subject}: strike-off proposed",
                str(
                    p.get("detail")
                    or "Companies House has proposed to strike the company off."
                ),
                link,
            )
        if event_type == "strike-off-discontinued":
            return (
                f"{subject}: strike-off dropped",
                "The proposal to strike the company off has been discontinued.",
                link,
            )
        if event_type == "dissolved":
            return f"{subject}: dissolved", "The company has been dissolved.", link
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
                f"{name} was appointed {role} on {p.get('appointed_on') or 'an unknown date'}.",
                link + "/officers",
            )
        if event_type == "resigned":
            return (
                f"{subject}: {role} resigned",
                f"{name} resigned as {role} on {p.get('resigned_on') or 'an unknown date'}.",
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
        who = ", ".join(str(x) for x in p.get("persons_entitled") or []) or "a lender"
        code = p.get("charge_code") or ""
        if event_type in ("created", "acquired"):
            return (
                f"{subject}: new charge registered",
                f"A charge in favour of {who} was registered"
                + (f" on {p.get('created_on')}" if p.get("created_on") else "")
                + ".",
                link + "/charges",
            )
        if event_type == "satisfied":
            return (
                f"{subject}: charge satisfied",
                f"The charge in favour of {who} ({code}) has been satisfied in full.",
                link + "/charges",
            )
        return (
            f"{subject}: charge part satisfied",
            f"The charge in favour of {who} ({code}) has been partly satisfied.",
            link + "/charges",
        )

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
                f"Appointed {role} at {where} on {p.get('appointed_on') or 'an unknown date'}.",
                target,
            )
        if event_type == "resigned":
            return (
                f"{subject}: stepped down",
                f"Resigned as {role} at {where} on {p.get('resigned_on') or 'an unknown date'}.",
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
                f"Disqualified until {p.get('disqualified_until') or 'an unknown date'}"
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


def _company_card(company: CompanyRuntime, since: datetime) -> JsonDict:
    profile = company.profile.data
    website = (company.website or "").strip()
    domain = website.removeprefix("https://").removeprefix("http://").split("/")[0]
    deadline = profile.next_deadline if profile else None
    days = days_until(deadline[0], dt_util.now().date()) if deadline else None
    changes = []
    for change in _since(company.state.changes, since):
        title, message, link = describe_change(
            str(change.get("kind", "")),
            str(change.get("event_type", "")),
            dict(change.get("payload") or {}),
            subject=company.company_name,
            number=company.company_number,
        )
        changes.append(
            {
                "at": change.get("at"),
                "kind": change.get("kind"),
                "event_type": change.get("event_type"),
                "title": title,
                "message": message,
                "link": link,
            }
        )
    attention: list[str] = []
    if profile is not None:
        if profile.company_status_detail == "active-proposal-to-strike-off":
            attention.append("Strike-off proposed")
        if profile.company_status in ("liquidation", "administration", "receivership"):
            attention.append(f"In {_status_word(profile.company_status)}")
        if profile.accounts.next_overdue:
            attention.append("Accounts overdue")
        if profile.confirmation_statement.overdue:
            attention.append("Confirmation statement overdue")
    return {
        "name": company.company_name,
        "number": company.company_number,
        "status": _status_word(profile.company_status) if profile else "unknown",
        "label": company.label,
        "website": website,
        "logo": LOGO_URL.format(domain=domain) if domain else "",
        "initials": _initials(company.company_name),
        "link": _company_link(company.company_number),
        "close_watch": company.close_watch,
        "next_deadline": {
            "what": deadline[1],
            "date": deadline[0].isoformat(),
            "days": days,
        }
        if deadline
        else None,
        "attention": attention,
        "changes": changes,
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
    changes = []
    for change in _since(officer.state.changes, since):
        title, message, link = describe_change(
            str(change.get("kind", "")),
            str(change.get("event_type", "")),
            dict(change.get("payload") or {}),
            subject=officer.officer_name,
        )
        changes.append(
            {
                "at": change.get("at"),
                "kind": change.get("kind"),
                "event_type": change.get("event_type"),
                "title": title,
                "message": message,
                "link": link,
            }
        )
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
                "role": _role(a.officer_role),
            }
            for a in current
        ],
        "changes": changes,
    }


def build_digest(
    entry: CompaniesHouseConfigEntry, *, days: int, now: datetime | None = None
) -> JsonDict:
    """Gather everything a report needs, for the companies and people opted in."""
    now = now or dt_util.utcnow()
    since = now - timedelta(days=days)
    runtime = entry.runtime_data
    companies = [
        _company_card(c, since)
        for c in runtime.companies.values()
        if c.in_weekly_report and c.profile.data is not None
    ]
    people = [
        _person_card(o, since)
        for o in runtime.officers.values()
        if o.in_weekly_report and o.appointments.data is not None
    ]
    companies.sort(key=lambda c: (not c["changes"], not c["attention"], c["name"]))
    people.sort(key=lambda p: (not p["changes"], p["name"]))
    deadlines = sorted(
        (
            {
                **c["next_deadline"],
                "company": c["name"],
                "number": c["number"],
                "link": c["link"],
            }
            for c in companies
            if c["next_deadline"]
            and c["next_deadline"]["days"] is not None
            and c["next_deadline"]["days"] <= DEADLINE_HORIZON_DAYS
        ),
        key=lambda d: d["date"],
    )
    attention = [
        {
            "company": c["name"],
            "number": c["number"],
            "link": c["link"],
            "issues": c["attention"],
        }
        for c in companies
        if c["attention"]
    ]
    changed_companies = [c for c in companies if c["changes"]]
    changed_people = [p for p in people if p["changes"]]
    change_count = sum(len(c["changes"]) for c in companies) + sum(
        len(p["changes"]) for p in people
    )
    return {
        "generated_at": now.isoformat(),
        "since": since.isoformat(),
        "days": days,
        "summary": {
            "companies": len(companies),
            "people": len(people),
            "changes": change_count,
            "companies_with_changes": len(changed_companies),
            "people_with_changes": len(changed_people),
            "needs_attention": len(attention),
            "deadlines_soon": len(deadlines),
        },
        "needs_attention": attention,
        "deadlines": deadlines,
        "companies": changed_companies,
        "people": changed_people,
        "quiet_companies": [c["name"] for c in companies if not c["changes"]],
    }


# ---------------------------------------------------------------- rendering

_FONT = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
_MUTED = "color:#6b7280;"


def _e(value: Any) -> str:
    return escape(str(value if value is not None else ""))


def _avatar(logo: str, initials: str, colour: str = "#1f4e79") -> str:
    if logo:
        return (
            f'<img src="{_e(logo)}" width="40" height="40" alt="" '
            'style="width:40px;height:40px;border-radius:8px;display:block;'
            'background:#fff;object-fit:contain">'
        )
    return (
        f'<div style="width:40px;height:40px;border-radius:8px;background:{colour};'
        f'color:#fff;text-align:center;line-height:40px;font-weight:600;{_FONT}">'
        f"{_e(initials)}</div>"
    )


def _when(value: Any) -> str:
    parsed = dt_util.parse_datetime(str(value or ""))
    if parsed is None:
        return ""
    return dt_util.as_local(parsed).strftime("%a %-d %b")


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


def _section(title: str, body: str, *, icon: str = "") -> str:
    return (
        '<tr><td style="padding:24px 24px 0 24px">'
        f'<h2 style="margin:0 0 12px 0;font-size:17px;{_FONT}color:#111827">'
        f"{icon} {_e(title)}</h2>{body}</td></tr>"
    )


def _change_rows(changes: list[JsonDict]) -> str:
    rows = []
    for change in changes:
        link = change.get("link")
        title = _e(change["title"].split(": ", 1)[-1])
        if link:
            title = f'<a href="{_e(link)}" style="color:#1d4ed8;text-decoration:none">{title}</a>'
        rows.append(
            '<tr><td style="padding:6px 0;border-top:1px solid #f3f4f6;vertical-align:top;'
            f'width:72px;font-size:12px;{_MUTED}{_FONT}">{_e(_when(change.get("at")))}</td>'
            f'<td style="padding:6px 0 6px 8px;border-top:1px solid #f3f4f6;font-size:14px;{_FONT}">'
            f'<div style="font-weight:600;color:#111827">{title}</div>'
            f'<div style="{_MUTED}font-size:13px">{_e(change.get("message"))}</div></td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        + "".join(rows)
        + "</table>"
    )


def _company_block(card: JsonDict) -> str:
    deadline = card.get("next_deadline")
    due_text, due_style = _due(deadline["days"]) if deadline else ("", _MUTED)
    meta = [
        f'<a href="{_e(card["link"])}" style="color:#6b7280;text-decoration:none">{_e(card["number"])}</a>'
    ]
    if card.get("website"):
        site = (
            card["website"]
            if card["website"].startswith("http")
            else "https://" + card["website"]
        )
        meta.append(
            f'<a href="{_e(site)}" style="color:#1d4ed8;text-decoration:none">website</a>'
        )
    if card.get("label"):
        meta.append(_e(card["label"]))
    if deadline:
        meta.append(
            f'<span style="{due_style}">{_e(deadline["what"].replace("_", " "))} {_e(due_text)}</span>'
        )
    badges = "".join(
        f'<span style="display:inline-block;background:#fee2e2;color:#991b1b;border-radius:999px;'
        f'padding:2px 8px;font-size:12px;margin-left:6px;{_FONT}">{_e(a)}</span>'
        for a in card.get("attention") or []
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin-bottom:16px;border:1px solid #e5e7eb;border-radius:10px">'
        '<tr><td style="padding:12px 14px 4px 14px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:top;padding-right:10px">{_avatar(card.get("logo", ""), card["initials"])}</td>'
        f'<td style="vertical-align:top;{_FONT}"><div style="font-size:15px;font-weight:600;color:#111827">'
        f'<a href="{_e(card["link"])}" style="color:#111827;text-decoration:none">{_e(card["name"])}</a>{badges}</div>'
        f'<div style="font-size:12px;{_MUTED}">{" · ".join(meta)}</div></td></tr></table>'
        f'</td></tr><tr><td style="padding:4px 14px 10px 14px">{_change_rows(card["changes"])}</td></tr></table>'
    )


def _person_block(card: JsonDict) -> str:
    companies = ", ".join(_e(c["name"]) for c in card["companies"][:6])
    if len(card["companies"]) > 6:
        companies += f" and {len(card['companies']) - 6} more"
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin-bottom:16px;border:1px solid #e5e7eb;border-radius:10px">'
        '<tr><td style="padding:12px 14px 4px 14px">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="vertical-align:top;padding-right:10px">{_avatar("", card["initials"], "#0f766e")}</td>'
        f'<td style="vertical-align:top;{_FONT}"><div style="font-size:15px;font-weight:600;color:#111827">'
        f'<a href="{_e(card["link"])}" style="color:#111827;text-decoration:none">{_e(card["name"])}</a></div>'
        f'<div style="font-size:12px;{_MUTED}">{companies or "No current companies"}</div></td></tr></table>'
        f'</td></tr><tr><td style="padding:4px 14px 10px 14px">{_change_rows(card["changes"])}</td></tr></table>'
    )


def render_html(digest: JsonDict, *, title: str, summary: str | None = None) -> str:
    """Render the report as email-safe HTML with inline styles."""
    s = digest["summary"]
    period = f"{_when(digest['since'])} to {_when(digest['generated_at'])}"
    stats = (
        f"{s['changes']} changes across {s['companies_with_changes']} companies "
        f"and {s['people_with_changes']} people · {s['companies']} companies and "
        f"{s['people']} people watched"
    )
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
        items = "".join(
            f'<li style="margin:4px 0"><a href="{_e(a["link"])}" style="color:#991b1b;font-weight:600;'
            f'text-decoration:none">{_e(a["company"])}</a> — {_e(", ".join(a["issues"]))}</li>'
            for a in digest["needs_attention"]
        )
        parts.append(
            _section(
                "Needs attention",
                f'<ul style="margin:0;padding-left:18px;font-size:14px;{_FONT}">{items}</ul>',
                icon="⚠️",
            )
        )
    if digest["deadlines"]:
        rows = []
        for d in digest["deadlines"]:
            due_text, due_style = _due(d["days"])
            rows.append(
                f'<tr><td style="padding:5px 0;font-size:14px;{_FONT}"><a href="{_e(d["link"])}" '
                f'style="color:#111827;text-decoration:none">{_e(d["company"])}</a></td>'
                f'<td style="padding:5px 8px;font-size:13px;{_MUTED}{_FONT}">{_e(d["what"].replace("_", " "))}</td>'
                f'<td style="padding:5px 0;font-size:13px;{_FONT}white-space:nowrap">{_e(d["date"])}</td>'
                f'<td style="padding:5px 0 5px 8px;font-size:13px;{_FONT}{due_style}white-space:nowrap">{_e(due_text)}</td></tr>'
            )
        parts.append(
            _section(
                f"Deadlines in the next {DEADLINE_HORIZON_DAYS} days",
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
                + "".join(rows)
                + "</table>",
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
    if not digest["companies"] and not digest["people"]:
        parts.append(
            _section(
                "A quiet week",
                f'<p style="margin:0;font-size:14px;{_FONT}{_MUTED}">Nothing changed at any watched '
                "company or person.</p>",
                icon="😌",
            )
        )
    quiet = digest.get("quiet_companies") or []
    if quiet and digest["companies"]:
        parts.append(
            _section(
                "No change",
                f'<p style="margin:0;font-size:13px;{_FONT}{_MUTED}">{_e(", ".join(quiet))}</p>',
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
        f'<div style="font-size:13px;{_MUTED}{_FONT}">{_e(period)} · {_e(stats)}</div></td></tr>'
        f"{body}"
        f'<tr><td style="padding:24px;font-size:12px;{_MUTED}{_FONT}">Links open the Companies House '
        "register. Filed documents open as PDFs on the register. Built by your Home Assistant "
        "Companies House integration.</td></tr></table></td></tr></table></body></html>"
    )


def render_text(digest: JsonDict, *, title: str, summary: str | None = None) -> str:
    """Render a plain-text version for the email text part and for phones."""
    lines = [title, ""]
    if summary:
        lines += [summary.strip(), ""]
    if digest["needs_attention"]:
        lines.append("Needs attention:")
        lines += [
            f"  - {a['company']}: {', '.join(a['issues'])}"
            for a in digest["needs_attention"]
        ]
        lines.append("")
    if digest["deadlines"]:
        lines.append(f"Deadlines in the next {DEADLINE_HORIZON_DAYS} days:")
        for d in digest["deadlines"]:
            lines.append(
                f"  - {d['date']} {d['company']}: {d['what'].replace('_', ' ')} ({_due(d['days'])[0]})"
            )
        lines.append("")
    for card in digest["companies"]:
        lines.append(card["name"])
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
    if not digest["companies"] and not digest["people"]:
        lines.append("Nothing changed at any watched company or person.")
    return "\n".join(lines).rstrip() + "\n"


def is_finished(status: str | None) -> bool:
    """Whether a company status means it is no longer trading."""
    return status in FINISHED_STATUSES
