# GenLayer Rainfall Insurance: parametric crop cover with two-source weather consensus

A standalone GenLayer Intelligent Contract (`contracts/rainfall_insurance.py`) for
**parametric rainfall insurance** for smallholder farmers, for example rice farmers
in West Java. An insurer funds a GEN payout pool. A farmer pays a premium in native
GEN for a policy on a location, a coverage window, a **drought trigger**
(cumulative rainfall below X mm) and/or a **flood trigger** (wettest day above Y mm).
After the window closes, **anyone** can call `settle(policy_id)`. Validators
independently read daily precipitation from two free, keyless public sources and
pay the farmer automatically if a trigger is hit. There are no claim forms and no
loss adjusters.

| Source | Endpoint | Field |
|---|---|---|
| Open-Meteo historical archive (ERA5 reanalysis) | `https://archive-api.open-meteo.com/v1/archive?…&daily=precipitation_sum&timezone=auto` | `daily.precipitation_sum` (mm/day) |
| NASA POWER daily point API (MERRA-2 / GEOS-IT) | `https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG&…&format=JSON` | `properties.parameter.PRECTOTCORR` (mm/day, `-999` = missing) |

`get_source_urls(policy_id)` returns the exact URLs a policy is settled on.

## Why GenLayer and not a normal oracle

* **No trusted reporter.** With a Chainlink-style feed, someone has to run and pay
  for a custom job that pushes "rainfall at this exact farm, for this exact window".
  The feed operator is a single point of trust, and a long-tail location like a
  Tasikmalaya rice paddy will never have a feed. Here each validator fetches the raw
  public data itself, at settlement time, for any coordinates.
* **Consensus on derived facts, not raw bytes.** Two weather APIs never return
  byte-identical JSON, and the two products disagree day to day. GenLayer's custom
  leader/validator pattern lets validators agree on the *meaning* (days observed,
  totals within tolerance, trigger verdict, exact payout) rather than requiring
  identical responses, which a `strict_eq` oracle could not do.
* **Settlement logic is on-chain and auditable.** The disagreement rules, the
  tolerances and the money path are in one Python file, with no off-chain adapter.

## Consensus / validator design

Full write-up: **[CONSENSUS.md](CONSENSUS.md)**. The leader returns an evidence dict. Each
validator re-fetches both sources and accepts only if every settlement-driving field binds:

