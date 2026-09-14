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
`Name - born MM/YYYY - Town - N appointments`. Names collide constantly; the
month and year of birth is what tells people apart, and the list can be typed
into to narrow it. The officer id is resolved from the result, and the date of
birth is kept for disqualification matching. Tick **Watch their companies** to
add every company they currently hold a role at as a watched company, and any
they join later, automatically. The same setting is a switch on the person's
device.

**Pasting a link.** Both pickers accept the address of a page on the Companies
House website (`/company/12345678` or `/officers/.../appointments`), or a bare
company number or officer id, and go straight to it. Handy for a common name
that never surfaces in a search.

**One person, several records.** The register opens a new record whenever
someone is appointed with slightly different details, so one person can be
three "officers". Reconfigure the person and choose **Add another register
record**: its appointments are pooled with the ones already followed, on the
one device. Once a week the register is searched for further records of each
followed person; a record whose surname, forename and month and year of birth
all match is followed automatically (and raises a `new-record` event), while a
name-only match is only listed on the *Register records* sensor.

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

Adding or removing a company or person sets up or tears down just that one;
nothing else is touched. Everything fetched is kept in a per entry store
together with the change detection state, so a restart costs no requests while
the snapshots are fresh, and never replays old filings as new events. On the very first look at a company the store is
seeded silently and no events fire.

## Entities

Every entity is enabled. A disabled entity would cost nothing, but neither
does an enabled one that rarely changes: the recorder only writes when a value
changes, and most of these change a few times a year. The device page is long;
the diagnostic section keeps the rarely-needed rows out of the way.

### Company

Sensors on by default: next deadline, next deadline type, days to next
deadline, accounts next due, confirmation statement next due, company status,
earliest strike-off date, days to object to strike-off (both unknown unless a
strike-off is live; see below), last filing date, last filing (rendered
description), officers active, PSC active, charges outstanding. Diagnostic: company name, status detail, type,
subtype, jurisdiction, date of creation, date of cessation, registered office
address (structured in attributes), polling tier.

Also: company age, accounts and confirmation statement made-up-to and
period dates, accounting reference date, last accounts type, days to each
deadline (negative once overdue), SIC codes (count, descriptions in
attributes), primary SIC description, officer counts by role, PSC totals and
statements, charge counts by status, filings total and last 12 months, days
since last filing, last filing category, previous names, UK establishments,
insolvency cases, registers held.

