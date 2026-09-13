# Companies House integration for Home Assistant

Complete v1 build specification. Paste all of this into Claude Code in an empty directory, and keep it in the repo as `SPEC.md`.

---

## 1. What this is

A custom Home Assistant integration that puts the UK Companies House public register into Home Assistant as **devices and entities**, configured entirely through the UI. One device per monitored company, one device per monitored officer. No YAML, ever.

It exists to answer three questions without anyone having to remember to look:

1. **Are my own companies about to miss a statutory deadline?**
2. **Has a counterparty just done something I should know about?** A filing, a change of registered office, a new charge, a director leaving, administration, or a first gazette notice for compulsory strike off.
3. **What is this person doing?** Every appointment a tracked director holds across every company, plus the disqualified directors register.

Everything must be usable as an ordinary Home Assistant automation trigger.

## 2. Scale this is designed for

- **30 companies** and **30 officers**, with headroom to roughly triple that.
- The design must be **conspicuously respectful of the API**. Target under 1 percent of the published allowance in steady state. Section 6 sets out how, and it is the most important section in this document.

## 3. Design principles, in priority order

1. **One system.** A single custom integration installed through HACS. No companion service, no separate database, no MQTT bridge. Documents, archiving and anything else get built around it later, against the events it fires.
2. **Poll what changes, when it changes.** Never sweep everything on a timer because a timer is easy. See section 6.
3. **Keep it simple.** Fewer, better entities. Everything the API exposes is reachable, but only what someone would actually look at is enabled by default.
4. **Home Assistant is the signal layer.** It notices, alerts and shows. It is not a document archive or a reporting engine.
5. **Nothing is guessed.** Every field and endpoint is checked against the live specification before it is used.

## 4. Target environment

- Home Assistant Core **2026.9.2**, Home Assistant OS 18.2, Python **3.14**
- Installed via **HACS** as a custom repository, category `integration`
- Timezone `Europe/London`
- Recorder is SQLite and already over 3 GB, so attribute payloads stay small

Modern APIs only. No `async_setup_platform`, no `hass.data[DOMAIN]` where `entry.runtime_data` fits, no module level `SCAN_INTERVAL`, no `device_state_attributes`, no `async_add_job`.

## 5. Naming

- Domain: `companies_house`
- Repository: `ha-companies-house`
- Title: "Companies House"

An existing one star HACS integration, `YukariChiba/hass_companieshouse`, holds the same domain with thirteen basic sensors. Note the clash in the README. Do not try to be compatible with it.

---

## 6. Rate limits and the polling model

### 6.1 What the API allows

**600 requests per 5 minute rolling window.** Over that, everything returns `429` until the window rolls. That is the only published limit. No daily cap. No separately published limit for the Document API, so count downloads against the same budget.

Four headers come back on every response. Read them, expose them, and trust them over any local counter when they disagree:

| Header | Meaning |
|---|---|
| `X-Ratelimit-Limit` | Ceiling for the window, normally `600` |
| `X-Ratelimit-Remain` | Requests left in this window |
| `X-Ratelimit-Reset` | Unix timestamp when the window rolls |
| `X-Ratelimit-Window` | Window length, normally `5m` |

They are absent from the official guides but present in responses. Never crash on a missing header.

**One key, one budget.** Companies House say they do not encourage mass use of API keys, that they block IPs causing service degradation, and that they reserve the right to ban applications that regularly exceed or bypass the limits. Whether the limit is per key or per account is undocumented, which is reason enough to assume one budget and stay far under it.

### 6.2 How often the data actually changes

This is the design input that matters, so reason from it rather than from the allowance.

A typical active small company touches its register **two to ten times a year**. Accounts once, confirmation statement once, and a handful of officer, address or charge events. Most changes cluster around the two annual deadlines. Filings are submitted overwhelmingly during UK business hours, and electronic filings appear on the register within minutes of submission. Paper filings lag by days no matter how often you ask.

More importantly: **almost every change to a company's register data is accompanied by a filing.**

| Change | Filing that accompanies it |
|---|---|
| Officer appointed, resigned or amended | AP01 to AP04, TM01, TM02, CH01 to CH04 |
| Registered office moved | AD01, and AD02 to AD04 for SAIL |
| Company name changed | NM01 and relatives |
| PSC notified, ceased or changed | PSC01 to PSC09 |
| Charge created, satisfied or acquired | MR01, MR02, MR04, MR05 |
| Share capital changed | SH01 and relatives |
| Accounting reference date changed | AA01 |
| Accounts or confirmation statement filed | AA, CS01 |
| Strike off proposed or discontinued | GAZ1, GAZ2 |
| Insolvency proceedings | LIQ and insolvency category filings |
| Dissolved | DISS40, DISS42 |

So the filing history is the company's own change feed, and it can be probed with **one request**.

