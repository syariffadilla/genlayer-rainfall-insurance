"""settle(): payout, no-payout, source disagreement, missing sources, timing, double-settle.

All web responses are real Open-Meteo / NASA POWER payloads (see conftest.py).
Reference numbers (hundredths of mm) computed from the fixtures:

  2023-08  Open-Meteo total 4840  max 880   | NASA POWER total 1399  max 508
  2025-01  Open-Meteo total 33430 max 3150  | NASA POWER total 41246 max 8055
"""

import json

from tests.direct.conftest import (
    GEN,
    LAT,
    LON,
    pool_invariants,
    buy,
    fixture_text,
    mock_month,
    mock_nasa,
    mock_open_meteo,
    to_hex,
)

FUNDED = 100 * GEN
AUG = ("2023-08-01", "2023-08-31")
JAN = ("2025-01-01", "2025-01-31")


def _buy_jan(vm, c, clock, farmer, **kw):
    clock("2024-12-15")
    return buy(vm, c, clock, farmer, *JAN, **kw)


def _settle_jan(vm, c, clock, pid, anyone):
    clock("2025-02-10")
    mock_month(vm, "2025-01", *JAN)
    vm.sender = anyone
    return c.settle(pid)


# ------------------------------------------------------------------ payout


def test_drought_payout_real_el_nino_august_2023(direct_vm, clock, guard, direct_alice, direct_bob, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=10 * GEN)
    clock("2023-09-07")  # settlement opens end + 7 days
    mock_month(direct_vm, "2023-08", *AUG)
    direct_vm.sender = direct_bob  # anyone may settle
    ev = guard.settle(pid)

    assert ev["policy_id"] == pid
    assert (ev["lat_micro"], ev["lon_micro"]) == (LAT, LON)
    assert (ev["start_date"], ev["end_date"], ev["expected_days"]) == ("2023-08-01", "2023-08-31", 31)
    assert ev["sources"] == [
        {"name": "open-meteo", "status": "ok", "days": 31, "total_mm100": 4840, "max_mm100": 880},
        {"name": "nasa-power", "status": "ok", "days": 31, "total_mm100": 1399, "max_mm100": 508},
    ]
    assert ev["sources_ok"] == 2 and ev["sources_used"] == ["open-meteo", "nasa-power"]
    assert ev["drought"] == "TRIGGERED" and ev["flood"] == "DISABLED"
    assert ev["decided_total_mm100"] == (4840 + 1399) // 2
    assert ev["decision"] == "PAYOUT" and ev["defer_reason"] == ""
    assert ev["payout_wei"] == 10 * GEN

    p = guard.get_policy(pid)
    assert p["status"] == "PAID_OUT" and p["paid_amount"] == 10 * GEN and p["settle_attempts"] == 1
    assert json.loads(p["evidence"])["decision"] == "PAYOUT"
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]  # paid to the farmer, not the caller
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + GEN - 10 * GEN
    assert pool["total_paid_out"] == 10 * GEN


def test_flood_payout_both_sources_agree(direct_vm, clock, guard, direct_alice, transfers):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, flood=2500, payout=5 * GEN)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["flood"] == "TRIGGERED" and ev["decision"] == "PAYOUT"
    assert ev["payout_wei"] == 5 * GEN
    assert guard.get_policy(pid)["status"] == "PAID_OUT"
    assert transfers == [(to_hex(direct_alice), 5 * GEN)]
    pool_invariants(guard, "1", FUNDED)


# ------------------------------------------------------------------ no payout


def test_no_payout_wet_january_2025(direct_vm, clock, guard, direct_alice, direct_owner, transfers):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000, payout=10 * GEN)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_owner)
    assert ev["drought"] == "NOT_TRIGGERED"
    assert ev["decision"] == "NO_PAYOUT" and ev["payout_wei"] == 0
    p = guard.get_policy(pid)
    assert p["status"] == "SETTLED_NO_PAYOUT" and p["paid_amount"] == 0
    assert transfers == []
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + GEN  # premium earned by the pool
    # released reserve is now withdrawable by the insurer
    direct_vm.sender = direct_owner
    guard.withdraw("1", FUNDED + GEN)
    assert transfers == [(to_hex(direct_owner), FUNDED + GEN)]
    pool_invariants(guard, "1", FUNDED)


