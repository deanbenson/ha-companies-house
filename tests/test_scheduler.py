"""Tests for the adaptive scheduler."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import random

import pytest

from custom_components.companies_house.const import (
    PROBE_INTERVALS,
    PROFILE_INTERVAL,
    PROFILE_INTERVAL_NEAR_DEADLINE,
    Tier,
)
from custom_components.companies_house.models import CompanyProfile
from custom_components.companies_house.scheduler import (
    BANK_HOLIDAYS,
    LONDON,
    appointments_next_run,
    compute_tier,
    computed_bank_holidays,
    days_until,
    disqualification_next_run,
    easter_sunday,
    hash_offset,
    is_bank_holiday,
    is_business_hours,
    jitter,
    london_start_of_day,
    next_slot,
    probe_interval,
    profile_interval,
    projected_requests_per_window,
    reconciliation_due,
    structure_due,
    to_london,
)

from .conftest import load_fixture

TUESDAY_10 = datetime(2026, 9, 15, 10, 0, tzinfo=LONDON)  # business hours
TUESDAY_02 = datetime(2026, 9, 15, 2, 0, tzinfo=LONDON)
TODAY = date(2026, 9, 15)


def _profile(**overrides: object) -> CompanyProfile:
    data = load_fixture("company_active/profile")
    data.update(overrides)
    return CompanyProfile.from_api(data)


# ---------------------------------------------------------------- calendar


def test_easter_and_computed_bank_holidays_match_gov_uk() -> None:
    """The algorithm reproduces every regular GOV.UK bank holiday in the table."""
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2027) == date(2027, 3, 28)
    assert easter_sunday(2028) == date(2028, 4, 16)
    assert easter_sunday(2029) == date(2029, 4, 1)
    for year in (2026, 2027, 2028):
        expected = {d for d in BANK_HOLIDAYS if d.year == year}
        assert computed_bank_holidays(year) == expected, year
    # Beyond the table the fallback is used.
    assert is_bank_holiday(date(2029, 3, 30))  # Good Friday 2029
    assert is_bank_holiday(date(2029, 12, 25))
    assert not is_bank_holiday(date(2029, 12, 24))
    # 2021: Christmas on a Saturday, Boxing Day on a Sunday -> 27th and 28th.
    assert {date(2021, 12, 27), date(2021, 12, 28)} <= computed_bank_holidays(2021)
    # 2022's one-offs are not knowable algorithmically, but the regulars are.
    assert date(2022, 12, 26) in computed_bank_holidays(2022)
    assert date(2022, 12, 27) in computed_bank_holidays(2022)


def test_bank_holidays_match_gov_uk_file() -> None:
    """The shipped table matches the GOV.UK bank-holidays.json, recorded as a fixture."""
    path = Path(__file__).parent / "fixtures" / "gov-uk-bank-holidays.json"
    events = json.loads(path.read_text())["england-and-wales"]["events"]
    gov = {date.fromisoformat(e["date"]) for e in events if e["date"] >= "2026"}
    assert gov == BANK_HOLIDAYS
    # The algorithm also reproduces the regular holidays of every earlier year in the file,
    # apart from one-off days (jubilees, funerals, coronations).
    one_offs = {
        date(2020, 5, 8),  # VE Day 75: early May holiday moved to the Friday
        date(2022, 6, 2),
        date(2022, 6, 3),  # Platinum Jubilee
        date(2022, 9, 19),  # State funeral
        date(2023, 5, 8),  # Coronation
    }
    moved = {date(2020, 5, 4), date(2022, 5, 30)}  # moved for VE Day 75 and the Jubilee
    for year in range(2019, 2026):
        gov_year = {
            date.fromisoformat(e["date"])
            for e in events
            if e["date"].startswith(str(year))
        }
        assert computed_bank_holidays(year) - moved <= gov_year, year
        assert gov_year - computed_bank_holidays(year) <= one_offs, year


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 9, 15, 8, 0, tzinfo=LONDON), True),
        (datetime(2026, 9, 15, 7, 59, tzinfo=LONDON), False),
        (datetime(2026, 9, 15, 18, 29, tzinfo=LONDON), True),
        (datetime(2026, 9, 15, 18, 30, tzinfo=LONDON), False),
        (datetime(2026, 9, 12, 10, 0, tzinfo=LONDON), False),  # Saturday
        (datetime(2026, 8, 31, 10, 0, tzinfo=LONDON), False),  # Summer bank holiday
        (datetime(2026, 12, 28, 10, 0, tzinfo=LONDON), False),  # Boxing Day substitute
        (datetime(2026, 9, 15, 9, 0, tzinfo=UTC), True),  # 10:00 BST
        (datetime(2026, 9, 15, 17, 45, tzinfo=UTC), False),  # 18:45 BST
        (datetime(2026, 9, 15, 10, 0), True),  # naive treated as UTC
    ],
)
def test_is_business_hours(when: datetime, expected: bool) -> None:
    """Business hours are Mon-Fri 08:00-18:30 London, excluding bank holidays."""
    assert is_business_hours(when) is expected


def test_bst_boundary_start_of_day() -> None:
    """A date at the end of October localises to the right day either side of BST."""
    before = london_start_of_day(date(2026, 10, 24))
    after = london_start_of_day(date(2026, 10, 26))
    assert before.utcoffset() == timedelta(hours=1)
    assert after.utcoffset() == timedelta(hours=0)
    assert before.date() == date(2026, 10, 24)
    assert after.date() == date(2026, 10, 26)
    assert to_london(before.astimezone(UTC)).date() == date(2026, 10, 24)
    assert to_london(after.astimezone(UTC)).date() == date(2026, 10, 26)
    # The deadline itself must not move a day when viewed in UTC.
    assert london_start_of_day(date(2026, 10, 31)).astimezone(UTC).date() == date(
        2026, 10, 31
    )
    assert days_until(date(2026, 10, 31), date(2026, 10, 24)) == 7
    assert days_until(None, TODAY) is None


# ---------------------------------------------------------------- tiers


def test_tier_close_watch_and_dissolved() -> None:
    """Dissolved wins over everything; close watch wins over the rest."""
    profile = _profile()
    assert (
        compute_tier(
            profile, close_watch=True, last_filing_date=TODAY, today=TODAY
        ).tier
        is Tier.CLOSE_WATCH
    )
    dissolved = _profile(company_status="dissolved")
    assert (
        compute_tier(
            dissolved, close_watch=True, last_filing_date=TODAY, today=TODAY
        ).tier
        is Tier.DISSOLVED
    )
    removed = _profile(company_status="removed")
    assert (
        compute_tier(
            removed, close_watch=False, last_filing_date=None, today=TODAY
        ).tier
        is Tier.DISSOLVED
    )


def test_tier_deadline_normal_quiet() -> None:
    """Deadline within 30 days, otherwise normal, quiet when nothing is happening."""
    near = _profile(confirmation_statement={"next_due": "2026-09-27"})
    decision = compute_tier(
        near, close_watch=False, last_filing_date=TODAY, today=TODAY
    )
    assert decision.tier is Tier.DEADLINE
    assert decision.reason == "confirmation_statement due in 12 days"
    normal = _profile()
    assert (
        compute_tier(
            normal, close_watch=False, last_filing_date=TODAY, today=TODAY
        ).tier
        is Tier.NORMAL
    )
    quiet = compute_tier(
        normal, close_watch=False, last_filing_date=date(2026, 1, 1), today=TODAY
    )
    assert quiet.tier is Tier.QUIET
    # A deadline within 90 days keeps a company out of the quiet tier.
    busy = _profile(accounts={"next_accounts": {"due_on": "2026-11-30"}})
    assert (
        compute_tier(
            busy, close_watch=False, last_filing_date=date(2026, 1, 1), today=TODAY
        ).tier
        is Tier.NORMAL
    )
    # No filings ever and no deadlines is quiet; no profile at all is normal.
    assert (
        compute_tier(
            _profile(accounts={}, confirmation_statement={}),
            close_watch=False,
            last_filing_date=None,
            today=TODAY,
        ).tier
        is Tier.QUIET
    )
    assert (
        compute_tier(None, close_watch=False, last_filing_date=None, today=TODAY).tier
        is Tier.NORMAL
    )


@pytest.mark.parametrize(
    ("tier", "when", "expected"),
    [
        (Tier.DEADLINE, TUESDAY_10, timedelta(minutes=30)),
        (Tier.DEADLINE, TUESDAY_02, timedelta(hours=3)),
        (Tier.CLOSE_WATCH, TUESDAY_10, timedelta(minutes=15)),
        (Tier.CLOSE_WATCH, TUESDAY_02, timedelta(hours=1)),
        (Tier.NORMAL, TUESDAY_10, timedelta(hours=2)),
        (Tier.NORMAL, TUESDAY_02, timedelta(hours=12)),
        (Tier.QUIET, TUESDAY_10, timedelta(hours=6)),
        (Tier.QUIET, TUESDAY_02, timedelta(hours=24)),
        (Tier.DISSOLVED, TUESDAY_10, timedelta(days=30)),
        (Tier.DISSOLVED, TUESDAY_02, timedelta(days=30)),
        (
            Tier.DEADLINE,
            datetime(2026, 8, 31, 10, 0, tzinfo=LONDON),
            timedelta(hours=3),
        ),
    ],
)
def test_probe_interval(tier: Tier, when: datetime, expected: timedelta) -> None:
    """Every tier probes at the spec's interval, and a bank holiday is out of hours."""
    interval, reason = probe_interval(tier, when)
    assert interval == expected
    assert reason in ("business hours", "out of hours")