The exceptions, which are the reason a safety net still exists: the `registered_office_is_in_dispute` and `undeliverable_registered_office_address` flags, occasional status transitions that lag their filing, and any correction made without a new transaction.

### 6.3 The model: filing led refresh

**Probe.** `GET /company/{n}/filing-history?items_per_page=1`. One request. Compare `total_count` and the newest `transaction_id` against the stored values. Verify that the default sort is newest first; if that is not guaranteed, key the comparison on `total_count` alone.

**On a probe hit**, refresh the profile immediately, then refresh only the detail endpoints the filing category implicates:

| Filing category | Refresh |
|---|---|
| `officers` | officers |
| `persons-with-significant-control` | PSC and PSC statements |
| `mortgage` | charges |
| `insolvency`, `gazette` | insolvency and profile |
| `accounts`, `confirmation-statement`, `address`, `change-of-name`, `capital`, `resolution` | profile only |
| anything unrecognised | profile, officers, PSC and charges |

**Safety net.** A full reconciliation of every dataset for every company **once a week**, staggered so roughly one company reconciles every few hours. A clever optimisation must never be the only path to correctness.

**Profile** is polled on its own daily regardless, because of the exceptions above, and every 6 hours when a company is within 14 days of a deadline so the overdue alert clears promptly once they file.

**Structure** (registers, exemptions, UK establishments) monthly. These effectively never change.

### 6.4 Adaptive cadence

The probe interval comes from three inputs, evaluated per company on every scheduling decision.

**Business hours.** Companies House processes filings in UK working hours. Fast tier applies **Monday to Friday, 08:00 to 18:30 Europe/London**, excluding England and Wales bank holidays. Outside that, back right off. Ship a static bank holiday table covering the next few years and fall back gracefully when it runs out.

**Deadline proximity.** A company with accounts due in twelve days is worth watching. One with nothing due for eight months is not.

**Activity.** A company that has not filed in six months with no deadline within ninety days is quiet. A dissolved or removed company is finished.

| Company state | Probe, business hours | Probe, out of hours |
|---|---|---|
| Close watch (manual per company toggle) | 15 min | 1 hour |
| Deadline within 30 days | 30 min | 3 hours |
| Normal active | 2 hours | 12 hours |
| Quiet: no filing in 6 months and no deadline within 90 days | 6 hours | 24 hours |
| Dissolved or removed | 30 days | 30 days |

State is recomputed after every profile refresh and at each midnight rollover. Log the transition at debug level so the cadence is explicable.

**Officers** are their own case. An officer's appointment list changes when they are appointed to or resign from **any** company, including ones not monitored, so there is no cheap change feed and it has to be polled. It changes a few times a year per person at most.

| Officer dataset | Interval |
|---|---|
| Appointments | 24 hours, start time derived from a hash of the officer id so the thirty spread across the day |
| Disqualification check | 7 days |

### 6.5 What that costs

Worked for 30 companies and 30 officers, assuming 2 on close watch, 4 inside a 30 day deadline, 20 normal, 4 quiet.

| Source | Requests per weekday |
|---|---|
| Probes, business hours | about 280 |
| Probes, out of hours | about 70 |
| Profile, daily plus deadline proximity | about 46 |
| Officer appointments, daily | 30 |
| Disqualification checks, weekly, amortised | about 4 |
| Weekly reconciliation, amortised | about 21 |
| Filing triggered refreshes, amortised | about 6 |
| **Total** | **about 460** |

Against a theoretical 172,800 per day, that is **0.27 percent of the allowance**. Peak load, with jitter and a 2 requests per second cap, stays under 5 requests in any 5 minute window against a ceiling of 600.

Worth stating plainly because it justifies the whole design: a naive sweep of all six endpoints for 30 companies every 6 hours costs about 720 requests a day and gives 6 hour freshness. Filing led refresh costs **fewer** requests and gives **15 to 30 minute** freshness where it matters. Better and cheaper, not a trade.

### 6.6 Implementation rules

- One shared token bucket per config entry, sized from `X-Ratelimit-Limit` and `X-Ratelimit-Window` when seen, defaulting to 600 per 5 minutes. Sustained rate capped at **2 requests per second**.
- **Budget guard.** Never spend past 80 percent of the window on scheduled work. Scheduled requests beyond that defer to the next window. On demand work, a manual refresh or an action call, uses the reserve.
- Jitter every scheduled request within its interval so companies never move in lockstep.
- On `429`, honour `Retry-After` if present, otherwise back off exponentially from 30 seconds, and raise a repair issue if throttling persists past three windows.
- The options flow shows the projected requests per window for the current configuration and refuses a combination exceeding 400.
- Surface it: requests used this window, requests remaining, percent of budget, and next reset, as diagnostic sensors and in the `system_health` panel.
- Log a single debug line per scheduling decision: company, chosen tier, reason, next run.

### 6.7 Explicitly not in v1