# ------------------------------------------------------------------ disagreement rules


def test_sources_disagree_within_tolerance_mean_triggers(direct_vm, clock, guard, direct_alice, transfers):
    # drought trigger 400 mm: Open-Meteo 334.30 (< 400, triggered) vs NASA 412.46 (not).
    # |diff| = 78.16 mm <= 30 % of 412.46 -> decide on mean 373.38 mm -> TRIGGERED
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=40000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["drought"] == "TRIGGERED"
    assert ev["decided_total_mm100"] == 37338
    assert ev["decision"] == "PAYOUT"
    assert guard.get_policy(pid)["status"] == "PAID_OUT"
    assert len(transfers) == 1


def test_sources_disagree_within_tolerance_mean_not_triggered(direct_vm, clock, guard, direct_alice, transfers):
    # drought trigger 350 mm: OM 334.30 triggered, NASA 412.46 not; mean 373.38 >= 350 -> NOT_TRIGGERED
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=35000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["drought"] == "NOT_TRIGGERED" and ev["decided_total_mm100"] == 37338
    assert ev["decision"] == "NO_PAYOUT"
    assert guard.get_policy(pid)["status"] == "SETTLED_NO_PAYOUT"
    assert transfers == []


def test_sources_diverge_beyond_tolerance_defers(direct_vm, clock, guard, direct_alice, transfers):
    # flood trigger 50 mm/day: OM max 31.50 (no) vs NASA max 80.55 (yes) -> diff 49.05 mm,
    # beyond max(10 mm, 30 %) -> UNDECIDED -> DEFER, nothing moves
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, flood=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert ev["flood"] == "UNDECIDED" and ev["decided_max_mm100"] == -1
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCES_DIVERGE"
    assert ev["payout_wei"] == 0
    p = guard.get_policy(pid)
    assert p["status"] == "ACTIVE" and p["settle_attempts"] == 1
    assert json.loads(p["evidence"])["decision"] == "DEFER"
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 10 * GEN  # reserve stays locked while deferred
    assert transfers == []


def test_one_component_triggered_beats_undecided(direct_vm, clock, guard, direct_alice, transfers):
    # drought 400 mm -> TRIGGERED (mean rule); flood 50 mm -> UNDECIDED. Any trigger pays.
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=40000, flood=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert (ev["drought"], ev["flood"], ev["decision"]) == ("TRIGGERED", "UNDECIDED", "PAYOUT")
    assert ev["defer_reason"] == ""
    assert len(transfers) == 1


def test_not_triggered_plus_undecided_defers(direct_vm, clock, guard, direct_alice):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000, flood=5000)
    ev = _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    assert (ev["drought"], ev["flood"], ev["decision"]) == ("NOT_TRIGGERED", "UNDECIDED", "DEFER")
    assert guard.get_policy(pid)["status"] == "ACTIVE"


def test_nasa_fill_values_incomplete_defers(direct_vm, clock, guard, direct_alice, transfers):
    # Real NASA POWER response with -999 fill values for the last two days
    start, end = "2026-10-10", "2026-10-22"
    clock("2026-09-01")
    pid = buy(direct_vm, guard, clock, direct_alice, start, end, drought=6000)
    clock("2026-10-30")
    mock_month(direct_vm, "recent", start, end)
    ev = guard.settle(pid)
    assert ev["sources"][1] == {"name": "nasa-power", "status": "incomplete", "days": 11,
                                "total_mm100": 43272, "max_mm100": 8208}
    assert ev["sources"][0]["status"] == "ok" and ev["sources"][0]["days"] == 13
    assert ev["sources_ok"] == 1 and ev["sources_used"] == ["open-meteo"]
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCE_MISSING"
    assert ev["drought"] == "UNDECIDED"
    assert guard.get_policy(pid)["status"] == "ACTIVE"
    assert transfers == []


