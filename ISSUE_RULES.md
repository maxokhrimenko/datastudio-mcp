# Issue rules

How to file and keep GitHub issues for MCP for Google Datastudio by Max Omen. They apply to
people and to AI agents. The issue forms in [.github/ISSUE_TEMPLATE](.github/ISSUE_TEMPLATE)
follow these rules.

## Before you open an issue

- **Search first:** [open and closed issues](https://github.com/maxokhrimenko/datastudio-mcp/issues?q=is%3Aissue).
  Add what you know to an existing issue instead of opening a duplicate.
- **Security problems are not issues.** Report them privately through a
  [security advisory](https://github.com/maxokhrimenko/datastudio-mcp/security/advisories/new).
- **Check the scope.** Some actions stay with people by design (see
  [README › Not done by the MCP](README.md#not-done-by-the-mcp)); a request to automate
  them is closed as `wontfix`.

## Language and title

- English only: title, body and comments. Issues in another language are closed.
- Code identifiers, tool names, error codes, settings keys and commands stay verbatim.
- The title is one line of at most 72 characters, without a type prefix, tag or emoji.
- The title states the observable problem or the result you want:
  `page_screenshot refuses visible pages of older reports`, not `[bug] screenshot`.
- Short and factual. No walls of generated text.

## Type: exactly one label

| Label | Use when |
|---|---|
| `bug` | A tool does something wrong: an error it should not return, a wrong change, a write that is not read back. |
| `editor-change` | A tool fails with `*_CONTRACT_CHANGED` or `NATIVE_CONTRACT_FAILED`: Google changed the Datastudio editor. |
| `enhancement` | Something a person can do in the Datastudio editor that the MCP cannot do yet, or a better way to do it. |
| `documentation` | Only the README, the skill, the inventory or another doc needs to change. |
| `question` | You need an answer or a decision before anything can be done, including commercial licensing. |

Closing labels: `duplicate`, `invalid`, `wontfix`. A closed issue keeps its type label.

`bug` versus `editor-change`: if the error code ends in `_CONTRACT_CHANGED` or is
`NATIVE_CONTRACT_FAILED`, it is `editor-change`, even if it looks like a bug.

## Privacy: the repository is public

Never paste:

- cookies, tokens, XSRF values, the contents of `~/.datastudio-mcp/profile`;
- email addresses, people's names, company or client names;
- report, datasource, project or dataset IDs and URLs of private reports, SQL with real
  table names, field names or data values that reveal business data.

Replace them with placeholders (`<report-id>`, `user@example.com`, `example-project`) or
reproduce the problem on a test report with made-up data. A shortened tool result is fine
as long as it keeps the error code and the fields that matter.

## What a good issue contains

**Every issue:** the MCP version (`mcp_version` in the `report_open` result), the MCP
client (Claude Code, Codex, …) and the operating system.

**`bug`:**
1. The tool and its input (redacted).
2. What happened: the `status` (`rejected`, `verification_failed`, …) and the error code
   and message, verbatim.
3. What you expected, and why (the editor does X, the docs say Y).
4. Whether it repeats, and on which kind of report or chart.
5. Mark guesses as guesses. "The fingerprint changed" is a fact you saw; "I think the
   editor reloaded" is a guess. A cause is stated as a cause only if something you
   observed shows it.

**`editor-change`:** the failing tool, the error code and message, and the `bundle_contracts`
result for the same report (its changed entries name what moved).

**`enhancement`:** the editor action in plain words (menu, panel, button), what it changes
in the report, and why you need it. Name the row of
[docs/inventory/ui-inventory.md](docs/inventory/ui-inventory.md) if there is one.

**`documentation`:** the file and section, what is wrong or missing, and the corrected text
if you have it.

## One issue, one problem

One issue covers one failure or one capability. Two unrelated errors are two issues.
A large change gets an issue first, to agree on the approach before a pull request.

## Lifecycle

- New issues are read by the author. A label may be corrected and details may be asked.
- An issue closes when the fix is released (the release notes name it) or when it is
  closed as `duplicate`, `invalid` or `wontfix` with a short reason.
- Pull requests that fix an issue say `Fixes #N` in the description.

## AI agents

- File, comment on, label or close an issue only when a person asked for that exact
  action. Reading issues is always allowed.
- Do not file issues for problems noticed on the way; report them to the person instead.
- Apply these rules: English, one type label, no private data, facts separated from
  guesses.
- Run `report_open` and `bundle_contracts` to collect the version and the contract state
  before filing a `bug` or `editor-change`.
