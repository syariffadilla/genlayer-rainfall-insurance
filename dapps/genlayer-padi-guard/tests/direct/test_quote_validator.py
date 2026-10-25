"""Validator equivalence check of request_quote()'s gl.vm.run_nondet block.

direct_vm.run_validator() re-runs the captured validator_fn against the
CURRENT web mocks and the leader's history evidence.
"""

import copy
import json

import pytest

from tests.direct.conftest import contract_module, fixture_text, mock_history, quote, synthetic_history

AUG = ("2023-08-01", "2023-08-31")
AUG_HIST = "om_hist_aug_2018-2022.json"


@pytest.fixture
def quoted(direct_vm, clock, guard, direct_alice):
    q = quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, flood=15000, body=fixture_text(AUG_HIST))
    assert q["hits"] == 2
    return json.loads(q["history"])


def _swap(vm, clock, body, status=200):
    vm.clear_mocks()
    mock_history(vm, *AUG, clock.state["today"], body=body, status=status)


def _hist_with(day: str, delta_mm: float) -> str:
    h = json.loads(fixture_text(AUG_HIST))
    i = h["daily"]["time"].index(day)
    h["daily"]["precipitation_sum"][i] = round(h["daily"]["precipitation_sum"][i] + delta_mm, 2)
    return json.dumps(h)


def test_agrees_on_identical_data(direct_vm, quoted):
    assert direct_vm.run_validator() is True


def test_agrees_within_tolerance(direct_vm, clock, quoted):
    _swap(direct_vm, clock, _hist_with("2022-08-10", 1.5))  # +1.5 mm on one 2022 day
    assert direct_vm.run_validator() is True


def test_rejects_out_of_tolerance_year(direct_vm, clock, quoted):
    _swap(direct_vm, clock, _hist_with("2021-08-10", 40.0))  # 2021 total +40 mm (> 5 %)
    assert direct_vm.run_validator() is False


def test_rejects_when_its_data_flips_a_year(direct_vm, clock, quoted):
    # 2019: 44.90 mm -> 59.90 mm stays a hit, but +16 mm (60.90) flips it AND is out of tolerance
    _swap(direct_vm, clock, _hist_with("2019-08-10", 16.0))
    assert direct_vm.run_validator() is False


def test_rejects_when_source_unreachable_for_validator(direct_vm, clock, quoted):
    _swap(direct_vm, clock, "down", status=503)
    assert direct_vm.run_validator() is False


def test_rejects_when_validator_sees_a_missing_day(direct_vm, clock, quoted):
    h = json.loads(fixture_text(AUG_HIST))
    h["daily"]["precipitation_sum"][h["daily"]["time"].index("2020-08-15")] = None
    _swap(direct_vm, clock, json.dumps(h))
    assert direct_vm.run_validator() is False


def test_rejects_leader_error(direct_vm, quoted):
    assert direct_vm.run_validator(leader_error=Exception("boom")) is False


def _tampered(ev, path, value):
    t = copy.deepcopy(ev)
    obj = t
    for key in path[:-1]:
        obj = obj[key]
    obj[path[-1]] = value
    return t


TAMPER = [
    (("lat_micro",), -7327401),
    (("lon_micro",), 108220701),
    (("windows", 0, 0), "2022-08-02"),
    (("windows", 4, 1), "2018-08-30"),
    (("source",), "nasa-power"),
    (("status",), "incomplete"),
    (("years", 0, "start_date"), "2022-08-02"),
    (("years", 1, "end_date"), "2021-08-30"),
    (("years", 2, "expected_days"), 30),
    (("years", 3, "days"), 30),
    (("years", 0, "total_mm100"), 23520 + 2000),
    (("years", 4, "max_mm100"), 510 + 201),
    (("years", 1, "total_mm100"), -1),
    (("years", 2, "max_mm100"), True),
    (("hit_flags", 0), True),
    (("hit_flags", 3), False),
    (("hits",), 1),
    (("hits",), 0),
]


@pytest.mark.parametrize("path,value", TAMPER, ids=["/".join(map(str, p)) for p, _ in TAMPER])
def test_rejects_tampered_field(direct_vm, quoted, path, value):
    assert direct_vm.run_validator(leader_result=_tampered(quoted, path, value)) is False


def test_rejects_dropped_year(direct_vm, quoted):
    t = copy.deepcopy(quoted)
    t["years"] = t["years"][:4]
    assert direct_vm.run_validator(leader_result=t) is False


def test_rejects_internally_consistent_lie(direct_vm, quoted):
    # Leader claims 2019 and 2018 were wet and recomputes self-consistent flags (0 hits -> cheap premium)
    mod = contract_module()
    t = copy.deepcopy(quoted)
    t["years"][3]["total_mm100"] = 9000
    t["years"][4]["total_mm100"] = 9000
    t.update(mod._history_decide(t["status"], t["years"], 6000, 15000))
    assert t["hits"] == 0
    assert direct_vm.run_validator(leader_result=t) is False


def test_accepts_small_drift_in_leader_values(direct_vm, quoted):
    t = _tampered(quoted, ("years", 0, "total_mm100"), 23520 + 150)
    assert direct_vm.run_validator(leader_result=t) is True


def test_agrees_on_incomplete_history_when_both_see_it(direct_vm, clock, guard, direct_alice):
    # leader and validator both see a gap -> they agree on "incomplete"; the contract then refuses the quote
    trunc = synthetic_history("2018-08-01", "2021-08-31", 500)
    with direct_vm.expect_revert("Rainfall history incomplete"):
        quote(direct_vm, guard, clock, direct_alice, *AUG, drought=6000, body=trunc)
    assert direct_vm.run_validator() is True
