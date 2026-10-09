# Procurement Agent

A policy-bound purchase-order agent. It reads a planning database, works out what each production
order is short of and by when, applies a written purchasing policy and the memos in force on that
date, and writes purchase orders and alerts back into the same database. Routine orders are
released; anything risky is held for a named approver with the reason.

**Design in one line:** the model interprets, code decides, and an independent check verifies.
Semantic interpretation → deterministic decisioning → independent verification → escalation and
execution. An open model (optional) reads what is written in words (part descriptions, supplier
notes, memos). Code makes every purchasing decision. A separate check verifies every order before
it is written. Anything the rules cannot decide goes to a person, with the numbers.

Author: Juhi Parekh.

> **All data in this repository is synthetic.** The company (Harbor Fabrication Co.), its
> suppliers, parts, customers, prices, lead times, purchasing policy and memos are invented for
> this project. Any resemblance to real organizations is coincidental.

**Results on the six sample scenarios:** 94 purchase orders worth $53,487.95; 29 released
automatically and 65 held for approval. 17 of 22 production orders are on time, and none is late
that an allowed supplier could have made on time. 0 failed policy checks on the samples (656
checks), on 12 created scenarios (1,452 checks) and on a 140-scenario stress test (12,994 checks).
The agent passes all 17 cases of an independent golden set, whose expected answers were written
from the policy text outside this code; when 15 policy mistakes were planted on purpose, the golden
set caught all 15 and the policy scorecard, which shares the agent's rulebook, caught 3. 223
automated tests. What these results do not show is under [Evaluation](#evaluation).

---

## Run it

```bash
python3 agent.py --scenario data/scenarios/scenario_05_competing_demand.sqlite
```

That is the whole interface: one argument, no packages to install (standard library only, Python
3.9 or later), no model, no network, and it works from any folder. The same command works on any
scenario file with the same table layout.

On Windows, type `python` (or `py`) wherever this README says `python3`; everything else is the
same, and forward-slash paths work as written.

It writes its purchase orders and alerts into that file, in one transaction, and prints what it
wrote:

```
Scenario scenario_05_competing_demand.sqlite | date 2026-11-09 | 4 production orders

PURCHASE ORDERS (19)
  AGT-0001 PT-106 Motor Controller IC (DSP)     12 from Brightline Distributio $    100.80  due 2026-11-12
  ...
  AGT-0012 PT-103 Samarium-Cobalt Magnet Seg   137 from Ironbridge Magnet Co.  $    712.40  due 2026-11-21
  AGT-0019 PT-103 Samarium-Cobalt Magnet Seg   205 from Halong Magnetics       $    594.50  due 2026-12-09

ALERTS (21)
  [CRITICAL] Cannot meet 2026-12-04 start date for WO-7102 (Ostrander Shipyards, 22 x ASM-220): Samarium-Cobalt
             Magnet Segment (PT-103) arrives 2026-12-09 because the agent followed MEMO-2026-018 (see decision alert). ...
  ...
```

What to expect:

| | |
|---|---|
| Reads | The scenario file, plus the project's own `rules/policy_rules.json` (the policy and memos as dated rules) |
| Writes | New rows in `purchase_orders` (numbered `AGT-0001`, ...) and `alerts`, in one transaction. The existing columns only: whether an order is released or held is the first words of its `rationale` |
| Never | Changes or deletes an existing purchase order |
| Rerun | Safe: every open order, including its own, counts as supply, so a second run buys nothing more; its alerts are refreshed |
| Assumes | The table layout of the sample databases and the same company-wide policy and memos (a new memo is flagged in the alerts) |

Optional flags: `--dry-run` (plan and print, write nothing), `--verbose` (each order's full
rationale), `--quiet`, and `--llm-profile` to add a model (see below).

To grade any scenario against the policy, clause by clause (the agent is run on a copy, so your
file is not changed):

```bash
python3 evals/policy_scorecard.py --scenario <scenario.sqlite> [<more files> ...]
```

## What it writes

The tables keep their existing columns; there is no status column. Each order's rationale starts
with `RELEASED automatically` or `DRAFT, awaiting approval by <approver>`.

A released order (rationale, as stored):

> RELEASED automatically: routine order within the approval guardrails. Buy 80 units of Sealed Cable
> Connector from Larchmont Electronics (VEN-301) at $4.10 = $328.00; ordered 2026-11-09, 6-day lead
> time, expected 2026-11-15. Need: 210 units for WO-7101, WO-7102, WO-7103, WO-7104, minus 130 on
> hand and 0 on open orders, leaves 80 to buy. ... Why this supplier: lowest price among allowed
> suppliers that can deliver on time.

A held order:

> DRAFT, awaiting approval by Sourcing Manager before release to the supplier (because: critical
> part (samarium-cobalt magnet (HF-PUR-100 §4)); a management memo applies to this part
> (MEMO-2026-018); international supplier (Vietnam); supplier rated C, below B; arrives 5 day(s)
> after a customer need-by date; part of a policy-versus-schedule conflict that needs a decision).
> Buy 205 units of Samarium-Cobalt Magnet Segment from Halong Magnetics ...

Alerts:

> [CRITICAL] Cannot meet 2026-12-04 start date for WO-7102 (Ostrander Shipyards, 22 x ASM-220): ...
> Materials complete 2026-12-09, 5 day(s) late. 4 of 22 units can be built by the need-by date.
> Revenue at risk: 18 x TideRunner 90 at $3,860 = $69,480.
>
> [ACTION] Recovery options for WO-7102 (Ostrander Shipyards, 22 x TideRunner 90), 5 day(s) late,
> $69,480 of revenue at risk. Each option was checked by the agent against the policy: (A) Waive
> MEMO-2026-018 (magnet supplier split, 60% cap) for this order ... (B) Extend MEMO-2026-044
> (air-freight authorization) for this order: the order is on time; no change in parts cost ...
> Decision: the Sourcing Manager.
>
> [ACTION] Approval needed from Sourcing Manager for 2 draft order(s) totalling $1,306.90, most
> urgent first. Nothing is sent to these suppliers until approved: ...

Alerts are tagged CRITICAL, WARNING, ACTION or INFO, most severe first. Each run also lists the
assumptions it relied on (for example, that stock goes to the earliest need-by date first).

## How it works

One command runs eight steps in four stages, with no human input:

| Stage | Steps | Modules |
|---|---|---|
| Semantic interpretation | 1 Read the data · 2 Classify parts and apply rule scope | `db.py`, `rules.py`, `part_labels.py`*, `classifier.py` |
| Deterministic decisioning | 3 Calculate material requirements · 4 Choose suppliers | `planner.py`, `options.py`, `decider.py` |
| Independent verification | 5 Validation gate · 6 AI flags risks in suppliers' notes | `validator.py`, `language.py`* |
| Escalation and execution | 7 Release or hold orders · 8 Handle exceptions | `release.py`, `explain.py`, `alerts.py`, `exceptions.py`*, `writer.py` |

\* The only modules that call the model. Code checks every answer, and the model can only add
caution or recommend: it never chooses a supplier, changes a quantity or releases an order.

```
RUNTIME (every planning run)
  agent.py --scenario <file>
     reads: scenario database  +  reviewed rulebook (rules/policy_rules.json)
     runs:  interpret → decide → verify → escalate and execute
     writes: purchase_orders + alerts, one transaction
     model (optional): llm.py → any OpenAI-compatible server

POLICY CONTROL (when a new memo arrives)
  propose_rules.py --memo <file>
     model drafts rule changes → code checks every quote and that the rulebook loads
     → dry run shows which orders would change → a person approves and applies (--apply)

EVALUATION (offline, on copies)
  build checks:   automated tests
  agent evals:    policy scorecard · stress test · independent golden set
  outcome evals:  outcome checks · policy-blind baseline
  (sensitivity runs answer what-if questions about the open settings; they are not pass/fail)
```

**Rules are data, not code.** Every policy and memo rule lives in `rules/policy_rules.json` with
its source quote, its start and end dates and the rule it overrides. No part, supplier, memo
number, date, approver or threshold is written into the agent's code, and a test fails if one is.
That is what lets the same code run on scenarios it has never seen, and lets memos switch on and
off by date.

## Using a model (optional)

The agent runs fully without a model. With one, it also classifies every part by name and
description (so renamed parts still get the right rules), reads supplier notes for risks, and
recommends one of the checked recovery options.

```bash
python3 agent.py --scenario <file> --llm-profile self-hosted
```

Any server that speaks the OpenAI-compatible chat API works (vLLM, SGLang, TGI, Ollama,
llama.cpp, LM Studio, hosted). Choose it in one of these ways, most specific first:
`--llm-base-url` and `--llm-model`; the `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` environment
variables; or a profile from `config/model.json` with `--llm-profile` or `LLM_PROFILE`. The
`self-hosted` profile is a placeholder for any open model served with vLLM or SGLang. Prompts are
plain text files in `prompts/`. There is no orchestration framework or vendor SDK, and a test fails
if one is imported. If the model is missing or returns something unusable, the agent falls back to
its keyword rules and says what it skipped.

## Evaluation

```bash
python3 evals/policy_scorecard.py      # every policy clause, on the samples and 12 created scenarios
python3 evals/stress_test.py           # stress test: 140 varied scenarios built from the samples
python3 evals/baseline.py              # cost of following the policy vs. a policy-blind planner
python3 evals/sensitivity.py           # what changes if each open question is answered differently
python3 evals/outcomes.py              # outcome metrics, and checks that results move the right way
                                       # when a scenario changes (2 known failures, see Limitations)
python3 evals/golden/golden_eval.py    # 17 independent cases, answers written from the policy text
python3 evals/golden/misreading_check.py  # plants 15 policy misreadings: which evaluator notices?
python3 -m pytest                      # 223 tests (needs pytest: pip install pytest)
```

Everything runs on copies, and each copy starts from the scenario as provided (rows from an earlier
`agent.py` run on a sample are removed from the copy), so running the agent on a sample first does
not change any result. Results are written to `docs/`. Set `PROCUREMENT_DATA_DIR` to point the
evaluations at another folder of scenarios.

**What you should see**

| Eval | Result |
|---|---|
| Policy scorecard | 0 failed: 656 checks on the samples, 1,452 on 12 created scenarios |
| Stress test | 0 of 12,994 checks failed on 140 scenarios; no crashes; identical output from identical copies |
| Golden set | 17 of 17 cases passed |
| Misreading check | Of 15 planted misreadings, the golden set caught 15 and the scorecard 3 |
| Outcome checks | 2 known failures, both listed under Limitations |
| Automated tests | 223 passed |

**Tested the way you will run it.** `evals/stress_test.py` builds 140 varied scenarios from the
samples (dates every two weeks from November 2025 to July 2027, four ID formats, demand scaled up
and down, suppliers removed, a part nobody sells, an empty schedule, missing optional data) and runs
the one command on each from an unrelated folder, with no model. Each run takes under a second.

**Results by scenario**

| Scenario | Orders (released / held) | Spend | Production orders on time | Main finding |
|---|---|---|---|---|
| 1 Baseline | 17 (5 / 12) | $6,643.80 | 3 of 4 | Bayline Marine 1 day late: no allowed magnet supplier delivers before October 17 |
| 2 Partial procurement | 15 (4 / 11) | $5,716.90 | 4 of 4 | Lopsided existing magnet orders make the 60% cap impossible; the closest, 65%, is flagged |
| 3 Tight timeline | 21 (6 / 15) | $11,484.00 | 3 of 5 | Pellworth Automation rush order 1 day late (circuit boards, capacitor packs); choke cores flown in under the air-freight memo; Bayline 1 day late |
| 4 Low inventory | 20 (7 / 13) | $15,120.55 | 3 of 4 | Bayline 1 day late, unavoidable |
| 5 Competing demand | 19 (6 / 13) | $13,898.70 | 3 of 4 | Ostrander Shipyards 5 days late under the magnet memo; extending the expired air-freight memo would recover it |
| 6 Simple | 2 (1 / 1) | $624.00 | 1 of 1 | Two orders; the critical level transducers are held for approval |
| **Total** | **94 (29 / 65)** | **$53,487.95** | **17 of 22** | **None late that an allowed supplier could have made on time** |

**Cost of following the policy:** $1,623.90 more (3.1%) than a planner that ignores it, and one
order late. The policy-blind plan breaks 8 hard rules.

**Why so much is held:** of the 65 held orders, 31 have a reason the policy requires a person for
(Strategic-supplier shift, hazmat review, below-B review, air freight). The other 34 are held only by the
agent's caution rules, and those are the ones to relax as it earns trust. The biggest levers are
two caution rules that are not in the policy: holding every critical part, and every part with one
allowed supplier. Both are already rulebook options (`critical_part: "single_sourced_only"`,
`single_allowed_supplier: "critical_only"`). Narrowing them lifts automatic release from 29 to 52
of 94 orders; also setting "significant volume" to orders over $10,000 takes it to 66 of 94. Spend
and late orders don't change (`evals/sensitivity.py`).

**What these results do not show**

- **A misread policy.** The scorecard uses the agent's own rulebook, so it would pass a misreading
  too. The golden set covers this for the clear rules: it caught all 15 planted misreadings, where
  the scorecard caught 3 (`evals/golden/misreading_check.py`).
- **The ambiguous rules.** The golden set was drafted outside this code, not labelled by the
  business. It doesn't cover what proves receipt, borderline critical parts or "significant
  volume"; those need planners to decide.
- **Brand-new data.** The created scenarios and the stress test reuse the samples' parts, suppliers
  and memos. A new kind of part, supplier or memo is untested.
- **The model.** The model steps are tested with a stand-in model only.

## Changing the rules

Business choices are settings in `rules/policy_rules.json`, not code. The main ones:

| Setting | Today | What it controls |
|---|---|---|
| `approval_exceptions.max_order_value` | 5000 | Orders above this wait for a person |
| `approval_exceptions.critical_part` | `true` | Or `"single_sourced_only"` (hold a critical part only with one allowed supplier), or `false` |
| `approval_exceptions.single_allowed_supplier` | `true` | Or `"critical_only"` (what the policy's two-supplier rule covers), or `false` |
| `order_release` | `approve_by_exception` | Or `approve_every_order`, or `policy_thresholds_only` |
| `when_rule_conflicts_with_schedule` | `follow_rule` | Or `protect_schedule` when a memo and a customer date collide |
| `default_approver` / `conflict_decided_by` | Buyer on duty / Sourcing Manager | Who approves and who decides conflicts |
| `strategic_shift_significant_above` | 0 | What counts as a "significant" move away from a Strategic supplier |

When a new memo arrives:

```bash
python3 propose_rules.py --memo data/memos/<new memo> --llm-profile self-hosted --scenario <file>
python3 propose_rules.py --apply proposals/<proposal>.json        # after a person has reviewed it
```

The model drafts the change, code checks that every quote appears in the memo and that the
rulebook still loads, and the dry run shows which orders would change. Nothing changes until a
person applies it. (Reading a PDF memo needs `pypdf`; a `.txt` memo needs nothing.)

## Tradeoffs, assumptions and open questions

Five tensions shaped the design:

- **Predictability vs. flexibility:** code makes every decision and the model only interprets language. Results are exact and repeatable, but a new kind of rule needs a person to update the rulebook.
- **Autonomy vs. control:** start cautious and hold anything risky for a named approver. People keep control, but 65 of 94 sample orders are held today.
- **Compliance vs. schedule:** follow the written rules and escalate conflicts instead of breaking them. Following the policy costs 3.1% more and makes 1 order late (Ostrander Shipyards, 5 days, $69,480 at risk).
- **Strict vs. workable readings of missing data:** take the most workable reasonable reading, flag it, and make it a setting. Circuit boards can still be bought (11 fewer late orders); those orders are held until confirmed.
- **Model help vs. model risk:** the model can only add caution, so a wrong answer can't make an order unsafe, at the price of some unnecessary holds.

Where the policy, memos or data were silent or disagreed, the agent makes a call and says so (each
is a setting):

- **A memo and a customer deadline conflict:** follow the memo and escalate to the Sourcing Manager with checked recovery options.
- **Circuit-board freeze with no receiving history:** a prior order, or a Strategic or Preferred relationship, counts as proof of receipt.
- **Mexico:** domestic, as the policy says, although the database marks the Mexican supplier international.
- **Supplier limits:** the magnet split is enforced on every order; the 12-month 65% and 80% limits are reported, because there is no 12-month history.
- **Emergency orders:** still held, and flagged for retroactive approval.
- **Never buy twice:** every open order counts in full, so a rerun adds only what is missing.

Open questions the settings leave to the business:

1. **Memo vs deadline:** when a policy rule and a customer deadline conflict, which wins?
2. **Release limit and hold relaxations:** is $5,000 the right limit, and should every critical
   part be held? That hold is a caution rule, not in the policy.
3. **Significant strategic suppliers:** what counts as "significant volume" moved away from a
   Strategic supplier?
4. **Proof of receipt for circuit boards:** where is the receiving history the freeze relies on?

## Limitations

- Without a model, parts are matched by keywords, and an unfamiliar part name can be missed.
- Supplier capacity, shipping costs and delivery history are not in the data.
- 34 of 65 holds come only from caution rules the policy does not require; approvals happen
  outside the agent, so it has no record yet of which holds people approve unchanged.
- An open order counts as supply even if it arrives after the need-by date, so the agent never
  buys a duplicate but can accept a delay an allowed supplier could avoid.
- The agent recognizes its own alerts by their `[INFO]` / `[ACTION]` prefix, so a person's alert
  written in that format is replaced on the next run.
- Each run plans from a snapshot; approval time is not added to delivery dates, and recovery
  options cover only what code can check.
- The air-freight memo's $20,000 cap is noted on each request but not totalled across orders or runs.
- The model steps are tested with a stand-in model, not with a production model.

## Project layout

```
agent.py                  the agent: python3 agent.py --scenario <file>
propose_rules.py          drafts rulebook changes from a new memo (a person applies them)
procurement/              the agent's code, one module per step (see "How it works")
rules/policy_rules.json   the policy and memos as dated rules, plus the business settings
prompts/                  prompts for the model, as plain text
config/model.json         model profiles (self-hosted, vllm, sglang, tgi, ollama, ...)
evals/                    scorecard, created scenarios, stress test, baseline, sensitivity, outcomes,
                          and golden/ (an independent golden set and the misreading check)
tests/                    223 tests, including a stand-in model server
data/                     synthetic sample scenarios, policy and memos (never modified)
docs/                     the latest evaluation results
```
