# Datastudio UI inventory → MCP coverage

Walk of the editor on 2026-11-14 (bundle `datastudio.datastudio.en.dkJhSqeSsyI.2018`), a sandbox
report. Statuses: see [README](README.md). Every property-panel setting of every
component type, the report and the page is listed in [native-catalog.json](native-catalog.json)
(89 types/presets, ~7,600 settings) and is read/written by `settings_describe` / `settings_set`.

## Session, modes, publishing (header)

| UI | Feature | MCP | Status |
|---|---|---|---|
| Open report | open by URL/UUID, account selector, login | `report_open` | ✅ |
| Edit / View toggle | switch modes, keep the draft loaded | automatic (`ReportSession.acquire`) before every tool | ✅ |
| Duplicate tabs, editor left open, overlay over the toggle | settle autosave, close extras, reload editor | automatic | ✅ |
| Viewer showing the published revision (publishing on) | load the draft instead | automatic (edit route → View toggle) | ✅ |
| Stale model after any write | reload | automatic (stale flag, EDITOR_STALE retry) | ✅ |
| Draft version ▾ › Published / Draft | publishing state, unpublished changes | `report_publishing` | ✅ |
| Publish | publish draft | `report_publish` | ✅ |
| File › Publishing settings | auto / manual publishing | `report_publishing_mode` | ✅ auto→manual live |
| File › Version history › Name current version | named revision | `revision_name_current` | ✅ |
| File › Version history › See version history | list revisions (report, datasource) | `revisions_list` | ✅ |
| Version history › Restore / Publish this version | restore as draft / republish | `revision_restore`, `report_publish(revision_number)` | ✅ restore (content-identical read-back) |
| Report title (click to rename) | rename | `report_rename` | ✅ |
| Reset (viewer) | reset viewer filters/controls | `control_reset` | ✅ (per control) |
| Share › Invite people | report ACL | — | 👤 people only (owner decision 2026-11-14) |
| Share › Schedule delivery | email schedule (`batchSchedule`) | — | 👤 people only (owner decision 2026-11-14; requests also carry a reCAPTCHA token) |
| Share › Get report link | view/edit/embed URLs | `report_links`; viewer-state links via `statefulUrlEnabled` setting | ✅ |
| Share › Embed report, File › Embed report | embedding allowed + embed code | `report_links` (iframe snippet), `report_patch` `isEmbeddable` | ✅ |
| Share › Download report, File › Download as PDF | PDF export (pages, background, link, tables, page numbers) | `report_pdf` | ✅ |
| ⋮ › Make a copy, File › Make a copy, File › New report | report asset creation | — | 👤 people only (owner decision 2026-11-14) |
| ⋮ › Refresh data, View › Refresh data | clear cache, requery | `report_refresh` reloads the editor model | ⚪ viewer action, no saved effect |
| ⋮ › Present | presentation mode | — | ⚪ |
| ⋮ › Report details | owner/created/modified | `report_content_get`, `settings_describe` (report: reportCreated/Modified/Location) | ✅ |
| ⋮ › View applied filters | effective filters | `page_filter_inheritance`, `report_filters` | ✅ |
| ⋮ › Report content | page/component list | `report_inspect`, `page_get` | ✅ |
| Pause updates | stop chart refresh while editing | — | ⚪ editor-only convenience |

## File menu

| Item | MCP | Status |
|---|---|---|
| Share… | see header | 👤 |
| Theme and layout | `settings_describe/settings_set` `{"kind":"report"}` | ✅ |
| Report settings (data source, filters, date range dimension, GA ID, Maps key, bookmarks, refresh, cross-filtering, description) | `settings_*` report target; filters `report_filters` | ✅ (description, GA, cross-filter keys in panel) |
| Version history, Publishing settings | see header | ✅/🟢 |
| New report, Make a copy | — | 👤 people only |
| Copy / paste components and pages between reports | `pages_import` (+ `report_theme_copy` for the look) | ✅ 2026-11-14: a production section (4 pages, 36 components each) copied into the sandbox 1:1: datasource attached, 21 filters, conditional formatting, logos re-uploaded, Monthly/Weekly buttons relinked, canvas 1900 kept; screenshots identical |
| Download as PDF | `report_pdf` | ✅ |
| Embed report | `report_links`, `report_patch` `isEmbeddable` | ✅ |

## Edit menu, component context menu, keyboard

