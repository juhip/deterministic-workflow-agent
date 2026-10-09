# Baseline: cost of compliance

| Scenario | Agent spend | Naive spend | Cost of compliance | Late orders (agent / naive) | Hard-rule breaks in the naive plan |
|---|---|---|---|---|---|
| 01_baseline | $6,643.80 | $6,537.50 | $+106.30 | 1 / 1 | 1 |
| 02_partial_procurement | $5,716.90 | $5,609.90 | $+107.00 | 0 / 0 | 1 |
| 03_tight_timeline | $11,484.00 | $11,060.50 | $+423.50 | 2 / 2 | 2 |
| 04_low_inventory | $15,120.55 | $14,893.55 | $+227.00 | 1 / 1 | 0 |
| 05_competing_demand | $13,898.70 | $13,138.60 | $+760.10 | 1 / 0 | 4 |
| 06_simple | $624.00 | $624.00 | $+0.00 | 0 / 0 | 0 |
| **Total** | $53,487.95 | $51,864.05 | $+1,623.90 | | |

Examples of naive orders the policy forbids:
- scenario_01_baseline: Samarium-Cobalt Magnet Segment: one supplier takes 67% (MEMO-2026-018 caps it at 60%)
- scenario_02_partial_procurement: Samarium-Cobalt Magnet Segment: one supplier takes 100% (MEMO-2026-018 caps it at 60%)
- scenario_03_tight_timeline: Samarium-Cobalt Magnet Segment: one supplier takes 67% (MEMO-2026-018 caps it at 60%)
- scenario_03_tight_timeline: DC Link Capacitor Pack: Brightline Distribution lacks IEC-62368
- scenario_05_competing_demand: Samarium-Cobalt Magnet Segment: one supplier takes 100% (MEMO-2026-018 caps it at 60%)
- scenario_05_competing_demand: Circuit Board Assembly (4-layer): Orrin Board Works is not on the approved list
- scenario_05_competing_demand: Circuit Board Assembly (4-layer): Orrin Board Works has no prior receipt (MEMO-2026-051 §2)
- scenario_05_competing_demand: DC Link Capacitor Pack: Brightline Distribution lacks IEC-62368