def test_probe_interval_multiplier() -> None:
    """The cadence multiplier slows everything down proportionally."""
    assert probe_interval(Tier.NORMAL, TUESDAY_10, 2.0)[0] == timedelta(hours=4)
    assert PROBE_INTERVALS[Tier.NORMAL][0] == timedelta(hours=2)


def test_profile_interval() -> None:
    """Profile refreshes daily, 6 hourly near a deadline, monthly once dissolved."""
    assert profile_interval(_profile(), Tier.NORMAL, TODAY) == (
        PROFILE_INTERVAL,
        "daily",
    )
    near = _profile(confirmation_statement={"next_due": "2026-09-20"})
    assert (
        profile_interval(near, Tier.DEADLINE, TODAY)[0]
        == PROFILE_INTERVAL_NEAR_DEADLINE
    )
    assert profile_interval(near, Tier.DISSOLVED, TODAY)[0] == timedelta(days=30)
    assert profile_interval(None, Tier.NORMAL, TODAY)[0] == PROFILE_INTERVAL
    assert profile_interval(_profile(), Tier.NORMAL, TODAY, 3.0)[0] == timedelta(days=3)


# ---------------------------------------------------------------- spreading


def test_jitter_within_bounds() -> None:
    """Jitter stays within +/-15 percent and never drops below a minute."""
    rng = random.Random(42)
    base = timedelta(hours=2)
    samples = [jitter(base, rng) for _ in range(200)]
    assert all(timedelta(minutes=102) <= s <= timedelta(minutes=138) for s in samples)
    assert len(set(samples)) > 100
    assert jitter(timedelta(seconds=10), rng) == timedelta(seconds=60)
    assert timedelta(minutes=51) <= jitter(timedelta(hours=1)) <= timedelta(minutes=69)


