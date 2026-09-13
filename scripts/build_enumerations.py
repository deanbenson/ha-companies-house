"""Generate ``enumerations.py`` from the Companies House api-enumerations repository.

The integration ships the description mappings as constants, so that a filing
description key such as ``confirmation-statement-with-updates`` can be rendered
without a network round trip. Re-run this script to refresh them:

    .venv/bin/python scripts/build_enumerations.py

Source: https://github.com/companieshouse/api-enumerations (master branch).
"""

from __future__ import annotations

import json
from pathlib import Path
import pprint
import urllib.request

import yaml

RAW = "https://raw.githubusercontent.com/companieshouse/api-enumerations/master/"
API = "https://api.github.com/repos/companieshouse/api-enumerations/commits/master"
OUT = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "companies_house"
    / "enumerations.py"
)

FILES = {
    "filing_history_descriptions.yml": None,
    "constants.yml": None,
    "psc_descriptions.yml": None,
    "mortgage_descriptions.yml": None,
    "disqualified_officer_descriptions.yml": None,
}


def _fetch(name: str) -> str:
    with urllib.request.urlopen(RAW + name, timeout=30) as resp:
        return resp.read().decode()


def _commit_sha() -> str:
    try:
        with urllib.request.urlopen(API, timeout=30) as resp:
            return str(json.load(resp)["sha"])
    except OSError:
        return "unknown"


def _section(doc: dict[str, object], key: str) -> dict[str, str]:
    value = doc.get(key) or {}
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def main() -> None:
    """Fetch the YAML files and write the Python module."""
    docs = {name: yaml.safe_load(_fetch(name)) for name in FILES}
    sha = _commit_sha()

    filing = docs["filing_history_descriptions.yml"]
    constants = docs["constants.yml"]
    psc = docs["psc_descriptions.yml"]
    mortgage = docs["mortgage_descriptions.yml"]
    disq = docs["disqualified_officer_descriptions.yml"]

    tables = {
        "FILING_DESCRIPTIONS": _section(filing, "description"),
        "COMPANY_STATUS": _section(constants, "company_status"),
        "COMPANY_STATUS_DETAIL": _section(constants, "company_status_detail"),
        "COMPANY_TYPE": _section(constants, "company_type"),
        "COMPANY_SUBTYPE": _section(constants, "company_subtype"),
        "JURISDICTION": _section(constants, "jurisdiction"),
        "OFFICER_ROLE": _section(constants, "officer_role"),
        "ACCOUNT_TYPE": _section(constants, "account_type"),
        "INSOLVENCY_CASE_TYPE": _section(constants, "insolvency_case_type"),
        "INSOLVENCY_CASE_DATE_TYPE": _section(constants, "insolvency_case_date_type"),
        "REGISTER_TYPES": _section(constants, "register_types"),
        "REGISTER_LOCATIONS": _section(constants, "register_locations"),
        "IDENTIFICATION_TYPE": _section(constants, "identification_type"),
        "SIC_DESCRIPTIONS": _section(constants, "sic_descriptions"),
        "PSC_NATURES_OF_CONTROL": _section(psc, "description"),
        "PSC_STATEMENT_DESCRIPTIONS": _section(psc, "statement_description"),
        "CHARGE_STATUS": _section(mortgage, "status"),
        "CHARGE_CLASSIFICATION": _section(mortgage, "classificationDesc"),
        "DISQUALIFICATION_REASONS": _section(disq, "description_identifier"),
    }

    lines = [
        '"""Companies House enumerations, generated from the api-enumerations repository.',
        "",
        "Do not edit by hand. Regenerate with ``scripts/build_enumerations.py``.",
        f"Source commit: {sha}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Final",
        "",
    ]
    for name, table in tables.items():
        body = pprint.pformat(table, width=88, sort_dicts=True)
        lines.append(f"{name}: Final[dict[str, str]] = {body}")
        lines.append("")
    OUT.write_text("\n".join(lines))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, commit {sha})")
    for name, table in tables.items():
        print(f"  {name}: {len(table)} entries")


if __name__ == "__main__":
    main()
