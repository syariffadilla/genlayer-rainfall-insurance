# How PadiGuard reaches consensus

`contracts/padi_guard.py` has two methods that read the outside world. Both use a
custom leader/validator pair through `gl.vm.run_nondet(leader_fn, validator_fn)`
(py-genlayer `1jb45aa8…`, GenVM v0.2.x; renamed `gl.vm.run_nondet_default` in v0.3).

| Method | What validators fetch | What they must agree on | What it decides |
|---|---|---|---|
| `request_quote` (new in PadiGuard) | Open-Meteo ERA5 archive: daily precipitation for the same calendar window in each of the last 5 years (one request) | per-year day counts, totals and wettest day (within tolerance), per-year "would have paid" flags and the hit count (exact) | the premium rate |
| `settle` (from the earlier RainfallInsurance contract, unchanged) | Open-Meteo ERA5 archive **and** NASA POWER daily point API for the coverage window | per-source coverage, totals and wettest day (within tolerance), the trigger outcome and the exact payout | whether GEN moves |

In both cases the leader returns a structured evidence dict, every validator
re-fetches the data itself and compares, and after consensus every node
re-checks the evidence deterministically before anything is stored or paid.

---

## 1. Quote consensus (`request_quote`)

### Which years

`_history_windows(start, end, today)` is deterministic and runs **before** the
non-deterministic block, so leader and validators use the same list:

* the coverage window shifted back 1, 2, 3 … years;
* a year is used only if its shifted window ended at least 7 days before the
  transaction date (ERA5 latency);
* the first 5 usable years are taken, newest first (`QUOTE_YEARS = 5`);
* 29 February maps to 28 February in non-leap years.

All five windows are covered by **one** Open-Meteo request from the earliest
start to the latest end (`get_quote_source_url(quote_id)` returns it).

### Evidence

```json
{
  "lat_micro": -7327400, "lon_micro": 108220700,
  "windows": [["2025-11-01","2025-11-30"], ["2024-11-01","2024-11-30"], ...],
  "source": "open-meteo", "status": "ok",
  "years": [{"start_date":"2025-11-01","end_date":"2025-11-30","expected_days":30,"days":30,"total_mm100":38440,"max_mm100":3370}, ...],
  "hit_flags": [false, false, true, false, false], "hits": 1
}
```

(Values from the live Studionet quote #2 on contract `0xE33aaE2B162663688E0FcB21F030C5BB87B5656F`.)

### Equivalence check (`_history_matches`)

| # | Fields | Rule |
|---|---|---|
| 1 | `lat_micro`, `lon_micro`, `windows`, `source`, `status` | exact |
| 2 | number of years, and per year `start_date`, `end_date`, `expected_days`, `days` | exact |
| 3 | per year `total_mm100`, `max_mm100` | non-negative int, \|a−b\| ≤ max(2.00 mm, 5 %) |
| 4 | `hit_flags`, `hits` | exact vs `_history_decide()` re-run on the **leader's own** numbers |
| 5 | `hit_flags`, `hits` | exact vs the validator's **own** result |
| 6 | leader returned an error | disagree |

Check 4 stops a leader from reporting real numbers with a cheaper hit count.
Check 5 stops a leader from nudging a year across the trigger inside the
tolerance band. Check 3 stops invented numbers. Validators also have to agree on
`status`: if the leader claims the history is incomplete but the validator can
read it (or the reverse), the round is rejected.

### After consensus

Every node checks the identity fields again, requires `status == "ok"` with every
day of every year present (otherwise the transaction reverts with *"Rainfall history
incomplete or unavailable, try again later"* and no quote is stored), recomputes
the flags, and only then prices:

```
loss_bps = hits * 10000 / 5
rate_bps = min(10000, max(pool.min_rate_bps, loss_bps + pool.loading_bps))
premium  = ceil(payout * rate_bps / 10000)
```

The quote (terms, rate, premium, hit count and the full agreed history JSON) is
stored on-chain and is valid for 3 days. `buy_policy(quote_id)` charges exactly
that premium. The pool's pricing at request time is locked into the quote.

**Edge of tolerance.** If ERA5 is revised between the leader's and a validator's
fetch and the revision flips a year across the trigger, check 5 fails and the
round rotates. A quote is not money-moving on its own, so the cost is only a
retry.

**Why Open-Meteo only for quotes.** NASA POWER is used at settlement as an
independent second source. For pricing, one consistent long series is enough and
keeps the request to a single call. A pricing model that blends both sources is on
the roadmap.

---

## 2. Settlement consensus (`settle`, unchanged from RainfallInsurance)

The settlement helpers (`_fetch_evidence`, `_decide`, `_evidence_matches`) are
copied unchanged from the earlier RainfallInsurance contract. Only the money
logic around them changed: the payout now comes out of the policy's own pool.

### Evidence

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
  "decision": "PAYOUT", "defer_reason": "", "payout_wei": 10000000000000000000
}
```

(From the direct-mode test on real August 2023 data.)

### Equivalence check (`_evidence_matches`)

| # | Fields | Rule |
|---|---|---|
| 1 | `policy_id`, `lat_micro`, `lon_micro`, `start_date`, `end_date`, `expected_days` | exact |
| 2 | number of sources, order, `name`, `status`, `days` | exact |
| 3 | per source `total_mm100`, `max_mm100` | non-negative int, \|a−b\| ≤ max(2.00 mm, 5 %) |
| 4 | `sources_ok`, `sources_used`, `drought`, `flood`, `decided_*`, `decision`, `defer_reason`, `payout_wei` | exact vs `_decide()` re-run on the leader's own numbers |
| 5 | `sources_ok`, `sources_used`, `drought`, `flood`, `decision`, `defer_reason`, `payout_wei` | exact vs the validator's own evidence |
| 6 | leader returned an error | disagree |

### Source-disagreement rules (`_decide`)

1. Both sources must be complete (every date present, no `null`, no NASA `-999`).
   Otherwise → `DEFER` (`SOURCE_MISSING`).
2. Drought: cumulative total `< X`. Flood: wettest day `> Y`. If the sources agree,
   use that. If they disagree within max(10 mm, 30 %), decide on the integer mean.
   Beyond that → `UNDECIDED` (`SOURCES_DIVERGE`).
3. Any trigger `TRIGGERED` → `PAYOUT` (full amount). All `NOT_TRIGGERED` →
   `NO_PAYOUT`. Otherwise `DEFER`: no money moves, the policy stays `ACTIVE`,
   anyone can retry until the deadline, then `expire()` refunds the premium.

| Window / trigger (real data, Tasikmalaya) | Open-Meteo | NASA POWER | Outcome |
|---|---|---|---|
| Aug 2023, drought < 60 mm | 48.40 mm | 13.99 mm | PAYOUT |
| Jan 2025, drought < 60 mm | 334.30 mm | 412.46 mm | NO_PAYOUT |
| Jan 2025, drought < 400 mm | 334.30 (yes) | 412.46 (no) | gap ≤ 30 % → mean 373.38 → PAYOUT |
| Jan 2025, flood > 50 mm/day | 31.50 (no) | 80.55 (yes) | gap > 30 % → DEFER |
| 10–22 Oct 2026 | complete | 2 days `-999` | DEFER (SOURCE_MISSING) |

After consensus every node re-checks the evidence identity against the stored
policy and recomputes `_decide()` before moving GEN, so the money path never
trusts a verdict string on its own.
