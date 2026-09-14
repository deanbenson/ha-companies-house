"""Tests for the iXBRL accounts reader, on real filings and hand-built edge cases."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from custom_components.companies_house.ixbrl import (
    MAX_BYTES,
    METRICS,
    Figure,
    IxbrlError,
    looks_like_ixbrl,
    parse_accounts,
    parse_number,
)

IXBRL = Path(__file__).parent / "fixtures" / "ixbrl"


def _read(name: str) -> bytes:
    return (IXBRL / f"{name}.html").read_bytes()


def _values(parsed, metric: str) -> tuple[Decimal | None, Decimal | None, str]:
    figure = parsed.figures[metric]
    return figure.value, figure.prior, figure.status


# ---------------------------------------------------------------- real filings


def test_full_frs102_accounts_carry_every_figure() -> None:
    """A small company's full accounts disclose turnover, profit and cash."""
    parsed = parse_accounts(_read("full_frs102"))
    assert parsed.period_start == date(2025, 1, 1)
    assert parsed.period_end == date(2025, 12, 31)
    assert parsed.prior_period_end == date(2024, 12, 31)
    assert not parsed.dormant
    assert parsed.accounting_standard == "SmallEntities"
    assert parsed.accounts_type_member == "FullAccounts"
    assert parsed.company_number == "14686215"
    assert parsed.company_name == "GG-559-821 Limited"
    assert _values(parsed, "turnover") == (Decimal(13784), Decimal(16600), "ok")
    assert _values(parsed, "profit_before_tax") == (Decimal(2643), Decimal(5431), "ok")
    assert _values(parsed, "profit_after_tax") == (Decimal(1839), Decimal(5431), "ok")
    assert _values(parsed, "cash") == (Decimal(13552), Decimal(8398), "ok")
    assert _values(parsed, "net_assets") == (Decimal(-1026), Decimal(-2865), "ok")
    assert _values(parsed, "creditors_within_one_year")[2] == "ok"
    assert _values(parsed, "debtors") == (Decimal(1013), Decimal(789), "ok")
    assert _values(parsed, "employees") == (Decimal(0), Decimal(0), "ok")
    assert parsed.figures["turnover"].concept == "TurnoverRevenue"
    assert set(parsed.figures) == set(METRICS)


def test_micro_entity_accounts_use_equity_and_hidden_employees() -> None:
    """Micro accounts omit the profit and loss; net assets come from Equity, staff from ix:hidden."""
    parsed = parse_accounts(_read("micro_hidden_employees"))
    assert parsed.accounting_standard == "Micro-entities"
    assert parsed.company_name == "Connect-All Properties Limited"
    assert _values(parsed, "net_assets") == (Decimal(100), Decimal(100), "ok")
    assert parsed.figures["net_assets"].concept == "Equity"
    assert _values(parsed, "employees") == (Decimal(0), Decimal(0), "ok")
    for metric in ("turnover", "profit_before_tax", "profit_after_tax", "cash"):
        assert _values(parsed, metric) == (None, None, "not_disclosed"), metric


def test_small_filleted_accounts_have_a_balance_sheet_only() -> None:
    """Filleted accounts keep cash and net assets and drop the rest."""
    parsed = parse_accounts(_read("small_filleted"))
    assert parsed.accounts_type_member == "FilletedAccounts"
    assert parsed.period_start == date(2025, 3, 1)
    assert parsed.period_end == date(2026, 2, 28)
    assert parsed.prior_period_end == date(2025, 2, 28)
    assert _values(parsed, "cash") == (Decimal(11), Decimal(11), "ok")
    assert _values(parsed, "net_assets") == (Decimal(11), Decimal(11), "ok")
    assert _values(parsed, "turnover") == (None, None, "not_disclosed")


