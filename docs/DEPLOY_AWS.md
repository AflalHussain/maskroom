# Deploying Maskroom on the AWS server (beside the sovereign-ai stack)

The server already runs the sovereign-ai platform as compose project `sovereign-ai` from
`/hms/apps/sovereign-ai/docker-compose.yaml`, including an nginx container that owns ports
80 and 443. Maskroom runs as its own compose project **`maskroom`** in
`/hms/apps/masking`, publishes nothing on the host, and is reached through that
same nginx on its own subdomain. Maskroom's bundled Keycloak provides sign-on at
`https://<domain>/sso/`.

```
browser / extension --443--> sovereign-ai-nginx-1 (existing container)
   sovereign-ai.hsenidmobile.com  -> platform, unchanged
   <MASKROOM_DOMAIN>              -> maskroom-app-1:8080      (deploy/aws/nginx/maskroom.conf)
   <MASKROOM_DOMAIN>/sso/         -> maskroom-keycloak-1:8180
project maskroom: app (image shipped from a workstation), db (postgres:16), keycloak; data in ./data
```

Files: `deploy/aws/docker-compose.server.yml` (standalone, uses the shipped image),
`deploy/aws/nginx/maskroom.conf.template`, `deploy/aws/install.sh` (runs on the server),
`deploy/aws/ship.sh` (runs on your machine and builds the image).

## 1. Prerequisites

- A DNS A record for the subdomain (for example `safepii.hsenidmobile.com`) pointing at the
  server's public IP. Find the IP with:
  ```bash
  TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4
  ```
- SSH access with the server's `.pem` key as a user that can run Docker (`ec2-user`), and a
  deploy directory that user owns: `sudo mkdir -p /hms/apps/masking && sudo chown ec2-user:ec2-user /hms/apps/masking`.
- Docker with buildx on **your machine** (the image is built here, never on the server).
- Roughly 3 GB of free RAM and 3 GB of free disk on the server.

## 2. Ship and install

From this checkout (branch with the auth work):

```bash
deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server>
```

This builds `maskroom:<git sha>` locally, streams it to the server with `docker save |
ssh docker load` (about 2 GB, compressed in flight; skipped when that tag is already there),
copies **only** the deploy files (`docker-compose.server.yml`, `install.sh`,
`nginx/maskroom.conf.template`, `keycloak/realm-maskroom.json`) to `/hms/apps/masking`, and
runs `install.sh` there, which:

1. creates `.env` on first run (asks for the domain and the admin emails; generates every
   secret; mode 600) and records the shipped image tag;
2. renders the Keycloak realm into `data/keycloak-import/` with the domain, the client
   secret and random passwords for the two example users;
3. renders `nginx/maskroom.conf`, picks the certificate from `/hms/apps/masking/certs/`
   (see section 5), then validates the nginx config in a throwaway container;
4. adds a 2 GB swapfile when it can (`sudo` without a password);
5. starts `app`, `db`, `keycloak` from the shipped image; waits for health; warms the model;
6. prints the platform edit below and the example users' passwords.

Re-run `ship.sh` for every update; `.env`, certificates and `data/` survive. `SKIP_BUILD=1`
reuses the local image, `MASKROOM_TAG=...` names it.

## 3. Connect the platform's nginx (once)

Edit `/hms/apps/sovereign-ai/docker-compose.yaml`:

```yaml
services:
  nginx:
    networks: [default, maskroom]                       # add
    volumes:
      # ... existing lines ...
      - ../masking/nginx/maskroom.conf:/etc/nginx/conf.d/maskroom.conf:ro
      - ../masking/certs/safepii.hsenidmobile.com.cer:/etc/nginx/certs/maskroom.crt:ro
      - ../masking/certs/safepii.hsenidmobile.com.key:/etc/nginx/certs/maskroom.key:ro
networks:                                               # top level; add
  maskroom:
    external: true
    name: maskroom_default
```

Then `cd /hms/apps/sovereign-ai && docker compose up -d nginx`. Only nginx is recreated.
The Maskroom stack must be up first (the external network has to exist). The nginx config
resolves Maskroom's containers at request time, so nginx keeps starting even if Maskroom is
down; requests then return 502 until it is back.