**The Streaming API.** It needs a second key, allows two concurrent connections per account, and every stream is a firehose of the entire UK register filtered locally. For data that changes monthly, that is a great deal of machinery to shave fifteen minutes off an alert. Leave a clean seam and do not build it.

---

## 7. The API

### 7.1 Auth

- Public Data API: `https://api.company-information.service.gov.uk`
- Document API: `https://document-api.company-information.service.gov.uk`
- **HTTP Basic**, API key as **username**, **empty password**. Header is `Authorization: Basic base64("<key>:")`. There is no bearer token.
- Keys come from an application registered at `https://developer.company-information.service.gov.uk/`.

### 7.2 Endpoints

Company scoped:

```
GET /company/{company_number}
GET /company/{company_number}/registered-office-address
GET /company/{company_number}/officers
GET /company/{company_number}/appointments/{appointment_id}
GET /company/{company_number}/registers
GET /company/{company_number}/charges
GET /company/{company_number}/charges/{charge_id}
GET /company/{company_number}/filing-history
GET /company/{company_number}/filing-history/{transaction_id}
GET /company/{company_number}/insolvency
GET /company/{company_number}/exemptions
GET /company/{company_number}/uk-establishments
GET /company/{company_number}/persons-with-significant-control
GET /company/{company_number}/persons-with-significant-control-statements
GET /company/{company_number}/persons-with-significant-control-statements/{statement_id}
GET /company/{company_number}/persons-with-significant-control/individual/{notification_id}
GET /company/{company_number}/persons-with-significant-control/corporate-entity/{notification_id}
GET /company/{company_number}/persons-with-significant-control/legal-person/{notification_id}
GET /company/{company_number}/persons-with-significant-control/super-secure/{super_secure_id}
GET /company/{company_number}/persons-with-significant-control/individual-beneficial-owner/{notification_id}
GET /company/{company_number}/persons-with-significant-control/corporate-entity-beneficial-owner/{notification_id}
GET /company/{company_number}/persons-with-significant-control/legal-person-beneficial-owner/{notification_id}
GET /company/{company_number}/persons-with-significant-control/super-secure-beneficial-owner/{super_secure_id}
```

Officer scoped:

```
GET /officers/{officer_id}/appointments
GET /disqualified-officers/natural/{officer_id}
GET /disqualified-officers/corporate/{officer_id}
```

Search:

```
GET /search
GET /search/companies
GET /search/officers
GET /search/disqualified-officers
GET /advanced-search/companies
GET /alphabetical-search/companies
GET /dissolved-search/companies
```

Documents:

```
GET https://document-api.company-information.service.gov.uk/document/{document_id}
GET https://document-api.company-information.service.gov.uk/document/{document_id}/content
```

Every one of these is reachable in v1, whether polled or exposed as an action.

### 7.3 Key response fields

**Company profile:** `company_name`, `company_number`, `company_status`, `company_status_detail`, `type`, `subtype`, `jurisdiction`, `date_of_creation`, `date_of_cessation`, `accounts` (with `next_accounts.due_on`, `next_accounts.period_start_on`, `next_accounts.period_end_on`, `last_accounts.made_up_to`, `last_accounts.type`, `accounting_reference_date`, `overdue`), `confirmation_statement` (with `next_due`, `next_made_up_to`, `last_made_up_to`, `overdue`), `registered_office_address`, `registered_office_is_in_dispute`, `undeliverable_registered_office_address`, `sic_codes`, `previous_company_names`, `links`, `can_file`, `etag`, `super_secure_managing_officer_count`, `annual_return`, `branch_company_details`, `foreign_company_details`, `corporate_annotation`, `last_full_members_list_date`, `partial_data_available`, `external_registration_number`.

**Filing history list:** `total_count`, `items_per_page`, `start_index`, `filing_history_status`, `etag`, `items[]`. Each item: `transaction_id`, `category`, `subcategory`, `type`, `date`, `description`, `description_values`, `barcode`, `pages`, `paper_filed`, `links.document_metadata`, `links.self`, `annotations[]`, `associated_filings[]`, `resolutions[]`.

**Officer list:** `active_count`, `resigned_count`, `total_results`, `items_per_page`, `start_index`, `kind`, `etag`, `items[]`. Each item: `name`, `officer_role`, `appointed_on`, `appointed_before`, `resigned_on`, `date_of_birth` (month and year only), `nationality`, `occupation`, `country_of_residence`, `former_names[]`, `address`, `identification` (with `identification_type`, `registration_number`, `legal_form`, `legal_authority`, `place_registered`), `links.self`, `links.officer.appointments`.

### 7.4 Quirks that will bite you

