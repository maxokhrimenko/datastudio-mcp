---
name: datastudio-mcp
description: "How to install and drive the datastudio MCP (mcp__datastudio__* tools) to build, style, verify and maintain Datastudio reports end to end: report_open, read-before-write with fingerprints, dry_run, verified writes, publishing, BigQuery datasources, blends, themes and every editor panel setting. Use before any Datastudio report/datasource task, when the user invokes /datastudio-mcp, or when a datastudio tool returns an error you do not understand."
---

# Datastudio MCP

Skill version **0.6.8**, for datastudio-mcp **0.6.8**. `report_open` returns
`mcp_version`; if it differs, this skill is stale: reinstall it (below) or read the
repo's `skills/datastudio-mcp/SKILL.md` and `CHANGELOG.md`.

The MCP drives the Datastudio editor in its own Chrome profile through the editor's
own services: anything a person does in the editor UI, with conflict checks, a durable
journal and read-back for every write. Google's internal API has no compatibility
guarantee; the MCP resolves editor internals at call time and says precisely when
Google changed something (`bundle_contracts`).

Done by people only, never by the MCP: report sharing, changing or removing existing
datasource access, scheduled email delivery, report creation/copy/deletion, datasources
from connectors other than BigQuery, datasource deletion. Say so instead of trying.

## Install

```sh
git clone https://github.com/maxokhrimenko/datastudio-mcp.git && cd datastudio-mcp
uv sync --frozen
claude mcp add datastudio --scope user -- "$PWD/.venv/bin/datastudio-mcp"   # Claude Code
codex mcp add datastudio -- "$PWD/.venv/bin/datastudio-mcp"                 # Codex
scripts/install-skill.sh        # this skill for Claude (~/.claude/skills) and Codex (~/.agents/skills)
```

Restart the client session (`/mcp` in Claude Code) after installing or updating; a
running MCP keeps its old code. Update: `git pull && uv sync --frozen`, then restart.
The skill is a symlink into the repo, so it updates with the pull.

## Session and sign-in

- Always start with `report_open(<report URL or UUID>)`. It starts the MCP's Chrome
  in the background (headless, no window), opens the report and makes it the default
  for every other tool. Pass `report_id` to target another open report.
- `login_required` means Google wants a sign-in: a Chrome window is open on it. Ask
  the user to sign in there, then call `report_open` again; the window closes and work
  continues in the background. Sign-in is per profile and survives restarts.
- The MCP owns tabs and modes: never ask the user to switch View/Edit or close tabs.
- `access: "view"` = the account cannot edit; reads work, writes are refused.
- Multiple Google accounts: pass the report URL that carries `/u/N/` or `?authuser=N`.

## How every write works (do not skip)

1. **Read first** and take its fingerprint: `page_get` (page tools),
   `report_inspect` (structure: pages, navigation), `report_content_get` (report
   resources), `blends_list`, `datasource_get` or `datasource_parameters_list`
   (datasources), `*_list` tools for their resource.
2. **Preview**: writes default to `dry_run=true` and return `status: "planned"` with
   the exact change. Show the user anything non-trivial.
3. **Apply** with `dry_run=false` and the same fingerprint. `VERSION_CONFLICT` means
   something changed since your read: read again, re-plan; never force.
4. Only `verified` (or `unchanged`) is success. Anything else (`partial`,
   `verification_failed`, `outcome_unknown=true`) needs inspection before the next
   write: `operation_get(operation_id)`, then read the entity.
5. Undo: `operation_restore(operation_id)` (refused if later edits conflict). Some
   actions are undone by another tool; the error names it.
6. Writes go to the **draft**. With manual publishing, viewers see changes only after
   `report_publish`. Check with `report_publishing`. Never publish unless asked.

No tool retries a mutation by itself. Batch tools are not cross-entity transactions:
read `partial` results.

## Tool map

Report and pages
- `report_open`, `reports_list`, `report_refresh`, `report_inspect`, `report_audit`
  (everything that silently breaks a report or differs for viewers).
