# Fixtures: real API responses

Captured with `curl` on 2026-10-23 for Tasikmalaya, West Java (lat -7.327400, lon 108.220700).
They are used unmodified as web mocks (some tests derive modified copies in code).

| File | URL |
|---|---|
| `om_2023-08.json` | `https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400&longitude=108.220700&start_date=2023-08-01&end_date=2023-08-31&daily=precipitation_sum&timezone=auto` |
| `om_2025-01.json` | same, `start_date=2025-01-01&end_date=2025-01-31` |
| `om_recent.json` | same, `start_date=2026-10-10&end_date=2026-10-22` |
| `nasa_2023-08.json` | `https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG&longitude=108.220700&latitude=-7.327400&start=20230801&end=20230831&format=JSON` |
| `nasa_2025-01.json` | same, `start=20250101&end=20250131` |
| `nasa_recent.json` | same, `start=20261010&end=20261022` (last 2 days are the `-999` fill value) |

Totals in hundredths of mm (what the contract computes):

| Window | Open-Meteo total / max | NASA POWER total / max |
|---|---|---|
| Aug 2023 (El Niño dry season) | 4840 / 880 | 1399 / 508 |
| Jan 2025 (wet season) | 33430 / 3150 | 41246 / 8055 |
| 10–22 Oct 2026 | 2870 / 620 (13 days) | 43272 / 8208 (11 of 13 days) |

## Quote history fixtures (added for PadiGuard)

Captured with `curl` on 2026-10-25, same location, one request each (exactly the
request `request_quote()` makes for these windows):

| File | URL | Used for |
|---|---|---|
| `om_hist_aug_2018-2022.json` | `…/v1/archive?latitude=-7.327400&longitude=108.220700&start_date=2018-08-01&end_date=2022-08-31&daily=precipitation_sum&timezone=auto` | quote for 2023-08-01..31 requested on 2023-07-01 |
| `om_hist_jan_2020-2024.json` | same, `start_date=2020-01-01&end_date=2024-01-31` | quote for 2025-01-01..31 requested on 2024-12-15 |

Per-window totals / wettest day (hundredths of mm):

| Year | Aug total / max | Jan total / max |
|---|---|---|
| 2018 | 4930 / 510 | – |
| 2019 | 4490 / 630 | – |
| 2020 | 13470 / 2170 | 50930 / 10000 |
| 2021 | 34460 / 6340 | 40500 / 6070 |
| 2022 | 23520 / 2470 | 28860 / 3130 |
| 2023 | – | 20760 / 2150 |
| 2024 | – | 40380 / 4860 |

Tests that only exercise settlement or pool accounting use a **synthetic**
constant-rainfall history built in `conftest.py` (`synthetic_history`), chosen so
no history year hits the trigger; that keeps their premium at the pool minimum.
