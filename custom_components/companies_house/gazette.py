"""The strike-off countdown: from a Gazette notice to the last day to object.

Everything here is a pure function of dates so the rules can be tested
without Home Assistant. The rules are GOV.UK's ("Striking off or dissolving a
limited company" and "Object to a limited company being struck off"):

- The registrar strikes a company off not less than two months after the
  first Gazette notice, then publishes a second notice dissolving it. In
  false-registration cases it is 28 days, but the register does not say
  which cases those are, so two months is assumed and the caveat says so.
- Anyone with an interest may object once the first notice is out. A postal
  objection must arrive at least two weeks before the strike-off date; an
  online one is taken any time before the company is struck off.
- A successful objection suspends the strike-off for six months.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .const import FIND_AND_UPDATE_BASE

STRIKE_OFF_MONTHS = 2
SUSPENSION_MONTHS = 6
POSTAL_OBJECTION_DAYS = 14

CAVEAT_TWO_MONTHS = (
    "Assumes the usual two months' notice; the register does not say when the "
    "28-day rule for false registrations applies. Postal objections must arrive "
    "two weeks before the earliest date; online objections are taken until the "
    "company is struck off."
)
CAVEAT_SUSPENDED = (
    "The strike-off was suspended, which holds it off for six months from the "
    "suspension; the earliest date is that, extendable if progress is shown."
)
CAVEAT_UNKNOWN = (
    "The register says a strike-off is proposed but the Gazette notice is not "
    "in the filings kept, so the date is unknown and no countdown can be given."
)


def add_months(day: date, months: int) -> date:
    """Return the same day ``months`` on, clamped to the end of that month."""
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def strike_off_kind(description: str | None) -> str | None:
    """Return "voluntary" or "compulsory" from a first Gazette notice key, else None."""
    if not description or not description.startswith("gazette-notice-"):
        return None
    return "voluntary" if description.endswith("voluntary") else "compulsory"


def notice_link(company_number: str, transaction_id: str | None) -> str:
    """Return the Gazette notice PDF on the register, or its Gazette filings list."""
    base = f"{FIND_AND_UPDATE_BASE}/company/{company_number}/filing-history"
    if transaction_id:
        return f"{base}/{transaction_id}/document?format=pdf&download=0"
    return f"{base}?category=gazette"


def _pretty(day: date) -> str:
    return day.strftime("%-d %b %Y")


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


@dataclass(frozen=True, kw_only=True)
class StrikeOffCountdown:
    """Where a live strike-off stands today."""

    kind: str | None
    notice_on: date | None
    suspended_on: date | None
    transaction_id: str | None
    earliest_on: date | None
    objection_deadline: date | None
    days_to_object: int | None
    days_to_strike_off: int | None
    caveat: str

    @property
    def suspended(self) -> bool:
        """Whether a suspension filing has pushed the strike-off back."""
        return self.suspended_on is not None

    @property
    def kind_words(self) -> str:
        """Return " (compulsory)" or " (voluntary)", or nothing when unknown."""
        return f" ({self.kind})" if self.kind else ""

    def objection_phrase(self) -> str:
        """Say how long is left to object, in plain English."""
        days, until = self.days_to_object, self.days_to_strike_off
        if days is None or until is None or self.earliest_on is None:
            return "notice date unknown"
        if until <= 0:
            # The earliest date is here or gone: "before today" is no advice.
            return "could be struck off any day now"
        if days < 0:
            return f"object online before {_pretty(self.earliest_on)}"
        if days == 0:
            return "last day to object by post"
        return f"{days} day{'' if days == 1 else 's'} to object"

    def attention_line(self) -> str:
        """Return the one-line summary for the report: what and how long is left."""
        if self.suspended and self.earliest_on is not None:
            if self.days_to_strike_off is not None and self.days_to_strike_off < 0:
                # The six-month hold has run out; the register may still say
                # "proposal to strike off" long after (the TRU:VAI case).
                return (
                    f"Strike-off suspended{self.kind_words} — hold ended "
                    f"{_pretty(self.earliest_on)}, could be struck off any day now"
                )
            return (
                f"Strike-off suspended{self.kind_words} — "
                f"earliest strike-off {_pretty(self.earliest_on)}"
            )
        return f"Strike-off proposed{self.kind_words} — {self.objection_phrase()}"

    def as_dict(self) -> dict[str, Any]:
        """Return the countdown with dates as ISO strings, for attributes and tools."""
        return {
            "kind": self.kind,
            "notice_on": _iso(self.notice_on),
            "earliest_on": _iso(self.earliest_on),
            "objection_deadline": _iso(self.objection_deadline),
            "days_to_object": self.days_to_object,
            "days_to_strike_off": self.days_to_strike_off,
            "suspended_on": _iso(self.suspended_on),
            "transaction_id": self.transaction_id,
            "caveat": self.caveat,
        }


def compute_countdown(
    *,
    kind: str | None,
    notice_on: date | None,
    suspended_on: date | None,
    transaction_id: str | None,
    today: date,
) -> StrikeOffCountdown:
    """Work out the earliest strike-off date and the last day to object.

    The earliest date is the notice date plus two months (same day, clamped
    to the month end). A suspension filed after the notice moves it to six
    months after the suspension. Postal objections must arrive two weeks
    before that. Without a notice date nothing can be counted down.
    """
    if notice_on is None:
        return StrikeOffCountdown(
            kind=kind,
            notice_on=None,
            suspended_on=suspended_on,
            transaction_id=transaction_id,
            earliest_on=None,
            objection_deadline=None,
            days_to_object=None,
            days_to_strike_off=None,
            caveat=CAVEAT_UNKNOWN,
        )
    if suspended_on is not None and suspended_on >= notice_on:
        earliest = add_months(suspended_on, SUSPENSION_MONTHS)
        caveat = CAVEAT_SUSPENDED
    else:
        suspended_on = None
        earliest = add_months(notice_on, STRIKE_OFF_MONTHS)
        caveat = CAVEAT_TWO_MONTHS
    objection = earliest - timedelta(days=POSTAL_OBJECTION_DAYS)
    return StrikeOffCountdown(
        kind=kind,
        notice_on=notice_on,
        suspended_on=suspended_on,
        transaction_id=transaction_id,
        earliest_on=earliest,
        objection_deadline=objection,
        days_to_object=(objection - today).days,
        days_to_strike_off=(earliest - today).days,
        caveat=caveat,
    )


ATTRIBUTE_KEYS = (
    "kind",
    "notice_on",
    "earliest_on",
    "objection_deadline",
    "suspended_on",
    "transaction_id",
)


def countdown_attributes(
    countdown: StrikeOffCountdown | None, company_number: str
) -> dict[str, Any]:
    """Return the entity attributes for a countdown, all None when there is none."""
    if countdown is None:
        return {**dict.fromkeys(ATTRIBUTE_KEYS), "link": None, "caveat": None}
    values = countdown.as_dict()
    return {
        **{key: values[key] for key in ATTRIBUTE_KEYS},
        "link": notice_link(company_number, countdown.transaction_id),
        "caveat": countdown.caveat,
    }
