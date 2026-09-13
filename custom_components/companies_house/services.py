"""Actions. Registered in async_setup so they exist without a config entry.

Every read action returns the API resource as its response. The document
download writes a file and fires ``companies_house_document_downloaded``;
nothing downloads automatically. The read actions are also exposed to the
Assist / LLM API so a voice assistant can answer register questions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any, override

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.helpers.service import async_extract_config_entry_ids
from homeassistant.util.json import JsonObjectType
import voluptuous as vol

from .api import (
    CompaniesHouseClient,
    CompaniesHouseError,
    CompaniesHouseNotFoundError,
    Priority,
)
from .const import (
    API_BASE,
    CONF_DOCUMENT_DIRECTORY,
    DEFAULT_DOCUMENT_DIRECTORY,
    DOCUMENT_FILENAME_MAX,
    DOMAIN,
    EVENT_DOCUMENT_DOWNLOADED,
    LOGGER,
    Dataset,
)
from .models import JsonDict, parse_date, render_filing_description

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_DEVICE_ID = "device_id"
ATTR_COMPANY_NUMBER = "company_number"
ATTR_DATASETS = "datasets"
ATTR_DOCUMENT_ID = "document_id"
ATTR_TRANSACTION_ID = "transaction_id"
ATTR_CONTENT_TYPE = "content_type"
ATTR_FILENAME = "filename"
ATTR_DESCRIPTION = "description"
ATTR_FILING_DATE = "filing_date"
ATTR_KIND = "kind"

PSC_KINDS = (
    "individual",
    "corporate-entity",
    "legal-person",
    "super-secure",
    "individual-beneficial-owner",
    "corporate-entity-beneficial-owner",
    "legal-person-beneficial-owner",
    "super-secure-beneficial-owner",
)
_COMPANY_NUMBER_RE = re.compile(r"^([A-Z]{0,2})(\d{5,8})$")
_UNSAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

type Handler = Callable[
    [HomeAssistant, CompaniesHouseClient, ServiceCall], Awaitable[JsonDict]
]


def normalise_company_number(value: Any) -> str:
    """Validate and normalise a company number such as ``sc12345`` to ``SC012345``."""
    text = str(value).strip().upper().replace(" ", "")
    match = _COMPANY_NUMBER_RE.match(text)
    if not match:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_company_number",
            translation_placeholders={"company_number": str(value)},
        )
    prefix, digits = match.groups()
    return prefix + digits.zfill(8 - len(prefix))


def _date_field(value: Any) -> str:
    if parse_date(value) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_date",
            translation_placeholders={"value": str(value)},
        )
    return str(value)


ENTRY_FIELD = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
COMPANY_FIELD = {
    vol.Required(ATTR_COMPANY_NUMBER): vol.All(cv.string, normalise_company_number)
}
PAGING_FIELDS = {
    vol.Optional("items_per_page"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
    vol.Optional("start_index"): vol.All(vol.Coerce(int), vol.Range(min=0)),
}


@dataclass(frozen=True, kw_only=True)
class ActionDef:
    """One action: schema, handler and how it is exposed."""

    name: str
    description: str
    schema: dict[Any, Any]
    handler: Handler
    llm: bool = True


def _q(call: ServiceCall) -> str:
    return str(call.data["query"])


def _n(call: ServiceCall) -> str:
    return str(call.data[ATTR_COMPANY_NUMBER])


async def _search_companies(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.search_companies(
        _q(call),
        items_per_page=call.data.get("items_per_page"),
        start_index=call.data.get("start_index"),
    )


async def _advanced_search(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    params = {k: v for k, v in call.data.items() if k != ATTR_CONFIG_ENTRY_ID}
    for key in ("sic_codes", "company_status", "company_type"):
        if isinstance(params.get(key), list):
            params[key] = ",".join(str(v) for v in params[key])
    return await client.advanced_search(params)


async def _alphabetical_search(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.alphabetical_search(
        _q(call),
        search_above=call.data.get("search_above"),
        search_below=call.data.get("search_below"),
        size=call.data.get("size"),
    )


async def _dissolved_search(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.dissolved_search(
        _q(call),
        search_type=call.data.get("search_type"),
        start_index=call.data.get("start_index"),
        size=call.data.get("size"),
    )


async def _search_officers(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.search_officers(
        _q(call),
        items_per_page=call.data.get("items_per_page"),
        start_index=call.data.get("start_index"),
    )


async def _search_disqualified(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.search_disqualified_officers(
        _q(call), items_per_page=call.data.get("items_per_page")
    )


async def _search_all(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.search_all(
        _q(call), items_per_page=call.data.get("items_per_page")
    )


async def _get_company(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.request(f"/company/{_n(call)}", priority=Priority.ON_DEMAND)


async def _get_registered_office(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_registered_office(_n(call))


async def _get_officers(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_officers_raw(
        _n(call),
        register_view=call.data.get("register_view"),
        order_by=call.data.get("order_by"),
        items_per_page=call.data.get("items_per_page"),
    )


async def _get_officer_appointment(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_officer_appointment(
        _n(call), str(call.data["appointment_id"])
    )


async def _get_officer_appointments(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_officer_appointments_raw(str(call.data["officer_id"]))


async def _get_disqualification(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    result = await client.get_disqualification(
        str(call.data["officer_id"]), str(call.data.get(ATTR_KIND, "natural"))
    )
    return result or {}


async def _get_filing_history(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    data = await client.request_optional(
        f"/company/{_n(call)}/filing-history",
        params={
            "category": call.data.get("category"),
            "items_per_page": call.data.get("items_per_page"),
            "start_index": call.data.get("start_index"),
        },
        priority=Priority.ON_DEMAND,
    )
    data = data or {"items": [], "total_count": 0}
    for item in data.get("items") or []:
        if isinstance(item, dict):
            item["rendered_description"] = render_filing_description(
                item.get("description"), item.get("description_values")
            )
    return data


async def _get_filing(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    data = await client.get_filing(_n(call), str(call.data[ATTR_TRANSACTION_ID]))
    data["rendered_description"] = render_filing_description(
        data.get("description"), data.get("description_values")
    )
    return data


async def _get_charges(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_charges_raw(_n(call))


async def _get_charge(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_charge(_n(call), str(call.data["charge_id"]))


async def _get_psc(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_psc_raw(_n(call))


async def _get_psc_detail(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_psc_detail(
        _n(call), str(call.data[ATTR_KIND]), str(call.data["notification_id"])
    )


async def _get_psc_statements(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_psc_statements_raw(_n(call))


async def _get_insolvency(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_insolvency_raw(_n(call))


async def _get_exemptions(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_exemptions(_n(call)) or {}


async def _get_registers(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_registers(_n(call)) or {}


async def _get_uk_establishments(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_uk_establishments(_n(call)) or {}


async def _get_document_metadata(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    return await client.get_document_metadata(str(call.data[ATTR_DOCUMENT_ID]))


# ---------------------------------------------------------------- documents


def sanitise_filename(name: str, extension: str) -> str:
    """Make a filename safe for the filesystem and cap its length."""
    cleaned = _UNSAFE_RE.sub("", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    limit = DOCUMENT_FILENAME_MAX - len(extension)
    return cleaned[:limit].rstrip(" .") + extension


def sanitise_dirname(name: str) -> str:
    """Make a directory name safe for the filesystem."""
    return re.sub(r"\s+", " ", _UNSAFE_RE.sub("", name)).strip(" .")[:120] or "unknown"


def _write_unique(directory: Path, filename: str, content: bytes) -> tuple[Path, bool]:
    """Write without ever overwriting; return the path and whether it already existed."""
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidate = directory / filename
    counter = 2
    while candidate.exists():
        if hashlib.sha256(candidate.read_bytes()).hexdigest() == digest:
            return candidate, True
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    candidate.write_bytes(content)
    return candidate, False


_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/xhtml+xml": ".xhtml",
    "application/xml": ".xml",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/zip": ".zip",
}


async def _download_document(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    document_id = str(call.data[ATTR_DOCUMENT_ID])
    content_type = str(call.data.get(ATTR_CONTENT_TYPE, "application/pdf"))
    entry = _resolve_entry(hass, call)
    metadata = await client.get_document_metadata(document_id)
    company_number = call.data.get(ATTR_COMPANY_NUMBER) or metadata.get(
        "company_number"
    )
    if company_number:
        company_number = normalise_company_number(company_number)
    description = call.data.get(ATTR_DESCRIPTION)
    filing_date = call.data.get(ATTR_FILING_DATE)
    company_name: str | None = None
    if company_number:
        for company in entry.runtime_data.companies.values():
            if (
                company.company_number == company_number
                and company.profile.data is not None
            ):
                company_name = company.profile.data.company_name
    if (
        company_number
        and call.data.get(ATTR_TRANSACTION_ID)
        and not (description and filing_date)
    ):
        filing = await client.get_filing(
            company_number, str(call.data[ATTR_TRANSACTION_ID])
        )
        description = description or render_filing_description(
            filing.get("description"), filing.get("description_values")
        )
        filing_date = filing_date or filing.get("date")
    if company_number and company_name is None:
        profile = await client.get_company(company_number, priority=Priority.ON_DEMAND)
        company_name = profile.company_name
    description = (
        description
        or str(metadata.get("category") or "document").replace("-", " ").capitalize()
    )
    filing_date = filing_date or str(
        metadata.get("significant_date")
        or str(metadata.get("created_at", ""))[:10]
        or "undated"
    )
    extension = _EXTENSIONS.get(content_type, ".bin")
    filename = (
        call.data.get(ATTR_FILENAME)
        or f"{filing_date} - Companies House - {description} ({company_name or company_number or document_id})"
    )
    filename = sanitise_filename(str(filename).removesuffix(extension), extension)
    base = Path(
        str(entry.options.get(CONF_DOCUMENT_DIRECTORY, DEFAULT_DOCUMENT_DIRECTORY))
    )
    folder = sanitise_dirname(
        f"{company_number} {company_name}" if company_number else "documents"
    )
    directory = base / folder
    if not hass.config.is_allowed_path(str(directory)):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="path_not_allowed",
            translation_placeholders={"path": str(directory)},
        )
    content = await client.download_document(document_id, content_type=content_type)
    try:
        path, existed = await hass.async_add_executor_job(
            _write_unique, directory, filename, content
        )
    except OSError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="document_write_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    result: JsonDict = {
        "path": str(path),
        "bytes": len(content),
        "already_existed": existed,
        "document_id": document_id,
        "company_number": company_number,
        "company_name": company_name,
        "description": description,
        "filing_date": filing_date,
        "transaction_id": call.data.get(ATTR_TRANSACTION_ID),
        "content_type": content_type,
    }
    hass.bus.async_fire(EVENT_DOCUMENT_DOWNLOADED, result)
    LOGGER.debug("Downloaded document %s to %s", document_id, path)
    return result


# ---------------------------------------------------------------- refresh


async def _refresh(
    hass: HomeAssistant, client: CompaniesHouseClient, call: ServiceCall
) -> JsonDict:
    datasets = {
        Dataset(d) for d in call.data.get(ATTR_DATASETS) or [d.value for d in Dataset]
    }
    entry_ids = await async_extract_config_entry_ids(call)
    if ATTR_CONFIG_ENTRY_ID in call.data:
        entry_ids.add(str(call.data[ATTR_CONFIG_ENTRY_ID]))
    device_ids = set(call.data.get(ATTR_DEVICE_ID) or [])
    refreshed = 0
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (
            entry.entry_id not in entry_ids
            or entry.state is not ConfigEntryState.LOADED
        ):
            continue
        runtime = entry.runtime_data
        for company in runtime.companies.values():
            if device_ids and not _device_matches(
                hass, device_ids, f"company_{company.company_number}", entry.entry_id
            ):
                continue
            await company.async_refresh_datasets(
                sorted(datasets), reason="refresh action", on_demand=True
            )
            refreshed += 1
        for officer in runtime.officers.values():
            if device_ids and not _device_matches(
                hass, device_ids, f"officer_{officer.officer_id}", entry.entry_id
            ):
                continue
            for coordinator in officer.coordinators.values():
                await coordinator.async_refresh_now(on_demand=True)
            refreshed += 1
    if not entry_ids:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_target"
        )
    return {"refreshed": refreshed}


def _device_matches(
    hass: HomeAssistant, device_ids: set[str], identifier: str, entry_id: str
) -> bool:
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry_id
    )
    return device is not None and device.id in device_ids


# ---------------------------------------------------------------- registry


ACTIONS: tuple[ActionDef, ...] = (
    ActionDef(
        name="search_companies",
        description="Search the register for companies by name.",
        schema={**ENTRY_FIELD, vol.Required("query"): cv.string, **PAGING_FIELDS},
        handler=_search_companies,
    ),
    ActionDef(
        name="advanced_search",
        description="Advanced company search by name, location, SIC codes, status, type and dates.",
        schema={
            **ENTRY_FIELD,
            vol.Optional("company_name_includes"): cv.string,
            vol.Optional("company_name_excludes"): cv.string,
            vol.Optional("location"): cv.string,
            vol.Optional("postcode"): cv.string,
            vol.Optional("sic_codes"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("company_status"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("company_type"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("incorporated_from"): _date_field,
            vol.Optional("incorporated_to"): _date_field,
            vol.Optional("dissolved_from"): _date_field,
            vol.Optional("dissolved_to"): _date_field,
            vol.Optional("size"): vol.All(vol.Coerce(int), vol.Range(min=1, max=5000)),
        },
        handler=_advanced_search,
    ),
    ActionDef(
        name="alphabetical_search",
        description="Alphabetical company search around a name.",
        schema={
            **ENTRY_FIELD,
            vol.Required("query"): cv.string,
            vol.Optional("search_above"): cv.string,
            vol.Optional("search_below"): cv.string,
            vol.Optional("size"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        },
        handler=_alphabetical_search,
    ),
    ActionDef(
        name="dissolved_search",
        description="Search dissolved companies.",
        schema={
            **ENTRY_FIELD,
            vol.Required("query"): cv.string,
            vol.Optional("search_type"): vol.In(
                ["alphabetical", "best-match", "previous-name-dissolved"]
            ),
            vol.Optional("start_index"): vol.All(vol.Coerce(int), vol.Range(min=0)),
            vol.Optional("size"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        },
        handler=_dissolved_search,
    ),
    ActionDef(
        name="search_officers",
        description="Search the register for officers by name.",
        schema={**ENTRY_FIELD, vol.Required("query"): cv.string, **PAGING_FIELDS},
        handler=_search_officers,
    ),
    ActionDef(
        name="search_disqualified_officers",
        description="Search the disqualified directors register by name.",
        schema={
            **ENTRY_FIELD,
            vol.Required("query"): cv.string,
            vol.Optional("items_per_page"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        },
        handler=_search_disqualified,
    ),
    ActionDef(
        name="search_all",
        description="Search companies, officers and disqualified officers together.",
        schema={
            **ENTRY_FIELD,
            vol.Required("query"): cv.string,
            vol.Optional("items_per_page"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        },
        handler=_search_all,
    ),
    ActionDef(
        name="get_company",
        description="Get a company profile: status, type, deadlines, registered office.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_company,
    ),
    ActionDef(
        name="get_registered_office",
        description="Get a company's registered office address.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_registered_office,
    ),
    ActionDef(
        name="get_officers",
        description="List a company's officers.",
        schema={
            **ENTRY_FIELD,
            **COMPANY_FIELD,
            vol.Optional("register_view"): cv.boolean,
            vol.Optional("order_by"): vol.In(
                ["appointed_on", "resigned_on", "surname"]
            ),
            vol.Optional("items_per_page"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        },
        handler=_get_officers,
    ),
    ActionDef(
        name="get_officer_appointment",
        description="Get one officer appointment on a company.",
        schema={
            **ENTRY_FIELD,
            **COMPANY_FIELD,
            vol.Required("appointment_id"): cv.string,
        },
        handler=_get_officer_appointment,
    ),
    ActionDef(
        name="get_officer_appointments",
        description="List every appointment an officer holds.",
        schema={**ENTRY_FIELD, vol.Required("officer_id"): cv.string},
        handler=_get_officer_appointments,
    ),
    ActionDef(
        name="get_disqualification",
        description="Get a disqualified officer record.",
        schema={
            **ENTRY_FIELD,
            vol.Required("officer_id"): cv.string,
            vol.Optional(ATTR_KIND, default="natural"): vol.In(
                ["natural", "corporate"]
            ),
        },
        handler=_get_disqualification,
    ),
    ActionDef(
        name="get_filing_history",
        description="List a company's filing history, newest first, with rendered descriptions.",
        schema={
            **ENTRY_FIELD,
            **COMPANY_FIELD,
            vol.Optional("category"): cv.string,
            **PAGING_FIELDS,
        },
        handler=_get_filing_history,
    ),
    ActionDef(
        name="get_filing",
        description="Get one filing history transaction.",
        schema={
            **ENTRY_FIELD,
            **COMPANY_FIELD,
            vol.Required(ATTR_TRANSACTION_ID): cv.string,
        },
        handler=_get_filing,
    ),
    ActionDef(
        name="get_charges",
        description="List a company's charges.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_charges,
    ),
    ActionDef(
        name="get_charge",
        description="Get one charge.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD, vol.Required("charge_id"): cv.string},
        handler=_get_charge,
    ),
    ActionDef(
        name="get_psc",
        description="List a company's persons with significant control.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_psc,
    ),
    ActionDef(
        name="get_psc_detail",
        description="Get one PSC notification of a given kind.",
        schema={
            **ENTRY_FIELD,
            **COMPANY_FIELD,
            vol.Required(ATTR_KIND): vol.In(PSC_KINDS),
            vol.Required("notification_id"): cv.string,
        },
        handler=_get_psc_detail,
    ),
    ActionDef(
        name="get_psc_statements",
        description="List a company's PSC statements.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_psc_statements,
    ),
    ActionDef(
        name="get_insolvency",
        description="Get a company's insolvency cases.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_insolvency,
    ),
    ActionDef(
        name="get_exemptions",
        description="Get a company's exemptions.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_exemptions,
    ),
    ActionDef(
        name="get_registers",
        description="Get a company's registers.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_registers,
    ),
    ActionDef(
        name="get_uk_establishments",
        description="List an overseas company's UK establishments.",
        schema={**ENTRY_FIELD, **COMPANY_FIELD},
        handler=_get_uk_establishments,
    ),
    ActionDef(
        name="get_document_metadata",
        description="Get metadata for a filed document.",
        schema={**ENTRY_FIELD, vol.Required(ATTR_DOCUMENT_ID): cv.string},
        handler=_get_document_metadata,
    ),
    ActionDef(
        name="download_document",
        description="Download a filed document to the document directory.",
        schema={
            **ENTRY_FIELD,
            vol.Required(ATTR_DOCUMENT_ID): cv.string,
            vol.Optional(ATTR_CONTENT_TYPE, default="application/pdf"): vol.In(
                list(_EXTENSIONS)
            ),
            vol.Optional(ATTR_COMPANY_NUMBER): vol.All(
                cv.string, normalise_company_number
            ),
            vol.Optional(ATTR_TRANSACTION_ID): cv.string,
            vol.Optional(ATTR_DESCRIPTION): cv.string,
            vol.Optional(ATTR_FILING_DATE): _date_field,
            vol.Optional(ATTR_FILENAME): cv.string,
        },
        handler=_download_document,
        llm=False,
    ),
    ActionDef(
        name="refresh",
        description="Refresh monitored companies and officers now.",
        schema={
            **ENTRY_FIELD,
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_DATASETS): vol.All(
                cv.ensure_list, [vol.In([d.value for d in Dataset])]
            ),
        },
        handler=_refresh,
        llm=False,
    ),
)
READ_ACTIONS = frozenset(
    a.name for a in ACTIONS if a.name not in ("refresh", "download_document")
)
SEARCH_ACTIONS = frozenset(
    {
        "search_companies",
        "advanced_search",
        "alphabetical_search",
        "dissolved_search",
        "search_officers",
        "search_disqualified_officers",
        "search_all",
    }
)


def _resolve_entry(hass: HomeAssistant, call: ServiceCall) -> Any:
    """Return the loaded config entry a call refers to, or the only one."""
    entries = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        for entry in entries:
            if entry.entry_id == wanted:
                return entry
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="entry_not_loaded"
        )
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entry"
        )
    return entries[0]


def _make_service(
    action: ActionDef,
) -> Callable[[ServiceCall], Coroutine[Any, Any, ServiceResponse]]:
    async def _service(call: ServiceCall) -> ServiceResponse:
        entry = _resolve_entry(call.hass, call)
        client: CompaniesHouseClient = entry.runtime_data.client
        try:
            result = await action.handler(call.hass, client, call)
        except CompaniesHouseNotFoundError as err:
            if action.name in SEARCH_ACTIONS:
                # The API answers a search with no hits with a 404.
                return {"items": [], "total_results": 0, "hits": 0}
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="resource_not_found",
                translation_placeholders={"resource": str(err).removeprefix(API_BASE)},
            ) from err
        except CompaniesHouseError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        if action.name == "refresh":
            return None
        return result

    return _service


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions and the LLM API."""
    for action in ACTIONS:
        if action.name == "refresh":
            supports = SupportsResponse.NONE
        elif action.name == "download_document":
            supports = SupportsResponse.OPTIONAL
        else:
            supports = SupportsResponse.ONLY
        hass.services.async_register(
            DOMAIN,
            action.name,
            _make_service(action),
            schema=vol.Schema(action.schema),
            supports_response=supports,
        )
    llm.async_register_api(hass, CompaniesHouseLLMAPI(hass))


