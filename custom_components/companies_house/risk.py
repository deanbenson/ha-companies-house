"""Counterparty risk rating: a green, amber or red traffic light per company.

Everything here is a pure function of the data the integration already holds
(profile, officers, PSCs, charges, insolvency, the probe's recent filings,
the stored state and the followed people), so the scoring table can be tested
exhaustively without Home Assistant and costs no API requests.

The rating is a *register health* rating built from Companies House data
alone. It cannot see county court judgments, winding-up petitions before an
order, trade payment behaviour or bank data, which commercial credit scores
rely on, so it is never a credit limit. Missing data counts as risk rather
than as the absence of risk: a company whose directors, charges or insolvency
record are not monitored, or whose register data is stale, cannot be green.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from .const import FINISHED_STATUSES, INSOLVENT_STATUSES, STATUS_DETAIL_STRIKE_OFF
from .enumerations import COMPANY_STATUS, INSOLVENCY_CASE_TYPE
from .models import (
    AppointmentList,
    ChargeList,
    CompanyProfile,
    DisqualificationResult,
    FilingHistoryItem,
    Insolvency,
    InsolvencyCase,
    JsonDict,
    OfficerList,
    PscData,
    display_name,
    parse_date,
)
from .store import CompanyState

# Bump when the table below changes, so automations can pin a version.
SCORING_VERSION: Final = "1"

BAND_GREEN: Final = "green"
BAND_AMBER: Final = "amber"
BAND_RED: Final = "red"
BANDS: Final = [BAND_GREEN, BAND_AMBER, BAND_RED]
BAND_RANK: Final[dict[str, int]] = {BAND_GREEN: 0, BAND_AMBER: 1, BAND_RED: 2}

AMBER_FROM: Final = 10
RED_FROM: Final = 30
SCORE_CAP: Final = 100

BASIS: Final = (
    "Companies House data only: filing compliance, status, board, ownership, "
    "charges and the register's own flags. It cannot see county court "
    "judgments, winding-up petitions, payment behaviour or bank data, so it "
    "is a register health rating, not a credit limit."
)

# Every point value in one place, so tuning the table is a data change.
POINTS: Final[dict[str, int]] = {
    # Hard red overrides.
    "R1": 100,  # dissolved or removed
    "R2": 100,  # insolvent status
    "R3": 100,  # open insolvency case
    "R4": 60,  # strike-off proposed or applied for
    "R5": 40,  # registered office moved to the Companies House default address
    "R6": 100,  # sanctioned person with significant control
    "R7": 40,  # a followed director is disqualified
    "R8": 40,  # accounts more than 180 days overdue
    # A. Filing compliance.
    "A1_1_30": 10,
    "A1_31_90": 18,
    "A1_91_180": 28,
    "A2_1_30": 6,
    "A2_31_90": 10,
    "A2_91_": 16,
    "A3": 6,
    "A4_each": 4,
    "A4_max": 12,
    "A5": 6,
    "A6": 8,
    "A8": 2,
    # B. Age, board and ownership.
    "B1_under_1": 8,
    "B1_1_3": 5,
    "B1_3_9": 2,
    "B2_none": 15,
    "B2_sole": 4,
    "B2_sole_corporate": 7,
    "B3_1": 3,
    "B3_2": 6,
    "B3_3": 10,
    "B4": 4,
    "B5": 8,
    "B6_none": 3,
    "B6_unidentified": 6,
    "B7": 4,
    "B7_all_ceased": 5,
    "B8_each": 6,
    "B8_max": 12,
    "B8_dissolved": 3,
    # C. Security, insolvency history, registered office, identity.
    "C1_1_2": 2,
    "C1_3_5": 4,
    "C1_6_": 6,
    "C2_recent": 4,
    "C2_several": 8,
    "C3": 10,
    "C4": 3,
    "C5": 10,
    "C5_old": 5,
    "C6": 12,
    "C7": 8,
    "C8": 3,
    "C9": 2,
    "C10": 3,
    "C11": 2,
    # D. Uncertainty: unknown is not green.
    "D2": 5,
    "D3_officers": 4,
    "D3_charges": 4,
    "D3_insolvency": 4,
    "D3_psc": 2,
    "D4": 4,
}

_SECTION_ORDER: Final = {"R": 0, "A": 1, "B": 2, "C": 3, "D": 4}

DIRECTOR_ROLES: Final = frozenset(
    {
        "director",
        "corporate-director",
        "nominee-director",
        "corporate-nominee-director",
    }
)
LLP_MEMBER_ROLES: Final = frozenset(
    {
        "llp-member",
        "llp-designated-member",
        "corporate-llp-member",
        "corporate-llp-designated-member",
    }
)
CORPORATE_ROLES: Final = frozenset(
    {
        "corporate-director",
        "corporate-nominee-director",
        "corporate-llp-member",
        "corporate-llp-designated-member",
    }
)

STRIKE_OFF_NOTICES: Final[dict[str, str]] = {
    "gazette-notice-voluntary": "voluntary",
    "gazette-notice-compulsory": "compulsory",
    "gazette-notice-compulsary": "compulsory",
}
STRIKE_OFF_ENDED: Final = frozenset(
    {
        "gazette-filings-brought-up-to-date",
        "dissolution-voluntary-strike-off-discontinued",
        "dissolution-voluntary-strike-off-suspended",
        "dissolved-compulsory-strike-off-suspended",
    }
)
STRIKE_OFF_APPLICATIONS: Final = frozenset(
    {
        "dissolution-application-strike-off-company",
        "dissolution-application-strike-off-limited-liability-partnership",
    }
)
STRIKE_OFF_WITHDRAWALS: Final = frozenset(
    {
        "dissolution-withdrawal-application-strike-off-company",
        "dissolution-withdrawal-application-strike-off-limited-liability-partnership",
    }
)
DEFAULT_ADDRESS_FILING: Final = (
    "default-companies-house-registered-office-address-applied"
)
DEFAULT_ADDRESS_MARKERS: Final = ("crown way", "po box 4385")

INSOLVENCY_START_DATES: Final = frozenset(
    {
        "petitioned-on",
        "wound-up-on",
        "ordered-to-wind-up-on",
        "administration-started-on",
        "voluntary-arrangement-started-on",
        "moratorium-started-on",
        "instrumented-on",
        "declaration-solvent-on",
    }
)
INSOLVENCY_END_DATES: Final = frozenset(
    {
        "administration-ended-on",
        "administration-discharged-on",
        "concluded-winding-up-on",
        "voluntary-arrangement-ended-on",
        "moratorium-ended-on",
        "dissolved-on",
    }
)

UNIDENTIFIED_PSC_STATEMENTS: Final = frozenset(
    {
        "psc-exists-but-not-identified",
        "psc-exists-but-not-identified-partnership",
        "psc-details-not-confirmed",
        "psc-details-not-confirmed-partnership",
        "psc-contacted-but-no-response",
        "psc-contacted-but-no-response-partnership",
        "psc-has-failed-to-confirm-changed-details",
        "psc-has-failed-to-confirm-changed-details-partnership",
        "restrictions-notice-issued-to-psc",
        "restrictions-notice-issued-to-psc-partnership",
        "awaiting-confirmation-from-psc",
        "steps-to-find-psc-not-yet-completed",
        "steps-to-find-psc-not-yet-completed-partnership",
    }
)
MINIMAL_DISCLOSURE_ACCOUNTS: Final = frozenset(
    {"micro-entity", "total-exemption-small", "unaudited-abridged", "small"}
)
HMRC_MARKERS: Final = ("hm revenue", "hmrc")
# Only original accounts count as late; amended accounts arrive later by nature.
ORIGINAL_ACCOUNTS_PREFIX: Final = "accounts-with-accounts-type"

# Days after the period end that accounts are due (private companies and
# LLPs; public companies get six months). First accounts get 21 months from
# incorporation, which is why the look-back skips a company's first period.
ACCOUNTS_DEADLINE_MONTHS: Final = 9
PLC_ACCOUNTS_DEADLINE_MONTHS: Final = 6
FIRST_ACCOUNTS_MONTHS: Final = 21
STALE_PROFILE_DAYS: Final = 14
LATE_FILING_LOOKBACK_DAYS: Final = 3 * 365
NOTHING_FILED_DAYS: Final = 456
YEAR_DAYS: Final = 365
TWO_YEARS_DAYS: Final = 2 * 365
DUE_SOON_DAYS: Final = 30
COVERED_DATASETS: Final = (
    "profile",
    "filings",
    "officers",
    "psc",
    "charges",
    "insolvency",
)


@dataclass(frozen=True, kw_only=True)
class TrackedPerson:
    """What is known about a followed person, for the connected-party rules."""

    name: str
    officer_ids: Sequence[str]
    disqualification: DisqualificationResult | None = None
    appointments: AppointmentList | None = None


@dataclass(frozen=True, kw_only=True)
class RiskItem:
    """One triggered line of the scoring table."""

    code: str
    points: int
    text: str
    # Quiet items only show when the band is amber or red.
    quiet: bool = False

    @property
    def section(self) -> str:
        """Return the table section: R, A, B, C or D."""
        return self.code[0]


@dataclass(frozen=True, kw_only=True)
class RiskResult:
    """The rating of one company and how it was reached."""

    band: str | None
    score: int
    reasons: list[str]
    reason: str
    overrides: list[str]
    items: list[RiskItem]
    coverage: dict[str, str | None]
    data_age_days: int | None
    info: dict[str, Any] = field(default_factory=dict)
    computed_at: datetime | None = None
    scoring_version: str = SCORING_VERSION

    def as_dict(self) -> JsonDict:
        """Return the rating as plain data, for attributes, reports and tools."""
        return {
            "band": self.band,
            "score": self.score,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "overrides": list(self.overrides),
            "coverage": dict(self.coverage),
            "data_age_days": self.data_age_days,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
            "scoring_version": self.scoring_version,
            "basis": BASIS,
            "info": dict(self.info),
        }


# ---------------------------------------------------------------- helpers


def _pretty_date(value: date | None) -> str:
    """Render a date the way the reports do: ``8 Sep 2026``."""
    return value.strftime("%-d %b %Y") if value else "an unknown date"


def _duration(days: int) -> str:
    """Render a span of days as people say it: ``45 days``, ``4 months``, ``3 years``."""
    if days <= 60:
        return f"{days} day" if days == 1 else f"{days} days"
    if days < 2 * YEAR_DAYS:
        return f"{max(2, round(days / 30.44))} months"
    return f"{days // YEAR_DAYS} years"


def _age_words(days: int) -> str:
    if days < YEAR_DAYS:
        months = max(1, days // 30)
        return f"{months} month" if months == 1 else f"{months} months"
    years = days // YEAR_DAYS
    return f"{years} year" if years == 1 else f"{years} years"


def add_months(value: date, months: int) -> date:
    """Return the same day ``months`` later, clamped to the end of the month."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = (date(year + (month // 12), month % 12 + 1, 1) - date.resolution).day
    return date(year, month, min(value.day, last_day))


