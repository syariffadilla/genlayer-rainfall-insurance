"""buy_policy(quote_id): quote-bound purchase, premium, expiry, re-use, lookups."""

import json

from tests.direct.conftest import (
    FUNDED,
    GEN,
    LAT,
    LON,
    buy,
    fixture_text,
    pool_invariants,
    quote,
    to_hex,
)

AUG = ("2023-08-01", "2023-08-31")
AUG_HIST = "om_hist_aug_2018-2022.json"


def _real_quote(vm, guard, clock, farmer, payout=10 * GEN):
    return quote(vm, guard, clock, farmer, *AUG, drought=6000, flood=15000, payout=payout, body=fixture_text(AUG_HIST))


def test_buy_from_quote_records_everything(direct_vm, clock, guard, direct_alice):
    q = _real_quote(direct_vm, guard, clock, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = q["premium"]
    pid = guard.buy_policy(q["id"])
    direct_vm.value = 0
    assert pid == "1"
    p = guard.get_policy(pid)
    assert p["farmer"] == to_hex(direct_alice)
    assert (p["pool_id"], p["quote_id"]) == ("1", q["id"])
    assert (p["lat_micro"], p["lon_micro"]) == (LAT, LON)
    assert (p["start_date"], p["end_date"]) == AUG
    assert (p["drought_trigger_mm100"], p["flood_trigger_mm100"]) == (6000, 15000)
    assert p["payout"] == 10 * GEN
    assert p["premium"] == q["premium"] == 45 * GEN // 10 and p["rate_bps"] == 4500
    assert p["status"] == "ACTIVE" and p["purchased_on"] == "2023-07-01"
    assert (p["settle_opens"], p["settle_deadline"]) == ("2023-09-07", "2023-11-06")
    assert p["settle_attempts"] == 0 and p["paid_amount"] == 0 and p["evidence"] == ""
    assert guard.get_quote(q["id"])["policy_id"] == pid
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["total_premiums"] == q["premium"] and pool["reserved"] == 10 * GEN
    assert pool["policy_count"] == 1 and pool["active_policies"] == 1
    urls = guard.get_source_urls(pid)
    assert urls[0] == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400&longitude=108.220700"
        "&start_date=2023-08-01&end_date=2023-08-31&daily=precipitation_sum&timezone=auto"
    )
    assert urls[1] == (
        "https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG"
        "&longitude=108.220700&latitude=-7.327400&start=20230801&end=20230831&format=JSON"
    )
    s = guard.get_stats()
    assert (s["policy_count"], s["quote_count"], s["total_premiums"]) == (1, 1, q["premium"])


def test_buy_requires_exact_quoted_premium(direct_vm, clock, guard, direct_alice):
    q = _real_quote(direct_vm, guard, clock, direct_alice)
    direct_vm.sender = direct_alice
    for value in (0, q["premium"] - 1, q["premium"] + 1, GEN):
        direct_vm.value = value
        with direct_vm.expect_revert("Premium must equal " + str(q["premium"])):
            guard.buy_policy(q["id"])
    direct_vm.value = 0
    assert guard.get_stats()["policy_count"] == 0


def test_only_requester_can_use_quote(direct_vm, clock, guard, direct_alice, direct_bob):
    q = _real_quote(direct_vm, guard, clock, direct_alice)
    direct_vm.sender = direct_bob
    direct_vm.value = q["premium"]
    with direct_vm.expect_revert("Only the quote requester can buy"):
        guard.buy_policy(q["id"])
    direct_vm.value = 0


def test_quote_cannot_be_used_twice(direct_vm, clock, guard, direct_alice):
    q = _real_quote(direct_vm, guard, clock, direct_alice)
    direct_vm.sender = direct_alice
    direct_vm.value = q["premium"]
    guard.buy_policy(q["id"])
    with direct_vm.expect_revert("Quote already used"):
        guard.buy_policy(q["id"])
    direct_vm.value = 0
    assert guard.get_stats()["policy_count"] == 1


