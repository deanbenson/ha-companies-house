"""Tests for the risk rating: every line of the scoring table, on hand-built data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from custom_components.companies_house.accounts import (
    AccountsHistory,
    AccountsYear,
    Figure,
)
from custom_components.companies_house.models import (
    Appointment,
    AppointmentList,
    Charge,
    ChargeList,
    CompanyProfile,
    DisqualificationResult,
    FilingHistoryItem,
    Insolvency,
    OfficerList,
    PscData,
)
from custom_components.companies_house.risk import (
    BAND_AMBER,
    BAND_GREEN,
    BAND_RED,
    POINTS,
    SCORING_VERSION,
    RiskResult,
    TrackedPerson,
    add_months,
    band_worsened,
    compute_risk,
)
from custom_components.companies_house.store import CompanyState

from .conftest import load_fixture

TODAY = date(2026, 9, 15)
NOW = datetime(2026, 9, 15, 9, tzinfo=UTC)
FULL_COVERAGE = dict.fromkeys(
    ("profile", "filings", "officers", "psc", "charges", "insolvency", "accounts"),
    NOW,
)


def _profile(**overrides: Any) -> CompanyProfile:
    """A healthy ten-year-old company, with any top-level field overridden."""
    data = load_fixture("company_active/profile")
    data.update(overrides)
    return CompanyProfile.from_api(data)


def _accounts(**next_accounts: Any) -> dict[str, Any]:
    """The profile ``accounts`` object with the last accounts kept and the next set."""
    return {
        "last_accounts": {"made_up_to": "2025-03-31", "type": "micro-entity"},
        "next_accounts": next_accounts,
    }


def _officer(
    name: str = "SMITH, Jane",
    *,
    role: str = "director",
    appointed: str | None = "2015-03-12",
    resigned: str | None = None,
    officer_id: str = "officer-jane",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "officer_role": role,
        "links": {
            "self": f"/company/12345678/appointments/appt-{officer_id}",
            "officer": {"appointments": f"/officers/{officer_id}/appointments"},
        },
    }
    if appointed:
        item["appointed_on"] = appointed
    if resigned:
        item["resigned_on"] = resigned
    return item


def _officers(*items: dict[str, Any]) -> OfficerList:
    return OfficerList.from_api({"items": list(items)})


def _two_directors() -> OfficerList:
    return _officers(_officer(), _officer("PATEL, Priya", officer_id="officer-priya"))


def _psc(
    items: list[dict[str, Any]] | None = None,
    statements: list[dict[str, Any]] | None = None,
) -> PscData:
    if items is None:
        items = [{"name": "Ms Jane Smith", "notified_on": "2016-04-06"}]
    return PscData.from_api({"items": items}, {"items": statements or []})


def _charge(
    status: str = "outstanding",
    created: str = "2016-01-04",
    lender: str = "Big Bank plc",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "created_on": created,
        "persons_entitled": [{"name": lender}],
        **extra,
    }


def _charges(*items: dict[str, Any]) -> ChargeList:
    return ChargeList.from_api({"items": list(items)})


def _filing(
    description: str,
    on: str,
    *,
    category: str = "gazette",
    values: dict[str, Any] | None = None,
) -> FilingHistoryItem:
    return FilingHistoryItem.from_api(
        {
            "transaction_id": f"{description}-{on}",
            "category": category,
            "date": on,
            "description": description,
            "description_values": values or {},
        }
    )


def _accounts_filing(made_up: str, filed: str) -> FilingHistoryItem:
    return _filing(
        "accounts-with-accounts-type-micro-entity",
        filed,
        category="accounts",
        values={"made_up_date": made_up},
    )


def _insolvency(*cases: dict[str, Any], status: list[str] | None = None) -> Insolvency:
    return Insolvency.from_api({"status": status or [], "cases": list(cases)})


def _case(case_type: str, **dates: str) -> dict[str, Any]:
    return {
        "number": "1",
        "type": case_type,
        "dates": [{"type": k.replace("_", "-"), "date": v} for k, v in dates.items()],
    }


def _figure(
    value: int | None, prior: int | None = None, status: str | None = None
) -> Figure:
    """A read figure, or an undisclosed one when there is no value."""
    return Figure(
        value=None if value is None else Decimal(value),
        prior=None if prior is None else Decimal(prior),
        status=status or ("ok" if value is not None else "not_disclosed"),
    )


def _year(
    made_up_to: str,
    *,
    status: str = "ok",
    source: str = "ixbrl",
    **figures: Figure,
) -> AccountsYear:
    return AccountsYear(
        transaction_id=f"tx-{made_up_to}",
        made_up_to=date.fromisoformat(made_up_to),
        accounts_type="full",
        status=status,
        source=source,
        figures=figures,
    )


def _history(*years: AccountsYear) -> AccountsHistory:
    """Accounts as the coordinator keeps them: newest first."""
    return AccountsHistory(years=list(years), checked=True)


def rate(
    profile: CompanyProfile | None = None, *, full: bool = True, **kwargs: Any
) -> RiskResult:
    """Rate with every dataset checked and healthy unless told otherwise."""
    if profile is None:
        profile = _profile()
    defaults: dict[str, Any] = {
        "officers": _two_directors(),
        "psc": _psc(),
        "charges": _charges(),
        "insolvency": _insolvency(),
        "coverage": FULL_COVERAGE if full else {"profile": NOW, "filings": NOW},
        "today": TODAY,
    }
    defaults.update(kwargs)
    return compute_risk(profile=profile, **defaults)


def codes(result: RiskResult) -> list[str]:
    return [i.code for i in result.items]


def points(result: RiskResult, code: str) -> int:
    return sum(i.points for i in result.items if i.code == code)


# ---------------------------------------------------------------- bands


def test_healthy_company_is_green() -> None:
    """Ten years old, two directors, one charge, everything checked: green."""
    result = rate(charges=_charges(_charge()))
    assert result.band == BAND_GREEN
    assert result.score == 2
    assert result.reasons == ["1 outstanding charge"]
    assert result.reason == "Green: 1 outstanding charge"
    assert result.overrides == []
    assert result.data_age_days == 0
    assert result.scoring_version == SCORING_VERSION
    assert result.info == {"accounts_disclosure": "minimal"}
    assert result.coverage == {k: NOW.isoformat() for k in FULL_COVERAGE}


def test_nothing_wrong_still_says_something() -> None:
    """Green with no reasons has a sentence, not an empty string."""
    result = rate(_profile(accounts=_accounts()))
    assert result.score == 0
    assert result.reason == "Green: no concerns on the register"
    assert result.as_dict()["reason"] == result.reason


def test_worked_examples_from_the_briefing() -> None:
    """The sanity checks in the risk briefing hold."""
    sole = _officers(_officer())
    assert rate(charges=_charges(_charge()), officers=sole).score == 6
    young = _profile(date_of_creation="2024-06-01")
    resigned = _officers(_officer(), _officer("GONE, Bob", resigned="2026-05-01"))
    result = rate(young, officers=resigned)
    assert result.band == BAND_AMBER
    assert result.score == 12
    assert result.reason == (
        "Amber: only 2 years old; sole director; 1 director resigned in the last year"
    )
    overdue = _profile(accounts=_accounts(due_on="2026-07-15", overdue=True))
    assert rate(overdue, officers=sole).score == 22
    both = _profile(
        accounts=_accounts(due_on="2026-05-15", overdue=True),
        confirmation_statement={"next_due": "2026-08-20", "overdue": True},
    )
    result = rate(both)
    assert result.score == 28 + 6 + 6
    assert result.band == BAND_RED
    assert rate(_profile(undeliverable_registered_office_address=True)).score == 12


def test_profile_only_is_amber_because_unknown_is_not_green() -> None:
    """With nothing wrong but nothing checked, the rating cannot be green."""
    result = compute_risk(profile=_profile(), coverage={"profile": NOW}, today=TODAY)
    assert result.band == BAND_AMBER
    assert result.score == 14
    # One line for everything unchecked, so the one-liner keeps its slots.
    assert result.reasons == [
        "directors, charges, insolvency record and ownership not checked"
    ]
    assert result.reason == (
        "Amber: directors, charges, insolvency record and ownership not checked"
    )
    assert codes(result) == ["D3"]
    assert result.coverage["officers"] is None
    two = rate(
        coverage={k: v for k, v in FULL_COVERAGE.items() if k in ("profile", "psc")},
        officers=None,
        charges=None,
    )
    assert two.reasons == ["directors, charges and insolvency record not checked"]
    assert two.score == 12


def test_uncertainty_alone_never_lets_a_low_score_go_green() -> None:
    """Two points of doubt are enough to hold the band at amber."""
    coverage = {k: v for k, v in FULL_COVERAGE.items() if k != "psc"}
    result = rate(coverage=coverage, psc=None)
    assert result.score == 2
    assert result.band == BAND_AMBER
    assert result.reasons == ["ownership not checked"]


def test_unknown_when_there_is_no_profile_or_the_company_is_missing() -> None:
    """No band at all until the register has answered."""
    result = compute_risk(profile=None, today=TODAY)
    assert result.band is None
    assert result.reason == "Unknown: register data not fetched yet"
    assert result.coverage["profile"] is None
    missing = compute_risk(
        profile=_profile(),
        state=CompanyState(not_found=True),
        coverage={"profile": NOW},
        today=TODAY,
    )
    assert missing.band is None
    assert missing.info == {"error": "not found"}
    assert missing.reason == "Unknown: the company was not found on the register"


def test_reason_line_caps_at_four_and_orders_by_points() -> None:
    """The one-liner lists the four heaviest reasons then counts the rest."""
    profile = _profile(
        accounts=_accounts(due_on="2026-08-01", overdue=True),
        confirmation_statement={"next_due": "2026-09-01", "overdue": True},
        undeliverable_registered_office_address=True,
        registered_office_is_in_dispute=True,
        date_of_creation="2024-01-01",
    )
    result = rate(profile, officers=_officers(_officer()))
    assert codes(result) == ["A1", "C6", "C7", "A2", "A3", "B1", "B2"]
    assert result.reason == (
        "Red: accounts 45 days overdue; post to the registered office is "
        "undeliverable; the registered office address is in dispute; "
        "confirmation statement 14 days overdue and 3 more"
    )
    assert len(result.reasons) == 7


def test_no_accounts_read_changes_nothing() -> None:
    """With no accounts, unread accounts or figures not disclosed, nothing scores."""
    assert rate(accounts=None).score == rate().score
    assert rate(accounts=AccountsHistory()).score == rate().score
    pending = _history(_year("2025-12-31", status="pending", source="none"))
    assert rate(accounts=pending).score == rate().score
    paper = _history(_year("2025-12-31", status="no_ixbrl", source="none"))
    assert rate(accounts=paper).score == rate().score
    # Micro-entity accounts leave most lines out: never a mark against them.
    micro = _history(
        _year(
            "2025-12-31",
            net_assets=_figure(None),
            cash=_figure(None),
            creditors_within_one_year=_figure(None),
            employees=_figure(None),
        )
    )
    result = rate(accounts=micro)
    assert result.score == rate().score
    assert result.info["accounts_figures_at"] == "2025-12-31"
    # A figure that could not be trusted (two values tagged) is not scored either.
    conflict = _history(
        _year("2025-12-31", net_assets=_figure(-5000, status="conflict"))
    )
    assert rate(accounts=conflict).score == rate().score


# ---------------------------------------------------------------- R overrides


def test_dissolved_is_red_and_nothing_else_is_scored() -> None:
    """A dead company is red for that reason alone."""
    result = rate(
        _profile(
            company_status="dissolved",
            date_of_cessation="2024-08-13",
            undeliverable_registered_office_address=True,
        ),
        officers=None,
        full=False,
    )
    assert result.band == BAND_RED
    assert result.score == 100
    assert result.overrides == ["R1"]
    assert result.reasons == ["dissolved on 13 Aug 2024"]
    assert rate(_profile(company_status="dissolved")).reasons == ["dissolved"]
    assert rate(_profile(company_status="removed")).reasons == [
        "removed from the register"
    ]


def test_insolvent_status_wording_follows_the_case() -> None:
    """Status alone, a creditors' liquidation, and a solvent winding up."""
    assert rate(_profile(company_status="administration")).reasons[0] == (
        "in administration"
    )
    assert rate(_profile(company_status="receivership")).reasons[0] == (
        "in receiver action"
    )
    cvl = rate(
        _profile(company_status="liquidation"),
        insolvency=_insolvency(
            _case("creditors-voluntary-liquidation", wound_up_on="2026-07-01")
        ),
    )
    assert cvl.overrides == ["R2"]
    assert cvl.reasons[0] == "in creditors voluntary liquidation since 1 Jul 2026"
    mvl = rate(
        _profile(company_status="liquidation"),
        insolvency=_insolvency(
            _case("members-voluntary-liquidation", declaration_solvent_on="2026-06-20")
        ),
    )
    assert mvl.reasons[0] == "being wound up (solvent liquidation) since 20 Jun 2026"
    assert "has insolvency history" not in " ".join(mvl.reasons)


