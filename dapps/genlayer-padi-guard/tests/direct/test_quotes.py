"""request_quote(): risk pricing from 5 years of real Open-Meteo history.

Real history fixtures (Tasikmalaya, hundredths of mm, newest year first):

  August (window 2023-08-01..31 quoted on 2023-07-01, history 2022..2018)
      year   2022   2021   2020   2019   2018
      total 23520  34460  13470   4490   4930
      max    2470   6340   2170    630    510

  January (window 2025-01-01..31 quoted on 2024-12-15, history 2024..2020)
      year   2024   2023   2022   2021   2020
      total 40380  20760  28860  40500  50930
      max    4860   2150   3130   6070  10000
"""

import datetime
import json

from tests.direct.conftest import (
    FUNDED,
    GEN,
    LAT,
    LON,
    contract_module,
    fixture_text,
    mock_history,
    pool_invariants,
    quote,
    synthetic_history,
    to_hex,
)

AUG = ("2023-08-01", "2023-08-31")
JAN = ("2025-01-01", "2025-01-31")
AUG_HIST = "om_hist_aug_2018-2022.json"
JAN_HIST = "om_hist_jan_2020-2024.json"


def _d(s):
    return datetime.date.fromisoformat(s)


# ---------------------------------------------------------------- pure helpers


def test_history_windows_newest_first(guard):
    mod = contract_module()
    assert mod._history_windows(*AUG, _d("2023-07-01")) == [
        ["2022-08-01", "2022-08-31"],
        ["2021-08-01", "2021-08-31"],
        ["2020-08-01", "2020-08-31"],
        ["2019-08-01", "2019-08-31"],
        ["2018-08-01", "2018-08-31"],
    ]
    assert mod._history_range(mod._history_windows(*AUG, _d("2023-07-01"))) == ["2018-08-01", "2022-08-31"]


def test_history_windows_skip_years_not_yet_observed(guard):
    mod = contract_module()
    # quoted 2023-07-01 for June 2024: June 2023 ends 2023-06-30, only 1 day ago (< 7-day lag) -> skipped
    w = mod._history_windows("2024-06-01", "2024-06-30", _d("2023-07-01"))
    assert w[0] == ["2022-06-01", "2022-06-30"] and w[-1] == ["2018-06-01", "2018-06-30"] and len(w) == 5
    # exactly 7 days after the end of last year's window is usable
    w = mod._history_windows("2024-06-01", "2024-06-24", _d("2023-07-01"))
    assert w[0] == ["2023-06-01", "2023-06-24"]


def test_history_windows_leap_day(guard):
    mod = contract_module()
    w = mod._history_windows("2024-02-01", "2024-02-29", _d("2023-12-01"))
    assert w[0] == ["2023-02-01", "2023-02-28"]
    assert w[3] == ["2020-02-01", "2020-02-29"]  # 2020 is a leap year: 29 Feb kept
    w = mod._history_windows("2025-02-28", "2025-03-31", _d("2024-12-01"))
    assert w[0] == ["2024-02-28", "2024-03-31"]


def test_rate_formula(guard):
    mod = contract_module()
    assert mod._rate_for(0, 5, 1000, 500) == 1000  # floor at pool minimum
    assert mod._rate_for(1, 5, 1000, 500) == 2500  # 20 % + 5 %
    assert mod._rate_for(2, 5, 1000, 500) == 4500
    assert mod._rate_for(5, 5, 1000, 500) == 10000  # capped at 100 %
    assert mod._rate_for(0, 5, 300, 0) == 300
    assert mod._premium(10 * GEN, 4500) == 45 * GEN // 10
    assert mod._premium(1, 4500) == 1  # rounds up
    assert mod._premium(15, 1000) == 2


# ---------------------------------------------------------------- real-data quotes


