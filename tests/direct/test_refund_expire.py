"""cancel_policy (refund before coverage) and expire (claim window closed unsettled)."""

from tests.direct.conftest import GEN, assert_pool_invariants, buy, mock_month, to_hex

FUNDED = 100 * GEN
AUG = ("2023-08-01", "2023-08-31")
JAN = ("2025-01-01", "2025-01-31")


def test_cancel_before_start_refunds_premium(direct_vm, insurance, direct_alice, transfers):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000, payout=10 * GEN)
    direct_vm.warp("2023-07-31T23:00:00Z")
    direct_vm.sender = direct_alice
    assert insurance.cancel_policy(pid) == GEN
    p = insurance.get_policy(pid)
    assert p["status"] == "CANCELLED"
    assert transfers == [(to_hex(direct_alice), GEN)]
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["reserved"] == 0 and pool["pool_balance"] == FUNDED
    assert pool["total_refunded"] == GEN
    with direct_vm.expect_revert("Policy not active"):
        insurance.cancel_policy(pid)
    with direct_vm.expect_revert("Policy not active"):
        insurance.settle(pid)


def test_cancel_only_by_farmer(direct_vm, insurance, direct_alice, direct_bob, transfers):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only policy holder"):
        insurance.cancel_policy(pid)
    assert insurance.get_policy(pid)["status"] == "ACTIVE"
    assert transfers == []


def test_cancel_after_coverage_started_reverts(direct_vm, insurance, direct_alice, transfers):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-08-01T00:00:00Z")
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Coverage already started"):
        insurance.cancel_policy(pid)
    assert transfers == []
    assert insurance.get_pool()["reserved"] == 10 * GEN


def test_expire_before_claim_window_closes_reverts(direct_vm, insurance, direct_alice):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-11-06T23:59:59Z")  # last settle day
    with direct_vm.expect_revert("Claim window still open"):
        insurance.expire(pid)


def test_expire_deferred_policy_refunds_premium(direct_vm, insurance, direct_alice, direct_bob, transfers):
    # Jan 2025 flood 50 mm/day: sources diverge beyond tolerance -> DEFER every time
    direct_vm.warp("2024-12-15T00:00:00Z")
    pid = buy(direct_vm, insurance, direct_alice, *JAN, flood=5000, payout=10 * GEN)
    direct_vm.warp("2025-02-10T00:00:00Z")
    mock_month(direct_vm, "2025-01", *JAN)
    assert insurance.settle(pid)["decision"] == "DEFER"
    direct_vm.warp("2025-03-20T00:00:00Z")
    assert insurance.settle(pid)["decision"] == "DEFER"
    assert insurance.get_policy(pid)["settle_attempts"] == 2

    direct_vm.warp("2025-04-09T00:00:00Z")  # deadline = 2025-01-31 + 67 days = 2025-04-08
    direct_vm.sender = direct_bob  # anyone may expire; refund always goes to the farmer
    with direct_vm.expect_revert("Settlement deadline passed"):
        insurance.settle(pid)
    assert insurance.expire(pid) == GEN
    p = insurance.get_policy(pid)
    assert p["status"] == "EXPIRED_REFUNDED"
    assert transfers == [(to_hex(direct_alice), GEN)]
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["reserved"] == 0 and pool["pool_balance"] == FUNDED
    with direct_vm.expect_revert("Policy not active"):
        insurance.expire(pid)


def test_expire_never_settled_policy(direct_vm, insurance, direct_alice, transfers):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000, payout=3 * GEN)
    direct_vm.warp("2024-01-01T00:00:00Z")
    assert insurance.expire(pid) == insurance.quote_premium(3 * GEN)
    assert transfers == [(to_hex(direct_alice), 3 * GEN // 10)]
    assert_pool_invariants(insurance, FUNDED)


def test_expire_settled_policy_reverts(direct_vm, insurance, direct_alice, transfers):
    pid = buy(direct_vm, insurance, direct_alice, *AUG, drought=6000)
    direct_vm.warp("2023-09-10T00:00:00Z")
    mock_month(direct_vm, "2023-08", *AUG)
    insurance.settle(pid)
    direct_vm.warp("2024-01-01T00:00:00Z")
    with direct_vm.expect_revert("Policy not active"):
        insurance.expire(pid)
    assert len(transfers) == 1  # only the payout


def test_full_lifecycle_accounting(direct_vm, insurance, direct_owner, direct_accounts, transfers):
    """Five farmers, every terminal state, insurer withdraws exactly the free funds."""
    f = direct_accounts
    paid = buy(direct_vm, insurance, f[1], *AUG, drought=6000, payout=10 * GEN)
    nopay = buy(direct_vm, insurance, f[2], *AUG, drought=1000, payout=20 * GEN)
    cancel = buy(direct_vm, insurance, f[3], *AUG, drought=6000, payout=5 * GEN)
    expire = buy(direct_vm, insurance, f[4], *AUG, drought=6000, payout=7 * GEN)
    active = buy(direct_vm, insurance, f[5], "2023-12-01", "2023-12-31", drought=6000, payout=8 * GEN)
    premiums = GEN + 2 * GEN + GEN // 2 + 7 * GEN // 10 + 8 * GEN // 10
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["total_premiums"] == premiums
    assert pool["reserved"] == 50 * GEN

    direct_vm.sender = f[3]
    insurance.cancel_policy(cancel)
    direct_vm.warp("2023-09-10T00:00:00Z")
    mock_month(direct_vm, "2023-08", *AUG)
    insurance.settle(paid)
    insurance.settle(nopay)
    direct_vm.warp("2023-11-07T00:00:00Z")
    insurance.expire(expire)

    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["reserved"] == 8 * GEN  # only the December policy
    expected_balance = FUNDED + premiums - 10 * GEN - GEN // 2 - 7 * GEN // 10
    assert pool["pool_balance"] == expected_balance
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("Amount exceeds unreserved funds"):
        insurance.withdraw(expected_balance - 8 * GEN + 1)
    insurance.withdraw(expected_balance - 8 * GEN)
    pool = assert_pool_invariants(insurance, FUNDED)
    assert pool["pool_balance"] == 8 * GEN == pool["reserved"]
    assert insurance.get_policy(active)["status"] == "ACTIVE"
    statuses = [insurance.get_policy(p)["status"] for p in (paid, nopay, cancel, expire, active)]
    assert statuses == ["PAID_OUT", "SETTLED_NO_PAYOUT", "CANCELLED", "EXPIRED_REFUNDED", "ACTIVE"]
    assert transfers == [
        (to_hex(f[3]), GEN // 2),
        (to_hex(f[1]), 10 * GEN),
        (to_hex(f[4]), 7 * GEN // 10),
        (to_hex(direct_owner), expected_balance - 8 * GEN),
    ]
