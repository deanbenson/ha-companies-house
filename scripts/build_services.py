"""Build services.yaml and the ``services`` strings section from one table.

    .venv/bin/python scripts/build_services.py

``build_strings.py`` imports ``SERVICES_STRINGS`` from here, so run this first
(or run both) after changing an action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "companies_house"

TEXT = {"text": {}}
NUMBER = {"number": {"min": 1, "max": 100, "mode": "box"}}
START = {"number": {"min": 0, "max": 10000, "mode": "box"}}
DATE = {"date": {}}
BOOL = {"boolean": {}}
ENTRY = (
    "Config entry",
    "Optional. Which Companies House account to use when more than one is configured.",
    {"config_entry": {"integration": "companies_house"}},
)
COMPANY = (
    "Company number",
    "The 8 character company number, for example 12345678 or SC123456.",
    TEXT,
)
QUERY = ("Query", "Search text.", TEXT)
ITEMS = ("Items per page", "Number of results to return (1 to 100).", NUMBER)
START_INDEX = ("Start index", "Offset into the result set for paging.", START)

# Each action maps to (title, description, fields); each field to
# (name, description, selector[, required[, example]]).
ACTIONS: dict[str, tuple[str, str, dict[str, tuple[Any, ...]]]] = {
    "search_companies": (
        "Search companies",
        "Search the register for companies by name.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Example Trading"),
            "items_per_page": ITEMS,
            "start_index": START_INDEX,
        },
    ),
    "advanced_search": (
        "Advanced company search",
        "Search companies by name, location, SIC codes, status, type and incorporation or dissolution dates.",
        {
            "config_entry_id": ENTRY,
            "company_name_includes": (
                "Name includes",
                "Words the company name must contain.",
                TEXT,
            ),
            "company_name_excludes": (
                "Name excludes",
                "Words the company name must not contain.",
                TEXT,
            ),
            "location": (
                "Location",
                "Matches the registered office address, for example a town or postcode.",
                TEXT,
            ),
            "postcode": ("Postcode", "Registered office postcode.", TEXT),
            "sic_codes": (
                "SIC codes",
                "One or more SIC codes.",
                {"text": {"multiple": True}},
            ),
            "company_status": (
                "Company status",
                "One or more statuses, for example active or dissolved.",
                {
                    "select": {
                        "multiple": True,
                        "custom_value": True,
                        "options": [
                            "active",
                            "dissolved",
                            "open",
                            "closed",
                            "converted-closed",
                            "receivership",
                            "administration",
                            "liquidation",
                            "insolvency-proceedings",
                            "voluntary-arrangement",
                            "registered",
                            "removed",
                        ],
                    }
                },
            ),
            "company_type": (
                "Company type",
                "One or more company types, for example ltd or llp.",
                {"text": {"multiple": True}},
            ),
            "incorporated_from": (
                "Incorporated from",
                "Earliest incorporation date.",
                DATE,
            ),
            "incorporated_to": ("Incorporated to", "Latest incorporation date.", DATE),
            "dissolved_from": ("Dissolved from", "Earliest dissolution date.", DATE),
            "dissolved_to": ("Dissolved to", "Latest dissolution date.", DATE),
            "size": (
                "Size",
                "Number of results to return.",
                {"number": {"min": 1, "max": 5000, "mode": "box"}},
            ),
        },
    ),
    "alphabetical_search": (
        "Alphabetical company search",
        "Companies alphabetically around a name.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Pulse"),
            "search_above": (
                "Search above",
                "Company name to page upwards from.",
                TEXT,
            ),
            "search_below": (
                "Search below",
                "Company name to page downwards from.",
                TEXT,
            ),
            "size": ("Size", "Number of results to return.", NUMBER),
        },
    ),
    "dissolved_search": (
        "Dissolved company search",
        "Search dissolved companies.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Pulse"),
            "search_type": (
                "Search type",
                "How to search.",
                {
                    "select": {
                        "options": [
                            "alphabetical",
                            "best-match",
                            "previous-name-dissolved",
                        ]
                    }
                },
            ),
            "start_index": START_INDEX,
            "size": ("Size", "Number of results to return.", NUMBER),
        },
    ),
    "search_officers": (
        "Search officers",
        "Search the register for officers by name.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Jane Smith"),
            "items_per_page": ITEMS,
            "start_index": START_INDEX,
        },
    ),
    "search_disqualified_officers": (
        "Search disqualified officers",
        "Search the disqualified directors register by name. Names collide; check the date of birth.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Jane Smith"),
            "items_per_page": ITEMS,
        },
    ),
    "search_all": (
        "Search everything",
        "Search companies, officers and disqualified officers together.",
        {
            "config_entry_id": ENTRY,
            "query": (*QUERY, True, "Smith"),
            "items_per_page": ITEMS,
        },
    ),
    "get_company": (
        "Get company",
        "Get a company profile.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_registered_office": (
        "Get registered office",
        "Get a company's registered office address.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_officers": (
        "Get officers",
        "List a company's officers.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "register_view": (
                "Register view",
                "Only officers on the public register.",
                BOOL,
            ),
            "order_by": (
                "Order by",
                "Sort order.",
                {"select": {"options": ["appointed_on", "resigned_on", "surname"]}},
            ),
            "items_per_page": ITEMS,
        },
    ),
    "get_officer_appointment": (
        "Get officer appointment",
        "Get one appointment on a company.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "appointment_id": (
                "Appointment id",
                "From the officer list links.",
                TEXT,
                True,
            ),
        },
    ),
    "get_officer_appointments": (
        "Get officer appointments",
        "List every appointment an officer holds across every company.",
        {
            "config_entry_id": ENTRY,
            "officer_id": (
                "Officer id",
                "From the officer's appointments link, not the appointment id.",
                TEXT,
                True,
            ),
        },
    ),
    "get_disqualification": (
        "Get disqualification",
        "Get a disqualified officer record.",
        {
            "config_entry_id": ENTRY,
            "officer_id": (
                "Officer id",
                "The disqualified officer id from a search result.",
                TEXT,
                True,
            ),
            "kind": (
                "Kind",
                "Natural person or corporate officer.",
                {"select": {"options": ["natural", "corporate"]}},
            ),
        },
    ),
    "get_filing_history": (
        "Get filing history",
        "List a company's filings, newest first, with rendered descriptions.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "category": (
                "Category",
                "Comma separated categories to filter by, for example accounts,officers.",
                TEXT,
            ),
            "items_per_page": ITEMS,
            "start_index": START_INDEX,
        },
    ),
    "get_filing": (
        "Get filing",
        "Get one filing.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "transaction_id": (
                "Transaction id",
                "From the filing history or a filing event.",
                TEXT,
                True,
            ),
        },
    ),
    "get_charges": (
        "Get charges",
        "List a company's charges.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_charge": (
        "Get charge",
        "Get one charge.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "charge_id": ("Charge id", "From the charge list links.", TEXT, True),
        },
    ),
    "get_psc": (
        "Get PSCs",
        "List persons with significant control.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_psc_detail": (
        "Get PSC detail",
        "Get one PSC notification.",
        {
            "config_entry_id": ENTRY,
            "company_number": (*COMPANY, True, "12345678"),
            "kind": (
                "Kind",
                "The kind of PSC, which selects the endpoint.",
                {
                    "select": {
                        "options": [
                            "individual",
                            "corporate-entity",
                            "legal-person",
                            "super-secure",
                            "individual-beneficial-owner",
                            "corporate-entity-beneficial-owner",
                            "legal-person-beneficial-owner",
                            "super-secure-beneficial-owner",
                        ]
                    }
                },
                True,
            ),
            "notification_id": (
                "Notification id",
                "From the PSC list links.",
                TEXT,
                True,
            ),
        },
    ),
    "get_psc_statements": (
        "Get PSC statements",
        "List PSC statements.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_insolvency": (
        "Get insolvency",
        "Get insolvency cases.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_exemptions": (
        "Get exemptions",
        "Get exemptions.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_registers": (
        "Get registers",
        "Get registers.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "12345678")},
    ),
    "get_uk_establishments": (
        "Get UK establishments",
        "List an overseas company's UK establishments.",
        {"config_entry_id": ENTRY, "company_number": (*COMPANY, True, "FC012345")},
    ),
    "get_document_metadata": (
        "Get document metadata",
        "Get metadata for a filed document.",
        {
            "config_entry_id": ENTRY,
            "document_id": (
                "Document id",
                "From a filing's document link or a filing event.",
                TEXT,
                True,
            ),
        },
    ),
    "download_document": (
        "Download document",
        "Download a filed document into the document directory, named by filing date and description. Never overwrites.",
        {
            "config_entry_id": ENTRY,
            "document_id": (
                "Document id",
                "From a filing's document link or a filing event.",
                TEXT,
                True,
            ),
            "content_type": (
                "Content type",
                "Format to request.",
                {
                    "select": {
                        "options": [
                            "application/pdf",
                            "application/xhtml+xml",
                            "application/xml",
                            "text/csv",
                            "application/json",
                            "application/zip",
                        ]
                    }
                },
            ),
            "company_number": (*COMPANY, False),
            "transaction_id": (
                "Transaction id",
                "With a company number, fetches the filing for the date and description.",
                TEXT,
            ),
            "description": (
                "Description",
                "Overrides the description in the filename.",
                TEXT,
            ),
            "filing_date": ("Filing date", "Overrides the date in the filename.", DATE),
            "filename": ("Filename", "Overrides the whole filename.", TEXT),
        },
    ),
    "refresh": (
        "Refresh",
        "Refresh monitored companies and officers now, using the on demand budget.",
        {
            "config_entry_id": ENTRY,
            "device_id": (
                "Device",
                "Company or officer devices to refresh. All when empty.",
                {"device": {"integration": "companies_house", "multiple": True}},
            ),
            "datasets": (
                "Datasets",
                "Which datasets to refresh. All when empty.",
                {
                    "select": {
                        "multiple": True,
                        "options": [
                            "profile",
                            "filings",
                            "officers",
                            "psc",
                            "charges",
                            "insolvency",
                            "structure",
                        ],
                    }
                },
            ),
        },
    ),
}


def services_yaml() -> dict[str, Any]:
    """Return the services.yaml document."""
    out: dict[str, Any] = {}
    for name, (_, _, fields) in ACTIONS.items():
        spec: dict[str, Any] = {"fields": {}}
        for field, definition in fields.items():
            selector = definition[2]
            entry: dict[str, Any] = {"selector": selector}
            if len(definition) > 3 and definition[3]:
                entry["required"] = True
            if len(definition) > 4:
                entry["example"] = definition[4]
            spec["fields"][field] = entry
        out[name] = spec
    return out


SERVICES_STRINGS: dict[str, Any] = {
    name: {
        "name": title,
        "description": description,
        "fields": {
            field: {"name": d[0], "description": d[1]} for field, d in fields.items()
        },
    }
    for name, (title, description, fields) in ACTIONS.items()
}


def main() -> None:
    """Write services.yaml."""
    path = ROOT / "services.yaml"
    path.write_text(
        yaml.safe_dump(services_yaml(), sort_keys=False, allow_unicode=True)
    )
    print(f"wrote {path} ({path.stat().st_size} bytes, {len(ACTIONS)} actions)")


if __name__ == "__main__":
    main()
