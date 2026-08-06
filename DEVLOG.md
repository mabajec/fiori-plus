# Development Log

This is the project's persistent memory. Chat session history is ephemeral
(local transcripts are auto-deleted after ~30 days), so **everything a
future developer session needs to pick up the work lives here**, in git,
pushed to the remote. Read this file top to bottom before touching code
after any gap in development.

Rules for maintaining this file are in [CLAUDE.md](CLAUDE.md).

---

## Status snapshot

*Last updated: 2026-08-06*

**What the app is.** Fiori Plus — a FastAPI + htmx web app for importing
TSV financial exports (FIORI exports from the university's SAP system) into
Postgres and analyzing project finances: transactions browser, per-project
dashboards with budget projections, and a cross-project per-person × month
grid. Deployed via Docker on a single Linux host behind an
externally-managed Traefik proxy (see [DEPLOY.md](DEPLOY.md)).

**Stack.** Python ≥3.10, FastAPI, SQLAlchemy 2 + Alembic, Postgres
(psycopg3), Jinja2 templates with htmx partials, Typer CLI (`fiori`
entry point), Docker Compose for dev and prod. No JS build step.

**Module map.**
- `app/web.py` — all routes (full pages + htmx partials)
- `app/importer.py` — TSV parsing: header-driven column resolution,
  encoding auto-detection, footer-total verification, natural-key dedup
- `app/analytics.py` — projections, category/monthly aggregates, people grid
- `app/models.py` / `alembic/` — schema and migrations
- `app/auth.py`, `app/totp.py` — password login + mandatory TOTP 2FA
- `app/cli.py` — `fiori init`, `fiori user create`, etc.
- `app/templates/` — `_`-prefixed files are htmx partials

**Feature state (all phases below are DONE and deployed):**
1. Importer scaffold, header-driven parser, footer-total verification
2. Imports web UI (2a); transactions browser with filters/sort/edit (2b)
3. Project metadata + budgets (3a); dashboards (3b); per-category
   projections, monthly trend, donut chart (3c)
4. Auth: password + mandatory TOTP, user management CLI (4a); project
   sharing (4b); production deployment (4c)
5. Cross-project per-person × month grid, including students
6. Import modes add/replace/analyze with selective apply; encoding
   auto-detection; header-mapping UI with saved/editable mappings;
   in-app file upload to per-user inputs subfolders
7. Rebrand to "Fiori Plus"; FIORI export guide on the imports page

**Known open items / ideas.** None recorded — the original session
transcripts were lost before this log existed. Nothing in the code is
known to be broken. Future entries must record open threads explicitly.

---

## Log (newest first)

### 2026-08-06 — Devlog established; earlier history reconstructed

Discovered that all Claude Code session transcripts from the May–June
development had been auto-deleted (30-day local retention), losing the
working context. Created this DEVLOG.md plus CLAUDE.md with the protocol
that keeps project memory in git from now on.

The snapshot above was reconstructed from git history (32 commits,
2026-05-16 → 2026-06-24) and the code itself, not from memory of the
sessions. Notable history:

- **2026-05-16** — bulk of the app built in one long push: phases 1–5
  (importer → transactions → dashboards/projections → auth/2FA/sharing →
  people grid) plus production deployment.
- **2026-05-17** — import hardening: add/replace/analyze modes with
  selective apply, footer-mismatch made mode-agnostic, encoding
  auto-detection, header-mapping UI with persistence, file upload with
  per-user inputs subfolders, project start/end dates clamping the
  projection period.
- **2026-05-18** — rebrand to Fiori Plus; fix 422 on `/transactions` when
  the filter form leaves `project_id` blank.
- **2026-06-24** — FIORI export guide added to the imports page and
  mapping panel (last feature work before this entry).

No uncommitted work was found; the tree was clean when this log was
created.