| Item | MCP | Status |
|---|---|---|
| Undo / Redo | `operation_restore` (per MCP operation, conflict-safe) | ✅ |
| Cut / Copy / Paste / Duplicate | `components_duplicate` (same page), `components_copy_to_page` (other page), `components_delete` | ✅ |
| Paste special › Paste style only | read `settings_describe` of source, `settings_set` on targets | 🟢 |
| Delete | `components_delete` | ✅ |
| Select all / none | ⚪ UI selection |
| Context › Order (bring forward/back…) | `components_layers` | ✅ |
| Context › Make report-level / Make page-level | `components_scope` | ✅ both directions |
| Context › Control visibility (component, page) | `visibility_set` | ✅ |
| Context › Resize columns | `settings_set` table `columnsWidth` (include_hidden) | 🟢 |
| Context › Sort by / Reset | chart sort settings via `settings_set`; viewer `control_reset` | 🟢 |
| Context › Show underlying data, Export chart, Show data request, Diagnose Query Performance, Explore, Refresh data | viewer/diagnostic actions | ⚪ not report edits (data checks: `page_screenshot`, `report_pdf`) |
| Context › Link to this component | ⚪ |
| Context › Blend data | `blend_create` (+ `blends_*` for existing blends) | ✅ 2026-11-14: blend of two sources (inner join on one dimension) created, a table on it showed joined data, deleted with `blends_delete` |

## View menu

| Item | MCP | Status |
|---|---|---|
| Show navigation in edit mode | `settings_set` report / `report_patch` `showNavigationInEditMode` | 🟢 |
| Snap to › Grid / Smart guides, View grid, Grid size/padding/offsets | `settings_set` report `snapMode`, `gridSize`, `gridPadding`, `gridXOffset`, `gridYOffset` | ✅ (`gridSize`) |
| Zoom, Reset panel layouts | ⚪ editor viewport |
| Refresh data | ⚪ viewer action |

## Insert menu, toolbar (Add a chart / Add a control / icons)

All created by `components_create`, which calls the editor's own creation service (same
defaults, behaviours and field picks as Insert), then sets explicit fields if given. Style and
setup via `settings_set`; text/URL/shape content via `settings_set` with `display_key`.

| Item (type/preset) | Status |
|---|---|
| Time series `simple-linechart` (default, sparkline, smoothed) | ✅ |
| Bar `simple-barchart` (default, vertical, stacked, 100%…) | ✅ |
| Histogram `simple-histogram` | ✅ |
| Combo `simple-combochart` (default, line-combo-stacked, line, smoothed, boxplot, candlesticks) | ✅ |
| Pie `simple-piechart` (default, donut) | ✅ |
| Table `simple-table` (default, bar, heatmap) | ✅ |
| Bubble/Filled/Heatmap/Line/Connection/Combo map `google-map/*` | ✅ filled + bubble on a country field; others need lat/long fields |
| Geo chart `simple-geomap` | ✅ |
| Scorecard `kpi-metric` (default, compact, dimension mode) | ✅ |
| Scatter `simple-scatterchart` (default, bubble) | ✅ |
| Bullet `bulletchart` | ✅ |
| Area `simple-areachart` (stacked, 100%, horizontal) | ✅ |
| Pivot `pivot-table` (default, bar, heatmap) | ✅ |
| Treemap `simple-treemap` | ✅ |
| Gauge `gauge-viz` (default, ranges) | ✅ |
| Sankey `simple-sankey` | ✅ |
| Waterfall `simple-waterfall` | ✅ |
| Timeline `simple-timeline` | ✅ |
| Funnel `simple-funnel` (default, stepped bar, inverted triangle) | ✅ |
| Drop-down list / Fixed-size list / Input box / Advanced filter / Slider / Checkbox `dimension-filter/*` | ✅ (checkbox needs a boolean field) |
| Preset filter `button/button-filter` | 🟢 needs a filter field |
| Date range control `simple-daterangepicker` | ✅ |
| Data control `dataset-picker` | ✅ |
| Dimension control `dimension-control` | ✅ |
| Button `button` (link, page navigation, reset, filter) | ✅ created; actions via `settings_set` |
| URL Embed `framed-external-content` | ✅ (`contentUrl` display key) |
| Text `simple-description` | ✅ (HTML `text` display key + panel) |
| Image `image-component` | ✅ `image_upload_set` uploads a local file through the panel's own upload |
| Line / Arrow `arrow` (linear, elbow, curved elbow) | ✅ (`terminals` display key, line style panel) |
| Shapes `generic-shape/*` (rectangle, ellipse, triangle, arrows, heart, diamond, star, polygons, cylinder, callout) | ✅ rectangle/ellipse; others same type |
| Community visualizations `third-party-viz` | ✅ `community_viz_register` + `components_create type=<viz_id>`: the type is registered natively (registration loader + registry) before the Insert service creates it with its default fields; verified 2026-11-14 with the Google gauge (rendered 3,767,578 Users), then removed |
| New page | `pages_create` | ✅ |