def _in_status(status: str | None) -> str:
    """Say ``in liquidation`` or ``in administration`` without doubling the ``in``."""
    word = str(COMPANY_STATUS.get(status or "", status or "unknown")).lower()
    return word if word.startswith("in ") else f"in {word}"


def _case_word(case_type: str | None) -> str:
    if not case_type:
        return "insolvency"
    return str(INSOLVENCY_CASE_TYPE.get(case_type, case_type)).lower()


def _case_start(case: InsolvencyCase) -> date | None:
    starts = [d for k, d in case.dates.items() if k in INSOLVENCY_START_DATES]
    return min(starts) if starts else None


def _case_end(case: InsolvencyCase) -> date | None:
    ends = [d for k, d in case.dates.items() if k in INSOLVENCY_END_DATES]
    return max(ends) if ends else None


def _open_cases(insolvency: Insolvency | None) -> list[InsolvencyCase]:
    if insolvency is None:
        return []
    return [c for c in insolvency.cases if _case_start(c) and not _case_end(c)]


def _board_roles(profile: CompanyProfile) -> frozenset[str]:
    return LLP_MEMBER_ROLES if profile.type == "llp" else DIRECTOR_ROLES


def _board_word(profile: CompanyProfile, plural: bool = False) -> str:
    if profile.type == "llp":
        return "members" if plural else "member"
    return "directors" if plural else "director"