- `has_charges`, `has_insolvency_history`, `has_been_liquidated` and `is_community_interest_company` on the profile are **deprecated**. Derive those from the `links` object and the dedicated endpoints. Fall back to the flags only when the link is absent.
- `company_status_detail` is where `active-proposal-to-strike-off` lives. Along with insolvency, that is the single most valuable alert here. Treat it as first class.
- Natural officers return **month and year of birth only**. Never fabricate a day.
- `appointed_before` replaces `appointed_on` for pre 1992 appointments.
- The officer id used by `/officers/{officer_id}/appointments` is **not** the appointment id. Parse it out of `links.officer.appointments`.
- Lists paginate with `items_per_page` and `start_index`, and search endpoints cap paging depth. Page defensively with a configured maximum.
- Dissolved companies still return a profile. That is not an error. Mark the company, drop it to the dissolved cadence, keep the entities.
- Filing descriptions use `description` as an enumeration key with `description_values` to interpolate. Render them properly rather than showing the raw key. Fetch the enumeration mapping from the specification repository once and ship it as a constant.
- Dates are `YYYY-MM-DD` with no timezone. Parse as dates. Where a timestamp entity is needed, localise to Europe/London at start of day, and test at a BST boundary, because an accounts deadline at the end of October must not move a day.

---

## 8. Architecture

### 8.1 Config entry and subentries

One **config entry per API key**, the account. Monitored things are **config subentries**, added and removed from the integration page without touching the entry.

| Subentry | Represents | Stored |
|---|---|---|
| `company` | One company on the register | `company_number`, enabled datasets, close watch flag, optional label |
| `officer` | One person or corporate officer across every appointment | `officer_id`, display name, date of birth for match confirmation |

Implement `async_get_supported_subentry_types` returning a `ConfigSubentryFlow` per type, each supporting `async_step_user` and `async_step_reconfigure` via `self._get_entry()` and `self._get_reconfigure_subentry()`.

Two subentry types only. A saved search watchlist is where this stops being simple. Leave it out.

### 8.2 Devices

| Device | Owner | Key attributes |
|---|---|---|
| Service device, "Companies House" | config entry | Holds account diagnostics |
| Company device | `company` subentry | `name` company name, `model` company type, `model_id` company subtype, `serial_number` company number, `manufacturer` "Companies House", `via_device` service device, `configuration_url` `https://find-and-update.company-information.service.gov.uk/company/{number}` |
| Officer device | `officer` subentry | `name` officer name, `model` officer role, `via_device` service device, `configuration_url` `https://find-and-update.company-information.service.gov.uk/officers/{officer_id}/appointments` |

Pass `config_subentry_id` explicitly when registering. It is not inherited, and omitting it is an error. Deleting a subentry must remove its device and every entity on it, and nothing else.

### 8.3 Coordinators

One `DataUpdateCoordinator` subclass per dataset rather than one for everything, so a failing charges endpoint does not blank the profile sensors. Use `_async_setup` for one time setup work.

Coordinators are **not** all on fixed timers. The probe coordinator owns the schedule described in section 6 and drives the others. Implement the cadence with `async_call_later` reschedules rather than a fixed `update_interval`, and expose the next scheduled run as a diagnostic attribute.

| Coordinator | Trigger |
|---|---|
| `probe` | adaptive schedule, section 6.4 |
| `profile` | daily, on probe hit, every 6 hours when within 14 days of a deadline |
| `officers`, `psc`, `charges`, `insolvency` | on probe hit filtered by category, plus weekly reconciliation |
| `structure` | monthly |
| `appointments` | daily per officer, hash spread |
| `disqualification` | weekly per officer |

### 8.4 Change detection and persistence

Compare each fetch to the last and persist the comparison with a versioned `homeassistant.helpers.storage.Store` per config entry, so a restart does not replay fifty filings as new events.

Per company: the set of seen filing `transaction_id`s with the newest date and `total_count`, a hash per officer appointment, a hash per PSC notification, a charge id to status map, and the last seen `company_status`, `company_status_detail`, name, registered office, SIC codes and accounting reference date. Per officer: the set of seen appointments and the last disqualification result.

**On first setup, seed the store silently and fire no events.** Test this explicitly. Cap the stored transaction id set at the most recent 500 per company so the store cannot grow without bound.

---

## 9. Entities

`has_entity_name = True` throughout, `_attr_translation_key` on everything, all display strings in `strings.json` and `translations/en.json`. No hardcoded English in Python.

Attributes stay small. No dumping full officer lists, filing history or PSC lists onto entities that change often. Cap any list attribute at ten items, except the officer appointments list which caps at fifty. Document a `recorder:` exclude example for the heaviest entities.

The **Default** column is what keeps this readable. Everything is present, but a company device page shows about fifteen rows out of the box, not sixty. `off` means `entity_registry_enabled_default = False`.

### 9.1 Company sensors

