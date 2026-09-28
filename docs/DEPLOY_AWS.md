# Deploying Maskroom on the AWS server (beside the sovereign-ai stack)

Same shape as the other `/hms/apps` deployments: images are pulled from the HMS registry,
**nothing builds on the server**, and the deployment is a directory with a compose file,
an `.env`, and the configs it mounts.

```
browser / extension --443--> sovereign-ai-nginx-1 (the platform's existing container)
   sovereign-ai.hsenidmobile.com  -> platform, unchanged
   safepii.hsenidmobile.com       -> maskroom-app-1:8080         (nginx/safepii.conf)
   safepii.hsenidmobile.com/sso/  -> maskroom-keycloak-1:8180    (Maskroom's own Keycloak)

/hms/apps/masking/            compose project "maskroom", network "maskroom_default"
├── docker-compose.yml        from deploy/aws/ in the repo
├── .env                      secrets and the domain (from .env.example)
├── nginx/safepii.conf        server block the platform's nginx mounts
├── keycloak/realm-maskroom.json   realm import; ${VAR} placeholders filled from .env
├── certs/                    safepii.hsenidmobile.com.cer + .key (CA-issued)
└── data/                     runtime: uploads, audit files, extension .crx
```

The app talks to Keycloak over the compose network (`http://keycloak:8180/sso/...`) while
Keycloak reports the public https issuer to browsers (`KC_HOSTNAME_BACKCHANNEL_DYNAMIC`), so
no hairpin to the public hostname and no certificate trust inside the container.

## 1. Publish the image (workstation)

```bash
export HMS_REPO_WORKBENCH_REGISTRY_ROBOT_USER=...
export HMS_REPO_WORKBENCH_REGISTRY_ROBOT_PWD=...
./docker-build.sh && ./docker-publish.sh          # repo.hsenidmobile.com/hms_data/maskroom:v<version>
```

The tag is `v<version>` from `pyproject.toml`; bump it for a release. `NO_CACHE=1
./docker-build.sh` refreshes the OS package layer.

## 2. Server prerequisites (once)

- DNS A record `safepii.hsenidmobile.com` -> the server's public IP (IMDSv2):
  ```bash
  TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4
  ```
- A deploy directory owned by the Docker user:
  `sudo mkdir -p /hms/apps/masking && sudo chown ec2-user:ec2-user /hms/apps/masking`
- Registry login: `docker login repo.hsenidmobile.com` (as `ec2-user`).
- The CA certificate and key in `/hms/apps/masking/certs/` as
  `safepii.hsenidmobile.com.cer` (PEM, full chain) and `safepii.hsenidmobile.com.key`
  (`chmod 600`). If the CA delivered DER: `openssl x509 -inform der -in x.cer -out safepii.hsenidmobile.com.cer`.
- Optional but recommended on 7.7 GB RAM: a 2 GB swapfile
  (`sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile`,
  plus the `/etc/fstab` line).

## 3. Deploy

Copy the deploy directory from the repo (workstation):

```bash
rsync -av -e "ssh -i ~/keys/server.pem" deploy/aws/ ec2-user@<server>:/hms/apps/masking/
```

(Contents of the directory, dotfiles included; `.env`, `certs/` and `data/` on the server are
left alone. Newer OpenSSH rejects `scp -r dir/.`, hence rsync.)

On the server:

```bash
cd /hms/apps/masking
cp .env.example .env && chmod 600 .env
nano .env                       # domain, MASKROOM_ADMIN_EMAILS, and every secret (openssl rand -hex 32)
docker compose pull
docker compose --profile keycloak up -d
docker compose ps               # app healthy after the model loads (~1-2 min)
```

The realm import reads `MASKROOM_DOMAIN`, `MASKROOM_OIDC_CLIENT_SECRET`, `KC_STAFF_PASSWORD`
and `KC_ADMIN_USER_PASSWORD` straight from `.env`, so the same realm file works for every
environment. Keycloak imports a realm only once; to re-import after changing those values,
`docker compose --profile keycloak down -v` (this also drops the Postgres volume, so do it
before real data exists, or export the realm from the console instead).

## 4. Connect the platform's nginx (once)

Edit `/hms/apps/sovereign-ai/docker-compose.yaml`:

```yaml
services:
  nginx:
    networks: [default, maskroom]                       # add
    volumes:
      # ... existing lines ...
      - ../masking/nginx/safepii.conf:/etc/nginx/conf.d/safepii.conf:ro
      - ../masking/certs/safepii.hsenidmobile.com.cer:/etc/nginx/certs/safepii.hsenidmobile.com.cer:ro
      - ../masking/certs/safepii.hsenidmobile.com.key:/etc/nginx/certs/safepii.hsenidmobile.com.key:ro
networks:                                               # top level; add
  maskroom:
    external: true
    name: maskroom_default
```

