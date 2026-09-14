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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_services import SERVICES_STRINGS

from custom_components.companies_house.enumerations import COMPANY_SUBTYPE, COMPANY_TYPE

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
    "close_watch": "Every 15 minutes in office hours (close watch)",
    "deadline": "Every 30 minutes in office hours (deadline near)",
    "normal": "Every 2 hours in office hours",
    "quiet": "Every 6 hours (nothing due, nothing filed lately)",
    "dissolved": "Monthly (dissolved)",
}

COMPANY_SENSORS = {
    "next_deadline": "Next deadline",
    "next_deadline_type": "Next deadline type",
    "days_to_next_deadline": "Days to next deadline",
    "accounts_next_due": "Accounts next due",
    "confirmation_statement_next_due": "Confirmation statement next due",
    "company_status": "Company status",
    "strike_off_earliest_on": "Earliest strike-off date",
    "days_to_object": "Days to object to strike-off",
    "last_filing_date": "Last filing date",
    "last_filing_description": "Last filing",
    "officers_active": "Officers active",
    "psc_active": "People with significant control",
    "charges_outstanding": "Outstanding charges",
    "company_name": "Company name",
    "company_status_detail": "Company status detail",
    "company_type": "Company type",
    "company_subtype": "Company subtype",
    "jurisdiction": "Jurisdiction",
    "date_of_creation": "Date of creation",
    "date_of_cessation": "Date of cessation",
    "registered_office_address": "Registered office address",
    "polling_tier": "How often it is checked",
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
    "psc_total": "People with significant control, ever",
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
    "current_companies": "Current companies",
    "appointments_active": "Appointments active",
    "appointments_total": "Appointments total",
    "most_recent_company": "Most recent company",
    "last_appointment_date": "Last appointment date",
    "appointments_resigned": "Appointments resigned",
    "first_appointment_date": "First appointment date",
    "register_records": "Register records",
    "nationality": "Nationality",
    "country_of_residence": "Country of residence",
    "occupation": "Occupation",
    "date_of_birth": "Date of birth",
    "officer_role": "Officer role",
}
SERVICE_SENSORS = {
    "requests_used": "API requests used in the last 5 minutes",
    "requests_remaining": "API requests remaining",
    "budget_used": "API allowance used",
    "window_resets_at": "API allowance resets at",
    "last_successful_update": "Last successful check",
    "last_error": "Last error",
    "companies_monitored": "Companies monitored",
    "officers_monitored": "Officers monitored",
    "next_scheduled_probe": "Next check",
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
    "has_super_secure_officers": "Has officers with protected details",
    "has_exemptions": "Has exemptions",
}
OFFICER_BINARY = {
    "disqualified": "Disqualified",
    "has_active_appointments": "Currently holds appointments",
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
            "strike-off-suspended": "Strike off suspended",
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
            "new-record": "New register record found",
            "company-now-watched": "Company now watched",
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
    # Enum states must be translation-key safe; the API keys already are.
    sensors["company_type"]["state"] = {k: v.strip() for k, v in COMPANY_TYPE.items()}
    sensors["company_subtype"]["state"] = {
        k: v.strip() for k, v in COMPANY_SUBTYPE.items()
    }
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
                    "description": "Paste the API key from your Companies House developer hub application ({developer_hub}). Each key may make 600 requests every 5 minutes. This integration uses a small fraction of that.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
                "reauth_confirm": {
                    "title": "Reauthenticate Companies House",
                    "description": "Companies House rejected your saved API key. Paste a new one. Everything you are watching carries on where it left off.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
                "reconfigure": {
                    "title": "Replace the API key",
                    "description": "Paste a new key. Everything you are watching is kept.",
                    "data": {"api_key": "API key"},
                    "data_description": {"api_key": api_key_desc},
                },
            },
            "abort": {
                "already_configured": "That API key is already set up.",
                "reauth_successful": "The new key works. Monitoring has resumed.",
                "reconfigure_successful": "The API key was replaced.",
            },
            "error": {
                "invalid_auth": "Companies House rejected that key. Check you copied all of it, and that it is a live REST key, not a test one.",
                "rate_limited": "Companies House is asking this key to slow down. Try again in a few minutes.",
                "cannot_connect": "Could not reach Companies House. Check your connection and try again.",
                "unknown": "Something unexpected went wrong.",
            },
        },
        "options": {
            "step": {
                "init": {
                    "title": "Companies House options",
                    "description": "Companies House allows 600 requests every 5 minutes. With these settings, checks will use about {projected} of them. You cannot save settings that would need more than {limit}.",
                    "data": {
                        "due_soon_days": "Warn this many days before a deadline",
                        "document_directory": "Folder for downloaded documents",
                        "max_pages": "Pages to fetch for very long lists",
                        "cadence_multiplier": "Check less often",
                    },
                    "data_description": {
                        "due_soon_days": 'The "due soon" warnings switch on this many days before a deadline.',
                        "document_directory": "Where downloaded documents are saved. Each company gets its own folder.",
                        "max_pages": "Only matters for companies with hundreds of officers, owners or charges. Each page holds 100. 10 is plenty for almost everyone.",
                        "cadence_multiplier": "1 is normal. 2 checks half as often. 3 checks a third as often.",
                    },
                }
            },
            "error": {
                "budget_exceeded": "These settings would check more often than Companies House allows. Slow things down, or take some companies off close watch.",
            },
        },
        "config_subentries": {
            "company": {
                "entry_type": "Company",
                "initiate_flow": {
                    "user": "Add company",
                    "reconfigure": "Change company settings",
                },
                "step": {
                    "user": {
                        "title": "Find a company",
                        "description": "Type part of the company name. Or enter a postcode to find the company registered at an address.",
                        "data": {
                            "query": "Company name, number or link",
                            "postcode": "Postcode",
                        },
                        "data_description": {
                            "query": "Part of the company name is enough. A company number, or the address of the company's page on the Companies House website, goes straight to it.",
                            "postcode": "Optional. Only show companies registered at this postcode.",
                        },
                    },
                    "select": {
                        "title": "Choose the company",
                        "data": {"selection": "Company"},
                    },
                    "confirm": {
                        "title": "Watch {company}",
                        "description": "Company number {number}. Choose what to keep an eye on.",
                        "data": {
                            "datasets": "Also keep an eye on",
                            "close_watch": "Close watch",
                            "notify_instantly": "Notify instantly",
                            "in_weekly_report": "In weekly report",
                            "label": "Note",
                            "website": "Website",
                        },
                        "data_description": {
                            "datasets": "The company's details and filings are always watched. Untick anything else you do not need.",
                            "close_watch": "Check this company every 15 minutes during office hours. Normally checks slow down when nothing is due. Turn this on if you need to hear about changes fast.",
                            "notify_instantly": "Raise an alert the moment anything changes, so an automation can push it to your phone or email.",
                            "in_weekly_report": "Include this company in the report built by the digest action.",
                            "label": 'Optional. Shown next to the company name, for example "Our landlord" or "Contractor, 47 High Street".',
                            "website": "Optional, for example example.co.uk. Used for the company's logo and a link in reports.",
                        },
                    },
                    "reconfigure": {
                        "title": "Change company settings",
                        "menu_options": {
                            "settings": "Change what is watched",
                            "track_officer": "Follow one of this company's officers",
                        },
                    },
                    "settings": {
                        "title": "Settings for {company}",
                        "description": "Company number {number}.",
                        "data": {
                            "datasets": "Also keep an eye on",
                            "close_watch": "Close watch",
                            "notify_instantly": "Notify instantly",
                            "in_weekly_report": "In weekly report",
                            "label": "Note",
                            "website": "Website",
                        },
                        "data_description": {
                            "datasets": "The company's details and filings are always watched. Untick anything else you do not need.",
                            "close_watch": "Check this company every 15 minutes during office hours. Normally checks slow down when nothing is due. Turn this on if you need to hear about changes fast.",
                            "notify_instantly": "Raise an alert the moment anything changes, so an automation can push it to your phone or email.",
                            "in_weekly_report": "Include this company in the report built by the digest action.",
                            "label": "Optional, shown next to the company name.",
                            "website": "Optional, for example example.co.uk. Used for the company's logo and a link in reports.",
                        },
                    },
                    "track_officer": {
                        "title": "Follow an officer of {company}",
                        "description": "Pick a current officer. You will then see every company they are involved with, not just this one.",
                        "data": {"selection": "Officer"},
                    },
                },
                "error": {
                    "cannot_connect": "Could not reach Companies House. Check your connection and try again.",
                    "no_results": "Nothing matched. Try a different name or postcode.",
                },
                "abort": {
                    "already_configured": "You are already watching this company.",
                    "invalid_auth": "Companies House rejected your API key. Fix that from the repair notice first.",
                    "officer_added": "Now following {name}.",
                    "officer_already_configured": "You are already following that person.",
                    "no_officers": "This company has no current officers on the register.",
                    "reconfigure_successful": "Saved.",
                },
            },
            "officer": {
                "entry_type": "Officer",
                "initiate_flow": {
                    "user": "Follow an officer",
                    "reconfigure": "Change officer settings",
                },
                "step": {
                    "user": {
                        "title": "Find a person",
                        "description": "Search by name for a director, secretary or other officer. Many people share a name. Use the month and year of birth in the results to pick the right one.",
                        "data": {"query": "Name or Companies House link"},
                        "data_description": {
                            "query": "You can also paste the address of the person's page on the Companies House website."
                        },
                    },
                    "select": {
                        "title": "Choose the person",
                        "data": {
                            "selection": "Person",
                            "watch_companies": "Watch their companies",
                        },
                        "data_description": {
                            "watch_companies": "Add every company they currently hold a role at, and any they join later, as watched companies."
                        },
                    },
                    "reconfigure": {
                        "title": "Change officer settings",
                        "menu_options": {
                            "settings": "Change the name or settings",
                            "add_record": "Add another register record for this person",
                        },
                    },
                    "settings": {
                        "title": "Settings",
                        "description": "Companies House reference {officer_id}. Followed as {records}.",
                        "data": {
                            "officer_name": "Name to show",
                            "watch_companies": "Watch their companies",
                            "notify_instantly": "Notify instantly",
                            "in_weekly_report": "In weekly report",
                        },
                        "data_description": {
                            "officer_name": "Useful when the register holds more than one record for the same person.",
                            "watch_companies": "Add every company they currently hold a role at, and any they join later, as watched companies.",
                            "notify_instantly": "Raise an alert the moment this person takes or leaves a role, so an automation can push it to your phone or email.",
                            "in_weekly_report": "Include this person in the report built by the digest action.",
                        },
                    },
                    "add_record": {
                        "title": "Find another record",
                        "description": "The register opens a new record whenever someone is appointed with slightly different details. Search for the person again, or paste the address of the other record's page.",
                        "data": {"query": "Name or Companies House link"},
                    },
                    "pick_record": {
                        "title": "Choose the record to add to {name}",
                        "description": "Its appointments will be pooled with the ones already followed.",
                        "data": {"selection": "Record"},
                    },
                },
                "error": {
                    "cannot_connect": "Could not reach Companies House. Check your connection and try again.",
                    "no_results": "Nobody matched that name.",
                },
                "abort": {
                    "already_configured": "You are already following this person.",
                    "invalid_auth": "Companies House rejected your API key. Fix that from the repair notice first.",
                    "reconfigure_successful": "Saved.",
                    "record_added": "Added {name} as another record of this person.",
                    "record_already_followed": "That record is already followed.",
                },
            },
        },
        "selector": {
            "datasets": {
                "options": {
                    "officers": "Officers (directors and secretaries)",
                    "psc": "People with significant control (the owners)",
                    "charges": "Charges (loans secured on the company)",
                    "insolvency": "Insolvency history",
                    "structure": "Registers, exemptions and UK establishments (rarely change)",
                }
            }
        },
        "entity": {
            "sensor": sensors,
            "binary_sensor": {
                k: {"name": v} for k, v in {**COMPANY_BINARY, **OFFICER_BINARY}.items()
            },
            "event": events,
            "switch": {
                "watch_companies": {"name": "Watch their companies"},
                "notify_instantly": {"name": "Notify instantly"},
                "in_weekly_report": {"name": "In weekly report"},
            },
            "calendar": {
                "deadlines": {"name": "Deadlines"},
                "all_deadlines": {"name": "All deadlines"},
            },
        },
        "exceptions": {
            "invalid_auth": {"message": "The Companies House API key was rejected."},
            "cannot_connect": {"message": "Could not reach Companies House: {error}"},
            "rate_limited": {
                "message": "Companies House is asking us to slow down. Try again in a few minutes."
            },
            "budget_exhausted": {
                "message": "Checks have used their share of the Companies House allowance for now. They will carry on shortly."
            },
            "not_found": {
                "message": "Companies House no longer has this on the register."
            },
            "resource_not_found": {"message": "Not found on the register: {resource}"},
            "no_entry": {
                "message": "Companies House is not set up yet. Add it under Settings, Devices and services."
            },
            "entry_not_loaded": {
                "message": "Companies House is not running at the moment."
            },
            "invalid_company_number": {
                "message": "{company_number} is not a valid company number."
            },
            "invalid_kind": {"message": "{kind} is not a valid kind."},
            "invalid_target": {
                "message": "Pick a Companies House company or officer, or the Companies House account itself."
            },
            "invalid_date": {
                "message": "{value} is not a valid date (use YYYY-MM-DD)."
            },
            "api_error": {"message": "Companies House returned an error: {error}"},
            "document_write_failed": {
                "message": "Could not write the document: {error}"
            },
            "path_not_allowed": {
                "message": "Home Assistant is not allowed to save files in {path}. Use a media folder, or add this folder to allowlist_external_dirs."
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
                "title": "Companies House is asking us to slow down",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Slow the checks down?",
                            "description": "Companies House has refused requests for over 15 minutes. Either something else is using your API key, or too many companies are on close watch. Confirm to check less often. You can change this back later in the options.",
                        }
                    }
                },
            },
            "company_dissolved": {
                "title": "{company} ({number}) has been dissolved",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Stop watching {company}?",
                            "description": "{company} ({number}) has been dissolved or removed from the register. From now on it is only checked once a month. Confirm to stop watching it and remove it from Home Assistant. Or ignore this notice to keep its history.",
                        }
                    }
                },
            },
            "company_not_found": {
                "title": "Company {number} is no longer on the register",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Stop watching company {number}?",
                            "description": "Companies House has no record of company {number} any more. Confirm to stop watching it and remove it from Home Assistant.",
                        }
                    }
                },
            },
            "officer_not_found": {
                "title": "{name} is no longer on the register",
                "fix_flow": {
                    "step": {
                        "confirm": {
                            "title": "Stop following {name}?",
                            "description": "Companies House has no record of {name} (reference {officer_id}) any more. Confirm to stop following them and remove them from Home Assistant.",
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
