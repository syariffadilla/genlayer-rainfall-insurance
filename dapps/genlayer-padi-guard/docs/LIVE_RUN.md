# Live run on GenLayer Studionet (2026-10-25)

Everything below ran against the deployed contract on GenLayer Studionet
(`https://studio.genlayer.com/api`, chain id `0xf22f`). All keys were throwaway
Studionet keys; balances are Studio test GEN with no real value.

| | |
|---|---|
| Contract | `0xE33aaE2B162663688E0FcB21F030C5BB87B5656F` |
| Source | `contracts/padi_guard.py`, sha256 `45208d51bb412f49458dcbf13fe286102f07115f88a6849196bf91f9901b8803` |
| On-chain code check | `npm run verify-code` → `MATCH` (code returned by `getContractCode` is byte-identical to the file) |
| Deploy tx | `0x693b613c27ba019dcb8bc1f0f5cad860c9adc2720b9c9b815a8a8c9e72486641` · FINALIZED · MAJORITY_AGREE (5/5 agree) |

Accounts:

* Insurer of pool #1 (Node script): `0xd3E7eBD19e1501ee94B8aBE66933Ed5316f47B83`
* Farmer (Node script): `0x4a2ED8EF12c17B9Ea7D1d6eC4511bbea25213c68`
* Browser burner account (headless Chromium driving the built frontend): `0xF8caAb3Dc756a83c234342E0f116CFCDeD4AD625`, acting as farmer and as the insurer of pool #2

## A. Node script (`frontend/scripts/e2e.mjs`, same client library as the UI)

| Step | Tx | Result |
|---|---|---|
| `create_pool("West Java Padi Pool (demo)", 1000, 500, West Java box)` + 20 GEN | `0xbea9fa6d37a267cf904d1b12e966150d6e94fe673157019462fb7c70c6110c32` | ACCEPTED · MAJORITY_AGREE · returned pool `"1"` |
| `fund_pool("1")` + 5 GEN | `0x8680d477561a91780e37e897f97e469b0386417bdea14d41b0d31742c5a23fde` | ACCEPTED · pool balance 25 GEN |
| `request_quote("1", -7327400, 108220700, "2026-11-01", "2026-11-30", 15000, 0, 2 GEN)` | `0xfd5edd8a4470622d11418ddeccc98734d962bc30fff25e8625f3977179793b6c` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. Validators fetched November 2021–2025 for Tasikmalaya. 0 of 5 years below 150 mm → rate 10 % (pool minimum) → premium 0.2 GEN |
| `buy_policy("1")` + 0.2 GEN | `0xa89121068081018524ac34265f5b678522397ab033e23699e4eda8762b984cea` | FINALIZED · policy `"1"` |
| `settle("1")` before the window | `0xcfde910e17d80160b78ec9ef19ba944a171dab60f66e5e552d2cf2b3f2f9490e` | ACCEPTED, execution ERROR **"Settlement not open yet"** (expected: settlement opens 2026-12-07) |
| `withdraw("1", 1 GEN)` | `0x852d64fb554fc93c03dcfe3993c1eebd1a9526a786287a766d8b1a126885cbe9` | FINALIZED; triggered transfer tx `0x737e63f28ba93737e152371ffcced8cf133c34dbb55a4009d675001922d86364`; insurer balance 175 → 176 GEN, contract balance 30.7 → 29.7 GEN after finalization |

## B. Browser (built `frontend/dist`, headless Chromium, `tools/browser_check.py`)

| UI action | Tx | Result |
|---|---|---|
| "+10 test GEN" (Studio faucet) | `0x3cbd787bc724a3ef0bae266d7ce2d6ff6138560f9245cbfb907ee222d44ad1a8` | balance 10 GEN shown |
| Farmer tab → Request quote (drought < 300 mm, payout 2 GEN, pool #1) | `0x728434d97a01e52318fe74706744de39e8becce4eb2673b1fc86838a108d70da` | **FINALIZED · MAJORITY_AGREE (3 agree, 2 idle)**. Quote #2: 1 of 5 years would have paid (Nov 2023: 246.50 mm) → 20 % + 5 % loading = 25 % → premium 0.5 GEN |
| Buy policy · pay 0.5 GEN | `0xf03dcd3eb9b16f4ee31a58107cb67154fd01e78fcaefe68c698d70f54145b8ec` | FINALIZED · policy `"2"` |
| Insurer tab → Create pool "Java Mutual (demo, 2nd insurer)", 8 % / 3 %, Java, 5 GEN | `0x98c813c3c2c4154f5709cbf0effabe487c589dd3c4a23f1c19f51674aeeb6047` | FINALIZED · pool `"2"` (second insurer) |
| Insurer tab → Fund pool #2 with 1 GEN | `0x0dc3f723fb358619dac8932e94fc68090a137a5cdecc34fbc7cade402e7bf279` | ACCEPTED · pool #2 balance 6 GEN |
| Policies tab → policy #2 → "Try settle anyway" | `0x52192bbe058664a5f1f5e6b561df6d178d72cad389a59525d9aa1c3495bcce8c` | execution ERROR "Settlement not open yet" shown in the UI (expected) |

Rainfall history agreed on-chain in quote #2 (Open-Meteo ERA5, Tasikmalaya):

| Window | Total | Wettest day | Below 300 mm? |
|---|---|---|---|
| 2025-11-01 → 30 | 384.40 mm | 33.70 mm | no |
| 2024-11-01 → 30 | 377.90 mm | 44.90 mm | no |
| 2023-11-01 → 30 | 246.50 mm | 28.60 mm | **yes** |
| 2022-11-01 → 30 | 457.80 mm | 49.60 mm | no |
| 2021-11-01 → 30 | 460.70 mm | 39.10 mm | no |

Screenshots of these screens are in `docs/screenshots/`.

## Not done live (and why)

* **A live `settle()` with a payout decision.** A policy can only be bought for a
  window that starts in the future, and settlement opens 7 days after the window
  ends. Policies #1 and #2 cover 2026-11-01..30, so `settle("1")` / `settle("2")`
  can first run on **2026-12-07** (deadline 2027-02-05). The settlement path is
  covered by the direct-mode tests on real captured data, and the same settlement
  consensus code was run live in the earlier RainfallInsurance project through a
  probe contract.
* **MetaMask.** The wallet path (`provider: window.ethereum` + `client.connect("studionet")`)
  is implemented but was not exercised here: a headless browser has no wallet. The
  burner-account path is the one tested end to end.

## State after the run

`npm run read` on 2026-10-25: 2 pools, 2 quotes, 2 policies (both ACTIVE),
total pool balance 29.7 GEN before the pool #2 top-up (30.7 GEN after), 4 GEN reserved for payouts,
0.7 GEN premiums collected, 0 GEN paid out.