def test_open_case_is_red_even_while_the_status_says_active() -> None:
    """An administration that has started and not ended overrides an active status."""
    result = rate(
        _profile(has_insolvency_history=True),
        insolvency=_insolvency(
            _case("in-administration", administration_started_on="2026-08-01")
        ),
    )
    assert result.overrides == ["R3"]
    assert (
        result.reasons[0] == "insolvency case open: in administration since 1 Aug 2026"
    )
    assert "C5" not in codes(result)
    closed = rate(
        _profile(has_insolvency_history=True),
        insolvency=_insolvency(
            _case(
                "in-administration",
                administration_started_on="2019-08-01",
                administration_ended_on="2020-08-01",
            )
        ),
    )
    assert "R3" not in closed.overrides
    assert closed.reasons == ["has insolvency history (in administration, ended 2020)"]
    assert points(closed, "C5") == POINTS["C5_old"]


def test_strike_off_from_the_status_detail_and_from_notices() -> None:
    """The status detail, the stored notice date and the Gazette filing all count."""
    result = rate(_profile(company_status_detail="active-proposal-to-strike-off"))
    assert result.overrides == ["R4"]
    assert result.reasons[0] == "strike-off proposed, date unknown"
    dated = rate(
        _profile(company_status_detail="active-proposal-to-strike-off"),
        state=CompanyState(strike_off_notice_on=date(2026, 8, 25)),
        filings=[_filing("gazette-notice-compulsory", "2026-08-25")],
    )
    assert dated.reasons[0] == "strike-off proposed (compulsory) on 25 Aug 2026"
    # A notice among the filings with nothing in the state or the status
    # detail is one the register has moved on from (the probe remembers every
    # notice it sees, and forgets it when the register clears): not live.
    notice_only = rate(filings=[_filing("gazette-notice-voluntary", "2026-08-25")])
    assert "R4" not in notice_only.overrides
    assert notice_only.band == BAND_GREEN
    # The kind and date come from the filing when the state has only the date.
    kind_from_filing = rate(
        state=CompanyState(strike_off_notice_on=date(2026, 8, 25)),
        filings=[_filing("gazette-notice-voluntary", "2026-08-25")],
    )
    assert kind_from_filing.reasons[0] == (
        "strike-off proposed (voluntary) on 25 Aug 2026"
    )
    assert kind_from_filing.band == BAND_RED
    ended = rate(
        filings=[
            _filing("gazette-notice-compulsory", "2026-06-01"),
            _filing("gazette-filings-brought-up-to-date", "2026-06-20"),
        ]
    )
    assert "R4" not in ended.overrides
    suspended = rate(
        _profile(company_status_detail="active-proposal-to-strike-off"),
        filings=[
            _filing("gazette-notice-compulsory", "2024-07-30"),
            _filing("dissolved-compulsory-strike-off-suspended", "2024-08-07"),
        ],
    )
    assert suspended.reasons[0] == (
        "strike-off proposed (compulsory) on 30 Jul 2024, suspended on 7 Aug 2024"
    )


