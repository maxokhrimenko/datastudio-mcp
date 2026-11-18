# Contributing

Thanks for helping to improve MCP for Google Datastudio by Max Omen.

## How

1. Fork the repository and create a feature branch from `main`.
2. Make the change. Follow [CLAUDE.md](CLAUDE.md): every write is previewed,
   fingerprint-checked, journalled and read back; no company-, customer- or person-specific
   data in code, tests or docs (use example IDs, emails and project names).
3. Write everything in English (see below).
4. Run the offline tests: `uv sync --frozen && uv run pytest -q`.
5. Add a line to `CHANGELOG.md` for any user-visible change.
6. Open a pull request against `main` and tick the contributor-terms box in the template.

**English only.** Everything you contribute is in English: code, comments, tests, docs,
commit messages, branch names, pull request titles and descriptions, and review comments.
Contributions in any other language are not accepted.

Only the author merges into `main`. Large changes: open an issue first to agree on the
approach. Issues follow [ISSUE_RULES.md](ISSUE_RULES.md).

## Contributor terms

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE) and is
also offered under commercial licenses by its author, Max Omen. To keep that possible, every
contribution comes with these terms. By submitting a pull request, patch or other
contribution ("Contribution"), you agree that:

1. **Your right to contribute.** The Contribution is your original work, or you have the
   right to submit it under these terms, and it does not include code or data you are not
   allowed to share (for example your employer's or a client's confidential material).
2. **License to the author.** You grant Max Omen and his successors a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, reproduce, modify, sublicense
   and distribute the Contribution, as part of this project or separately, under any
   license terms, including the PolyForm Noncommercial License and commercial licenses.
3. **Patents.** You grant the same parties a perpetual, worldwide, royalty-free,
   irrevocable patent license for any patent claims you can license that the Contribution
   would otherwise infringe.
4. **No obligation.** The author may accept, change or decline any Contribution. Nothing
   here obliges the author to use it or to pay for it.
5. **Credit.** Accepted Contributions keep your authorship in the git history.

You keep the copyright in your Contribution; these terms only grant the licenses above.
