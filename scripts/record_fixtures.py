"""Record real Companies House payloads as test fixtures.

Run this yourself with a key in the environment; the key never touches the
repository and is redacted from nothing because nothing in a response
contains it:

    CH_API_KEY=... .venv/bin/python scripts/record_fixtures.py \
        --company 12345678:company_active \
        --officer AbCdEf123:officer_many

Each company writes ``tests/fixtures/<name>/{profile,filing_history,officers,
psc,psc_statements,charges,insolvency,registers,exemptions,uk_establishments}.json``
(a 404 writes nothing). Each officer writes ``appointments.json`` and the
disqualified-officer search for their name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.companies_house.api import (  # noqa: E402
    CompaniesHouseClient,
    CompaniesHouseNotFoundError,
    Priority,
)

FIXTURES = ROOT / "tests" / "fixtures"

COMPANY_ENDPOINTS = {
    "profile": "/company/{n}",
    "filing_history": "/company/{n}/filing-history?items_per_page=25",
    "officers": "/company/{n}/officers?items_per_page=100",
    "psc": "/company/{n}/persons-with-significant-control?items_per_page=100",
    "psc_statements": "/company/{n}/persons-with-significant-control-statements",
    "charges": "/company/{n}/charges?items_per_page=100",
    "insolvency": "/company/{n}/insolvency",
    "registers": "/company/{n}/registers",
    "exemptions": "/company/{n}/exemptions",
    "uk_establishments": "/company/{n}/uk-establishments",
}


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path.relative_to(ROOT)}")


async def _record_company(client: CompaniesHouseClient, number: str, name: str) -> None:
    for key, template in COMPANY_ENDPOINTS.items():
        path, _, query = template.format(n=number).partition("?")
        params = dict(p.split("=", 1) for p in query.split("&")) if query else None
        try:
            data = await client.request(
                path, params=params, priority=Priority.ON_DEMAND
            )
        except CompaniesHouseNotFoundError:
            print(f"  {key}: 404 (not written)")
            continue
        _write(FIXTURES / name / f"{key}.json", data)


async def _record_officer(
    client: CompaniesHouseClient, officer_id: str, name: str
) -> None:
    data = await client.request(
        f"/officers/{officer_id}/appointments",
        params={"items_per_page": 100},
        priority=Priority.ON_DEMAND,
    )
    _write(FIXTURES / name / "appointments.json", data)
    search = await client.search_disqualified_officers(str(data.get("name", "")))
    _write(FIXTURES / name / "disqualified_search.json", search)


async def main() -> None:
    """Record the requested fixtures."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", action="append", default=[], metavar="NUMBER:NAME")
    parser.add_argument("--officer", action="append", default=[], metavar="ID:NAME")
    args = parser.parse_args()
    key = os.environ.get("CH_API_KEY")
    if not key:
        sys.exit("Set CH_API_KEY in the environment")
    async with aiohttp.ClientSession() as session:
        client = CompaniesHouseClient(session, key)
        for spec in args.company:
            number, name = spec.split(":", 1)
            await _record_company(client, number, name)
        for spec in args.officer:
            officer_id, name = spec.split(":", 1)
            await _record_officer(client, officer_id, name)


if __name__ == "__main__":
    asyncio.run(main())
