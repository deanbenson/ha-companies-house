"""Persistent change-detection state and dataset snapshots.

One :class:`homeassistant.helpers.storage.Store` per config entry holds, for
every monitored company and officer, the last fetched copy of each dataset
and the bookkeeping needed to tell what changed since. That is what stops a
restart from replaying fifty filings as new events, and what lets a reload
(every subentry add or remove reloads the entry) cost nothing on the API
while the snapshots are still fresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Self

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CHANGE_LOG_CAP,
    DOMAIN,
    LOGGER,
    STORAGE_KEY_TEMPLATE,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    STORE_TRANSACTION_CAP,
)
from .models import JsonDict, parse_date

SAVE_DELAY = 10
RECENT_FILINGS_CAP = 100


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return dt_util.as_utc(parsed) if parsed else None


@dataclass
class DatasetSnapshot:
    """The last fetched copy of one dataset."""

    fetched_at: datetime | None = None
    data: JsonDict | None = None

    def to_dict(self) -> JsonDict:
        """Serialise."""
        return {"fetched_at": _iso(self.fetched_at), "data": self.data}

    @classmethod
    def from_dict(cls, data: JsonDict) -> Self:
        """Deserialise."""
        stored = data.get("data")
        return cls(
            fetched_at=_dt(data.get("fetched_at")),
            data=stored if isinstance(stored, dict) else None,
        )


def remember_change(changes: list[JsonDict], change: JsonDict) -> list[JsonDict]:
    """Return the change log with a new entry at the front, capped."""
    return [change, *changes][:CHANGE_LOG_CAP]


def _changes(data: JsonDict) -> list[JsonDict]:
    return [c for c in data.get("changes") or [] if isinstance(c, dict)][
        :CHANGE_LOG_CAP
    ]


@dataclass
class CompanyState:
    """Everything remembered about one company between fetches."""

    seen_transaction_ids: list[str] = field(default_factory=list)
    recent_filings: list[JsonDict] = field(default_factory=list)
    filings_total_count: int | None = None
    newest_transaction_id: str | None = None
    newest_filing_date: date | None = None
    snapshots: dict[str, DatasetSnapshot] = field(default_factory=dict)
    last_reconciled: datetime | None = None
    last_structure: datetime | None = None
    strike_off_notice_on: date | None = None
    # The last risk rating, so a restart does not announce it again.
    risk_band: str | None = None
    risk_score: int | None = None
    not_found: bool = False
    # Every change detected, newest first, so a report can look back.
    changes: list[JsonDict] = field(default_factory=list)

    def remember_filings(self, transaction_ids: list[str]) -> None:
        """Add transaction ids, newest first, keeping the most recent 500."""
        merged = list(transaction_ids) + [
            t for t in self.seen_transaction_ids if t not in transaction_ids
        ]
        self.seen_transaction_ids = merged[:STORE_TRANSACTION_CAP]

    def to_dict(self) -> JsonDict:
        """Serialise."""
        return {
            "seen_transaction_ids": self.seen_transaction_ids,
            "recent_filings": self.recent_filings[:RECENT_FILINGS_CAP],
            "filings_total_count": self.filings_total_count,
            "newest_transaction_id": self.newest_transaction_id,
            "newest_filing_date": _iso(self.newest_filing_date),
            "snapshots": {k: v.to_dict() for k, v in self.snapshots.items()},
            "last_reconciled": _iso(self.last_reconciled),
            "last_structure": _iso(self.last_structure),
            "strike_off_notice_on": _iso(self.strike_off_notice_on),
            "risk_band": self.risk_band,
            "risk_score": self.risk_score,
            "changes": self.changes[:CHANGE_LOG_CAP],
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> Self:
        """Deserialise."""
        snapshots = data.get("snapshots")
        count = data.get("filings_total_count")
        return cls(
            seen_transaction_ids=[
                str(t) for t in data.get("seen_transaction_ids") or []
            ][:STORE_TRANSACTION_CAP],
            recent_filings=[
                f for f in data.get("recent_filings") or [] if isinstance(f, dict)
            ][:RECENT_FILINGS_CAP],
            filings_total_count=count if isinstance(count, int) else None,
            newest_transaction_id=data.get("newest_transaction_id") or None,
            newest_filing_date=parse_date(data.get("newest_filing_date")),
            snapshots={
                str(k): DatasetSnapshot.from_dict(v)
                for k, v in (snapshots or {}).items()
                if isinstance(v, dict)
            }
            if isinstance(snapshots, dict)
            else {},
            last_reconciled=_dt(data.get("last_reconciled")),
            last_structure=_dt(data.get("last_structure")),
            strike_off_notice_on=parse_date(data.get("strike_off_notice_on")),
            risk_band=band if isinstance(band := data.get("risk_band"), str) else None,
            risk_score=score
            if isinstance(score := data.get("risk_score"), int)
            and not isinstance(score, bool)
            else None,
            changes=_changes(data),
        )


@dataclass
class OfficerState:
    """Everything remembered about one officer between fetches."""

    snapshots: dict[str, DatasetSnapshot] = field(default_factory=dict)
    not_found: bool = False
    changes: list[JsonDict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        """Serialise."""
        return {
            "snapshots": {k: v.to_dict() for k, v in self.snapshots.items()},
            "changes": self.changes[:CHANGE_LOG_CAP],
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> Self:
        """Deserialise."""
        snapshots = data.get("snapshots")
        return cls(
            snapshots={
                str(k): DatasetSnapshot.from_dict(v)
                for k, v in (snapshots or {}).items()
                if isinstance(v, dict)
            }
            if isinstance(snapshots, dict)
            else {},
            changes=_changes(data),
        )


class ChangeStore:
    """The versioned store for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create the store."""
        self._store: Store[JsonDict] = Store(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY_TEMPLATE.format(entry_id=entry_id),
            minor_version=STORAGE_MINOR_VERSION,
        )
        self.companies: dict[str, CompanyState] = {}
        self.officers: dict[str, OfficerState] = {}
        self.loaded = False

    async def async_load(self) -> None:
        """Load from disk. A missing or unreadable file starts empty."""
        data = await self._store.async_load()
        if isinstance(data, dict):
            companies = data.get("companies")
            officers = data.get("officers")
            if isinstance(companies, dict):
                self.companies = {
                    str(k): CompanyState.from_dict(v)
                    for k, v in companies.items()
                    if isinstance(v, dict)
                }
            if isinstance(officers, dict):
                self.officers = {
                    str(k): OfficerState.from_dict(v)
                    for k, v in officers.items()
                    if isinstance(v, dict)
                }
        self.loaded = True
        LOGGER.debug(
            "Loaded %s store: %d companies, %d officers",
            DOMAIN,
            len(self.companies),
            len(self.officers),
        )

    def company(self, company_number: str) -> CompanyState:
        """Return, creating if needed, the state for a company."""
        return self.companies.setdefault(company_number, CompanyState())

    def officer(self, officer_id: str) -> OfficerState:
        """Return, creating if needed, the state for an officer."""
        return self.officers.setdefault(officer_id, OfficerState())

    def forget_company(self, company_number: str) -> None:
        """Drop a company's state."""
        if self.companies.pop(company_number, None) is not None:
            self.save()

    def forget_officer(self, officer_id: str) -> None:
        """Drop an officer's state."""
        if self.officers.pop(officer_id, None) is not None:
            self.save()

    def _data(self) -> JsonDict:
        return {
            "companies": {k: v.to_dict() for k, v in self.companies.items()},
            "officers": {k: v.to_dict() for k, v in self.officers.items()},
        }

    def save(self) -> None:
        """Schedule a delayed save."""
        self._store.async_delay_save(self._data, SAVE_DELAY)

    async def async_save(self) -> None:
        """Save immediately."""
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        """Delete the file when the entry is removed."""
        await self._store.async_remove()