| Sensor | Device class | Default |
|---|---|---|
| Next deadline, soonest of accounts due and confirmation statement due | date | on |
| Next deadline type | enum | on |
| Days to next deadline | duration, unit `d` | on |
| Accounts next due | date | on |
| Confirmation statement next due | date | on |
| Company status | enum | on |
| Last filing date | date | on |
| Last filing description, rendered | none | on |
| Officers active | none | on |
| PSC active | none | on |
| Charges outstanding | none | on |
| Company name | none | diagnostic |
| Company status detail | enum | diagnostic |
| Company type, subtype, jurisdiction | enum | diagnostic |
| Date of creation, date of cessation | date | diagnostic |
| Registered office address, one line, structured in attributes | none | diagnostic |
| Polling tier, current adaptive state | enum | diagnostic |
| Company age | none, unit `y` | off |
| Accounts next made up to, last made up to, next period start | date | off |
| Accounting reference date, last accounts type | none | off |
| Confirmation statement next made up to, last made up to | date | off |
| Days to accounts due, days to confirmation statement due, negative when overdue | duration, unit `d` | off |
| SIC codes, state is the count, codes with descriptions in attributes | none | off |
| Primary SIC description | none | off |
| Officers total, officers resigned, directors active, secretaries active, LLP members active | none | off |
| PSC total, PSC statements | none | off |
| Charges total, part satisfied, satisfied | none | off |
| Filings total, filings last 12 months, days since last filing, last filing category | none | off |
| Previous names count, names in attributes | none | off |
| UK establishments count, insolvency cases count | none | off |
| Registers held | none | off |

Enum sensors need an explicit `options` list. Company status options: `active`, `dissolved`, `liquidation`, `receivership`, `administration`, `voluntary-arrangement`, `converted-closed`, `insolvency-proceedings`, `registered`, `removed`, `closed`, `open`.

Truncate the registered office one liner to fit the 255 character state limit.

### 9.2 Company binary sensors

`BinarySensorDeviceClass.PROBLEM`, all on by default:

- Accounts overdue
- Confirmation statement overdue
- Accounts due soon, threshold configurable, default 30 days
- Confirmation statement due soon
- **Proposed strike off**, from `company_status_detail == "active-proposal-to-strike-off"` and from GAZ1 filings
- **Insolvent**, true for liquidation, receivership, administration, voluntary arrangement or insolvency proceedings, with the specific status in an attribute
- Registered office in dispute
- Undeliverable registered office

No device class, on by default: **Is active**.

No device class, off by default: can file, has outstanding charges, has insolvency history, has super secure officers, has exemptions.

### 9.3 Company calendar

One `calendar` entity per company carrying accounts due, confirmation statement due, next accounts period end and the accounting reference date as all day events, each with a stable `uid` and the company number in the description.

Plus **one aggregate calendar on the service device** holding every monitored company's deadlines, titled `{Company} - {deadline type}`. That aggregate is what goes on the dashboard, so it matters more than the per company ones.

### 9.4 Company event entities

`event` platform. Declare `event_types`, fire with `self._trigger_event(type, payload)` then `async_write_ha_state()`.

| Entity | Event types | Payload |
|---|---|---|
| Filing | `accounts`, `confirmation-statement`, `officers`, `address`, `capital`, `charges`, `mortgage`, `change-of-name`, `resolution`, `incorporation`, `gazette`, `insolvency`, `other` | transaction id, date, description, rendered description, category, subcategory, barcode, document id, paper filed, pages |
| Officer change | `appointed`, `resigned`, `details-changed` | name, role, appointed on, resigned on, officer id |
| PSC change | `notified`, `ceased`, `statement-added`, `details-changed` | name, kind, natures of control, notified on, ceased on |
| Charge change | `created`, `satisfied`, `part-satisfied`, `acquired` | charge code, persons entitled, created on, delivered on, status |
| Status change | `status-changed`, `strike-off-proposed`, `strike-off-discontinued`, `dissolved` | old status, new status, detail |
| Profile change | `name-changed`, `address-changed`, `sic-changed`, `accounting-reference-date-changed` | old value, new value |

Also fire **one bus event**, `companies_house_event`, carrying `company_number`, `company_name`, `kind` and the same payload. Event entities are the correct per device primitive, but a single bus event lets one automation cover the whole portfolio, which is how it gets used. Document both with a worked example of each.

### 9.5 Officer entities

| Entity | Type | Default |
|---|---|---|
| Appointments active | sensor | on |
| Appointments total | sensor | on |
| Most recent company | sensor | on |
| Last appointment date | sensor, date | on |
| Disqualified | binary, problem | on |
| Has active appointments | binary | on |
| Appointments resigned, first appointment date | sensor | off |
| Nationality, country of residence, occupation, date of birth month and year, officer role | sensor | diagnostic |

The appointments sensor carries the full list in attributes: company number, company name, role, appointed on, resigned on, company status. Cap at fifty.

Event entity `appointment`, types `appointed`, `resigned`, `company-status-changed`, `disqualified`.

**Disqualification matching must be honest.** `/search/disqualified-officers` matches on name, and names collide constantly. Only set the binary sensor true on an exact match of surname, forename **and** date of birth month and year. Anything weaker goes into a `possible_matches` attribute with a count and does not flip the sensor. Say so in the README. A false "your director is disqualified" is worse than no alert at all.

