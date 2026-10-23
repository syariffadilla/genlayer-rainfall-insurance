"""Pool accounting, premiums, and buy_policy input validation."""

from tests.direct.conftest import (
    CONTRACT,
    GEN,
    LAT,
    LON,
    RATE_BPS,
    assert_pool_invariants,
    buy,
    to_hex,
)

FUNDED = 100 * GEN


# ---------------------------------------------------------------- deploy / pool


def test_deploy_rejects_zero_rate(direct_vm, direct_deploy):
    with direct_vm.expect_revert("Invalid premium rate"):
        direct_deploy(CONTRACT, 0)


def test_deploy_rejects_rate_above_100_percent(direct_vm, direct_deploy):
    with direct_vm.expect_revert("Invalid premium rate"):
        direct_deploy(CONTRACT, 10001)


def test_initial_pool_state(insurance, direct_owner):
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["insurer"] == to_hex(direct_owner)
    assert pool["premium_rate_bps"] == RATE_BPS
    assert pool["pool_balance"] == FUNDED
    assert pool["reserved"] == 0
    assert pool["free_liquidity"] == FUNDED
    assert pool["policy_count"] == 0


def test_fund_pool_only_insurer_and_nonzero(direct_vm, insurance, direct_alice, direct_owner):
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    with direct_vm.expect_revert("Only insurer"):
        insurance.fund_pool()
    direct_vm.sender = direct_owner
    direct_vm.value = 0
    with direct_vm.expect_revert("Send GEN to fund the pool"):
        insurance.fund_pool()
    direct_vm.value = 5 * GEN
    assert insurance.fund_pool() == FUNDED + 5 * GEN
    direct_vm.value = 0
    assert_pool_invariants(insurance, FUNDED + 5 * GEN)


def test_quote_premium_rounds_up(insurance, direct_vm):
    assert insurance.quote_premium(10 * GEN) == GEN  # 10 %
    assert insurance.quote_premium(1) == 1  # ceil(0.1) = 1 wei
    assert insurance.quote_premium(15) == 2  # ceil(1.5)
    with direct_vm.expect_revert("Invalid payout"):
        insurance.quote_premium(0)


def test_set_premium_rate(direct_vm, insurance, direct_alice, direct_owner):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only insurer"):
        insurance.set_premium_rate(500)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Invalid premium rate"):
        insurance.set_premium_rate(0)
    insurance.set_premium_rate(500)
    assert insurance.quote_premium(10 * GEN) == GEN // 2


def test_withdraw_only_unreserved(direct_vm, insurance, direct_alice, direct_owner, transfers):
    pid = buy(direct_vm, insurance, direct_alice, "2023-08-01", "2023-08-31", drought=6000, payout=40 * GEN)
    assert pid == "1"
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["pool_balance"] == FUNDED + 4 * GEN
    assert pool["reserved"] == 40 * GEN
    assert pool["free_liquidity"] == 64 * GEN

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only insurer"):
        insurance.withdraw(GEN)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Invalid amount"):
        insurance.withdraw(0)
    with direct_vm.expect_revert("Amount exceeds unreserved funds"):
        insurance.withdraw(64 * GEN + 1)
    assert insurance.withdraw(64 * GEN) == 40 * GEN
    assert transfers == [(to_hex(direct_owner), 64 * GEN)]
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["free_liquidity"] == 0
    assert pool["reserved"] == 40 * GEN
    assert pool["total_withdrawn"] == 64 * GEN


# ---------------------------------------------------------------- buy_policy


def test_buy_policy_records_everything(direct_vm, insurance, direct_alice):
    pid = buy(direct_vm, insurance, direct_alice, "2023-08-01", "2023-08-31", drought=6000, flood=15000)
    p = insurance.get_policy(pid)
    assert p["farmer"] == to_hex(direct_alice)
    assert (p["lat_micro"], p["lon_micro"]) == (LAT, LON)
    assert (p["start_date"], p["end_date"]) == ("2023-08-01", "2023-08-31")
    assert (p["drought_trigger_mm100"], p["flood_trigger_mm100"]) == (6000, 15000)
    assert p["payout"] == 10 * GEN and p["premium"] == GEN
    assert p["status"] == "ACTIVE"
    assert p["purchased_on"] == "2023-07-01"
    assert p["settle_opens"] == "2023-09-07"
    assert p["settle_deadline"] == "2023-11-06"
    assert p["settle_attempts"] == 0 and p["paid_amount"] == 0
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["total_premiums"] == GEN and pool["reserved"] == 10 * GEN
    urls = insurance.get_source_urls(pid)
    assert urls[0] == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=-7.327400&longitude=108.220700"
        "&start_date=2023-08-01&end_date=2023-08-31&daily=precipitation_sum&timezone=auto"
    )
    assert urls[1] == (
        "https://power.larc.nasa.gov/api/temporal/daily/point?parameters=PRECTOTCORR&community=AG"
        "&longitude=108.220700&latitude=-7.327400&start=20230801&end=20230831&format=JSON"
    )