def test_quote_drought_real_history(direct_vm, clock, guard, direct_alice):
    q = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=10 * GEN, body=fixture_text(AUG_HIST))
    assert q["id"] == "1" and q["pool_id"] == "1"
    assert q["requester"] == to_hex(direct_alice)
    assert (q["lat_micro"], q["lon_micro"], q["start_date"], q["end_date"]) == (LAT, LON, *AUG)
    assert (q["drought_trigger_mm100"], q["flood_trigger_mm100"], q["payout"]) == (6000, 0, 10 * GEN)
    assert q["hits"] == 2 and q["years"] == 5  # Aug 2019 (44.90 mm) and Aug 2018 (49.30 mm) < 60 mm
    assert q["rate_bps"] == 4500  # 2/5 = 40 % + 5 % loading
    assert q["premium"] == 45 * GEN // 10
    assert (q["created_on"], q["valid_until"], q["policy_id"]) == ("2023-07-01", "2023-07-04", "")
    h = json.loads(q["history"])
    assert h["source"] == "open-meteo" and h["status"] == "ok"
    assert [y["total_mm100"] for y in h["years"]] == [23520, 34460, 13470, 4490, 4930]
    assert [y["max_mm100"] for y in h["years"]] == [2470, 6340, 2170, 630, 510]
    assert all(y["days"] == y["expected_days"] == 31 for y in h["years"])
    assert h["hit_flags"] == [False, False, False, True, True]
    assert guard.get_quote("1") == q
    assert [x["id"] for x in guard.get_farmer_quotes(to_hex(direct_alice))] == ["1"]
    assert guard.get_quote_source_url("1") == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400&longitude=108.220700"
        "&start_date=2018-08-01&end_date=2022-08-31&daily=precipitation_sum&timezone=auto"
    )
    # a quote moves no money and reserves nothing
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0 and pool["balance"] == FUNDED
    assert guard.get_stats()["quote_count"] == 1


def test_quote_flood_real_history(direct_vm, clock, guard, direct_alice):
    clock("2024-12-15")
    q = quote(direct_vm, guard, clock, direct_alice, *JAN, flood=2500, payout=10 * GEN, body=fixture_text(JAN_HIST))
    h = json.loads(q["history"])
    assert [w for w in h["windows"]][0] == ["2024-01-01", "2024-01-31"]
    assert h["hit_flags"] == [True, False, True, True, True]  # wettest day > 25 mm in 4 of 5 Januaries
    assert q["hits"] == 4 and q["rate_bps"] == 8500 and q["premium"] == 85 * GEN // 10


def test_quote_drought_and_flood_combined(direct_vm, clock, guard, direct_alice):
    clock("2024-12-15")
    # drought < 300 mm hits 2023 (207.60) and 2022 (288.60); flood > 80 mm hits 2020 (100 mm)
    q = quote(direct_vm, guard, clock, direct_alice, *JAN, drought=30000, flood=8000, body=fixture_text(JAN_HIST))
    assert json.loads(q["history"])["hit_flags"] == [False, True, True, False, True]
    assert q["hits"] == 3 and q["rate_bps"] == 6500


def test_quote_uses_pool_minimum_when_history_is_benign(direct_vm, clock, guard, direct_alice):
    # drought < 10 mm never happened in the last 5 Augusts
    q = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=1000, body=fixture_text(AUG_HIST))
    assert q["hits"] == 0 and q["rate_bps"] == 1000 and q["premium"] == GEN


def test_quote_reflects_pool_pricing_at_request_time(direct_vm, clock, guard, direct_alice, direct_owner):
    q1 = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, body=fixture_text(AUG_HIST))
    direct_vm.sender = direct_owner
    guard.set_pool_pricing("1", 1000, 2000)
    q2 = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, body=fixture_text(AUG_HIST))
    assert (q1["rate_bps"], q2["rate_bps"]) == (4500, 6000)
    assert guard.get_quote(q1["id"])["rate_bps"] == 4500  # existing quote is locked


# ---------------------------------------------------------------- missing / bad history