**Charges** — *outstanding charges* and *charges total* carry the charges
themselves in a `charges` attribute, newest first: who the charge is in favour
of (`lender`), its status, created and satisfied dates, the kind of charge in
words (`kind`: "fixed and floating charge over all the company's assets", or
the register's classification — "debenture", "legal charge" — for charges
registered before 2013, which carry no flags), what it secures (`secured`; the
register's standard all-monies wording is shortened to "All monies due"), what
it is over (`particulars`) and a `link` to the filing's PDF — the MR01 that
created it, or the MR04 that satisfied it. The outstanding list also names the
`lenders` still owed; the total list includes satisfied charges. Both are
capped at 25 and kept out of the recorder.

Binary sensors (problem class, on by default): accounts overdue, confirmation
statement overdue, accounts due soon, confirmation statement due soon,
**proposed strike off** (from `company_status_detail` and from GAZ1 filings;
off again as soon as a later filing discontinues the strike-off, with
`discontinued_on` saying so while the register's status detail lags),
**insolvent** (liquidation, receivership, administration, voluntary
arrangement, insolvency proceedings), registered office in dispute,
undeliverable registered office. Plus *is active*, can file, has outstanding
charges, has insolvency history, has officers with protected details, has
exemptions.

A **calendar** per company with accounts due, confirmation statement due,
accounts period end and the accounting reference date as all day events, plus
*Objection deadline* and *Earliest strike-off* while a strike-off is live.

### Strike-off countdown

When a company is under a first Gazette notice, two sensors count it down
using the GOV.UK rules: the registrar strikes a company off not less than
**two months** after the notice, a postal objection must arrive at least
**two weeks** before that, and an online objection is taken any time before
the company goes. So **Earliest strike-off date** is the notice date plus two
months (same day, clamped to the month end) and **Days to object to
strike-off** counts down to two weeks before it, going negative once passed.
Both carry `kind` (voluntary or compulsory, from the notice's filing
description), `notice_on`, `earliest_on`, `objection_deadline`,
`suspended_on`, `transaction_id`, a `link` to the Gazette notice PDF and a
`caveat`; the *Proposed strike off* binary sensor carries the same.

Two honest limits. The 28-day rule for false registrations cannot be told
from the filings, so two months is always assumed and the caveat says so. And
the register's status detail is not trusted on its own: a company can read
"active proposal to strike off" a year after its last notice was suspended.
The countdown keys off the newest first-notice filing kept for the company.
A later **discontinuation** filing (`gazette-filings-brought-up-to-date`,
withdrawal) ends it outright: the binary sensor goes off, the countdown
sensors go unknown and the report shows "strike-off dropped" rather than a
live problem, however long the status detail takes to catch up. A
**suspension** filing (a successful objection) keeps the notice and moves the
earliest date to six months after it, firing a `strike-off-suspended` status
event; once that hold has run out the line reads "hold ended 7 Feb 2025,
could be struck off any day now". A company already under a notice when it
is added has its notice recovered from the filings kept (so is one recorded
by an earlier version without its filing); when no notice is there the
sensors stay unknown and say "notice date unknown" rather than guess.

One notice is announced once, whichever side sees it first. The probe
announces a Gazette notice the moment it is filed, with the countdown. When
the status detail flips first, the filings kept are checked, then the filing
history is read once more on demand (the notice is normally filed the same
day) so the probe can announce it with the countdown; only when no notice can
be found does the profile announce "Companies House has proposed to strike
the company off; the Gazette notice has not appeared in the filings yet",
and the notice then fills the clock in quietly. The same goes for the end of
a strike-off: the discontinuation filing or the status detail clearing,
whichever comes first, and a company struck off is announced as dissolved,
not as a strike-off dropped.

### Officer

Current companies (the companies they hold a role at today, newest first,
with a `watched` flag per company in the attributes), appointments active,
appointments total (with the full list in attributes, capped at fifty), most
recent company, last appointment date, disqualified (problem), currently holds
appointments, appointments resigned, first appointment date; the *Watch their
companies* switch; diagnostic: nationality, country of residence, occupation,
date of birth (month and year only), officer role, register records (how many
records are followed as this person, with any lookalikes found).

**Disqualification matching is honest.** The register search matches on name,
and names collide constantly. The *Disqualified* sensor only turns on for an
exact match of surname, forename **and** month and year of birth. Anything
weaker is counted in the `possible_matches` attribute and does not flip the
sensor. A false "your director is disqualified" is worse than no alert at all.

### Service device

One "Companies House" device per API key with the aggregate **All deadlines**
calendar (every monitored company, titled `Company - deadline type`; this is
the one for the dashboard), the **Connections** sensor (see the connections
map below) and diagnostics: requests used this window, requests remaining,
budget used, window resets at, last successful update, last error, companies
monitored, officers monitored, next scheduled probe.

### Attribute size

Attributes are kept small (lists capped at ten, appointments at fifty, charges
at twenty-five and never recorded). If the recorder is large, exclude the
heaviest entities:

```yaml
recorder:
  exclude:
    entity_globs:
      - sensor.*_appointments_active
      - sensor.*_officers_active
      - sensor.*_psc_active
      - sensor.*_charges_outstanding
```

## Reports and alerts

Every company and person has three settings, each a switch on its device and
a field in its settings dialog:

- **Notify instantly** — the moment anything changes, a
  `companies_house_alert` event fires with a plain-English `title`, `message`
  and `link` (plus everything the ordinary event carries), so one automation
  can push it to a phone or an email. Off by default.
- **In weekly report** — include it in the report built by the `digest`
  action. On by default.
- **Watch their companies** (people only) — see above.

Companies also take a **website**, used for a logo (the site's icon, via
Google's favicon service) and a link in reports and alerts.

The **`companies_house.digest`** action gathers everything that changed at the
opted-in companies and people over the last `days` (default 7) — every change
is remembered in the store, newest first, and the register's own dates fill in
the rest: filings dated in the period and roles that started or ended in it
are included even if they were seen before the log began. Each change gets a
**score** (what
happened × how much the company matters: close watch 3, notify instantly 2,
else 1), so the report opens with a *Worth a look* list of the week's most
important changes. A charge change reads as who lent and what secures it
("A charge in favour of Lloyds Bank plc was registered on 3 Jul 2026 — fixed
and floating charge over all the company's assets; secures all monies due")
and opens the filing's PDF; a company's card says how many charges are
outstanding and to whom. Problems are split into **new this week** (a
strike-off or status change seen in the period, or a deadline that passed in
it) and **still open** (known before, listed quietly at the bottom until they
clear); a live strike-off reads "Strike-off proposed (compulsory) — 41 days to
object" with a link to the Gazette notice. It also lists deadlines in the
next 30 days (including the last day to object to a strike-off), **new
companies** (recently
incorporated, with the followed people at them) and, when ownership changed,
**who controls what** across every watched company. It returns the report as
data, as email-safe HTML (inline styles, logos, links to the register and to
filed PDFs) and as plain text. Pass `summary` to put a paragraph at the top (an
`ai_task.generate_data` call over the data works well), `save: true` to also
write it under `www/companies_house/` so it has a link (`report-DATE.html`,
plus `report-latest.html` at a fixed address), and `attach: true` to
download the period's accounts and every filing at close-watch companies
(capped) and return them as `attachments` (and `email_attachments`, already in
the shape `smtp.send_message` takes). Only `attach` costs API requests.

Downloads return a `media_content_id` when the document lands under a media
folder, so an automation can hand the PDF to `ai_task.generate_data` for a
plain-English reading and attach it to `notify.send_message`.

The report also has a **Connections** section (up to eight lines from the
connections map below, most serious first, each linking to the register) so
the weekly email says when a director of yours also runs a company in
liquidation, or when two people joined the same boards on the same day.

## Connections map

**Who sits with whom, who owns what.** The integration already holds every
watched company's officers and people with significant control, every
followed person's appointments, and every charge, so it draws the map from
memory: building it costs no API requests.

Nodes are companies (yours in dark blue, watched in blue, not watched in
grey), people (followed in teal, other officers in grey) and outside entities
(lenders, overseas parents, drawn as diamonds). Edges are roles (solid;
resigned ones dashed and hidden by default; roles at dissolved companies
count as resigned, as they do on the register), control (a thick arrow
pointing at the company controlled, labelled with the share band or
"appoints directors", the full wording on hover) and charges (a dotted arrow
from the lender, labelled "charge"). Companies in liquidation or facing
strike-off get a red ring, dissolved ones fade, a disqualified director or a
sanctioned owner gets a red badge, and anything that changed in the last week
says **new**.

Identity is handled honestly. A followed person is one node however many
register records they have. A person with significant control has no record
id, so they are joined to a director by surname, first forename and month and
year of birth (the same test the disqualification check uses); by name alone
only when there is no date of birth to check. Two register records are never
joined by name alone, so a secretary (the register gives secretaries no date
of birth) is not mistaken for a director who shares their name. A corporate
owner or corporate secretary registered in the UK becomes its company;
anything registered abroad (an Irish or Isle of Man parent has a "Companies
Act" of its own) stays an outside entity.

The map is also read for the **connections worth knowing about**, each with a
severity and links:

- *high* — a person with a role at a watched company also runs a company in
  liquidation or facing strike-off; a followed person is disqualified yet
  still holds roles; a person with significant control is on the sanctions
  list.
- *medium* — two watched companies control each other through their people;
  ownership chains and who ultimately owns what (`Alex Morgan → Holdco
  Capital → Portfolio One`); one owner controlling several watched
  companies; a corporate parent that is not watched; two or more people who
  sit on the same boards, or joined the same companies on the same day; a
  person with roles at five or more watched companies.
- *low* — a new role that links two previously separate groups, or a
  resignation that cuts one; a company nobody watches but two followed people
  run; an owner with no role at the company (or a sole director who is not an
  owner); a charge in favour of another watched company or a followed
  person; watched companies sharing a registered office.

**`companies_house.connections`** returns the whole thing (`nodes`, `edges`,
`interesting`, `summary`) and takes `days` (what counts as new), `include_resigned`
and `include_external` (what is drawn; the rules always see everything) and
`save`. With `save: true` it writes `www/companies_house/connections.html` (a
fixed page: inline styles and a small force layout on SVG, no libraries, no
outside requests; search box, legend, hover to highlight neighbours, click to
open the register, toggles for resigned roles, unwatched companies and other
officers) and `connections.json`, which the page fetches fresh every time it
opens. Called from an assistant, the action returns only the counts and the
lines; with `save: true` the page always gets the whole map (the toggles trim
it on screen), whatever the call asked to be returned. The map is kept current
on its own too: a change to any role, holding, charge, status or name, or a
company or person being added or removed, rebuilds it a minute later and
rewrites the files only when it actually changed; it is also rebuilt once a
day so last week's **new** stops saying so. The first write happens shortly
after start-up.

The service device gets a **Connections** sensor: its state is the number of
live connections between the companies and people you watch, and its
attributes carry the counts, when it was built and written, the page's
address and the lines worth knowing (up to twenty; kept out of the recorder).

If the `www` folder did not exist when Home Assistant started, `/local` is
not served until the next restart.

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
| charge | created, satisfied, part-satisfied, acquired | charge_code, charge_id, lender, persons_entitled, created_on, delivered_on, satisfied_on, acquired_on, status, charge_kind, particulars, secured, security (the kind, what it is over and what it secures, in one clause), negative_pledge, created_transaction_id and satisfied_transaction_id (the filing-history ids of the MR01 / MR04: pass one to `get_filing` for its `links.document_metadata` document id, which `download_document` takes; the alert event's `link` is the register's PDF), transaction_ids |
| status | status-changed, strike-off-proposed, strike-off-suspended, strike-off-discontinued, dissolved | old_status, new_status, detail; strike-off events seen from a filing add strike_off_kind, notice_on, suspended_on, earliest_on, objection_deadline, transaction_id (a strike-off-proposed from the status detail alone has none of these) |
| profile | name-changed, address-changed, sic-changed, accounting-reference-date-changed | old_value, new_value |
| appointment (officer) | appointed, resigned, company-status-changed, disqualified, new-record, company-now-watched | company_number, company_name, company_status, role, appointed_on, resigned_on; `new-record` carries officer_id and name |

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

`connections` builds the connections map from what is already known (see
above); `digest` builds the report. Neither costs a request unless asked to
fetch documents.

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

The connections map in a card (the page is fixed and fetches its data fresh,
so a plain address is fine; run `companies_house.connections` with
`save: true` once, or wait for the first automatic write after start-up):

```yaml
type: iframe
url: /local/companies_house/connections.html
aspect_ratio: 75%
title: Connections
```

Or a markdown card that lists the lines worth knowing and links to the map:

```yaml
type: markdown
content: >-
  {% for line in state_attr('sensor.companies_house_connections', 'interesting') or [] %}
  - {{ line }}
  {% endfor %}

  [Open the map](/local/companies_house/connections.html)
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
        {{ trigger.event.data.lender or "a lender" }}
        {%- if trigger.event.data.security %} — {{ trigger.event.data.security }}{% endif %}.
```

`security` is the kind of charge, what it is over and what it secures in one
clause, empty when the register recorded none of it (hence the guard);
`created_transaction_id` is the filing-history id of the MR01. To save the
PDF, look the filing up with `get_filing` and pass its `links.document_metadata`
id to `download_document`; or trigger on `companies_house_alert` instead, whose
`message` is the sentence above and whose `link` opens the PDF on the register.

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
* The connections map only knows the other companies of people you follow
  (their appointment lists are fetched) and of watched companies. An officer
  nobody follows appears with their watched roles only, so "also runs a
  company in liquidation" can only be spotted for followed people and for
  companies that are themselves watched.

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
