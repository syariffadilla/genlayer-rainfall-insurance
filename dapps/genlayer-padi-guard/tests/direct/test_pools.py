"""Insurer pools: creation, funding, withdrawals, pricing, open/close, isolation, list views."""

from tests.direct.conftest import (
    CONTRACT,
    FUNDED,
    GEN,
    LAT,
    LON,
    LOADING_BPS,
    MIN_RATE_BPS,
    WORLD,
    buy,
    fixture_text,
    mock_month,
    pool_invariants,
    premium10,
    to_hex,
)

# West Java bounding box (micro-degrees)
WEST_JAVA = (-7900000, -5900000, 106300000, 108900000)
AUG = ("2023-08-01", "2023-08-31")


def _create(vm, c, insurer, name="Pool", min_rate=MIN_RATE_BPS, loading=LOADING_BPS, box=WORLD, value=0):
    vm.sender = insurer
    vm.value = value
    try:
        return c.create_pool(name, min_rate, loading, *box)
    finally:
        vm.value = 0


# ---------------------------------------------------------------- create


def test_fresh_deploy_is_empty(direct_vm, direct_deploy):
    c = direct_deploy(CONTRACT)
    s = c.get_stats()
    assert (s["pool_count"], s["quote_count"], s["policy_count"]) == (0, 0, 0)
    assert s["total_balance"] == 0 and s["quote_years"] == 5
    assert c.list_pools(0, 10) == []
    assert c.list_policies(0, 10) == []


def test_initial_pool_state(guard, direct_owner):
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["id"] == "1"
    assert pool["insurer"] == to_hex(direct_owner)
    assert pool["name"] == "West Java Padi Pool"
    assert (pool["min_rate_bps"], pool["loading_bps"]) == (MIN_RATE_BPS, LOADING_BPS)
    assert (pool["lat_min"], pool["lat_max"], pool["lon_min"], pool["lon_max"]) == WORLD
    assert pool["accepting"] is True
    assert pool["balance"] == FUNDED and pool["total_deposits"] == FUNDED
    assert pool["reserved"] == 0 and pool["free_liquidity"] == FUNDED
    assert pool["policy_count"] == 0 and pool["active_policies"] == 0


def test_anyone_can_open_a_pool_without_initial_deposit(direct_vm, guard, direct_bob):
    pid = _create(direct_vm, guard, direct_bob, name="  Bob Mutual  ", min_rate=200, loading=0, box=WEST_JAVA)
    assert pid == "2"
    pool = guard.get_pool("2")
    assert pool["insurer"] == to_hex(direct_bob)
    assert pool["name"] == "Bob Mutual"  # trimmed
    assert pool["balance"] == 0 and pool["total_deposits"] == 0
    assert [p["id"] for p in guard.get_insurer_pools(to_hex(direct_bob))] == ["2"]
    assert guard.get_stats()["pool_count"] == 2


def test_create_pool_input_validation(direct_vm, guard, direct_bob):
    cases = [
        (dict(name=""), "Invalid pool name"),
        (dict(name="   "), "Invalid pool name"),
        (dict(name="x" * 65), "Invalid pool name"),
        (dict(min_rate=0), "Invalid minimum rate"),
        (dict(min_rate=10001), "Invalid minimum rate"),
        (dict(loading=-1), "Invalid loading"),
        (dict(loading=10001), "Invalid loading"),
        (dict(box=(-90000001, 0, 0, 1)), "Invalid latitude range"),
        (dict(box=(1, 0, 0, 1)), "Invalid latitude range"),
        (dict(box=(0, 90000001, 0, 1)), "Invalid latitude range"),
        (dict(box=(0, 1, -180000001, 0)), "Invalid longitude range"),
        (dict(box=(0, 1, 5, 4)), "Invalid longitude range"),
    ]
    for kw, msg in cases:
        direct_vm.sender = direct_bob
        with direct_vm.expect_revert(msg):
            guard.create_pool(
                kw.get("name", "ok"),
                kw.get("min_rate", 100),
                kw.get("loading", 0),
                *kw.get("box", WORLD),
            )
    assert guard.get_stats()["pool_count"] == 1


