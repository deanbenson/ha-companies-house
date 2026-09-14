"""Constants for the Companies House integration."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
import logging
from typing import Final

LOGGER = logging.getLogger(__package__)

DOMAIN: Final = "companies_house"
MANUFACTURER: Final = "Companies House"

API_BASE: Final = "https://api.company-information.service.gov.uk"
DOCUMENT_API_BASE: Final = "https://document-api.company-information.service.gov.uk"
FIND_AND_UPDATE_BASE: Final = (
    "https://find-and-update.company-information.service.gov.uk"
)
DEVELOPER_HUB_URL: Final = "https://developer.company-information.service.gov.uk/"

TIMEZONE: Final = "Europe/London"

# Config entry data / options
CONF_API_KEY: Final = "api_key"
CONF_DUE_SOON_DAYS: Final = "due_soon_days"
CONF_DOCUMENT_DIRECTORY: Final = "document_directory"
CONF_MAX_PAGES: Final = "max_pages"
CONF_CADENCE_MULTIPLIER: Final = "cadence_multiplier"

DEFAULT_DUE_SOON_DAYS: Final = 30
DEFAULT_DOCUMENT_DIRECTORY: Final = "/media/companies_house"
DEFAULT_MAX_PAGES: Final = 10
DEFAULT_CADENCE_MULTIPLIER: Final = 1.0
MIN_CADENCE_MULTIPLIER: Final = 1.0
MAX_CADENCE_MULTIPLIER: Final = 10.0

# Subentries
SUBENTRY_TYPE_COMPANY: Final = "company"
SUBENTRY_TYPE_OFFICER: Final = "officer"

CONF_COMPANY_NUMBER: Final = "company_number"
CONF_COMPANY_NAME: Final = "company_name"
CONF_DATASETS: Final = "datasets"
CONF_CLOSE_WATCH: Final = "close_watch"
CONF_LABEL: Final = "label"

CONF_OFFICER_ID: Final = "officer_id"
# Every register record followed as this one person; the first is the main one.
CONF_OFFICER_IDS: Final = "officer_ids"
CONF_OFFICER_NAME: Final = "officer_name"
# Add every company the person currently holds a role at, automatically.
CONF_WATCH_COMPANIES: Final = "watch_companies"
# Fire an alert event (for a push or an email) the moment something changes.
CONF_NOTIFY_INSTANTLY: Final = "notify_instantly"
# Include this company or person in the digest action's report.
CONF_IN_WEEKLY_REPORT: Final = "in_weekly_report"
# The company's own website, for a logo and a link in reports.
CONF_WEBSITE: Final = "website"
CONF_DATE_OF_BIRTH_MONTH: Final = "date_of_birth_month"
CONF_DATE_OF_BIRTH_YEAR: Final = "date_of_birth_year"

CONF_QUERY: Final = "query"
CONF_POSTCODE: Final = "postcode"
CONF_SELECTION: Final = "selection"


class Dataset(StrEnum):
    """A company dataset that can be monitored."""

    PROFILE = "profile"
    FILINGS = "filings"
    OFFICERS = "officers"
    PSC = "psc"
    CHARGES = "charges"
    INSOLVENCY = "insolvency"
    STRUCTURE = "structure"


OPTIONAL_DATASETS: Final = (
    Dataset.OFFICERS,
    Dataset.PSC,
    Dataset.CHARGES,
    Dataset.INSOLVENCY,
    Dataset.STRUCTURE,
)


class OfficerDataset(StrEnum):
    """An officer dataset."""

    APPOINTMENTS = "appointments"
    DISQUALIFICATION = "disqualification"
    RECORDS = "records"


# Rate limiting (section 6.6)
RATE_LIMIT_DEFAULT: Final = 600
RATE_WINDOW_DEFAULT: Final = timedelta(minutes=5)
RATE_MAX_PER_SECOND: Final = 2.0
BUDGET_SCHEDULED_FRACTION: Final = 0.80
BUDGET_PROJECTION_MAX_PER_WINDOW: Final = 400
BACKOFF_INITIAL: Final = timedelta(seconds=30)
BACKOFF_MAX: Final = timedelta(minutes=30)
THROTTLE_REPAIR_WINDOWS: Final = 3
REQUEST_TIMEOUT: Final = timedelta(seconds=30)

HEADER_RATE_LIMIT: Final = "X-Ratelimit-Limit"
HEADER_RATE_REMAIN: Final = "X-Ratelimit-Remain"
HEADER_RATE_RESET: Final = "X-Ratelimit-Reset"
HEADER_RATE_WINDOW: Final = "X-Ratelimit-Window"


# Adaptive cadence (section 6.4)
class Tier(StrEnum):
    """Adaptive polling tier of a company."""

    CLOSE_WATCH = "close_watch"
    DEADLINE = "deadline"
    NORMAL = "normal"
    QUIET = "quiet"
    DISSOLVED = "dissolved"


PROBE_INTERVALS: Final[dict[Tier, tuple[timedelta, timedelta]]] = {
    # (business hours, out of hours)
    Tier.CLOSE_WATCH: (timedelta(minutes=15), timedelta(hours=1)),
    Tier.DEADLINE: (timedelta(minutes=30), timedelta(hours=3)),
    Tier.NORMAL: (timedelta(hours=2), timedelta(hours=12)),
    Tier.QUIET: (timedelta(hours=6), timedelta(hours=24)),
    Tier.DISSOLVED: (timedelta(days=30), timedelta(days=30)),
}

DEADLINE_TIER_DAYS: Final = 30
# How long past a deadline a company still counts as "near" it. Beyond this an
# unfiled company is treated as abandoned rather than about to file.
OVERDUE_GRACE_DAYS: Final = 90
QUIET_NO_FILING_DAYS: Final = 183
QUIET_NO_DEADLINE_DAYS: Final = 90
PROFILE_INTERVAL: Final = timedelta(days=1)
PROFILE_INTERVAL_NEAR_DEADLINE: Final = timedelta(hours=6)
PROFILE_NEAR_DEADLINE_DAYS: Final = 14
RECONCILE_INTERVAL: Final = timedelta(days=7)
STRUCTURE_INTERVAL: Final = timedelta(days=30)
APPOINTMENTS_INTERVAL: Final = timedelta(days=1)
DISQUALIFICATION_INTERVAL: Final = timedelta(days=7)
# How often the register is searched for new records of a followed person.
RECORDS_INTERVAL: Final = timedelta(days=7)
JITTER_FRACTION: Final = 0.15

BUSINESS_HOURS_START: Final = (8, 0)
BUSINESS_HOURS_END: Final = (18, 30)

FINISHED_STATUSES: Final = frozenset(
    {"dissolved", "removed", "converted-closed", "closed"}
)
INSOLVENT_STATUSES: Final = frozenset(
    {
        "liquidation",
        "receivership",
        "administration",
        "voluntary-arrangement",
        "insolvency-proceedings",
    }
)
STATUS_DETAIL_STRIKE_OFF: Final = "active-proposal-to-strike-off"

# Filing description keys that start a strike-off (a first Gazette notice),
# that hold one off (a successful objection suspends the countdown for six
# months, not drops it) and that end one (the registrar stands it down or
# the directors withdraw their application). The probe, the countdown and
# the risk rating read the same lists, so they never disagree about a
# strike-off.
STRIKE_OFF_NOTICE_DESCRIPTIONS: Final = frozenset(
    {
        "gazette-notice-voluntary",
        "gazette-notice-compulsory",
        "gazette-notice-compulsary",
    }
)
STRIKE_OFF_SUSPENDED_DESCRIPTIONS: Final = frozenset(
    {
        "dissolution-voluntary-strike-off-suspended",
        "dissolved-compulsory-strike-off-suspended",
    }
)
STRIKE_OFF_DISCONTINUED_DESCRIPTIONS: Final = frozenset(
    {
        "gazette-filings-brought-up-to-date",
        "dissolution-voluntary-strike-off-discontinued",
        "dissolution-withdrawal-application-strike-off-company",
        "dissolution-withdrawal-application-strike-off-limited-liability-partnership",
    }
)

# Filing category -> datasets to refresh on a probe hit (section 6.3)
FILING_CATEGORY_REFRESH: Final[dict[str, tuple[Dataset, ...]]] = {
    "officers": (Dataset.PROFILE, Dataset.OFFICERS),
    "persons-with-significant-control": (Dataset.PROFILE, Dataset.PSC),
    "mortgage": (Dataset.PROFILE, Dataset.CHARGES),
    "insolvency": (Dataset.PROFILE, Dataset.INSOLVENCY),
    "liquidation": (Dataset.PROFILE, Dataset.INSOLVENCY),
    "gazette": (Dataset.PROFILE, Dataset.INSOLVENCY),
    "dissolution": (Dataset.PROFILE,),
    "accounts": (Dataset.PROFILE,),
    "confirmation-statement": (Dataset.PROFILE,),
    "annual-return": (Dataset.PROFILE,),
    "address": (Dataset.PROFILE,),
    "change-of-name": (Dataset.PROFILE,),
    "capital": (Dataset.PROFILE,),
    "resolution": (Dataset.PROFILE,),
    "incorporation": (Dataset.PROFILE,),
}
FILING_UNKNOWN_REFRESH: Final = (
    Dataset.PROFILE,
    Dataset.OFFICERS,
    Dataset.PSC,
    Dataset.CHARGES,
)

# Filing event entity types (section 9.4)
FILING_EVENT_TYPES: Final = [
    "accounts",
    "confirmation-statement",
    "officers",
    "persons-with-significant-control",
    "address",
    "capital",
    "charges",
    "mortgage",
    "change-of-name",
    "resolution",
    "incorporation",
    "gazette",
    "insolvency",
    "dissolution",
    "other",
]
FILING_CATEGORY_EVENT_TYPE: Final[dict[str, str]] = {
    "liquidation": "insolvency",
    "annual-return": "other",
}

OFFICER_CHANGE_EVENT_TYPES: Final = ["appointed", "resigned", "details-changed"]
PSC_CHANGE_EVENT_TYPES: Final = [
    "notified",
    "ceased",
    "statement-added",
    "details-changed",
]
CHARGE_CHANGE_EVENT_TYPES: Final = [
    "created",
    "satisfied",
    "part-satisfied",
    "acquired",
]
STATUS_CHANGE_EVENT_TYPES: Final = [
    "status-changed",
    "strike-off-proposed",
    "strike-off-suspended",
    "strike-off-discontinued",
    "dissolved",
    "risk-changed",
]
PROFILE_CHANGE_EVENT_TYPES: Final = [
    "name-changed",
    "address-changed",
    "sic-changed",
    "accounting-reference-date-changed",
]
APPOINTMENT_EVENT_TYPES: Final = [
    "appointed",
    "resigned",
    "company-status-changed",
    "disqualified",
    "new-record",
    "company-now-watched",
]

EVENT_COMPANIES_HOUSE: Final = "companies_house_event"
# Fired only for companies and people with "notify instantly" on, with a
# ready-made title, message and link so one automation can push it anywhere.
EVENT_COMPANIES_HOUSE_ALERT: Final = "companies_house_alert"
EVENT_DOCUMENT_DOWNLOADED: Final = "companies_house_document_downloaded"

# Entity options
COMPANY_STATUS_OPTIONS: Final = [
    "active",
    "dissolved",
    "liquidation",
    "receivership",
    "administration",
    "voluntary-arrangement",
    "converted-closed",
    "insolvency-proceedings",
    "registered",
    "removed",
    "closed",
    "open",
]
COMPANY_STATUS_DETAIL_OPTIONS: Final = [
    "active",
    "dissolved",
    "converted-closed",
    "transferred-from-uk",
    "active-proposal-to-strike-off",
    "petition-to-restore-dissolved",
    "transformed-to-se",
    "converted-to-plc",
    "converted-to-uk-societas",
    "converted-to-ukeig",
]
DEADLINE_TYPE_OPTIONS: Final = ["accounts", "confirmation_statement"]
JURISDICTION_OPTIONS: Final = [
    "england-wales",
    "wales",
    "scotland",
    "northern-ireland",
    "european-union",
    "united-kingdom",
    "england",
    "noneu",
]

# Attribute list caps (section 9)
ATTR_LIST_CAP: Final = 10
ATTR_APPOINTMENTS_CAP: Final = 50
STORE_TRANSACTION_CAP: Final = 500
# Changes remembered per company or person, newest first, for the digest.
CHANGE_LOG_CAP: Final = 200
MAX_STATE_LENGTH: Final = 255

# Storage
STORAGE_VERSION: Final = 1
STORAGE_MINOR_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = f"{DOMAIN}.{{entry_id}}"

# Pagination
ITEMS_PER_PAGE: Final = 100
SEARCH_ITEMS_PER_PAGE: Final = 20
# Common names have hundreds of namesakes; show more and let people filter.
OFFICER_SEARCH_ITEMS_PER_PAGE: Final = 50

# Documents
DOCUMENT_FILENAME_MAX: Final = 200
