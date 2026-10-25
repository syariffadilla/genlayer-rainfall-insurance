"""Validator equivalence check of settle()'s gl.vm.run_nondet block.

direct_vm.run_validator() runs the captured validator_fn the way a GenLayer
validator would: it re-fetches both sources itself (using the CURRENT mocks)
and compares against the leader's evidence.
"""

import copy
import json

import pytest

from tests.direct.conftest import GEN, buy, contract_module, fixture_text, mock_month, mock_nasa, mock_open_meteo

AUG = ("2023-08-01", "2023-08-31")


@pytest.fixture
def settled(direct_vm, clock, guard, direct_alice):
    """A drought policy settled (PAYOUT) on real Aug-2023 data; returns leader evidence."""
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, flood=15000, payout=10 * GEN)
    clock("2023-09-10")
    mock_month(direct_vm, "2023-08", *AUG)
    ev = guard.settle(pid)
    assert ev["decision"] == "PAYOUT"
    return ev


def _swap_mocks(vm, om_body=None, nasa_body=None, nasa_status=200):
    vm.clear_mocks()
    mock_open_meteo(vm, *AUG, om_body if om_body is not None else fixture_text("om_2023-08.json"))
    mock_nasa(vm, *AUG, nasa_body if nasa_body is not None else fixture_text("nasa_2023-08.json"), status=nasa_status)


def _om_with_delta(delta_mm_day0: str) -> str:
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["precipitation_sum"][0] = float(delta_mm_day0)
    return json.dumps(om)


def _nasa_scaled(factor: float) -> str:
    nasa = json.loads(fixture_text("nasa_2023-08.json"))
    series = nasa["properties"]["parameter"]["PRECTOTCORR"]
    for k in series:
        series[k] = round(series[k] * factor, 2)
    return json.dumps(nasa)


# ---------------------------------------------------------------- agreement


def test_validator_agrees_on_identical_data(direct_vm, settled):
    assert direct_vm.run_validator() is True


def test_validator_agrees_within_tolerance(direct_vm, settled):
    # validator sees a revised Open-Meteo day 1: 0.00 -> 1.50 mm (+1.5 mm total, <= 2 mm abs tol)
    om = json.loads(fixture_text("om_2023-08.json"))
    first = om["daily"]["precipitation_sum"][0]
    _swap_mocks(direct_vm, om_body=_om_with_delta(str(first + 1.5)))
    assert direct_vm.run_validator() is True


def test_validator_agrees_within_pct_tolerance(direct_vm, settled):
    # NASA values +4 % across the board: total 13.99 -> ~14.55 mm (< 2 mm), max ok
    _swap_mocks(direct_vm, nasa_body=_nasa_scaled(1.04))
    assert direct_vm.run_validator() is True


# ---------------------------------------------------------------- disagreement on data


def test_validator_rejects_out_of_tolerance_total(direct_vm, settled):
    # validator's NASA data is 60 % higher (13.99 -> 22.38 mm): beyond 2 mm / 5 %
    _swap_mocks(direct_vm, nasa_body=_nasa_scaled(1.6))
    assert direct_vm.run_validator() is False


def test_validator_rejects_when_it_cannot_see_a_source(direct_vm, settled):
    _swap_mocks(direct_vm, nasa_body="Service Unavailable", nasa_status=503)
    assert direct_vm.run_validator() is False


def test_validator_rejects_when_its_data_flips_the_decision(direct_vm, settled):
    # validator sees Open-Meteo total 4840 -> 9840 (+50 mm on day 1): values and decision differ
    om = json.loads(fixture_text("om_2023-08.json"))
    om["daily"]["precipitation_sum"][0] = om["daily"]["precipitation_sum"][0] + 50.0
    _swap_mocks(direct_vm, om_body=json.dumps(om))
    assert direct_vm.run_validator() is False


def test_validator_rejects_leader_error(direct_vm, settled):
    assert direct_vm.run_validator(leader_error=Exception("boom")) is False


# ---------------------------------------------------------------- every bound field


