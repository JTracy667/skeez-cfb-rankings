# CANONICAL REPO

**Canonical location: `C:\Users\jtracy\dev\cfb-power-rankings`**

This is the single source of truth for the Skeez CFB Rankings codebase.
All bots, work orders, and deployments must operate from this directory.

## Start here

**Read `OPERATIONS.md` before doing anything to this repo.** It is the operations
map: how the site runs, how it deploys, where its data actually lives (baked image
files vs D1), the refresh anchors, the admin gate, quotas, and which older docs are
stale. It exists so a session does not re-learn the site from source every time.

**Doc hygiene rule (Jeff, 2026-09-27):** update `OPERATIONS.md` after **every**
deploy — at minimum its `CURRENT STATE` block. Any durable fact you had to discover
by reading source belongs in that file before you finish.

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
