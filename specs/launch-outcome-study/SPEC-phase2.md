# SPEC: Launch Outcome Study, phase 2: schema, cohort, early features
**Feature slug:** launch-outcome-study (phase 2 of 6)
**Date:** 2026-11-07
**Status:** APPROVED. User go-ahead "Proceed with Interleaving, run the study" on PLAN.md. Decisions not answered took the PLAN's no-key defaults: coverage is reported as unknown, holder/creator features are deferred, and peaks are "observed peak".
**Requirement source:** "Add-on Feature: Launch Outcome Study" + `specs/launch-outcome-study/PLAN.md`.

## What This Is
A separate, passive process (`python -m solpaper.study`) that turns every
launch the scanner records into a cohort member, randomly samples a share of
them for later wallet-level data, and freezes cheap early features at 5, 15 and
60 minutes after first-seen, using only data timestamped before each window's
cutoff.

## What This Is NOT
- No wallet-level features yet: unique wallets, wash score, bundles, holders,
  creator history. Those are study phase 2b, after main Phase 3; the columns
  exist and stay NULL with a recorded reason.
- No checkpoints, outcomes or labels (study phase 3). No late-buyer
  simulation (phase 4). No report, MCP, dashboard (phases 5-6).
- No change to engine behavior. The scanner and filters are untouched.

## Functional Requirements

| ID | Requirement | Acceptance Criterion |
|----|-------------|----------------------|
| SF-01 | Tables `study_runs`, `launch_cohort`, `launch_features`, `launch_outcomes`, `late_buyer_sims`, `study_hypotheses`, `study_queue`, `study_gaps` exist in the solpaper DB. `launch_features` rows can never be updated or deleted (triggers). | Test: UPDATE/DELETE on `launch_features` raises. |
| SF-02 | A pair joins the cohort once, when the study first sees it in `pairs`, with source `pumpfun_curve`, `other_curve` (other launchpad curves) or `pool`, mapped from the normalized DEX label via config. | Test per source. |
| SF-03 | **Launch definition:** only pairs whose `pair_created_at` is at most `max_launch_age_min` before first-seen are launches. Others are counted as `excluded_not_new` and get no features. A missing creation time is also excluded, as `excluded_no_created_at`. | Tests. |
| SF-04 | **Sampling:** each cohort member gets `u = hash(run seed, pair)` in [0, 1); sampled if `u < sample_rate`. `u`, the rate and the reason are stored. Every graduate is also sampled (reason `graduate`). Sampling reads nothing from the filter tables. | Tests: rate within tolerance on 5,000 pairs; deterministic per seed; source scan shows no filter-table reference. |
| SF-05 | **Feature tasks** for windows 5/15/60 min are queued in `study_queue` when a pair joins. Each is due at first_seen + W - `lead_s`. | Test. |
| SF-06 | **No look-ahead:** a feature row for window W uses only `pairs_seen` rows with `observed_at <= data_cutoff`, and live reads made before it. `data_cutoff <= first_seen + W` always. | Test plants rows after the cutoff and asserts they're ignored. |
| SF-07 | Cheap features: observations count, latest liquidity, price, price change since first observation, m5/h1 volume, h1 buys/sells, buy/sell ratio, graduation status and minutes to graduate (mint seen in a non-curve pool, or curve `complete`). | Test on seeded data. |
| SF-08 | **Curve read** (pump.fun only): one batched read-only `getMultipleAccounts` (base64) at capture time. Stored: complete flag, virtual/real reserves (raw), and curve price = virtual SOL / virtual tokens. `curve_consistent` = curve price within `curve_price_tol` of the scanner's latest price. Progress and creator are **not** derived (layout not verified across curve variants). | Test on recorded account bytes. |
| SF-09 | **Late tasks:** if a task runs after its window's cutoff, the `pairs_seen` features are still computed with the cutoff, but live-only fields (curve) are NULL, the reason is recorded in `missing`, and a `study_gaps` row is written. Nothing is fabricated. | Test. |
| SF-10 | The queue lives in SQLite. A restarted process picks up pending tasks. | Test: new runner instance completes the old tasks. |
| SF-11 | The study has its own request-budget bucket per provider it uses (`solana_rpc: study`). | Config + test that study calls use the `study` bucket. |
| SF-12 | The engine never imports the study package. | Source scan test. |
| SF-13 | `python -m solpaper.study stats` prints, at the top, the observation window and cohort size, then: cohort by source, exclusions, sampling (rate, sampled, graduates), features captured and gap rate per window, and sample rows. Coverage is printed as **unknown** while discovery is saturated. | Test on a seeded DB. |
| SF-14 | The no-signing guard covers `solpaper/study/`. | The existing guard test scans the package recursively. |
