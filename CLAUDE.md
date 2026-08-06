# Fiori Plus — instructions for Claude Code

## Project memory lives in DEVLOG.md — this is mandatory

Session history is ephemeral and machines change. The single source of
truth for "where are we and what's next" is [DEVLOG.md](DEVLOG.md),
tracked in git and pushed to `origin`.

**At the start of every session:** read DEVLOG.md (at least the Status
snapshot and the most recent log entries) before doing anything else.

**Before ending any session in which the project changed** (code, schema,
deployment, decisions — anything a future developer would need):

1. Append a dated entry at the top of the Log section in DEVLOG.md:
   what was done, why, decisions made and alternatives rejected, and —
   most importantly — **open threads**: anything unfinished, known bugs,
   and the concrete next step.
2. Update the Status snapshot (feature state, open items, last-updated
   date) so it stays accurate on its own.
3. Commit the devlog update and **push to origin**, so the information is
   available from any computer. If other work from the session is not yet
   committed, commit that first — never leave the devlog describing
   uncommitted work as done.

If a session ends mid-task, the devlog entry must be written as a handoff:
assume the person picking it up remembers nothing and may arrive months
later.

## Git workflow

- Branch off `main` for each piece of work; merge back with `--no-ff`.
- No Claude attribution lines in commit messages.
- Push `main` to `origin` after merging.

## Quick orientation

FastAPI + htmx + SQLAlchemy/Alembic + Postgres app for importing FIORI
(SAP) TSV exports and analyzing project finances. Routes in `app/web.py`,
import logic in `app/importer.py`, analytics in `app/analytics.py`,
templates in `app/templates/` (`_`-prefixed = htmx partials). CLI entry
point: `fiori` (`app/cli.py`). Deployment: [DEPLOY.md](DEPLOY.md).