| Bound field | Rule |
|---|---|
| `policy_id` | exact |
| `lat_micro`, `lon_micro` (micro-degrees) | exact |
| `start_date`, `end_date` (window) | exact |
| `expected_days` | exact |
| per source: `name`, order, `status` (`ok`/`incomplete`/`unavailable`) | exact |
| per source: `days` observed | exact |
| per source: `total_mm100` (cumulative) | ±max(2.00 mm, 5 %) |
| per source: `max_mm100` (wettest day) | ±max(2.00 mm, 5 %) |
| `sources_ok`, `sources_used` | exact |
| `drought`, `flood` outcome | exact |
| `decided_total_mm100`, `decided_max_mm100` | exact vs `_decide()` re-run on the leader's numbers |
| `decision` (`PAYOUT`/`NO_PAYOUT`/`DEFER`), `defer_reason` | exact (vs leader self-consistency **and** the validator's own verdict) |
| `payout_wei` | exact (= stored payout for PAYOUT, else 0) |
| leader error | disagree |

After consensus, every node checks identity against the stored policy again and
recomputes the decision before moving GEN.

### Source-disagreement rules (deterministic, tested)

1. Both sources must be **complete** (every date present, no `null`/`-999`). Otherwise → `DEFER` (`SOURCE_MISSING`).
2. Per trigger: if the sources agree, use that answer. If they disagree within max(10 mm, 30 %), decide on the integer **mean**. If they disagree beyond that → `UNDECIDED` (`SOURCES_DIVERGE`).
3. Any trigger `TRIGGERED` → `PAYOUT` (full amount). All `NOT_TRIGGERED` → `NO_PAYOUT`. Otherwise → `DEFER`.
4. `DEFER` moves no money. The policy stays `ACTIVE` and can be retried until the claim deadline. After that, `expire()` refunds the premium.

## State machine

```
                    buy_policy (premium == quote, pool has free liquidity)
                                   │
                                   ▼
   cancel_policy (farmer,      ┌────────┐   settle → DEFER (source missing / diverging)
   before start_date) ◄────────│ ACTIVE │◄──────────────┐  attempts++, evidence stored
        │                      └────────┘───────────────┘
        ▼                       │   │   │
   CANCELLED                    │   │   └── expire (anyone, after deadline) ──► EXPIRED_REFUNDED
   (premium refunded)           │   │                                            (premium refunded)
                                │   └── settle → NO_PAYOUT ──► SETTLED_NO_PAYOUT (premium kept by pool)
                                └────── settle → PAYOUT ─────► PAID_OUT (payout sent to farmer)

 timeline: purchase < start_date ≤ … ≤ end_date | +7 days: settle opens | +60 days: deadline | then expire
```

### Pool accounting (integers only, wei)

* `pool_balance` = deposits + premiums − payouts − refunds − withdrawals
* `reserved` = sum of `payout` over `ACTIVE` policies. Invariant: `reserved ≤ pool_balance`.
* `buy_policy` requires `pool_balance − reserved ≥ payout` (premiums count as pool funds once received).
* The insurer can `withdraw` only `pool_balance − reserved`.
* Premium = `ceil(payout × premium_rate_bps / 10000)`. `msg.value` must equal it exactly.
* Every test re-checks `pool_balance == funded + premiums − paid − refunded − withdrawn`, and tests capture the native transfers (`EthSend`) to check recipient and amount.

## Method reference

| Method | Type | Who | Description |
|---|---|---|---|
| `__init__(premium_rate_bps: int)` | deploy | insurer | 1–10000 bps. The deployer becomes the insurer. |
| `fund_pool()` | write **payable** | insurer | Adds `msg.value` to the pool. |
| `withdraw(amount: int)` | write | insurer | Withdraws unreserved funds only. |
| `set_premium_rate(bps: int)` | write | insurer | Applies to new policies. |
| `quote_premium(payout: int) -> int` | view | anyone | Premium to send with `buy_policy`. |
| `buy_policy(lat_micro, lon_micro, start_date, end_date, drought_trigger_mm100, flood_trigger_mm100, payout) -> str` | write **payable** | farmer | Validates coords (±90/±180 deg), dates (`YYYY-MM-DD`, start 1–365 days ahead, ≤184-day window), triggers (≥0, at least one >0; 0 = disabled; hundredths of mm), payout >0, exact premium, liquidity. Returns the policy id. |
| `cancel_policy(policy_id) -> int` | write | policy holder | Before `start_date`: full premium refund. |
| `settle(policy_id) -> dict` | write (nondet) | anyone | From `end_date + 7d` to `end_date + 67d`. Returns the evidence. Pays, closes, or defers. |
| `expire(policy_id) -> int` | write | anyone | After `end_date + 67d` while still `ACTIVE`: premium refunded to the farmer. |
| `get_policy(policy_id) -> dict` | view | anyone | All fields plus `settle_opens`, `settle_deadline`, last `evidence` JSON. |
| `get_pool() -> dict` | view | anyone | Balances, reserve, free liquidity, totals. |
| `get_source_urls(policy_id) -> list` | view | anyone | The two exact URLs used at settlement. |

Units: coordinates in micro-degrees (`-7.327400` → `-7327400`), rainfall in hundredths
of a mm (`60 mm` → `6000`), money in wei (1 GEN = 10¹⁸).

## Run the tests

```bash
python3 -m pip install -r requirements.txt
genvm-lint check contracts/rainfall_insurance.py      # lint + SDK validation
genvm-lint typecheck contracts/rainfall_insurance.py  # pyright with the GenLayer SDK
pytest tests/direct -v                                # direct mode, no Studio needed
```

The first run downloads the GenVM v0.2.x SDK (~200 MB) into `~/.cache/gltest-direct`.
The web mocks are **real API responses** captured with curl; see
[`tests/direct/fixtures/README.md`](tests/direct/fixtures/README.md).

Result on 2026-10-23: **91 passed** (`test_pool_and_buy` 14, `test_settle` 19,
`test_refund_expire` 8, `test_validator` 36, `test_rules` 14). `genvm-lint check`:
lint passed (3 checks), validation passed (11 methods). `genvm-lint typecheck`: no type errors.

Coverage includes buy, settle→payout (drought and flood), settle→no-payout, both
disagreement branches (mean rule and diverge→defer), NASA `-999` fill / Open-Meteo
`null` / HTTP 503 / garbage body / wrong-window response → defer, defer-then-retry,
settle-too-early, settle-after-deadline, double-settle, cancel/refund, expire,
insufficient pool, every invalid input, withdraw limits, a full multi-policy
lifecycle, and the validator rejecting each tampered field (24 parametrised cases),
out-of-tolerance data, a missing source, a leader error, a self-consistent lie and a
stall attempt. `check_pickling` is on, so the leader/validator closures are verified
to serialise like they do in GenVM.

## Deploy

### Studionet (what was done)

```bash
python3 deploy/studionet.py faucet --gen 50          # Studio faucet (sim_fundAccount)
python3 deploy/studionet.py deploy --rate-bps 1000
python3 deploy/studionet.py fund --address <contract> --gen 10
python3 deploy/studionet.py buy  --address <contract> --start 2026-11-01 --end 2026-11-30 --drought-mm 60 --payout-gen 1
python3 deploy/studionet.py read --address <contract> --policy 1
```

The key comes from `$GENLAYER_PRIVATE_KEY` or a key file outside the repo (never commit it).
You can also paste the contract into https://studio.genlayer.com, pick a network, and deploy
with `premium_rate_bps = 1000`.
For testnet (Asimov/Bradbury), use `create_client(chain=testnet_asimov, …)` with a funded account. The faucet only works on Studio.

**Deployed on GenLayer Studionet** (exact file in this repo, sha256 `9671114b7422dcfffd41ab37a4037468f52ff6ec3a55ba7664f7cedf901848c7`):

| What | Value |
|---|---|
| Contract address | `0x6c84A4F9B0F00c12214FBECec8cBe421Bd0CE3a3` |
| Deploy tx | `0xb19df548d2f114770d9c3f1bd35261f1bc6888376bd20f32667db8d5af8df95c` (ACCEPTED, MAJORITY_AGREE) |
| `fund_pool` 10 GEN | `0x465b4353fd5c151da32c95c6a8524299bf1cdcfcc99061f2d2ecb30dcde70c4b` (ACCEPTED) |
| `buy_policy` #1 (Tasikmalaya, 2026-11-01..30, drought < 60 mm, payout 1 GEN, premium 0.1 GEN) | `0xe5ce5ea6d335b87f0e1bcd898f1f61fb2f7ade9aa051c8e60e5a0a9ed70d6bc9` (ACCEPTED) |
| Deployer / insurer (throwaway Studio key) | `0xFAb3F6D18120302b5c6d171619C9C2bcBA3f623f` |

Policy #1 settles from 2026-12-07 (deadline 2027-02-05):
`settle("1")` on the contract above.

### Live consensus check of the settlement pipeline

A real `settle()` cannot run on a fresh deployment, because coverage must start
after purchase. To exercise the leader/validator path on real GenVM validators today,
`tools/make_settlement_probe.py` builds a throwaway probe contract. It contains the
**verbatim** helper code from the contract (`_fetch_evidence`, `_decide`,
`_evidence_matches`, the same `gl.vm.run_nondet` call) and takes any past window.

* Probe contract: `0x2811D27AdF78ed2Bf4002240B95673028B83fbDA` (deploy tx `0xbdec63765924f4316698143416b68fc7eea8093a321fcac0550240257af62866`)
* `check(-7327400, 108220700, "2023-08-01", "2023-08-31", 6000, 0, 1 GEN)`: tx
  `0x74b615e6f03027f124f36181cc75913f93130d1154afd14b7539ea3287b8e3ad` → **FINALIZED, MAJORITY_AGREE**
  (3 validators agree, 2 idle). Both live sources were `ok` / 31 days. Open-Meteo 48.40 mm and NASA
  13.99 mm match the test fixtures exactly, so the result was `PAYOUT`.

## Limitations

* **Basis risk.** Gridded reanalysis (~9 km ERA5, ~50 km MERRA-2) is not a rain gauge
  on the farm. Parametric cover pays on the index, not on the actual loss.
* **Day boundaries.** Open-Meteo is requested in the location's local time
  (`timezone=auto`). NASA POWER days are local solar time. They are close but not identical.
* **Data latency.** ERA5/MERRA-2 lag by days. NASA returns `-999` for recent days, so settlement
  opens 7 days after `end_date` and defers while data is incomplete.
* **Simple binary payout.** One fixed payout per policy, with no graduated tiers.
  The cross-source rule uses a 2-source mean. A third source would allow a true median.
* **Single insurer, no premium pricing model.** One flat bps rate. The insurer is the deployer.
* **Tolerance edge.** A data revision that crosses a trigger between leader and validator
  fetches causes disagreement and leader rotation (intended, see CONSENSUS.md).
* **Studio balances are simulated.** Native transfers are emitted as messages (`emit_transfer`).
  In direct-mode tests they are captured and asserted, not settled on a real chain.
* **SDK version.** Pinned to `py-genlayer:1jb45aa8…` (GenVM v0.2.x, the hash in the
  current docs and boilerplate). The linter reports a newer v0.3 runner. Moving to it
  needs the v0.3 renames (`gl.contract.Contract`, `run_nondet` → `run_nondet_default`, …).

## Files

```
contracts/rainfall_insurance.py   the Intelligent Contract
tests/direct/                     direct-mode tests + real API fixtures
CONSENSUS.md                      equivalence-check / disagreement-rule write-up
deploy/studionet.py               genlayer-py deploy + interaction script
tools/make_settlement_probe.py    builds the live-consensus probe from the contract's helpers
.github/workflows/ci.yml          lint + tests on push
```

License: MIT
