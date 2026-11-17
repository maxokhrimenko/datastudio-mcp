# MCP for Google Datastudio by Max Omen

An MCP server that lets AI agents (Claude Code, Codex and any other MCP client) build,
edit, style and maintain Google Datastudio reports and datasources end to end — the way a
person does it in the editor, with every change previewed, conflict-checked, journalled
and read back.

> **License:** free for personal and noncommercial use under the
> [PolyForm Noncommercial License 1.0.0](LICENSE). Any commercial use — including use at
> or for a company — needs a commercial license. See [License and permitted use](#license-and-permitted-use).

---

## Why

Datastudio has no public API for editing report content. An agent can describe a
dashboard, but someone still has to build it by hand. This MCP drives the Datastudio
editor itself, in a dedicated Chrome profile on your machine, through the editor's own
services:

- **Everything the editor does:** pages, navigation, every chart type and preset, every
  property-panel setting (~7,600 of them), filters and controls, blends, calculated
  fields, parameters, themes, publishing and revision history.
- **Safe by construction:** writes default to a dry run; each one carries the fingerprint
  of what you read, so a concurrent edit is rejected instead of overwritten; every write is
  journalled with its before-state and verified by reading it back. Most writes can be
  undone with `operation_restore`.
- **Runs locally:** the MCP runs on your computer and talks only to Google. Your Google
  cookies and tokens never leave its browser profile.

## How it works

```
MCP client (Claude Code, Codex, …)
        │  stdio
        ▼
datastudio-mcp  ── Playwright / CDP ──►  Chrome (dedicated profile, headless)
                                              │  your Google session
                                              ▼
                                      datastudio.google.com editor
```

- A dedicated Chrome profile (`~/.datastudio-mcp/profile`) holds your Google sign-in. Chrome
  runs headless; a window opens only when Google asks you to sign in.
- Report edits go through the editor's native model and services, so they produce exactly
  what the editor would save.
- Chrome closes itself after 10 minutes without a tool call and starts again on the next
  one; the sign-in is kept.

## Requirements

- **Google Chrome**. Developed and tested on macOS; on Linux set `DATASTUDIO_CHROME_BINARY`
  to your Chrome executable (untested). Windows is not supported yet.
- [uv](https://docs.astral.sh/uv/) (it brings Python 3.12 itself).
- A Google account with access to the reports you want to edit.

## Install

### Claude Code

```sh
claude mcp add datastudio --scope user -- \
  uvx --refresh --from git+https://github.com/maxokhrimenko/datastudio-mcp datastudio-mcp
```

### Codex

```sh
codex mcp add datastudio -- \
  uvx --refresh --from git+https://github.com/maxokhrimenko/datastudio-mcp datastudio-mcp
```

`uvx --refresh` picks up the latest version every time the client starts the server.
Restart the client session (in Claude Code: `/mcp`) after installing.

### Agent skill (recommended)

The repository ships a skill that teaches agents the workflow (read → dry run → apply →
verify) and the tool map. Install it from a clone:

```sh
git clone https://github.com/maxokhrimenko/datastudio-mcp.git
cd datastudio-mcp
scripts/install-skill.sh   # links skills/datastudio-mcp into ~/.claude/skills and ~/.agents/skills
```

### From source

```sh
git clone https://github.com/maxokhrimenko/datastudio-mcp.git
cd datastudio-mcp
uv sync --frozen
claude mcp add datastudio --scope user -- "$PWD/.venv/bin/datastudio-mcp"
```

## First run

Ask your agent to open a report:

> Open https://datastudio.google.com/reporting/&lt;report-id&gt; and list its pages.

The first `report_open` starts Chrome. If Google wants a sign-in, a Chrome window appears:
sign in there once and ask the agent to continue. The session is stored in the MCP's own
profile — signing in to your everyday Chrome does not count.

## What you can do

| Area | Tools |
|---|---|
| Reports and pages | `report_open`, `report_inspect`, `report_audit`, `pages_create`, `pages_delete`, `page_duplicate`, `pages_import`, `navigation_patch`, `report_rename`, `page_get`, `page_screenshot`, `page_insert_space` |
| Components | `components_create`, `components_fields`, `components_switch_type`, `components_geometry`, `components_arrange`, `components_layers`, `components_duplicate`, `components_copy_to_page`, `components_delete`, `groups_*`, `image_upload_set` |
| Every panel setting | `settings_describe`, `settings_set`, `settings_set_batch`, `native_catalog`, `theme_from_image`, `report_theme_copy`, `dimension_value_colors_set` |
| Data | `datasources_list`, `datasource_get`, `bigquery_browse`, `datasource_create_bigquery`, `datasource_add_to_report`, calculated fields, field metadata, parameters, credentials, `datasource_sharing_get` / `_set` |
| Blends | `blend_create`, `blend_get`, `blends_joins`, `blends_input_filters`, `blends_rename`, `blends_delete` |
| Filters and controls | `filters_*`, `components_filters`, `page_filters`, `report_filters`, `page_filter_inheritance`, `control_*` |
| Publishing and history | `report_publishing`, `report_publish`, `revisions_list`, `revision_restore`, `report_links`, `report_pdf`, `image_export` |
| Recovery | `operation_get`, `operation_restore`, `bundle_contracts` |

109 tools in total. The full editor coverage, item by item, is in
[docs/inventory/ui-inventory.md](docs/inventory/ui-inventory.md).

### Datasources and connectors

The MCP **creates** new datasources from **BigQuery only**: a table or a custom query
(`bigquery_browse` → `datasource_create_bigquery`), including date range and viewer email
parameters.

Any other connector (Google Sheets, Google Analytics, Google Ads, Search Console, CSV
upload, MySQL/PostgreSQL, partner connectors, …) is **added by you** in the Datastudio UI:
*Create → Data source* (or *Add data* in a report), pick the connector, authorize it. Once
the datasource exists, the MCP works with it like with any other:

- attach it to a report with `datasource_add_to_report`, or use it where it is already
  attached;
- build and edit charts, tables, controls, filters and blends on it;
- edit its fields, calculated fields, parameters, settings, name, sharing and refresh its
  fields.

BigQuery-only extras: `bigquery_browse`, and the default reader account of
`datasource_credentials_set` (`DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE`); for other connectors
pass the account explicitly or set credentials in the UI.

### Every write follows the same contract

1. **Read** the entity and take its `fingerprint`.
2. **Preview:** writes default to `dry_run=true` and return the exact planned change.
3. **Apply** with `dry_run=false` and the same fingerprint. `VERSION_CONFLICT` means
   something changed since your read.
4. Only `verified` (or `unchanged`) is success. Writes land in the report **draft**;
   viewers see them after `report_publish` when the report uses manual publishing.

### Not done by the MCP

Some actions stay with people by design: sharing reports, changing or removing existing
datasource access, scheduled email delivery, creating, copying or deleting reports,
creating datasources from connectors other than BigQuery (see
[Datasources and connectors](#datasources-and-connectors)), and deleting datasources.

## Configuration

All optional.

| Variable | Default | Meaning |
|---|---|---|
| `DATASTUDIO_PROFILE_DIR` | `~/.datastudio-mcp/profile` | Chrome profile with the Google sign-in |
| `DATASTUDIO_STATE_DIR` | `~/.datastudio-mcp/state` | Operation journal (before-states for undo) |
| `DATASTUDIO_CHROME_BINARY` | macOS Google Chrome | Chrome executable |
| `DATASTUDIO_IDLE_CLOSE_S` | `600` | Close Chrome after this many idle seconds; `0` keeps it open |
| `DATASTUDIO_SERVICE_ACCOUNT_TEMPLATE` | — | Default for `datasource_credentials_set`, e.g. `datastudio-viewer@{project}.iam.gserviceaccount.com` |

Pass them with `claude mcp add -e NAME=value …` or `codex mcp add --env NAME=value …`.

## Security and privacy

- The profile directory contains your Google session cookies. Treat it like a password:
  never share it or commit it.
- The MCP exposes no tool that runs arbitrary JavaScript or calls arbitrary endpoints.
- The operation journal stores before-states of your reports locally; it never leaves
  your machine.
- Report security issues privately: open a
  [GitHub security advisory](https://github.com/maxokhrimenko/datastudio-mcp/security/advisories/new).

## Stability

The MCP talks to the Datastudio web editor, which has no public compatibility guarantee.
When Google changes the editor, affected tools fail loudly with `*_CONTRACT_CHANGED`
instead of writing something wrong; `bundle_contracts` shows exactly what moved.

## Development

```sh
uv sync --frozen
uv run pytest -q          # offline tests; nothing launches a browser or touches a report
```

Engineering rules for contributors and coding agents: [CLAUDE.md](CLAUDE.md).
Release notes: [CHANGELOG.md](CHANGELOG.md).

## Contributing

Pull requests are welcome. Fork the repository, work on a feature branch and open a pull
request against `main`; the author reviews and merges every change. Read
[CONTRIBUTING.md](CONTRIBUTING.md) first: by opening a pull request you agree to its
contributor terms, which let the author keep offering the whole product under both the
noncommercial and commercial licenses. Bugs and ideas go to
[issues](https://github.com/maxokhrimenko/datastudio-mcp/issues).

## License and permitted use

Copyright (c) 2026 Max Omen. Licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE).

**You may**, free of charge:
- use it for personal projects, study, research and experiments;
- use it in charitable, educational, public research, public health or government
  organizations;
- read, modify and share the source for those noncommercial purposes, keeping the license
  and the `Required Notice` line.

**You may not**, without a commercial license:
- use it for any commercial purpose — at or for a company, for clients, in paid services,
  consulting or internal business reporting;
- sell it, resell it, offer it as a hosted service, or ship it inside a commercial product;
- remove or change the license and copyright notices, or present the software or a
  derivative of it as your own product.

**Commercial license:** open an issue titled `Commercial license` or contact
[@maxokhrimenko](https://github.com/maxokhrimenko) on GitHub.

The software is provided as is, without warranty. You are responsible for what you do
with your reports and data through it.

## Trademarks

Google, Datastudio, Looker Studio and Chrome are trademarks of Google LLC. This project is
an independent product and is not affiliated with, endorsed or sponsored by Google.