def test_buy_requires_exact_premium(direct_vm, insurance, direct_alice):
    direct_vm.sender = direct_alice
    for value in (0, GEN - 1, GEN + 1):
        direct_vm.value = value
        with direct_vm.expect_revert("Premium must equal " + str(GEN)):
            insurance.buy_policy(LAT, LON, "2023-08-01", "2023-08-31", 6000, 0, 10 * GEN)
    direct_vm.value = 0
    assert insurance.get_pool()["policy_count"] == 0


def test_buy_insufficient_pool(direct_vm, insurance, direct_alice, direct_bob):
    buy(direct_vm, insurance, direct_alice, "2023-08-01", "2023-08-31", drought=6000, payout=60 * GEN)
    # free = 100 + 6 - 60 = 46 GEN
    direct_vm.sender = direct_bob
    direct_vm.value = insurance.quote_premium(47 * GEN)
    with direct_vm.expect_revert("Insufficient pool liquidity"):
        insurance.buy_policy(LAT, LON, "2023-08-01", "2023-08-31", 6000, 0, 47 * GEN)
    direct_vm.value = 0
    pid = buy(direct_vm, insurance, direct_bob, "2023-08-01", "2023-08-31", drought=6000, payout=46 * GEN)
    assert pid == "2"
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["reserved"] == 106 * GEN
    # premiums (6 + 4.6 GEN) stay in the pool and back future policies
    assert pool["total_premiums"] == 6 * GEN + insurance.quote_premium(46 * GEN)
    assert pool["free_liquidity"] == FUNDED + pool["total_premiums"] - 106 * GEN


def _expect_buy_revert(vm, c, sender, msg, lat=LAT, lon=LON, start="2023-08-01", end="2023-08-31",
                       drought=6000, flood=0, payout=10 * GEN):
    vm.sender = sender
    vm.value = (payout * RATE_BPS + 9999) // 10000 if payout > 0 else 0
    with vm.expect_revert(msg):
        c.buy_policy(lat, lon, start, end, drought, flood, payout)
    vm.value = 0


def test_buy_invalid_coordinates(direct_vm, insurance, direct_alice):
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid latitude", lat=90000001)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid latitude", lat=-90000001)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid longitude", lon=180000001)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid longitude", lon=-180000001)


def test_buy_invalid_dates(direct_vm, insurance, direct_alice):
    bad = "Invalid date format"
    for start, end in [
        ("2023-8-01", "2023-08-31"),
        ("2023/08/01", "2023-08-31"),
        ("2023-02-30", "2023-03-31"),
        ("2023-08-01", "20230831"),
        ("", "2023-08-31"),
    ]:
        _expect_buy_revert(direct_vm, insurance, direct_alice, bad, start=start, end=end)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "end_date before start_date",
                       start="2023-08-31", end="2023-08-01")
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Coverage window too long",
                       start="2023-08-01", end="2024-02-01")
    # clock is 2023-07-01: starting today / in the past is rejected (no adverse selection)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Coverage must start after purchase date",
                       start="2023-07-01", end="2023-07-31")
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Coverage must start after purchase date",
                       start="2023-06-01", end="2023-07-31")
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Coverage starts too far ahead",
                       start="2024-07-01", end="2024-07-31")
    # boundary: exactly 184 days and start tomorrow are accepted
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    assert insurance.buy_policy(LAT, LON, "2023-07-02", "2024-01-01", 6000, 0, 10 * GEN) == "1"
    direct_vm.value = 0


def test_buy_invalid_triggers_and_payout(direct_vm, insurance, direct_alice):
    _expect_buy_revert(direct_vm, insurance, direct_alice, "At least one trigger required", drought=0, flood=0)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid trigger", drought=-1)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid trigger", flood=-5)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid payout", payout=0)
    _expect_buy_revert(direct_vm, insurance, direct_alice, "Invalid payout", payout=-10)
    assert insurance.get_pool()["policy_count"] == 0
    assert_pool_invariants(insurance, FUNDED)


def test_unknown_policy(direct_vm, insurance):
    with direct_vm.expect_revert("Unknown policy"):
        insurance.get_policy("42")
    with direct_vm.expect_revert("Unknown policy"):
        insurance.settle("42")
    with direct_vm.expect_revert("Unknown policy"):
        insurance.expire("42")
    with direct_vm.expect_revert("Unknown policy"):
        insurance.cancel_policy("42")
