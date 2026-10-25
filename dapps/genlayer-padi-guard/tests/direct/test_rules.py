"""Unit tests for the pure deterministic helpers (decision rules, parsing, units)."""

from decimal import Decimal

import pytest

from tests.direct.conftest import GEN, contract_module


@pytest.fixture
def mod(guard):
    return contract_module()


def _src(name, total, mx, status="ok", days=31):
    return {"name": name, "status": status, "days": days, "total_mm100": total, "max_mm100": mx}


def test_micro_degree_formatting(mod):
    assert mod._micro_to_str(-7327400) == "-7.327400"
    assert mod._micro_to_str(108220700) == "108.220700"
    assert mod._micro_to_str(5) == "0.000005"
    assert mod._micro_to_str(-5) == "-0.000005"
    assert mod._micro_to_str(0) == "0.000000"


def test_mm100_conversion(mod):
    assert mod._to_mm100(Decimal("17.00")) == 1700
    assert mod._to_mm100(Decimal("0.005")) == 1  # half-up
    assert mod._to_mm100(Decimal("82.08")) == 8208
    assert mod._to_mm100(0) == 0
    assert mod._to_mm100(Decimal("-999.0")) == -1  # NASA fill value
    assert mod._to_mm100(None) == -1
    assert mod._to_mm100(True) == -1
    assert mod._to_mm100("3.2") == -1


def test_within_tolerance(mod):
    assert mod._within(1000, 1200, 200, 5)  # abs
    assert not mod._within(1000, 1201, 200, 5)
    assert mod._within(100000, 105000, 200, 5)  # 5 % of max
    assert not mod._within(100000, 105300, 200, 5)


def test_both_agree_triggered(mod):
    d = mod._decide([_src("open-meteo", 4840, 880), _src("nasa-power", 1399, 508)], 6000, 0, 7)
    assert (d["drought"], d["flood"], d["decision"], d["payout_wei"]) == ("TRIGGERED", "DISABLED", "PAYOUT", 7)


def test_both_agree_not_triggered(mod):
    d = mod._decide([_src("open-meteo", 33430, 3150), _src("nasa-power", 41246, 8055)], 6000, 0, 7)
    assert (d["decision"], d["payout_wei"], d["defer_reason"]) == ("NO_PAYOUT", 0, "")


def test_threshold_is_strict(mod):
    # drought: total < trigger ; flood: max > trigger (equality does not trigger)
    d = mod._decide([_src("open-meteo", 6000, 5000), _src("nasa-power", 6000, 5000)], 6000, 5000, 1)
    assert (d["drought"], d["flood"], d["decision"]) == ("NOT_TRIGGERED", "NOT_TRIGGERED", "NO_PAYOUT")


def test_disagree_within_cross_tolerance_uses_mean(mod):
    d = mod._decide([_src("open-meteo", 33430, 0), _src("nasa-power", 41246, 0)], 40000, 0, 1)
    assert d["drought"] == "TRIGGERED" and d["decided_total_mm100"] == 37338


def test_disagree_beyond_cross_tolerance_defers(mod):
    d = mod._decide([_src("open-meteo", 0, 3150), _src("nasa-power", 0, 8055)], 0, 5000, 1)
    assert (d["flood"], d["decision"], d["defer_reason"]) == ("UNDECIDED", "DEFER", "SOURCES_DIVERGE")
    assert d["payout_wei"] == 0


def test_small_absolute_gap_uses_mean(mod):
    # 4.00 mm vs 12.00 mm differ by 200 % but only 8 mm (<= 10 mm abs cross tolerance)
    d = mod._decide([_src("open-meteo", 400, 0), _src("nasa-power", 1200, 0)], 1000, 0, 1)
    assert d["drought"] == "TRIGGERED" and d["decided_total_mm100"] == 800


@pytest.mark.parametrize("status", ["incomplete", "unavailable"])
def test_single_source_defers(mod, status):
    d = mod._decide([_src("open-meteo", 100, 10), _src("nasa-power", 0, 0, status=status, days=0)], 6000, 0, 1)
    assert (d["sources_ok"], d["sources_used"]) == (1, ["open-meteo"])
    assert (d["drought"], d["decision"], d["defer_reason"]) == ("UNDECIDED", "DEFER", "SOURCE_MISSING")


def test_no_sources_defers(mod):
    d = mod._decide([], 6000, 2000, 1)
    assert (d["drought"], d["flood"], d["decision"]) == ("UNDECIDED", "UNDECIDED", "DEFER")


def test_order_of_sources_does_not_matter(mod):
    a = mod._decide([_src("open-meteo", 4840, 880), _src("nasa-power", 1399, 508)], 6000, 0, GEN)
    b = mod._decide([_src("nasa-power", 1399, 508), _src("open-meteo", 4840, 880)], 6000, 0, GEN)
    assert a == b


def test_window_dates(mod):
    d = mod._window_dates("2024-02-27", "2024-03-01")
    assert d == ["2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01"]
