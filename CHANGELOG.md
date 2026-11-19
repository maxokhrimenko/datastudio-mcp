# Changelog

## 0.6.13 - 2026-11-19

- `datasource_create_bigquery` with `attach_to_report=false` creates a reusable datasource
  again. It is created in the account's own Data Studio project, as Create > Data source
  does; before, the request had no project and Google answered HTTP 400.

## 0.6.12 - 2026-11-19

- New `datasource_connection_set`: point a BigQuery datasource at another table or a
  custom query, or back, as the editor's Edit connection does. The dry run reads the new
  columns (validating access and SQL) and lists the connection from/to, added, missing and
  retyped columns; fields keep their labels, types and descriptions; calculated fields
  and parameters stay; removing a column a formula or the report uses is blocked unless
  `allow_in_use`. Journalled, read back, reversible with `operation_restore`.
- Datasource writes no longer report `verification_failed` when the server only fills in
  derived BigQuery connector keys (`processingLocation`, empty parameter lists).

## 0.6.11 - 2026-11-19

- `datasource_sharing_set` answers `SHARING_RATE_LIMITED` when Google's reCAPTCHA refuses
  an automated share: nothing changed, retrying soon makes it worse, and the message links
  the source's Share dialog for a person. A refused share is journalled as `rejected` and
  names anyone an earlier pass already added.
- `datasource_sharing_get` lists domains that reach a source through its link under
  `link_sharing_domains` instead of as members with a raw role, and marks a deleted or
  recreated account with `former_account_uid` instead of an email ending in `?uid=...`.
  Such an address can be shared again.

## 0.6.10 - 2026-11-18

- Fingerprint tokens are domain-separated from plain content hashes. A fingerprint read
  before upgrading answers `VERSION_CONFLICT` once; read the entity again.

## 0.6.9 - 2026-11-17

- Documentation: the README and the skill say plainly which connectors the MCP works
  with. It creates datasources from BigQuery only; datasources of any other connector
  (Sheets, GA, Ads, CSV, …) are created in the Datastudio UI, after which every tool works
  with them.

## 0.6.8 - 2026-11-17

- Writes return the page fingerprint as it stands after the editor reload. Creating
  components on a new page let the editor finish a page migration after the read-back, so
  the next write with the returned fingerprint failed with `VERSION_CONFLICT`.
- `components_create` joins the points of a line or area chart over dates (missing data:
  linear interpolation); with the default "line to zero" a monthly series dropped to zero
  on every day between months.

## 0.6.7 - 2026-11-17

- `page_screenshot` captures visible pages of older reports whose URL shows another page
  token than the page ID (`/page/SEHDG` for `89400174`); before, it answered
  `PAGE_NOT_SHOWN`. Hidden pages, pages in hidden sections and unknown IDs are still refused.
- Aggregations take names as well as codes everywhere (`SUM`, `AVG`, `COUNT`,
  `COUNT_DISTINCT`, `MIN`, `MAX`, `MEDIAN`, `VARIANCE`, `STDDEV`, `AUTO`, `NONE`), each
  confirmed on known data; an unknown name or a code outside 0-14 is refused before
  anything is sent. Before, a mistyped code silently counted rows instead of distinct values.
- `settings_set` / `settings_set_batch` answer `UNKNOWN_PROPERTY` with the panel's similar
  keys for a key the panel does not have (`sortConcept` on a table: `sortConcept1`, ...);
  before, the error read `PROPERTY_NOT_WRITABLE`.
- `settings_set_batch` reports every component's changes under `steps`; before, the
  result showed only the last component's.

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
