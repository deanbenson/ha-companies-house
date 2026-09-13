"""Build strings.json and translations/en.json from one Python definition.

Custom integrations can't use ``[%key:...]`` references, so the two files are
identical; keeping the source here avoids maintaining them twice.

    .venv/bin/python scripts/build_strings.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_services import SERVICES_STRINGS

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "companies_house"

STATUS = {
    "active": "Active",
    "dissolved": "Dissolved",
    "liquidation": "Liquidation",
    "receivership": "Receiver action",
    "administration": "In administration",
    "voluntary-arrangement": "Voluntary arrangement",
    "converted-closed": "Converted / closed",
    "insolvency-proceedings": "Insolvency proceedings",
    "registered": "Registered",
    "removed": "Removed",
    "closed": "Closed",
    "open": "Open",
}
STATUS_DETAIL = {
    "active": "Active",
    "dissolved": "Dissolved",
    "converted-closed": "Converted / closed",
    "transferred-from-uk": "Transferred from UK",
    "active-proposal-to-strike-off": "Active proposal to strike off",
    "petition-to-restore-dissolved": "Petition to restore dissolved",
    "transformed-to-se": "Transformed to SE",
    "converted-to-plc": "Converted to PLC",
    "converted-to-uk-societas": "Converted to UK Societas",
    "converted-to-ukeig": "Converted to UKEIG",
}
JURISDICTION = {
    "england-wales": "England and Wales",
    "wales": "Wales",
    "scotland": "Scotland",
    "northern-ireland": "Northern Ireland",
    "european-union": "European Union",
    "united-kingdom": "United Kingdom",
    "england": "England",
    "noneu": "Foreign (non EU)",
}
TIERS = {
    "close_watch": "Close watch",
    "deadline": "Deadline within 30 days",
    "normal": "Normal",
    "quiet": "Quiet",
    "dissolved": "Dissolved",
}

COMPANY_SENSORS = {
    "next_deadline": "Next deadline",
    "next_deadline_type": "Next deadline type",
    "days_to_next_deadline": "Days to next deadline",
    "accounts_next_due": "Accounts next due",
    "confirmation_statement_next_due": "Confirmation statement next due",
    "company_status": "Company status",
    "last_filing_date": "Last filing date",
    "last_filing_description": "Last filing",
    "officers_active": "Officers active",
    "psc_active": "PSC active",
    "charges_outstanding": "Charges outstanding",
    "company_name": "Company name",
    "company_status_detail": "Company status detail",
    "company_type": "Company type",
    "company_subtype": "Company subtype",
    "jurisdiction": "Jurisdiction",
    "date_of_creation": "Date of creation",
    "date_of_cessation": "Date of cessation",
    "registered_office_address": "Registered office address",
    "polling_tier": "Polling tier",
    "company_age": "Company age",
    "accounts_next_made_up_to": "Accounts next made up to",
    "accounts_last_made_up_to": "Accounts last made up to",
    "accounts_next_period_start": "Accounts next period start",
    "accounting_reference_date": "Accounting reference date",
    "last_accounts_type": "Last accounts type",
    "confirmation_statement_next_made_up_to": "Confirmation statement next made up to",
    "confirmation_statement_last_made_up_to": "Confirmation statement last made up to",
    "days_to_accounts_due": "Days to accounts due",
    "days_to_confirmation_statement_due": "Days to confirmation statement due",
    "sic_codes": "SIC codes",
    "primary_sic_description": "Primary SIC description",
    "officers_total": "Officers total",
    "officers_resigned": "Officers resigned",
    "directors_active": "Directors active",
    "secretaries_active": "Secretaries active",
    "llp_members_active": "LLP members active",
    "psc_total": "PSC total",
    "psc_statements": "PSC statements",
    "charges_total": "Charges total",
    "charges_part_satisfied": "Charges part satisfied",
    "charges_satisfied": "Charges satisfied",
    "filings_total": "Filings total",
    "filings_last_12_months": "Filings last 12 months",
    "days_since_last_filing": "Days since last filing",
    "last_filing_category": "Last filing category",
    "previous_names_count": "Previous names",
    "uk_establishments_count": "UK establishments",
    "insolvency_cases_count": "Insolvency cases",
    "registers_held": "Registers held",
}
OFFICER_SENSORS = {
    "appointments_active": "Appointments active",
    "appointments_total": "Appointments total",
    "most_recent_company": "Most recent company",
    "last_appointment_date": "Last appointment date",
    "appointments_resigned": "Appointments resigned",
    "first_appointment_date": "First appointment date",
    "nationality": "Nationality",
    "country_of_residence": "Country of residence",
    "occupation": "Occupation",
    "date_of_birth": "Date of birth",
    "officer_role": "Officer role",
}
SERVICE_SENSORS = {
    "requests_used": "Requests used this window",
    "requests_remaining": "Requests remaining",
    "budget_used": "Budget used",
    "window_resets_at": "Window resets at",
    "last_successful_update": "Last successful update",
    "last_error": "Last error",
    "companies_monitored": "Companies monitored",
    "officers_monitored": "Officers monitored",
    "next_scheduled_probe": "Next scheduled probe",
}
COMPANY_BINARY = {
    "accounts_overdue": "Accounts overdue",
    "confirmation_statement_overdue": "Confirmation statement overdue",
    "accounts_due_soon": "Accounts due soon",
    "confirmation_statement_due_soon": "Confirmation statement due soon",
    "proposed_strike_off": "Proposed strike off",
    "insolvent": "Insolvent",
    "registered_office_in_dispute": "Registered office in dispute",
    "undeliverable_registered_office": "Undeliverable registered office",
    "is_active": "Is active",
    "can_file": "Can file",
    "has_outstanding_charges": "Has outstanding charges",
    "has_insolvency_history": "Has insolvency history",
    "has_super_secure_officers": "Has super secure officers",
    "has_exemptions": "Has exemptions",
}
OFFICER_BINARY = {
    "disqualified": "Disqualified",
    "has_active_appointments": "Has active appointments",
}
EVENTS = {
    "filing": (
        "Filing",
        {
            "accounts": "Accounts",
            "confirmation-statement": "Confirmation statement",
            "officers": "Officers",
            "persons-with-significant-control": "Persons with significant control",
            "address": "Address",
            "capital": "Capital",
            "charges": "Charges",
            "mortgage": "Mortgage",
            "change-of-name": "Change of name",
            "resolution": "Resolution",
            "incorporation": "Incorporation",
            "gazette": "Gazette",
            "insolvency": "Insolvency",
            "dissolution": "Dissolution",
            "other": "Other",
        },
    ),
    "officer_change": (
        "Officer change",
        {
            "appointed": "Appointed",
            "resigned": "Resigned",
            "details-changed": "Details changed",
        },
    ),
    "psc_change": (
        "PSC change",
        {
            "notified": "Notified",
            "ceased": "Ceased",
            "statement-added": "Statement added",
            "details-changed": "Details changed",
        },
    ),
    "charge_change": (
        "Charge change",
        {
            "created": "Created",
            "satisfied": "Satisfied",
            "part-satisfied": "Part satisfied",
            "acquired": "Acquired",
        },
    ),
    "status_change": (
        "Status change",
        {
            "status-changed": "Status changed",
            "strike-off-proposed": "Strike off proposed",
            "strike-off-discontinued": "Strike off discontinued",
            "dissolved": "Dissolved",
        },
    ),
    "profile_change": (
        "Profile change",
        {
            "name-changed": "Name changed",
            "address-changed": "Address changed",
            "sic-changed": "SIC codes changed",
            "accounting-reference-date-changed": "Accounting reference date changed",
        },
    ),
    "appointment": (
        "Appointment",
        {
            "appointed": "Appointed",
            "resigned": "Resigned",
            "company-status-changed": "Company status changed",
            "disqualified": "Disqualified",
        },
    ),
}


def sensor_entry(name: str, state: dict[str, str] | None = None) -> dict:
    """Return one entity translation entry."""
    entry: dict = {"name": name}
    if state:
        entry["state"] = state
    return entry


def build() -> dict:
    """Assemble the whole strings document."""
    sensors = {
        k: sensor_entry(v)
        for k, v in {**COMPANY_SENSORS, **OFFICER_SENSORS, **SERVICE_SENSORS}.items()
    }
    sensors["company_status"]["state"] = STATUS
    sensors["company_status_detail"]["state"] = STATUS_DETAIL
    sensors["jurisdiction"]["state"] = JURISDICTION
    sensors["polling_tier"]["state"] = TIERS
    sensors["next_deadline_type"]["state"] = {
        "accounts": "Accounts",
        "confirmation_statement": "Confirmation statement",
    }

    events = {}
    for key, (name, types) in EVENTS.items():
        events[key] = {
            "name": name,
            "state_attributes": {"event_type": {"state": types}},
        }

    api_key_desc = "A REST API key for an application registered on the Companies House developer hub."
    return {
        "title": "Companies House",
        "config": {
            "step": {
                "user": {
                    "title": "Connect to Companies House",
                    "description": "Enter the API key of an application registered at https://developer.company-information.service.gov.uk/. One key is one budget: 600 requests per 5 minutes.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
                "reauth_confirm": {
                    "title": "Reauthenticate Companies House",
                    "description": "The API key was rejected. Enter a working key.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
                "reconfigure": {
                    "title": "Replace the API key",
                    "description": "Enter a new key. Monitored companies and officers are kept.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
            },
            "abort": {
                "already_configured": "This API key is already configured.",
                "reauth_successful": "Reauthentication was successful.",
                "reconfigure_successful": "The API key was replaced.",
            },
            "error": {
                "invalid_auth": "The API key was rejected.",
                "rate_limited": "The API is rate limiting this key. Try again in a few minutes.",
                "cannot_connect": "Could not connect to Companies House.",
                "unknown": "Unexpected error.",
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": "Companies House options",
                    "description": "Projected scheduled requests per 5 minute window with these settings: {projected} (the flow refuses more than {limit}).",
                    "data": {
                        "due_soon_days": "Due soon threshold",
                        "document_directory": "Document directory",
                        "max_pages": "Maximum pages per list",
                        "cadence_multiplier": "Cadence multiplier",
                    },
                    "data_description": {
                        "due_soon_days": "Days before a deadline at which the due soon sensors turn on.",
                        "document_directory": "Where download_document writes files. A folder per company is created inside it.",
                        "max_pages": "Cap on pages fetched for long officer, PSC and charge lists (100 items per page).",
                        "cadence_multiplier": "Multiply every polling interval, for people who want everything slower.",
                    },
                }
            },
            "error": {
                "budget_exceeded": "This configuration would exceed the request budget. Increase the cadence multiplier or monitor fewer companies.",
            },
        },
        "config_subentries": {
            "company": {
                "entry_type": "Company",
                "initiate_flow": {
                    "user": "Add company",
                    "reconfigure": "Reconfigure company",
                },
                "step": {
                    "user": {
                        "title": "Find a company",
                        "description": "Search the register by name, or by postcode for the company at an address.",
                        "data": {"query": "Company name", "postcode": "Postcode"},
                        "data_description": {
                            "query": "Part of the company name.",
                            "postcode": "Optional. Narrows the search to companies registered at this postcode.",
                        },
                    },
                    "select": {
                        "title": "Choose the company",
                        "data": {"selection": "Company"},
                    },
                    "confirm": {
                        "title": "Monitor {company}",
                        "description": "Company number {number}. Choose what to monitor.",
                        "data": {
                            "datasets": "Datasets",
                            "close_watch": "Close watch",
                            "label": "Label",
                        },
                        "data_description": {
                            "datasets": "Profile and filing history are always monitored. Untick datasets you do not need.",
                            "close_watch": "Probe every 15 minutes in business hours instead of the adaptive cadence.",
                            "label": "Optional note shown in the subentry title, such as the address or your relationship to the company.",
                        },
                    },
                    "reconfigure": {
                        "title": "Reconfigure company",
                        "menu_options": {
                            "settings": "Change settings",
                            "track_officer": "Track an officer of this company",
                        },
                    },
                    "settings": {
                        "title": "Settings for {company}",
                        "description": "Company number {number}.",
                        "data": {
                            "datasets": "Datasets",
                            "close_watch": "Close watch",
                            "label": "Label",
                        },
                        "data_description": {
                            "datasets": "Profile and filing history are always monitored. Untick datasets you do not need.",
                            "close_watch": "Probe every 15 minutes in business hours instead of the adaptive cadence.",
                            "label": "Optional note shown in the subentry title.",
                        },
                    },
                    "track_officer": {
                        "title": "Track an officer of {company}",
                        "description": "Pick a current officer to create an officer subentry in one step.",
                        "data": {"selection": "Officer"},
                    },
                },
                "error": {
                    "cannot_connect": "Could not connect to Companies House.",
                    "no_results": "No companies matched. Try a different name or postcode.",
                },
                "abort": {
                    "already_configured": "This company is already monitored.",
                    "invalid_auth": "The API key was rejected. Reauthenticate the integration first.",
                    "officer_added": "{name} is now tracked as an officer.",
                    "officer_already_configured": "That officer is already tracked.",
                    "no_officers": "This company has no current officers on the register.",
                    "reconfigure_successful": "The company settings were updated.",
                },
            },
            "officer": {
                "entry_type": "Officer",
                "initiate_flow": {
                    "user": "Add officer",
                    "reconfigure": "Reconfigure officer",
                },
                "step": {
                    "user": {
                        "title": "Find an officer",
                        "description": "Names collide constantly; the month and year of birth in the results is what tells people apart.",
                        "data": {"query": "Officer name"},
                    },
                    "select": {
                        "title": "Choose the officer",
                        "data": {"selection": "Officer"},
                    },
                    "reconfigure": {
                        "title": "Reconfigure officer",
                        "description": "Officer id {officer_id}. The register name is used once appointments load; this is the fallback.",
                        "data": {"officer_name": "Display name"},
                    },
                },
                "error": {
                    "cannot_connect": "Could not connect to Companies House.",
                    "no_results": "No officers matched.",
                },
                "abort": {
                    "already_configured": "This officer is already tracked.",
                    "invalid_auth": "The API key was rejected. Reauthenticate the integration first.",
                    "reconfigure_successful": "The officer was updated.",
                },
            },
        },
        "selector": {
            "datasets": {
                "options": {
                    "officers": "Officers",
                    "psc": "Persons with significant control",
                    "charges": "Charges",
                    "insolvency": "Insolvency",
                    "structure": "Registers, exemptions and UK establishments",
                }
            }
        },
        "entity": {
            "sensor": sensors,
            "binary_sensor": {
                k: {"name": v} for k, v in {**COMPANY_BINARY, **OFFICER_BINARY}.items()
            },
            "event": events,
            "calendar": {
                "deadlines": {"name": "Deadlines"},
                "all_deadlines": {"name": "All deadlines"},
            },
        },
        "exceptions": {
            "invalid_auth": {"message": "The Companies House API key was rejected."},
            "cannot_connect": {"message": "Could not reach Companies House: {error}"},
            "rate_limited": {"message": "Companies House is rate limiting requests."},
            "budget_exhausted": {
                "message": "The scheduled request budget for this window is spent."
            },
            "not_found": {"message": "The resource no longer exists on the register."},
            "no_entry": {"message": "Companies House is not set up."},
            "entry_not_loaded": {
                "message": "The Companies House integration is not loaded."
            },
            "invalid_company_number": {
                "message": "{company_number} is not a valid company number."
            },
            "invalid_kind": {"message": "{kind} is not a valid kind."},
            "invalid_target": {
                "message": "The target is not a Companies House device or config entry."
            },
            "invalid_date": {
                "message": "{value} is not a valid date (use YYYY-MM-DD)."
            },
            "api_error": {"message": "Companies House returned an error: {error}"},
            "document_write_failed": {
                "message": "Could not write the document: {error}"
            },
            "path_not_allowed": {
                "message": "{path} is not an allowed directory. Add it to allowlist_external_dirs or use a media directory."
            },
        },
        "services": SERVICES_STRINGS,
        "issues": {
            "invalid_auth": {
                "title": "Companies House API key rejected",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Reauthenticate",
                            "description": "The API key was rejected. Reauthenticate the integration to continue monitoring.",
                        }
                    }
                },
            },
            "rate_limited": {
                "title": "Companies House is throttling requests",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Persistent rate limiting",
                            "description": "The API has returned 429 for more than three windows. Something else is using this key, or the configuration is too busy. Confirm to raise the cadence multiplier by one.",
                        }
                    }
                },
            },
            "company_dissolved": {
                "title": "{company} ({number}) has been dissolved",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Company dissolved",
                            "description": "{company} ({number}) is dissolved or removed from the register. It is polled monthly now; confirm to stop monitoring it and remove its device.",
                        }
                    }
                },
            },
            "company_not_found": {
                "title": "{number} is not on the register",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Company not found",
                            "description": "Companies House returns not found for company {number}. Confirm to stop monitoring it and remove its device.",
                        }
                    }
                },
            },
            "officer_not_found": {
                "title": "Officer {name} no longer resolves",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Officer not found",
                            "description": "The appointments for officer id {officer_id} ({name}) no longer exist. Confirm to stop tracking and remove the device.",
                        }
                    }
                },
            },
        },
    }


def main() -> None:
    """Write both files."""
    data = build()
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    (ROOT / "strings.json").write_text(text)
    (ROOT / "translations" / "en.json").write_text(text)
    print(f"wrote strings.json ({len(text)} bytes)")


if __name__ == "__main__":
    main()