def test_hash_offsets_spread_officers_across_the_day() -> None:
    """Thirty officers land in distinct hours; the offset is stable."""
    day = timedelta(days=1)
    offsets = [hash_offset(f"appointments:officer-{i}", day) for i in range(30)]
    assert all(timedelta(0) <= o < day for o in offsets)
    assert len({int(o.total_seconds() // 3600) for o in offsets}) >= 15
    assert hash_offset("x", day) == hash_offset("x", day)


def test_next_slot_daily_and_weekly() -> None:
    """Slots are periodic, hash spread, and strictly after the last run."""
    now = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    first = appointments_next_run("officer-1", now, None)
    assert now < first <= now + timedelta(days=1)
    again = appointments_next_run("officer-1", now, first)
    assert again == first + timedelta(days=1)
    # A run just before its slot does not trigger another run at the slot.
    early = appointments_next_run("officer-1", now, first - timedelta(hours=1))
    assert early == first + timedelta(days=1)
    # Never runs earlier than now even if the last run was long ago.
    stale = appointments_next_run("officer-1", now, now - timedelta(days=40))
    assert now < stale <= now + timedelta(days=1)
    weekly = disqualification_next_run("officer-1", now, None)
    assert now < weekly <= now + timedelta(days=7)
    assert next_slot("k", timedelta(hours=1), now, None) > now


def test_reconciliation_and_structure_due() -> None:
    """Weekly and monthly work is due once its slot passes, and immediately if never done."""
    now = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    assert reconciliation_due("12345678", now, None)
    assert not reconciliation_due("12345678", now, now)
    assert not reconciliation_due("12345678", now + timedelta(days=3), now)
    assert reconciliation_due("12345678", now + timedelta(days=11), now)
    assert structure_due("12345678", now, None)
    assert not structure_due("12345678", now + timedelta(days=14), now)
    assert structure_due("12345678", now + timedelta(days=46), now)


def test_projected_requests_per_window() -> None:
    """The worked example in the spec projects a handful of requests per window."""
    tiers = (
        [Tier.CLOSE_WATCH] * 2
        + [Tier.DEADLINE] * 4
        + [Tier.NORMAL] * 20
        + [Tier.QUIET] * 4
    )
    projected = projected_requests_per_window(tiers, 30)
    assert 1 < projected < 5
    assert projected_requests_per_window(tiers, 30, 2.0) < projected
    assert projected_requests_per_window([], 0) == 0
