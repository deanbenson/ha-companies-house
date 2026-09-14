"""Structured accounts: the figures read from a company's filed accounts.

Each set of accounts the company has filed becomes an :class:`AccountsYear`;
the years together are the :class:`AccountsHistory` kept per company. This
module holds the models and the pure helpers that turn figures into words,
flags and percentages. Reading the accounts from the register is the job of
:mod:`accounts_coordinator`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .ixbrl import METRICS as PARSED_METRICS, Figure
from .models import JsonDict, StorableModel

# The figures kept per year, in the order they are shown.
METRICS: tuple[str, ...] = (
    "turnover",
    "profit_before_tax",
    "profit_after_tax",
    "cash",
    "net_assets",
    "creditors_within_one_year",
    "employees",
)
MONEY_METRICS = frozenset(METRICS) - {"employees"}
METRIC_NAMES: dict[str, str] = {
    "turnover": "Turnover",
    "profit_before_tax": "Profit before tax",
    "profit_after_tax": "Profit after tax",
    "cash": "Cash",
    "net_assets": "Net assets",
    "creditors_within_one_year": "Creditors due within a year",
    "debtors": "Debtors",
    "employees": "Employees",
}
# How many years of accounts are kept per company, newest first.
HISTORY_CAP = 6
# How far back the first read goes.
BACKFILL_YEARS = 5

STATUS_WORDS: dict[str, str] = {
    "ok": "read",
    "pending": "not read yet",
    "no_ixbrl": "no structured data",
    "parse_error": "could not be read",
}
ACCOUNTS_TYPE_WORDS: dict[str, str] = {
    "micro-entity": "micro-entity accounts",
    "small": "small company accounts",
    "full": "full accounts",
    "dormant": "dormant company accounts",
    "total-exemption-full": "full accounts (audit exempt)",
    "total-exemption-small": "small company accounts (audit exempt)",
    "unaudited-abridged": "abridged accounts",
    "audited-abridged": "abridged accounts",
    "medium": "medium company accounts",
    "group": "group accounts",
    "full-group": "group accounts",
    "small-group": "small group accounts",
    "medium-group": "medium group accounts",
    "initial": "initial accounts",
    "interim": "interim accounts",
    "partial-exemption": "partially exempt accounts",
}
# Accounts that leave out the profit and loss account, or nearly everything.
_ABBREVIATED_TYPES = frozenset(
    {
        "micro-entity",
        "small",
        "total-exemption-small",
        "unaudited-abridged",
        "audited-abridged",
        "dormant",
    }
)


@dataclass(frozen=True, kw_only=True)
class AccountsYear(StorableModel):
    """One set of filed accounts and what was read from it.

    ``status`` says how the read went: ``ok``, ``pending`` (not read yet),
    ``no_ixbrl`` (paper accounts, or filed without structured data) or
    ``parse_error``. ``source`` says where the figures came from: ``ixbrl``
    (this year's own accounts), ``comparative`` (the year-before column of
    the next accounts, used only when this year has no structured data) or
    ``none``.
    """

    transaction_id: str
    made_up_to: date
    filed_on: date | None = None
    # The year end the comparative (year before) column refers to, once read.
    prior_period_end: date | None = None
    accounts_type: str | None = None
    amended: bool = False
    paper_filed: bool = False
    document_id: str | None = None
    source: str = "none"
    status: str = "pending"
    dormant: bool = False
    accounting_standard: str | None = None
    accounts_type_member: str | None = None
    figures: dict[str, Figure] = field(default_factory=dict)
    error: str | None = None
    # A fresh read was asked for (``read_accounts`` with ``force``): the
    # figures on hand stay, and keep feeding the rating, until it lands.
    reread: bool = False

    @property
    def is_read(self) -> bool:
        """Return True once this year's own accounts were read."""
        return self.status == "ok"

    @property
    def has_figures(self) -> bool:
        """Return True when any figure has a value."""
        return any(f.value is not None for f in self.figures.values())

    def figure(self, metric: str) -> Figure:
        """Return one figure, or an undisclosed one."""
        return self.figures.get(metric) or Figure()

    def value(self, metric: str) -> Decimal | None:
        """Return one figure's value."""
        return self.figure(metric).value

    @property
    def type_words(self) -> str:
        """Return the kind of accounts in plain English."""
        if self.accounts_type in ACCOUNTS_TYPE_WORDS:
            return ACCOUNTS_TYPE_WORDS[self.accounts_type]
        if self.dormant:
            return ACCOUNTS_TYPE_WORDS["dormant"]
        member = (self.accounts_type_member or "").lower()
        if "filleted" in member:
            return "filleted accounts"
        if "abridged" in member:
            return "abridged accounts"
        return "accounts"

    @property
    def undisclosed_reason(self) -> str | None:
        """Explain why figures are missing when the accounts type explains it."""
        if self.accounts_type in _ABBREVIATED_TYPES or self.dormant:
            return f"not disclosed ({self.type_words})"
        member = (self.accounts_type_member or "").lower()
        if "filleted" in member or "abridged" in member:
            return f"not disclosed ({self.type_words})"
        return None

    def figure_status_words(self, metric: str) -> str:
        """Say in plain English why a figure is there or not.

        A figure can only be "not disclosed" once the accounts were read;
        before that the year's own state is the reason ("not read yet", "no
        structured data"). Figures borrowed from the next year's comparative
        column are missing when that column left them out.
        """
        figure = self.figure(metric)
        if figure.value is not None or figure.status != "not_disclosed":
            return figure.status.replace("_", " ")
        if self.source == "comparative":
            return "not in the following year's comparatives"
        if not self.is_read:
            return STATUS_WORDS.get(self.status, self.status)
        return self.undisclosed_reason or "not disclosed"


