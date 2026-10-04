# ARCHITECTURE.md — AI Worker

A prototype agent that takes a natural-language business task, autonomously completes it on a
real computer (browser + files), then **independently verifies** the result before reporting.

---

## 1. Design thesis

Three decisions drive everything else.

**1. A generic tool loop, not scripted flows.**
The agent has no knowledge of "invoices", "Acme", or any CSS selector. It receives a goal, a
tool schema, and a page snapshot, and decides what to do. Environment knowledge lives in
`environments/`; the goal text lives in the task. This is the only way the same code handles
"latest Globex invoice" and "check if Initech was already entered" without edits.

**2. Snapshot over screenshot.**
The observation the LLM reasons over is a compact accessibility-tree projection with stable
element refs (`e12`), not a raw HTML dump (token explosion, unparseable) and not a screenshot
(an image the model must OCR, brittle, expensive, no refs to act on). Screenshots are kept, but
as *evidence*, not as the reasoning channel. This is the single highest-leverage decision for
reliability.

**3. Belief and proof are separated.**
The agent's own claim is never trusted. `finish()` does not end a run — it triggers an
*independent verifier* that re-reads ground truth through a different path (fresh browser
context, direct DB/API read) and compares against the values the agent claims to have
extracted. A "Bill saved ✓" toast is not evidence. This is what makes eval 8 (fake success
toast) pass instead of silently passing a broken system.

---

## 2. System map

```
                        ┌──────────────────────────────────────────┐
   human ──── task ───▶ │  orchestrator (FastAPI :8000) + SSE      │
                        │  runs: plan · live feed · approvals · UI  │
                        └───────────────────┬──────────────────────┘
                                            │
                        ┌───────────────────▼──────────────────────┐
                        │         agent.loop  (PLAN→ACT→OBS→      │
                        │            REFLECT → VERIFY → REPORT)   │
                        └───┬────────┬────────┬────────┬──────────┘
                            │        │        │        │
              ┌─────────────▼──┐  ┌──▼─────┐ ┌▼───────┐│
              │ agent.planner  │  │memory  │ │ safety ││
              │ agent.recovery │  │(facts+ │ │(policy ││
              │ agent.verifier │  │proven.)│ │enforce)││
              │ agent.trace    │  └────────┘ └────────┘│
              └────────┬───────┘                     │
                       │ agent.tools.*               │
       ┌───────────────┼──────────────┬──────────────┘
       │               │              │
  ┌────▼─────┐   ┌─────▼──────┐  ┌────▼──────────┐
  │ browser  │   │ files      │  │ http / recall │
  │ (Playwright│  │ (sandboxed │  │ remember      │
  │  Chromium)│  │ workspace) │  │ ask_user      │
  └────┬─────┘   └────────────┘  │ request_approv│
       │                         │ finish        │
       │                         └───────────────┘
       │            ┌──────────────────────────────┐
       └───────────▶│ environments/                 │
                    │  vendor_portal  :8001 (FastAPI)│
                    │  ap_system      :8002 (FastAPI)│
                    │  fault injection middleware   │
                    └──────────────────────────────┘
```

---

## 3. Module boundaries

| Module | Owns | Must not know about |
|---|---|---|
| `agent/loop.py` | iteration, budgets, wiring | vendor names, ports, selectors |
| `agent/planner.py` | goal → steps, success criteria, replan | tool internals |
| `agent/memory.py` | fact store w/ provenance + rolling summary | any site |
| `agent/safety.py` | classify + enforce approvals | *how* tools work |
| `agent/recovery.py` | classify failure, choose strategy | tool semantics |
| `agent/verifier.py` | independent re-read + compare | the agent's reasoning |
| `agent/trace.py` | append-only JSONL + SQLite | business logic |
| `agent/tools/` | tool schemas + impls | planning policy |
| `agent/llm.py` | Anthropic client, retry/backoff, fake client | anything else |
| `environments/*` | seed data, HTML, validation, faults | the agent |
| `evals/` | scenarios + ground-truth assertions | agent internals |

Dependency direction is strictly one-way: `evals → loop → tools`. `agent/` never imports
`environments/`.

---

## 4. The loop

Each iteration:

1. **Context assembly** — system prompt + goal + success criteria + current plan + working
   memory (facts) + rolling summary of old steps + last N observations + tool schemas.
   Observations are truncated and de-duplicated by (tool, args) fingerprint.
