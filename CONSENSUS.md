# Consensus design: how `settle()` reaches agreement

`settle(policy_id)` is the only method that touches the outside world. It uses a
custom leader/validator pair through `gl.vm.run_nondet(leader_fn, validator_fn)`.
In the pinned SDK (`py-genlayer:1jb45aa8…`, GenVM v0.2.x) this is the safe variant:
the validator runs in a sandbox. In the v0.3 SDK it is renamed `gl.vm.run_nondet_default`.

```
          leader                                   each validator
 ┌────────────────────────────┐          ┌──────────────────────────────────────┐
 │ GET Open-Meteo archive     │          │ GET Open-Meteo archive  (own fetch)  │
 │ GET NASA POWER daily       │          │ GET NASA POWER daily    (own fetch)  │
 │ parse -> per-source summary│          │ parse -> own evidence                │
 │ _decide() -> decision      │ evidence │ 1. identity fields exact             │
 │ return evidence dict  ─────┼─────────►│ 2. per-source status/days exact,     │
 └────────────────────────────┘          │    total & max within tolerance      │
                                          │ 3. _decide(leader numbers) == leader │
                                          │ 4. own decision == leader decision   │
                                          │ -> agree / disagree                  │
                                          └──────────────────────────────────────┘
 after consensus (deterministic, every node):
   re-check identity vs stored policy, recompute _decide(), then move money (integers only)
```

## What the leader returns (the evidence)

```json
{
  "policy_id": "1", "lat_micro": -7327400, "lon_micro": 108220700,
  "start_date": "2023-08-01", "end_date": "2023-08-31", "expected_days": 31,
  "sources": [
    {"name": "open-meteo", "status": "ok", "days": 31, "total_mm100": 4840, "max_mm100": 880},
    {"name": "nasa-power", "status": "ok", "days": 31, "total_mm100": 1399, "max_mm100": 508}
  ],
  "sources_ok": 2, "sources_used": ["open-meteo", "nasa-power"],
  "drought": "TRIGGERED", "flood": "DISABLED",
  "decided_total_mm100": 3119, "decided_max_mm100": -1,
  "decision": "PAYOUT", "defer_reason": "", "payout_wei": 1000000000000000000
}
```

(This exact object came back from a live Studionet run; see README, "Live consensus check".)

## The equivalence check (`_evidence_matches`)

| # | Check | Fields | Rule |
|---|---|---|---|
| 1 | Identity | `policy_id`, `lat_micro`, `lon_micro`, `start_date`, `end_date`, `expected_days` | exact |
| 2 | Source list | number of sources, order, `name` | exact |
| 3 | Source status | `status` (`ok` / `incomplete` / `unavailable`) | exact |
| 4 | Coverage | `days` observed per source | exact |
| 5 | Observations | `total_mm100`, `max_mm100` per source | \|a−b\| ≤ max(2.00 mm, 5 % of the larger value); must be a non-negative int |
| 6 | Leader honesty | `sources_ok`, `sources_used`, `drought`, `flood`, `decided_total_mm100`, `decided_max_mm100`, `decision`, `defer_reason`, `payout_wei` | exact, compared with `_decide()` re-run on the **leader's own** numbers |
| 7 | Independent verdict | `sources_ok`, `sources_used`, `drought`, `flood`, `decision`, `defer_reason`, `payout_wei` | exact, compared with the validator's **own** evidence |
| 8 | Leader error | the leader returned an error instead of evidence | disagree |

Check 6 stops a leader from reporting true numbers with a false verdict. Check 7
stops a leader from shifting numbers inside the tolerance band so that the verdict
flips (for example, nudging a total across a trigger). Check 5 stops it from
inventing data outright. `payout_wei` is exact: it must equal the policy's stored
payout for `PAYOUT` and 0 otherwise.

A validator also has to agree on `DEFER`: if the leader claims a source is down
but the validator can read it, the leader's evidence is rejected (tested). This
stops a leader from stalling a payout.

**Edge of tolerance.** If a data provider revises a value between the leader's
fetch and a validator's fetch and the change crosses a trigger, check 7 fails and
the round rotates to a new leader. That is the intended behaviour: when the
outcome itself is in doubt, we would rather not settle in that round.

## Source-disagreement rules (`_decide`, deterministic)

The two products disagree a lot day by day. Open-Meteo/ERA5 is a ~9 km grid
reanalysis and NASA POWER/MERRA-2 is a ~50 km grid, so the rules are explicit:

1. **Both sources must be complete** (`status == ok`: every date in the window
   present, no `null`, no `-999` fill). If not, every enabled trigger is
   `UNDECIDED` and the decision is `DEFER` with reason `SOURCE_MISSING`.
2. **Per trigger.** Drought checks the cumulative total `< X`; flood checks the
   max daily value `> Y`. Both comparisons are strict.
   * Both sources give the same answer → that answer.
   * They give different answers, but \|a−b\| ≤ max(10.00 mm, 30 % of the larger
     value) → decide on the integer mean `(a+b)//2`.
   * They give different answers and the gap is beyond that → `UNDECIDED`, reason `SOURCES_DIVERGE`.
3. **Combine.** Any trigger `TRIGGERED` → `PAYOUT` of the full insured amount.
   All enabled triggers `NOT_TRIGGERED` → `NO_PAYOUT`. Otherwise → `DEFER`.
4. **DEFER moves no money.** The policy stays `ACTIVE`, the reserve stays locked,
   the evidence and attempt count are stored, and anyone can retry until the
   claim deadline. After that, `expire()` refunds the premium.

Real examples covered by tests (fixtures captured from the live APIs):

| Window / trigger | Open-Meteo | NASA POWER | Outcome |
|---|---|---|---|
| Aug 2023, drought < 60 mm | 48.40 mm | 13.99 mm | both trigger → PAYOUT |
| Jan 2025, drought < 60 mm | 334.30 mm | 412.46 mm | neither → NO_PAYOUT |
| Jan 2025, drought < 400 mm | 334.30 (yes) | 412.46 (no) | gap 78 mm ≤ 30 % → mean 373.38 → PAYOUT |
| Jan 2025, drought < 350 mm | 334.30 (yes) | 412.46 (no) | mean 373.38 → NO_PAYOUT |
| Jan 2025, flood > 50 mm/day | max 31.50 (no) | max 80.55 (yes) | gap 49 mm > 30 % → DEFER (SOURCES_DIVERGE) |
| Jan 2025, flood > 25 mm/day | 31.50 (yes) | 80.55 (yes) | PAYOUT |
| 10–22 Oct 2026 | complete | 2 days `-999` | DEFER (SOURCE_MISSING) |

## Why the post-consensus re-check

After `run_nondet` returns, the contract runs deterministically on every node. It
checks again that the evidence identity matches the stored policy, then recomputes
`_decide()` from the evidence numbers before moving any GEN. The money path never
trusts a string verdict on its own.