### 9.6 Service device entities

All `EntityCategory.DIAGNOSTIC`: requests used this window, requests remaining, percent of budget used, window resets at (timestamp), last successful update, last error, companies monitored, officers monitored, next scheduled probe.

---

## 10. Actions

All read actions use `SupportsResponse.ONLY`. Register them in `async_setup`, not `async_setup_entry`, so they exist without a config entry (quality scale rule `action-setup`). Full selectors and translated field names in `services.yaml`. Raise `ServiceValidationError` with a `translation_key` for bad input, never a bare string.

| Action | Fields |
|---|---|
| `search_companies` | `query`, `items_per_page`, `start_index` |
| `advanced_search` | `company_name_includes`, `company_name_excludes`, `location`, `postcode`, `sic_codes`, `company_status`, `company_type`, `incorporated_from`, `incorporated_to`, `dissolved_from`, `dissolved_to`, `size` |
| `alphabetical_search` | `query`, `search_above`, `search_below` |
| `dissolved_search` | `query`, `search_type` |
| `search_officers` | `query`, `items_per_page` |
| `search_disqualified_officers` | `query` |
| `search_all` | `query` |
| `get_company` | `company_number` |
| `get_registered_office` | `company_number` |
| `get_officers` | `company_number`, `register_view`, `order_by`, `items_per_page` |
| `get_officer_appointment` | `company_number`, `appointment_id` |
| `get_officer_appointments` | `officer_id` |
| `get_disqualification` | `officer_id`, `kind` (natural or corporate) |
| `get_filing_history` | `company_number`, `category`, `items_per_page` |
| `get_filing` | `company_number`, `transaction_id` |
| `get_charges` | `company_number` |
| `get_charge` | `company_number`, `charge_id` |
| `get_psc` | `company_number` |
| `get_psc_detail` | `company_number`, `kind`, `notification_id` |
| `get_psc_statements` | `company_number` |
| `get_insolvency` | `company_number` |
| `get_exemptions` | `company_number` |
| `get_registers` | `company_number` |
| `get_uk_establishments` | `company_number` |
| `get_document_metadata` | `document_id` |
| `download_document` | `document_id`, `content_type`, `filename` |
| `refresh` | device or entry target, optional `datasets` |

`download_document` writes to a configurable directory, default `/media/companies_house/{company_number} {company_name}/`, with the filename `YYYY-MM-DD - Companies House - {description} ({company_name}).pdf` using the **filing** date. Sanitise for the filesystem, cap at 200 characters, never overwrite (same hash returns the existing path, different content suffixes ` (2)`), and fire `companies_house_document_downloaded` with the path and filing metadata. Nothing downloads automatically in v1. The archive and any sync to cloud storage is built around this event later.

Register the read actions with the **LLM / Assist API** and give the deadline sensors aliases, so "when is the confirmation statement due for Example Trading" works from voice.

---

## 11. Config flow

**Entry setup**

1. One field, API key, `TextSelector` with `password` type. Validate with `/search/companies?q=test&items_per_page=1`. Map `401` to `invalid_auth`, `429` to `rate_limited`, network failure to `cannot_connect`. Unique id from a hash of the key so the same key cannot be added twice.
2. `async_step_reauth` and `async_step_reauth_confirm`, triggered by any coordinator raising `ConfigEntryAuthFailed` on a `401`.
3. Options flow: due soon threshold in days, document directory, maximum pages per list endpoint, cadence multiplier for people who want everything slower, and a read only line showing projected requests per 5 minute window.

**Adding a company.** Nobody types an eight digit number.

1. Search step: query box plus an optional postcode field, because "the company at this address" is a common need.
2. Results in a `SelectSelector` reading `Name (12345678) - status - incorporated YYYY`.
3. Confirm step: checkboxes for datasets, a close watch toggle, and an optional label such as "Contractor, 47 High Street".

Defaults: everything on, close watch off.

**Adding an officer**

1. Search step calling `/search/officers`.
2. Results reading `Name - born MM/YYYY - N appointments`, because officer names collide constantly and the date of birth is the only separator.
3. Resolve and store the `officer_id` from the appointments link, plus the date of birth for disqualification matching.

**The shortcut that matters.** From a company subentry's reconfigure step, offer "track an officer of this company", listing that company's current officers so one click creates the officer subentry. Far better than searching by name for someone already on screen.

---

## 12. Branding

Since **Home Assistant 2026.3** a custom integration can ship its own brand images with no manifest change and no pull request to the brands repository. Local images take priority over the CDN.

Create a `brand/` folder alongside `__init__.py` and `manifest.json` containing:

```
brand/icon.png          256 x 256
brand/icon@2x.png       512 x 512
brand/logo.png          shortest side 128 to 256
brand/logo@2x.png       shortest side 256 to 512
brand/dark_icon.png     optional, for dark themes
brand/dark_logo.png     optional
```

