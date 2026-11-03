# Development Log

This is the project's persistent memory. Chat session history is ephemeral
(local transcripts are auto-deleted after ~30 days), so **everything a
future developer session needs to pick up the work lives here**, in git,
pushed to the remote. Read this file top to bottom before touching code
after any gap in development.

Rules for maintaining this file are in [CLAUDE.md](CLAUDE.md).

---

## Status snapshot

*Last updated: 2026-11-03*

**What the app is.** Fiori Plus — a FastAPI + htmx web app for importing
TSV financial exports (FIORI exports from the university's SAP system) into
Postgres and analyzing project finances: transactions browser, per-project
dashboards with budget projections, and a cross-project per-person × month
grid. Deployed via Docker on a single Linux host, served at
https://fiori-plus.true-bar.si through the host's Traefik proxy (see
[DEPLOY.md](DEPLOY.md)).

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

**Known open items / ideas.** None open. The app is live at
https://fiori-plus.true-bar.si (verified 2026-11-03).

---

## Log (newest first)

### 2026-11-03 — Public HTTPS route via Traefik (deployed and verified)

**Outcome.** Live at https://fiori-plus.true-bar.si. Cloudflare A record
(DNS only) created, app container recreated, Let's Encrypt certificate
issued (valid until 2027-02-01, auto-renewed by Traefik), HTTP → 308 →
HTTPS → 303 `/login`. Host port 8000 is loopback-only. The original
handoff text below is kept for the reasoning.

**Problem.** The app had been reachable only on `http://88.200.23.57:8000`
from inside the 88.200.23.0/24 subnet. The host firewall (`PERIMETER`
iptables chain, hooked into both INPUT and Docker's `DOCKER-USER`) drops
everything from the internet except ports 22, 80, 443 (443 Slovenia-only
via `SI-ONLY`) and a few Tailscale/Headscale UDP ports, so port 8000 was
invisible from outside. Opening 8000 was rejected: plain-HTTP logins, no
CrowdSec, and the firewall rules live outside the repo.

**Done (committed, not yet applied).**
- `docker-compose.prod.yml`: `app` joins the external Docker network
  `web` (owned by `/opt/truebar/traefik`), gets Traefik Docker-provider
  labels (http router → permanent HTTPS redirect; https router with
  `certresolver: http`; service port 8000; `traefik.docker.network: web`),
  and the host port binding is now `${APP_BIND:-127.0.0.1}:${APP_PORT}`
  so 8000 is loopback-only. New required var `APP_HOST`.
- `.env.prod` on the server: `APP_HOST=fiori-plus.true-bar.si`,
  `APP_BIND=127.0.0.1`, `SESSION_HTTPS_ONLY=true`. `.env.prod.example`
  updated to match.
- `DEPLOY.md` rewritten around the Traefik route: Cloudflare DNS step
  (A record, **DNS only / grey cloud**, no AAAA), how the labels work,
  troubleshooting commands. Fixed the backup volume name
  (`fiori-plus_db_data`).

**Steps that were followed (all done 2026-11-03).**
1. Cloudflare, zone `true-bar.si`: add `A` record `fiori-plus` →
   `88.200.23.57`, proxy status **DNS only**. Existing `*.true-bar.si`
   hosts on this server are all DNS-only.
2. On the server, from the repo dir:
   `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`
   (recreates `app`; `db` is untouched). The Claude Code session could
   not run this — blocked as a production deploy.
3. Verify: `docker logs traefik --since 5m | grep -i fiori` shows the
   router; `curl -skI https://fiori-plus.true-bar.si/` returns 303 to
   `/login`; cert is from Let's Encrypt (not the Traefik default).
4. Note for users: with `SESSION_HTTPS_ONLY=true`, logging in over
   `http://127.0.0.1:8000` no longer works (cookie is `Secure`). That is
   intended.

**Rejected alternative.** Adding `8000` to the `PERIMETER` allow list.

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
