# UI inventory

What the Datastudio editor lets a person do, and how the MCP covers each item. This is
the checklist to walk when Google ships changes, instead of rediscovering the product.

| File | What | Maintained by |
|---|---|---|
| [ui-inventory.md](ui-inventory.md) | Every UI surface (menus, toolbar, header, panels, dialogs, datasource flows) → feature → MCP tool → status | hand, from a UI walk |
| [native-catalog.json](native-catalog.json) | Every component type/preset with its complete property panel (tabs, sections, settings, controls, allowed values, ranges, bindings) plus the report (theme, layout, settings) and page panels | generated |

## Statuses

- ✅ **verified** — done through MCP on the sandbox report and independently read back / seen
  on a page screenshot during the last inventory pass.
- 🟢 **covered** — reachable through a generic engine that is verified on other members of the
  same family (e.g. any panel setting through `settings_set`), not exercised one by one.
- 🟡 **partial** — some of the feature works; the row says what is missing.
- ⛔ **gap** — no MCP path yet. The row says why and what it would take.
- ⚪ **n/a** — not a report-editing feature (help links, account, billing).
- 👤 **people only** — the owner decided this is done by a person in the UI, never by the MCP.

## Refreshing after a Datastudio release

1. `uv run python scripts/refresh-inventory.py <editable report with a datasource>` rewrites
   `native-catalog.json`. `git diff docs/inventory/native-catalog.json` lists every type, preset
   or setting that appeared, changed or vanished. New settings need no code: `settings_describe`
   and `settings_set` read the same panels. Removed/renamed keys may break saved scripts.
2. Run `bundle_contracts` on the same report. A `changed` contract names the resolver in
   `bundle.py` to update.
3. Walk the menus listed in ui-inventory.md (File, Edit, View, Insert, Page, Arrange,
   Resource, Help, toolbar, header, component context menu, Theme and layout, Add data). For a
   new item: perform it in the sandbox editor while recording the RPCs it sends (Playwright
   `page.on("request")` over the MCP profile's CDP port) and diff `report_content_get` /
   `page_get` before and after. Add a row with the result.
4. Re-run the sample flow (the sandbox report) for rows marked ✅ that touch changed areas.

The generic engines mean most UI additions are covered automatically:
`settings_describe`/`settings_set` (every property panel, including theme and common sections),
`components_create` (the editor's own Insert service, every type/preset),
`report_patch`/`components_patch` (any saved field the typed tools do not name).
