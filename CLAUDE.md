# Repository contract

Engineering rules for contributors and coding agents. `AGENTS.md` points here.
Contributions are accepted under the terms in `CONTRIBUTING.md`.
Private, machine-local additions go in `CLAUDE.local.md` (ignored by git).

## Product

MCP for Google Datastudio by Max Omen: a local stdio MCP server that drives the Datastudio
editor in its own Chrome profile (`report_open` by URL or UUID, optional `report_id` on
every tool). No cloud deployment, no database; it talks only to Google.

Goal: everything a person does in the Datastudio editor UI
([docs/inventory/ui-inventory.md](docs/inventory/ui-inventory.md)). Done by people only,
never by the MCP: report sharing, changing or removing existing datasource access (the MCP
only adds viewers/editors to datasources, through the Share dialog, never building its
reCAPTCHA-guarded request), scheduled email delivery, report creation/copy/deletion,
datasources from connectors other than BigQuery, and datasource deletion. Reusable sources
have consumers outside the active report and must be identified as such in plans.

## Language

English only, everywhere in this repository, with no exceptions: code, comments,
docstrings, tests, documentation, commit messages, branch names, pull request titles and
descriptions, review comments, issues, changelog entries, tags and release notes. Not a
single word in another language reaches the repository or its GitHub pages.

## Invariants

- No arbitrary browser JavaScript or arbitrary endpoint execution exposed as tools.
- Session cookies and XSRF tokens remain inside the browser; never export or log them.
- Never work around Google's abuse protection (reCAPTCHA, rate limits): when it refuses,
  report it.
- Preserve unknown server fields during read-modify-write. Never reconstruct an
  existing page/report/datasource from a partial inventory.
- Every mutation has durable before-state, conflict detection and read-back.
  Timeout after dispatch means unknown outcome until read-back resolves it.
- Batch is not a cross-entity transaction. Report partial completion explicitly.
- No background retries of mutations, automatic publication of unrelated drafts,
  or silent success based on HTTP status alone.
- Live tests restore their changes; record pre-existing errors separately.
- The MCP owns tab state: tools must work whatever mode/tabs the user left
  (`ReportSession.acquire`); never ask the user to switch View/Edit.
- Native editor operations run on the View draft model (never autosaves) through
  `nativeops.NativeOps`, which merges only their delta over saved JSON and reloads.
- Never reference a minified bundle name (`pb`, `Oc`, `qxa`, ...) outside
  `bundle.py`. Declare the capability there, resolved by what the native code
  does, and call it through `R` from a script wrapped in `native_script`.
- No company-, customer- or person-specific data in the repository: test fixtures use
  example IDs, emails and project names.

## Entry points

`README.md`: setup and operation. `docs/inventory/`: the UI inventory (curated table +
generated native catalog; refresh with `scripts/refresh-inventory.py`).
`src/datastudio_mcp/`: implementation. `tests/`: offline behavior tests; no test launches
a browser or mutates a report. `skills/datastudio-mcp/SKILL.md`: how agents use the MCP.

Use `uv` and the lockfile. Runtime dependencies are exact pins: FastMCP owns MCP and
Playwright owns CDP.

## Releases

Semantic versions; every user-visible change gets a `CHANGELOG.md` entry. The version in
`pyproject.toml` and the skill's version line move together (a test checks it). Integrate
through pull requests with merge commits; tag the merge commit `vX.Y.Z`.
