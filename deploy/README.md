# Deploying to Oracle Cloud Always Free

The free tier that can actually run this. Oracle's Ampere **A1** shape gives you
4 OCPU / 24 GB of ARM for free, indefinitely, which is more than enough for
Chromium plus three Python servers.

## Why not the free tiers on the PaaS providers

Almost all of them give you 512 MB of RAM, and Chromium plus the orchestrator and
the two mock apps needs roughly 1.5 GB. Some also stop the machine when idle,
which kills a run parked waiting for a human. See the table in the root README.

## Why this needs a real VM at all

A run parks for up to `QUESTION_TIMEOUT_SECONDS` (900 s) waiting for a human to
approve a write, while the browser holds an SSE connection open the whole time.
Serverless freezes the process the moment the response is sent, so it cannot host
this. The UI being static makes the app *look* deployable to Vercel; it isn't.

---

## 1. Create the VM

In the OCI console:

1. **Create instance** → shape **VM.Standard.A1.Flex**, `2 OCPU` / `12 GB`
   (the free allowance is 4/24; leave headroom or the shape may be unavailable)
2. Image: **Oracle-Linux 9.23** or **Ubuntu 24.04**, either aarch64
3. Create a **public IP** for it
4. Add ingress rules to the VCN security list for **TCP 80**, **TCP 443** and
   **TCP 443/UDP** from `0.0.0.0/0`

> **Out of host capacity is normal here.** ARM capacity in busy regions sells out
> quickly and OCI's error message is unhelpful. Retry, pick a quieter region
> (e.g. `uk-hamina-1`, `ap-mumbai-1`), or drop to 1 OCPU.

## 2. Point DNS at it

```bash
curl -4 ifconfig.me     # on the VM, or from anywhere
```

Create an `A` record for your domain pointing at that address. Do this **before**
deploying — Caddy retries forever without a resolvable name and the site stays on
the HTTP redirect.

## 3. Prepare the VM

```bash
git clone https://github.com/Tanishk042/Task-Performer.git
cd Task-Performer
sudo bash deploy/oracle_setup.sh
```

Installs Docker, opens 80/443, and writes a bcrypt hash for the web password into
`deploy/.env`. Safe to re-run.

## 4. Configure

```bash
nano deploy/.env        # at minimum: DOMAIN
```

| Variable | |
| --- | --- |
| `DOMAIN` | Must already resolve to the VM |
| `ACME_EMAIL` | Where Let's Encrypt sends expiry warnings — set it |
| `CADDY_USER` / `CADDY_HASH` | Written for you by step 3 |
| `LLM_PROVIDER` | `scripted` by default. Set `anthropic` + `ANTHROPIC_API_KEY` for the real model |

## 5. Deploy

```bash
bash deploy/deploy.sh
```

Builds, starts, waits for the health check, and prints the URL.

Redeploys are non-destructive — the databases live in a named Docker volume and
the apps seed themselves only when the volume is empty.

```bash
docker compose -f deploy/docker-compose.yml logs -f aiworker   # follow
docker compose -f deploy/docker-compose.yml restart aiworker   # bounce
```

---

## Security, honestly

**This app has no authentication of its own.** Anyone who reaches it can start
agent runs, read the AP database, and — if you switch to the real Anthropic
provider — spend your API tokens. The seeded portal and AP logins
(`buyer@northwind.example` / `portal-demo-2026`) are deliberately public because
they gate a mock environment.

Caddy's basic auth is therefore **mandatory and fails closed**: if `CADDY_USER`
or `CADDY_HASH` is empty the proxy refuses to start rather than serving the agent
openly. Do not remove the `basic_auth` block from `deploy/Caddyfile` on a public
IP.

The orchestrator is bound to `127.0.0.1:8080` inside the host, so Caddy is the
only way in.

## Notes

- **arm64, not a problem.** `Dockerfile` derives the Playwright image tag from
  `TARGETARCH`, so the same file builds the arm64 image here and amd64 on a
  laptop or Fly machine.
- **`docker-compose.yml` never resets.** The apps call `bootstrap()` on startup,
  which is idempotent — a fresh volume seeds itself and later boots leave it
  alone. Passing `--reset` would wipe every bill on each restart, which is why
  the deploy scripts refuse a `RESET=true` in `.env`.
- **One machine.** Run state is in-process. Scaling out would give every replica
  its own SQLite files and its own copy of the run, so a browser could start a
  run on one and poll another and find nothing.