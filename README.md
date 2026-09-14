# Companies House for Home Assistant

The UK Companies House public register as Home Assistant devices and entities.
One device per monitored company, one per monitored officer, configured
entirely through the UI, and conspicuously respectful of the API: thirty
companies and thirty officers run at well under 1 percent of the rate limit.

It answers three questions without anyone having to remember to look:

1. **Are my own companies about to miss a statutory deadline?** Accounts and
   confirmation statement due dates as sensors, binary sensors and calendars.
2. **Has a counterparty just done something I should know about?** A filing,
   a change of registered office, a new charge, a director leaving,
   administration, or a first gazette notice for compulsory strike off, as
   event entities and a single bus event.
3. **What is this person doing?** Every appointment a tracked director holds
   across every company, plus an honest check of the disqualified directors
   register.

> **Domain clash.** An older HACS integration, `YukariChiba/hass_companieshouse`,
> uses the same `companies_house` domain with a handful of YAML sensors. This
> integration is not compatible with it. Remove that one before installing this.

## Installation

1. In HACS, open **Integrations → ⋮ → Custom repositories**, add
   `https://github.com/deanbenson/ha-companies-house` with category
   **Integration**, then install **Companies House** and restart Home Assistant.
2. Get an API key: sign in at the
   [Companies House developer hub](https://developer.company-information.service.gov.uk/),
   create an application (live environment, REST API) and copy its key.
3. **Settings → Devices & services → Add integration → Companies House** and
   paste the key. The key is tested with one request before the entry is created.

Requires Home Assistant 2026.9 or later. No YAML, no companion service, no
extra database.

### Installation parameters

| Field | Meaning |
|---|---|
| API key | The REST API key of your developer hub application. Stored only in the config entry; never logged; redacted from diagnostics. |

## Adding companies and officers

On the integration page use **Add company** or **Add officer**.

**Company.** Search by name, or by postcode for "the company at this address"
(a postcode uses the advanced search). Pick from results shown as
`Name (12345678) - status - incorporated YYYY`, then choose which datasets to
monitor, whether to enable **close watch**, and an optional label such as
"Contractor, 47 High Street". Profile and filing history are always monitored.

**Officer.** Search by name and pick from results shown as
`Name - born MM/YYYY - N appointments`. Names collide constantly; the month and
year of birth is what tells people apart. The officer id is resolved from the
result, and the date of birth is kept for disqualification matching.

**The shortcut.** Reconfigure a company and choose **Track an officer of this
company**: its current officers are listed and one click creates the officer
subentry.

Deleting a subentry removes exactly its device and entities, nothing else.

### Configuration parameters (options)

| Option | Default | Meaning |
|---|---|---|
| Due soon threshold | 30 days | When the *due soon* binary sensors turn on. |
| Document directory | `/media/companies_house` | Where `download_document` writes. A folder per company is created inside it. |
| Maximum pages per list | 10 | Cap on pages fetched for long officer, PSC and charge lists (100 items per page). |
| Cadence multiplier | 1.0 | Multiplies every polling interval, for people who want everything slower. |

The options form shows the projected scheduled requests per 5 minute window
and refuses a configuration that would exceed 400.

## How data is updated

Companies House allows **600 requests per 5 minute window** per key. This
integration is designed around how often register data actually changes,
not around the allowance.

Almost every change to a company is accompanied by a filing, so the filing
history is the company's own change feed and can be probed with **one
request**: `GET /company/{n}/filing-history?items_per_page=1`. If the total
count or newest transaction id moved, the profile is refreshed immediately
and then only the datasets the filing category implicates (an officers filing
refreshes officers, a mortgage filing refreshes charges, and so on).

The probe interval adapts per company:

| Company state | Business hours | Out of hours |
|---|---|---|
| Close watch (per company toggle) | 15 min | 1 h |
| Deadline within 30 days, or overdue by up to 90 | 30 min | 3 h |
| Normal active | 2 h | 12 h |
| Quiet: no filing in 6 months, no deadline within 90 days | 6 h | 24 h |
| Dissolved or removed | 30 days | 30 days |

Both deadlines are considered, so an overdue confirmation statement cannot
hide accounts due next week. A deadline more than 90 days in the past no
longer counts as near: that company has stopped filing, and polling it every
half hour would not change that.

Business hours are Monday to Friday 08:00 to 18:30 Europe/London, excluding
England and Wales bank holidays (a table from GOV.UK for 2026 to 2028 with an
algorithmic fallback after that). Every interval is jittered by ±15 percent
so companies never move in lockstep. The `Polling tier` diagnostic sensor
shows the current tier, the reason, and the next probe.

On top of the probe:

* **Profile** daily, every 6 hours within 14 days of a deadline so an overdue
  alert clears promptly, monthly once dissolved.
* **Weekly reconciliation** of every dataset, staggered by a hash of the
  company number, so a clever optimisation is never the only path to correctness.
* **Structure** (registers, exemptions, UK establishments) monthly.
* **Officer appointments** daily, at a time of day derived from a hash of the
  officer id so thirty officers spread across the day. **Disqualification** weekly.

Worked example, 30 companies and 30 officers: about 460 requests per weekday,
**0.27 percent** of the allowance, never more than a handful in any window.

Budget rules: one shared token bucket per key, sized from the
`X-Ratelimit-*` headers (trusted over the local count), a sustained cap of
2 requests per second, and scheduled work never spends past **80 percent** of
a window; the reserve is for your action calls and manual refreshes. A `429`
honours `Retry-After`, otherwise backs off from 30 seconds, and throttling
that persists past three windows raises a repair issue.

Everything fetched is kept in a per entry store together with the change
detection state, so a restart or a reload (every subentry add or remove reloads
the entry) costs no requests while the snapshots are fresh, and never replays
old filings as new events. On the very first look at a company the store is
seeded silently and no events fire.

## Entities

Every entity is enabled. A disabled entity would cost nothing, but neither
does an enabled one that rarely changes: the recorder only writes when a value
changes, and most of these change a few times a year. The device page is long;
the diagnostic section keeps the rarely-needed rows out of the way.

### Company

Sensors on by default: next deadline, next deadline type, days to next
deadline, accounts next due, confirmation statement next due, company status,
last filing date, last filing (rendered description), officers active, PSC
active, charges outstanding. Diagnostic: company name, status detail, type,
subtype, jurisdiction, date of creation, date of cessation, registered office
address (structured in attributes), polling tier.

Also: company age, accounts and confirmation statement made-up-to and
period dates, accounting reference date, last accounts type, days to each
deadline (negative once overdue), SIC codes (count, descriptions in
attributes), primary SIC description, officer counts by role, PSC totals and
statements, charge counts by status, filings total and last 12 months, days
since last filing, last filing category, previous names, UK establishments,
insolvency cases, registers held.

Binary sensors (problem class, on by default): accounts overdue, confirmation
statement overdue, accounts due soon, confirmation statement due soon,
**proposed strike off** (from `company_status_detail` and from GAZ1 filings),
**insolvent** (liquidation, receivership, administration, voluntary
arrangement, insolvency proceedings), registered office in dispute,
undeliverable registered office. Plus *is active*, can file, has outstanding
charges, has insolvency history, has officers with protected details, has
exemptions.

A **calendar** per company with accounts due, confirmation statement due,
accounts period end and the accounting reference date as all day events.

### Officer

Appointments active, appointments total (with the full list in attributes,
capped at fifty), most recent company, last appointment date, disqualified
(problem), currently holds appointments, appointments resigned, first
appointment date; diagnostic: nationality, country of residence, occupation,
date of birth (month and year only), officer role.

**Disqualification matching is honest.** The register search matches on name,
and names collide constantly. The *Disqualified* sensor only turns on for an
exact match of surname, forename **and** month and year of birth. Anything
weaker is counted in the `possible_matches` attribute and does not flip the
sensor. A false "your director is disqualified" is worse than no alert at all.

### Service device

One "Companies House" device per API key with the aggregate **All deadlines**
calendar (every monitored company, titled `Company - deadline type`; this is
the one for the dashboard) and diagnostics: requests used this window,
requests remaining, budget used, window resets at, last successful update,
last error, companies monitored, officers monitored, next scheduled probe.

### Attribute size

Attributes are kept small (lists capped at ten, appointments at fifty). If the
recorder is large, exclude the heaviest entities:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.*_appointments_active
      - sensor.*_officers_active
      - sensor.*_psc_active
      - sensor.*_charges_outstanding
```

## Triggers

Every change is delivered two ways.

**Event entities**, one per kind of change on each device: `filing`,
`officer_change`, `psc_change`, `charge_change`, `status_change`,
`profile_change` on companies and `appointment` on officers. The entity's
`event_type` attribute carries the type and the other attributes carry the
payload. Trigger on one company:

```yaml
triggers:
  - trigger: state
    entity_id: event.example_trading_limited_filing
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.event_type == 'accounts' }}"
actions:
  - action: notify.notify
    data:
      message: >
        {{ trigger.to_state.attributes.rendered_description }}
        ({{ trigger.to_state.attributes.date }})
