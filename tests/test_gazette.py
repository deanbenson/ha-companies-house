"""Tests for the strike-off countdown rules, on plain dates."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.companies_house.const import FIND_AND_UPDATE_BASE
from custom_components.companies_house.gazette import (
    CAVEAT_SUSPENDED,
    CAVEAT_TWO_MONTHS,
    CAVEAT_UNKNOWN,
    add_months,
    compute_countdown,
    countdown_attributes,
    notice_link,
    strike_off_kind,
)


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2026, 8, 25), 2, date(2026, 10, 25)),
        (date(2026, 12, 31), 2, date(2027, 2, 28)),  # clamped to month end
        (date(2027, 12, 31), 2, date(2028, 2, 29)),  # leap year
        (date(2026, 11, 30), 6, date(2027, 5, 30)),
        (date(2024, 8, 7), 6, date(2025, 2, 7)),
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2026, 3, 15), 0, date(2026, 3, 15)),
        (date(2026, 10, 31), 4, date(2027, 2, 28)),  # across a year end
    ],
)
def test_add_months(start: date, months: int, expected: date) -> None:
    """Same day N months on, clamped to the end of a shorter month."""
    assert add_months(start, months) == expected


def test_strike_off_kind() -> None:
    """The kind comes from the Gazette notice key, both spellings of compulsory."""
    assert strike_off_kind("gazette-notice-voluntary") == "voluntary"
    assert strike_off_kind("gazette-notice-compulsory") == "compulsory"
    assert strike_off_kind("gazette-notice-compulsary") == "compulsory"
    assert strike_off_kind("gazette-filings-brought-up-to-date") is None
    assert strike_off_kind(None) is None
    assert strike_off_kind("") is None


def test_notice_link() -> None:
    """With a transaction id the link is the PDF, otherwise the Gazette filings."""
    base = f"{FIND_AND_UPDATE_BASE}/company/45678901/filing-history"
    assert (
        notice_link("45678901", "tx1") == f"{base}/tx1/document?format=pdf&download=0"
    )
    assert notice_link("45678901", None) == f"{base}?category=gazette"
    assert notice_link("45678901", "") == f"{base}?category=gazette"


def test_running_countdown() -> None:
    """Two months from the notice; postal objections two weeks before that."""
    countdown = compute_countdown(
        kind="compulsory",
        notice_on=date(2026, 8, 25),
        suspended_on=None,
        transaction_id="tx1",
        today=date(2026, 9, 15),
    )
    assert countdown.earliest_on == date(2026, 10, 25)
    assert countdown.objection_deadline == date(2026, 10, 11)
    assert countdown.days_to_object == 26
    assert countdown.days_to_strike_off == 40
    assert not countdown.suspended
    assert countdown.caveat == CAVEAT_TWO_MONTHS
    assert countdown.kind_words == " (compulsory)"
    assert countdown.objection_phrase() == "26 days to object"
    assert countdown.attention_line() == (
        "Strike-off proposed (compulsory) — 26 days to object"
    )
    assert countdown.as_dict() == {
        "kind": "compulsory",
        "notice_on": "2026-08-25",
        "earliest_on": "2026-10-25",
        "objection_deadline": "2026-10-11",
        "days_to_object": 26,
        "days_to_strike_off": 40,
        "suspended_on": None,
        "transaction_id": "tx1",
        "caveat": CAVEAT_TWO_MONTHS,
    }


@pytest.mark.parametrize(
    ("today", "phrase"),
    [
        (date(2026, 10, 10), "1 day to object"),
        (date(2026, 10, 11), "last day to object by post"),
        (date(2026, 10, 12), "object online before 25 Oct 2026"),
        (date(2026, 10, 24), "object online before 25 Oct 2026"),
        (date(2026, 10, 25), "could be struck off any day now"),
        (date(2026, 10, 26), "could be struck off any day now"),
    ],
)
def test_objection_phrase_as_the_deadline_nears(today: date, phrase: str) -> None:
    """The phrase changes as the postal deadline and then the earliest date pass."""
    countdown = compute_countdown(
        kind="voluntary",
        notice_on=date(2026, 8, 25),
        suspended_on=None,
        transaction_id=None,
        today=today,
    )
    assert countdown.objection_phrase() == phrase
    assert countdown.attention_line() == f"Strike-off proposed (voluntary) — {phrase}"


def test_suspension_moves_the_earliest_date_six_months_on() -> None:
    """A suspension after the notice holds the strike-off off for six months."""
    countdown = compute_countdown(
        kind="compulsory",
        notice_on=date(2024, 7, 30),
        suspended_on=date(2024, 8, 7),
        transaction_id="tx-gaz1",
        today=date(2024, 9, 1),
    )
    assert countdown.suspended
    assert countdown.suspended_on == date(2024, 8, 7)
    assert countdown.earliest_on == date(2025, 2, 7)
    assert countdown.objection_deadline == date(2025, 1, 24)
    assert countdown.days_to_object == 145
    assert countdown.caveat == CAVEAT_SUSPENDED
    assert countdown.attention_line() == (
        "Strike-off suspended (compulsory) — earliest strike-off 7 Feb 2025"
    )
    # Long after: the register may still say "proposal to strike off".
    stale = compute_countdown(
        kind="compulsory",
        notice_on=date(2024, 7, 30),
        suspended_on=date(2024, 8, 7),
        transaction_id="tx-gaz1",
        today=date(2026, 9, 14),
    )
    assert stale.days_to_object < 0
    assert stale.objection_phrase() == "could be struck off any day now"
    assert stale.attention_line() == (
        "Strike-off suspended (compulsory) — hold ended 7 Feb 2025, could be "
        "struck off any day now"
    )
    # On the day the hold ends it is still ahead, just.
    ending = compute_countdown(
        kind="compulsory",
        notice_on=date(2024, 7, 30),
        suspended_on=date(2024, 8, 7),
        transaction_id="tx-gaz1",
        today=date(2025, 2, 7),
    )
    assert ending.days_to_strike_off == 0
    assert ending.attention_line() == (
        "Strike-off suspended (compulsory) — earliest strike-off 7 Feb 2025"
    )


def test_suspension_before_the_notice_is_ignored() -> None:
    """A suspension older than the notice belongs to an earlier cycle."""
    countdown = compute_countdown(
        kind="compulsory",
        notice_on=date(2026, 8, 25),
        suspended_on=date(2026, 1, 1),
        transaction_id=None,
        today=date(2026, 9, 15),
    )
    assert not countdown.suspended
    assert countdown.suspended_on is None
    assert countdown.earliest_on == date(2026, 10, 25)


def test_unknown_notice_date() -> None:
    """Without a notice date there is no countdown, and the line says so."""
    countdown = compute_countdown(
        kind=None,
        notice_on=None,
        suspended_on=None,
        transaction_id=None,
        today=date(2026, 9, 15),
    )
    assert countdown.earliest_on is None
    assert countdown.objection_deadline is None
    assert countdown.days_to_object is None
    assert countdown.days_to_strike_off is None
    assert countdown.caveat == CAVEAT_UNKNOWN
    assert countdown.kind_words == ""
    assert countdown.objection_phrase() == "notice date unknown"
    assert countdown.attention_line() == "Strike-off proposed — notice date unknown"
    # A suspension with no notice is kept as a fact but cannot be counted from.
    suspended = compute_countdown(
        kind=None,
        notice_on=None,
        suspended_on=date(2026, 9, 1),
        transaction_id=None,
        today=date(2026, 9, 15),
    )
    assert suspended.suspended
    assert suspended.earliest_on is None
    assert suspended.attention_line() == "Strike-off proposed — notice date unknown"


def test_countdown_attributes() -> None:
    """Entity attributes carry the countdown and the notice link, or all None."""
    empty = countdown_attributes(None, "45678901")
    assert empty == {
        "kind": None,
        "notice_on": None,
        "earliest_on": None,
        "objection_deadline": None,
        "suspended_on": None,
        "transaction_id": None,
        "link": None,
        "caveat": None,
    }
    countdown = compute_countdown(
        kind="voluntary",
        notice_on=date(2026, 8, 25),
        suspended_on=None,
        transaction_id="tx1",
        today=date(2026, 9, 15),
    )
    attrs = countdown_attributes(countdown, "45678901")
    assert attrs["kind"] == "voluntary"
    assert attrs["notice_on"] == "2026-08-25"
    assert attrs["earliest_on"] == "2026-10-25"
    assert attrs["objection_deadline"] == "2026-10-11"
    assert attrs["suspended_on"] is None
    assert attrs["transaction_id"] == "tx1"
    assert attrs["link"] == notice_link("45678901", "tx1")
    assert attrs["caveat"] == CAVEAT_TWO_MONTHS
    assert "days_to_object" not in attrs  # that is the sensor's state