@dataclass(frozen=True, kw_only=True)
class AccountsHistory(StorableModel):
    """Every set of accounts read for a company, newest first."""

    years: list[AccountsYear] = field(default_factory=list)
    # True once the register's list of accounts filings has been looked at.
    checked: bool = False

    @property
    def latest(self) -> AccountsYear | None:
        """Return the newest accounts filed, whatever their state."""
        return self.years[0] if self.years else None

    @property
    def latest_read(self) -> AccountsYear | None:
        """Return the newest year with figures."""
        return next((y for y in self.years if y.has_figures), None)

    @property
    def pending(self) -> list[AccountsYear]:
        """Return the years still to be read, or read again."""
        return [y for y in self.years if y.status == "pending" or y.reread]

    def series(self) -> list[JsonDict]:
        """Return one row per year with plain numbers, oldest first."""
        return [
            {
                "made_up_to": year.made_up_to.isoformat(),
                "source": year.source,
                **{metric: plain_number(year.value(metric)) for metric in METRICS},
            }
            for year in reversed(self.years)
            if year.has_figures
        ]


# ---------------------------------------------------------------- numbers


def plain_number(value: Decimal | None) -> int | float | None:
    """Return a JSON friendly number: an int when whole, else a float."""
    if value is None:
        return None
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def percent_change(value: Decimal | None, prior: Decimal | None) -> float | None:
    """Return the percentage change from ``prior`` to ``value``, one decimal."""
    if value is None or prior is None or prior == 0:
        return None
    return round(float((value - prior) / abs(prior) * 100), 1)


def format_money(value: Decimal | float | None) -> str:
    """Write a sum of money the way people do: £43.3m, £912k, £1.2k, £100."""
    if value is None:
        return "not disclosed"
    amount = Decimal(str(value))
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    # The unit is chosen after rounding, so £999,950 is £1m rather than £1000k.
    if round(amount / 1_000) >= 1_000:
        text = f"{amount / 1_000_000:.1f}".removesuffix(".0") + "m"
    elif amount >= 100_000:
        text = f"{amount / 1_000:.0f}k"
    elif amount >= 1_000:
        text = f"{amount / 1_000:.1f}".removesuffix(".0") + "k"
    else:
        text = f"{amount:.0f}"
    return f"{sign}£{text}"


def format_count(value: Decimal | float | None) -> str:
    """Write a head count."""
    if value is None:
        return "not disclosed"
    number = plain_number(Decimal(str(value)))
    return f"{number:g}" if isinstance(number, float) else str(number)