def _newest(items: Iterable[FilingHistoryItem]) -> FilingHistoryItem | None:
    dated = [i for i in items if i.date is not None]
    return max(dated, key=lambda i: i.date or date.min) if dated else None


def _latest_of(
    filings: Sequence[FilingHistoryItem], keys: frozenset[str] | Mapping[str, str]
) -> FilingHistoryItem | None:
    return _newest(i for i in filings if i.description in keys)


# ---------------------------------------------------------------- the scorer


class _Scorer:
    """Walk the scoring table for one company, collecting triggered items."""

    def __init__(
        self,
        *,
        profile: CompanyProfile,
        officers: OfficerList | None,
        psc: PscData | None,
        charges: ChargeList | None,
        insolvency: Insolvency | None,
        filings: Sequence[FilingHistoryItem],
        state: CompanyState,
        coverage: Mapping[str, datetime | None],
        people: Sequence[TrackedPerson],
        today: date,
    ) -> None:
        self.profile = profile
        self.officers = officers
        self.psc = psc
        self.charges = charges
        self.insolvency = insolvency
        self.filings = filings
        self.state = state
        self.coverage = coverage
        self.people = people
        self.today = today
        self.items: list[RiskItem] = []
        self.overrides: list[str] = []
        self.info: dict[str, Any] = {}
        self._board_changed = False
        self._insolvent = False

    def add(self, code: str, text: str, *, points: int | None = None) -> None:
        """Record a triggered line; overrides are the R codes."""
        value = POINTS[code] if points is None else points
        self.items.append(
            RiskItem(code=code.split("_", maxsplit=1)[0], points=value, text=text)
        )
        if code.startswith("R"):
            self.overrides.append(code)

    def add_quiet(self, code: str, text: str) -> None:
        """Record a line that only shows once the band is amber or red."""
        self.items.append(
            RiskItem(
                code=code.split("_", maxsplit=1)[0],
                points=POINTS[code],
                text=text,
                quiet=True,
            )
        )

    def _days_ago(self, value: date | None) -> int | None:
        return None if value is None else (self.today - value).days

    def _company_age_days(self) -> int | None:
        return self._days_ago(self.profile.date_of_creation)

    def _older_than(self, days: int) -> bool:
        age = self._company_age_days()
        return age is not None and age > days

    # -- R: hard overrides --------------------------------------------------

    def overrides_section(self) -> None:
        """Status events that are a band on their own."""
        profile = self.profile
        status = profile.company_status
        open_cases = _open_cases(self.insolvency)
        if status in INSOLVENT_STATUSES:
            self._insolvent = True
            self.add("R2", self._insolvent_text(status, open_cases))
        elif open_cases:
            self._insolvent = True
            case = open_cases[0]
            self.add(
                "R3",
                f"insolvency case open: {_case_word(case.type)} since "
                f"{_pretty_date(_case_start(case))}",
            )
        if self.psc is not None:
            for psc in self.psc.items:
                if psc.is_sanctioned and not psc.ceased:
                    self.add(
                        "R6",
                        f"{display_name(psc.name)} (person with significant control) "
                        "is sanctioned",
                    )
        self._strike_off()
        self._default_address()
        for person, result in self._disqualified_people():
            self.add(
                "R7",
                f"{person.name} is a disqualified director until "
                f"{_pretty_date(result.disqualified_until)}",
            )

    def _insolvent_text(self, status: str | None, cases: list[InsolvencyCase]) -> str:
        if cases:
            case = cases[0]
            since = _pretty_date(_case_start(case))
            if case.type == "members-voluntary-liquidation":
                return f"being wound up (solvent liquidation) since {since}"
            return f"in {_case_word(case.type)} since {since}"
        return _in_status(status)

    def _strike_off(self) -> None:
        notice = _latest_of(self.filings, STRIKE_OFF_NOTICES)
        ended = _latest_of(self.filings, STRIKE_OFF_ENDED)
        notice_live = notice is not None and (
            ended is None or (ended.date or date.min) < (notice.date or date.min)
        )
        proposed = (
            self.profile.company_status_detail == STATUS_DETAIL_STRIKE_OFF
            or self.state.strike_off_notice_on is not None
        )
        if proposed or notice_live:
            kind = STRIKE_OFF_NOTICES.get(notice.description or "") if notice else None
            when = self.state.strike_off_notice_on or (notice.date if notice else None)
            words = f" ({kind})" if kind else ""
            text = (
                f"strike-off proposed{words} on {_pretty_date(when)}"
                if when
                else f"strike-off proposed{words}, date unknown"
            )
            if (
                ended is not None
                and not notice_live
                and "suspended" in (ended.description or "")
            ):
                text += f", suspended on {_pretty_date(ended.date)}"
            self.add("R4", text)
            return
        application = _latest_of(self.filings, STRIKE_OFF_APPLICATIONS)
        withdrawal = _latest_of(self.filings, STRIKE_OFF_WITHDRAWALS)
        if application is not None and (
            withdrawal is None
            or (withdrawal.date or date.min) < (application.date or date.min)
        ):
            self.add(
                "R4",
                f"the {_board_word(self.profile, plural=True)} applied to strike the "
                f"company off on {_pretty_date(application.date)}",
            )

    def _default_address(self) -> None:
        address = self.profile.registered_office_address
        line = address.one_line().casefold() if address else ""
        at_default = all(marker in line for marker in DEFAULT_ADDRESS_MARKERS)
        moved = _latest_of(self.filings, frozenset({DEFAULT_ADDRESS_FILING}))
        moved_back = _newest(
            i
            for i in self.filings
            if i.category == "address" and i.description != DEFAULT_ADDRESS_FILING
        )
        moved_live = moved is not None and (
            moved_back is None
            or (moved_back.date or date.min) < (moved.date or date.min)
        )
        if at_default or moved_live:
            when = moved.date if moved else None
            self.add(
                "R5",
                "registered office moved to the Companies House default address"
                + (f" on {_pretty_date(when)}" if when else ""),
            )

    def _people_here(self) -> list[TrackedPerson]:
        """Followed people who hold a live appointment at this company."""
        if self.officers is None:
            return []
        ids = {
            o.officer_id for o in self.officers.items if o.is_active and o.officer_id
        }
        return [p for p in self.people if any(i in ids for i in p.officer_ids)]

    def _disqualified_people(
        self,
    ) -> list[tuple[TrackedPerson, DisqualificationResult]]:
        found: list[tuple[TrackedPerson, DisqualificationResult]] = []
        for person in self._people_here():
            result = person.disqualification
            if result is not None and result.disqualified:
                found.append((person, result))
        return found

    # -- A: filing compliance ---------------------------------------------

    def compliance_section(self) -> None:
        """Late and missing filings: the strongest non-financial predictor."""
        accounts_late = self._accounts_overdue()
        statement_late = self._statement_overdue()
        if accounts_late and statement_late:
            self.add("A3", "both accounts and confirmation statement overdue")
        self._late_history()
        accounts = self.profile.accounts
        if (
            accounts.last_made_up_to is None
            and accounts.last_type is None
            and self._older_than(FIRST_ACCOUNTS_MONTHS * 30)
        ):
            self.add("A5", "no accounts filed yet")
        if accounts.last_type == "dormant":
            self.add("A6", "files dormant accounts (declares it is not trading)")
        elif accounts.last_type in MINIMAL_DISCLOSURE_ACCOUNTS:
            self.info["accounts_disclosure"] = "minimal"
        gap = self._days_ago(self.state.newest_filing_date)
        if (
            gap is not None
            and gap > NOTHING_FILED_DAYS
            and self._older_than(TWO_YEARS_DAYS)
        ):
            self.add("A8", f"nothing filed for {_duration(gap)}")
        due_soon = [
            kind
            for due, kind in (
                (accounts.next_due, "accounts"),
                (
                    self.profile.confirmation_statement.next_due,
                    "confirmation_statement",
                ),
            )
            if due is not None and 0 <= (due - self.today).days <= DUE_SOON_DAYS
        ]
        if due_soon:
            self.info["due_soon"] = due_soon

    def _accounts_overdue(self) -> bool:
        accounts = self.profile.accounts
        days = self._days_ago(accounts.next_due)
        if days is not None and days > 0:
            words = f"accounts {_duration(days)} overdue"
            if days > 180:
                self.add("R8", words)
            elif days > 90:
                self.add("A1_91_180", words)
            elif days > 30:
                self.add("A1_31_90", words)
            else:
                self.add("A1_1_30", words)
            return True
        if accounts.next_overdue and accounts.next_due is None:
            self.add("A1_31_90", "accounts overdue")
            return True
        return False

    def _statement_overdue(self) -> bool:
        statement = self.profile.confirmation_statement
        days = self._days_ago(statement.next_due)
        if days is not None and days > 0:
            words = f"confirmation statement {_duration(days)} overdue"
            if days > 90:
                self.add("A2_91_", words)
            elif days > 30:
                self.add("A2_31_90", words)
            else:
                self.add("A2_1_30", words)
            return True
        if statement.overdue and statement.next_due is None:
            self.add("A2_31_90", "confirmation statement overdue")
            return True
        return False

    def _late_history(self) -> None:
        """Count accounts among the recent filings delivered after their deadline."""
        deadline_months = (
            PLC_ACCOUNTS_DEADLINE_MONTHS
            if self.profile.type == "plc"
            else ACCOUNTS_DEADLINE_MONTHS
        )
        created = self.profile.date_of_creation
        late = 0
        for item in self.filings:
            if (
                not (item.description or "").startswith(ORIGINAL_ACCOUNTS_PREFIX)
                or item.date is None
                or (self.today - item.date).days > LATE_FILING_LOOKBACK_DAYS
            ):
                continue
            made_up_to = parse_date(item.description_values.get("made_up_date"))
            if made_up_to is None:
                continue
            due = add_months(made_up_to, deadline_months)
            if created is not None and (made_up_to - created).days < 548:
                # First accounts: 21 months from incorporation.
                due = max(due, add_months(created, FIRST_ACCOUNTS_MONTHS))
            if item.date > due:
                late += 1
        if late:
            times = "once" if late == 1 else f"{late} times"
            self.add(
                "A4_each",
                f"filed accounts late {times} in the last 3 years",
                points=min(late * POINTS["A4_each"], POINTS["A4_max"]),
            )

    # -- B: age, board and ownership ----------------------------------------

    def board_section(self) -> None:
        """Company age, directors and people with significant control."""
        self._age()
        if self.officers is not None:
            self._board()
            self._connected_parties()
        if self.psc is not None:
            self._ownership()

    def _age(self) -> None:
        age = self._company_age_days()
        if age is None:
            return
        if age < YEAR_DAYS:
            self.add("B1_under_1", f"incorporated {_age_words(age)} ago")
        elif age < 3 * YEAR_DAYS:
            self.add("B1_1_3", f"only {_age_words(age)} old")
        elif age < 9 * YEAR_DAYS:
            self.add_quiet("B1_3_9", f"{_age_words(age)} old")

    def _board(self) -> None:
        assert self.officers is not None
        profile = self.profile
        roles = _board_roles(profile)
        board = [o for o in self.officers.items if o.officer_role in roles]
        current = [o for o in board if o.is_active]
        word = _board_word(profile)
        if not current:
            self.add("B2_none", f"no {_board_word(profile, plural=True)} in office")
        elif len(current) == 1:
            if current[0].officer_role in CORPORATE_ROLES:
                self.add("B2_sole_corporate", f"the sole {word} is a company")
            else:
                self.add("B2_sole", f"sole {word}")
        year_ago = self.today - date.resolution * YEAR_DAYS
        resigned = [
            o for o in board if o.resigned_on is not None and o.resigned_on >= year_ago
        ]
        appointed = [
            o
            for o in board
            if (o.appointed_on or o.appointed_before) is not None
            and (o.appointed_on or o.appointed_before or date.min) >= year_ago
        ]
        board_then = [
            o
            for o in board
            if (o.appointed_on or o.appointed_before or date.min) < year_ago
            and (o.resigned_on is None or o.resigned_on >= year_ago)
        ]
        if resigned:
            n = len(resigned)
            # Half the board going is grave on a real board; on a board of two
            # it is one person leaving, which the count already scores.
            half_gone = len(board_then) >= 3 and n * 2 >= len(board_then)
            code = "B3_3" if n >= 3 or half_gone else ("B3_2" if n == 2 else "B3_1")
            who = f"1 {word} resigned" if n == 1 else f"{n} {word}s resigned"
            self.add(code, f"{who} in the last year")
            shrink = n - len(appointed)
            if shrink >= 2:
                self.add("B4", f"the board shrank by {shrink} in the last year")
        if (
            current
            and self._older_than(TWO_YEARS_DAYS)
            and all(
                o.appointed_on is not None and o.appointed_on >= year_ago
                for o in current
            )
        ):
            self._board_changed = True
            self.add("B5", f"every {word} changed in the last year")

    def _ownership(self) -> None:
        assert self.psc is not None
        psc = self.psc
        active = [p for p in psc.items if not p.ceased]
        statements = [s for s in psc.statements if s.ceased_on is None]
        unidentified = [
            s for s in statements if s.statement in UNIDENTIFIED_PSC_STATEMENTS
        ]
        if unidentified:
            self.add("B6_unidentified", "owner not identified or not confirmed")
        elif not psc.items and not statements:
            self.add("B6_none", "no person with significant control recorded")
        year_ago = self.today - date.resolution * YEAR_DAYS
        if psc.items and not active:
            self._board_changed = True
            self.add(
                "B7_all_ceased", "every person with significant control has ceased"
            )
            return
        changed = [
            d
            for p in psc.items
            for d in (p.ceased_on, p.notified_on)
            if d is not None and d >= year_ago
        ]
        if changed and self._older_than(TWO_YEARS_DAYS):
            self._board_changed = True
            self.add("B7", f"ownership changed on {_pretty_date(max(changed))}")

    def _connected_parties(self) -> None:
        """Followed directors of this company still serving at failed companies."""
        insolvent_points = 0
        for person in self._people_here():
            if person.appointments is None:
                continue
            dissolved = 0
            for appointment in person.appointments.items:
                if appointment.company_number == self.profile.company_number:
                    continue
                if appointment.company_status in FINISHED_STATUSES:
                    dissolved += 1
                elif (
                    appointment.resigned_on is None
                    and appointment.company_status in INSOLVENT_STATUSES
                    and insolvent_points < POINTS["B8_max"]
                ):
                    insolvent_points += POINTS["B8_each"]
                    self.add(
                        "B8_each",
                        f"{person.name} is a director of "
                        f"{appointment.company_name or appointment.company_number}, "
                        f"now {_in_status(appointment.company_status)}",
                    )
            if dissolved >= 3:
                self.add(
                    "B8_dissolved",
                    f"{person.name} has been a director of {dissolved} dissolved "
                    "companies",
                )

    # -- C: security, history, office, identity -----------------------------

    def security_section(self) -> None:
        """Charges, insolvency history, the registered office and renames."""
        profile = self.profile
        if self.charges is not None:
            self._charges()
        if (
            profile.has_insolvency_history
            and not self._insolvent
            and profile.company_status not in FINISHED_STATUSES
        ):
            self._insolvency_history()
        if profile.undeliverable_registered_office_address:
            self.add("C6", "post to the registered office is undeliverable")
        if profile.registered_office_is_in_dispute:
            self.add("C7", "the registered office address is in dispute")
        self._office_moves()
        self._renames()
        if profile.can_file is False and profile.company_status == "active":
            self.info["cannot_file"] = True
            self.add_quiet("C11", "the company cannot file online")

    def _charges(self) -> None:
        assert self.charges is not None
        outstanding = [c for c in self.charges.items if c.is_outstanding]
        n = len(outstanding)
        if n:
            code = "C1_6_" if n >= 6 else ("C1_3_5" if n >= 3 else "C1_1_2")
            self.add(
                code, "1 outstanding charge" if n == 1 else f"{n} outstanding charges"
            )
        year_ago = self.today - date.resolution * YEAR_DAYS
        half_year_ago = self.today - date.resolution * 183
        recent_year = [
            c for c in self.charges.items if c.created_on and c.created_on >= year_ago
        ]
        recent_half = [
            c for c in recent_year if (c.created_on or date.min) >= half_year_ago
        ]
        if len(recent_year) >= 2:
            self.add(
                "C2_several",
                f"{len(recent_year)} new charges registered in the last year",
            )
        elif recent_half:
            charge = max(recent_half, key=lambda c: c.created_on or date.min)
            lender = ", ".join(charge.persons_entitled) or "a lender"
            self.add(
                "C2_recent",
                f"new charge registered on {_pretty_date(charge.created_on)} "
                f"in favour of {lender}",
            )
        if any(
            any(m in p.casefold() for m in HMRC_MARKERS)
            for c in outstanding
            for p in c.persons_entitled
        ):
            self.add("C3", "a charge in favour of HMRC is outstanding")
        # Floating charge over all assets: read when the charge model carries
        # the particulars (added by the charges feature), silent otherwise.
        if any(
            getattr(c, "floating_charge_covers_all", False) is True for c in outstanding
        ):
            self.add(
                "C4",
                "an all-assets debenture is outstanding (trade creditors rank behind)",
            )

    def _insolvency_history(self) -> None:
        cases = self.insolvency.cases if self.insolvency is not None else []
        if not cases:
            self.add("C5", "has insolvency history")
            return
        case = max(cases, key=lambda c: _case_end(c) or _case_start(c) or date.min)
        end = _case_end(case)
        if end is not None:
            years_ago = (self.today - end).days // YEAR_DAYS
            self.add(
                "C5_old" if years_ago >= 5 else "C5",
                f"has insolvency history ({_case_word(case.type)}, ended {end.year})",
            )
            return
        start = _case_start(case)
        self.add(
            "C5",
            f"has insolvency history ({_case_word(case.type)}"
            + (f", {start.year})" if start else ")"),
        )

    def _office_moves(self) -> None:
        year_ago = self.today - date.resolution * YEAR_DAYS
        logged = 0
        for change in self.state.changes:
            if (
                change.get("kind") != "profile"
                or change.get("event_type") != "address-changed"
            ):
                continue
            at = str(change.get("at") or "")[:10]
            try:
                when = date.fromisoformat(at)
            except ValueError:
                continue
            if when >= year_ago:
                logged += 1
        filed = sum(
            1
            for i in self.filings
            if i.category == "address"
            and i.date is not None
            and i.date >= year_ago
            and i.description != DEFAULT_ADDRESS_FILING
        )
        moves = max(logged, filed)
        if moves >= 2:
            self.add("C8", f"registered office changed {moves} times in the last year")

    def _renames(self) -> None:
        two_years_ago = self.today - date.resolution * 2 * YEAR_DAYS
        half_year_ago = self.today - date.resolution * 183
        renames = [
            n.ceased_on
            for n in self.profile.previous_company_names
            if n.ceased_on is not None and n.ceased_on >= two_years_ago
        ]
        if len(renames) >= 2:
            self.add("C9", f"renamed {len(renames)} times in the last two years")
        if self._board_changed and any(d >= half_year_ago for d in renames):
            self.add("C10", "renamed after a change of control")

    # -- D: uncertainty -----------------------------------------------------

    def uncertainty_section(self) -> int:
        """Unknown is not green. Returns the uncertainty points added."""
        before = sum(i.points for i in self.items)
        age = self.data_age_days()
        if age is not None and age > STALE_PROFILE_DAYS:
            self.add("D2", f"register data is {age} days old")
        checks = (
            ("officers", "D3_officers", "directors not checked"),
            ("charges", "D3_charges", "charges not checked"),
            ("insolvency", "D3_insolvency", "insolvency record not checked"),
            ("psc", "D3_psc", "ownership not checked"),
        )
        for dataset, code, text in checks:
            if dataset not in self.coverage:
                self.add(code, text)
        if self.profile.partial_data_available:
            self.add("D4", "the register holds only partial data")
        return sum(i.points for i in self.items) - before

    def data_age_days(self) -> int | None:
        """How old the profile is, in days."""
        fetched = self.coverage.get("profile")
        if fetched is None:
            return None
        return max(0, (self.today - fetched.date()).days)


