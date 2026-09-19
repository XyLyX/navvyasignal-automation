# NavvyaSignal — Handover Document (Pre-v2 Baseline)

Status: production paused for strategy/monetisation reconsideration. This document is the authoritative state as of the pause point, intended to let another engineer or agent continue without relying on chat history.

**Recovery reference:** git tag `navvyasignal-pre-v2-monetisation-baseline` → commit `fdcfc7b2df1984137ac3cb9333b4b43e70799e92`

---

## Architecture

Two independent repos, decoupled by design:
- **`navvyasignal-automation`** — research, writing, Notion push, email/WhatsApp send
- **`notion-framer-sync`** — Notion → Framer CMS sync, triggered by a separate Cloudflare Worker cron (`notion-framer-sync-cron`), not GitHub's native scheduler

## Canonical desks (7)

West Asia, India, UAE, Global Politics, Markets & Capital, Technology & AI, Maritime Energy & Supply Chains (no comma — Notion's multi-select rejects commas in option names).

Sports, Trends & Forecasting, and Real Estate & Infrastructure were removed as desks. Real Estate now exists only as a `Coverage Theme` tag.

## Content layers

`Signal` → `Today's Intelligence` (curated flag, max 7, no minimum) → `Cross-Desk` (standalone synthesized piece, `Content Type = Cross-Desk`) → `Watchlist` (`Watchlist = true` + `Watch Status`) → `Briefing` (weekly, `Content Type = Briefing`, pattern-level synthesis of the week's existing material, no new research).

## Production flow

Research (per-desk, solo dispatch, web search) → Notion write (`push_to_notion`) → `compile_send` (daily: fetches today's entries, runs `select_todays_intelligence`, `generate_cross_desk_signal`, `resolve_watchlist_items`, then sends via Kit + Whapi) → separately, `notion-framer-sync`'s Worker-triggered cron syncs unsynced Notion pages to Framer CMS → live site.

## Automation / scheduler

Single source of truth: `CRON_TO_RUN_TYPE` dict in `main.py` (not the workflow YAML — the YAML just passes the raw matched cron string through as `CRON_SCHEDULE`). Fails loudly (`sys.exit(1)`) if a cron string has no mapping, rather than silently defaulting.

| Cron (UTC) | GST | run_type |
|---|---|---|
| `33 1 * * *` | 05:33 | west_asia |
| `33 2 * * *` | 06:33 | maritime_energy |
| `33 10 * * *` | 14:33 | technology_ai |
| `3 11 * * *` | 15:03 | uae |
| `33 11 * * *` | 15:33 | india |
| `3 12 * * *` | 16:03 | global_politics |
| `33 12 * * *` | 16:33 | markets_capital |
| `3 14 * * *` | 18:03 | compile_send (only run type that sends) |
| `4 12 * * 5` | 16:04, Fri only | weekly_synthesis |

Every desk gets a **solo dispatch** — no multi-desk grouping. This was a deliberate fix: earlier multi-desk groups caused chronic under-coverage of lighter desks sharing a call with heavier ones (West Asia, Maritime, Technology & AI all carry dense ongoing-conflict content).

## Notion contract (database: "Signal Feed")

Core fields: `Name`, `Category` (desk, select), `Signal Brief`, `Text 1` (sources), `Long Read` (checkbox), `Ready to Post` (checkbox, hardcoded true — no human approval gate, by explicit user choice), `Synced to Framer` (checkbox, reset to false on every write so `sync.js` can detect and re-sync updates).

New metadata (Stage 1C, live since 2026-09-10): `Content Type` (select: Signal / Cross-Desk / Long Read / Briefing), `Coverage Theme` (multi-select, grows organically — currently includes Real Estate & Infrastructure, Defence & Security, AI Policy, Markets & Capital), `Today's Intelligence` (checkbox), `Watchlist` (checkbox), `Watch Status` (select: Active / Resolved / Abandoned), `Watch Trigger` (text), `Next Review` (date), `Resolution Signal` (relation, self-referencing), `Related Desks` (multi-select).

**Known schema debt (not cleaned up, by design — "don't backfill unless there's a later requirement"):**
- `Category` still carries all 9 old desk options as unused stale entries
- `Related Desks` still carries a stray `Maritime & Energy Desk` (old name) option from an early bug, alongside the correct `Maritime Energy & Supply Chains Desk`
- 467 records predate the metadata schema entirely (`Content Type: null`) — this is expected and was an explicit decision, not an oversight
- **Content Type has a "Long Read" option (3 records) that is separate from and possibly redundant with the existing `Long Read` checkbox field.** This inconsistency was discovered during handover compilation and is not yet understood — needs resolution before Stage 2 Framer work relies on either field.

**Current record counts (as of handover):** 196 Signal, 8 Cross-Desk, 3 "Long Read" (Content Type value), 2 Briefing, 467 null (legacy). Watchlist: 28 Active, 26 Resolved.

## Framer / frontend state

**Confirmed via live-site inspection:** still entirely on the old 9-desk taxonomy. "One signal per desk" is literal page copy on `/signals`, not just a layout convention. Destination pages: `/signals` (curated, 1/desk), `/feed` (fuller list, "Expand for All" links), `/category/[desk]` (full unpaginated archive — a real pre-existing UX issue independent of this project), `/editorials` (Long Read destination, filtered on the `Long Read` checkbox), `/signals/[slug]` (detail template, auto-splits `Signal Brief` into "What Happened"/"Why It Matters").

**Confirmed resolved:** the Cross-Desk duplicate created during Stage 1C idempotency testing was **never publicly visible** (404 on the live site before quarantine).

**NOT confirmed — marked UNKNOWN per instruction, not inferred:**
- Whether the temporary reassignment of Technology & AI / Maritime Energy & Supply Chains posts to a "Global" category actually completed. Neither `/signals` nor the checked portion of `/feed` shows Technology & AI content anywhere, including under Global Politics. Needs direct confirmation.
- Exact Framer CMS field list/types as configured in Framer Studio (public-site behavior was used to infer field usage; direct CMS panel inspection was not completed both in the original Stage 2 audit and in this handover pass)
- Exact mechanism setting `Synced to Framer` from the Framer side (backend side is confirmed: `sync.js` sets it after successful push)
- `/briefing` page structure — appears to be a homepage anchor (`./#lead-briefing`), not a separate page; unconfirmed

## Verified vs. awaiting-cycle-acceptance

**VERIFIED (production, not just dry-run):**
- All 7 desks individually validated with real research runs
- Watchlist creation, positive resolution, negative resolution — verified directly against real Notion records
- Cross-Desk generation quality and (after a fix) idempotency — verified
- Weekly synthesis quality and idempotency — verified
- `[TEST...]`/`[DUPLICATE...]` exclusion — verified
- **9 days of continuous unmodified automated operation** (2026-09-10 through 2026-09-19, confirmed via commit history — no code commits in that window, only automated log commits, every scheduled slot firing including the Friday weekly_synthesis)

This exceeds the original "Checkpoint A" bar (a single day's natural cron cycle) — 9 days of clean unattended operation is stronger evidence than the checkpoint originally called for.

## Security incident (contained, not resolved)

An unexplained process dispatched multiple `compile_send` and other runs, and pushed several commits, without being traced to any identified session or tool. Consequence: 3 duplicate subscriber emails sent in one day. Contained by regenerating the compromised GitHub PAT — old token confirmed dead (401 on test call), new token confirmed working. **Root cause was never identified.** No further recurrence observed in the 9 days since, but this should not be read as a confirmed resolution — only as an absence of further evidence.

## Temporary workarounds currently in place

- Technology & AI and Maritime Energy & Supply Chains posts reassigned to a "Global" Framer category as a stopgap against invisibility (their real categories don't exist in Framer yet) — **completion status of this reassignment is unconfirmed, see above**
- `Ready to Post` is hardcoded `true` with no human review gate — a deliberate standing choice, not a bug, but worth flagging as a monetisation-relevant editorial control question

## Outstanding items (not started / not completed)

- Full Stage 2 Framer build (brief was delivered, build not started)
- Technology & AI / Maritime Energy & Supply Chains proper Framer categories
- `Content Type = "Long Read"` vs. `Long Read` checkbox inconsistency
- `/category/[desk]` pagination (pre-existing issue)
- Direct Framer Studio CMS field confirmation
- `/briefing` page audit
- Security incident root cause

---

*Compiled as a read-only preservation/handover pass. No backend, Notion, Framer, or scheduler changes were made during this compilation.*
