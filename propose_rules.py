#!/usr/bin/env python3
"""AI rule drafter: propose rulebook changes for a new management memo.

    python3 propose_rules.py --memo data/memos/memo_2026-11-02_new.pdf --llm-profile self-hosted
    python3 propose_rules.py --memo new_memo.txt --scenario ../data/scenarios/scenario_05_competing_demand.sqlite
    python3 propose_rules.py --memo new_memo.txt --apply proposals/MEMO-2026-090.json   (after a person reviews it)

The open model drafts; code checks every change; nothing changes until a person runs --apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from procurement.db import load_state  # noqa: E402
from procurement.llm import LLMError, get_provider, resolve_config  # noqa: E402
from procurement.rule_drafter import ask_model, check, describe, impact, read_memo  # noqa: E402
from procurement.rules import DEFAULT_RULES_PATH, Rulebook  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draft rulebook changes for a new memo (a person approves them)")
    ap.add_argument("--memo", help="the new memo (.pdf, .txt or .md)")
    ap.add_argument("--rules", default=str(DEFAULT_RULES_PATH))
    ap.add_argument("--scenario", help="optional: show how this scenario's plan would change")
    ap.add_argument("--out-dir", default=str(HERE / "proposals"), help="where the proposal file is saved")
    ap.add_argument("--apply", metavar="PROPOSAL", help="apply a reviewed proposal file to the rulebook")
    ap.add_argument("--llm-base-url"), ap.add_argument("--llm-model"), ap.add_argument("--llm-api-key")
    ap.add_argument("--llm-profile", help="a profile from config/model.json, e.g. self-hosted")
    args = ap.parse_args(argv)

    if args.apply:
        proposal = json.loads(Path(args.apply).read_text())
        new = proposal["new_rulebook"]
        new["status"] = "human-reviewed"
        Rulebook(new)                                          # refuse to write a rulebook that won't load
        Path(args.rules).write_text(json.dumps(new, indent=2))
        print(f"Applied {Path(args.apply).name} to {args.rules}.")
        return 0

    if not args.memo:
        ap.error("--memo is required (or --apply a reviewed proposal)")
    cfg = resolve_config(args.llm_base_url, args.llm_model, args.llm_api_key, args.llm_profile)
    if cfg is None:
        print("No model configured. Set --llm-profile (see config/model.json) or LLM_BASE_URL and LLM_MODEL.")
        return 2
    rules = Rulebook.load(args.rules)
    memo = read_memo(args.memo)
    try:
        reply = ask_model(get_provider(cfg), memo, rules)
    except LLMError as e:
        print(f"The model could not draft a proposal: {e}")
        return 1
    prop = check(reply, memo, rules)
    print(describe(prop))
    if prop.new_rulebook and args.scenario:
        print()
        print(impact(load_state, args.scenario, rules, Rulebook(prop.new_rulebook)))
    if prop.new_rulebook:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{prop.document['id']}.json"
        path.write_text(json.dumps({"memo": str(args.memo), "model": cfg.model, "document": prop.document,
                                    "summary": prop.summary, "accepted": prop.accepted,
                                    "rejected": [{"change": c, "why": w} for c, w in prop.rejected],
                                    "questions": prop.questions, "new_rulebook": prop.new_rulebook}, indent=2))
        print(f"\nSaved {path}. After review, apply with: python3 propose_rules.py --apply {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
