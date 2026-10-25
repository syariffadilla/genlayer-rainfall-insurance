"""Shared helpers for direct-mode tests of PadiGuard.

Web responses for settlement are REAL payloads captured from the two public
APIs with curl (see tests/direct/fixtures/README.md) for Tasikmalaya, West Java
(lat -7.327400, lon 108.220700). The quote tests use real multi-year
Open-Meteo history captures (om_hist_*.json).

Settlement-focused tests don't care about pricing, so `buy()` below mocks a
synthetic, clearly-labelled "no-hit" rainfall history (every history year
stays clear of the triggers) -> 0 historical hits -> the pool minimum rate
(10 %) applies, which keeps the premium arithmetic in those tests simple.
"""

import datetime
import json
import re
import sys
from pathlib import Path

# Pin the GenVM release used by gltest direct mode. Without this, gltest asks
# GitHub for the "latest" GenVM release, which is now a v0.3 release candidate
# that ships no genvm-universal tarball (HTTP 404), and every test errors on a
# fresh machine. The contract's py-genlayer hash belongs to the v0.2 line.
import os as _os
from gltest.direct import sdk_loader as _sdk_loader

GENVM_VERSION = _os.environ.get("GLTEST_GENVM_VERSION", "v0.2.16")
_sdk_loader.get_latest_version = lambda: GENVM_VERSION


import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = str(Path(__file__).resolve().parents[2] / "contracts" / "padi_guard.py")

GEN = 10**18
LAT = -7327400  # Tasikmalaya, West Java (micro-degrees)
LON = 108220700
MIN_RATE_BPS = 1000  # pool 1: 10 % minimum premium rate
LOADING_BPS = 500  # pool 1: +5 % on top of the historical hit rate
WORLD = (-90000000, 90000000, -180000000, 180000000)
FUNDED = 100 * GEN

OM_RE = r"archive-api\.open-meteo\.com/v1/archive\?latitude=-7\.327400&longitude=108\.220700&start_date={s}&end_date={e}&daily=precipitation_sum&timezone=auto$"
NASA_RE = r"power\.larc\.nasa\.gov/api/temporal/daily/point\?parameters=PRECTOTCORR&community=AG&longitude=108\.220700&latitude=-7\.327400&start={s}&end={e}&format=JSON$"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def fixture_json(name: str) -> dict:
    return json.loads(fixture_text(name))


def mock_open_meteo(vm, start, end, body=None, status=200):
    vm.mock_web(OM_RE.format(s=re.escape(start), e=re.escape(end)), {"status": status, "body": body or ""})


def mock_nasa(vm, start, end, body=None, status=200):
    vm.mock_web(
        NASA_RE.format(s=start.replace("-", ""), e=end.replace("-", "")),
        {"status": status, "body": body or ""},
    )


def mock_month(vm, month: str, start: str, end: str, om_body=None, nasa_body=None):
    """Register real fixture payloads for a settlement window (optionally overridden)."""
    mock_open_meteo(vm, start, end, om_body if om_body is not None else fixture_text(f"om_{month}.json"))
    mock_nasa(vm, start, end, nasa_body if nasa_body is not None else fixture_text(f"nasa_{month}.json"))


def contract_module():
    for name, mod in sys.modules.items():
        if name == "_contract_padi_guard":
            return mod
    raise RuntimeError("contract module not loaded")


def to_hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex
    from genlayer.py.types import Address

    return Address(addr).as_hex


def history_windows(start: str, end: str, today: str):
    return contract_module()._history_windows(start, end, datetime.date.fromisoformat(today))


