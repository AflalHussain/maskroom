# Hosting specification: Maskroom for ~200 daily users

Sizing for a fintech deployment where about 200 staff mask prompts and files through the
Chrome extension every working day. Based on measurements taken on the `feat/database-state`
branch on 2026-09-22 (`en_core_web_lg`, CPU only) and on the design-doc baselines in
[`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md) §6.

## 1. Measured cost per request

| Request | Measured |
|---|---|
| `POST /api/mask`, 1.5 KB prompt, in a session | 0.24 s |
| `POST /api/unmask`, same text | 0.13 s |
| `POST /api/process`, ~250-cell workbook | 1.0 s |
| `POST /api/process`, 11-page native PDF | 2.7 s |
| `POST /api/process`, 2-page scanned PDF (OCR) | 16.8 s |
| Vault download, 727 entries | 0.09 s |
| Audit list (admin) | 0.01 s |

Database work adds tens of milliseconds per request and nothing that scales with vault or
audit size.

## 2. Load model

| Assumption | Value | Peak hour (20% of the day) |
|---|---|---|
| Prompts masked per user per day | 30 | 1,200 per hour, one every 3 s |
| Files processed per user per day | 2 | 80 per hour |
| Share of files that are scanned PDFs | 10% | 8 per hour, ~20 s each |
| Cost per prompt | 0.25 s | ~5% of one worker |
| Cost per workbook or native PDF | 1 to 3 s | ~6% of one worker |

Throughput is not the constraint: one worker could carry the company at under 20%
utilisation. Two properties drive the design instead:

- **Requests are serialised per process** behind one engine lock, so a single long scanned
  PDF stalls every prompt behind it for minutes.
- **The extension guard fails closed**: if the server is slow or down, staff cannot send
  anything to Claude. Availability matters more than raw capacity.

## 3. Topology (one host)

```
staff browsers --443--> nginx (TLS, basic auth on UI, body cap, timeouts)
    /api/process, /api/unmask-file  --> app-files  (1 gunicorn worker)
    everything else                 --> app-text   (2 gunicorn workers)
    both share:  Postgres 16 container  +  /data volume (uploads, audit files, .crx)
```

- Three model copies load, about 1 GB each. State is shared through Postgres, which the
  database migration made possible.
- Follow-up needed in the image: make the gunicorn worker count an environment variable
  (`WEB_CONCURRENCY`) instead of the hard-coded `--workers 1`.

## 4. Server specification

| Item | Minimum | Recommended |
|---|---|---|
| Instance | m6i.xlarge (4 vCPU, 16 GB) | m6i.2xlarge (8 vCPU, 32 GB) |
| Why | 3 GB of models, 2 GB Postgres, OCR spikes, OS | Two scanned PDFs at once without starving prompt workers |
| Family | Fixed-performance; not burstable T-series | NLP and OCR are sustained CPU |
| Disk | gp3, 200 GB, encrypted with KMS | Same, plus a separate 200 GB data volume for snapshots |
| Disk reasoning | Audit keeps originals and both file copies for 90 days: ~70 GB at 400 files/day | 50% headroom for 7 MB scanned PDFs |
| Postgres | 16, same host, `shared_buffers` 1 GB, nightly `pg_dump` to the data volume | Same, plus daily EBS snapshots retained 14 days |
| Network | Private subnet; 443 only from office ranges or VPN; no public Postgres port | Same; egress limited to package repos and certificate renewal |
| Availability | EC2 auto-recovery alarm, container restart policies, documented restore drill | Standby AMI with the same compose stack, RTO under one hour |
| Observability | CloudWatch agent (CPU, memory, disk), nginx access log, gunicorn timeout alerts | Add a synthetic check that masks a sample prompt every minute |

## 5. Fintech-specific controls

- Audit records hold raw customer data. Keep `AUDIT_TTL_DAYS` at 90 or lower, restrict admin
  key holders, encrypt the volume. Encryption of values inside the database is still open
  work (ROADMAP item 6) and should be scheduled before onboarding regulated data.
- Run with `MASKROOM_AUTH_MODE=oidc` against the company identity provider (or the bundled
  Keycloak). `PII_TOKEN_SALT`, `MASKROOM_SECRET_KEY` and `MASKROOM_OIDC_CLIENT_SECRET` belong
  in AWS Secrets Manager or SSM Parameter Store, pulled at boot, never in the compose file
  or an image.
- Choose the region deliberately for data residency (no AWS region in Sri Lanka: Mumbai or
  Singapore) and record the choice.

## 6. Scaling knob

If prompt latency at peak exceeds one second, add text workers: each costs about 1 GB of
RAM and nothing else. A second host behind a load balancer is possible for availability,
but Postgres should then move to RDS Multi-AZ rather than remain a container.
