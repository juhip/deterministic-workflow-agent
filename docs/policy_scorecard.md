# Policy scorecard

| Rubric | Source | Rule | Checks on the 6 scenarios | Failed |
|---|---|---|---|---|
| Compliant | Policy §2 | Every supplier is on the Approved Supplier List | 94 | 0 |
| Compliant | Policy §2.1 | Supplier holds every required certificate (ISO-9001; IEC-62368 for power parts) | 94 | 0 |
| Compliant | Policy §3 | International orders state a justification, and it is true | 7 | 0 |
| Compliant | MEMO-2026-018 | Magnets: no supplier above 60%, second supplier at least 25%, or the conflict is escalated | 5 | 0 |
| Compliant | Policy §5 | Single-source parts carry a Sole Source Justification | not triggered | 0 |
| Compliant | Policy §5.1 | Quantity is a whole number at or above the supplier's minimum | 94 | 0 |
| Compliant | Policy §6 | Hazardous orders are held for procurement review and flagged for Hazmat Storage | 10 | 0 |
| Compliant | Policy §7 | Orders over $40,000 / $120,000 go to the Sourcing Manager / VP | not triggered | 0 |
| Compliant | Policy §8 | Suppliers rated below B only when needed, and held for review | 4 | 0 |
| Compliant | Policy §9 | Moving volume from a Strategic supplier that could deliver goes to the VP | 16 | 0 |
| Compliant | Policy §10 | Delivery date = order date + quoted lead time | 94 | 0 |
| Compliant | MEMO-2026-044 | Air freight only in its window, international only, Sourcing Manager approves | 1 | 0 |
| Compliant | MEMO-2026-051 | Circuit boards only from previously used suppliers; Certificate of Conformance flagged | 5 | 0 |
| Compliant | Spec / catalog | Unit price is the catalog price; order date is the scenario date | 94 | 0 |
| Effective | Policy §10 | No order is late if an allowed supplier could have made the date (unless a rule forced it and it is escalated) | 6 | 0 |
| Effective | Policy §7 | A cheaper allowed on-time supplier is passed over only with a stated policy reason | 16 | 0 |
| Honest | Spec | Every late or short production order has an alert with the days late | 5 | 0 |
| Effective | Spec | Every late order comes with recovery options checked against the policy | 5 | 0 |
| Honest | Policy (effective date) | A scenario dated before the policy took effect is flagged, and the policy still applies | not triggered | 0 |
| Honest | Spec, Policy §3 | Every order has a rationale and says whether it was released or who must approve | 94 | 0 |
| Reliable | Spec | A second run buys nothing more | 6 | 0 |
| Reliable | Spec | Pre-existing purchase orders are unchanged | 6 | 0 |
| Generalizes | Spec | All checks above, on 3 samples moved to other dates (before any memo; air-freight window open, before the circuit-board freeze; air freight in its last days, freeze active) | 416 | 0 |
| Generalizes | Spec | All checks above, on 6 generated scenarios with new ID formats (JOB-, ITM-, VN-) and new dates (new ID formats only; before the magnet memo and the air-freight window; air freight open, before the circuit-board freeze; air freight in its last days, freeze active, existing orders; before the procurement policy took effect; well after every memo window) | 631 | 0 |
| Generalizes | Spec | All checks above, on 3 scenarios with a changed structure (demand x40: orders cross the $40,000 and $120,000 approval levels; the approved supplier with the widest catalog removed; a new product and customer order compete for the same parts) | 405 | 0 |

No checks failed.

## Useful: how many holds the policy actually requires (informational, not pass/fail)

The checks above guard against unsafe releases. This guards against the opposite error, over-caution.

| Measure | Orders |
|---|---|
| Released automatically | 29 of 94 (31%) |
| Held, with at least one reason the policy requires a person for | 31 |
| Held only for the agent's caution rules (candidates to relax, with evidence) | 34 |

| Hold reason | Held orders citing it | Required by |
|---|---|---|
| moves volume away from a Strategic supplier | 16 | Policy §9: VP approval to shift volume from a Strategic supplier |
| hazardous material | 10 | Policy §6: procurement review |
| supplier rated below B | 4 | Policy §8: additional review |
| air freight | 1 | MEMO-2026-044: Sourcing Manager approves each request |
| critical part | 38 | caution rule (not in the policy) |
| a memo covers the part | 14 | caution rule (not in the policy) |
| only one allowed supplier | 8 | caution rule (not in the policy) |
| international supplier | 7 | caution rule (not in the policy) |
| arrives after a need-by date | 6 | caution rule (not in the policy) |
| policy-vs-deadline conflict | 3 | caution rule (not in the policy) |