def test_strike_off_ends_the_way_the_probe_says_it_does() -> None:
    """Withdrawals, discontinuations and suspensions all end a notice or a DS01.

    The scorer reads the same list of ending filings as the probe, which
    clears the stored notice date on them, so the two never disagree.
    """
    withdrawn_after_notice = rate(
        filings=[
            _filing("dissolution-application-strike-off-company", "2026-05-01"),
            _filing("gazette-notice-voluntary", "2026-06-01"),
            _filing(
                "dissolution-withdrawal-application-strike-off-company", "2026-06-20"
            ),
        ]
    )
    assert withdrawn_after_notice.overrides == []
    assert withdrawn_after_notice.band == BAND_GREEN
    discontinued_after_application = rate(
        filings=[
            _filing("dissolution-application-strike-off-company", "2026-05-01"),
            _filing("gazette-notice-voluntary", "2026-06-01"),
            _filing("dissolution-voluntary-strike-off-discontinued", "2026-06-20"),
        ]
    )
    assert discontinued_after_application.overrides == []
    suspended_after_application = rate(
        filings=[
            _filing("dissolution-application-strike-off-company", "2026-05-01"),
            _filing("dissolution-voluntary-strike-off-suspended", "2026-06-20"),
        ]
    )
    assert suspended_after_application.overrides == []
    # A fresh application after the last ending counts again.
    applied_again = rate(
        filings=[
            _filing("dissolution-application-strike-off-company", "2026-05-01"),
            _filing("dissolution-voluntary-strike-off-discontinued", "2026-06-20"),
            _filing("dissolution-application-strike-off-company", "2026-08-01"),
        ]
    )
    assert applied_again.overrides == ["R4"]
    assert applied_again.reasons[0].endswith("on 1 Aug 2026")


def test_strike_off_application_by_the_directors() -> None:
    """A DS01 counts until it is withdrawn."""
    applied = rate(
        filings=[_filing("dissolution-application-strike-off-company", "2026-09-01")]
    )
    assert applied.overrides == ["R4"]
    assert applied.reasons[0] == (
        "the directors applied to strike the company off on 1 Sep 2026"
    )
    withdrawn = rate(
        filings=[
            _filing("dissolution-application-strike-off-company", "2026-09-01"),
            _filing(
                "dissolution-withdrawal-application-strike-off-company", "2026-09-10"
            ),
        ]
    )
    assert withdrawn.overrides == []
    llp = rate(
        _profile(type="llp"),
        filings=[
            _filing(
                "dissolution-application-strike-off-limited-liability-partnership",
                "2026-09-01",
            )
        ],
    )
    assert llp.reasons[0].startswith("the members applied")


def test_default_registered_office_address() -> None:
    """Moved to Crown Way by the registrar, from the filing or the address itself."""
    by_filing = rate(
        filings=[
            _filing(
                "default-companies-house-registered-office-address-applied",
                "2026-08-01",
                category="address",
            )
        ]
    )
    assert by_filing.overrides == ["R5"]
    assert by_filing.reasons[0] == (
        "registered office moved to the Companies House default address on 1 Aug 2026"
    )
    by_address = rate(
        _profile(
            registered_office_address={
                "address_line_1": "PO Box 4385",
                "address_line_2": "Crown Way",
                "locality": "Cardiff",
                "postal_code": "CF14 3UZ",
            }
        )
    )
    assert by_address.reasons[0] == (
        "registered office moved to the Companies House default address"
    )
    moved_on = rate(
        filings=[
            _filing(
                "default-companies-house-registered-office-address-applied",
                "2026-05-01",
                category="address",
            ),
            _filing(
                "change-registered-office-address-company-with-date-old-address-new-address",
                "2026-06-01",
                category="address",
            ),
        ]
    )
    assert "R5" not in moved_on.overrides


def test_default_address_is_seen_in_every_field() -> None:
    """The PO box field and the register's newer wording both give it away.

    A company already at the default address when added has no filing
    among the recent ones to say so, so the address itself must do.
    """
    po_box = rate(
        _profile(
            registered_office_address={
                "address_line_1": "Crown Way",
                "po_box": "PO Box 4385",
                "locality": "Cardiff",
                "postal_code": "CF14 3UZ",
            }
        )
    )
    assert po_box.overrides == ["R5"]
    worded = rate(
        _profile(
            registered_office_address={
                "address_line_1": "Companies House Default Address",
                "po_box": "4385",
                "locality": "Cardiff",
                "postal_code": "CF14 8LH",
            }
        )
    )
    assert worded.overrides == ["R5"]
    assert worded.reasons[0] == (
        "registered office moved to the Companies House default address"
    )
    # A PO box 4385 somewhere else is somebody's own.
    elsewhere = rate(
        _profile(
            registered_office_address={
                "po_box": "PO Box 4385",
                "locality": "Leeds",
                "postal_code": "LS1 1AA",
            }
        )
    )
    assert elsewhere.overrides == []
    assert rate(_profile(registered_office_address=None)).overrides == []