2. **LLM turn** — Anthropic tool-use. Returns thought text + zero or more tool calls.
3. **Gate** — every tool call passes `safety.classify()` → `read` / `reversible_write` /
   `irreversible_write`, then `policy.evaluate()` against `config/policy.yaml`. Anything that
   needs approval *and* was not granted is **refused by the executor** with a structured error.
   The prompt is not the enforcement point.
4. **Execute** — via `recovery.guard()`, which wraps the call: exception capture, timeout,
   unchanged-state detection, loop detection (same fingerprint 3×), backoff, session-expiry
   detection.
5. **Observe** — compact result + optional snapshot.
6. **Reflect** — append to memory/trace; update plan; detect divergence.
7. **Finish** — only via `finish` tool → verifier gate → pass, or 2 repair cycles, or honest
   failure.

**Budgets (all enforced in code):** max steps, max tokens, wall-clock deadline, max repairs.

---

## 5. Safety model

Classification is per-tool (`ToolSpec.risk`), refined per-call by argument inspection
(e.g. a `browser_click` on a ref whose snapshot text matches `/save|submit|delete|void/i`
upgrades read → irreversible_write). That is the important part: the same `browser_click` is a
read when it opens a detail page and a write when it presses Save.

Rules (YAML-configurable): `amount_over_threshold`, `vendor_ambiguity`,
`duplicate_suspected`, `low_confidence_extraction`, plus blanket `irreversible_write`.

`request_approval` blocks the run on a future; the UI modal answers it; the grant is scoped to
a **fingerprint** of the approved call, so approving one save does not silently approve the next
different one.

---

## 6. Verification

`verifier.py` runs *outside* the agent's browser context on purpose — a new context, no
cookies, re-login — so it cannot be fooled by leftover client state or a stale DOM. For each
success criterion it produces `{criterion, expected, actual, passed, evidence}`.

Ground truth comes from the AP system's own read API (an allowed read path) **and** its
rendered bills page, so a bug in either one is caught.

Failure → return to loop with the discrepancy attached. 2 repair cycles max, then report
`failed`/`partial` honestly. The agent is structurally unable to declare success over a failed
criterion.

---

## 7. Environments

Real FastAPI apps with real HTML, real validation, real SQLite.

**Vendor Portal :8001** — seeded login, vendor list, per-vendor paginated/sortable invoice
list, detail page, PDF download link. 8 vendors / ~40 invoices seeded deterministically
(fixed seed, fixed "today" so `due in next 7 days` is stable across runs). Deliberate traps:
- `Acme Corp` vs `Acme Corporation Ltd` → ambiguity, must `ask_user`
- draft + void invoices with the newest dates → must not be treated as "latest"
- 4 date formats (`2026-03-04`, `04/03/2026`, `Mar 4, 2026`, `03.04.2026`)
- 3 currencies
- one invoice whose **PDF amount differs from the page summary** → cross-check must surface it

**AP System :8002** — seeded login, New Bill form (vendor select, invoice #, amount, currency,
due date, notes), bills list + detail. Validation: required fields, duplicate invoice-number
rejection, due-date format enforcement, >$10,000 manager-approval flag.

**Fault injection** — off by default, toggled by env or `POST /admin/faults`. Five faults:
`session_expiry`, `transient_500_on_submit`, `slow_page`, `flaky_render`, `ui_rename`,
`silent_save_failure` (shows success toast, writes nothing).

**workspace/** — invoice PDFs + a CSV for the file tools, path-sandboxed.

---

## 8. Evals

10 scenarios, `make eval`, each asserting on **both** the SQLite ground truth and the agent's
reported status. Table emitted with pass/fail, steps, retries, tokens, wall time.

The eval harness supplies scripted answers for `ask_user` and scripted decisions for
`request_approval`, so runs are headless and deterministic in *outcome expectations* while the
agent's path is genuinely produced by the LLM.

---

## 9. UI

Single page served by FastAPI. Visual language borrowed from the reference site
(Atelier / atelier-meridian-site.webflow.io): black-on-white, hairline `#000`/`#f3f3f3` borders,
fluid `rem`-based type via `calc(16 * 100vw/1440)`, tight negative tracking on display type,
uppercase underlined link-buttons, `.25rem` radii, mono for identifiers/values. No frameworks —
plain HTML/CSS/JS + SSE, so it cannot break the build.

Panels: task input + examples · plan checklist · action feed · live screenshot · working memory ·
approval modal · final report card · fault toggle · replay trace.

---

## 10. Build order

1. environments + seed (run it, click it)  2. browser tools + snapshot (drive it with code)
3. loop + trace  4. safety  5. recovery  6. verifier  7. UI  8. evals  9. docs

Each stage is *run for real* before moving on.