Then `cd /hms/apps/sovereign-ai && docker compose config -q && docker compose up -d nginx`.
Only nginx is recreated. Maskroom must be up first (the external network has to exist).
`docker compose exec nginx nginx -t` checks the merged config.

Why nginx joins Maskroom's network rather than the reverse: putting Maskroom's `keycloak`
service on the platform network would register a second `keycloak` name there and the
platform's own `/auth` proxy could resolve to the wrong container.

## 5. Verify

```bash
curl https://safepii.hsenidmobile.com/api/config                      # "auth_mode": "oidc"
curl https://safepii.hsenidmobile.com/sso/realms/maskroom/.well-known/openid-configuration | head -c 300
curl -o /dev/null -w '%{http_code}\n' https://safepii.hsenidmobile.com/ext/update.xml   # 200 or 503, never 401
openssl s_client -connect safepii.hsenidmobile.com:443 -servername safepii.hsenidmobile.com </dev/null 2>/dev/null | grep "Verify return code"
curl -sI https://sovereign-ai.hsenidmobile.com | head -1              # platform untouched
```

Browser: `/` redirects to the Keycloak login under `/sso/`; sign in as `admin@example.com`
with `KC_ADMIN_USER_PASSWORD` (admin if listed in `MASKROOM_ADMIN_EMAILS`); mask a file and
download it; `/admin/users` lists the user and can issue a service key; sign out lands on
the signed-out page and the next visit asks for credentials again. Extension: set the server
URL to `https://safepii.hsenidmobile.com`, click Sign in, mask on claude.ai.

## 6. Releases and day-2

| Task | Command |
|---|---|
| New version | bump `pyproject.toml`, publish (§1), set `MASKROOM_VERSION` in `.env`, `docker compose pull && docker compose --profile keycloak up -d` |
| Config change | edit `.env`, `docker compose --profile keycloak up -d` |
| nginx change | edit `nginx/safepii.conf`, `cd /hms/apps/sovereign-ai && docker compose exec nginx nginx -s reload` |
| Certificate renewal | replace the two files in `certs/`, reload nginx as above |
| Logs | `docker compose logs -f app` / `keycloak` / `db` |
| Users, roles, keys | `docker compose exec app maskroom-admin user list` … `key create NAME` |
| Rules import/export | `docker compose exec app maskroom-admin policy export` |
| Publish a desktop helper build | copy `SafePIIHelper-<version>.msi` into `data/ext/` on the server; `curl -s https://safepii.hsenidmobile.com/desktop/latest.json` to confirm |
| Publish an extension build | copy the `.crx` into the same `data/ext/` folder |
| Database backup | `docker compose exec -T db pg_dump -U maskroom maskroom \| gzip > data/backup-$(date +%F).sql.gz` |
| Disk | `docker system df`; `du -sh data/`; run scratch dirs under `data/runs` are not swept |
| Keycloak console | `https://safepii.hsenidmobile.com/sso/admin/` with `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` |

Sizing on this server (2 vCPU, 7.7 GB RAM shared with the platform): text masking answers
in well under a second; scanned-PDF OCR takes several seconds a page. Memory limits (app
2.5 GB, Keycloak 1 GB, db 512 MB) keep a runaway from affecting the platform.

## 7. Known limits and follow-ups

- Keycloak runs `start-dev --import-realm`, as the platform's own Keycloak does; a hardened
  `start --optimized` with its own Postgres is a later step.
- Switching to the platform's Keycloak instead of the bundled one is three `.env` values
  (`MASKROOM_OIDC_ISSUER`, client id, client secret) plus a client in that realm; it would
  save about 0.8 GB of RAM.
- The platform's `MASKING_SERVICE_URL` still points at the ai-guardrails service; wiring it
  to Maskroom is a separate decision.
- `data/ext/` is the client distribution folder (`EXT_DIST_DIR`, which defaults to
  `$MASKROOM_DATA_DIR/ext`). It is served without signing in, because a machine fetches an
  update before anyone has signed in on it. The newest helper is chosen by **version
  number**, so re-copying an old MSI cannot look like a new release; the newest extension
  `.crx` is chosen by file date. Nothing is published until a file is put there: both
  `/desktop/` routes answer 404 until an MSI is copied in, which is what this server does
  today.
- Helper builds must be code-signed before they are published to real users (PKG-2 in the
  readiness doc). The routes will serve an unsigned MSI perfectly happily.