def test_sanctioned_controller_is_red() -> None:
    """A sanctioned PSC is a hard override; a ceased one is history."""
    live = rate(
        psc=_psc([{"name": "Mr Bad Actor", "is_sanctioned": True}]),
    )
    assert live.overrides == ["R6"]
    assert live.reasons[0] == (
        "Mr Bad Actor (person with significant control) is sanctioned"
    )
    ceased = rate(
        psc=_psc(
            [
                {
                    "name": "Mr Bad Actor",
                    "is_sanctioned": True,
                    "ceased_on": "2020-01-01",
                },
                {"name": "Ms Fine", "notified_on": "2020-01-01"},
            ]
        )
    )
    assert "R6" not in ceased.overrides


def test_disqualified_followed_director() -> None:
    """A followed person who is disqualified and sits here is an override."""
    jane = TrackedPerson(
        name="Jane Smith",
        officer_ids=["officer-jane"],
        disqualification=DisqualificationResult(
            disqualified=True, disqualified_until=date(2031, 1, 1)
        ),
    )
    result = rate(people=[jane])
    assert result.overrides == ["R7"]
    assert result.reasons[0] == "Jane Smith is a disqualified director until 1 Jan 2031"
    elsewhere = TrackedPerson(
        name="Someone Else",
        officer_ids=["officer-other"],
        disqualification=DisqualificationResult(disqualified=True),
    )
    assert rate(people=[elsewhere]).overrides == []
    clean = TrackedPerson(
        name="Jane Smith",
        officer_ids=["officer-jane"],
        disqualification=DisqualificationResult(disqualified=False),
    )
    assert rate(people=[clean]).overrides == []
    assert rate(people=[jane], officers=None).overrides == []


# ---------------------------------------------------------------- A compliance


@pytest.mark.parametrize(
    ("due", "code", "value", "words"),
    [
        ("2026-09-01", "A1", POINTS["A1_1_30"], "accounts 14 days overdue"),
        ("2026-07-20", "A1", POINTS["A1_31_90"], "accounts 57 days overdue"),
        ("2026-06-01", "A1", POINTS["A1_91_180"], "accounts 3 months overdue"),
        ("2026-02-01", "R8", POINTS["R8"], "accounts 7 months overdue"),
    ],
)
def test_accounts_overdue_bands(due: str, code: str, value: int, words: str) -> None:
    """Each late band scores as the table says, and beyond six months is red."""
    result = rate(_profile(accounts=_accounts(due_on=due)))
    assert points(result, code) == value
    assert result.reasons[0] == words
    assert (result.band == BAND_RED) == (code == "R8")
    assert ("R8" in result.overrides) == (code == "R8")


@pytest.mark.parametrize(
    ("due", "value", "words"),
    [
        ("2026-09-01", POINTS["A2_1_30"], "confirmation statement 14 days overdue"),
        ("2026-07-20", POINTS["A2_31_90"], "confirmation statement 57 days overdue"),
        ("2026-04-20", POINTS["A2_91_"], "confirmation statement 5 months overdue"),
    ],
)
def test_confirmation_statement_overdue_bands(due: str, value: int, words: str) -> None:
    """The confirmation statement is lighter than accounts but still counts."""
    result = rate(_profile(confirmation_statement={"next_due": due}))
    assert points(result, "A2") == value
    assert result.reasons[0] == words


def test_overdue_flags_without_dates_take_the_middle_band() -> None:
    """The register's overdue flag with no date is treated as one to three months."""
    result = rate(
        _profile(
            accounts=_accounts(overdue=True),
            confirmation_statement={"overdue": True},
        )
    )
    assert points(result, "A1") == POINTS["A1_31_90"]
    assert points(result, "A2") == POINTS["A2_31_90"]
    assert points(result, "A3") == POINTS["A3"]
    assert result.reasons[:3] == [
        "accounts overdue",
        "confirmation statement overdue",
        "both accounts and confirmation statement overdue",
    ]
    future = rate(_profile(accounts=_accounts(due_on="2026-12-31")))
    assert "A1" not in codes(future)


def test_late_filing_history_from_recent_accounts() -> None:
    """Accounts delivered after their deadline count, capped, with first accounts spared."""
    late = rate(filings=[_accounts_filing("2025-03-31", "2026-01-15")])
    assert points(late, "A4") == POINTS["A4_each"]
    assert late.reasons[0] == "filed accounts late once in the last 3 years"
    on_time = rate(filings=[_accounts_filing("2025-03-31", "2025-12-30")])
    assert "A4" not in codes(on_time)
    many = rate(
        filings=[
            _accounts_filing("2025-03-31", "2026-01-15"),
            _accounts_filing("2024-03-31", "2025-01-15"),
            _accounts_filing("2023-12-31", "2024-11-15"),
            _accounts_filing("2023-03-31", "2024-02-15"),
            _accounts_filing("2019-03-31", "2020-06-15"),  # beyond three years
        ]
    )
    assert points(many, "A4") == POINTS["A4_max"]
    assert many.reasons[0] == "filed accounts late 4 times in the last 3 years"
    plc = rate(
        _profile(type="plc"), filings=[_accounts_filing("2025-03-31", "2025-11-15")]
    )
    assert points(plc, "A4") == POINTS["A4_each"]
    first = rate(
        _profile(date_of_creation="2024-06-01"),
        filings=[_accounts_filing("2025-06-30", "2026-02-20")],
    )
    assert "A4" not in codes(first)
    amended = rate(
        filings=[
            _filing(
                "accounts-amended-with-accounts-type-full",
                "2026-08-01",
                category="accounts",
                values={"made_up_date": "2024-03-31"},
            )
        ]
    )
    assert "A4" not in codes(amended)
    skipped = rate(
        filings=[
            _filing(
                "accounts-with-accounts-type-micro-entity",
                "2026-01-15",
                category="accounts",
            ),
            _filing(
                "accounts-with-accounts-type-micro-entity",
                "2026-01-15",
                category="accounts",
                values={"made_up_date": "nonsense"},
            ),
        ]
    )
    assert "A4" not in codes(skipped)


def test_no_accounts_yet_dormant_and_disclosure() -> None:
    """A mature company with no accounts, a dormant filer, and the info flags."""
    none_filed = rate(_profile(accounts={}, date_of_creation="2024-01-01"))
    assert points(none_filed, "A5") == POINTS["A5"]
    assert "no accounts filed yet" in none_filed.reasons
    assert "accounts_disclosure" not in none_filed.info
    young = rate(_profile(accounts={}, date_of_creation="2026-01-01"))
    assert "A5" not in codes(young)
    dormant = rate(_profile(accounts={"last_accounts": {"type": "dormant"}}))
    assert dormant.reasons == ["files dormant accounts (declares it is not trading)"]
    full = rate(_profile(accounts={"last_accounts": {"type": "full"}}))
    assert "accounts_disclosure" not in full.info