def test_source_http_error_defers_then_settles_on_retry(direct_vm, clock, guard, direct_alice, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    mock_open_meteo(direct_vm, *AUG, fixture_text("om_2023-08.json"))
    mock_nasa(direct_vm, *AUG, "Service Unavailable", status=503)
    ev = guard.settle(pid)
    assert ev["sources"][1]["status"] == "unavailable"
    assert ev["decision"] == "DEFER" and ev["defer_reason"] == "SOURCE_MISSING"
    assert guard.get_policy(pid)["settle_attempts"] == 1

    # NASA back online the next day -> settles normally
    direct_vm.clear_mocks()
    clock("2023-09-09")
    mock_month(direct_vm, "2023-08", *AUG)
    ev = guard.settle(pid)
    assert ev["decision"] == "PAYOUT"
    p = guard.get_policy(pid)
    assert p["status"] == "PAID_OUT" and p["settle_attempts"] == 2
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool_invariants(guard, "1", FUNDED)


def test_both_sources_unavailable_or_garbage_defers(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    mock_open_meteo(direct_vm, *AUG, "<html>not json</html>")
    mock_nasa(direct_vm, *AUG, '{"messages": ["error"]}')
    ev = guard.settle(pid)
    assert [s["status"] for s in ev["sources"]] == ["unavailable", "unavailable"]
    assert ev["sources_ok"] == 0 and ev["decision"] == "DEFER"


def test_open_meteo_null_day_is_incomplete(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["precipitation_sum"][30] = None  # real API returns null for not-yet-available days
    mock_month(direct_vm, "2023-08", *AUG, om_body=json.dumps(om))
    ev = guard.settle(pid)
    assert ev["sources"][0]["status"] == "incomplete" and ev["sources"][0]["days"] == 30
    assert ev["decision"] == "DEFER"


def test_open_meteo_wrong_window_rejected(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-08")
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["time"][0] = "2023-07-31"  # response for a different window must not count
    mock_month(direct_vm, "2023-08", *AUG, om_body=json.dumps(om))
    ev = guard.settle(pid)
    assert ev["sources"][0] == {"name": "open-meteo", "status": "incomplete", "days": 0,
                                "total_mm100": 0, "max_mm100": 0}
    assert ev["decision"] == "DEFER"


# ------------------------------------------------------------------ timing / state machine


def test_settle_before_window_opens_reverts(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-09-06T23:59:59Z")  # end + 6 days
    with direct_vm.expect_revert("Settlement not open yet"):
        guard.settle(pid)
    assert guard.get_policy(pid)["settle_attempts"] == 0


def test_settle_on_deadline_day_allowed(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-11-06T12:00:00Z")  # deadline day itself is still allowed
    mock_month(direct_vm, "2023-08", *AUG)
    assert guard.settle(pid)["decision"] == "PAYOUT"


def test_settle_day_after_deadline_reverts(direct_vm, clock, guard, direct_alice):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-11-07")
    with direct_vm.expect_revert("Settlement deadline passed"):
        guard.settle(pid)


def test_double_settle_reverts_and_pays_once(direct_vm, clock, guard, direct_alice, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-10")
    mock_month(direct_vm, "2023-08", *AUG)
    guard.settle(pid)
    with direct_vm.expect_revert("Policy not active"):
        guard.settle(pid)
    assert transfers == [(to_hex(direct_alice), 10 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["total_paid_out"] == 10 * GEN


def test_double_settle_after_no_payout_reverts(direct_vm, clock, guard, direct_alice):
    pid = _buy_jan(direct_vm, guard, clock, direct_alice, drought=6000)
    _settle_jan(direct_vm, guard, clock, pid, direct_alice)
    with direct_vm.expect_revert("Policy not active"):
        guard.settle(pid)


def test_multiple_policies_independent(direct_vm, clock, guard, direct_alice, direct_bob, transfers):
    p1 = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=20 * GEN)
    p2 = buy(direct_vm, guard, clock, direct_bob, *AUG, drought=1000, payout=30 * GEN)  # 10 mm: not triggered
    clock("2023-09-10")
    mock_month(direct_vm, "2023-08", *AUG)
    assert guard.settle(p1)["decision"] == "PAYOUT"
    ev2 = guard.settle(p2)
    # 4840 vs 1399 with trigger 1000: both NOT_TRIGGERED
    assert ev2["decision"] == "NO_PAYOUT"
    assert transfers == [(to_hex(direct_alice), 20 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["reserved"] == 0
    assert pool["balance"] == FUNDED + 2 * GEN + 3 * GEN - 20 * GEN
