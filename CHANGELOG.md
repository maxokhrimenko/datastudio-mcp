# Changelog

## 0.6.6 - 2026-11-17

- `datasource_sharing_get` / `_set` on an embedded datasource answer `EMBEDDED_DATASOURCE`
  (it shares with its report) instead of a misleading `AUTH_REQUIRED`.
- `components_create` sorts a chart whose first dimension is a date (day, month, week,
  timestamp) by that date, oldest first, as the editor does; before, months came out in
  order of the metric.

## 0.6.5 - 2026-11-17

First public release of MCP for Google Datastudio by Max Omen.

- 109 tools covering the Datastudio editor: reports, pages and navigation, components of
  every type and preset, every property-panel setting, filters and controls, blends,
  datasource fields, parameters, credentials and sharing, BigQuery datasources, themes,
  publishing and revision history, audits and screenshots.
- Every write: dry run by default, fingerprint conflict check, durable journal with the
  before-state, read-back; `operation_restore` undoes most writes.
- A dedicated headless Chrome profile keeps the Google sign-in; Chrome closes after 10
  idle minutes (`DATASTUDIO_IDLE_CLOSE_S`) and relaunches on the next call.
- `datasource_credentials_set` takes its default account from
  `DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE` (e.g. `datastudio-viewer@{project}.iam.gserviceaccount.com`);
  without it, pass `service_account`.
- Licensed under the PolyForm Noncommercial License 1.0.0.
- Contributions welcome through pull requests under the contributor terms in
  `CONTRIBUTING.md`.