def test_nothing_filed_for_a_long_time() -> None:
    """A long silence at a mature company is a small signal, in years when it is years."""
    quiet = rate(state=CompanyState(newest_filing_date=date(2023, 1, 1)))
    assert points(quiet, "A8") == POINTS["A8"]
    assert quiet.reasons == ["nothing filed for 3 years"]
    months = rate(state=CompanyState(newest_filing_date=date(2025, 5, 1)))
    assert months.reasons == ["nothing filed for 16 months"]
    young = rate(
        _profile(date_of_creation="2025-01-01"),
        state=CompanyState(newest_filing_date=date(2025, 1, 2)),
    )
    assert "A8" not in codes(young)


def test_due_soon_is_information_not_points() -> None:
    """Deadlines inside 30 days are noted but do not score."""
    result = rate(
        _profile(
            accounts=_accounts(due_on="2026-09-30"),
            confirmation_statement={"next_due": "2026-10-10"},
        )
    )
    assert result.info["due_soon"] == ["accounts", "confirmation_statement"]
    assert result.score == 0


# ---------------------------------------------------------------- B board


@pytest.mark.parametrize(
    ("created", "code", "words"),
    [
        ("2026-06-20", "B1", "incorporated 2 months ago"),
        ("2026-09-01", "B1", "incorporated 1 month ago"),
        ("2024-01-01", "B1", "only 2 years old"),
        ("2025-08-01", "B1", "only 1 year old"),
        ("2020-01-01", "B1", "6 years old"),
        ("2010-01-01", None, None),
    ],
)
def test_company_age_bands(created: str, code: str | None, words: str | None) -> None:
    """Younger companies fail more often; the middle band only shows when amber."""
    profile = _profile(date_of_creation=created)
    result = rate(profile)
    if code is None:
        assert "B1" not in codes(result)
        return
    assert "B1" in codes(result)
    if points(result, "B1") == POINTS["B1_3_9"]:
        assert result.band == BAND_GREEN
        assert result.reasons == []  # quiet while green
        amber = rate(profile, officers=None, coverage={"profile": NOW})
        assert words in amber.reasons
    else:
        assert result.reasons[0] == words
    assert "B1" not in codes(rate(_profile(date_of_creation=None)))


def test_board_size_and_corporate_sole_director() -> None:
    """None, one, and one that is itself a company."""
    empty = rate(officers=_officers(_officer(resigned="2020-01-01")))
    assert points(empty, "B2") == POINTS["B2_none"]
    assert "no directors in office" in empty.reasons
    sole = rate(officers=_officers(_officer()))
    assert sole.reasons == ["sole director"]
    corporate = rate(
        officers=_officers(_officer("ACME LTD", role="corporate-director"))
    )
    assert corporate.reasons == ["the sole director is a company"]
    secretary_only = rate(officers=_officers(_officer(role="secretary")))
    assert points(secretary_only, "B2") == POINTS["B2_none"]


def test_llp_members_are_the_board() -> None:
    """For an LLP the members count, and the words say so."""
    llp = _profile(type="llp")
    members = rate(
        llp,
        officers=_officers(
            _officer(role="llp-designated-member"),
            _officer("B, Ann", role="llp-member", officer_id="ann"),
        ),
    )
    assert "B2" not in codes(members)
    none = rate(llp, officers=_officers(_officer(role="director")))
    assert "no members in office" in none.reasons
    sole = rate(llp, officers=_officers(_officer(role="llp-member")))
    assert "sole member" in sole.reasons


def test_resignations_shrinkage_and_a_whole_new_board() -> None:
    """Churn rules: counts, half the board, net shrinkage, everyone new."""
    one = rate(
        officers=_officers(
            _officer(), _officer("A, B", resigned="2026-05-01", officer_id="b")
        )
    )
    assert points(one, "B3") == POINTS["B3_1"]
    two = rate(
        officers=_officers(
            _officer(),
            _officer("K, L", officer_id="l"),
            _officer("M, N", officer_id="n"),
            _officer("A, B", resigned="2026-05-01", officer_id="b"),
            _officer("C, D", resigned="2026-06-01", officer_id="d"),
        )
    )
    assert points(two, "B3") == POINTS["B3_2"]  # two of five: not half
    assert "2 directors resigned in the last year" in two.reasons
    assert points(two, "B4") == POINTS["B4"]
    assert "the board shrank by 2 in the last year" in two.reasons
    three = rate(
        officers=_officers(
            _officer(),
            _officer("A, B", resigned="2026-05-01", officer_id="b"),
            _officer("C, D", resigned="2026-06-01", officer_id="d"),
            _officer("E, F", resigned="2026-07-01", officer_id="f"),
            _officer("G, H", appointed="2026-07-02", officer_id="h"),
            _officer("I, J", appointed="2026-07-02", officer_id="j"),
        )
    )
    assert points(three, "B3") == POINTS["B3_3"]
    assert "B4" not in codes(three)  # three out, two in
    half = rate(
        officers=_officers(
            _officer(),
            _officer("A, B", officer_id="b"),
            _officer("C, D", resigned="2026-05-01", officer_id="d"),
            _officer("E, F", resigned="2026-06-01", officer_id="f"),
        )
    )
    assert points(half, "B3") == POINTS["B3_3"]  # two of four gone
    old_news = rate(
        officers=_officers(
            _officer(), _officer("A, B", resigned="2020-05-01", officer_id="b")
        )
    )
    assert "B3" not in codes(old_news)
    new_board = rate(
        officers=_officers(
            _officer(appointed="2026-03-01"),
            _officer("A, B", appointed="2026-03-01", officer_id="b"),
            _officer(
                "OLD, Guard",
                appointed="2015-01-01",
                resigned="2026-03-01",
                officer_id="g",
            ),
        )
    )
    assert points(new_board, "B5") == POINTS["B5"]
    assert "every director changed in the last year" in new_board.reasons
    startup = rate(
        _profile(date_of_creation="2026-03-01"),
        officers=_officers(_officer(appointed="2026-03-01")),
    )
    assert "B5" not in codes(startup)
    undated = rate(
        officers=_officers(_officer(appointed=None), _officer("A, B", officer_id="b"))
    )
    assert "B5" not in codes(undated)


def test_ownership_register() -> None:
    """Unidentified owners, an empty register, a harmless statement."""
    unidentified = rate(psc=_psc([], [{"statement": "psc-exists-but-not-identified"}]))
    assert unidentified.reasons == ["owner not identified or not confirmed"]
    assert points(unidentified, "B6") == POINTS["B6_unidentified"]
    empty = rate(psc=_psc([]))
    assert empty.reasons == ["no person with significant control recorded"]
    harmless = rate(
        psc=_psc([], [{"statement": "no-individual-or-entity-with-signficant-control"}])
    )
    assert "B6" not in codes(harmless)
    withdrawn = rate(
        psc=_psc(
            [],
            [{"statement": "psc-exists-but-not-identified", "ceased_on": "2020-01-01"}],
        )
    )
    assert points(withdrawn, "B6") == POINTS["B6_none"]


