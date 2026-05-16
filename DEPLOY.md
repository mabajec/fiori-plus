# Deploy Fiori

Production setup for a single Linux host running Docker. The app runs on
plain HTTP behind an externally-managed Traefik reverse proxy that the
Traefik admins will later configure to terminate TLS and serve it on a
real domain.

## One-time setup

1. **Clone the repo on the server.**

   ```bash
   git clone <repo-url> /srv/fiori
   cd /srv/fiori
   ```

2. **Create the inputs directory.** TSV exports from the source financial
   system will be `scp`'d into this directory; the container mounts it
   read-only.

   ```bash
   sudo mkdir -p /srv/fiori/inputs
   sudo chown $USER /srv/fiori/inputs
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

   The defaults for `APP_PORT` (8000) and `HOST_INPUTS_DIR`
   (`/srv/fiori/inputs`) are fine unless you have a reason to change them.

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
   at `http://<server>:8000` — you'll be forced to change it on first
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

The database lives in the named Docker volume `fiori-scripts_db_data`. Back
it up with `pg_dump` (run inside the db container):

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod \
  exec -T db pg_dump -U fiori fiori | gzip > backup-$(date +%F).sql.gz
```

Inputs and the application code don't need separate backup — they live
in the git repo and on the host filesystem respectively.

## When Traefik adds HTTPS

When the Traefik admins put the app behind TLS at a real hostname,
two small things change:

1. Flip `SESSION_HTTPS_ONLY=true` in `.env.prod`, then
   `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`.
   The session cookie will then have the `Secure` flag set and the browser
   will refuse to send it over plain HTTP.
2. Nothing else. The app already passes `--proxy-headers` to Uvicorn,
   so it trusts `X-Forwarded-Proto: https` from Traefik when computing
   the request scheme.