Why nginx joins Maskroom's network rather than the reverse: putting Maskroom's `keycloak`
service on the platform network would register a second `keycloak` name there and the
platform's own `/auth` proxy could resolve to the wrong container.

## 4. Verify

```bash
cd /hms/apps/masking
docker compose ps                                        # app, db, keycloak up / healthy
curl -k https://<domain>/api/config                      # "auth_mode": "oidc"
curl -k https://<domain>/sso/realms/maskroom/.well-known/openid-configuration | head -c 400
   # issuer https://<domain>/sso/realms/maskroom ; token_endpoint http://keycloak:8180/... is expected
curl -k -o /dev/null -w '%{http_code}\n' https://<domain>/ext/update.xml   # 200 or 503, never 401
curl -sI https://sovereign-ai.hsenidmobile.com | head -1 # platform untouched
```

Browser (accept the certificate warning): `https://<domain>/` redirects to the Keycloak login
under `/sso/`; sign in as `admin@example.com` (admin if listed in `MASKROOM_ADMIN_EMAILS`);
mask a file and download it; `/admin/users` lists the user and can issue a service key;
sign out lands on the signed-out page and the next visit asks for credentials again.

Extension: on a machine that **trusts** `maskroom.crt` (see below), set the server URL to
`https://<domain>`, click Sign in, mask on claude.ai; `/admin` attributes it to the email.

## 5. Certificates

The installer looks in `/hms/apps/masking/certs/` for a CA-issued pair named after the domain,
`<domain>.cer` (or `.crt`/`.pem`) and `<domain>.key`, for example
`safepii.hsenidmobile.com.cer` + `safepii.hsenidmobile.com.key`. It converts a DER `.cer` to
PEM, checks that the key matches, and warns when the file holds a single certificate (nginx
needs the intermediate chain appended to it). Only when nothing is there does it generate a
self-signed pair, which browsers warn about and which **the Chrome extension cannot use**
unless each machine trusts it.

To install or renew a certificate later: put the new files in `/hms/apps/masking/certs/`
under the same names and run `cd /hms/apps/sovereign-ai && docker compose up -d nginx`
(the nginx mount points at those files). If the file names change, re-run
`/hms/apps/masking/install.sh` and apply the mount lines it prints.

## 6. Day-2

| Task | Command |
|---|---|
| Update to a new build | `deploy/aws/ship.sh -i ~/keys/server.pem ec2-user@<server>` (from your machine) |
| Logs | `docker compose logs -f app` / `keycloak` / `db` |
| Users, roles, keys | `docker compose exec app maskroom-admin user list` … `key create NAME` |
| Rules import/export | `docker compose exec app maskroom-admin policy export` |
| Database backup | `docker compose exec -T db pg_dump -U maskroom maskroom | gzip > data/backup-$(date +%F).sql.gz` |
| Disk | `docker system df`; `du -sh data/`; run scratch dirs under `data/runs` are not swept |
| Keycloak console | `https://<domain>/sso/admin/` with `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` from `.env` |
| Stop / start | `docker compose --profile keycloak stop` / `start` (in the masking dir) |

Sizing on this server (2 vCPU, 7.7 GB RAM shared with the platform): text masking answers in well under a second; scanned-PDF OCR takes several
seconds a page. Memory limits in the override (app 2.5 GB, Keycloak 1 GB, db 512 MB) keep a
runaway from affecting the platform.

## 7. Known limits and follow-ups

- Keycloak runs `start-dev --import-realm`, as the platform's own Keycloak does; a hardened
  `start --optimized` with its own Postgres is a later step.
- Switching to the platform's Keycloak instead of the bundled one is three `.env` values
  (`MASKROOM_OIDC_ISSUER`, client id, client secret) plus a client in that realm; it would
  save about 0.8 GB of RAM.
- The platform's `MASKING_SERVICE_URL` still points at the ai-guardrails service; wiring it
  to Maskroom (`http://maskroom-app-1:8080` once the backend joins the network) is a separate
  decision.