- `pages_create`, `pages_delete`, `page_duplicate`, `navigation_patch` (page names,
  icons, order, hidden, sections/headers/dividers), `report_rename`.
- `pages_import` copies pages from another report (open both with `report_open`, the
  target last): attaches their reusable datasources, copies their saved filters,
  conditional formatting and images, relinks page buttons, keeps the source canvas
  size; `section_name` groups them. `report_theme_copy` then gives this report the
  source's theme so the copy looks 1:1 (report-wide). The source is only read.
  `replace={source_page: page_here}` puts the page on an existing page instead (keeps
  its ID, name and navigation place): how a redesign made in a sandbox goes back.
- `page_get`, `page_screenshot` (look at your result; always do this after visual work).

Components (charts, text, images, shapes, controls)
- `native_catalog` lists every type/preset with its full property panel.
- `components_create` uses the editor's Insert service (native defaults), then binds
  fields: names from `datasource_get`, every metric needs an explicit aggregation by name
  (`SUM`, `COUNT_DISTINCT`, `AVG`, `MIN`, `MAX`, `AUTO`, ...).
- `components_fields`, `components_switch_type`, `components_geometry`,
  `components_arrange`, `components_layers`, `page_insert_space` (room for a new section), `components_duplicate`,
  `components_copy_to_page`, `components_scope` (report-level vs page-level),
  `components_delete`, `groups_create`, `groups_move`, `groups_ungroup`,
  `image_upload_set`, `components_patch` (wire-level escape hatch).

Every panel setting (style, theme, layout, page size, fonts, colors, radius, shadow...)
- `settings_describe(target, search)` then `settings_set(target, changes)`. Targets:
  `{"kind":"report"}` (theme, layout, canvas, grid, navigation, report settings),
  `{"kind":"page","page_id":...}`, `{"kind":"component","page_id":...,"component_id":...}`.
  Use the keys and allowed values describe returns; `PROPERTY_DISABLED` = the UI hides it too.
  Field pickers take a field name (`"sortConcept": ["_month_start_"]`); a text box's
  content is `text` (HTML).
  Read many: `settings_describe_page(page_id, search=...)` (all components of a page in
  one call, seconds instead of minutes). Write many:
  `settings_set_batch(page_id, [{component_id, changes}])`
  is one write instead of one per component (minutes faster); a refused change names
  its step and key, drop it and resend. Fields go first (`actionConceptList`,
  `metrics` take concept definitions as describe shows them), then sorts
  (`sortConcept2` opens once `sortConcept1` is set), conditional formatting
  (`conditionalFormat`), style. A text box's HTML and a button's text/link are display
  keys (`display_key=true`).
- `theme_from_image` (Extract theme from image), `dimension_value_colors_set`.

Data
- `datasources_list`, `datasource_get`, `bigquery_browse` → `datasource_create_bigquery`
  (table or custom query; `date_range_params`, `viewer_email_param`),
  `datasource_add_to_report`, `datasources_remove_from_report`, `datasource_rename`,
  `datasource_settings_set`, `datasource_credentials_set`, `datasource_refresh_fields`.
- Fields: `datasource_fields_rename`, `datasource_fields_edit_metadata`,
  `datasource_calculated_fields_create`, `datasource_calculated_fields_edit`,
  `datasource_calculated_fields_delete`, `formula_validate`,
  `formula_references`, `datasource_dependencies`,
  `datasource_parameters_list`, `datasource_parameters_upsert`,
  `datasource_parameters_delete`.
