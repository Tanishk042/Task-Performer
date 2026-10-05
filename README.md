# AI Worker

A browser agent that does a real piece of back-office work — carrying invoices
from a vendor portal into an accounts-payable system — and then **independently
checks that it actually did**.

It stops and asks when two sources disagree. It recovers when the session
expires or a form relabels itself. And when AP shows a green toast while
silently writing nothing, it reports failure rather than success.

The interesting part is not that it clicks buttons. It's that `finish()` is
treated as a *claim*, not a result — see [Verification](#verification).

---

## Run it

```bash
.venv/bin/python scripts/serve.py --reset      # apps + orchestrator + UI
```

Open **http://127.0.0.1:8000**, type a task, press **Start run**.

`--reset` re-seeds both databases so the world is byte-for-byte identical every
time. Add `--visible` to watch the agent's own browser window.

Without `ANTHROPIC_API_KEY` the agent runs on `ScriptedClient`, a deterministic
offline policy engine. It drives the same tools, hits the same approvals and
fails in the same places — it just chooses its steps by rule rather than by
model. Set the key and the real Anthropic client takes over with no other change.

### CLI, if you'd rather not use the UI

```bash
.venv/bin/python scripts/aiw.py "Enter the latest open invoice from Hooli Cloud Services into AP."
.venv/bin/python scripts/aiw.py --max-steps=0 --interactive "Enter every open payable from Globex Industrial into AP."
```

---

## What to try

| Task | What it demonstrates |
| --- | --- |
| Enter the latest open invoice from Hooli Cloud Services into AP. | The happy path, plus an approval because `$14,500 > $10,000` |
| Enter invoice **HL-2260** for Hooli Cloud Services into AP. | A conflict: the page says `7275.00`, the PDF says `7995.00`. The agent stops and asks which is authoritative |
| Enter every open payable from Globex Industrial into AP… | A sweep that skips `HL-2201` because it is already in AP |
| Check whether invoice HL-2291 was already entered in AP. | A read-only task that must **not** create a bill |

Then arm a fault from the left rail and run any of them again:

| Fault | What the agent hits |
| --- | --- |
| `session_expiry` | Bounced to the login page mid-run |
| `transient_500_on_submit` | HTTP 503 on the first submit only |
| `ui_rename` | AP relabels its buttons between plan and act |
| `slow_page` | Multi-second page loads |
| `flaky_render` | The invoice table populates after the snapshot is taken |
| `silent_save_failure` | **AP shows "Bill saved successfully" and writes nothing** |

`silent_save_failure` is the important one. The agent's last action *succeeded*.
The only way to know the task failed is to go and look — which is what the
verifier does.

---

## How it works

```
human ──▶ orchestrator (FastAPI :8000) + SSE
               │
               ▼
        agent.loop   PLAN → ACT → OBSERVE → REFLECT → VERIFY → REPORT
             │
   ┌─────────┼──────────┬──────────┬──────────────┐
   ▼         ▼          ▼          ▼              ▼
planner   memory     safety    verifier      agent.tools.*
                              recovery
   │
   ▼
browser (Playwright) ──▶ environments/  vendor_portal :8001 · ap_system :8002
```

- **Planner** states the success criteria *before* acting, which is what makes
  verification mean something.
- **Memory** stores facts with provenance. "Where did 14500.00 come from?" must
  have an answer, or the verifier can call it a fabrication.
- **Safety** classifies each call by risk. Irreversible writes above the
  threshold need a human; the grant is scoped to one exact fingerprint, so
  approving a `$14,500` submit does not approve the next one.
- **Recovery** classifies a failure and picks a strategy — re-login, backoff,
  re-read the DOM for a renamed control, escalate.
- **Trace** appends every thought, call, observation, screenshot and refusal to
  `runs/<run_id>/trace.jsonl`.

### Verification

`finish()` is a claim. The verifier throws it away and goes to the source:

1. Re-read the **vendor portal** database — is that invoice really open?
2. Re-read the **PDF** the agent downloaded and compare the amount.
3. Re-read the **AP** database — exactly one bill, right amount, right due date,
   correctly flagged.
4. Re-open AP **in a fresh browser** and confirm a person would see the bill.

That last check is why `silent_save_failure` fails correctly: three of four
checks would pass if the agent lied convincingly.

Each criterion carries its own evidence, and the UI shows it —
`ap.db:bills[HL-2291]#8`, `HL-2291: portal page=14500.00 pdf=14500.00`.

### Human in the loop

When the agent hits a contradiction or needs approval it **parks**. The loop
suspends on a future that the UI's answer resolves — the run keeps its place,
no re-planning, no lost context. Time spent waiting on a human is credited back
against the agent's deadline, because the budget exists to bound the *agent's*
work, not to punish a slow reader.

Two defaults, both fail-safe:

- an approval that times out is **denied**;
- a question that times out answers **empty**, so the agent abstains instead of
  inventing a value.

---

## Tests

```bash
.venv/bin/python -m pytest tests -q          # 56 unit tests, ~0.8s
.venv/bin/python evals/run_eval.py           # 10 browser scenarios, ~4min
```

The evals assert against the **databases**, not the agent's summary — a run that
claims success and writes nothing is exactly the case worth catching.

```
pass  happy_path               verified
pass  denied_approval          failed     <- correct: a refusal must stop the write
pass  explicit_invoice         verified
pass  skip_duplicates          verified
pass  session_expiry           verified
pass  transient_500_on_submit  verified
pass  ui_rename                verified
pass  slow_page                verified
pass  flaky_render             verified
pass  silent_save_failure      failed     <- correct: it must not pass

10/10 scenarios passed
```

Two of those are expected to end `failed`, and that is the point:

- **`silent_save_failure`** — AP shows "Bill saved successfully" and writes nothing.
- **`denied_approval`** — a human clicked Deny. Nothing may be written, and the
  run must report the refusal rather than the work as done.

`evals/manual_ui.py` is a separate, non-automated check that clicks Deny in a
real browser and confirms the database is untouched.

---

## Deploy

**This cannot run on Vercel or any serverless platform**, and it is worth being
explicit about why, because the UI is static and looks deployable:

- **No Chromium.** Playwright's browser is ~150–400 MB and needs system libraries
  (`libnss3`, `libatk`, `libgbm`) that serverless sandboxes don't provide. You can
  install the Python package; you cannot install the browser.
- **Read-only filesystem.** `data/*.db`, `runs/` and `workspace/` all live on
  local disk, and SQLite needs real random-access writes.
- **A run outlives the response.** A run parks for up to
  `QUESTION_TIMEOUT_SECONDS` (900 s) waiting for a human to approve a write,
  while the browser holds an SSE connection open throughout. That is the core
  of how this demo works, and it is the one thing serverless cannot do.

It is already shaped like an ordinary web service, so it wants a **container with
a real disk and long-running processes**.

### Free: Oracle Cloud Always Free (recommended)

Oracle's Ampere **A1** shape is free indefinitely and gives you 4 OCPU / 24 GB of
ARM — far more than the ~1.5 GB this needs. `deploy/` has the lot.

```bash
# 1. Create a VM.Standard.A1.Flex instance (aarch64) with a public IP.
#    Open TCP 80/443 in the VCN security list.
# 2. Point an A record for your domain at it. Caddy cannot get a certificate
#    until this resolves.
git clone https://github.com/Tanishk042/Task-Performer.git && cd Task-Performer
sudo bash deploy/oracle_setup.sh     # Docker, firewall, password hash
nano deploy/.env                     # at minimum DOMAIN
bash deploy/deploy.sh                # build, start, wait for health
```

| | |
| --- | --- |
| `deploy/docker-compose.yml` | The agent plus Caddy; named volume for state |
| `deploy/Caddyfile` | Automatic TLS, and `flush_interval -1` so SSE is not buffered |
| `deploy/oracle_setup.sh` | Idempotent VM prep |
| `deploy/deploy.sh` | Validates config, builds, waits for health, prints the URL |

**Authentication is mandatory and fails closed.** This app has no auth of its own,
so anyone who reaches it can start agent runs and — with the real Anthropic
provider — spend your tokens. Caddy refuses to start if `CADDY_USER` or
`CADDY_HASH` is empty rather than serving the agent openly, and the
orchestrator is bound to `127.0.0.1` so Caddy is the only way in.

arm64 is not an obstacle: `Dockerfile` derives the Playwright tag from
`TARGETARCH`, so one file builds correctly for Oracle's ARM and for amd64
elsewhere.

Two traps encoded in the config:

- **Never let the machine stop.** Run state is in-process. `suspend` freezes a
  run mid-approval; `stop` discards it and only SQLite survives.
- **One machine only.** Scaling out gives every replica its own databases and its
  own copy of the run, so a browser could start a run on one and poll another.

Nothing passes `--reset` in production: the apps call `bootstrap()` on startup,
which is idempotent — a fresh volume seeds itself, later boots leave it alone.
The deploy scripts refuse a `RESET=true` in `.env` for exactly that reason.

Full instructions and the OCI gotchas (ARM capacity is often out of stock) are in
[`deploy/README.md`](deploy/README.md).

### Paid: Fly.io

```bash
fly launch --no-deploy --copy-config
fly volumes create aiworker_state --size 1
fly secrets set ANTHROPIC_API_KEY=...
fly deploy
```

Same Dockerfile. `fly.toml` sets `auto_stop_machines = "off"` and pins one
machine — the same "don't stop the machine" rule as above, which is why Fly's
default `suspend` would quietly break parked runs. Needs a 2 GB VM; the 256 MB
default will not hold Chromium.

### Other hosts that work

`Railway`, `Render` and a plain VPS + systemd + Caddy all run the same image. Only
the volume and health-check config changes. Avoid Render's *free* tier: 512 MB is
too small and it sleeps after 15 minutes.

---

| Path | |
| --- | --- |
| `agent/loop.py` | PLAN→ACT→OBSERVE→REFLECT→VERIFY→REPORT |
| `agent/ground_truth.py` | Independent portal/AP/PDF/browser checks |
| `agent/safety.py` | Risk classification, fingerprint-scoped approval |
| `agent/recovery.py` | Failure classification → strategy |
| `agent/llm_scripted.py` | Deterministic offline policy engine |
| `orchestrator/app.py` | FastAPI: start, stream, answer, cancel |
| `orchestrator/runs.py` | Run lifecycle, pause/resume, persistence |
| `orchestrator/events.py` | Replayable event fan-out for SSE |
| `ui/` | One page, no framework, no build step |
| `environments/` | The two mock apps + fault injection |
| `scripts/serve.py` | Start everything at once |
| `Dockerfile` | Multi-arch container image (amd64 + arm64) |
| `fly.toml` | Fly.io deploy |
| `deploy/` | Oracle Always Free deploy: compose, Caddy, scripts |

`ARCHITECTURE.md` has the full design and the reasoning behind it.