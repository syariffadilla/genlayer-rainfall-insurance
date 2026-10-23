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