- Access: `datasource_sharing_get` (members and roles), `datasource_sharing_set` (add
  viewers/editors through the Share dialog; exact emails only, notify off by default;
  Google's reCAPTCHA may refuse a long automated series: a person shares the rest).
  One business rule used by many metrics (e.g. which exits count as attrition) belongs in
  ONE calculated field; the other formulas reference it as `t0.calc_*`.
- Blends: `blend_create` (2-5 tables, join keys must be dimensions; the result
  `blend_id` is the datasource for `components_create`), `blends_list`, `blend_get`,
  `blend_validate`, `blends_joins`, `blends_input_filters`, `blends_inputs_rename`,
  `blends_rename`, `blends_delete`.

Filters, controls, variables
- `filters_list`, `filters_create`, `filters_update_conditions`, `filters_rename`,
  `filters_delete`, `filter_conditions_list`, `filter_validate`; bindings:
  `report_filters`, `page_filters`, `groups_filters`, `components_filters`,
  `page_filter_inheritance`.
- Runtime (viewer state, not saved): `control_filter_set`, `control_date_range_set`,
  `control_parameter_set`, `control_checkbox_set`, `control_reset`,
  `component_interaction_state`.
- `report_variables_list`, `report_variables_write`, `visibility_set`,
  `field_overrides_list`, `field_overrides_write`, `community_viz_list`,
  `community_viz_register`, `report_content_get` + `report_patch` (escape hatch).

Publishing and history
- `report_publishing`, `report_publish`, `report_publishing_mode`, `revisions_list`,
  `revision_name_current`, `revision_restore`, `report_links`, `report_pdf`.

Recovery
- `operation_get`, `operation_restore`, `bundle_contracts`.

## Recipes

Build a report end to end
1. `report_open` → `datasources_list`; data missing: `bigquery_browse` →
   `datasource_create_bigquery` (dry run first: it validates SQL and returns field names).
2. Calculated fields on the datasource (`formula_validate` first).
3. Theme: `settings_describe({"kind":"report"}, "theme")` → `settings_set` (font,
   palette, radius, canvas size) or `theme_from_image`.
4. Pages: `pages_create`, `navigation_patch` for names/icons/sections.
5. Components: `components_create` in batches per page, then `settings_set` on
   components for style. Lay out with `components_geometry` or `components_arrange`.
6. `page_screenshot` every page; fix what looks wrong. `report_audit`.
7. `report_publish` only when the user asks.

Rebuild a page like another one: `settings_describe(include_hidden=true)` each source
component (read-only) → `components_create` (type, datasource, geometry) →
`settings_set_batch` with the source values that differ → `components_filters` /
page `filters` with your own filter IDs → `groups_create` → `image_upload_set`; for
pages with the same layout use `page_duplicate` and fix the differences.

Style an existing chart: `page_get` → `settings_describe` (component, search) →
`settings_set`; check with `page_screenshot`.

Copy a section from a production report into a sandbox: `report_open(source)`,
`report_open(sandbox)`, `report_inspect` → `pages_import(source_report_id, page_ids,
section_name=...)` (dry run first), `report_theme_copy` if it must look identical,
`page_screenshot` both sides. Never write to the production report until the user
approves; then bring the redesign back with `pages_import(..., replace={...})`.

Investigate a broken report: `report_audit` (live=true renders every page;
`component.stale_sort` = a chart sorted by a leftover field whatever its Sort panel says;
fix: `settings_set` its `sortConcept` (field name) and `sortDir` again),
`datasource_get`, `datasource_dependencies`, `page_filter_inheritance`.

## Errors

- `login_required`: see Session above.
- `NO_ACTIVE_REPORT`: call `report_open` or pass `report_id`.
- `VERSION_CONFLICT`: re-read, re-plan. `EDITOR_STALE`: `report_refresh`, then retry.
- `EDITOR_DIRTY`: a human editor tab has unsaved changes; wait, do not discard them.
- `NATIVE_CONTRACT_CHANGED` / `*_CONTRACT_CHANGED`: Google changed the editor. Run
  `bundle_contracts`; report the `changed` entries to the user (the fix belongs in
  `bundle.py` of the MCP repo). Do not work around it with `report_patch`.
- `UNPUBLISHED_DATASOURCE`: someone left a datasource draft; ask before publishing it.
- `allow_shared_source` / `allow_in_use` flags exist for a reason: a reusable source
  has consumers outside this report. Ask the user before setting them.

## Keeping the inventory honest

The repo's `docs/inventory/ui-inventory.md` maps every editor UI surface to a tool and
status; `docs/coverage.md` holds the evidence. After Google changes the editor, follow
`docs/inventory/README.md` (refresh `native-catalog.json`, run `bundle_contracts`, walk
the menus).