def _order(items: list[RiskItem]) -> list[RiskItem]:
    return sorted(items, key=lambda i: (-i.points, _SECTION_ORDER.get(i.section, 9)))


def _reason_line(band: str, reasons: list[str]) -> str:
    if not reasons:
        return f"{band.capitalize()}: no concerns on the register"
    line = f"{band.capitalize()}: " + "; ".join(reasons[:4])
    if len(reasons) > 4:
        line += f" and {len(reasons) - 4} more"
    return line


def _coverage_out(coverage: Mapping[str, datetime | None]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for dataset in COVERED_DATASETS:
        fetched = coverage.get(dataset)
        out[dataset] = fetched.isoformat() if fetched is not None else None
    return out


def compute_risk(
    *,
    profile: CompanyProfile | None,
    officers: OfficerList | None = None,
    psc: PscData | None = None,
    charges: ChargeList | None = None,
    insolvency: Insolvency | None = None,
    filings: Sequence[FilingHistoryItem] = (),
    state: CompanyState | None = None,
    coverage: Mapping[str, datetime | None] | None = None,
    people: Sequence[TrackedPerson] = (),
    today: date,
    accounts: object | None = None,
) -> RiskResult:
    """Rate a company from the register data on hand.

    ``coverage`` maps each dataset that has been fetched (by its ``Dataset``
    value) to when it was fetched; a dataset missing from it counts as not
    checked. ``people`` are the followed people, for disqualification and
    connected-party rules. ``accounts`` is reserved for the structured
    accounts feature (net liabilities, falling cash and the like); it is
    accepted and ignored until that lands, so callers can pass it now.
    """
    del accounts  # Hook for the accounts feature: not scored yet.
    state = state or CompanyState()
    coverage = coverage or {}
    if profile is None or state.not_found:
        return RiskResult(
            band=None,
            score=0,
            reasons=[],
            reason="Unknown: "
            + (
                "the company was not found on the register"
                if state.not_found
                else "register data not fetched yet"
            ),
            overrides=[],
            items=[],
            coverage=_coverage_out(coverage),
            data_age_days=None,
            info={"error": "not found"} if state.not_found else {},
        )
    scorer = _Scorer(
        profile=profile,
        officers=officers,
        psc=psc,
        charges=charges,
        insolvency=insolvency,
        filings=filings,
        state=state,
        coverage=coverage,
        people=people,
        today=today,
    )
    if profile.company_status in FINISHED_STATUSES:
        # Nothing else about a dead company is worth scoring.
        if profile.company_status == "dissolved":
            text = (
                f"dissolved on {_pretty_date(profile.date_of_cessation)}"
                if profile.date_of_cessation
                else "dissolved"
            )
        else:
            text = "removed from the register"
        scorer.add("R1", text)
        uncertainty = 0
    else:
        scorer.overrides_section()
        scorer.compliance_section()
        scorer.board_section()
        scorer.security_section()
        uncertainty = scorer.uncertainty_section()
    items = _order(scorer.items)
    score = min(SCORE_CAP, sum(i.points for i in items))
    if scorer.overrides or score >= RED_FROM:
        band = BAND_RED
    elif score >= AMBER_FROM or uncertainty > 0:
        # Unknown is never green, even when the points alone would allow it.
        band = BAND_AMBER
    else:
        band = BAND_GREEN
    reasons = [i.text for i in items if not i.quiet or band != BAND_GREEN]
    age = scorer.data_age_days()
    if age is not None and age > 1:
        reasons.append(f"register data {age} days old")
    return RiskResult(
        band=band,
        score=score,
        reasons=reasons,
        reason=_reason_line(band, reasons),
        overrides=list(scorer.overrides),
        items=items,
        coverage=_coverage_out(coverage),
        data_age_days=age,
        info=scorer.info,
    )


def band_worsened(old: str | None, new: str | None) -> bool:
    """Whether a band change is for the worse, treating unknown as neutral."""
    if old is None or new is None:
        return False
    return BAND_RANK.get(new, 0) > BAND_RANK.get(old, 0)


__all__ = [
    "BANDS",
    "BAND_AMBER",
    "BAND_GREEN",
    "BAND_RED",
    "BASIS",
    "POINTS",
    "SCORING_VERSION",
    "RiskItem",
    "RiskResult",
    "TrackedPerson",
    "add_months",
    "band_worsened",
    "compute_risk",
]