Runtime (viewer) use of controls: `control_filter_set`, `control_checkbox_set`,
`control_date_range_set`, `control_parameter_set`, `control_reset`, `component_interaction_state`.

## Page menu, left page list, navigation

| Item | MCP | Status |
|---|---|---|
| New page (name, icon, hidden) | `pages_create` | ✅ |
| Duplicate page | `page_duplicate` | ✅ (earlier verification) |
| Delete page | `pages_delete` | ✅ |
| Current page settings (background, canvas size, page data/filters/date range) | `settings_set` `{"kind":"page"}`, `page_filters` | ✅ |
| Manage pages (rename, reorder, hide, icons, sections, headers, dividers) | `navigation_patch` | ✅ sections/headers/dividers/icons |
| Navigation style (left, top, tabs, hidden) | `report_patch` `/attributeConfig/reportAttribute/viewModeNav` (1–4) | ✅ (panel input itself is not natively writable) |
| Next/previous/first/last page | ⚪ |

## Arrange menu

| Item | MCP | Status |
|---|---|---|
| Order | `components_layers` | ✅ |
| Align horizontally / vertically, Distribute | `components_arrange` | ✅ |
| Make report-level / page-level | `components_scope` | ✅ |
| Group / Ungroup | `groups_create`, `groups_ungroup`, `groups_move`, `groups_filters` | ✅ |
| Position/size/rotation (properties panel) | `components_geometry` | ✅ |

## Resource menu