def test_dormant_aa02_has_an_instant_end_date_and_no_prior_year() -> None:
    """Dormant accounts tag the year end as an instant and nothing over the year."""
    parsed = parse_accounts(_read("dormant_aa02"))
    assert parsed.dormant
    assert parsed.period_start == date(2025, 7, 1)
    assert parsed.period_end == date(2026, 6, 30)
    # No duration for last year, but the balance sheet has a comparative.
    assert parsed.prior_period_end == date(2025, 6, 30)
    assert _values(parsed, "net_assets") == (Decimal(1), Decimal(1), "ok")
    assert _values(parsed, "employees") == (Decimal(0), None, "ok")


def test_2008_inline_xbrl_namespace_is_read() -> None:
    """The older ix namespace is matched by local name just the same."""
    data = _read("ns2008_micro_turnover")
    assert b"2008/inlineXBRL" in data
    parsed = parse_accounts(data)
    assert _values(parsed, "turnover") == (Decimal(67074), Decimal(57676), "ok")
    assert _values(parsed, "creditors_within_one_year") == (
        Decimal(42448),
        Decimal(47864),
        "ok",
    )
    assert _values(parsed, "employees") == (Decimal(1), None, "ok")
    assert _values(parsed, "net_assets") == (None, None, "not_disclosed")


def test_charity_accounts_use_charity_funds_as_net_assets() -> None:
    """A charity's total funds stand in for net assets."""
    parsed = parse_accounts(_read("charity"))
    assert parsed.company_name == "THE SPORTING AMBITION FOUNDATION"
    assert _values(parsed, "net_assets") == (Decimal(33), Decimal(696), "ok")
    assert parsed.figures["net_assets"].concept == "CharityFunds"
    assert _values(parsed, "employees") == (Decimal("0.00"), Decimal("0.00"), "ok")


def test_creditors_prefer_the_within_one_year_dimension() -> None:
    """When creditors are tagged several ways the one-year maturity wins, without a conflict."""
    parsed = parse_accounts(_read("full_net_liabilities"))
    assert parsed.company_number == "12468167"
    assert _values(parsed, "creditors_within_one_year") == (
        Decimal(168320),
        Decimal(545255),
        "ok",
    )
    assert _values(parsed, "net_assets") == (Decimal(-48576), Decimal(-43086), "ok")
    assert _values(parsed, "cash") == (Decimal(66637), Decimal(18992), "ok")
    # Tagged "2" with scale="-2", a common filer slip: two people, not 0.02.
    assert _values(parsed, "employees") == (Decimal(2), Decimal(2), "ok")


def test_doctype_and_first_year_accounts() -> None:
    """A DOCTYPE parses, and a first year has no prior figures at all."""
    data = _read("micro_doctype")
    assert b"<!DOCTYPE" in data
    parsed = parse_accounts(data)
    assert parsed.period_start == date(2024, 12, 9)
    assert parsed.period_end == date(2025, 12, 31)
    assert parsed.prior_period_end is None
    assert _values(parsed, "net_assets") == (Decimal(100), None, "ok")
    assert _values(parsed, "employees") == (Decimal(0), None, "ok")


@pytest.mark.parametrize("name", [p.stem for p in sorted(IXBRL.glob("*.html"))])
def test_every_fixture_looks_like_ixbrl(name: str) -> None:
    """The cheap sniff accepts every real filing."""
    assert looks_like_ixbrl(_read(name))


# ---------------------------------------------------------------- hand-built