```

**One bus event**, `companies_house_event`, for the whole portfolio. It carries
`company_number`, `company_name` (or `officer_id`, `officer_name`), `kind`,
`event_type` and the same payload:

```yaml
triggers:
  - trigger: event
    event_type: companies_house_event
    event_data:
      kind: filing
actions:
  - action: notify.notify
    data:
      message: >
        {{ trigger.event.data.company_name }}:
        {{ trigger.event.data.rendered_description }}
```

Event types by kind:

| Kind | Types | Payload |
|---|---|---|
| filing | accounts, confirmation-statement, officers, persons-with-significant-control, address, capital, mortgage, change-of-name, resolution, incorporation, gazette, insolvency, dissolution, other | transaction_id, date, description, rendered_description, category, subcategory, type, barcode, document_id, paper_filed, pages |
| officer | appointed, resigned, details-changed | name, role, appointed_on, resigned_on, officer_id, appointment_id |
| psc | notified, ceased, statement-added, details-changed | name, psc_kind, natures_of_control, notified_on, ceased_on |
| charge | created, satisfied, part-satisfied, acquired | charge_code, persons_entitled, created_on, delivered_on, satisfied_on, status |
| status | status-changed, strike-off-proposed, strike-off-discontinued, dissolved | old_status, new_status, detail |
| profile | name-changed, address-changed, sic-changed, accounting-reference-date-changed | old_value, new_value |
| appointment (officer) | appointed, resigned, company-status-changed, disqualified | company_number, company_name, company_status, role, appointed_on, resigned_on |

`companies_house_document_downloaded` fires after `download_document` with the
path and filing metadata.

## Actions

All read actions return the API resource as response data and are also
exposed to Assist / the LLM API, so "when is the confirmation statement due for
Example Trading" works from voice once the assistant has access to the
Companies House API.

`search_companies`, `advanced_search`, `alphabetical_search`,
`dissolved_search`, `search_officers`, `search_disqualified_officers`,
`search_all`, `get_company`, `get_registered_office`, `get_officers`,
`get_officer_appointment`, `get_officer_appointments`, `get_disqualification`,
`get_filing_history` (with rendered descriptions), `get_filing`, `get_charges`,
`get_charge`, `get_psc`, `get_psc_detail`, `get_psc_statements`,
`get_insolvency`, `get_exemptions`, `get_registers`, `get_uk_establishments`,
`get_document_metadata`.

`download_document` writes to the document directory as
`{company_number} {company_name}/YYYY-MM-DD - Companies House - {description} ({company_name}).pdf`
using the filing date, sanitised and capped at 200 characters. It never
overwrites: the same content returns the existing path, different content
gets a ` (2)` suffix. Nothing downloads automatically; build an archive around
the `filing` event and this action. The directory must be a media directory or
in `allowlist_external_dirs`.

`refresh` refreshes monitored companies and officers now (target a device or
the entry, optionally choose datasets) using the on demand reserve.

```yaml
action: companies_house.get_filing_history
data:
  company_number: "12345678"
  category: accounts
  items_per_page: 5
