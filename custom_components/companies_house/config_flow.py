"""Config flow: the account, its options, and the company and officer subentries."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    CompaniesHouseNotFoundError,
    CompaniesHouseRateLimitError,
)
from .const import (
    BUDGET_PROJECTION_MAX_PER_WINDOW,
    CONF_API_KEY,
    CONF_CADENCE_MULTIPLIER,
    CONF_CLOSE_WATCH,
    CONF_COMPANY_NAME,
    CONF_COMPANY_NUMBER,
    CONF_DATASETS,
    CONF_DATE_OF_BIRTH_MONTH,
    CONF_DATE_OF_BIRTH_YEAR,
    CONF_DOCUMENT_DIRECTORY,
    CONF_DUE_SOON_DAYS,
    CONF_LABEL,
    CONF_MAX_PAGES,
    CONF_OFFICER_ID,
    CONF_OFFICER_NAME,
    CONF_POSTCODE,
    CONF_QUERY,
    CONF_SELECTION,
    DEFAULT_CADENCE_MULTIPLIER,
    DEFAULT_DOCUMENT_DIRECTORY,
    DEFAULT_DUE_SOON_DAYS,
    DEFAULT_MAX_PAGES,
    DEVELOPER_HUB_URL,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    MAX_CADENCE_MULTIPLIER,
    MIN_CADENCE_MULTIPLIER,
    OPTIONAL_DATASETS,
    SEARCH_ITEMS_PER_PAGE,
    SUBENTRY_TYPE_COMPANY,
    SUBENTRY_TYPE_OFFICER,
    Tier,
)
from .models import DateOfBirth, JsonDict, officer_id_from_link
from .scheduler import projected_requests_per_window

API_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


def key_unique_id(api_key: str) -> str:
    """Return a stable id for a key without storing the key itself."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


async def async_validate_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Return an error key, or None when the key works."""
    client = CompaniesHouseClient(async_get_clientsession(hass), api_key)
    try:
        await client.validate_key()
    except CompaniesHouseAuthError:
        return "invalid_auth"
    except CompaniesHouseRateLimitError:
        return "rate_limited"
    except CompaniesHouseError:
        return "cannot_connect"
    return None


def _client_for(hass: HomeAssistant, entry: ConfigEntry) -> CompaniesHouseClient:
    """Use the entry's shared client when loaded so the budget is respected."""
    if entry.state is ConfigEntryState.LOADED:
        client: CompaniesHouseClient = entry.runtime_data.client
        return client
    return CompaniesHouseClient(async_get_clientsession(hass), entry.data[CONF_API_KEY])


class CompaniesHouseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the account config flow."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the API key and test it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            await self.async_set_unique_id(key_unique_id(api_key))
            self._abort_if_unique_id_configured()
            error = await async_validate_key(self.hass, api_key)
            if error is None:
                return self.async_create_entry(
                    title=MANUFACTURER, data={CONF_API_KEY: api_key}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=API_KEY_SCHEMA,
            errors=errors,
            description_placeholders={"developer_hub": DEVELOPER_HUB_URL},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a replacement key."""
        return await self._async_step_replace_key("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the key of an existing entry."""
        return await self._async_step_replace_key("reconfigure", user_input)

    async def _async_step_replace_key(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = (
            self._get_reauth_entry()
            if step_id == "reauth_confirm"
            else self._get_reconfigure_entry()
        )
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            unique_id = key_unique_id(api_key)
            await self.async_set_unique_id(unique_id)
            if unique_id != entry.unique_id:
                self._abort_if_unique_id_configured()
            error = await async_validate_key(self.hass, api_key)
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=unique_id,
                    data_updates={CONF_API_KEY: api_key},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id=step_id, data_schema=API_KEY_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return CompaniesHouseOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the two subentry flows."""
        return {
            SUBENTRY_TYPE_COMPANY: CompanySubentryFlow,
            SUBENTRY_TYPE_OFFICER: OfficerSubentryFlow,
        }


def _projection(entry: ConfigEntry, multiplier: float) -> float:
    tiers: list[Tier] = []
    if entry.state is ConfigEntryState.LOADED:
        tiers = [c.tier for c in entry.runtime_data.companies.values()]
    else:
        tiers = [
            Tier.NORMAL
            for s in entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_COMPANY
        ]
    officers = sum(
        1 for s in entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_OFFICER
    )
    return projected_requests_per_window(tiers, officers, multiplier)


class CompaniesHouseOptionsFlow(OptionsFlow):
    """Options: thresholds, document directory, paging, cadence."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        multiplier = float(
            options.get(CONF_CADENCE_MULTIPLIER, DEFAULT_CADENCE_MULTIPLIER)
        )
        if user_input is not None:
            multiplier = float(user_input[CONF_CADENCE_MULTIPLIER])
            if (
                _projection(self.config_entry, multiplier)
                > BUDGET_PROJECTION_MAX_PER_WINDOW
            ):
                errors["base"] = "budget_exceeded"
            else:
                return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DUE_SOON_DAYS,
                    default=options.get(CONF_DUE_SOON_DAYS, DEFAULT_DUE_SOON_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=180,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="d",
                    )
                ),
                vol.Required(
                    CONF_DOCUMENT_DIRECTORY,
                    default=options.get(
                        CONF_DOCUMENT_DIRECTORY, DEFAULT_DOCUMENT_DIRECTORY
                    ),
                ): TextSelector(),
                vol.Required(
                    CONF_MAX_PAGES,
                    default=options.get(CONF_MAX_PAGES, DEFAULT_MAX_PAGES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, max=50, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
                vol.Required(
                    CONF_CADENCE_MULTIPLIER, default=multiplier
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_CADENCE_MULTIPLIER,
                        max=MAX_CADENCE_MULTIPLIER,
                        step=0.5,
                        mode=NumberSelectorMode.SLIDER,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "projected": f"{_projection(self.config_entry, multiplier):.1f}",
                "limit": str(BUDGET_PROJECTION_MAX_PER_WINDOW),
            },
        )


# ---------------------------------------------------------------- companies


def _configured(entry: ConfigEntry, subentry_type: str) -> set[str]:
    """Return the unique ids already configured for a subentry type."""
    return {
        s.unique_id or ""
        for s in entry.subentries.values()
        if s.subentry_type == subentry_type
    }


def _plural(count: Any, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _display_name(name: str) -> str:
    """Turn the officer list's ``SURNAME, Forenames`` into ``Forenames SURNAME``."""
    surname, sep, forenames = name.partition(", ")
    return f"{forenames} {surname}".strip() if sep else name


def _company_label(item: JsonDict) -> str:
    """Render ``Name (12345678) - status - incorporated YYYY``."""
    name = item.get("title") or item.get("company_name") or "?"
    number = item.get("company_number", "?")
    status = item.get("company_status") or "unknown"
    created = str(item.get("date_of_creation") or "")[:4]
    label = f"{name} ({number}) - {status}"
    if created:
        label += f" - incorporated {created}"
    return label


class CompanySubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a monitored company."""

    def __init__(self) -> None:
        """Initialise the search state."""
        self._results: dict[str, JsonDict] = {}
        self._selected: JsonDict = {}

    @property
    def _client(self) -> CompaniesHouseClient:
        return _client_for(self.hass, self._get_entry())

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Search by name, optionally narrowed to a postcode."""
        errors: dict[str, str] = {}
        if user_input is not None:
            query = user_input.get(CONF_QUERY, "").strip()
            postcode = user_input.get(CONF_POSTCODE, "").strip()
            try:
                self._results = await self._search(query, postcode)
            except CompaniesHouseAuthError:
                return self.async_abort(reason="invalid_auth")
            except CompaniesHouseNotFoundError:
                # The API answers a search with no hits with a 404.
                self._results = {}
            except CompaniesHouseError as err:
                LOGGER.debug("Company search failed: %s", err)
                errors["base"] = "cannot_connect"
            if not errors and not self._results:
                errors["base"] = "no_results"
            elif not errors:
                return await self.async_step_select()
        schema = vol.Schema(
            {
                vol.Optional(CONF_QUERY, default=""): TextSelector(),
                vol.Optional(CONF_POSTCODE, default=""): TextSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    async def _search(self, query: str, postcode: str) -> dict[str, JsonDict]:
        if not query and not postcode:
            return {}
        if postcode:
            params: dict[str, Any] = {
                "location": postcode,
                "size": SEARCH_ITEMS_PER_PAGE,
            }
            if query:
                params["company_name_includes"] = query
            raw = await self._client.advanced_search(params)
        else:
            raw = await self._client.search_companies(
                query, items_per_page=SEARCH_ITEMS_PER_PAGE
            )
        results: dict[str, JsonDict] = {}
        for item in raw.get("items") or []:
            if not isinstance(item, dict) or not item.get("company_number"):
                continue
            number = str(item["company_number"])
            results[number] = {
                "company_number": number,
                "title": item.get("title") or item.get("company_name"),
                "company_status": item.get("company_status"),
                "date_of_creation": item.get("date_of_creation"),
            }
        return results

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a company from the results. Ones already watched are marked."""
        watched = _configured(self._get_entry(), SUBENTRY_TYPE_COMPANY)
        if user_input is not None:
            if user_input[CONF_SELECTION] in watched:
                return self.async_abort(reason="already_configured")
            self._selected = self._results[user_input[CONF_SELECTION]]
            return await self.async_step_confirm()
        options = [
            SelectOptionDict(
                value=number,
                label=_company_label(item)
                + (" (already watching)" if number in watched else ""),
            )
            for number, item in self._results.items()
        ]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTION): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    def _settings_schema(self, data: Mapping[str, Any]) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_DATASETS,
                    default=list(
                        data.get(CONF_DATASETS, [d.value for d in OPTIONAL_DATASETS])
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[d.value for d in OPTIONAL_DATASETS],
                        multiple=True,
                        translation_key="datasets",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    CONF_CLOSE_WATCH, default=bool(data.get(CONF_CLOSE_WATCH, False))
                ): BooleanSelector(),
                vol.Optional(
                    CONF_LABEL, default=data.get(CONF_LABEL, "")
                ): TextSelector(),
            }
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose datasets, close watch and a label, then create the subentry."""
        number = str(self._selected["company_number"])
        name = str(self._selected.get("title") or number)
        if user_input is not None:
            entry = self._get_entry()
            if any(
                s.subentry_type == SUBENTRY_TYPE_COMPANY and s.unique_id == number
                for s in entry.subentries.values()
            ):
                return self.async_abort(reason="already_configured")
            label = user_input.get(CONF_LABEL, "").strip()
            return self.async_create_entry(
                title=f"{name} ({label})" if label else name,
                data={
                    CONF_COMPANY_NUMBER: number,
                    CONF_COMPANY_NAME: name,
                    CONF_DATASETS: user_input[CONF_DATASETS],
                    CONF_CLOSE_WATCH: user_input[CONF_CLOSE_WATCH],
                    CONF_LABEL: label,
                },
                unique_id=number,
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=self._settings_schema({}),
            description_placeholders={"company": name, "number": number},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Offer the settings or the officer shortcut."""
        return self.async_show_menu(
            step_id="reconfigure", menu_options=["settings", "track_officer"]
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change datasets, close watch or the label."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            label = user_input.get(CONF_LABEL, "").strip()
            name = subentry.data.get(
                CONF_COMPANY_NAME, subentry.data[CONF_COMPANY_NUMBER]
            )
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=f"{name} ({label})" if label else name,
                data_updates={
                    CONF_DATASETS: user_input[CONF_DATASETS],
                    CONF_CLOSE_WATCH: user_input[CONF_CLOSE_WATCH],
                    CONF_LABEL: label,
                },
            )
        return self.async_show_form(
            step_id="settings",
            data_schema=self._settings_schema(subentry.data),
            description_placeholders={
                "company": subentry.data.get(CONF_COMPANY_NAME, ""),
                "number": subentry.data[CONF_COMPANY_NUMBER],
            },
        )

    async def async_step_track_officer(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """List this company's current officers; one click creates the officer subentry."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        number = subentry.data[CONF_COMPANY_NUMBER]
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = self._results[user_input[CONF_SELECTION]]
            officer_id = str(chosen["officer_id"])
            if any(
                s.subentry_type == SUBENTRY_TYPE_OFFICER and s.unique_id == officer_id
                for s in entry.subentries.values()
            ):
                return self.async_abort(reason="officer_already_configured")
            dob = chosen.get("date_of_birth") or {}
            self.hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(
                        {
                            CONF_OFFICER_ID: officer_id,
                            CONF_OFFICER_NAME: _display_name(str(chosen["name"])),
                            CONF_DATE_OF_BIRTH_MONTH: dob.get("month"),
                            CONF_DATE_OF_BIRTH_YEAR: dob.get("year"),
                        }
                    ),
                    subentry_type=SUBENTRY_TYPE_OFFICER,
                    title=_display_name(str(chosen["name"])),
                    unique_id=officer_id,
                ),
            )
            return self.async_abort(
                reason="officer_added",
                description_placeholders={"name": _display_name(str(chosen["name"]))},
            )
        if not self._results:
            try:
                self._results = await self._current_officers(entry, number)
            except CompaniesHouseNotFoundError:
                self._results = {}
            except CompaniesHouseError as err:
                LOGGER.debug("Officer list failed: %s", err)
                errors["base"] = "cannot_connect"
            if not errors and not self._results:
                return self.async_abort(reason="no_officers")
        followed = _configured(entry, SUBENTRY_TYPE_OFFICER)
        options = [
            SelectOptionDict(
                value=key,
                label=f"{item['name']} - {item.get('role') or ''}"
                + (f" - born {item['dob_display']}" if item.get("dob_display") else "")
                + (" (already following)" if key in followed else ""),
            )
            for key, item in self._results.items()
        ]
        return self.async_show_form(
            step_id="track_officer",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTION): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "company": subentry.data.get(CONF_COMPANY_NAME, number)
            },
        )

    async def _current_officers(
        self, entry: ConfigEntry, number: str
    ) -> dict[str, JsonDict]:
        """Return the active officers, from the runtime when loaded."""
        officers = None
        if entry.state is ConfigEntryState.LOADED:
            for company in entry.runtime_data.companies.values():
                if company.company_number == number and company.officers is not None:
                    officers = company.officers.data
        if officers is None:
            officers = await self._client.get_officers(number)
        results: dict[str, JsonDict] = {}
        for officer in officers.items:
            if not officer.is_active or not officer.officer_id:
                continue
            results[officer.officer_id] = {
                "officer_id": officer.officer_id,
                "name": officer.name,
                "role": officer.officer_role,
                "date_of_birth": officer.date_of_birth.to_storage()
                if officer.date_of_birth
                else None,
                "dob_display": officer.date_of_birth.display()
                if officer.date_of_birth
                else None,
            }
        return results


# ---------------------------------------------------------------- officers


class OfficerSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure a tracked officer."""

    def __init__(self) -> None:
        """Initialise the search state."""
        self._results: dict[str, JsonDict] = {}

    @property
    def _client(self) -> CompaniesHouseClient:
        return _client_for(self.hass, self._get_entry())

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Search officers by name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            query = user_input[CONF_QUERY].strip()
            try:
                raw = await self._client.search_officers(
                    query, items_per_page=SEARCH_ITEMS_PER_PAGE
                )
            except CompaniesHouseAuthError:
                return self.async_abort(reason="invalid_auth")
            except CompaniesHouseNotFoundError:
                raw = {"items": []}
            except CompaniesHouseError as err:
                LOGGER.debug("Officer search failed: %s", err)
                errors["base"] = "cannot_connect"
            if not errors:
                self._results = {}
                for item in raw.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    raw_links = item.get("links")
                    links: JsonDict = raw_links if isinstance(raw_links, dict) else {}
                    officer_id = officer_id_from_link(links.get("self"))
                    if not officer_id:
                        continue
                    dob = DateOfBirth.from_api(item.get("date_of_birth"))
                    self._results[officer_id] = {
                        "officer_id": officer_id,
                        "name": item.get("title") or officer_id,
                        "appointment_count": item.get("appointment_count"),
                        "date_of_birth": dob.to_storage() if dob else None,
                        "dob_display": dob.display() if dob else None,
                    }
                if not self._results:
                    errors["base"] = "no_results"
                else:
                    return await self.async_step_select()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({vol.Required(CONF_QUERY): TextSelector()}), user_input
            ),
            errors=errors,
        )

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the officer; the date of birth is what separates namesakes."""
        if user_input is not None:
            chosen = self._results[user_input[CONF_SELECTION]]
            officer_id = str(chosen["officer_id"])
            if any(
                s.subentry_type == SUBENTRY_TYPE_OFFICER and s.unique_id == officer_id
                for s in self._get_entry().subentries.values()
            ):
                return self.async_abort(reason="already_configured")
            dob = chosen.get("date_of_birth") or {}
            return self.async_create_entry(
                title=str(chosen["name"]),
                data={
                    CONF_OFFICER_ID: officer_id,
                    CONF_OFFICER_NAME: chosen["name"],
                    CONF_DATE_OF_BIRTH_MONTH: dob.get("month"),
                    CONF_DATE_OF_BIRTH_YEAR: dob.get("year"),
                },
                unique_id=officer_id,
            )
        followed = _configured(self._get_entry(), SUBENTRY_TYPE_OFFICER)
        options = [
            SelectOptionDict(
                value=key,
                label=f"{item['name']}"
                + (f" - born {item['dob_display']}" if item.get("dob_display") else "")
                + (
                    f" - {_plural(item['appointment_count'], 'appointment', 'appointments')}"
                    if item.get("appointment_count") is not None
                    else ""
                )
                + (" (already following)" if key in followed else ""),
            )
            for key, item in self._results.items()
        ]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SELECTION): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change the display name."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            name = (
                user_input[CONF_OFFICER_NAME].strip()
                or subentry.data[CONF_OFFICER_NAME]
            )
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=name,
                data_updates={CONF_OFFICER_NAME: name},
            )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_OFFICER_NAME,
                        default=subentry.data.get(CONF_OFFICER_NAME, ""),
                    ): TextSelector()
                }
            ),
            description_placeholders={"officer_id": subentry.data[CONF_OFFICER_ID]},
        )