def test_ownership_changes() -> None:
    """A change of control this year, and everyone ceased."""
    changed = rate(
        psc=_psc(
            [
                {
                    "name": "Old Owner",
                    "notified_on": "2016-04-06",
                    "ceased_on": "2026-03-01",
                },
                {"name": "New Owner", "notified_on": "2026-03-01"},
            ]
        )
    )
    assert changed.reasons == ["ownership changed on 1 Mar 2026"]
    assert points(changed, "B7") == POINTS["B7"]
    young = rate(
        _profile(date_of_creation="2025-06-01"),
        psc=_psc([{"name": "Founder", "notified_on": "2025-06-01"}]),
    )
    assert "B7" not in codes(young)
    gone = rate(
        psc=_psc(
            [
                {
                    "name": "Old Owner",
                    "notified_on": "2016-04-06",
                    "ceased_on": "2026-03-01",
                }
            ]
        )
    )
    assert "every person with significant control has ceased" in gone.reasons
    assert points(gone, "B7") == POINTS["B7_all_ceased"]
    assert "B6" not in codes(gone)


def _appointment(number: str, status: str, resigned: str | None = None) -> Appointment:
    return Appointment(
        company_number=number,
        company_name=f"{number} LTD",
        company_status=status,
        officer_role="director",
        resigned_on=date.fromisoformat(resigned) if resigned else None,
    )


def test_connected_parties_through_followed_people() -> None:
    """Directors still serving at failed companies, and serial dissolutions."""
    jane = TrackedPerson(
        name="Jane Smith",
        officer_ids=["officer-jane"],
        appointments=AppointmentList(
            officer_id="officer-jane",
            name="SMITH, Jane",
            items=[
                _appointment("12345678", "active"),  # this company: ignored
                _appointment("11111111", "liquidation"),
                _appointment("22222222", "administration"),
                _appointment("33333333", "liquidation"),  # beyond the cap
                _appointment("44444444", "liquidation", resigned="2020-01-01"),
                _appointment("55555555", "dissolved"),
                _appointment("66666666", "dissolved"),
                _appointment("77777777", "dissolved"),
            ],
        ),
    )
    result = rate(people=[jane])
    assert points(result, "B8") == POINTS["B8_max"] + POINTS["B8_dissolved"]
    assert (
        "Jane Smith is a director of 11111111 LTD, now in liquidation" in result.reasons
    )
    assert "Jane Smith is a director of 22222222 LTD, now in administration" in (
        result.reasons
    )
    assert "Jane Smith has been a director of 3 dissolved companies" in result.reasons
    assert result.band == BAND_AMBER
    no_data = TrackedPerson(name="Jane Smith", officer_ids=["officer-jane"])
    assert "B8" not in codes(rate(people=[no_data]))


# ---------------------------------------------------------------- C security


def test_charges() -> None:
    """Outstanding counts, recent registrations, HMRC and an all-assets debenture."""
    three = rate(charges=_charges(_charge(), _charge(), _charge()))
    assert points(three, "C1") == POINTS["C1_3_5"]
    assert "3 outstanding charges" in three.reasons
    six = rate(charges=_charges(*[_charge() for _ in range(6)]))
    assert points(six, "C1") == POINTS["C1_6_"]
    satisfied = rate(charges=_charges(_charge("fully-satisfied")))
    assert "C1" not in codes(satisfied)
    recent = rate(
        charges=_charges(_charge(created="2026-08-01", lender="Lloyds Bank plc"))
    )
    assert (
        "new charge registered on 1 Aug 2026 in favour of Lloyds Bank plc"
        in recent.reasons
    )
    assert points(recent, "C2") == POINTS["C2_recent"]
    nameless = rate(
        charges=_charges({"status": "outstanding", "created_on": "2026-08-01"})
    )
    assert (
        "new charge registered on 1 Aug 2026 in favour of a lender" in nameless.reasons
    )
    several = rate(
        charges=_charges(_charge(created="2026-08-01"), _charge(created="2025-11-01"))
    )
    assert points(several, "C2") == POINTS["C2_several"]
    assert "2 new charges registered in the last year" in several.reasons
    older = rate(charges=_charges(_charge(created="2026-01-01")))
    assert "C2" not in codes(older)
    hmrc = rate(charges=_charges(_charge(lender="HM Revenue & Customs")))
    assert "a charge in favour of HMRC is outstanding" in hmrc.reasons
    assert points(hmrc, "C3") == POINTS["C3"]
    paid_hmrc = rate(charges=_charges(_charge("satisfied", lender="HMRC")))
    assert "C3" not in codes(paid_hmrc)


@dataclass(frozen=True, kw_only=True)
class _ChargeWithParticulars(Charge):
    """A charge as the charges feature extends it."""

    floating_charge_covers_all: bool | None = None


def test_all_assets_debenture_scores_once_the_model_carries_it() -> None:
    """The floating-charge flag is read when present and ignored when absent."""
    debenture = ChargeList(
        items=[
            _ChargeWithParticulars(
                status="outstanding", floating_charge_covers_all=True
            ),
            _ChargeWithParticulars(status="satisfied", floating_charge_covers_all=True),
        ]
    )
    result = rate(charges=debenture)
    assert points(result, "C4") == POINTS["C4"]
    assert "an all-assets debenture is outstanding (trade creditors rank behind)" in (
        result.reasons
    )
    plain = rate(charges=_charges(_charge()))
    assert "C4" not in codes(plain)


def test_insolvency_history() -> None:
    """The register's flag, dated from the cases when there are any."""
    flag_only = rate(_profile(has_insolvency_history=True))
    assert flag_only.reasons == ["has insolvency history"]
    assert points(flag_only, "C5") == POINTS["C5"]
    recent = rate(
        _profile(has_insolvency_history=True),
        insolvency=_insolvency(
            _case(
                "corporate-voluntary-arrangement",
                voluntary_arrangement_started_on="2022-01-01",
                voluntary_arrangement_ended_on="2024-01-01",
            )
        ),
    )
    assert recent.reasons == [
        "has insolvency history (corporate voluntary arrangement (cva), ended 2024)"
    ]
    assert points(recent, "C5") == POINTS["C5"]
    undated_end = rate(
        _profile(has_insolvency_history=True),
        insolvency=_insolvency({"number": "1", "type": "moratorium", "dates": []}),
    )
    assert undated_end.reasons == ["has insolvency history (moratorium)"]
    started = rate(
        _profile(has_insolvency_history=True),
        insolvency=_insolvency(
            _case(
                "compulsory-liquidation",
                petitioned_on="2015-01-01",
                dissolved_on="2016-01-01",
            ),
            {
                "number": "2",
                "type": "receiver-manager",
                "dates": [{"type": "unknown-on", "date": "2018-01-01"}],
            },
        ),
    )
    assert started.reasons == [
        "has insolvency history (compulsory liquidation, ended 2016)"
    ]
    assert points(started, "C5") == POINTS["C5_old"]