def test_quote_expires(direct_vm, clock, guard, direct_alice):
    q = _real_quote(direct_vm, guard, clock, direct_alice)
    assert q["valid_until"] == "2023-07-04"
    clock("2023-07-05")
    direct_vm.sender = direct_alice
    direct_vm.value = q["premium"]
    with direct_vm.expect_revert("Quote expired"):
        guard.buy_policy(q["id"])
    clock("2023-07-04")  # last valid day
    assert guard.buy_policy(q["id"]) == "1"
    direct_vm.value = 0


def test_buy_rechecks_coverage_start(direct_vm, clock, guard, direct_alice):
    # quoted on 2023-07-01 for a window starting 2023-07-02; buying on 2023-07-02 is too late
    q = quote(direct_vm, guard, clock, direct_alice, "2023-07-02", "2023-07-31", drought=6000)
    clock("2023-07-02")
    direct_vm.sender = direct_alice
    direct_vm.value = q["premium"]
    with direct_vm.expect_revert("Coverage must start after purchase date"):
        guard.buy_policy(q["id"])
    direct_vm.value = 0


def test_buy_rechecks_pool_open_and_liquidity(direct_vm, clock, guard, direct_alice, direct_bob, direct_owner):
    qa = _real_quote(direct_vm, guard, clock, direct_alice, payout=60 * GEN)
    qb = _real_quote(direct_vm, guard, clock, direct_bob, payout=60 * GEN)
    # insurer closes the pool after quoting
    direct_vm.sender = direct_owner
    guard.set_pool_accepting("1", False)
    direct_vm.sender = direct_alice
    direct_vm.value = qa["premium"]
    with direct_vm.expect_revert("Pool is not accepting new policies"):
        guard.buy_policy(qa["id"])
    direct_vm.sender = direct_owner
    direct_vm.value = 0
    guard.set_pool_accepting("1", True)
    direct_vm.sender = direct_alice
    direct_vm.value = qa["premium"]
    assert guard.buy_policy(qa["id"]) == "1"
    # free liquidity is now 100 + 27 - 60 = 67 GEN ... still enough for bob's 60
    # but not after the insurer withdraws
    direct_vm.sender = direct_owner
    direct_vm.value = 0
    guard.withdraw("1", 10 * GEN)
    direct_vm.sender = direct_bob
    direct_vm.value = qb["premium"]
    with direct_vm.expect_revert("Insufficient pool liquidity"):
        guard.buy_policy(qb["id"])
    direct_vm.value = 0


def test_farmer_lookups_and_policy_list(direct_vm, clock, guard, direct_alice, direct_bob):
    a1 = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=GEN)
    b1 = buy(direct_vm, guard, clock, direct_bob, *AUG, drought=6000, payout=2 * GEN)
    a2 = buy(direct_vm, guard, clock, direct_alice, "2023-09-01", "2023-09-30", flood=9000, payout=3 * GEN)
    assert [p["id"] for p in guard.get_farmer_policies(to_hex(direct_alice))] == [a1, a2]
    assert [p["id"] for p in guard.get_farmer_policies(to_hex(direct_bob))] == [b1]
    # address lookup is case-insensitive
    assert [p["id"] for p in guard.get_farmer_policies(to_hex(direct_alice).lower())] == [a1, a2]
    assert guard.get_farmer_policies("0x" + "22" * 20) == []
    assert [q["policy_id"] for q in guard.get_farmer_quotes(to_hex(direct_alice))] == [a1, a2]
    assert [p["id"] for p in guard.list_policies(0, 50)] == ["1", "2", "3"]
    assert [p["id"] for p in guard.list_policies(2, 5)] == ["3"]
    assert [p["id"] for p in guard.get_pool_policies("1")] == ["1", "2", "3"]
    assert guard.get_pool("1")["active_policies"] == 3