| Item | MCP | Status |
|---|---|---|
| Manage added data sources (list, edit, remove, add) | `datasources_list`, `datasource_*`, `datasources_remove_from_report`, `datasource_add_to_report` | ✅ |
| Manage blends | `blend_create`, `blends_delete`, `blends_list/get/validate/rename/joins/input_filters/inputs_rename` | ✅ create (2-5 tables, all join types; output fields from the editor's own blend schema builder, identical to a UI save) and delete verified live; changing a saved blend's field list = `blends_delete` + `blend_create` |
| Manage filters | `filters_*`, `filter_conditions_list`, `filter_validate` | ✅ |
| Manage dimension value colors | `dimension_value_colors_set` | 🟢 |
| Manage variables (report variables, query variables) | `report_variables_list`, `report_variables_write`; datasource params `datasource_parameters_*` | ✅ |
| Manage community visualizations | `community_viz_list`, `community_viz_register` (register=false removes; refused while used) | ✅ |
| Manage field names and IDs | `field_overrides_list`, `field_overrides_write` | ✅ wire captured from the dialog; set display name + shared ID on two sources and removed, verified 2026-11-14 |

## Theme and layout panel

| Item | MCP | Status |
|---|---|---|
| Theme gallery (Default, Edge, Constellation, … Simple Dark) and custom theme | `settings_set` report key `theme` (options list the gallery) | 🟢 |
| Customize (primary/accent/textbox/data/shape/navigation styles, palette, grid, positive/negative colors, chart header, component background/border/radius/shadow) | `settings_set` theme keys (built-in theme copied to a custom one first, as Customize does) | ✅ |
| Chart titles defaults, axis color | — | ⚪ disabled by a feature flag in this build (panel hides them; engine refuses `PROPERTY_DISABLED`) |
| Modern charts toggle | `settings_set` `theme` value `isModernTheme` | 🟢 |
| Extract theme from image | `theme_from_image` (options, then choice) | ✅ `intelligence/color/getColorScheme`; Apply reproduced: MCP apply of the scheme the UI had applied returned `unchanged`; a second scheme applied and the original theme restored |
| Manage dimension value colors | `dimension_value_colors_set` | 🟢 |
| Layout: freeform/responsive | `settings_set` (report layout settings) | ⚪ not a UI capability: Datastudio refuses it — "You cannot directly change from freeform layout to responsive layout… create a new report in responsive layout" (seen 2026-11-14). Responsive layout exists only in reports created that way (report creation is out of scope); component creation on responsive pages is refused with a clear error |
| Layout: header visibility, navigation type, respect theme, display mode, margin, canvas size, snap, grid, report-level component position | `settings_set` report | ✅ canvas/scale/margin; others 🟢 |

## Data panel, properties panel, filter bar

| Item | MCP | Status |
|---|---|---|
| Data panel field list per source | `datasource_get` | ✅ |
| Add a field (calculated) | `datasource_calculated_fields_create/edit/delete`, chart-local formulas via `components_fields` | ✅ |
| Add a parameter | `datasource_parameters_upsert` / `_delete` | ✅ 2026-11-14: wire captured from the UI dialog; number parameter created on a sandbox source, read back, deleted |
| Datasource › Share (people and roles) | `datasource_sharing_get`, `datasource_sharing_set` (add viewer/editor; change/remove 👤) | ✅ `getSharing` read; adds through the Share dialog (its `updateSharing` carries a reCAPTCHA token the MCP never builds); 17 production sources shared and read back 2026-11-17; a long automated series got `RECAPTCHA_VERIFICATION_FAILED`, the retry after a pause passed; 2026-11-19 a single share was refused three times in a row, now `SHARING_RATE_LIMITED` |
| Datasource name (editor title) | `datasource_rename` | ✅ `renameDatasource` sets title and draft/published block name (publishing a block name is ignored); verified 2026-11-14 |
| Add Data | `datasource_create_bigquery`, `datasource_add_to_report` | ✅ |
| Properties › Setup (fields, date range dimension, default date range, filters, interactions, rows, sort, drill down, optional metrics, breakdown) | `settings_set`, `components_fields`, `components_filters`, `components_switch_type` | ✅ sort/fields/filters; 🟢 others |
| Properties › Style (every type, every section) | `settings_set` | ✅ |
| Filter bar / Add filter (viewer filter bar) | — (runtime: `control_*_set` for saved controls) | ⚪ viewer/session state: adding a filter-bar filter sends no write and is gone after reload (verified 2026-11-14); the editor strips ad-hoc entries from `viewerFilterBarConfig` (short-links flag). The "Filter bar" side button only shows/hides the bar |
| Chart header (filter/sort/more icons visibility) | `settings_set` `componentHeaderPosition` | ✅ |
| Conditional formatting | `settings_set` / `settings_set_batch` `conditionalFormat` (native rule list) | ✅ 2026-11-14: rules of 9 QC tables rebuilt from a snapshot; written to the page's chartCondition |

## Add data → BigQuery (and other connectors)

| Item | MCP | Status |
|---|---|---|
| Recent / My projects / Shared projects | `bigquery_browse` (recent) | ✅ |
| Datasets, tables | `bigquery_browse(project_id[, dataset_id])` | ✅ |
| Table → Add (embedded in report) | `datasource_create_bigquery` table | ✅ |
| Custom query (standard/legacy, billing project) | `datasource_create_bigquery` sql | ✅ |
| Custom query › Enable date range parameters / viewer email address parameter | `datasource_create_bigquery` `source.date_range_params` / `viewer_email_param` | ✅ written as the checkboxes write `connectorConfig.parameterConfiguration` (DS_START_DATE/DS_END_DATE dataType 5, DS_USER_EMAIL dataType 1, roles 1+2); verified 2026-11-14: identical to UI wire, saved, SQL with `@DS_*` accepted by getColumns |
| My data sources (reusable) → Add | `datasource_add_to_report` | ✅ |
| Data credentials (owner/viewer) | `datasource_credentials_set` | ✅ (earlier) |
| Refresh fields / reconnect | `datasource_refresh_fields` | ✅ (earlier) |
| Edit connection › another table / custom query (and back) | `datasource_connection_set` | ✅ 2026-11-19 in the sandbox: table → custom query (+1 column), → narrower query (10 columns removed), → table again, all `verified` in draft and published; field labels and calculated fields kept, removing a column a formula uses blocked; a chart showed the query's data. Written as Refresh fields with the new `connectorConfig` (the server derives `processingLocation`); the editor's Reconnect button does not render in headless Chrome, so the UI wire was not captured |
| Field type, semantic, aggregation, label, description, enable | `datasource_fields_edit_metadata`, `datasource_fields_rename` | ✅ |
| Data freshness (cache policy), community viz access, field editing in reports, native functions | `datasource_settings_set` | ✅ |
| Other connectors (Sheets, GA, Ads, CSV, …) | — | 👤 people only (owner decision 2026-11-14); existing datasources of any connector are edited by every `datasource_*` tool |

## Help menu

Help, Community forum, Release notes, Request a feature, Learn, Legal — ⚪.
