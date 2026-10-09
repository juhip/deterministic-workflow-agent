#!/usr/bin/env python3
"""Policy-bound autonomous procurement agent.

    python3 agent.py --scenario path/to/scenario.sqlite

One run: read the state of the world, work out what is short, choose suppliers under the
policy and memos, double-check the plan, then release routine purchase orders, hold risky
ones for a named approver, and write everything (with alerts) into the same database. No
human prompting is needed. If a model is configured, optional AI checks read part
descriptions and supplier notes and can hold risky orders for review. Existing purchase orders are never changed; a re-run counts
them as supply and only adds what is still missing. Alerts are refreshed on every run.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from procurement.db import load_state  # noqa: E402
from procurement.llm import get_provider, resolve_config  # noqa: E402
from procurement.pipeline import run  # noqa: E402
from procurement.rules import DEFAULT_RULES_PATH, Rulebook  # noqa: E402
from procurement.rule_drafter import unknown_memo_files  # noqa: E402
from procurement.alerts import Alert  # noqa: E402
from procurement.writer import write  # noqa: E402

HERE = Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Policy-bound autonomous procurement agent")
    ap.add_argument("--scenario", required=True, help="path to the scenario .sqlite file")
    ap.add_argument("--rules", default=str(DEFAULT_RULES_PATH), help="rulebook JSON (default: rules/policy_rules.json)")
    ap.add_argument("--dry-run", action="store_true", help="plan and print, but write nothing")
    ap.add_argument("--verbose", action="store_true", help="also print each order's full rationale")
    ap.add_argument("--quiet", action="store_true", help="print nothing")
    ap.add_argument("--policy-dir", default=str(HERE / "data" / "memos"),
                    help="folder of memo files (default data/memos); a memo missing from the rulebook raises an alert")
    ai = ap.add_argument_group("optional AI checks (an open model reads part descriptions and supplier notes)")
    ai.add_argument("--llm-profile", help="a profile from config/model.json, e.g. self-hosted, vllm, ollama; or set LLM_PROFILE")
    ai.add_argument("--llm-base-url", help="OpenAI-compatible server address, e.g. http://localhost:8000/v1")
    ai.add_argument("--llm-model", help="model name as the server knows it")
    ai.add_argument("--llm-api-key", help="only if the server needs one")
    ai.add_argument("--no-llm", action="store_true", help="skip the AI checks even if a model is configured")
    args = ap.parse_args(argv)

    state = load_state(args.scenario)
    rules = Rulebook.load(args.rules)
    cfg = None if args.no_llm else resolve_config(args.llm_base_url, args.llm_model, args.llm_api_key, args.llm_profile)
    result = run(state, rules, get_provider(cfg) if cfg else None,
                 cache_dir=Path(os.environ.get("PROCUREMENT_CACHE_DIR", HERE / "cache")))
    for name in unknown_memo_files(args.policy_dir, rules):
        result.alerts.insert(0, Alert("ACTION", f"New memo {name} is in the policy folder but not in the rulebook, so it was "
                                                "NOT applied to this run. Draft the rule changes with: python3 propose_rules.py "
                                                f"--memo {Path(args.policy_dir) / name}, then have a person review and apply them."))

    if not args.dry_run:
        write(args.scenario, result.pos, result.alerts)
    if not args.quiet:
        report(state, result, args.dry_run, args.verbose)
    return 0


def report(state, result, dry_run: bool, verbose: bool) -> None:
    print(f"Scenario {Path(state.path).name} | date {state.current_date} | {len(state.schedule)} production orders"
          + ("  (DRY RUN - nothing written)" if dry_run else ""))
    print(f"\nPURCHASE ORDERS ({len(result.pos)})")
    for i, p in enumerate(result.pos, 1):
        sup = state.suppliers[p.supplier_id]
        print(f"  AGT-{i:04d} {p.component_id} {state.components[p.component_id].name[:26]:<26} {p.quantity:>5} "
              f"from {sup.name[:22]:<22} ${p.total:>10,.2f}  due {p.expected_delivery_date}{'  AIR' if p.mode == 'air' else ''}")
        if verbose:
            print(f"           {p.rationale}\n")
    print(f"\nALERTS ({len(result.alerts)})")
    for a in result.alerts:
        print(f"  {a.render()}")


if __name__ == "__main__":
    raise SystemExit(main())