def test_registered_office_problems_and_moves() -> None:
    """Undeliverable post, a dispute, and a restless registered office."""
    result = rate(
        _profile(
            undeliverable_registered_office_address=True,
            registered_office_is_in_dispute=True,
        )
    )
    assert result.reasons == [
        "post to the registered office is undeliverable",
        "the registered office address is in dispute",
    ]
    assert result.score == POINTS["C6"] + POINTS["C7"]
    logged = CompanyState(
        changes=[
            {
                "at": "2026-08-01T10:00:00+00:00",
                "kind": "profile",
                "event_type": "address-changed",
            },
            {
                "at": "2026-05-01T10:00:00+00:00",
                "kind": "profile",
                "event_type": "address-changed",
            },
            {"at": "nonsense", "kind": "profile", "event_type": "address-changed"},
            {
                "at": "2026-06-01T10:00:00+00:00",
                "kind": "profile",
                "event_type": "name-changed",
            },
            {
                "at": "2024-06-01T10:00:00+00:00",
                "kind": "profile",
                "event_type": "address-changed",
            },
        ]
    )
    moves = rate(state=logged)
    assert moves.reasons == ["registered office changed 2 times in the last year"]
    filed = rate(
        filings=[
            _filing(
                "change-registered-office-address-company-with-date-old-address-new-address",
                "2026-08-01",
                category="address",
            ),
            _filing(
                "change-registered-office-address-company-with-date-old-address-new-address",
                "2026-07-01",
                category="address",
            ),
            _filing(
                "change-registered-office-address-company-with-date-old-address-new-address",
                "2026-06-01",
                category="address",
            ),
        ]
    )
    assert filed.reasons == ["registered office changed 3 times in the last year"]
    once = rate(
        filings=[
            _filing(
                "change-registered-office-address-company-with-date-old-address-new-address",
                "2026-08-01",
                category="address",
            )
        ]
    )
    assert "C8" not in codes(once)


def test_renames_and_renamed_after_change_of_control() -> None:
    """Two renames in two years, and a rename on the heels of new owners."""
    renamed = rate(
        _profile(
            previous_company_names=[
                {"name": "ONE LTD", "ceased_on": "2025-01-01"},
                {"name": "TWO LTD", "ceased_on": "2026-08-01"},
                {"name": "ANCIENT LTD", "ceased_on": "2015-01-01"},
            ]
        )
    )
    assert renamed.reasons == ["renamed 2 times in the last two years"]
    phoenix = rate(
        _profile(
            previous_company_names=[{"name": "OLD LTD", "ceased_on": "2026-08-01"}]
        ),
        psc=_psc([{"name": "New Owner", "notified_on": "2026-07-01"}]),
    )
    assert "renamed after a change of control" in phoenix.reasons
    assert "C9" not in codes(phoenix)
    unrelated = rate(
        _profile(
            previous_company_names=[{"name": "OLD LTD", "ceased_on": "2025-01-01"}]
        ),
        psc=_psc([{"name": "New Owner", "notified_on": "2026-07-01"}]),
    )
    assert "C10" not in codes(unrelated)


def test_cannot_file_is_quiet_information() -> None:
    """A company that cannot file is noted but rarely amber for it alone."""
    result = rate(_profile(can_file=False))
    assert result.info["cannot_file"] is True
    assert result.band == BAND_GREEN
    assert result.reasons == []
    amber = rate(_profile(can_file=False, undeliverable_registered_office_address=True))
    assert "the company cannot file online" in amber.reasons
    assert "C11" not in codes(rate(_profile(can_file=False, company_status="open")))


# ---------------------------------------------------------------- E accounts


def test_net_liabilities_and_creditors_beyond_cash() -> None:
    """A balance sheet under water is the heaviest accounts line; creditors add to it."""
    history = _history(
        _year(
            "2025-12-31",
            net_assets=_figure(-1026, -2865),
            cash=_figure(13552, 8398),
            creditors_within_one_year=_figure(186298, 184704),
            employees=_figure(0, 0),
        )
    )
    result = rate(accounts=history)
    assert result.band == BAND_AMBER
    assert result.score == POINTS["E1"] + POINTS["E4"]
    assert codes(result) == ["E1", "E4"]
    assert result.reasons == [
        "net liabilities of £1k at 31 Dec 2025",
        "creditors due within a year (£186k) exceed cash (£13.6k) at 31 Dec 2025",
    ]
    assert result.reason == (
        "Amber: net liabilities of £1k at 31 Dec 2025; creditors due within a "
        "year (£186k) exceed cash (£13.6k) at 31 Dec 2025"
    )
    assert result.info["accounts_figures_at"] == "2025-12-31"
    assert result.as_dict()["scoring_version"] == "2"
    # Creditors only count against cash when both figures are there.
    no_cash = _history(
        _year("2025-12-31", creditors_within_one_year=_figure(186298, 184704))
    )
    assert codes(rate(accounts=no_cash)) == []
    covered = _history(
        _year(
            "2025-12-31",
            cash=_figure(200000),
            creditors_within_one_year=_figure(186298),
        )
    )
    assert codes(rate(accounts=covered)) == []
    # Net liabilities that were worse last year still count (the sign is the point).
    assert codes(rate(accounts=_history(_year("2025-12-31", net_assets=_figure(-1)))))


def test_falling_net_assets_cash_and_headcount() -> None:
    """Falls of more than a quarter, a half and a half score; smaller ones do not."""
    history = _history(
        _year(
            "2025-12-31",
            net_assets=_figure(700_000, 1_200_000),
            cash=_figure(40_000, 100_000),
            employees=_figure(4, 10),
        )
    )
    result = rate(accounts=history)
    assert codes(result) == ["E2", "E3", "E5"]
    assert result.score == POINTS["E2"] + POINTS["E3"] + POINTS["E5"]
    assert result.reasons == [
        "net assets down 41.7 % year on year (£700k at 31 Dec 2025)",
        "cash down 60 % year on year (£40k at 31 Dec 2025)",
        "headcount halved (10 to 4) in the year to 31 Dec 2025",
    ]
    assert result.band == BAND_AMBER
    # Exactly a quarter and exactly a half are not "more than".
    edge = _history(
        _year(
            "2025-12-31",
            net_assets=_figure(75, 100),
            cash=_figure(50, 100),
            employees=_figure(5, 10),
        )
    )
    assert codes(rate(accounts=edge)) == ["E5"]
    # A rise, or a fall from nothing, never scores; nor does one person going.
    fine = _history(
        _year(
            "2025-12-31",
            net_assets=_figure(120, 100),
            cash=_figure(10, 0),
            employees=_figure(0, 1),
        )
    )
    assert codes(rate(accounts=fine)) == []
    # Falling from positive to negative is both a fall and net liabilities.
    sunk = _history(_year("2025-12-31", net_assets=_figure(-100, 1000)))
    assert codes(rate(accounts=sunk)) == ["E1", "E2"]
    assert rate(accounts=sunk).reasons[1] == (
        "net assets down 110 % year on year (-£100 at 31 Dec 2025)"
    )


