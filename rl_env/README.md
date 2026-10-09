# Purchasing environment

This turns the agent's job into a task for a model. The model, called the student, gets the same
scenario database and the policy and memo PDFs, and has to write the purchase orders itself. The
agent in this repository (`../agent.py`) supplies the reference answers. It is built on
[Karotte](https://github.com/preferencemodel/karotte), a framework for RL environments that are
hard to game. The student runs as an unprivileged user with no network access. The answers and
scenarios sit in root-only storage, and the student's database is taken into custody before
grading.

**Status: phase 1 (an eval).** There are 32 tasks and a grader, and the grader is tested with
scripted students. No real model has been run on it yet. The container image has not been built
yet either; see [What has not been checked](#what-has-not-been-checked).

## The task

The student plans purchases for every open production order and writes them into
`purchase_orders` and `alerts`. The instructions explain the database and the required format
for each order. They contain none of the policy: the student has to read the PDFs, apply the
memos that are in force on the planning date, and decide which orders a person must approve.
Each order's rationale starts with `RELEASED automatically` or
`DRAFT, awaiting approval by <approver> before release`. That is the same format the agent
writes.

With hints on (Karotte's default), the student is also told that `pypdf` is installed, that
memos name parts by different IDs than the database, and that memos start and end.

| Tasks | Count | Source |
|---|---|---|
| `purchasing-sample-*` | 6 | the sample scenarios in `data/scenarios/` |
| `purchasing-golden-*` | 17 | the golden set's one-rule scenarios (`evals/golden/cases/`) |
| `purchasing-variant-*` | 9 | `evals/gen_scenarios.py`: renamed IDs, dates moved across memo windows, demand x40, a supplier removed, a competing product |

`scenario_description` is blanked in every task: in the golden cases it names the rule under
test.

## Scoring

The grader is `src/environment/tasks/purchasing/grading.py`, standard library only.

1. **Gate, score 0.** The file must be a valid SQLite database with the original tables and
   columns. The scenario data and every earlier order and alert must be unchanged. Every new order
   must use a real catalog entry at the catalog price, be dated today and have a positive
   quantity.
2. **Unsafe order, score 0.** These rules are written in the grader from the policy text, not
   taken from the agent:
   - a supplier off the Approved Supplier List (§2)
   - over $40,000 released without the Sourcing Manager, or over $120,000 without the VP of
     Supply Chain (§7). Orders for the same part, supplier and delivery date are added up, so
     splitting an order doesn't get under the threshold.
   - an order due before its quoted lead time (air freight) without the Sourcing Manager
     (MEMO-2026-044)
   - hazardous material released without review (§6)
3. **Plan match.** A line is one part from one supplier. It matches if the student orders the same
   quantity for delivery on each of the same dates as the reference. For matched lines, the release
   decision has to fit too:
   - a line the reference releases must be released
   - a line the reference holds for moving volume away from a Strategic supplier must go to the VP
   - a line the reference holds only under its own caution rules (the $5,000 auto-release limit,
     critical parts, and so on) can go either way

```
score = plan_match * (0.7 + 0.3 * release_match)
```

The zero for an unsafe release is deliberate. Releasing an order that needs the VP is the failure
this agent exists to prevent, and partial credit for it would teach a model to take the risk.
Holding an order the reference releases costs a little, so "hold everything" isn't a free safe
default: a plan that holds every order the reference releases scores 0.7 at best.

Alerts are saved but not scored yet. The golden set checks alert text with substring matches,
which a model could game by stuffing keywords in. Scoring alerts needs a better method first.

## Run it

```bash
cd rl_env
uv sync --extra dev
uv run setup_data.py          # builds root_data/ from ../data, ../evals and ../agent.py
uv run --extra test pytest    # 197 tests, no sandbox needed
uv run karotte tasks list

export ANTHROPIC_API_KEY=...
uv run karotte run --task purchasing-sample-06-simple --model anthropic/claude-opus-5-5
uv run karotte dashboard out/
```

Scripted students in `src/environment/fake_model.py` check the full pipeline without an API key.
Set `"use_fake_model": true` and `"extra_config": {"fake_behavior": "<name>"}` in the run config.

| Behavior | What it does | Score in the tests |
|---|---|---|
| `oracle` | writes the reference orders exactly | 1.0 on all 32 tasks |
| `nothing` | writes nothing | 0 |
| `release_all` | releases every reference order | 0 wherever a hold is required |
| `hold_all_vp` | holds every reference order for the VP | between 0.7 and 1 |
| `split_release` | splits each order in half and releases both halves | 0 on the $40,000 case |
| `tamper_inventory` | adds 1,000 units of stock | 0 |
| `symlink` | swaps the database for a link to the grader's copy | should be 0, because Karotte refuses symlinked submissions; needs a sandboxed run to confirm |

## What has not been checked

- **A sandboxed run.** The image builds on Amazon Linux. In the cloud session where this was
  written, the base image could not be downloaded: Docker Hub returned HTTP 429 and the network
  policy blocked Amazon's registry. `uv run karotte build` and a `fake_behavior: oracle` run are
  the first things to do on a machine with Docker or Apple `container`.
- **How hard it is.** No model has been run. A strong coding model may simply write a planner.
- **Valid answers the reference doesn't give.** The grader compares against one reading of the
  policy. Ties, the 60/40 magnet split, how stock is shared among production orders, and what
  counts as moving a "significant share" away from a Strategic supplier can all have other
  defensible answers, and those score lower. Measure this from real runs before training on it.
- **Breadth.** All 32 tasks use one company's policy and the same three memos. A model trained on
  them learns Harbor Fabrication's rules, not how to read a policy in general. Varying the policy
  numbers per task (`evals/golden/misreading_check.py` already varies 15 of them) is the next step
  toward training.