def format_figure(metric: str, value: Decimal | float | None) -> str:
    """Write one figure in the right style for its metric."""
    return format_money(value) if metric in MONEY_METRICS else format_count(value)


def format_change(percent: float | None) -> str:
    """Write a percentage change: "up 12 %", "down 8 %", "unchanged"."""
    if percent is None:
        return ""
    if percent == 0:
        return "unchanged"
    word = "up" if percent > 0 else "down"
    size = abs(percent)
    # Big jumps (a dormant company starting to trade) are written in full,
    # never as 2.5e+06.
    number = f"{size:,.0f}" if size >= 1_000 else f"{size:g}"
    return f"{word} {number} %"


# ---------------------------------------------------------------- reading


def changes_for(figures: Mapping[str, Figure]) -> dict[str, float]:
    """Return the percentage change on last year for every figure that has one."""
    out: dict[str, float] = {}
    for metric in METRICS:
        figure = figures.get(metric)
        if figure is None:
            continue
        change = percent_change(figure.value, figure.prior)
        if change is not None:
            out[metric] = change
    return out


def _both(figure: Figure | None) -> tuple[Decimal, Decimal] | None:
    if figure is None or figure.value is None or figure.prior is None:
        return None
    return figure.value, figure.prior


def flags_for(figures: Mapping[str, Figure]) -> list[str]:
    """Return the plain-English warnings the figures justify.

    Each one needs its inputs to be present; nothing is inferred from a
    figure the accounts do not carry.
    """
    flags: list[str] = []
    net_assets = figures.get("net_assets")
    if net_assets is not None and net_assets.value is not None and net_assets.value < 0:
        flags.append("Net liabilities")
    if (cash := _both(figures.get("cash"))) and cash[1] > 0 and cash[0] <= cash[1] / 2:
        flags.append("Cash halved")
    if (
        (turnover := _both(figures.get("turnover")))
        and turnover[1] > 0
        and turnover[0] <= turnover[1] * Decimal("0.8")
    ):
        flags.append("Turnover down 20 %+")
    if (
        (assets := _both(net_assets))
        and assets[1] > 0
        and assets[0] <= assets[1] * Decimal("0.75")
    ):
        flags.append("Net assets down 25 %+")
    creditors = figures.get("creditors_within_one_year")
    cash_now = figures.get("cash")
    debtors = figures.get("debtors")
    if (
        creditors is not None
        and cash_now is not None
        and debtors is not None
        and creditors.value is not None
        and cash_now.value is not None
        and debtors.value is not None
        and creditors.value > cash_now.value + debtors.value
    ):
        flags.append("Creditors exceed cash and debtors")
    # Two people becoming one is a family firm, not a warning: only a team of
    # four or more losing half counts.
    if (
        (staff := _both(figures.get("employees")))
        and staff[1] >= 4
        and staff[0] <= staff[1] / 2
    ):
        flags.append("Headcount halved")
    return flags


def accounts_type_from_description(description: str | None) -> tuple[str | None, bool]:
    """Return the accounts type and whether the filing is an amendment.

    The filing description keys look like ``accounts-with-accounts-type-full``
    and ``accounts-amended-with-accounts-type-micro-entity``.
    """
    if not description:
        return None, False
    amended = "amended" in description
    marker = "accounts-type-"
    if marker in description:
        return description.split(marker, 1)[1] or None, amended
    if description == "accounts-dormant-company":
        return "dormant", amended
    return None, amended


def figures_payload(figures: Mapping[str, Figure]) -> dict[str, int | float | None]:
    """Return the figures as plain numbers, for events and attributes."""
    return {
        metric: plain_number(figures.get(metric, Figure()).value) for metric in METRICS
    }


