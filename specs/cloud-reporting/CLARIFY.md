# CLARIFY: Cloud reporting for solpaper

**Feature slug:** cloud-reporting
**Date:** 2026-11-07
**Status:** BLOCKED on the questions below. No spec or code until they're answered.
**Requirement source:** "I want to run this from my local PC, from my AI box. But I want to also report to a front-end system on Cloudflare like we do with our Project Uploader."

## What is already settled

- The engine runs on your local AI box. The cloud side is **reporting only**.
- Data flows **one way**: the box pushes reports out and never reads anything
  back. Nothing in the cloud can change config, start a trade or stop one. The
  engine runs the same with reporting switched off (build prompt: "the engine
  must run fine with MCP, dashboard, and Ollama all turned off").
- solpaper gets **its own** database and edge app. It never uses Project
  Uploader's `project-photo` database, storage zone or edge script
  (`CLAUDE.md` rule 9; rule 1 forbids touching a production DB without
  explicit confirmation).

## Blocking questions

### Q1. Cloudflare or Bunny?
You said Cloudflare, but Project Uploader runs entirely on **Bunny**: Edge
Scripting (standalone script), Bunny Database (libsql over HTTP `/v2/pipeline`
with a bearer token), Bunny Storage and a linked pull zone.

| | Bunny (same as Project Uploader) | Cloudflare |
|---|---|---|
| Compute | Edge Script, ScriptType 1 | Worker |
| Data | Bunny Database (libsql) | D1 (SQLite) |
| Deploy | code -> `/publish` -> pull zone | `wrangler deploy` |
| Known pitfalls | Already in your Bible: ASCII-only source, `Cache-Control: no-store`, `CREATE IF NOT EXISTS` doesn't migrate, validate the rendered page | New to this project |

**Recommendation:** Bunny. It reuses a stack you've already debugged, and those
lessons carry over. Choose Cloudflare if you want this off Bunny on purpose.

### Q2. How does the box authenticate?
- **(a) Direct to the database.** The box holds a read-write DB token and
  writes rows over HTTP. This is simplest, and it's how Project Uploader's edge
  script talks to its DB. If the box's token leaks, it can rewrite or wipe
  every report.
- **(b) Through an ingest endpoint.** The box POSTs report batches to the edge
  app with an ingest-only token. Only the edge app holds DB credentials, and
  it can only append. A leaked box token can only add rows.

**Recommendation:** (b), least privilege.

### Q3. What gets pushed, and how often?
Proposal:

| Data | When |
|---|---|
| Heartbeat: agent status, run number, alive/cooked, wallet balance, vault, peak, drawdown | Every 60s |
| Closed trades with reason, delay, slippage, fees | As they close, batched |
| Skip-reason counts, wash-flag count | Every 5 min |
| Equity curve points | After every open/close (same as wallet snapshots) |
| Morning report | Once, after it's generated |

Never pushed: the raw response cache, full price history, anything secret.
Confirm, or say what to add or drop.

### Q4. Who can see the front end?
- An admin-key-gated page, like Project Uploader's `/` with `ADMIN_KEY`?
- Or also shareable read-only links, like the `/u/{token}` client links?

### Q5. What OS does the AI box run?
The build prompt says an always-on Linux machine with systemd. The path you
shared (`D:\Projects\ProjectPose`) is Windows. If the box is Windows, Phase 7
should ship a Windows service (NSSM or Task Scheduler) instead of, or as well
as, systemd units.

### Q6. When should this be built?
- As part of Phase 7 (deployment), after phases 2-6. **Recommended**: by then
  there are real reports to send.
- Right after Phase 1, so each later phase's data shows up in the cloud as it's built.

## What I need from you to build it (after the answers)
- A **new** Bunny Database, or D1 database, for solpaper. You create it in your
  account; I can't from this session. I need its URL.
- The deploy credentials, kept in `.secrets/` on your machine, never pasted into
  chat or committed. Deploys run from your machine, as they do for Project Uploader.
