"""The Companies House integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .api import CompaniesHouseClient
from .connections import ConnectionsCoordinator
from .const import (
    CONF_API_KEY,
    CONF_CLOSE_WATCH,
    CONF_COMPANY_NUMBER,
    CONF_DATASETS,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    CONF_IN_WEEKLY_REPORT,
    CONF_LABEL,
    CONF_MAX_PAGES,
    CONF_NOTIFY_INSTANTLY,
    CONF_OFFICER_ID,
    CONF_OFFICER_IDS,
    CONF_OFFICER_NAME,
    CONF_WATCH_COMPANIES,
    CONF_WEBSITE,
    DEFAULT_MAX_PAGES,
    DOMAIN,
    LOGGER,
    SUBENTRY_TYPE_COMPANY,
    SUBENTRY_TYPE_OFFICER,
    Dataset,
)
from .coordinator import (
    AccountCoordinator,
    CompanyRuntime,
    OfficerRuntime,
    signal_new_company,
    signal_new_officer,
)
from .entity import service_device_info
from .models import DateOfBirth
from .services import async_setup_services
from .store import ChangeStore

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class CompaniesHouseRuntimeData:
    """Runtime data stored on the config entry."""

    client: CompaniesHouseClient
    store: ChangeStore
    account: AccountCoordinator
    service_device_id: str = ""
    # The connections map (who sits with whom, who owns what) on the service device.
    connections: ConnectionsCoordinator | None = None
    companies: dict[str, CompanyRuntime] = field(default_factory=dict)
    officers: dict[str, OfficerRuntime] = field(default_factory=dict)
    snapshot: dict[str, Any] = field(default_factory=dict)
    # Changes are applied one at a time, so an officer adding companies while
    # a change is still being applied cannot start the same company twice.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


type CompaniesHouseConfigEntry = ConfigEntry[CompaniesHouseRuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions so they exist without a config entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> bool:
    """Set up Companies House from a config entry."""
    client = CompaniesHouseClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        max_pages=int(entry.options.get(CONF_MAX_PAGES, DEFAULT_MAX_PAGES)),
    )
    store = ChangeStore(hass, entry.entry_id)
    await store.async_load()

    runtime = CompaniesHouseRuntimeData(
        client=client,
        store=store,
        account=AccountCoordinator(
            hass,
            entry,
            client,
            lambda: list(entry.runtime_data.companies.values()),
            lambda: list(entry.runtime_data.officers.values()),
        ),
    )
    entry.runtime_data = runtime

    # The service device must exist before company devices point at it.
    device_registry = dr.async_get(hass)
    service_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, **service_device_info(entry)
    )
    runtime.service_device_id = service_device.id

    for subentry in entry.subentries.values():
        await _async_start_subentry(hass, entry, subentry)

    _raise_if_auth_failed(runtime)
    if not runtime.companies and not runtime.officers:
        # Nothing has exercised the key yet; test it so a bad key is caught now.
        await _validate_key(client)

    _prune_store(runtime)
    await runtime.account.async_config_entry_first_refresh()
    # Rate only now that every company and person is up: a rating depends on
    # the followed people, so rating each company as it started would drop a
    # band that rests on a person listed after it, and announce the drop.
    for company in runtime.companies.values():
        company.recompute_risk()
    runtime.connections = ConnectionsCoordinator(hass, entry)
    runtime.connections.async_build()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    for company in runtime.companies.values():
        company.mark_ready()
    for officer in runtime.officers.values():
        officer.mark_ready()
    runtime.connections.async_start()
    runtime.snapshot = _subentry_snapshot(entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    LOGGER.debug(
        "Set up %s with %d companies and %d officers",
        entry.title,
        len(runtime.companies),
        len(runtime.officers),
    )
    return True


def _raise_if_auth_failed(runtime: CompaniesHouseRuntimeData) -> None:
    for company in runtime.companies.values():
        for coordinator in company.coordinators.values():
            if isinstance(coordinator.last_exception, ConfigEntryAuthFailed):
                raise coordinator.last_exception
    for officer in runtime.officers.values():
        for coordinator in officer.coordinators.values():
            if isinstance(coordinator.last_exception, ConfigEntryAuthFailed):
                raise coordinator.last_exception


async def _validate_key(client: CompaniesHouseClient) -> None:
    from homeassistant.exceptions import ConfigEntryNotReady

    from .api import CompaniesHouseAuthError, CompaniesHouseError

    try:
        await client.validate_key()
    except CompaniesHouseAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except CompaniesHouseError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err


def _prune_store(runtime: CompaniesHouseRuntimeData) -> None:
    """Forget state for companies and officers that are no longer configured."""
    wanted_companies = {c.company_number for c in runtime.companies.values()}
    wanted_officers = {o.officer_id for o in runtime.officers.values()}
    for number in list(runtime.store.companies):
        if number not in wanted_companies:
            runtime.store.forget_company(number)
    for officer_id in list(runtime.store.officers):
        if officer_id not in wanted_officers:
            runtime.store.forget_officer(officer_id)


# Settings that only change what is shown, never what is fetched.
COSMETIC_KEYS: frozenset[str] = frozenset({CONF_LABEL, CONF_OFFICER_NAME, CONF_WEBSITE})
# Settings a running company or officer can take on board without a rebuild.
LIVE_KEYS: frozenset[str] = frozenset(
    {
        CONF_CLOSE_WATCH,
        CONF_WATCH_COMPANIES,
        CONF_OFFICER_IDS,
        CONF_NOTIFY_INSTANTLY,
        CONF_IN_WEEKLY_REPORT,
    }
)


def _subentry_snapshot(entry: ConfigEntry) -> dict[str, Any]:
    """Return what the listener compares against to see what changed."""
    return {
        "options": dict(entry.options),
        "subentries": {sid: dict(sub.data) for sid, sub in entry.subentries.items()},
    }


def _material(data: dict[str, Any]) -> dict[str, Any]:
    """Return the settings that need a rebuild to change: what is fetched."""
    return {k: v for k, v in data.items() if k not in COSMETIC_KEYS | LIVE_KEYS}


def _rename_officer(hass: HomeAssistant, officer: OfficerRuntime, name: str) -> None:
    """Apply a new display name to a running officer and its device."""
    officer.configured_name = name
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, f"officer_{officer.officer_id}"), officer.entry.entry_id
    )
    if device is not None:
        device_registry.async_update_device(device.id, name=officer.officer_name)


async def _async_start_subentry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry, subentry: ConfigSubentry
) -> CompanyRuntime | OfficerRuntime | None:
    """Create the runtime for a subentry and fetch its data once."""
    runtime = entry.runtime_data
    if subentry.subentry_type == SUBENTRY_TYPE_COMPANY:
        datasets = {Dataset.PROFILE, Dataset.FILINGS} | {
            Dataset(d)
            for d in subentry.data.get(CONF_DATASETS, [d.value for d in Dataset])
            if d in Dataset.__members__.values()
        }
        company = CompanyRuntime(
            hass,
            entry,
            subentry,
            runtime.client,
            runtime.store,
            company_number=subentry.data[CONF_COMPANY_NUMBER],
            close_watch=bool(subentry.data.get(CONF_CLOSE_WATCH, False)),
            datasets=datasets,
            label=str(subentry.data.get(CONF_LABEL, "") or ""),
            website=str(subentry.data.get(CONF_WEBSITE, "") or ""),
            notify_instantly=bool(subentry.data.get(CONF_NOTIFY_INSTANTLY, False)),
            in_weekly_report=bool(subentry.data.get(CONF_IN_WEEKLY_REPORT, True)),
        )
        await company.async_first_refresh()
        runtime.companies[subentry.subentry_id] = company
        return company
    if subentry.subentry_type == SUBENTRY_TYPE_OFFICER:
        month = subentry.data.get(CONF_DATE_OF_BIRTH_MONTH)
        year = subentry.data.get(CONF_DATE_OF_BIRTH_YEAR)
        officer = OfficerRuntime(
            hass,
            entry,
            subentry,
            runtime.client,
            runtime.store,
            officer_id=subentry.data[CONF_OFFICER_ID],
            officer_name=subentry.data.get(CONF_OFFICER_NAME, ""),
            date_of_birth=DateOfBirth(month=month, year=year)
            if month is not None and year is not None
            else None,
            officer_ids=list(subentry.data.get(CONF_OFFICER_IDS) or []),
            watch_companies=bool(subentry.data.get(CONF_WATCH_COMPANIES, False)),
            notify_instantly=bool(subentry.data.get(CONF_NOTIFY_INSTANTLY, False)),
            in_weekly_report=bool(subentry.data.get(CONF_IN_WEEKLY_REPORT, True)),
        )
        await officer.async_first_refresh()
        runtime.officers[subentry.subentry_id] = officer
        return officer
    return None


async def _async_update_listener(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> None:
    """React to the entry changing.

    A company or officer being added, removed or renamed is handled in place,
    and so is close watch being switched on or off, so the ones already
    running are untouched (no flicker to unavailable). Changing what is
    fetched for an existing one, or changing the options, reloads the entry.
    """
    runtime = entry.runtime_data
    async with runtime.lock:
        await _async_apply_changes(hass, entry)


async def _async_apply_changes(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> None:
    runtime = entry.runtime_data
    before = runtime.snapshot
    after = _subentry_snapshot(entry)
    known = set(before["subentries"])
    current = set(after["subentries"])
    changed = {
        sid
        for sid in known & current
        if _material(before["subentries"][sid]) != _material(after["subentries"][sid])
    }
    if before["options"] != after["options"] or changed:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    for sid in known & current:
        if before["subentries"][sid] == after["subentries"][sid]:
            continue
        data = after["subentries"][sid]
        live = runtime.officers.get(sid) or runtime.companies.get(sid)
        if live is not None:
            live.notify_instantly = bool(data.get(CONF_NOTIFY_INSTANTLY, False))
            live.in_weekly_report = bool(data.get(CONF_IN_WEEKLY_REPORT, True))
        if (officer := runtime.officers.get(sid)) is not None:
            _rename_officer(hass, officer, data.get(CONF_OFFICER_NAME, ""))
            watch = bool(data.get(CONF_WATCH_COMPANIES, False))
            if watch and not officer.watch_companies:
                officer.watch_companies = True
                await officer.async_watch_companies()
            officer.watch_companies = watch
            ids = list(data.get(CONF_OFFICER_IDS) or [officer.officer_id])
            if set(ids) != set(officer.officer_ids):
                officer.officer_ids = [officer.officer_id] + [
                    i for i in ids if i != officer.officer_id
                ]
                await officer.appointments.async_refresh_now()
                officer.records.async_update_listeners()
        if (company := runtime.companies.get(sid)) is not None:
            company.label = str(data.get(CONF_LABEL, "") or "")
            company.website = str(data.get(CONF_WEBSITE, "") or "")
            company.close_watch = bool(data.get(CONF_CLOSE_WATCH, False))
            company.recompute_tier()

    for sid in known - current:
        if (company := runtime.companies.pop(sid, None)) is not None:
            await company.async_shutdown()
            runtime.store.forget_company(company.company_number)
        if (officer := runtime.officers.pop(sid, None)) is not None:
            await officer.async_shutdown()
            runtime.store.forget_officer(officer.officer_id)

    new_companies: list[CompanyRuntime] = []
    new_officers: list[OfficerRuntime] = []
    for sid in current - known:
        started = await _async_start_subentry(hass, entry, entry.subentries[sid])
        if isinstance(started, CompanyRuntime):
            new_companies.append(started)
        elif isinstance(started, OfficerRuntime):
            new_officers.append(started)
    if new_officers:
        # A newly followed person feeds every company's rating at once.
        for company in runtime.companies.values():
            company.recompute_risk()
    else:
        for company in new_companies:
            company.recompute_risk()
    for company in new_companies:
        async_dispatcher_send(hass, signal_new_company(entry.entry_id), company)
        company.mark_ready()
    for officer in new_officers:
        async_dispatcher_send(hass, signal_new_officer(entry.entry_id), officer)
        officer.mark_ready()
    _raise_if_auth_failed(runtime)
    runtime.snapshot = after
    await runtime.account.async_refresh()
    if runtime.connections is not None:
        # Removals raise no event, so the map is asked to catch up here.
        runtime.connections.request_rebuild("settings changed")


async def async_unload_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime = entry.runtime_data
        for company in runtime.companies.values():
            await company.async_shutdown()
        for officer in runtime.officers.values():
            await officer.async_shutdown()
        await runtime.account.async_shutdown()
        if runtime.connections is not None:
            await runtime.connections.async_shutdown()
        await runtime.store.async_save()
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> None:
    """Delete the store when the entry is removed."""
    await ChangeStore(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a device that no longer matches a configured subentry."""
    runtime = entry.runtime_data
    live = {(DOMAIN, entry.entry_id)}
    live.update(
        (DOMAIN, f"company_{c.company_number}") for c in runtime.companies.values()
    )
    live.update((DOMAIN, f"officer_{o.officer_id}") for o in runtime.officers.values())
    return not any(identifier in live for identifier in device.identifiers)


async def async_migrate_entry(
    hass: HomeAssistant, entry: CompaniesHouseConfigEntry
) -> bool:
    """Migrate old config entries.

    1.1 was the first release. 1.2 turned every entity on by default; entities
    that 1.1 created switched off are switched on so existing installs match.
    """
    LOGGER.debug(
        "Migrating entry %s from %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        entity_registry = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        ):
            if entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION:
                entity_registry.async_update_entity(entity.entity_id, disabled_by=None)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True
