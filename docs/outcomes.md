# Outcome checks

## Outcome metrics on the six samples

| Outcome | Metric | Result |
|---|---|---|
| Customers on time | Production orders with every part by the need-by date | 17 of 22 |
| Avoidable lateness | Late orders where an allowed supplier could have made the date, with no rule forcing it | 0 |
| Cost of the policy | Spend above the cheapest allowed on-time supplier for each part | $503.70 (0.9% of $53,487.95), on 6 parts |
| Over-ordering | Units bought beyond need (supplier minimums, magnet split) | 96 units, $698.10 |
| Planner workload | Released automatically; held orders the policy requires vs caution only | 29 of 94 (31%); 31 vs 34 |

Why each late production order is late:

| Scenario | Customer | Days late | Why |
|---|---|---|---|
| scenario_01_baseline | Bayline Marine | 1 | unavoidable: no allowed supplier is fast enough |
| scenario_03_tight_timeline | Pellworth Automation | 1 | unavoidable: no allowed supplier is fast enough |
| scenario_03_tight_timeline | Bayline Marine | 1 | unavoidable: no allowed supplier is fast enough |
| scenario_04_low_inventory | Bayline Marine | 1 | unavoidable: no allowed supplier is fast enough |
| scenario_05_competing_demand | Ostrander Shipyards | 5 | a rule forced it, and it was escalated |

## Checks under change

Each check changes the samples in a known way and checks the result moves the right way. No answer key is needed.

| Check | Cases | Failed | Known limitation |
|---|---|---|---|
| More stock never means more orders | 86 | pass |  |
| More time never makes an order late | 17 | pass |  |
| Renaming IDs never changes a decision | 6 | pass |  |
| An existing order arriving soon never makes production later | 86 | pass |  |
| An existing order arriving late never makes production later | 86 | **86** | README Limitations: late existing orders count as supply |
| A run never removes alerts it did not write | 12 | **6** | README Limitations: the agent recognizes its own alerts by their [INFO] / [ACTION] prefix |

Examples of failures:

- An existing order arriving late never makes production later: scenario_01_baseline: an existing order for PT-102 arriving 2026-12-13 took lateness from 1 order(s) / 1 day(s) to 2 / 31
- An existing order arriving late never makes production later: scenario_01_baseline: an existing order for PT-103 arriving 2026-12-13 took lateness from 1 order(s) / 1 day(s) to 2 / 88
- An existing order arriving late never makes production later: scenario_01_baseline: an existing order for PT-104 arriving 2026-12-13 took lateness from 1 order(s) / 1 day(s) to 2 / 88
- An existing order arriving late never makes production later: scenario_01_baseline: an existing order for PT-105 arriving 2026-12-13 took lateness from 1 order(s) / 1 day(s) to 4 / 136
- ... and 82 more
- A run never removes alerts it did not write: scenario_01_baseline: the person's alert "[INFO] Human-entered follow-up: call customer." was removed
- A run never removes alerts it did not write: scenario_02_partial_procurement: the person's alert "[INFO] Human-entered follow-up: call customer." was removed
- A run never removes alerts it did not write: scenario_03_tight_timeline: the person's alert "[INFO] Human-entered follow-up: call customer." was removed
- A run never removes alerts it did not write: scenario_04_low_inventory: the person's alert "[INFO] Human-entered follow-up: call customer." was removed
- ... and 2 more