# ---------------------------------------------------------------- fund / withdraw


def test_fund_pool_only_insurer_and_nonzero(direct_vm, guard, direct_alice, direct_owner):
    direct_vm.sender = direct_alice
    direct_vm.value = GEN
    with direct_vm.expect_revert("Only pool insurer"):
        guard.fund_pool("1")
    direct_vm.sender = direct_owner
    direct_vm.value = 0
    with direct_vm.expect_revert("Send GEN to fund the pool"):
        guard.fund_pool("1")
    direct_vm.value = GEN
    with direct_vm.expect_revert("Unknown pool"):
        guard.fund_pool("9")
    direct_vm.value = 5 * GEN
    assert guard.fund_pool("1") == FUNDED + 5 * GEN
    direct_vm.value = 0
    pool_invariants(guard, "1", FUNDED + 5 * GEN)


def test_withdraw_only_unreserved(direct_vm, clock, guard, direct_alice, direct_owner, transfers):
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=40 * GEN)
    assert pid == "1"
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["balance"] == FUNDED + 4 * GEN
    assert pool["reserved"] == 40 * GEN
    assert pool["free_liquidity"] == 64 * GEN

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only pool insurer"):
        guard.withdraw("1", GEN)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Invalid amount"):
        guard.withdraw("1", 0)
    with direct_vm.expect_revert("Amount exceeds unreserved funds"):
        guard.withdraw("1", 64 * GEN + 1)
    assert guard.withdraw("1", 64 * GEN) == 40 * GEN
    assert transfers == [(to_hex(direct_owner), 64 * GEN)]
    pool = pool_invariants(guard, "1", FUNDED)
    assert pool["free_liquidity"] == 0
    assert pool["reserved"] == 40 * GEN
    assert pool["total_withdrawn"] == 64 * GEN


# ---------------------------------------------------------------- pricing / open-close


def test_set_pool_pricing_only_insurer_and_valid(direct_vm, guard, direct_alice, direct_owner):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only pool insurer"):
        guard.set_pool_pricing("1", 500, 0)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Invalid minimum rate"):
        guard.set_pool_pricing("1", 0, 0)
    with direct_vm.expect_revert("Invalid loading"):
        guard.set_pool_pricing("1", 500, -1)
    guard.set_pool_pricing("1", 500, 250)
    pool = guard.get_pool("1")
    assert (pool["min_rate_bps"], pool["loading_bps"]) == (500, 250)


def test_closed_pool_rejects_quotes_and_purchases(direct_vm, clock, guard, direct_alice, direct_owner):
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only pool insurer"):
        guard.set_pool_accepting("1", False)
    direct_vm.sender = direct_owner
    guard.set_pool_accepting("1", False)
    assert guard.get_pool("1")["accepting"] is False
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Pool is not accepting new policies"):
        guard.request_quote("1", LAT, LON, *AUG, 6000, 0, 10 * GEN)
    direct_vm.sender = direct_owner
    guard.set_pool_accepting("1", True)
    assert buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000) == "1"


def test_pool_region_is_enforced(direct_vm, clock, guard, direct_bob, direct_alice):
    _create(direct_vm, guard, direct_bob, name="West Java only", box=WEST_JAVA, value=50 * GEN)
    direct_vm.sender = direct_alice
    # Tasikmalaya is inside the box ...
    assert buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, pool_id="2") == "1"
    # ... Surabaya (East Java, -7.2575, 112.7521) is not
    with direct_vm.expect_revert("Location outside pool coverage region"):
        guard.request_quote("2", -7257500, 112752100, *AUG, 6000, 0, 10 * GEN)
    # one micro-degree south of the box is outside
    with direct_vm.expect_revert("Location outside pool coverage region"):
        guard.request_quote("2", WEST_JAVA[0] - 1, LON, *AUG, 6000, 0, 10 * GEN)