def _doc(
    body: str,
    *,
    contexts: str = "",
    units: str = "",
    header: str = "",
    ix_ns: str = "http://www.xbrl.org/2013/inlineXBRL",
    prefix: str = "",
) -> bytes:
    default_contexts = """
      <xbrli:context id="cur"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="prev"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="cur_end"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="prev_end"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
      <xbrli:context id="cur_group"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier>
        <xbrli:segment><xbrldi:explicitMember dimension="bus:GroupCompanyDataDimension"> bus:Consolidated </xbrldi:explicitMember></xbrli:segment></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="cur_typed"><xbrli:entity><xbrli:identifier scheme="http://www.companieshouse.gov.uk/">11111111</xbrli:identifier>
        <xbrli:segment><xbrldi:typedMember dimension="core:SomeTypedDimension"><core:x>1</core:x></xbrldi:typedMember></xbrli:segment></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-12-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="broken"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity><xbrli:period></xbrli:period></xbrli:context>
      <xbrli:context><xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period></xbrli:context>
    """
    default_units = """
      <xbrli:unit id="GBP"><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unit>
      <xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
      <xbrli:unit id="rate"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:GBP</xbrli:measure></xbrli:unitNumerator>
        <xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>
      <xbrli:unit><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
    """
    return (
        f"""{prefix}<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="{ix_ns}"
      xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2010-04-20"
      xmlns:ixt2="http://www.xbrl.org/inlineXBRL/transformation/2011-07-31"
      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
      xmlns:core="http://xbrl.frc.org.uk/fr/2025-01-01/core" xmlns:bus="http://xbrl.frc.org.uk/cd/2025-01-01/business">
      <body>{body}
      <div style="display:none"><ix:header>{header}<ix:resources>{contexts or default_contexts}{units or default_units}</ix:resources></ix:header></div>
      </body></html>"""
    ).encode()


def _fact(
    concept: str,
    context: str,
    text: str,
    *,
    unit: str = "GBP",
    fmt: str | None = "ixt:numcommadot",
    scale: str | None = None,
    sign: str | None = None,
    extra: str = "",
) -> str:
    attrs = f'name="{concept}" contextRef="{context}" unitRef="{unit}"'
    if fmt:
        attrs += f' format="{fmt}"'
    if scale:
        attrs += f' scale="{scale}"'
    if sign:
        attrs += f' sign="{sign}"'
    return f"<ix:nonFraction {attrs} {extra}>{text}</ix:nonFraction>"


END_DATE = (
    '<ix:nonNumeric name="bus:EndDateForPeriodCoveredByReport" contextRef="cur" '
    'format="ixt:datelonguk">31 December 2025</ix:nonNumeric>'
)


