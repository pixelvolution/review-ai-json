# Project Orchestrator

This repo holds three things:

| Path | What it is |
|---|---|
| `app.py` | The original WordPress JSON reviewer (FastAPI, deployed on Render) |
| `mock_wallet/` | Mock wallet, strategy research and the suggestion/validation pipeline (prototype) |
| `solpaper/` | The paper-trading research system for new Solana pairs, built phase by phase |

## Active Agents

The specialist agents live in `.agents/`. Invoke them by name.

- **Project Bible**: `.agents/bible.md`
  - Run after completing a feature, after an architectural decision, or after an incident.
  - Invoke: "Update the Project Bible based on [what was just done]."
- **Flagging Agent**: `.agents/flagging.md`
  - Run before a build to turn rough requirements into `specs/<slug>/SPEC.md` (Mode A), and after a build to check it against the spec (Mode B).
  - Invoke: "Run the Flagging Agent on [feature]."
- **Anti-Vibe Code**: `.agents/anti-vibe.md`
  - Run after a large generated change, or when ghost code is flagged.
- **Version Warden**: `.agents/version.md`
  - Run after a verified change ships, to bump the version and the changelog.
- **Drift Detective**: `.agents/drift.md`
  - Run before a deploy, after infrastructure changes, or when config drift is suspected.
- **Dependency Sentinel**: `.agents/dependency.md`
  - Run weekly, before major releases, or after adding packages.
- **Crawl Detective**: `.agents/crawl.md` and **Thin Content Sentinel**: `.agents/thin-content.md`
  - SEO auditors. Only relevant if a public web surface is added. Today they should report N/A.

## Orchestrator Rules

1. Never modify production databases without explicit confirmation.
2. Never push to git without running tests first.
3. If a task is ambiguous, write a `CLARIFY.md` listing questions. Do not guess.
4. Log all significant actions to `PROJECT_BIBLE.md` via the Bible agent.
5. Prefer reversible changes over irreversible ones. If irreversible, flag it.
6. Each agent operates within its defined scope boundary. Do not cross them.

## Project Rules (solpaper)

7. **Simulator only.** No code may hold keys, connect a wallet, sign or send a
   transaction, or place a real order. `solpaper/tests/test_phase1.py` enforces this.
8. **Never commit secrets.** Tokens and keys live in `.secrets/` or `*.env` (both
   gitignored), never in source, config or docs.
9. **solpaper gets its own infrastructure.** It must never read from or write to
   another project's database, storage or edge script (e.g. Project Uploader's
   `project-photo` DB).
10. **Honest results over good-looking ones.** Never fabricate data to fill a gap,
    never use look-ahead, and flag any result that looks too good to be true.
11. **Phased build.** Stop after each phase of `solpaper/` and show the results
    before starting the next.

## What "Done" Means

A task is complete when:

- [ ] The code runs without errors
- [ ] Relevant tests pass (or new tests were written)
- [ ] The Project Bible has been updated with what changed and why
- [ ] No new lint errors introduced
- [ ] If a deploy occurred, the Drift Detective has verified the environment post-deploy
