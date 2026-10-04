#!/usr/bin/env python
"""Run one task from the terminal and watch the loop work.

Examples
--------
    python -m aiw ask "Log in to the vendor portal and enter Acme Corp's latest \
invoice into the AP system, flagging anything over $10,000 for manager approval."

    python -m aiw ask --answer "Use the PDF amount (7995)" "..."      # script the human
    python -m aiw ask --headful --trace                                # watch it happen
    python -m aiw ask --approve=false "..."                            # refuse all writes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime import RunConfig, execute, new_run_id  # noqa: E402
from agent.verifier import STATUS_SUCCESS  # noqa: E402
from common.config import get_settings  # noqa: E402
from common.logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("aiw")

C = {
    "run.started": "\033[1m·\033[0m run started",
    "thought": "\033[2m\033[36m?\033[0m",
    "step": "\033[32m→\033[0m",
    "step.note": "\033[2m\033[36m?\033[0m",
    "safety.decision": "\033[33m!\033[0m",
    "failure": "\033[31m✗\033[0m",
    "recovery": "\033[33m↻\033[0m",
    "verify.started": "\033[35m◇\033[0m",
    "verify.finished": "\033[35m◆\033[0m",
    "verify.repair": "\033[35m⟳\033[0m",
    "run.timeout": "\033[31m⏱\033[0m",
    "run.finished": "\033[1m■\033[0m run finished",
}


def make_printer(quiet: bool = False):
    def emit(kind: str, payload: dict) -> None:
        if quiet:
            return
        marker = C.get(kind, "  ")
        if kind == "thought":
            text = payload.get("text", "").replace("\n", " ")
            print(f"{marker} {text[:220]}")
        elif kind == "step":
            args = json.dumps(payload.get("args", {}))[:110]
            print(f"{marker} {payload['index']:>2}. {payload['tool']}({args})")
        elif kind == "safety.decision" and not payload.get("allowed"):
            print(f"{marker} BLOCKED {payload['tool']}: {payload['reason'][:120]}")
        elif kind == "safety.decision":
            print(f"{marker} allowed {payload['tool']} (risk={payload['risk']})")
        elif kind == "failure":
            print(f"{marker} {payload['kind']}: {payload['message'][:120]}")
        elif kind == "recovery":
            print(f"{marker} {payload['strategy']}: {payload.get('detail', '')[:110]}")
        elif kind == "verify.finished":
            print(f"{marker} verification -> \033[1m{payload['status']}\033[0m")
            for c in payload.get("criteria", []):
                mark = "\033[32m✓\033[0m" if c["passed"] else "\033[31m✗\033[0m"
                print(f"    {mark} {c['id']}: {c['description'][:80]}")
                if not c["passed"] and c.get("detail"):
                    print(f"       \033[2m{c['detail'][:150]}\033[0m")
        elif kind == "run.finished":
            print(f"{marker} status=\033[1m{payload['status']}\033[0m "
                  f"steps={len(payload['steps'])} "
                  f"{payload['elapsed_seconds']}s")
        else:
            print(f"{marker} {kind}")

    return emit


async def main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    config = RunConfig(
        goal=args.goal,
        run_id=new_run_id(),
        interaction_mode="strict" if args.strict else ("scripted" if args.batch else "live"),
        headless=not args.headful,
        max_steps=args.max_steps or 0,
        max_repairs=args.max_repairs,
        timeout_seconds=args.timeout,
        auto_approve=args.approve,
        scripted_answers=args.answer or [],
        settings=settings,
    )
    print(f"\n\033[1mGoal\033[0m: {args.goal}")
    print(f"\033[2mrun {config.run_id} · provider={settings.llm_provider} · "
          f"mode={config.interaction_mode} · auto-approve={config.auto_approve}\033[0m\n")

    outcome, handle = await execute(config, emit=make_printer(args.quiet))

    print("\n" + "─" * 72)
    print(f"\033[1mResult\033[0m: {outcome.status}")
    if outcome.summary:
        print(outcome.summary)
    print(f"\033[2magent claimed: {outcome.agent_status or 'nothing'} · "
          f"steps: {len(outcome.steps)} · tokens: "
          f"{outcome.tokens['input']}in/{outcome.tokens['output']}out · "
          f"run dir: {handle.run_dir}\033[0m")

    if outcome.report:
        print(f"verification: \033[1m{outcome.report.status}\033[0m")
        for criterion in outcome.report.results:
            mark = "PASS" if criterion.passed else "FAIL"
            print(f"  [{mark}] {criterion.criterion_id} ({criterion.channel}) "
                  f"{criterion.description[:70]}")

    if args.json:
        print(json.dumps(outcome.to_dict(), indent=2, default=str))

    return 0 if outcome.status == STATUS_SUCCESS else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="aiw", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal", help="the task, in plain language")
    parser.add_argument("--answer", action="append",
                        help="a scripted answer to the next question (repeatable)")
    parser.add_argument("--batch", action="store_true",
                        help="headless: answer from --answer/--approve instead of a human")
    parser.add_argument("--strict", action="store_true",
                        help="headless and refuses every question and every risky write")
    parser.add_argument("--headful", action="store_true", help="show the browser")
    parser.add_argument("--approve", default="true",
                        help="'true' (default) or 'false' to refuse risky writes")
    parser.add_argument("--max-steps", type=int, default=0,
                        help="step budget; 0 uses AGENT_MAX_STEPS")
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--json", action="store_true", help="print the full outcome")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    setup_logging(args.log_level)
    args.approve = str(args.approve).lower() not in {"false", "no", "0"}
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())