def year_payload(year: AccountsYear) -> JsonDict:
    """Return the payload of an ``accounts`` / ``read`` change event."""
    return {
        "transaction_id": year.transaction_id,
        "document_id": year.document_id,
        "made_up_to": year.made_up_to.isoformat(),
        "filed_on": year.filed_on.isoformat() if year.filed_on else None,
        "accounts_type": year.accounts_type,
        "accounts_type_words": year.type_words,
        "amended": year.amended,
        "dormant": year.dormant,
        "source": year.source,
        "figures": figures_payload(year.figures),
        "changes": changes_for(year.figures),
        "flags": flags_for(year.figures),
    }


def figure_attributes(year: AccountsYear) -> dict[str, JsonDict]:
    """Return every figure with its prior, change and status, for a sensor."""
    out: dict[str, JsonDict] = {}
    for metric in METRICS:
        figure = year.figure(metric)
        out[metric] = {
            "value": plain_number(figure.value),
            "prior": plain_number(figure.prior),
            "change_percent": percent_change(figure.value, figure.prior),
            "status": year.figure_status_words(metric),
        }
    return out


def describe_figures(payload: Mapping[str, Any]) -> str:
    """Write the figures in an event payload as one sentence.

    For example: "Turnover £13.8k (down 17 %), profit before tax £2.6k, cash
    £13.6k, net assets -£1.0k, 0 employees. Net liabilities."
    """
    figures = payload.get("figures") or {}
    changes = payload.get("changes") or {}
    parts: list[str] = []
    for metric in METRICS:
        value = figures.get(metric)
        if value is None:
            continue
        change = format_change(changes.get(metric))
        suffix = f" ({change})" if change else ""
        if metric == "employees":
            count = format_count(value)
            noun = "employee" if count == "1" else "employees"
            parts.append(f"{count} {noun}{suffix}")
        else:
            name = METRIC_NAMES[metric]
            name = name if not parts else name[0].lower() + name[1:]
            parts.append(f"{name} {format_money(value)}{suffix}")
    sentence = ", ".join(parts) + "." if parts else ""
    flags = [str(f) for f in payload.get("flags") or []]
    if flags:
        sentence = (sentence + " " if sentence else "") + ". ".join(flags) + "."
    if not sentence:
        words = str(payload.get("accounts_type_words") or "accounts")
        return f"No headline figures are disclosed in {words}."
    return sentence


def year_summary(year: AccountsYear) -> JsonDict:
    """Return one year as plain data, for actions, reports and assistants."""
    figures: dict[str, JsonDict] = {}
    for metric, attrs in figure_attributes(year).items():
        figures[metric] = {
            **attrs,
            "text": format_figure(metric, attrs["value"])
            if attrs["value"] is not None
            else attrs["status"],
        }
    return {
        "made_up_to": year.made_up_to.isoformat(),
        "filed_on": year.filed_on.isoformat() if year.filed_on else None,
        "transaction_id": year.transaction_id,
        "document_id": year.document_id,
        "accounts_type": year.accounts_type,
        "accounts_type_words": year.type_words,
        "amended": year.amended,
        "paper_filed": year.paper_filed,
        "dormant": year.dormant,
        "status": year.status,
        "status_words": STATUS_WORDS.get(year.status, year.status),
        "source": year.source,
        "error": year.error,
        "figures": figures,
        "flags": flags_for(year.figures),
    }


def history_summary(history: AccountsHistory | None) -> JsonDict:
    """Return a whole history as plain data."""
    if history is None:
        return {"checked": False, "latest": None, "years": [], "series": []}
    latest = history.latest_read or history.latest
    return {
        "checked": history.checked,
        "latest": year_summary(latest) if latest else None,
        "years": [year_summary(y) for y in history.years],
        "series": history.series(),
    }


__all__ = [
    "BACKFILL_YEARS",
    "HISTORY_CAP",
    "METRICS",
    "METRIC_NAMES",
    "MONEY_METRICS",
    "PARSED_METRICS",
    "AccountsHistory",
    "AccountsYear",
    "Figure",
    "accounts_type_from_description",
    "changes_for",
    "describe_figures",
    "figure_attributes",
    "figures_payload",
    "flags_for",
    "format_change",
    "format_count",
    "format_figure",
    "format_money",
    "history_summary",
    "percent_change",
    "plain_number",
    "year_payload",
    "year_summary",
]