response_variable: filings
```

## Examples

### Dashboard

```yaml
type: vertical-stack
cards:
  - type: calendar
    entities:
      - calendar.companies_house_all_deadlines
    initial_view: listWeek
  - type: entities
    title: Deadlines
    entities:
      - entity: sensor.example_trading_limited_next_deadline
        name: Example Trading
        secondary_info: last-changed
      - entity: sensor.example_trading_limited_days_to_next_deadline
      - entity: binary_sensor.example_trading_limited_proposed_strike_off
      - entity: sensor.companies_house_budget_used
```

### Notify at 30 and 7 days before any deadline

```yaml
alias: Companies House deadline reminders
triggers:
  - trigger: time
    at: "09:00:00"
actions:
  - repeat:
      for_each: "{{ integration_entities('companies_house') | select('match', 'sensor\\..*_days_to_next_deadline') | list }}"
      sequence:
        - if:
            - condition: template
              value_template: "{{ states(repeat.item) in ['30', '7'] }}"
          then:
            - action: notify.notify
              data:
                message: >
                  {{ state_attr(repeat.item, 'friendly_name') | replace(' Days to next deadline', '') }}:
                  deadline in {{ states(repeat.item) }} days.
```

### Alert immediately on a strike off proposal or insolvency filing

```yaml
alias: Companies House strike off or insolvency
triggers:
  - trigger: event
    event_type: companies_house_event
    event_data:
      event_type: strike-off-proposed
  - trigger: event
    event_type: companies_house_event
    event_data:
      kind: filing
      event_type: insolvency
