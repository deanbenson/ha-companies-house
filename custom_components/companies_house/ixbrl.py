"""Read the headline figures out of a set of iXBRL accounts.

Companies House serves electronically filed accounts as Inline XBRL: an
XHTML page whose numbers are wrapped in ``ix:nonFraction`` tags that name a
taxonomy concept, a reporting period (a context) and a unit. This module
turns one such document into a handful of figures per period, without any
knowledge of the taxonomy beyond a table of concept names.

Everything here is pure: bytes in, dataclasses out. Nothing touches Home
Assistant, so it can run in an executor thread.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from html.entities import html5
import re
import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring as defused_fromstring

from .models import StorableModel

# Larger than any filing seen (2 MB); a cheap defence against a pathological file.
MAX_BYTES = 8 * 1024 * 1024

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
IX_NAMESPACES = frozenset(
    {"http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL"}
)
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"

# Transformation formats and bare text that mean zero.
ZERO_FORMATS = frozenset(
    {"numdash", "zerodash", "fixedzero", "fixed-zero", "nocontent"}
)
# A lone hyphen, en dash, em dash or minus sign stands for nothing.
ZERO_WORDS = frozenset(
    {"", "-", "\u2013", "\u2014", "\u2212", "nil", "none", "no", "zero"}
)
# Formats where "." separates thousands and "," the decimals.
COMMA_DECIMAL_FORMATS = frozenset(
    {"numdotcomma", "numcommadecimal", "num-comma-decimal", "numspacecomma"}
)
# Ordinary, non-breaking, narrow and thin spaces all turn up as thousands separators.
_SPACES = (" ", "\u00a0", "\u202f", "\u2009")

# Every metric this module reads, in the order they are reported.
METRICS: tuple[str, ...] = (
    "turnover",
    "profit_before_tax",
    "profit_after_tax",
    "cash",
    "net_assets",
    "creditors_within_one_year",
    "debtors",
    "employees",
)
# Metrics measured over the year (duration contexts); the rest are balance
# sheet figures at the year end (instant contexts).
DURATION_METRICS = frozenset(
    {"turnover", "profit_before_tax", "profit_after_tax", "employees"}
)
MONEY_METRICS = frozenset(METRICS) - {"employees"}

type Dims = tuple[tuple[str, str], ...]
type Period = tuple[str, ...]

NO_DIMS: Dims = ()
_WITHIN_ONE_YEAR: Dims = (("MaturitiesOrExpirationPeriodsDimension", "WithinOneYear"),)
_CURRENT_INSTRUMENTS: Dims = (
    ("FinancialInstrumentCurrentNon-currentDimension", "CurrentFinancialInstruments"),
)
_CONSOLIDATED = ("GroupCompanyDataDimension", "Consolidated")

# Concept candidates per metric, in priority order: (concept local name, the
# exact explicit dimensions the fact must carry). FRC names first, then the
# legacy UK GAAP 2009 names, then the charities taxonomy.
CANDIDATES: dict[str, tuple[tuple[str, Dims], ...]] = {
    "turnover": (
        ("TurnoverRevenue", NO_DIMS),
        ("TurnoverGrossOperatingRevenue", NO_DIMS),
        ("Revenue", NO_DIMS),
        ("IncomeEndowments", NO_DIMS),
    ),
    "profit_before_tax": (
        ("ProfitLossOnOrdinaryActivitiesBeforeTax", NO_DIMS),
        ("ProfitLossBeforeTax", NO_DIMS),
        ("NetIncomeExpenditureBeforeTaxForReportingPeriod", NO_DIMS),
    ),
    "profit_after_tax": (
        ("ProfitLoss", NO_DIMS),
        ("ProfitLossOnOrdinaryActivitiesAfterTax", NO_DIMS),
        ("ProfitLossForPeriod", NO_DIMS),
        (
            "NetIncomeExpenditureBeforeTransfersBetweenFundsOtherRecognisedGainsLosses",
            NO_DIMS,
        ),
    ),
    "cash": (
        ("CashBankOnHand", NO_DIMS),
        ("CashBankInHand", NO_DIMS),
        ("CashCashEquivalents", NO_DIMS),
        ("CashCashEquivalentsCashFlowValue", NO_DIMS),
    ),
    "net_assets": (
        ("NetAssetsLiabilities", NO_DIMS),
        ("NetAssetsLiabilitiesIncludingPensionAssetLiability", NO_DIMS),
        ("Equity", NO_DIMS),
        ("ShareholderFunds", NO_DIMS),
        ("CharityFunds", NO_DIMS),
    ),
    "creditors_within_one_year": (
        ("Creditors", _WITHIN_ONE_YEAR),
        ("Creditors", _CURRENT_INSTRUMENTS),
        ("Creditors", tuple(sorted(_CURRENT_INSTRUMENTS + _WITHIN_ONE_YEAR))),
        ("CreditorsDueWithinOneYear", NO_DIMS),
        ("Creditors", NO_DIMS),
        ("CurrentLiabilities", NO_DIMS),
    ),
    "debtors": (
        ("Debtors", NO_DIMS),
        ("Debtors", _WITHIN_ONE_YEAR),
        ("DebtorsDueWithinOneYear", NO_DIMS),
    ),
    "employees": (
        ("AverageNumberEmployeesDuringPeriod", NO_DIMS),
        ("EmployeesTotal", NO_DIMS),
    ),
}

# Facts about the report itself, read from ix:nonNumeric tags.
_REPORT_CONCEPTS = frozenset(
    {
        "StartDateForPeriodCoveredByReport",
        "EndDateForPeriodCoveredByReport",
        "BalanceSheetDate",
        "EntityDormantTruefalse",
        "EntityDormant",
        "UKCompaniesHouseRegisteredNumber",
        "EntityCurrentLegalOrRegisteredName",
        "EntityCurrentLegalName",
    }
)

_ENTITY_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})


class IxbrlError(ValueError):
    """The document could not be read as iXBRL accounts."""


@dataclass(frozen=True, kw_only=True)
class Figure(StorableModel):
    """One metric for one set of accounts: this year and the year before.

    ``status`` says why a value may be missing: ``ok``, ``not_disclosed``
    (the accounts do not carry that line, which is normal for filleted and
    micro-entity accounts), ``conflict`` (the same figure was tagged twice
    with different values; the visible one is kept) or ``unit_mismatch``
    (a monetary figure in a currency other than sterling).
    """

    value: Decimal | None = None
    prior: Decimal | None = None
    status: str = "not_disclosed"
    concept: str | None = None


@dataclass(frozen=True, kw_only=True)
class ParsedAccounts:
    """What one iXBRL document says about the company."""

    period_start: date | None
    period_end: date | None
    prior_period_end: date | None
    dormant: bool
    accounting_standard: str | None
    accounts_type_member: str | None
    company_number: str | None
    company_name: str | None
    figures: dict[str, Figure]
    fact_count: int


@dataclass(frozen=True)
class Fact:
    """One numeric fact as tagged in the document."""

    concept: str
    period: Period
    dims: Dims
    value: Decimal | None
    unit: str | None
    hidden: bool
    order: int


@dataclass
class _Document:
    """Everything gathered in one pass over the tree."""

    facts: list[Fact] = field(default_factory=list)
    periods: dict[str, Period] = field(default_factory=dict)
    dims: dict[str, Dims] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    text: dict[str, str] = field(default_factory=dict)
    text_period: dict[str, Period] = field(default_factory=dict)
    text_format: dict[str, str] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)
    nonnumeric_count: int = 0


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def _local(qname: str | None) -> str:
    return (qname or "").strip().rpartition(":")[2]


def _format_name(value: str | None) -> str:
    return _local(value).lower()


def parse_number(
    text: str, fmt: str | None, scale: str | None, sign: str | None
) -> Decimal | None:
    """Turn the text of an ``ix:nonFraction`` into a number, per the iXBRL rules.

    The transformation format decides which separators to strip, then the
    value is multiplied by ten to the power of ``scale`` and negated when
    ``sign`` is a minus. Anything that still does not parse is None.
    """
    cleaned = text.strip().strip("()").strip()
    name = _format_name(fmt)
    if name in ZERO_FORMATS or cleaned.casefold() in ZERO_WORDS:
        return Decimal(0)
    for space in _SPACES:
        cleaned = cleaned.replace(space, "")
    if name in COMMA_DECIMAL_FORMATS:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    if scale:
        try:
            value = value.scaleb(int(scale))
        except ValueError, InvalidOperation:
            return None
    if sign == "-":
        value = -value
    return value


def _fact_text(element: ET.Element) -> str:
    """Return the text of a fact, leaving out anything inside ``ix:exclude``."""
    parts: list[str] = [element.text or ""]
    for child in element:
        if _split_tag(child.tag)[1] != "exclude":
            parts.append(_fact_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _rewrite_entities(data: bytes) -> bytes:
    """Replace HTML named entities such as ``&nbsp;`` with numeric references.

    A ``<!DOCTYPE html>`` promises those entities, but expat never fetches
    the DTD, so they would otherwise be undefined.
    """

    def _sub(match: re.Match[bytes]) -> bytes:
        name = match.group(1).decode()
        if name in _XML_ENTITIES:
            return match.group(0)
        replacement = html5.get(name + ";")
        if replacement is None:
            return match.group(0)
        return "".join(f"&#{ord(c)};" for c in replacement).encode()

    return _ENTITY_RE.sub(_sub, data)


def _parse_tree(data: bytes) -> ET.Element:
    start = data.find(b"<")
    if start < 0:
        raise IxbrlError("not an XML document")
    if start:
        data = data[start:]
    try:
        root: ET.Element = defused_fromstring(data)
    except ET.ParseError as err:
        if "undefined entity" not in str(err):
            raise IxbrlError(f"malformed XML: {err}") from err
        try:
            root = defused_fromstring(_rewrite_entities(data))
        except ET.ParseError as err2:
            raise IxbrlError(f"malformed XML: {err2}") from err2
    except ValueError as err:
        # defusedxml refuses entity declarations and external references.
        raise IxbrlError(f"refused XML: {err}") from err
    return root


def _iter_hidden(root: ET.Element) -> Iterator[ET.Element]:
    for element in root.iter():
        namespace, local = _split_tag(element.tag)
        if local == "hidden" and namespace in IX_NAMESPACES:
            yield from element.iter()


def _read_context(context: ET.Element, doc: _Document) -> None:
    context_id = context.get("id")
    if not context_id:
        return
    instant = context.find(f".//{{{XBRLI}}}instant")
    if instant is not None and instant.text:
        doc.periods[context_id] = (instant.text.strip(),)
    else:
        start = context.find(f".//{{{XBRLI}}}startDate")
        end = context.find(f".//{{{XBRLI}}}endDate")
        if start is None or end is None or not start.text or not end.text:
            return
        doc.periods[context_id] = (start.text.strip(), end.text.strip())
    members: list[tuple[str, str]] = []
    for member in context.iter(f"{{{XBRLDI}}}explicitMember"):
        dimension = _local(member.get("dimension"))
        value = _local(member.text)
        members.append((dimension, value))
        doc.members.setdefault(dimension, value)
    members.extend(
        (_local(member.get("dimension")), "*typed*")
        for member in context.iter(f"{{{XBRLDI}}}typedMember")
    )
    doc.dims[context_id] = tuple(sorted(members))


def _read_unit(unit: ET.Element, doc: _Document) -> None:
    unit_id = unit.get("id")
    if not unit_id:
        return
    measures = [
        _local(m.text) for m in unit.iter(f"{{{XBRLI}}}measure") if _local(m.text)
    ]
    doc.units[unit_id] = "/".join(measures) if measures else unit_id


def _read_document(root: ET.Element) -> _Document:
    doc = _Document()
    # Contexts and units live in ix:resources, usually at the very end of the
    # page, so they are gathered before the facts that refer to them.
    for context in root.iter(f"{{{XBRLI}}}context"):
        _read_context(context, doc)
    for unit in root.iter(f"{{{XBRLI}}}unit"):
        _read_unit(unit, doc)
    hidden = {id(e) for e in _iter_hidden(root)}
    order = 0
    for element in root.iter():
        namespace, local = _split_tag(element.tag)
        if namespace not in IX_NAMESPACES:
            continue
        if local == "nonFraction":
            order += 1
            context_ref = element.get("contextRef") or ""
            if context_ref not in doc.periods:
                continue
            value = (
                None
                if element.get(XSI_NIL) == "true"
                else parse_number(
                    _fact_text(element),
                    element.get("format"),
                    element.get("scale"),
                    element.get("sign"),
                )
            )
            unit_ref = element.get("unitRef")
            doc.facts.append(
                Fact(
                    concept=_local(element.get("name")),
                    period=doc.periods[context_ref],
                    dims=doc.dims.get(context_ref, ()),
                    value=value,
                    unit=doc.units.get(unit_ref or "", unit_ref),
                    hidden=id(element) in hidden,
                    order=order,
                )
            )
        elif local == "nonNumeric":
            doc.nonnumeric_count += 1
            concept = _local(element.get("name"))
            if concept not in _REPORT_CONCEPTS:
                continue
            text = " ".join(_fact_text(element).split())
            if concept in doc.text:
                continue
            doc.text[concept] = text
            doc.text_format[concept] = element.get("format") or ""
            context_ref = element.get("contextRef") or ""
            if context_ref in doc.periods:
                doc.text_period[concept] = doc.periods[context_ref]
    return doc


def _iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class _Periods:
    """The reporting periods a document is read against."""

    start: date | None
    end: date
    current: Period | None
    prior: Period | None
    current_instant: Period
    prior_instant: Period | None


def _choose_periods(doc: _Document) -> _Periods | None:
    """Work out which contexts are this year and which are last year.

    The year end comes from the context of the report's end date fact (never
    its display text): a duration context, or an instant in dormant AA02
    filings. This year's duration is the undimensioned one ending then, and
    last year's the undimensioned one ending before this year starts. The
    balance sheet is read at both year ends.
    """
    durations = sorted(
        {f.period for f in doc.facts if len(f.period) == 2 and not f.dims},
        key=lambda p: (p[1], p[0]),
        reverse=True,
    )
    instants = sorted(
        {f.period[0] for f in doc.facts if len(f.period) == 1 and not f.dims},
        reverse=True,
    )
    end_period = doc.text_period.get(
        "EndDateForPeriodCoveredByReport"
    ) or doc.text_period.get("BalanceSheetDate")
    end = end_period[-1] if end_period else None
    if end is None or _iso(end) is None:
        end = durations[0][1] if durations else (instants[0] if instants else None)
    if end is None or (end_date := _iso(end)) is None:
        return None
    current = next((p for p in durations if p[1] == end), None)
    if current is None:
        # Dormant accounts tag nothing over the year; take the start from the
        # report's own start date when it is written as an ISO date.
        start_period = doc.text_period.get("StartDateForPeriodCoveredByReport")
        start_text = doc.text.get("StartDateForPeriodCoveredByReport", "")
        if start_period and len(start_period) == 2:
            start = start_period[0]
        elif _iso(start_text) is not None and start_text < end:
            start = start_text
        else:
            start = None
        current = (start, end) if start else None
    start_date = _iso(current[0]) if current else None
    prior = None
    if start_date is not None:
        prior = next(
            (
                p
                for p in durations
                if p != current and (d := _iso(p[1])) is not None and d < start_date
            ),
            None,
        )
    if prior is not None:
        prior_instant: Period | None = (prior[1],)
    elif start_date is not None:
        prior_instant = ((start_date - timedelta(days=1)).isoformat(),)
    else:
        earlier = [i for i in instants if i < end]
        prior_instant = (earlier[0],) if earlier else None
    return _Periods(
        start=start_date,
        end=end_date,
        current=current if start_date is not None else None,
        prior=prior,
        current_instant=(end,),
        prior_instant=prior_instant,
    )


def _dims_without_group(dims: Dims) -> Dims:
    return tuple(d for d in dims if d != _CONSOLIDATED)


def _pick(
    facts: list[Fact], metric: str, period: Period, *, consolidated: bool
) -> tuple[Decimal | None, str | None, str]:
    """Return (value, concept, status) for one metric in one period."""
    money = metric in MONEY_METRICS
    saw_foreign = False
    for concept, required in CANDIDATES[metric]:
        matching = [
            f
            for f in facts
            if f.concept == concept
            and f.period == period
            and f.value is not None
            and _dims_without_group(f.dims) == required
        ]
        if not matching:
            continue
        if money:
            sterling = [f for f in matching if f.unit == "GBP"]
            if not sterling:
                saw_foreign = True
                continue
            matching = sterling
        group = [f for f in matching if _CONSOLIDATED in f.dims]
        company = [f for f in matching if _CONSOLIDATED not in f.dims]
        chosen = group if consolidated and group else (company or group)
        values = {f.value for f in chosen}
        if len(values) == 1:
            return chosen[0].value, concept, "ok"
        visible = sorted((f for f in chosen if not f.hidden), key=lambda f: f.order)
        first = (visible or sorted(chosen, key=lambda f: f.order))[0]
        return first.value, concept, "conflict"
    return None, None, "unit_mismatch" if saw_foreign else "not_disclosed"


def _dormant(doc: _Document) -> bool:
    for concept in ("EntityDormantTruefalse", "EntityDormant"):
        if concept not in doc.text:
            continue
        fmt = _format_name(doc.text_format.get(concept))
        if fmt == "booleantrue":
            return True
        if fmt == "booleanfalse":
            return False
        return doc.text[concept].strip().casefold() in ("true", "yes", "dormant")
    return False


def parse_accounts(data: bytes) -> ParsedAccounts:
    """Read the headline figures from one iXBRL accounts document.

    Raises :class:`IxbrlError` when the bytes are not an iXBRL document
    (a PDF, plain HTML, malformed XML, or one with no tagged facts).
    """
    if len(data) > MAX_BYTES:
        raise IxbrlError(f"document too large ({len(data)} bytes)")
    root = _parse_tree(data)
    doc = _read_document(root)
    if not doc.facts and not doc.nonnumeric_count:
        raise IxbrlError("no inline XBRL facts")
    periods = _choose_periods(doc)
    if periods is None:
        raise IxbrlError("no reporting period")
    consolidated = any(_CONSOLIDATED in f.dims for f in doc.facts)
    figures: dict[str, Figure] = {}
    for metric in METRICS:
        duration = metric in DURATION_METRICS
        period = periods.current if duration else periods.current_instant
        prior_period = periods.prior if duration else periods.prior_instant
        value: Decimal | None = None
        concept: str | None = None
        status = "not_disclosed"
        if period is not None:
            value, concept, status = _pick(
                doc.facts, metric, period, consolidated=consolidated
            )
        prior_value: Decimal | None = None
        prior_concept: str | None = None
        if prior_period is not None:
            prior_value, prior_concept, _ = _pick(
                doc.facts, metric, prior_period, consolidated=consolidated
            )
        if metric == "employees":
            value = abs(value) if value is not None else None
            prior_value = abs(prior_value) if prior_value is not None else None
        figures[metric] = Figure(
            value=value,
            prior=prior_value,
            status=status,
            concept=concept or prior_concept,
        )
    return ParsedAccounts(
        period_start=periods.start,
        period_end=periods.end,
        prior_period_end=_iso(periods.prior[1]) if periods.prior else None,
        dormant=_dormant(doc),
        accounting_standard=doc.members.get("AccountingStandardsDimension"),
        accounts_type_member=doc.members.get("AccountsTypeDimension"),
        company_number=doc.text.get("UKCompaniesHouseRegisteredNumber") or None,
        company_name=(
            doc.text.get("EntityCurrentLegalOrRegisteredName")
            or doc.text.get("EntityCurrentLegalName")
            or None
        ),
        figures=figures,
        fact_count=len(doc.facts),
    )


def looks_like_ixbrl(data: bytes) -> bool:
    """Return True when the bytes could be an inline XBRL document at all.

    A cheap check before parsing: a PDF or an empty body is never iXBRL.
    """
    head = data[:4096].lstrip()
    if not head or head.startswith(b"%PDF"):
        return False
    return b"inlineXBRL" in data[:200_000]


__all__ = [
    "CANDIDATES",
    "DURATION_METRICS",
    "MAX_BYTES",
    "METRICS",
    "MONEY_METRICS",
    "Figure",
    "IxbrlError",
    "ParsedAccounts",
    "looks_like_ixbrl",
    "parse_accounts",
    "parse_number",
]