All PNG, lossless, optimised, interlaced preferred.

**Design the mark yourself.** Do not copy the Companies House crown or GOV.UK branding, and do not use any Home Assistant branded imagery, which is explicitly forbidden for custom integrations because it implies official status. Make something original and plain: a simple monogram or a document and building glyph works, flat, single accent colour, legible at 32 pixels. Generate it as SVG, render the PNGs at the sizes above, and commit both the SVG source and the PNGs.

Also ship `icons.json` with per translation key entity icons and state based variants, for example a deadline sensor that changes icon when overdue.

---

## 13. Home Assistant features to use

Target **Platinum** on the quality scale, with honest exemptions. Ship `quality_scale.yaml` recording every rule below as `done`, `exempt` with a reason, or `todo`.

**Bronze:** `action-setup`, `appropriate-polling`, `brands`, `common-modules`, `config-flow-test-coverage`, `config-flow`, `dependency-transparency`, `docs-actions`, `docs-triggers`, `docs-conditions`, `docs-high-level-description`, `docs-installation-instructions`, `docs-removal-instructions`, `entity-event-setup`, `entity-unique-id`, `has-entity-name`, `runtime-data`, `test-before-configure`, `test-before-setup`, `unique-config-entry`.

**Silver:** `action-exceptions`, `config-entry-unloading`, `docs-configuration-parameters`, `docs-installation-parameters`, `entity-unavailable`, `integration-owner`, `log-when-unavailable`, `parallel-updates`, `reauthentication-flow`, `test-coverage`.

**Gold:** `devices`, `diagnostics`, `discovery-update-info`, `discovery`, `docs-data-update`, `docs-examples`, `docs-known-limitations`, `docs-supported-devices`, `docs-supported-functions`, `docs-troubleshooting`, `docs-use-cases`, `dynamic-devices`, `entity-category`, `entity-device-class`, `entity-disabled-by-default`, `entity-translations`, `exception-translations`, `icon-translations`, `reconfiguration-flow`, `repair-issues`, `stale-devices`.

**Platinum:** `async-dependency`, `inject-websession`, `strict-typing`.

`discovery` and `discovery-update-info` are legitimately `exempt` for a cloud service with no local presence. Everything else is achievable, so achieve it.

Concretely, that means all of the following are in v1:

- Config subentries with per subentry devices
- `entry.runtime_data` with a typed `ConfigEntry` alias, no `hass.data`
- `DataUpdateCoordinator` with `_async_setup`, one per dataset, in `coordinator.py`
- Base entity classes in `entity.py`, `PARALLEL_UPDATES = 0` on every read only platform
- Entity translation keys, `icons.json` icon translations, exception translations via `translation_key`
- `EntityCategory.DIAGNOSTIC` and `entity_registry_enabled_default` used properly
- Device classes on every sensor that has a sensible one, `suggested_display_precision` on the numeric ones
- Diagnostics for config entry and device, redacting the API key, officer dates of birth and residential addresses
- Repair issues with repair flows for: invalid key, sustained rate limiting, a monitored company now dissolved, an officer id that no longer resolves
- `async_remove_config_entry_device` so stale devices can be removed
- Config entry `version` and `minor_version` with a migration path
- A `system_health.py` reporting API reachability and remaining rate budget:

```python
from typing import Any
from homeassistant.components import system_health
from homeassistant.core import HomeAssistant, callback


@callback
def async_register(
    hass: HomeAssistant, register: system_health.SystemHealthRegistration
) -> None:
    register.async_register_info(system_health_info)


async def system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    return {
        "can_reach_server": system_health.async_check_can_reach_url(hass, API_BASE),
        "requests_remaining": ...,
        "window_resets_at": ...,
        "companies_monitored": ...,
    }
```

`manifest.json`: `"config_flow": true`, `"integration_type": "hub"`, `"iot_class": "cloud_polling"`, `"quality_scale": "platinum"`, a `version`, `documentation` and `issue_tracker` URLs, `"requirements": []`, `"loggers": []`.

`hacs.json`: `{"name": "Companies House", "homeassistant": "2026.9.0", "render_readme": true}`.

---

## 14. Repository

```
custom_components/companies_house/
  __init__.py            entry setup, runtime data, subentries, store, services
  api.py                 async client, auth, token bucket, budget guard, typed responses, errors
  const.py               domain, defaults, enums, filing category map, bank holidays
  coordinator.py         probe, profile, officers, psc, charges, insolvency, structure, appointments, disqualification
  scheduler.py           adaptive cadence, business hours, jitter
  models.py              dataclasses and TypedDicts for every API resource used
  config_flow.py         entry, options, company subentry, officer subentry
  entity.py              base classes for company, officer and service entities
  sensor.py
  binary_sensor.py
  event.py
  calendar.py
  diagnostics.py
  repairs.py
  system_health.py
  services.py
  services.yaml
  strings.json
  icons.json
  translations/en.json
  manifest.json
  quality_scale.yaml
  brand/
    icon.png  icon@2x.png  logo.png  logo@2x.png  dark_icon.png  dark_logo.png
tests/
  conftest.py
  fixtures/*.json
  test_api.py  test_scheduler.py  test_config_flow.py  test_init.py
  test_sensor.py  test_binary_sensor.py  test_event.py  test_calendar.py
  test_services.py  test_diagnostics.py  test_repairs.py
  snapshots/
hacs.json
README.md
SPEC.md
brand/source/icon.svg
.github/workflows/validate.yml
.github/workflows/test.yml
```