def test_hand_built_document_covers_formats_scale_sign_and_dedupe() -> None:
    """Number formats, scale, sign, duplicates and foreign units behave as specified."""
    body = END_DATE + "".join(
        [
            _fact("core:TurnoverRevenue", "cur", "1.234,5", fmt="ixt2:numcommadecimal"),
            _fact("core:TurnoverRevenue", "prev", "1,000", scale="3"),
            # The same figure tagged twice with the same value is one fact.
            _fact("core:CashBankOnHand", "cur_end", "500"),
            _fact("core:CashBankOnHand", "cur_end", "500"),
            _fact("core:CashBankOnHand", "prev_end", "1 000", fmt="ixt:numspacedot"),
            # A loss shown in brackets outside the tag, negated by sign.
            _fact(
                "core:ProfitLossOnOrdinaryActivitiesBeforeTax", "cur", "42", sign="-"
            ),
            _fact(
                "core:ProfitLossOnOrdinaryActivitiesBeforeTax",
                "prev",
                "-",
                fmt="ixt:numdash",
            ),
            # Net assets only in dollars: a unit mismatch, not a value.
            _fact("core:NetAssetsLiabilities", "cur_end", "9", unit="USD"),
            # A nil fact is skipped.
            _fact("core:Creditors", "cur_end", "", extra='xsi:nil="true"'),
            _fact("core:Creditors", "prev_end", "77"),
            # Employees as a negative count and in a rate unit are both tolerated.
            _fact(
                "core:AverageNumberEmployeesDuringPeriod",
                "cur",
                "3",
                unit="pure",
                sign="-",
            ),
            _fact(
                "core:AverageNumberEmployeesDuringPeriod",
                "prev",
                "nil",
                unit="pure",
                fmt="ixt2:numwordsen",
            ),
            # Facts in contexts that do not exist, or typed dimensions, are ignored.
            _fact("core:TurnoverRevenue", "missing", "1"),
            _fact("core:TurnoverRevenue", "cur_typed", "2"),
            # Unparseable text is no value; an ix:exclude inside a fact is skipped.
            _fact("core:Debtors", "cur_end", "n/a"),
            (
                '<ix:nonFraction name="core:CurrentLiabilities" contextRef="cur_end" unitRef="GBP">'
                "<ix:exclude>ignored </ix:exclude><span>4</span>2</ix:nonFraction>"
            ),
            '<ix:nonNumeric name="bus:EntityDormantTruefalse" contextRef="cur" format="ixt2:booleanfalse">No</ix:nonNumeric>',
            '<ix:nonNumeric name="bus:UKCompaniesHouseRegisteredNumber" contextRef="cur">11111111</ix:nonNumeric>',
            '<ix:nonNumeric name="bus:EntityCurrentLegalName" contextRef="cur"> Test <b>Co</b> Ltd </ix:nonNumeric>',
        ]
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_start == date(2025, 1, 1)
    assert parsed.period_end == date(2025, 12, 31)
    assert parsed.prior_period_end == date(2024, 12, 31)
    assert parsed.company_number == "11111111"
    assert parsed.company_name == "Test Co Ltd"
    assert not parsed.dormant
    assert _values(parsed, "turnover") == (Decimal("1234.5"), Decimal(1_000_000), "ok")
    assert _values(parsed, "cash") == (Decimal(500), Decimal(1000), "ok")
    assert _values(parsed, "profit_before_tax") == (Decimal(-42), Decimal(0), "ok")
    assert _values(parsed, "net_assets") == (None, None, "unit_mismatch")
    assert _values(parsed, "creditors_within_one_year") == (
        Decimal(42),
        Decimal(77),
        "ok",
    )
    assert parsed.figures["creditors_within_one_year"].concept == "CurrentLiabilities"
    assert _values(parsed, "employees") == (Decimal(3), Decimal(0), "ok")
    assert _values(parsed, "debtors") == (None, None, "not_disclosed")


def test_head_counts_ignore_a_negative_scale_on_whole_numbers() -> None:
    """A head count tagged "2" with scale="-2" is two people; money keeps its scale."""
    body = END_DATE + "".join(
        [
            _fact(
                "core:AverageNumberEmployeesDuringPeriod",
                "cur",
                "2",
                unit="pure",
                fmt=None,
                scale="-2",
            ),
            # A genuine fraction keeps the scale it was tagged with.
            _fact(
                "core:AverageNumberEmployeesDuringPeriod",
                "prev",
                "250.0",
                unit="pure",
                fmt=None,
                scale="-2",
            ),
            # Sums of money are read exactly as tagged.
            _fact("core:CashBankOnHand", "cur_end", "150", scale="-2"),
        ]
    )
    parsed = parse_accounts(_doc(body))
    assert _values(parsed, "employees") == (Decimal(2), Decimal("2.500"), "ok")
    assert _values(parsed, "cash") == (Decimal("1.50"), None, "ok")


def test_conflicting_duplicates_keep_the_visible_value() -> None:
    """Two different values for one figure: the visible one wins and it is flagged."""
    body = END_DATE + "".join(
        [
            "<ix:hidden>"
            + _fact("core:NetAssetsLiabilities", "cur_end", "1")
            + "</ix:hidden>",
            _fact("core:NetAssetsLiabilities", "cur_end", "2"),
            _fact("core:NetAssetsLiabilities", "cur_end", "3"),
        ]
    )
    parsed = parse_accounts(_doc(body))
    assert _values(parsed, "net_assets") == (Decimal(2), None, "conflict")
    # With every copy hidden, the first in document order is kept.
    body = (
        END_DATE
        + "<ix:hidden>"
        + _fact("core:NetAssetsLiabilities", "cur_end", "5")
        + _fact("core:NetAssetsLiabilities", "cur_end", "6")
        + "</ix:hidden>"
    )
    parsed = parse_accounts(_doc(body))
    assert _values(parsed, "net_assets") == (Decimal(5), None, "conflict")


def test_group_accounts_prefer_the_consolidated_figure() -> None:
    """When a consolidated figure exists it is used over the company-only one."""
    body = END_DATE + "".join(
        [
            _fact("core:TurnoverRevenue", "cur", "10"),
            _fact("core:TurnoverRevenue", "cur_group", "25"),
            _fact("core:ProfitLoss", "cur_group", "4"),
        ]
    )
    parsed = parse_accounts(_doc(body))
    assert _values(parsed, "turnover") == (Decimal(25), None, "ok")
    assert _values(parsed, "profit_after_tax") == (Decimal(4), None, "ok")


def test_period_falls_back_to_the_latest_duration_and_balance_sheet_date() -> None:
    """Without an end date fact the latest duration is the year; a bad text start is ignored."""
    body = _fact("core:TurnoverRevenue", "prev", "1") + _fact(
        "core:TurnoverRevenue", "cur", "2"
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_end == date(2025, 12, 31)
    assert parsed.prior_period_end == date(2024, 12, 31)
    assert _values(parsed, "turnover") == (Decimal(2), Decimal(1), "ok")
    # Only instant facts, but the start date fact sits in a duration context.
    body = (
        END_DATE
        + '<ix:nonNumeric name="bus:StartDateForPeriodCoveredByReport" contextRef="cur">1 January 2025</ix:nonNumeric>'
        + _fact("core:NetAssetsLiabilities", "cur_end", "8")
        + _fact("core:NetAssetsLiabilities", "prev_end", "7")
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_start == date(2025, 1, 1)
    assert parsed.prior_period_end == date(2024, 12, 31)
    assert _values(parsed, "net_assets") == (Decimal(8), Decimal(7), "ok")
    # The same with the start written as an ISO date in an instant context.
    body = (
        END_DATE
        + '<ix:nonNumeric name="bus:StartDateForPeriodCoveredByReport" contextRef="cur_end">2025-02-01</ix:nonNumeric>'
        + _fact("core:NetAssetsLiabilities", "cur_end", "8")
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_start == date(2025, 2, 1)
    assert parsed.period_end == date(2025, 12, 31)
    # Only instants and a balance sheet date: the year has no known start, so no
    # duration figures, and the prior balance sheet is the earlier instant.
    body = (
        '<ix:nonNumeric name="bus:BalanceSheetDate" contextRef="cur_end">31 December 2025</ix:nonNumeric>'
        '<ix:nonNumeric name="bus:StartDateForPeriodCoveredByReport" contextRef="cur_end">1 January 2025</ix:nonNumeric>'
        + _fact("core:NetAssetsLiabilities", "cur_end", "8")
        + _fact("core:NetAssetsLiabilities", "prev_end", "7")
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_start is None
    assert parsed.period_end == date(2025, 12, 31)
    assert parsed.prior_period_end == date(2024, 12, 31)
    assert _values(parsed, "net_assets") == (Decimal(8), Decimal(7), "ok")
    assert _values(parsed, "turnover") == (None, None, "not_disclosed")
    # A first year with a start but nothing tagged before it has no comparative.
    body = (
        END_DATE
        + '<ix:nonNumeric name="bus:StartDateForPeriodCoveredByReport" contextRef="cur">1 January 2025</ix:nonNumeric>'
        + _fact("core:NetAssetsLiabilities", "cur_end", "8")
    )
    parsed = parse_accounts(_doc(body))
    assert parsed.period_start == date(2025, 1, 1)
    assert parsed.prior_period_end is None
    # An end date whose text is not a date and no facts at all: no period.
    body = '<ix:nonNumeric name="bus:EndDateForPeriodCoveredByReport" contextRef="broken">soon</ix:nonNumeric>'
    with pytest.raises(IxbrlError, match="no reporting period"):
        parse_accounts(_doc(body))


def test_dormant_flag_reads_formats_and_prose() -> None:
    """The dormant flag honours the boolean formats, then the text."""
    prose = '<ix:nonNumeric name="bus:EntityDormant" contextRef="cur">Dormant</ix:nonNumeric>'
    assert parse_accounts(_doc(END_DATE + prose)).dormant
    fmt = '<ix:nonNumeric name="bus:EntityDormantTruefalse" contextRef="cur" format="ixt2:booleantrue">x</ix:nonNumeric>'
    assert parse_accounts(_doc(END_DATE + fmt)).dormant
    plain = '<ix:nonNumeric name="bus:EntityDormantTruefalse" contextRef="cur">false</ix:nonNumeric>'
    assert not parse_accounts(_doc(END_DATE + plain)).dormant


def test_html_entities_leading_bytes_and_old_namespace() -> None:
    """Named HTML entities, a BOM before the prolog and the 2008 namespace all parse."""
    body = (
        END_DATE
        + "<p>A&nbsp;B&amp;C&pound;</p>"
        + _fact("core:CashBankOnHand", "cur_end", "1")
    )
    parsed = parse_accounts(_doc(body, prefix="﻿\n"))
    assert _values(parsed, "cash") == (Decimal(1), None, "ok")
    parsed = parse_accounts(_doc(body, ix_ns="http://www.xbrl.org/2008/inlineXBRL"))
    assert _values(parsed, "cash") == (Decimal(1), None, "ok")


def test_documents_that_are_not_accounts_are_refused() -> None:
    """PDFs, plain pages, malformed XML, entity bombs and huge files raise."""
    with pytest.raises(IxbrlError, match="not an XML document"):
        parse_accounts(b"%PDF-1.4 ...")
    with pytest.raises(IxbrlError, match="no inline XBRL facts"):
        parse_accounts(b"<html><body>Hello</body></html>")
    with pytest.raises(IxbrlError, match="malformed XML"):
        parse_accounts(b"<html><body>")
    with pytest.raises(IxbrlError, match="malformed XML"):
        parse_accounts(b"<html>&nbsp;&nbsp;&notanentity;</html")
    bomb = (
        b'<!DOCTYPE lol [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        b"<html><body>&lol2;</body></html>"
    )
    with pytest.raises(IxbrlError, match="refused XML"):
        parse_accounts(bomb)
    with pytest.raises(IxbrlError, match="too large"):
        parse_accounts(b"<" + b" " * MAX_BYTES)
    assert not looks_like_ixbrl(b"%PDF-1.4")
    assert not looks_like_ixbrl(b"")
    assert not looks_like_ixbrl(b"<html><body>plain</body></html>")


def test_parse_number_edge_cases() -> None:
    """The number reader handles every transformation seen in the wild."""
    assert parse_number("1,234.56", "ixt:numcommadot", None, None) == Decimal("1234.56")
    assert parse_number("1.234,56", "ixt:numdotcomma", None, None) == Decimal("1234.56")
    assert parse_number("(1,000)", None, None, "-") == Decimal(-1000)
    assert parse_number("-", None, None, None) == Decimal(0)
    assert parse_number("\u2013", None, None, None) == Decimal(0)
    assert parse_number("anything", "ixt2:zerodash", None, None) == Decimal(0)
    assert parse_number("12", None, "3", None) == Decimal(12000)
    assert parse_number("12", None, "-2", None) == Decimal("0.12")
    assert parse_number("12", None, "big", None) is None
    assert parse_number("twelve", None, None, None) is None
    assert parse_number("Infinity", None, None, None) is None
    assert parse_number("1\u00a0000", None, None, None) == Decimal(1000)


def test_figure_defaults() -> None:
    """A bare figure is undisclosed."""
    assert Figure() == Figure(
        value=None, prior=None, status="not_disclosed", concept=None
    )
