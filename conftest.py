"""Shared helpers for direct-mode tests of RainfallInsurance.

Web responses are REAL payloads captured from the two public APIs with curl
(see tests/direct/fixtures/README.md) for Tasikmalaya, West Java
(lat -7.327400, lon 108.220700):

  * om_2023-08.json / nasa_2023-08.json  - August 2023 (El Nino drought)
  * om_2025-01.json / nasa_2025-01.json  - January 2025 (wet season)
  * om_recent.json  / nasa_recent.json   - Oct 2026, NASA still has -999 fill days
"""

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
CONTRACT = str(Path(__file__).resolve().parents[2] / "contracts" / "rainfall_insurance.py")

GEN = 10**18
LAT = -7327400  # Tasikmalaya, West Java (micro-degrees)
LON = 108220700
RATE_BPS = 1000  # 10 % premium

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
    """Register real fixture payloads for a window (optionally overridden)."""
    mock_open_meteo(vm, start, end, om_body if om_body is not None else fixture_text(f"om_{month}.json"))
    mock_nasa(vm, start, end, nasa_body if nasa_body is not None else fixture_text(f"nasa_{month}.json"))


def contract_module():
    for name, mod in sys.modules.items():
        if name == "_contract_rainfall_insurance":
            return mod
    raise RuntimeError("contract module not loaded")


def to_hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex
    from genlayer.py.types import Address

    return Address(addr).as_hex


def assert_pool_invariants(c, funded: int):
    pool = c.get_pool()
    assert pool["reserved"] <= pool["pool_balance"]
    assert pool["free_liquidity"] == pool["pool_balance"] - pool["reserved"]
    assert pool["pool_balance"] == (
        funded
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
def insurance(direct_vm, direct_deploy, direct_owner):
    """Deployed contract, funded with 100 GEN by the insurer, clock at 2023-07-01."""
    direct_vm.warp("2023-07-01T00:00:00Z")
    direct_vm.sender = direct_owner
    c = direct_deploy(CONTRACT, RATE_BPS)
    direct_vm.value = 100 * GEN
    c.fund_pool()
    direct_vm.value = 0
    direct_vm.check_pickling = True
    return c


def buy(vm, c, farmer, start, end, drought=0, flood=0, payout=10 * GEN):
    vm.sender = farmer
    vm.value = c.quote_premium(payout)
    try:
        return c.buy_policy(LAT, LON, start, end, drought, flood, payout)
    finally:
        vm.value = 0