CI runs hassfest, the HACS validation action with `category: integration`, `ruff check`, `ruff format --check`, `mypy --strict`, and `pytest` with coverage.

## 15. Testing

`pytest-homeassistant-custom-component` with `aioresponses` and `syrupy`.

Record real payloads once by hand and commit as fixtures covering: an ordinary active trading company, a dissolved company, one in liquidation, one with an active proposal to strike off, an LLP, a company with outstanding charges, a company with a corporate PSC, a pre 1992 officer appointment, and an officer with more than twenty appointments. Never commit a real API key.

Must be tested:

- Every config flow path including errors, both subentry flows, reauth, reconfigure.
- First setup seeds the store and fires **no** events. A second fetch with one new filing fires exactly one event of the right type.
- **The scheduler.** A company inside 30 days of a deadline probes every 30 minutes at 10:00 on a Tuesday and every 3 hours at 02:00. A quiet company probes every 6 hours. A dissolved company probes monthly. A bank holiday behaves as out of hours.
- **The filing led refresh.** A probe with an unchanged `total_count` issues no further requests. A probe with a new `officers` category filing refreshes the profile and officers, and not charges or PSC.
- **The token bucket and budget guard.** 700 queued requests never exceed 600 in a simulated 5 minute window. Scheduled work stops at 80 percent while an action call still succeeds. A `429` with `Retry-After` is honoured.
- Date handling at a BST boundary.
- Disqualification matching: exact name and date of birth match sets the sensor, a name only match does not and populates `possible_matches`.
- Snapshot of every entity state and attributes for each fixture company and officer.

Target 95 percent coverage, no skipped tests.

## 16. Build order

Separate commits, Home Assistant loading cleanly after each.

1. Scaffold, manifest, `hacs.json`, CI, brand images, an integration that sets up and unloads.
2. `api.py`: auth, token bucket, budget guard, header parsing, error types, typed models, company profile. Unit tested.
3. Config flow, API key, validation, reauth. Service device, diagnostic entities, `system_health.py`.
4. Company subentry with the search picker. Profile coordinator, company device, profile sensors, deadline and status binary sensors.
5. `scheduler.py` and the probe coordinator. Adaptive cadence, business hours, bank holidays, jitter. Fully unit tested before anything depends on it.
6. Filing history, change store, filing led refresh dispatch, filing event entity, bus event.
7. Officers, PSC, charges, insolvency and structure coordinators, their sensors and event entities, weekly reconciliation.
8. Calendars, per company and aggregate.
9. Officer subentry: device, appointments, disqualification matching, events, and the track an officer of this company shortcut.
10. All actions with response data, `services.yaml`, exception translations, LLM exposure, document download.
11. Diagnostics, repairs and repair flows, icon translations, full translations, `quality_scale.yaml`, README.

## 17. Done means

- Installs from HACS, sets up entirely through the UI, shows its own logo, survives a restart with no replayed events.
- 30 companies and 30 officers sit at **under 1 percent** of the rate limit in steady state, and the diagnostic sensors prove it.
- A new filing on a watched company produces an event within 30 minutes during working hours.
- Deleting a subentry removes exactly its device and entities.
- hassfest, HACS validation, ruff, mypy strict and pytest all pass in CI.
- README ships a dashboard example with the aggregate calendar and a deadline table, plus five automations: notify at 30 and 7 days before any deadline, alert immediately on a strike off proposal or insolvency filing for any monitored company, alert when a tracked officer takes a new appointment, alert on a new charge against a company, and weekly digest of everything filed.

## 18. Do not

- Do not invent endpoints or fields. Check the live specification or leave it out.
- Do not sweep every endpoint on a fixed timer. The filing history is the change feed, and using it is the point of the design.
- Do not create an entity per filing, officer, charge or appointment. Those are events and attributes. Entity count scales with subentries, never with register history.
- Do not put large lists into attributes by default.
- Do not let scheduled work spend past 80 percent of the rate limit window.
- Do not add a second API key to get more budget.
- Do not flip the disqualification sensor on a name only match.
- Do not store the API key anywhere but config entry data, and never log it.
- Do not use Home Assistant branded imagery, or Companies House and GOV.UK branding, in the integration's own icon.
- Do not build the Streaming API, an archive, or any cloud sync in v1.
