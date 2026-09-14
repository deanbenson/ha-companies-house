"""Charges in plain English: who lent, secured on what, created versus satisfied.

Pure helpers over the ``Charge`` model, shared by the change events, the
sensors, the report and anything that answers questions about a company.
Nothing here touches the API; everything reads from the stored snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import TYPE_CHECKING

from .const import FIND_AND_UPDATE_BASE
from .models import Charge, JsonDict

if TYPE_CHECKING:
    from .coordinator import CompanyRuntime

# How many charges a sensor lists in its attributes. Some companies carry
# nearly thirty; the list stays out of the recorder, but the state itself
# still travels to every dashboard, so it is capped and each entry is short.
CHARGE_LIST_CAP = 25
# The longest a free-text field (particulars, what is secured) gets in an
# attribute or an alert; the register has the full wording.
TEXT_LIMIT = 80


def charges_page(number: str) -> str:
    """Return the register's charges page for a company."""
    return f"{FIND_AND_UPDATE_BASE}/company/{number}/charges"


def charge_filing_link(number: str, transaction_id: str | None) -> str:
    """Return the PDF of a charge filing, or the charges page when unknown."""
    if not transaction_id:
        return charges_page(number)
    return (
        f"{FIND_AND_UPDATE_BASE}/company/{number}/filing-history/{transaction_id}"
        "/document?format=pdf&download=0"
    )


def shorten(text: str | None, limit: int = TEXT_LIMIT) -> str:
    """Collapse whitespace and cut long legal wording at a word boundary."""
    words = (text or "").split()
    if not words:
        return ""
    out = words[0]
    for word in words[1:]:
        if len(out) + 1 + len(word) > limit - 1:
            return out.rstrip(",;:") + "…"
        out = f"{out} {word}"
    return out


def lender(charge: Charge) -> str:
    """Return who the charge is in favour of, or empty when the register says nothing."""
    names = ", ".join(charge.persons_entitled)
    if names and charge.more_than_four_persons_entitled:
        names += " and others"
    return names


def charge_words(charge: Charge) -> str:
    """Say what kind of charge it is: "fixed and floating charge over all the company's assets".

    Built from the particulars flags; empty when the register did not
    record any (older charges and some paper filings).
    """
    kinds = [
        word
        for word, present in (
            ("fixed", charge.contains_fixed_charge),
            ("floating", charge.contains_floating_charge),
        )
        if present
    ]
    if not kinds:
        return ""
    words = f"{' and '.join(kinds)} charge"
    if charge.floating_charge_covers_all and "floating" in kinds:
        words += " over all the company's assets"
    return words


def particulars(charge: Charge) -> str:
    """Return a short version of what the charge is over."""
    return shorten(charge.particulars_description)


def secured(charge: Charge) -> str:
    """Return a short version of what the charge secures."""
    return shorten(charge.secured_description)


def _lower_first(text: str) -> str:
    """Lower the first letter of legal wording unless it starts with a name or code."""
    if len(text) > 1 and text[1].isupper():
        return text
    return text[:1].lower() + text[1:]


def security_words(charge: Charge) -> str:
    """Describe the security in one clause: kind, what it is over, what it secures.

    Examples: "fixed and floating charge over all the company's assets; secures
    all monies due", "charge over the property at 1 High Street". Empty when
    the register recorded none of it.
    """
    kind = charge_words(charge)
    over = particulars(charge)
    parts: list[str] = []
    if kind:
        parts.append(
            kind
            if charge.floating_charge_covers_all or not over
            else f"{kind} over {_lower_first(over)}"
        )
    elif over:
        parts.append(f"charge over {_lower_first(over)}")
    if what := secured(charge):
        parts.append(f"secures {_lower_first(what)}")
    return "; ".join(parts)


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def charge_payload(charge: Charge) -> JsonDict:
    """Return the change event payload for a charge.

    Everything an automation or a report might want, JSON-safe: the parties,
    the dates, the security in words and the filing ids for the PDFs.
    """
    return {
        "charge_code": charge.charge_code,
        "charge_id": charge.charge_id,
        "persons_entitled": list(charge.persons_entitled),
        "lender": lender(charge),
        "created_on": _iso(charge.created_on),
        "delivered_on": _iso(charge.delivered_on),
        "satisfied_on": _iso(charge.satisfied_on),
        "acquired_on": _iso(charge.acquired_on),
        "status": charge.status,
        # Not "kind": the bus event spreads the payload next to the change kind.
        "charge_kind": charge_words(charge),
        "particulars": particulars(charge),
        "secured": secured(charge),
        "security": security_words(charge),
        "negative_pledge": charge.contains_negative_pledge,
        "created_transaction_id": charge.creation_transaction_id,
        "satisfied_transaction_id": charge.satisfaction_transaction_id,
        "transaction_ids": [
            t.transaction_id for t in charge.transactions if t.transaction_id
        ],
    }


def charge_record(charge: Charge, number: str) -> JsonDict:
    """Return one charge as a short attribute row, with a link to its filing."""
    return {
        "charge_code": charge.charge_code,
        "lender": shorten(lender(charge)),
        "status": charge.status,
        "created_on": _iso(charge.created_on),
        "satisfied_on": _iso(charge.satisfied_on),
        "secured": secured(charge),
        "particulars": particulars(charge),
        "kind": charge_words(charge),
        "link": charge_filing_link(
            number,
            (
                charge.satisfaction_transaction_id
                if charge.is_satisfied
                else charge.creation_transaction_id
            )
            or charge.creation_transaction_id,
        ),
    }


def newest_first(charges: Iterable[Charge]) -> list[Charge]:
    """Order charges by creation date, newest first; undated ones last."""
    return sorted(charges, key=lambda c: c.created_on or date.min, reverse=True)


def lenders(charges: Iterable[Charge]) -> list[str]:
    """Return the distinct lenders behind the outstanding charges, newest first."""
    out: list[str] = []
    for charge in newest_first(c for c in charges if c.is_outstanding):
        who = lender(charge)
        if who and who not in out:
            out.append(who)
    return out


def charges_overview(company: CompanyRuntime) -> JsonDict | None:
    """Summarise a company's charges from its snapshot, or None without the dataset.

    Counts, the lenders still owed, and a capped list of every charge newest
    first (satisfied ones included) with a link to each filing's PDF.
    """
    if company.charges is None or company.charges.data is None:
        return None
    data = company.charges.data
    ordered = newest_first(data.items)
    return {
        "outstanding": data.outstanding_count,
        "total": data.total_count,
        "satisfied": data.satisfied_count,
        "part_satisfied": data.part_satisfied_count,
        "lenders": lenders(data.items),
        "link": charges_page(company.company_number),
        "charges": [
            charge_record(c, company.company_number) for c in ordered[:CHARGE_LIST_CAP]
        ],
    }