def _expect_quote_fails(vm, guard, clock, farmer, msg, **hist):
    with vm.expect_revert(msg):
        quote(vm, guard, clock, farmer, *AUG, drought=6000, **hist)
    assert guard.get_stats()["quote_count"] == 0
    assert guard.get_farmer_quotes(to_hex(farmer)) == []


def test_quote_with_a_missing_day_is_refused(direct_vm, clock, guard, direct_alice):
    body = json.loads(fixture_text(AUG_HIST))
    i = body["daily"]["time"].index("2020-08-15")
    body["daily"]["precipitation_sum"][i] = None
    _expect_quote_fails(direct_vm, guard, clock, direct_alice, "Rainfall history incomplete", body=json.dumps(body))


def test_quote_with_source_down_is_refused(direct_vm, clock, guard, direct_alice):
    _expect_quote_fails(direct_vm, guard, clock, direct_alice, "Rainfall history incomplete", body="busy", status=503)


def test_quote_with_garbage_or_truncated_body_is_refused(direct_vm, clock, guard, direct_alice):
    _expect_quote_fails(direct_vm, guard, clock, direct_alice, "Rainfall history incomplete", body="<html>oops</html>")
    # response that stops a year early (last window missing entirely)
    trunc = synthetic_history("2018-08-01", "2021-08-31", 500)
    _expect_quote_fails(direct_vm, guard, clock, direct_alice, "Rainfall history incomplete", body=trunc)


# ---------------------------------------------------------------- input validation (shared with buy)


def _expect_terms_revert(vm, guard, farmer, msg, lat=LAT, lon=LON, start=AUG[0], end=AUG[1],
                         drought=6000, flood=0, payout=10 * GEN):
    vm.sender = farmer
    with vm.expect_revert(msg):
        guard.request_quote("1", lat, lon, start, end, drought, flood, payout)


def test_quote_invalid_coordinates(direct_vm, guard, direct_alice):
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid latitude", lat=90000001)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid latitude", lat=-90000001)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid longitude", lon=180000001)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid longitude", lon=-180000001)


def test_quote_invalid_dates(direct_vm, clock, guard, direct_alice):
    bad = "Invalid date format"
    for start, end in [
        ("2023-8-01", "2023-08-31"),
        ("2023/08/01", "2023-08-31"),
        ("2023-02-30", "2023-03-31"),
        ("2023-08-01", "20230831"),
        ("", "2023-08-31"),
    ]:
        _expect_terms_revert(direct_vm, guard, direct_alice, bad, start=start, end=end)
    _expect_terms_revert(direct_vm, guard, direct_alice, "end_date before start_date", start="2023-08-31", end="2023-08-01")
    _expect_terms_revert(direct_vm, guard, direct_alice, "Coverage window too long", start="2023-08-01", end="2024-02-01")
    _expect_terms_revert(direct_vm, guard, direct_alice, "Coverage must start after purchase date", start="2023-07-01", end="2023-07-31")
    _expect_terms_revert(direct_vm, guard, direct_alice, "Coverage must start after purchase date", start="2023-06-01", end="2023-07-31")
    _expect_terms_revert(direct_vm, guard, direct_alice, "Coverage starts too far ahead", start="2024-07-01", end="2024-07-31")
    # boundary: exactly 184 days, starting tomorrow, is accepted
    q = quote(direct_vm, guard, clock, direct_alice, "2023-07-02", "2024-01-01", drought=6000)
    assert q["id"] == "1"


def test_quote_invalid_triggers_and_payout(direct_vm, guard, direct_alice):
    _expect_terms_revert(direct_vm, guard, direct_alice, "At least one trigger required", drought=0, flood=0)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid trigger", drought=-1)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid trigger", flood=-5)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid payout", payout=0)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Invalid payout", payout=-10)
    _expect_terms_revert(direct_vm, guard, direct_alice, "Insufficient pool liquidity", payout=FUNDED + 1)
    assert guard.get_stats()["quote_count"] == 0
