# CANONICAL REPO

**Canonical location: `C:\Users\jtracy\dev\cfb-power-rankings`**

This is the single source of truth for the Skeez CFB Rankings codebase.
All bots, work orders, and deployments must operate from this directory.

## Start here — MANDATORY, before any action on this repo

**Read these files first, in this order, before you run a command, edit a file,
or answer a question about how the site works:**

1. **`OPERATIONS.md`** — how the site runs, how it deploys, the refresh anchors, the
   admin gate, quotas, and which older docs are stale.
2. **`docs/DATA_FLOW.md`** — the authoritative map of every dataset: who produces it,
   which table stores it, what reads it, and whether it is durable. It also lists the
   traps that have already caused incidents (ephemeral container disk, D1-first
   serving, upsert overwrites, silent `@_guard` failures).
3. **`docs/DATA_PERSISTENCE_PLAN.md`** — the **active phased plan** for closing the data
   defect class (phases, acceptance criteria, sequencing, rollback). If you are about to
   change a producer or a reader, check whether a phase covers it first.
4. **`docs/DATA_SYMMETRY_AUDIT.md`** — the measured write/read audit (findings F1–F8 with
   receipts) that the plan works from.
5. **The traps → tests table** — in `docs/DATA_PERSISTENCE_PLAN.md`, Phase 7. Every trap this
   repo has paid for, next to the mechanical thing that CATCHES it, or labelled an accepted
   limitation. Check it before "fixing" anything that looks like a deploy or data-path bug:
   most of them already have a guard, and the ones that do not are written down as such.

**Why this is mandatory:** the site has repeatedly been broken by a session reasoning
about its wiring from memory instead of from a document. The v50 incident — a full week
of weekly pulls never reaching the site because the serving path read the image file
instead of D1 — happened for exactly that reason. Do not re-derive; read.

**Doc hygiene rule (Jeff, 2026-09-27):** update `OPERATIONS.md` after **every** deploy
— at minimum its `CURRENT STATE` block — and update `docs/DATA_FLOW.md` in the same
commit as any change to a producer or a reader. Any durable fact you had to discover by
reading source belongs in one of those files before you finish.

## History

On 2026-09-20, two stale duplicate clones were archived to
`C:\Users\jtracy\graveyard\2026-09-20\` after they caused a verification
incident (a bot validated against a stale clone and nearly blocked a deploy
on false conclusions). Before archiving, it was verified that every commit in
the clones (through `4e09f1f` "Havoc") was already contained in this repo's
history. The graveyard is retained for ~30 days as insurance, then deletable.

## Rules

1. Never clone or copy this repo into another profile's home directory.
2. If you find a second copy anywhere on this machine, treat it as suspect:
   verify against this repo's `git log` before trusting anything in it.
3. Feature work gets committed before a session ends — uncommitted work is
   invisible to deployments and to other bots.
