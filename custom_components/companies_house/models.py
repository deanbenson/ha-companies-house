"""Typed models for the Companies House resources this integration uses.

Every model is a frozen dataclass built from an API payload with ``from_api``
and round-tripped through storage with ``to_storage`` / ``from_storage``.
Only the fields the integration reads are kept, so the store stays small.
Field names follow the API where a field maps one to one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
import datetime as dt
from datetime import date
import hashlib
import json
import re
import types
import typing
from typing import Any, Self, get_args, get_origin, get_type_hints

from .enumerations import FILING_DESCRIPTIONS

type JsonDict = dict[str, Any]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def parse_date(value: Any) -> date | None:
    """Parse a ``YYYY-MM-DD`` API date. Never fabricates a day."""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def format_date(value: date | None) -> str | None:
    """Render a date the way Companies House does: ``5 April 2026``."""
    if value is None:
        return None
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"


def last_path_segment(link: Any) -> str | None:
    """Return the final segment of a resource link such as ``/officers/abc/appointments``."""
    if not isinstance(link, str) or not link:
        return None
    return link.rstrip("/").rsplit("/", 1)[-1] or None


def officer_id_from_link(link: Any) -> str | None:
    """Extract the officer id from ``links.officer.appointments``.

    The link is ``/officers/{officer_id}/appointments``; the id is not the
    appointment id, so it has to come from here.
    """
    if not isinstance(link, str):
        return None
    match = re.search(r"/officers/([^/]+)/appointments", link)
    return match.group(1) if match else None


def stable_hash(*parts: Any) -> str:
    """Return a short stable hash of the given parts for change detection."""
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]  # noqa: S324


def _encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    return value


def _decode(hint: Any, value: Any) -> Any:
    origin = get_origin(hint)
    if origin in (types.UnionType, typing.Union):
        args = [a for a in get_args(hint) if a is not type(None)]
        if value is None:
            return None
        return _decode(args[0], value)
    if hint is date:
        return parse_date(value)
    if origin is list:
        (item_hint,) = get_args(hint)
        return [_decode(item_hint, v) for v in value or []]
    if origin is dict:
        _, item_hint = get_args(hint)
        return {str(k): _decode(item_hint, v) for k, v in (value or {}).items()}
    if isinstance(hint, type) and is_dataclass(hint):
        return hint.from_storage(value or {})  # type: ignore[attr-defined]
    return value


class StorableModel:
    """Mixin giving dataclasses a storage round trip."""

    def to_storage(self) -> JsonDict:
        """Serialise for the store."""
        result: JsonDict = _encode(self)
        return result

    @classmethod
    def from_storage(cls, data: JsonDict) -> Self:
        """Rebuild from stored data, tolerating missing keys."""
        hints = get_type_hints(cls)
        kwargs: JsonDict = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                kwargs[f.name] = _decode(hints[f.name], data[f.name])
        return cls(**kwargs)


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict(value: Any) -> JsonDict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True, kw_only=True)
class Address(StorableModel):
    """A postal address."""

    premises: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    locality: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    care_of: str | None = None
    po_box: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict | None) -> Address | None:
        """Build from an API address object."""
        if not isinstance(data, dict) or not data:
            return None
        return cls(**{f.name: _str(data.get(f.name)) for f in fields(cls)})

    def one_line(self) -> str:
        """Render as a single comma separated line."""
        parts = [
            self.care_of and f"c/o {self.care_of}",
            " ".join(p for p in (self.premises, self.address_line_1) if p) or None,
            self.address_line_2,
            self.locality,
            self.region,
            self.postal_code,
            self.country,
        ]
        return ", ".join(p for p in parts if p)


@dataclass(frozen=True, kw_only=True)
class AccountsInfo(StorableModel):
    """Accounts deadlines from the company profile."""

    next_due: date | None = None
    next_period_start: date | None = None
    next_period_end: date | None = None
    next_overdue: bool = False
    last_made_up_to: date | None = None
    last_type: str | None = None
    reference_day: int | None = None
    reference_month: int | None = None

    @classmethod
    def from_api(cls, data: JsonDict | None) -> AccountsInfo:
        """Build from the profile ``accounts`` object, preferring non deprecated fields."""
        data = _dict(data)
        nxt = _dict(data.get("next_accounts"))
        last = _dict(data.get("last_accounts"))
        ard = _dict(data.get("accounting_reference_date"))
        return cls(
            next_due=parse_date(nxt.get("due_on")) or parse_date(data.get("next_due")),
            next_period_start=parse_date(nxt.get("period_start_on")),
            next_period_end=parse_date(nxt.get("period_end_on"))
            or parse_date(data.get("next_made_up_to")),
            next_overdue=bool(nxt.get("overdue", data.get("overdue", False))),
            last_made_up_to=parse_date(last.get("period_end_on"))
            or parse_date(last.get("made_up_to")),
            last_type=_str(last.get("type")),
            reference_day=_int(_coerce_int(ard.get("day"))),
            reference_month=_int(_coerce_int(ard.get("month"))),
        )


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


@dataclass(frozen=True, kw_only=True)
class ConfirmationStatementInfo(StorableModel):
    """Confirmation statement deadlines from the company profile."""

    next_due: date | None = None
    next_made_up_to: date | None = None
    last_made_up_to: date | None = None
    overdue: bool = False

    @classmethod
    def from_api(cls, data: JsonDict | None) -> ConfirmationStatementInfo:
        """Build from the profile ``confirmation_statement`` object."""
        data = _dict(data)
        return cls(
            next_due=parse_date(data.get("next_due")),
            next_made_up_to=parse_date(data.get("next_made_up_to")),
            last_made_up_to=parse_date(data.get("last_made_up_to")),
            overdue=bool(data.get("overdue", False)),
        )


@dataclass(frozen=True, kw_only=True)
class PreviousName(StorableModel):
    """A former company name."""

    name: str
    effective_from: date | None = None
    ceased_on: date | None = None


@dataclass(frozen=True, kw_only=True)
class CompanyProfile(StorableModel):
    """The company profile resource."""

    company_number: str
    company_name: str
    company_status: str | None = None
    company_status_detail: str | None = None
    type: str | None = None
    subtype: str | None = None
    jurisdiction: str | None = None
    date_of_creation: date | None = None
    date_of_cessation: date | None = None
    accounts: AccountsInfo = field(default_factory=AccountsInfo)
    confirmation_statement: ConfirmationStatementInfo = field(
        default_factory=ConfirmationStatementInfo
    )
    registered_office_address: Address | None = None
    registered_office_is_in_dispute: bool = False
    undeliverable_registered_office_address: bool = False
    sic_codes: list[str] = field(default_factory=list)
    previous_company_names: list[PreviousName] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    can_file: bool | None = None
    etag: str | None = None
    super_secure_managing_officer_count: int | None = None
    has_charges: bool = False
    has_insolvency_history: bool = False
    has_been_liquidated: bool = False
    is_community_interest_company: bool = False
    partial_data_available: str | None = None
    external_registration_number: str | None = None
    last_full_members_list_date: date | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> CompanyProfile:
        """Build from the company profile payload."""
        links = {
            k: v for k, v in _dict(data.get("links")).items() if isinstance(v, str)
        }
        subtype = _str(data.get("subtype"))
        return cls(
            company_number=str(data.get("company_number", "")),
            company_name=str(data.get("company_name", "")),
            company_status=_str(data.get("company_status")),
            company_status_detail=_str(data.get("company_status_detail")),
            type=_str(data.get("type")),
            subtype=subtype,
            jurisdiction=_str(data.get("jurisdiction")),
            date_of_creation=parse_date(data.get("date_of_creation")),
            date_of_cessation=parse_date(data.get("date_of_cessation")),
            accounts=AccountsInfo.from_api(data.get("accounts")),
            confirmation_statement=ConfirmationStatementInfo.from_api(
                data.get("confirmation_statement")
            ),
            registered_office_address=Address.from_api(
                data.get("registered_office_address")
            ),
            registered_office_is_in_dispute=bool(
                data.get("registered_office_is_in_dispute", False)
            ),
            undeliverable_registered_office_address=bool(
                data.get("undeliverable_registered_office_address", False)
            ),
            sic_codes=[str(c) for c in _list(data.get("sic_codes"))],
            previous_company_names=[
                PreviousName(
                    name=str(item.get("name", "")),
                    effective_from=parse_date(item.get("effective_from")),
                    ceased_on=parse_date(item.get("ceased_on")),
                )
                for item in _list(data.get("previous_company_names"))
                if isinstance(item, dict)
            ],
            links=links,
            can_file=_bool(data.get("can_file")),
            etag=_str(data.get("etag")),
            super_secure_managing_officer_count=_int(
                data.get("super_secure_managing_officer_count")
            ),
            # Deprecated flags are only a fallback when the link is absent (7.4).
            has_charges="charges" in links or bool(data.get("has_charges", False)),
            has_insolvency_history="insolvency" in links
            or bool(data.get("has_insolvency_history", False)),
            has_been_liquidated="insolvency" in links
            or bool(data.get("has_been_liquidated", False)),
            is_community_interest_company=subtype == "community-interest-company"
            or bool(data.get("is_community_interest_company", False)),
            partial_data_available=_str(data.get("partial_data_available")),
            external_registration_number=_str(data.get("external_registration_number")),
            last_full_members_list_date=parse_date(
                data.get("last_full_members_list_date")
            ),
        )

    @property
    def next_deadline(self) -> tuple[date, str] | None:
        """Return the soonest of the accounts and confirmation statement deadlines."""
        candidates = [
            (d, kind)
            for d, kind in (
                (self.accounts.next_due, "accounts"),
                (self.confirmation_statement.next_due, "confirmation_statement"),
            )
            if d is not None
        ]
        return min(candidates) if candidates else None


def _format_description_value(value: Any) -> str:
    if isinstance(value, str):
        parsed = parse_date(value)
        return format_date(parsed) or value if parsed else value
    if isinstance(value, list):
        return ", ".join(_format_description_value(v) for v in value)
    if isinstance(value, dict):
        if "figure" in value:
            return " ".join(
                str(v) for v in (value.get("currency"), value.get("figure")) if v
            )
        return ", ".join(str(v) for v in value.values() if v)
    return str(value)


def render_filing_description(
    description: str | None, description_values: JsonDict | None
) -> str:
    """Render a filing description key with its values, without markdown."""
    if not description:
        return ""
    template = FILING_DESCRIPTIONS.get(description)
    values = _dict(description_values)
    if template is None:
        if description == "legacy" and isinstance(values.get("description"), str):
            return str(values["description"])
        return description.replace("-", " ").capitalize()
    rendered = template.replace("**", "")

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return _format_description_value(values[key])
        return ""

    rendered = re.sub(r"\{([a-z_]+)\}", _sub, rendered)
    return re.sub(r"\s{2,}", " ", rendered).strip()


@dataclass(frozen=True, kw_only=True)
class FilingHistoryItem(StorableModel):
    """One filing history transaction."""

    transaction_id: str
    category: str | None = None
    subcategory: str | None = None
    type: str | None = None
    date: dt.date | None = None
    description: str | None = None
    description_values: dict[str, Any] = field(default_factory=dict)
    rendered_description: str = ""
    barcode: str | None = None
    pages: int | None = None
    paper_filed: bool = False
    document_id: str | None = None
    action_date: dt.date | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> FilingHistoryItem:
        """Build from a filing history item payload."""
        links = _dict(data.get("links"))
        values = _dict(data.get("description_values"))
        description = _str(data.get("description"))
        return cls(
            transaction_id=str(data.get("transaction_id", "")),
            category=_str(data.get("category")),
            subcategory=_str(data.get("subcategory")),
            type=_str(data.get("type")),
            date=parse_date(data.get("date")),
            description=description,
            description_values={
                str(k): v
                for k, v in values.items()
                if isinstance(v, str | int | list | dict)
            },
            rendered_description=render_filing_description(description, values),
            barcode=_str(data.get("barcode")),
            pages=_int(data.get("pages")),
            paper_filed=bool(data.get("paper_filed", False)),
            document_id=last_path_segment(links.get("document_metadata")),
            action_date=parse_date(data.get("action_date")),
        )


@dataclass(frozen=True, kw_only=True)
class FilingHistory(StorableModel):
    """A page of filing history."""

    total_count: int = 0
    items: list[FilingHistoryItem] = field(default_factory=list)
    filing_history_status: str | None = None
    etag: str | None = None
    start_index: int = 0
    items_per_page: int = 0

    @classmethod
    def from_api(cls, data: JsonDict) -> FilingHistory:
        """Build from the filing history list payload."""
        return cls(
            total_count=_int(data.get("total_count")) or 0,
            items=[
                FilingHistoryItem.from_api(item)
                for item in _list(data.get("items"))
                if isinstance(item, dict)
            ],
            filing_history_status=_str(data.get("filing_history_status")),
            etag=_str(data.get("etag")),
            start_index=_int(data.get("start_index")) or 0,
            items_per_page=_int(data.get("items_per_page")) or 0,
        )


@dataclass(frozen=True, kw_only=True)
class DateOfBirth(StorableModel):
    """Month and year of birth. The register never publishes the day."""

    month: int | None = None
    year: int | None = None

    @classmethod
    def from_api(cls, data: Any) -> DateOfBirth | None:
        """Build from a ``date_of_birth`` object or a full ``YYYY-MM-DD`` string."""
        if isinstance(data, dict):
            month = _coerce_int(data.get("month"))
            year = _coerce_int(data.get("year"))
            if month is None and year is None:
                return None
            return cls(month=month, year=year)
        if isinstance(data, str) and (parsed := parse_date(data)):
            return cls(month=parsed.month, year=parsed.year)
        return None

    def display(self) -> str | None:
        """Render as ``MM/YYYY``."""
        if self.month is None or self.year is None:
            return None
        return f"{self.month:02d}/{self.year}"


@dataclass(frozen=True, kw_only=True)
class Officer(StorableModel):
    """A company officer appointment."""

    appointment_id: str | None = None
    officer_id: str | None = None
    name: str
    officer_role: str | None = None
    appointed_on: date | None = None
    appointed_before: date | None = None
    resigned_on: date | None = None
    date_of_birth: DateOfBirth | None = None
    nationality: str | None = None
    occupation: str | None = None
    country_of_residence: str | None = None
    former_names: list[str] = field(default_factory=list)
    identification: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: JsonDict) -> Officer:
        """Build from an officer list item."""
        links = _dict(data.get("links"))
        return cls(
            appointment_id=last_path_segment(links.get("self")),
            officer_id=officer_id_from_link(
                _dict(links.get("officer")).get("appointments")
            ),
            name=str(data.get("name", "")),
            officer_role=_str(data.get("officer_role")),
            appointed_on=parse_date(data.get("appointed_on")),
            appointed_before=parse_date(data.get("appointed_before")),
            resigned_on=parse_date(data.get("resigned_on")),
            date_of_birth=DateOfBirth.from_api(data.get("date_of_birth")),
            nationality=_str(data.get("nationality")),
            occupation=_str(data.get("occupation")),
            country_of_residence=_str(data.get("country_of_residence")),
            former_names=[
                " ".join(
                    str(p) for p in (item.get("forenames"), item.get("surname")) if p
                )
                for item in _list(data.get("former_names"))
                if isinstance(item, dict)
            ],
            identification={
                k: v
                for k, v in _dict(data.get("identification")).items()
                if isinstance(v, str)
            },
        )

    @property
    def is_active(self) -> bool:
        """Return True while the appointment has not been resigned."""
        return self.resigned_on is None

    @property
    def details_hash(self) -> str:
        """Hash of the mutable details, for ``details-changed`` events."""
        return stable_hash(
            self.name,
            self.officer_role,
            self.appointed_on,
            self.appointed_before,
            self.resigned_on,
            self.nationality,
            self.occupation,
            self.country_of_residence,
        )


@dataclass(frozen=True, kw_only=True)
class OfficerList(StorableModel):
    """The officer list resource."""

    active_count: int = 0
    resigned_count: int = 0
    total_results: int = 0
    items: list[Officer] = field(default_factory=list)
    etag: str | None = None

    @classmethod
    def from_api(
        cls, data: JsonDict, items: list[JsonDict] | None = None
    ) -> OfficerList:
        """Build from the first page payload and the combined items of all pages."""
        raw_items = items if items is not None else _list(data.get("items"))
        return cls(
            active_count=_int(data.get("active_count")) or 0,
            resigned_count=_int(data.get("resigned_count")) or 0,
            total_results=_int(data.get("total_results")) or 0,
            items=[Officer.from_api(i) for i in raw_items if isinstance(i, dict)],
            etag=_str(data.get("etag")),
        )


@dataclass(frozen=True, kw_only=True)
class Psc(StorableModel):
    """A person with significant control notification."""

    notification_id: str | None = None
    name: str
    kind: str | None = None
    natures_of_control: list[str] = field(default_factory=list)
    notified_on: date | None = None
    ceased_on: date | None = None
    ceased: bool = False
    date_of_birth: DateOfBirth | None = None
    nationality: str | None = None
    country_of_residence: str | None = None
    identification: dict[str, str] = field(default_factory=dict)
    is_sanctioned: bool = False
    etag: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> Psc:
        """Build from a PSC list item."""
        links = _dict(data.get("links"))
        return cls(
            notification_id=last_path_segment(links.get("self")),
            name=str(data.get("name", "")),
            kind=_str(data.get("kind")),
            natures_of_control=[str(n) for n in _list(data.get("natures_of_control"))],
            notified_on=parse_date(data.get("notified_on")),
            ceased_on=parse_date(data.get("ceased_on")),
            ceased=bool(data.get("ceased", False)) or data.get("ceased_on") is not None,
            date_of_birth=DateOfBirth.from_api(data.get("date_of_birth")),
            nationality=_str(data.get("nationality")),
            country_of_residence=_str(data.get("country_of_residence")),
            identification={
                k: v
                for k, v in _dict(data.get("identification")).items()
                if isinstance(v, str)
            },
            is_sanctioned=bool(data.get("is_sanctioned", False)),
            etag=_str(data.get("etag")),
        )

    @property
    def details_hash(self) -> str:
        """Hash of the mutable details, for ``details-changed`` events."""
        return stable_hash(
            self.name,
            self.kind,
            sorted(self.natures_of_control),
            self.notified_on,
            self.ceased_on,
            self.nationality,
            self.country_of_residence,
        )


@dataclass(frozen=True, kw_only=True)
class PscStatement(StorableModel):
    """A PSC statement, such as ``no-individual-or-entity-with-signficant-control``."""

    statement_id: str | None = None
    statement: str | None = None
    notified_on: date | None = None
    ceased_on: date | None = None
    linked_psc_name: str | None = None
    etag: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> PscStatement:
        """Build from a PSC statement list item."""
        links = _dict(data.get("links"))
        return cls(
            statement_id=last_path_segment(links.get("self")),
            statement=_str(data.get("statement")),
            notified_on=parse_date(data.get("notified_on")),
            ceased_on=parse_date(data.get("ceased_on")),
            linked_psc_name=_str(data.get("linked_psc_name")),
            etag=_str(data.get("etag")),
        )


@dataclass(frozen=True, kw_only=True)
class PscData(StorableModel):
    """PSC notifications and statements together."""

    active_count: int = 0
    ceased_count: int = 0
    total_results: int = 0
    items: list[Psc] = field(default_factory=list)
    statements_active_count: int = 0
    statements_total: int = 0
    statements: list[PscStatement] = field(default_factory=list)

    @classmethod
    def from_api(
        cls,
        data: JsonDict,
        statements: JsonDict | None,
        items: list[JsonDict] | None = None,
        statement_items: list[JsonDict] | None = None,
    ) -> PscData:
        """Build from the PSC list and PSC statements list payloads."""
        raw_items = items if items is not None else _list(data.get("items"))
        statements = _dict(statements)
        raw_statements = (
            statement_items
            if statement_items is not None
            else _list(statements.get("items"))
        )
        return cls(
            active_count=_int(data.get("active_count")) or 0,
            ceased_count=_int(data.get("ceased_count")) or 0,
            total_results=_int(data.get("total_results")) or 0,
            items=[Psc.from_api(i) for i in raw_items if isinstance(i, dict)],
            statements_active_count=_int(statements.get("active_count")) or 0,
            statements_total=_int(statements.get("total_results")) or 0,
            statements=[
                PscStatement.from_api(i) for i in raw_statements if isinstance(i, dict)
            ],
        )


@dataclass(frozen=True, kw_only=True)
class Charge(StorableModel):
    """A registered charge (mortgage)."""

    charge_id: str | None = None
    charge_code: str | None = None
    charge_number: int | None = None
    status: str | None = None
    created_on: date | None = None
    delivered_on: date | None = None
    satisfied_on: date | None = None
    acquired_on: date | None = None
    persons_entitled: list[str] = field(default_factory=list)
    classification: str | None = None
    assets_ceased_released: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> Charge:
        """Build from a charge list item."""
        links = _dict(data.get("links"))
        classification = _dict(data.get("classification"))
        return cls(
            charge_id=last_path_segment(links.get("self")) or _str(data.get("id")),
            charge_code=_str(data.get("charge_code")),
            charge_number=_int(data.get("charge_number")),
            status=_str(data.get("status")),
            created_on=parse_date(data.get("created_on")),
            delivered_on=parse_date(data.get("delivered_on")),
            satisfied_on=parse_date(data.get("satisfied_on")),
            acquired_on=parse_date(data.get("acquired_on")),
            persons_entitled=[
                str(p.get("name"))
                for p in _list(data.get("persons_entitled"))
                if isinstance(p, dict) and p.get("name")
            ],
            classification=_str(classification.get("description")),
            assets_ceased_released=_str(data.get("assets_ceased_released")),
        )

    @property
    def is_outstanding(self) -> bool:
        """Return True while the charge is not fully satisfied."""
        return self.status in ("outstanding", "part-satisfied")


@dataclass(frozen=True, kw_only=True)
class ChargeList(StorableModel):
    """The charge list resource."""

    total_count: int = 0
    unfiltered_count: int = 0
    satisfied_count: int = 0
    part_satisfied_count: int = 0
    items: list[Charge] = field(default_factory=list)

    @classmethod
    def from_api(
        cls, data: JsonDict, items: list[JsonDict] | None = None
    ) -> ChargeList:
        """Build from the charge list payload."""
        raw_items = items if items is not None else _list(data.get("items"))
        return cls(
            total_count=_int(data.get("total_count")) or 0,
            unfiltered_count=_int(data.get("unfiltered_count")) or 0,
            satisfied_count=_int(data.get("satisfied_count")) or 0,
            part_satisfied_count=_int(data.get("part_satisfied_count")) or 0,
            items=[Charge.from_api(i) for i in raw_items if isinstance(i, dict)],
        )

    @property
    def outstanding_count(self) -> int:
        """Number of charges still outstanding or part satisfied."""
        return sum(1 for c in self.items if c.is_outstanding)


@dataclass(frozen=True, kw_only=True)
class InsolvencyPractitioner(StorableModel):
    """An insolvency practitioner on a case."""

    name: str
    role: str | None = None
    appointed_on: date | None = None
    ceased_to_act_on: date | None = None


@dataclass(frozen=True, kw_only=True)
class InsolvencyCase(StorableModel):
    """One insolvency case."""

    number: str | None = None
    type: str | None = None
    dates: dict[str, date] = field(default_factory=dict)
    practitioners: list[InsolvencyPractitioner] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: JsonDict) -> InsolvencyCase:
        """Build from an insolvency case payload."""
        dates: dict[str, date] = {}
        for item in _list(data.get("dates")):
            if isinstance(item, dict) and (d := parse_date(item.get("date"))):
                dates[str(item.get("type"))] = d
        return cls(
            number=str(data["number"]) if data.get("number") is not None else None,
            type=_str(data.get("type")),
            dates=dates,
            practitioners=[
                InsolvencyPractitioner(
                    name=str(p.get("name", "")),
                    role=_str(p.get("role")),
                    appointed_on=parse_date(p.get("appointed_on")),
                    ceased_to_act_on=parse_date(p.get("ceased_to_act_on")),
                )
                for p in _list(data.get("practitioners"))
                if isinstance(p, dict)
            ],
            notes=[str(n) for n in _list(data.get("notes"))],
        )


@dataclass(frozen=True, kw_only=True)
class Insolvency(StorableModel):
    """The company insolvency resource. Empty when the company has no history."""

    status: list[str] = field(default_factory=list)
    cases: list[InsolvencyCase] = field(default_factory=list)
    etag: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict | None) -> Insolvency:
        """Build from the insolvency payload, or an empty resource for a 404."""
        data = _dict(data)
        status = data.get("status")
        return cls(
            status=[str(s) for s in status]
            if isinstance(status, list)
            else ([str(status)] if isinstance(status, str) else []),
            cases=[
                InsolvencyCase.from_api(c)
                for c in _list(data.get("cases"))
                if isinstance(c, dict)
            ],
            etag=_str(data.get("etag")),
        )


@dataclass(frozen=True, kw_only=True)
class UkEstablishment(StorableModel):
    """A UK establishment of an overseas company."""

    company_number: str
    company_name: str | None = None
    company_status: str | None = None
    locality: str | None = None


@dataclass(frozen=True, kw_only=True)
class Structure(StorableModel):
    """Registers, exemptions and UK establishments. These almost never change."""

    registers: dict[str, str] = field(default_factory=dict)
    exemptions: list[str] = field(default_factory=list)
    uk_establishments: list[UkEstablishment] = field(default_factory=list)

    @classmethod
    def from_api(
        cls,
        registers: JsonDict | None,
        exemptions: JsonDict | None,
        establishments: JsonDict | None,
    ) -> Structure:
        """Build from the three structure payloads, any of which may be a 404."""
        regs: dict[str, str] = {}
        for key, value in _dict(_dict(registers).get("registers")).items():
            if not isinstance(value, dict):
                continue
            items = _list(value.get("items"))
            latest = items[0] if items and isinstance(items[0], dict) else {}
            regs[key] = str(latest.get("register_moved_to", "unspecified-location"))
        exemption_kinds = [
            str(item.get("exemption_type", key))
            for key, item in _dict(_dict(exemptions).get("exemptions")).items()
            if isinstance(item, dict)
        ]
        ests = [
            UkEstablishment(
                company_number=str(item.get("company_number", "")),
                company_name=_str(item.get("company_name")),
                company_status=_str(item.get("company_status")),
                locality=_str(item.get("locality")),
            )
            for item in _list(_dict(establishments).get("items"))
            if isinstance(item, dict)
        ]
        return cls(registers=regs, exemptions=exemption_kinds, uk_establishments=ests)


@dataclass(frozen=True, kw_only=True)
class Appointment(StorableModel):
    """One appointment held by a tracked officer."""

    appointment_id: str | None = None
    company_number: str
    company_name: str | None = None
    company_status: str | None = None
    officer_role: str | None = None
    appointed_on: date | None = None
    appointed_before: date | None = None
    resigned_on: date | None = None
    nationality: str | None = None
    occupation: str | None = None
    country_of_residence: str | None = None

    @classmethod
    def from_api(cls, data: JsonDict) -> Appointment:
        """Build from an appointment list item."""
        appointed_to = _dict(data.get("appointed_to"))
        links = _dict(data.get("links"))
        return cls(
            appointment_id=last_path_segment(links.get("self")),
            company_number=str(appointed_to.get("company_number", "")),
            company_name=_str(appointed_to.get("company_name")),
            company_status=_str(appointed_to.get("company_status")),
            officer_role=_str(data.get("officer_role")),
            appointed_on=parse_date(data.get("appointed_on")),
            appointed_before=parse_date(data.get("appointed_before")),
            resigned_on=parse_date(data.get("resigned_on")),
            nationality=_str(data.get("nationality")),
            occupation=_str(data.get("occupation")),
            country_of_residence=_str(data.get("country_of_residence")),
        )

    @property
    def key(self) -> str:
        """Stable identity of the appointment for change detection."""
        return (
            self.appointment_id
            or f"{self.company_number}:{self.officer_role}:{self.appointed_on}"
        )

    @property
    def is_active(self) -> bool:
        """Return True while the appointment has not been resigned."""
        return self.resigned_on is None


@dataclass(frozen=True, kw_only=True)
class AppointmentList(StorableModel):
    """The officer appointment list resource."""

    officer_id: str
    name: str
    is_corporate_officer: bool = False
    date_of_birth: DateOfBirth | None = None
    total_results: int = 0
    items: list[Appointment] = field(default_factory=list)
    etag: str | None = None

    @classmethod
    def from_api(
        cls, officer_id: str, data: JsonDict, items: list[JsonDict] | None = None
    ) -> AppointmentList:
        """Build from the first page payload and the combined items of all pages."""
        raw_items = items if items is not None else _list(data.get("items"))
        return cls(
            officer_id=officer_id,
            name=str(data.get("name", "")),
            is_corporate_officer=bool(data.get("is_corporate_officer", False)),
            date_of_birth=DateOfBirth.from_api(data.get("date_of_birth")),
            total_results=_int(data.get("total_results")) or 0,
            items=[Appointment.from_api(i) for i in raw_items if isinstance(i, dict)],
            etag=_str(data.get("etag")),
        )

    @property
    def active(self) -> list[Appointment]:
        """Appointments that have not been resigned."""
        return [a for a in self.items if a.is_active]

    @property
    def resigned(self) -> list[Appointment]:
        """Appointments that have been resigned."""
        return [a for a in self.items if not a.is_active]


@dataclass(frozen=True, kw_only=True)
class DisqualificationMatch(StorableModel):
    """A candidate from the disqualified officers search."""

    title: str
    date_of_birth: DateOfBirth | None = None
    address_snippet: str | None = None
    disqualified_officer_id: str | None = None
    exact: bool = False


@dataclass(frozen=True, kw_only=True)
class DisqualificationResult(StorableModel):
    """Outcome of a disqualification check for a tracked officer."""

    checked_on: date | None = None
    disqualified: bool = False
    possible_matches: list[DisqualificationMatch] = field(default_factory=list)
    disqualified_from: date | None = None
    disqualified_until: date | None = None
    reason: str | None = None
    company_names: list[str] = field(default_factory=list)