def synthetic_history(range_start: str, range_end: str, daily_mm100: int, overrides=None) -> str:
    """Open-Meteo-shaped body with a constant daily value (SYNTHETIC, for pricing-neutral tests)."""
    s = datetime.date.fromisoformat(range_start)
    e = datetime.date.fromisoformat(range_end)
    times, vals = [], []
    d = s
    while d <= e:
        times.append(d.isoformat())
        vals.append(daily_mm100 / 100)
        d += datetime.timedelta(days=1)
    for k, v in (overrides or {}).items():
        vals[times.index(k)] = v
    return json.dumps({"daily_units": {"precipitation_sum": "mm"}, "daily": {"time": times, "precipitation_sum": vals}})


def mock_history(vm, start, end, today, body=None, status=200, daily_mm100=None, drought=0, flood=0):
    """Mock the single history request request_quote() makes for (start, end) on `today`."""
    windows = history_windows(start, end, today)
    rng = contract_module()._history_range(windows)
    if body is None:
        if daily_mm100 is None:
            days = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days + 1
            daily_mm100 = max(500, drought // days + 100)
            if flood > 0:
                assert daily_mm100 <= flood, "cannot build a no-hit history for these triggers"
        body = synthetic_history(rng[0], rng[1], daily_mm100)
    mock_open_meteo(vm, rng[0], rng[1], body, status=status)
    return windows, rng


def pool_invariants(c, pool_id: str, funded: int):
    pool = c.get_pool(pool_id)
    assert pool["reserved"] <= pool["balance"]
    assert pool["free_liquidity"] == pool["balance"] - pool["reserved"]
    assert pool["total_deposits"] == funded
    assert pool["balance"] == (
        pool["total_deposits"]
        + pool["total_premiums"]
        - pool["total_paid_out"]
        - pool["total_refunded"]
        - pool["total_withdrawn"]
    )
    return pool


@pytest.fixture
def transfers(direct_vm):
    """Capture native GEN transfers (emit_transfer -> EthSend) as (to_hex, value)."""
    sent = []

    def hook(vm, request):
        if isinstance(request, dict) and "EthSend" in request:
            data = request["EthSend"]
            to = data.get("address")
            sent.append((to_hex(to), int(data.get("value", 0))))
        return None

    direct_vm._gl_call_hook = hook
    return sent


@pytest.fixture
def clock(direct_vm):
    """Warp helper that remembers the current date for quote mocks."""
    state = {"today": None}

    def warp(day: str):
        direct_vm.warp(day + "T00:00:00Z")
        state["today"] = day

    warp.state = state
    return warp


@pytest.fixture
def guard(direct_vm, direct_deploy, direct_owner, clock):
    """Deployed PadiGuard with pool "1" (insurer = owner, 100 GEN, worldwide,
    min rate 10 %, loading 5 %). Clock at 2023-07-01."""
    clock("2023-07-01")
    direct_vm.sender = direct_owner
    c = direct_deploy(CONTRACT)
    direct_vm.value = FUNDED
    assert c.create_pool("West Java Padi Pool", MIN_RATE_BPS, LOADING_BPS, *WORLD) == "1"
    direct_vm.value = 0
    direct_vm.check_pickling = True
    return c


def quote(vm, c, clock, farmer, start, end, drought=0, flood=0, payout=10 * GEN, pool_id="1", **hist):
    mock_history(vm, start, end, clock.state["today"], drought=drought, flood=flood, **hist)
    vm.sender = farmer
    vm.value = 0
    return c.request_quote(pool_id, LAT, LON, start, end, drought, flood, payout)


def buy(vm, c, clock, farmer, start, end, drought=0, flood=0, payout=10 * GEN, pool_id="1", **hist):
    """request_quote (no-hit synthetic history unless overridden) + buy_policy."""
    q = quote(vm, c, clock, farmer, start, end, drought, flood, payout, pool_id, **hist)
    vm.sender = farmer
    vm.value = q["premium"]
    try:
        return c.buy_policy(q["id"])
    finally:
        vm.value = 0


def premium10(payout: int) -> int:
    """Premium at pool 1's 10 % minimum rate (0 historical hits): ceil(payout * 0.1)."""
    return (payout * MIN_RATE_BPS + 9999) // 10000