actions:
  - action: notify.notify
    data:
      title: "Companies House: {{ trigger.event.data.company_name }}"
      message: "{{ trigger.event.data.event_type }} - {{ trigger.event.data.rendered_description or trigger.event.data.detail }}"
```

### Alert when a tracked officer takes a new appointment

```yaml
alias: Companies House new appointment
triggers:
  - trigger: event
    event_type: companies_house_event
    event_data:
      kind: appointment
      event_type: appointed
actions:
  - action: notify.notify
    data:
      message: >
        {{ trigger.event.data.officer_name }} was appointed
        {{ trigger.event.data.role }} of {{ trigger.event.data.company_name }}
        ({{ trigger.event.data.company_number }}).
```

### Alert on a new charge against a company

```yaml
alias: Companies House new charge
triggers:
  - trigger: event
    event_type: companies_house_event
    event_data:
      kind: charge
      event_type: created
actions:
  - action: notify.notify
    data:
      message: >
        New charge {{ trigger.event.data.charge_code }} against
        {{ trigger.event.data.company_name }} in favour of
        {{ trigger.event.data.persons_entitled | join(', ') }}.
```

### Weekly digest of everything filed

```yaml
alias: Companies House weekly digest
triggers:
  - trigger: time
    at: "08:00:00"
conditions:
  - condition: time
    weekday: [mon]
actions:
  - variables:
      lines: >
        {% set ns = namespace(lines=[]) %}
        {% for e in integration_entities('companies_house') | select('match', 'event\\..*_filing') %}
          {% set s = states[e] %}
          {% if s and s.state != 'unknown' and (now() - s.last_changed).days < 7 %}
            {% set ns.lines = ns.lines + [s.attributes.friendly_name ~ ': ' ~ s.attributes.rendered_description ~ ' (' ~ s.attributes.date ~ ')'] %}
          {% endif %}
        {% endfor %}
        {{ ns.lines | join('\n') }}
  - action: notify.notify
    data:
      title: Companies House this week
      message: "{{ lines or 'Nothing filed.' }}"
```

## Removal

Remove the integration from **Settings → Devices & services**. Its devices,
entities and stored change state are deleted with it. Then uninstall it from
HACS if you no longer want the files.

## Known limitations

* The Streaming API is not used. Freshness during business hours is 15 to 30
  minutes for companies that matter and a few hours otherwise; that is the
  design, and it is what keeps the budget at a fraction of a percent.
* Paper filings appear on the register days after submission no matter how
  often anyone asks.
* Officer changes on companies you do not monitor are only seen through the
  daily officer poll.
* Natural officers only ever expose month and year of birth. The integration
  never fabricates a day.
* `filings_last_12_months` counts the filings seen since monitoring began
  (seeded with the newest 25), so it is exact for typical companies and a
  lower bound for very active ones.
* Search endpoints cap paging depth; lists are fetched up to the configured
  maximum number of pages.

## Troubleshooting

* **"The API key was rejected"**: create a new key in the developer hub and
  reauthenticate from the repair issue. Keys for the sandbox environment do not
  work against the live API.
* **Budget used climbs or a "throttling" repair appears**: something else is
  using the same key, or the configuration is very busy. The repair raises the
  cadence multiplier by one; you can also reduce close watch companies.
* **A company shows a "dissolved" repair**: it is polled monthly now. Confirm
  the repair to stop monitoring it, or ignore the issue to keep the entities.
* **Nothing changes after a filing**: check the `Polling tier` sensor for the
  next probe time, and `Last error` on the service device. Enable debug
  logging for `custom_components.companies_house` to see every scheduling
  decision as one line: company, tier, reason, next run.
* Diagnostics for the entry and for each device are available from the device
  page; the key and dates of birth are redacted.

## Development

```bash
uv venv --python 3.14 .venv && uv pip install -r requirements_test.txt
.venv/bin/pytest            # with coverage, 95 percent required
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy
```

`scripts/build_enumerations.py` refreshes the filing description tables from
the Companies House api-enumerations repository; `scripts/build_services.py`
and `scripts/build_strings.py` regenerate `services.yaml`, `strings.json` and
`translations/en.json`; `scripts/record_fixtures.py` records real payloads as
test fixtures with `CH_API_KEY` in the environment; `scripts/hassfest.sh` runs
hassfest locally. The design is in [SPEC.md](SPEC.md).
