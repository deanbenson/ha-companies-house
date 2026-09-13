# Companies House for Home Assistant

Puts the UK Companies House public register into Home Assistant as devices and
entities: one device per monitored company, one per monitored officer,
configured entirely through the UI.

Work in progress. See [SPEC.md](SPEC.md) for the full design.

> **Note on the domain.** An older HACS integration,
> `YukariChiba/hass_companieshouse`, uses the same `companies_house` domain
> with a handful of YAML sensors. This integration is not compatible with it;
> remove that one before installing this.
