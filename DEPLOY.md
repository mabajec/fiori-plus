# Deploy Fiori

Production setup for a single Linux host running Docker. The app is served
at **https://fiori-plus.true-bar.si** by the host's Traefik reverse proxy,
which terminates TLS (automatic Let's Encrypt certificate) and forwards to
the app container over the shared Docker network `web`. The app itself
listens on plain HTTP inside the container; nothing else is exposed to the
internet.

Why not just open a port: the host firewall (`PERIMETER` iptables chain,
applied to both the host and Docker's forwarding path) only admits ports
22, 80 and 443 from the internet, and 443 only from Slovenian IP ranges.
Traefik also runs a CrowdSec bouncer on both entrypoints. Routing through
Traefik gives the app HTTPS and that protection for free; opening 8000
would bypass all of it and serve logins over plain HTTP.

## One-time setup

1. **Clone the repo on the server.**

   ```bash
   git clone <repo-url> /srv/fiori
   cd /srv/fiori
   ```

2. **Create the inputs directory.** TSV exports from the source financial
   system will live here; the container mounts it read-only. By default
   this is `./inputs` inside the repo — `cp file.tsv inputs/` "just works".
   If you'd rather host it elsewhere (a separate data volume, say), set
   `HOST_INPUTS_DIR` in `.env.prod`.

   ```bash
   mkdir -p inputs
   ```

3. **Copy and edit the env file.**

   ```bash
   cp .env.prod.example .env.prod
   $EDITOR .env.prod
   ```

   Two secrets need real random values — generate them with:

   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # POSTGRES_PASSWORD
   python3 -c "import secrets; print(secrets.token_urlsafe(64))"   # SESSION_SECRET
   ```

   `APP_HOST` is the public hostname Traefik routes (default
   `fiori-plus.true-bar.si`). `SESSION_HTTPS_ONLY` stays `true` in
   production. `APP_BIND`/`APP_PORT` (default `127.0.0.1:8000`) bind the
   app on loopback for local checks only; `HOST_INPUTS_DIR` defaults to
   `./inputs` (in the repo) and only needs to be set if you want the
   directory somewhere else.

3b. **Create the DNS record.** In Cloudflare (zone `true-bar.si`), add an
   `A` record `fiori-plus` → the host's public IPv4 (`88.200.23.57`),
   **proxy status "DNS only" (grey cloud), not proxied**. All other
   `*.true-bar.si` hosts on this server are set up the same way. Proxying
   would break two things: Traefik's Let's Encrypt HTTP challenge, and the
   firewall's Slovenia-only rule on 443, which would drop Cloudflare's
   edge IPs. Do not add an `AAAA` record — the firewall rules are IPv4.

   The Docker network `web` must exist (it is created by the Traefik
   deployment in `/opt/truebar/traefik`); `docker network ls | grep web`.

4. **Build and start.**

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
   ```

5. **Apply migrations.**

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec app alembic upgrade head
   ```

6. **Create the first admin.**

   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod \
     exec app fiori init
   ```

   The temp password is printed once. Save it somewhere safe, then log in
   at `https://fiori-plus.true-bar.si` — you'll be forced to change it on first
   login and then enroll in 2FA (scan QR with Google Authenticator / Authy
   / 1Password / etc.).

## Day-to-day commands

All commands run from `/srv/fiori`.

```bash
# Tail logs
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f app

# Add a colleague
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec app fiori user create alice@example.com --name "Alice"

# List users
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec app fiori user list

# Reset someone's password (prints a new temp)
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec app fiori user reset-password alice@example.com

# Reset someone's 2FA (they re-enroll on next login)
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec app fiori user reset-2fa alice@example.com
```

Tip: most folks alias the long prefix:

```bash
echo 'alias fc="docker compose -f /srv/fiori/docker-compose.prod.yml --env-file /srv/fiori/.env.prod"' >> ~/.bashrc
# Then:
fc exec app fiori user list
```

## Updates

```bash
cd /srv/fiori
git pull
docker compose -f docker-compose.prod.yml --env-file .env.prod build app
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec app alembic upgrade head     # only when migrations were added
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

## Backups

The database lives in the named Docker volume `fiori-plus_db_data`. Back
it up with `pg_dump` (run inside the db container):

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec -T db pg_dump -U fiori fiori | gzip > backup-$(date +%F).sql.gz
```

Inputs and the application code don't need separate backup — they live
in the git repo and on the host filesystem respectively.

## How the Traefik route works

Traefik on this host uses the Docker provider (`exposedByDefault: false`),
so the route is defined entirely by labels on the `app` service in
`docker-compose.prod.yml`:

- `fiori-plus` router on the `http` entrypoint → permanent redirect to
  HTTPS (the ACME challenge is answered by Traefik before routing).
- `fiori-plus-secure` router on the `https` entrypoint with
  `certresolver: http`; Traefik requests and renews the certificate.
- `traefik.docker.network: web` tells Traefik which of the container's
  networks to dial; the service targets container port 8000.

Uvicorn runs with `--proxy-headers`, so it trusts `X-Forwarded-Proto`
from Traefik and `SESSION_HTTPS_ONLY=true` makes the session cookie
`Secure`.

Troubleshooting:

```bash
# Did Traefik pick up the router?
docker logs traefik --since 5m 2>&1 | grep -i fiori
# Does the route answer locally (expect 308 → https, then 303 → /login)?
curl -sI -H 'Host: fiori-plus.true-bar.si' http://127.0.0.1/ | head -3
curl -skI --resolve fiori-plus.true-bar.si:443:127.0.0.1 https://fiori-plus.true-bar.si/ | head -3
# Certificate issued? (needs the DNS record to exist first)
docker exec traefik cat /etc/traefik/acme.json | grep -c fiori-plus
```

A certificate error right after the first start usually means the DNS
record did not exist yet when Traefik first tried; it retries on its own.