def test_prior_figures_come_from_the_previous_year_when_needed() -> None:
    """Without a comparative column the year before's own accounts stand in."""
    latest = _year("2025-12-31", cash=_figure(40_000), net_assets=_figure(50))
    before = _year("2024-12-31", cash=_figure(100_000), net_assets=_figure(40))
    result = rate(accounts=_history(latest, before))
    assert codes(result) == ["E3"]
    # A borrowed comparative column counts as the year before too.
    borrowed = _year(
        "2024-12-31", status="no_ixbrl", source="comparative", cash=_figure(100_000)
    )
    assert codes(rate(accounts=_history(latest, borrowed))) == ["E3"]
    # Not when the previous accounts are too far back to be last year's.
    long_ago = _year("2023-06-30", cash=_figure(100_000))
    assert codes(rate(accounts=_history(latest, long_ago))) == []
    # Nor when the previous year is unread, or missing altogether.
    unread = _year("2024-12-31", status="pending", source="none")
    assert codes(rate(accounts=_history(latest, unread))) == []
    assert codes(rate(accounts=_history(latest))) == []
    # The newest year read is scored, not a newer one still pending.
    pending = _year("2026-12-31", status="pending", source="none")
    result = rate(accounts=_history(pending, latest, before))
    assert codes(result) == ["E3"]
    assert result.info["accounts_figures_at"] == "2025-12-31"


def test_accounts_figures_are_not_scored_for_a_dead_company() -> None:
    """A dissolved company is red for that alone; its old figures are not listed."""
    history = _history(_year("2025-12-31", net_assets=_figure(-1000)))
    result = rate(_profile(company_status="dissolved"), accounts=history)
    assert codes(result) == ["R1"]
    assert "accounts_figures_at" not in result.info


# ---------------------------------------------------------------- D uncertainty


def test_stale_register_data_and_the_age_caveat() -> None:
    """Old data costs points past two weeks; older than a day it is always mentioned.

    The age is said once: as the scored reason when it costs points, as the
    trailing caveat otherwise.
    """
    stale = rate(coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=20)})
    assert stale.data_age_days == 20
    assert stale.reasons == ["register data is 20 days old"]
    assert stale.reason == "Amber: register data is 20 days old"
    assert stale.band == BAND_AMBER
    fresh = rate(coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=3)})
    assert fresh.reasons == ["register data 3 days old"]
    assert fresh.band == BAND_GREEN
    # The caveat never stands in for the reason a company is green.
    assert (
        fresh.reason == "Green: no concerns on the register; register data 3 days old"
    )
    charged = rate(
        charges=_charges(_charge()),
        coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=3)},
    )
    assert charged.reasons == ["1 outstanding charge", "register data 3 days old"]
    assert charged.reason == "Green: 1 outstanding charge; register data 3 days old"
    unknown_age = rate(coverage={**FULL_COVERAGE, "profile": None})
    assert unknown_age.data_age_days is None
    assert unknown_age.coverage["profile"] is None
    assert unknown_age.reason == "Green: no concerns on the register"


def test_refreshes_failing_for_over_a_week_count_as_stale() -> None:
    """A recent copy that will not refresh is stale after seven days of failures."""
    failing = rate(
        coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=10)},
        profile_failing_since=NOW - timedelta(days=8),
    )
    assert failing.band == BAND_AMBER
    assert codes(failing) == ["D2"]
    assert failing.reasons == [
        "register data is 10 days old and has not refreshed for 8 days"
    ]
    assert failing.info["refresh_failing_days"] == 8
    # A week of failures is tolerated; the caveat still says the age.
    tolerated = rate(
        coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=10)},
        profile_failing_since=NOW - timedelta(days=7),
    )
    assert tolerated.band == BAND_GREEN
    assert tolerated.reasons == ["register data 10 days old"]
    assert tolerated.info["refresh_failing_days"] == 7
    # Past two weeks the age alone is the reason, said once.
    old_and_failing = rate(
        coverage={**FULL_COVERAGE, "profile": NOW - timedelta(days=20)},
        profile_failing_since=NOW - timedelta(days=9),
    )
    assert old_and_failing.reasons == ["register data is 20 days old"]
    no_age = rate(
        coverage={**FULL_COVERAGE, "profile": None},
        profile_failing_since=NOW - timedelta(days=9),
    )
    assert no_age.reasons == ["register data has not refreshed for 9 days"]
    assert no_age.band == BAND_AMBER


def test_partial_data_is_uncertainty() -> None:
    """The register saying it holds partial data holds the band at amber."""
    result = rate(
        _profile(
            partial_data_available="full-data-available-from-financial-conduct-authority"
        )
    )
    assert result.reasons == ["the register holds only partial data"]
    assert result.band == BAND_AMBER


# ---------------------------------------------------------------- helpers


def test_add_months_clamps_to_the_end_of_the_month() -> None:
    """Month arithmetic never invents a day."""
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 3, 31), 9) == date(2026, 12, 31)
    assert add_months(date(2026, 5, 31), 9) == date(2027, 2, 28)
    assert add_months(date(2026, 11, 30), 21) == date(2028, 8, 30)


def test_band_worsened() -> None:
    """Only a move up the scale counts, and unknown is neutral."""
    assert band_worsened(BAND_GREEN, BAND_AMBER)
    assert band_worsened(BAND_AMBER, BAND_RED)
    assert not band_worsened(BAND_RED, BAND_AMBER)
    assert not band_worsened(None, BAND_RED)
    assert not band_worsened(BAND_GREEN, None)
    assert not band_worsened(BAND_AMBER, BAND_AMBER)


def test_as_dict_and_items() -> None:
    """The plain-data form carries everything the sensor and the tools need."""
    result = rate(charges=_charges(_charge()))
    data = result.as_dict()
    assert data["band"] == BAND_GREEN
    assert data["computed_at"] is None
    assert data["basis"].startswith("Companies House data only")
    assert data["scoring_version"] == SCORING_VERSION
    assert set(data) == {
        "band",
        "score",
        "reason",
        "reasons",
        "overrides",
        "coverage",
        "data_age_days",
        "computed_at",
        "scoring_version",
        "basis",
        "info",
    }
    (item,) = result.items
    assert (item.code, item.section, item.points, item.quiet) == ("C1", "C", 2, False)


def test_case_without_a_type_and_several_followed_people() -> None:
    """An untyped case still reads, and each followed person is scored in turn."""
    untyped = rate(
        _profile(company_status="liquidation"),
        insolvency=_insolvency(
            {"number": "1", "dates": [{"type": "wound-up-on", "date": "2026-07-01"}]}
        ),
    )
    assert untyped.reasons[0] == "in insolvency since 1 Jul 2026"
    jane = TrackedPerson(
        name="Jane Smith",
        officer_ids=["officer-jane"],
        appointments=AppointmentList(
            officer_id="officer-jane",
            name="SMITH, Jane",
            items=[_appointment("11111111", "liquidation")],
        ),
    )
    priya = TrackedPerson(
        name="Priya Patel",
        officer_ids=["officer-priya"],
        appointments=AppointmentList(
            officer_id="officer-priya",
            name="PATEL, Priya",
            items=[_appointment("22222222", "administration")],
        ),
    )
    result = rate(people=[jane, priya])
    assert points(result, "B8") == 2 * POINTS["B8_each"]
    assert [r.split(" is ")[0] for r in result.reasons] == ["Jane Smith", "Priya Patel"]