def _tampered(ev, path, value):
    t = copy.deepcopy(ev)
    obj = t
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    return t


TAMPER_CASES = [
    (("policy_id",), "2"),
    (("lat_micro",), -7327401),
    (("lon_micro",), 108220701),
    (("start_date",), "2023-08-02"),
    (("end_date",), "2023-08-30"),
    (("expected_days",), 30),
    (("sources", 0, "days"), 30),
    (("sources", 1, "days"), 30),
    (("sources", 0, "status"), "incomplete"),
    (("sources", 1, "name"), "open-meteo"),
    (("sources", 0, "total_mm100"), 4840 + 300),
    (("sources", 1, "total_mm100"), 1399 + 201),
    (("sources", 0, "max_mm100"), 880 + 201),
    (("sources", 1, "max_mm100"), -1),
    (("sources_ok",), 1),
    (("sources_used",), ["open-meteo"]),
    (("drought",), "NOT_TRIGGERED"),
    (("flood",), "TRIGGERED"),
    (("decided_total_mm100",), 0),
    (("decided_max_mm100",), 99999),
    (("decision",), "NO_PAYOUT"),
    (("defer_reason",), "SOURCE_MISSING"),
    (("payout_wei",), 10 * GEN + 1),
    (("payout_wei",), 0),
]


@pytest.mark.parametrize("path,value", TAMPER_CASES, ids=["/".join(map(str, p)) for p, _ in TAMPER_CASES])
def test_validator_rejects_tampered_field(direct_vm, settled, path, value):
    assert direct_vm.run_validator(leader_result=_tampered(settled, path, value)) is False


def test_validator_rejects_dropped_source(direct_vm, settled):
    t = copy.deepcopy(settled)
    t["sources"] = t["sources"][:1]
    assert direct_vm.run_validator(leader_result=t) is False


def test_validator_rejects_internally_consistent_lie(direct_vm, settled):
    # Leader claims NASA reported a wet month and recomputes a self-consistent NO_PAYOUT.
    mod = contract_module()
    t = copy.deepcopy(settled)
    t["sources"][0]["total_mm100"] = 7000
    t["sources"][1]["total_mm100"] = 7000
    t.update(mod._decide(t["sources"], 6000, 15000, 10 * GEN))
    assert t["decision"] == "NO_PAYOUT"
    assert direct_vm.run_validator(leader_result=t) is False


def test_validator_rejects_consistent_stall(direct_vm, settled):
    # Leader inflates NASA so the sources "diverge" and settlement is deferred (stall attack).
    mod = contract_module()
    t = copy.deepcopy(settled)
    t["sources"][1]["total_mm100"] = 9000
    t.update(mod._decide(t["sources"], 6000, 15000, 10 * GEN))
    assert t["decision"] == "DEFER"
    assert direct_vm.run_validator(leader_result=t) is False


def test_validator_accepts_small_drift_in_leader_values(direct_vm, settled):
    # leader numbers off by 1.00 mm (within 2 mm) with the same decision -> still agree
    t = _tampered(settled, ("sources", 0, "total_mm100"), 4840 + 100)
    mod = contract_module()
    t.update(mod._decide(t["sources"], 6000, 15000, 10 * GEN))
    assert t["decision"] == "PAYOUT"
    assert direct_vm.run_validator(leader_result=t) is True


def test_validator_on_deferred_settlement(direct_vm, clock, guard, direct_alice):
    # Validators also have to agree on DEFER (otherwise a leader could stall a payout)
    pid = buy(direct_vm, guard, clock, direct_alice, *AUG, drought=6000)
    clock("2023-09-10")
    mock_open_meteo(direct_vm, *AUG, fixture_text("om_2023-08.json"))
    mock_nasa(direct_vm, *AUG, "err", status=500)
    ev = guard.settle(pid)
    assert ev["decision"] == "DEFER"
    assert direct_vm.run_validator() is True  # validator also cannot reach NASA
    _swap_mocks(direct_vm)  # validator CAN reach NASA -> leader's DEFER is rejected
    assert direct_vm.run_validator() is False