# ---------------------------------------------------------------- isolation between pools


def test_pools_are_isolated(direct_vm, clock, guard, direct_bob, direct_alice, direct_owner, transfers):
    _create(direct_vm, guard, direct_bob, name="Small pool", value=5 * GEN)
    # pool 2 cannot cover 10 GEN even though pool 1 holds 100 GEN
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Insufficient pool liquidity"):
        guard.request_quote("2", LAT, LON, *AUG, 6000, 0, 10 * GEN)
    p2 = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=5 * GEN, pool_id="2")
    p1 = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, payout=10 * GEN, pool_id="1")
    assert guard.get_policy(p2)["pool_id"] == "2" and guard.get_policy(p1)["pool_id"] == "1"
    # pool 2's insurer cannot touch pool 1 and vice versa
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only pool insurer"):
        guard.withdraw("1", GEN)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Only pool insurer"):
        guard.withdraw("2", 1)

    # settle the pool-2 policy (drought, real Aug-2023 data -> PAYOUT): only pool 2 pays
    clock("2023-09-07")
    mock_month(direct_vm, "2023-08", *AUG)
    ev = guard.settle(p2)
    assert ev["decision"] == "PAYOUT"
    assert transfers == [(to_hex(direct_alice), 5 * GEN)]
    pool2 = pool_invariants(guard, "2", 5 * GEN)
    assert pool2["balance"] == 5 * GEN + premium10(5 * GEN) - 5 * GEN
    assert pool2["reserved"] == 0 and pool2["total_paid_out"] == 5 * GEN
    pool1 = pool_invariants(guard, "1", FUNDED)
    assert pool1["balance"] == FUNDED + GEN and pool1["reserved"] == 10 * GEN
    assert pool1["total_paid_out"] == 0
    s = guard.get_stats()
    assert s["total_paid_out"] == 5 * GEN
    assert s["total_balance"] == pool1["balance"] + pool2["balance"]
    assert s["total_reserved"] == 10 * GEN
    assert [p["id"] for p in guard.get_pool_policies("2")] == [p2]
    assert [p["id"] for p in guard.get_pool_policies("1")] == [p1]


# ---------------------------------------------------------------- list views


def test_list_pools_paging(direct_vm, guard, direct_bob):
    for i in range(3):
        _create(direct_vm, guard, direct_bob, name=f"P{i}")
    assert [p["id"] for p in guard.list_pools(0, 50)] == ["1", "2", "3", "4"]
    assert [p["id"] for p in guard.list_pools(1, 2)] == ["2", "3"]
    assert [p["id"] for p in guard.list_pools(3, 10)] == ["4"]
    assert guard.list_pools(10, 10) == []
    for off, lim in [(-1, 1), (0, 0), (0, 51)]:
        with direct_vm.expect_revert("Invalid page"):
            guard.list_pools(off, lim)
    assert [p["id"] for p in guard.get_insurer_pools(to_hex(direct_bob))] == ["2", "3", "4"]
    assert guard.get_insurer_pools("0x" + "11" * 20) == []


def test_unknown_ids(direct_vm, guard):
    with direct_vm.expect_revert("Unknown pool"):
        guard.get_pool("7")
    with direct_vm.expect_revert("Unknown pool"):
        guard.get_pool_policies("7")
    with direct_vm.expect_revert("Unknown pool"):
        guard.request_quote("7", LAT, LON, *AUG, 6000, 0, GEN)
    with direct_vm.expect_revert("Unknown quote"):
        guard.get_quote("1")
    with direct_vm.expect_revert("Unknown quote"):
        guard.buy_policy("1")
    with direct_vm.expect_revert("Unknown policy"):
        guard.get_policy("42")
    with direct_vm.expect_revert("Unknown policy"):
        guard.settle("42")
    with direct_vm.expect_revert("Unknown policy"):
        guard.expire("42")
    with direct_vm.expect_revert("Unknown policy"):
        guard.cancel_policy("42")