# ---------------------------------------------------------------- LLM


class ActionTool(llm.Tool):
    """Expose one read action to an assistant."""

    def __init__(self, action: ActionDef) -> None:
        """Wrap the action."""
        self.name = f"{DOMAIN}_{action.name}"
        self.description = action.description
        self.parameters = vol.Schema(
            {
                k: v
                for k, v in action.schema.items()
                if getattr(k, "schema", k) != ATTR_CONFIG_ENTRY_ID
            }
        )
        self._action = action.name

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the action and return its response."""
        result = await hass.services.async_call(
            DOMAIN,
            self._action,
            tool_input.tool_args,
            blocking=True,
            context=llm_context.context,
            return_response=True,
        )
        return {"result": result}


class CompaniesHouseLLMAPI(llm.API):
    """The Companies House read actions as an LLM API."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Register under a stable id."""
        super().__init__(hass=hass, id=DOMAIN, name="Companies House")

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the tools with a short prompt."""
        monitored: list[str] = []
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                monitored.extend(
                    f"{c.company_name} ({c.company_number})"
                    for c in entry.runtime_data.companies.values()
                )
        prompt = (
            "You can look up the UK Companies House public register: company profiles, "
            "officers, filings, charges, persons with significant control and deadlines. "
            "Company numbers are 8 characters."
        )
        if monitored:
            prompt += " Monitored companies: " + ", ".join(monitored[:30]) + "."
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=[ActionTool(a) for a in ACTIONS if a.llm],
        )


__all__ = [
    "ACTIONS",
    "READ_ACTIONS",
    "async_setup_services",
    "normalise_company_number",
    "sanitise_filename",
]


def action_fields(action: ActionDef) -> Mapping[str, Any]:
    """Return the field names of an action, for documentation checks."""
    return {getattr(k, "schema", k): v for k, v in action.schema.items()}
