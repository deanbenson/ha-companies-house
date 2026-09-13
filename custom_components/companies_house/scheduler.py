"""Adaptive polling cadence: tiers, business hours, bank holidays and jitter.

Everything here is a pure function of its inputs so the scheduling decisions
described in the spec can be tested exhaustively without Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import random
from zoneinfo import ZoneInfo

from .const import (
    APPOINTMENTS_INTERVAL,
    BUSINESS_HOURS_END,
    BUSINESS_HOURS_START,
    DEADLINE_TIER_DAYS,
    DISQUALIFICATION_INTERVAL,
    FINISHED_STATUSES,
    JITTER_FRACTION,
    PROBE_INTERVALS,
    PROFILE_INTERVAL,
    PROFILE_INTERVAL_NEAR_DEADLINE,
    PROFILE_NEAR_DEADLINE_DAYS,
    QUIET_NO_DEADLINE_DAYS,
    QUIET_NO_FILING_DAYS,
    RECONCILE_INTERVAL,
    STRUCTURE_INTERVAL,
    TIMEZONE,
    Tier,
)
from .models import CompanyProfile

LONDON = ZoneInfo(TIMEZONE)

# England and Wales bank holidays from https://www.gov.uk/bank-holidays.json,
# retrieved 13 September 2026. Years beyond the table fall back to the
# algorithm in :func:`computed_bank_holidays`, which reproduces the regular
# holidays but cannot know about one-off days such as a coronation.
BANK_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 4, 3),
        date(2026, 4, 6),
        date(2026, 5, 4),
        date(2026, 5, 25),
        date(2026, 8, 31),
        date(2026, 12, 25),
        date(2026, 12, 28),
        date(2027, 1, 1),
        date(2027, 3, 26),
        date(2027, 3, 29),
        date(2027, 5, 3),
        date(2027, 5, 31),
        date(2027, 8, 30),
        date(2027, 12, 27),
        date(2027, 12, 28),
        date(2028, 1, 3),
        date(2028, 4, 14),
        date(2028, 4, 17),
        date(2028, 5, 1),
        date(2028, 5, 29),
        date(2028, 8, 28),
        date(2028, 12, 25),
        date(2028, 12, 26),
    }
)
BANK_HOLIDAY_TABLE_YEARS: frozenset[int] = frozenset({2026, 2027, 2028})


def easter_sunday(year: int) -> date:
    """Return Easter Sunday using the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month, day = divmod(h + length - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _substitute(d: date, taken: set[date]) -> date:
    """Move a weekend holiday to the next free weekday, as the UK does."""
    while d.weekday() >= 5 or d in taken:
        d += timedelta(days=1)
    return d


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (7 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=last.weekday())


def computed_bank_holidays(year: int) -> frozenset[date]:
    """Return the regular England and Wales bank holidays for any year."""
    easter = easter_sunday(year)
    taken: set[date] = set()
    for d in (
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        _nth_monday(year, 5, 1),
        _last_monday(year, 5),
        _last_monday(year, 8),
    ):
        taken.add(_substitute(d, taken))
    christmas = _substitute(date(year, 12, 25), taken)
    taken.add(christmas)
    taken.add(_substitute(date(year, 12, 26), taken))
    return frozenset(taken)


def is_bank_holiday(d: date) -> bool:
    """Return True on an England and Wales bank holiday."""
    if d.year in BANK_HOLIDAY_TABLE_YEARS:
        return d in BANK_HOLIDAYS
    return d in computed_bank_holidays(d.year)


def to_london(now: datetime) -> datetime:
    """Convert an aware datetime to Europe/London."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(LONDON)


def is_business_hours(now: datetime) -> bool:
    """Return True Monday to Friday 08:00 to 18:30 London time, bank holidays excluded."""
    local = to_london(now)
    if local.weekday() >= 5 or is_bank_holiday(local.date()):
        return False
    start = time(*BUSINESS_HOURS_START)
    end = time(*BUSINESS_HOURS_END)
    return start <= local.time() < end


def london_start_of_day(d: date) -> datetime:
    """Return midnight at the start of ``d`` in Europe/London, as an aware datetime."""
    return datetime.combine(d, time.min, tzinfo=LONDON)


def days_until(d: date | None, today: date) -> int | None:
    """Return days from today to ``d``, negative once passed."""
    return None if d is None else (d - today).days


@dataclass(frozen=True)
class TierDecision:
    """A tier and why it was chosen."""

    tier: Tier
    reason: str


def compute_tier(
    profile: CompanyProfile | None,
    *,
    close_watch: bool,
    last_filing_date: date | None,
    today: date,
) -> TierDecision:
    """Pick the adaptive tier for a company (spec 6.4)."""
    if profile is not None and profile.company_status in FINISHED_STATUSES:
        return TierDecision(Tier.DISSOLVED, f"status {profile.company_status}")
    if close_watch:
        return TierDecision(Tier.CLOSE_WATCH, "close watch enabled")
    deadline = profile.next_deadline if profile is not None else None
    days = days_until(deadline[0], today) if deadline else None
    if days is not None and days <= DEADLINE_TIER_DAYS:
        return TierDecision(
            Tier.DEADLINE, f"{deadline[1] if deadline else ''} due in {days} days"
        )
    filing_gap = days_until(last_filing_date, today)
    no_recent_filing = filing_gap is None or -filing_gap >= QUIET_NO_FILING_DAYS
    no_near_deadline = days is None or days > QUIET_NO_DEADLINE_DAYS
    if no_recent_filing and no_near_deadline and profile is not None:
        return TierDecision(
            Tier.QUIET, "no filing in 6 months and no deadline within 90 days"
        )
    return TierDecision(Tier.NORMAL, "active company")


def probe_interval(
    tier: Tier, now: datetime, multiplier: float = 1.0
) -> tuple[timedelta, str]:
    """Return the probe interval for a tier at a moment in time, and the reason."""
    business, out_of_hours = PROBE_INTERVALS[tier]
    if is_business_hours(now):
        return business * multiplier, "business hours"
    return out_of_hours * multiplier, "out of hours"


def profile_interval(
    profile: CompanyProfile | None, tier: Tier, today: date, multiplier: float = 1.0
) -> tuple[timedelta, str]:
    """Return the profile refresh interval (spec 6.3)."""
    if tier is Tier.DISSOLVED:
        return PROBE_INTERVALS[Tier.DISSOLVED][0] * multiplier, "dissolved"
    deadline = profile.next_deadline if profile is not None else None
    if deadline is not None:
        days = days_until(deadline[0], today)
        if days is not None and days <= PROFILE_NEAR_DEADLINE_DAYS:
            return (
                PROFILE_INTERVAL_NEAR_DEADLINE * multiplier,
                "within 14 days of a deadline",
            )
    return PROFILE_INTERVAL * multiplier, "daily"


def jitter(interval: timedelta, rng: random.Random | None = None) -> timedelta:
    """Spread an interval by +/- JITTER_FRACTION so nothing moves in lockstep."""
    rng = rng or random.SystemRandom()
    factor = 1 + rng.uniform(-JITTER_FRACTION, JITTER_FRACTION)
    return timedelta(seconds=max(60.0, interval.total_seconds() * factor))


def hash_offset(key: str, period: timedelta) -> timedelta:
    """Return a stable offset within ``period`` derived from ``key``."""
    digest = hashlib.sha256(key.encode()).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return timedelta(seconds=period.total_seconds() * fraction)


def next_slot(
    key: str, period: timedelta, now: datetime, last_run: datetime | None
) -> datetime:
    """Return the next hash-spread slot for a periodic job.

    Slots are ``epoch + offset + k * period``. The first slot strictly after
    ``last_run`` (or ``now`` when never run) is returned, so thirty officers
    with a daily period spread evenly across the day. A slot less than half a
    period after ``last_run`` is skipped so a fresh run is never followed by
    another almost immediately; once on the grid, runs are exactly a period
    apart.
    """
    offset = hash_offset(key, period).total_seconds()
    period_s = period.total_seconds()
    anchor = max(now, last_run) if last_run else now
    elapsed = anchor.timestamp() - offset
    k = elapsed // period_s + 1
    candidate = offset + k * period_s
    if last_run is not None and candidate < last_run.timestamp() + period_s / 2:
        candidate += period_s
    return datetime.fromtimestamp(candidate, tz=UTC)


def reconciliation_due(
    company_number: str, now: datetime, last: datetime | None
) -> bool:
    """Return True when a company's weekly reconciliation slot has passed."""
    if last is None:
        return True
    return now >= next_slot(
        f"reconcile:{company_number}", RECONCILE_INTERVAL, last, last
    )


def structure_due(company_number: str, now: datetime, last: datetime | None) -> bool:
    """Return True when a company's monthly structure refresh slot has passed."""
    if last is None:
        return True
    return now >= next_slot(
        f"structure:{company_number}", STRUCTURE_INTERVAL, last, last
    )


def appointments_next_run(
    officer_id: str, now: datetime, last_run: datetime | None
) -> datetime:
    """Return the next daily appointments slot for an officer."""
    return next_slot(f"appointments:{officer_id}", APPOINTMENTS_INTERVAL, now, last_run)


def disqualification_next_run(
    officer_id: str, now: datetime, last_run: datetime | None
) -> datetime:
    """Return the next weekly disqualification slot for an officer."""
    return next_slot(
        f"disqualification:{officer_id}", DISQUALIFICATION_INTERVAL, now, last_run
    )


def projected_requests_per_window(
    tiers: list[Tier], officer_count: int, multiplier: float = 1.0
) -> float:
    """Estimate scheduled requests per 5 minute window during business hours.

    Used by the options flow to refuse configurations that would exceed the
    budget. It is deliberately pessimistic: business hour probe rates plus the
    amortised daily, weekly and monthly work.
    """
    window = timedelta(minutes=5)
    total = 0.0
    for tier in tiers:
        business, _ = PROBE_INTERVALS[tier]
        total += window / (business * multiplier)
        total += window / (PROFILE_INTERVAL_NEAR_DEADLINE * multiplier)
        # Reconciliation (about 9 requests) weekly and structure (3) monthly.
        total += 9 * window / (RECONCILE_INTERVAL * multiplier)
        total += 3 * window / (STRUCTURE_INTERVAL * multiplier)
    total += officer_count * (
        window / (APPOINTMENTS_INTERVAL * multiplier)
        + window / (DISQUALIFICATION_INTERVAL * multiplier)
    )
    return round(total, 2)
