# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
RainfallInsurance - parametric crop (rainfall) insurance for smallholder farmers.

An insurer funds a GEN payout pool. A farmer buys a policy for a location,
a coverage window and a drought trigger (cumulative rainfall below X mm)
and/or a flood trigger (max daily rainfall above Y mm). After the window
closes anyone can call settle(policy_id). The contract reads daily
precipitation from two independent, keyless public sources:

  * Open-Meteo historical archive (ERA5 reanalysis), daily=precipitation_sum
  * NASA POWER daily point API, parameter PRECTOTCORR (MERRA-2 / GEOS-IT)

Consensus: a custom leader/validator pair (gl.vm.run_nondet). Validators
re-fetch both sources themselves and bind every field that drives money
(policy id, coordinates, exact window, days observed per source, per-source
totals and max-daily within tolerance, sources used, the trigger decision
and the exact payout amount). All money logic runs deterministically after
consensus, in integers only.

Units:
  * coordinates: micro-degrees (int), e.g. -7.327400 deg -> -7327400
  * rainfall: hundredths of a millimetre (int), e.g. 48.40 mm -> 4840
  * money: wei of native GEN (int / u256)
"""

import json
import datetime
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from genlayer import *


# --------------------------------------------------------------------------
# Constants (all integers)
# --------------------------------------------------------------------------

SOURCE_OPEN_METEO = "open-meteo"
SOURCE_NASA_POWER = "nasa-power"
SOURCE_NAMES = [SOURCE_OPEN_METEO, SOURCE_NASA_POWER]

# Leader vs validator, SAME source (data revisions / rounding):
#   values agree if |a - b| <= max(VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT% of max(a, b))
VALIDATOR_TOL_ABS_MM100 = 200  # 2.00 mm
VALIDATOR_TOL_PCT = 5  # 5 %

# Source A vs source B (independent products) when their trigger decisions
# differ: if |a - b| <= max(CROSS_TOL_ABS_MM100, CROSS_TOL_PCT% of max(a, b))
# decide on the integer mean, otherwise the component is UNDECIDED (defer).
CROSS_TOL_ABS_MM100 = 1000  # 10.00 mm
CROSS_TOL_PCT = 30  # 30 %

SETTLEMENT_DELAY_DAYS = 7  # data latency after end_date before settle() opens
CLAIM_WINDOW_DAYS = 60  # settle() allowed for this many days after it opens
MIN_LEAD_DAYS = 1  # coverage must start at least 1 day after purchase
MAX_LEAD_DAYS = 365  # and at most 1 year ahead
MAX_WINDOW_DAYS = 184  # longest coverage window (~ one growing season)
MAX_PREMIUM_RATE_BPS = 10000
BPS = 10000

STATUS_ACTIVE = "ACTIVE"
STATUS_PAID_OUT = "PAID_OUT"
STATUS_SETTLED_NO_PAYOUT = "SETTLED_NO_PAYOUT"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXPIRED_REFUNDED = "EXPIRED_REFUNDED"

DECISION_PAYOUT = "PAYOUT"
DECISION_NO_PAYOUT = "NO_PAYOUT"
DECISION_DEFER = "DEFER"

TRIGGERED = "TRIGGERED"
NOT_TRIGGERED = "NOT_TRIGGERED"
UNDECIDED = "UNDECIDED"
DISABLED = "DISABLED"

REASON_NONE = ""
REASON_SOURCE_MISSING = "SOURCE_MISSING"
REASON_SOURCES_DIVERGE = "SOURCES_DIVERGE"

SRC_OK = "ok"
SRC_INCOMPLETE = "incomplete"
SRC_UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------
# Pure helpers (deterministic, used by leader, validator and contract)
# --------------------------------------------------------------------------


def _parse_date(s: str) -> datetime.date:
    if not isinstance(s, str) or len(s) != 10 or s[4] != "-" or s[7] != "-":
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    if d.isoformat() != s:
        raise gl.vm.UserError("Invalid date format, expected YYYY-MM-DD")
    return d


def _window_dates(start_date: str, end_date: str) -> list:
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d = d + datetime.timedelta(days=1)
    return out


def _micro_to_str(v: int) -> str:
    """Format micro-degrees as a fixed 6-decimal string without floats."""
    sign = "-" if v < 0 else ""
    a = -v if v < 0 else v
    return sign + str(a // 1000000) + "." + str(a % 1000000).rjust(6, "0")


def _open_meteo_url(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> str:
    return (
        "https://archive-api.open-meteo.com/v1/archive"
        + "?latitude=" + _micro_to_str(lat_micro)
        + "&longitude=" + _micro_to_str(lon_micro)
        + "&start_date=" + start_date
        + "&end_date=" + end_date
        + "&daily=precipitation_sum&timezone=auto"
    )


def _nasa_power_url(lat_micro: int, lon_micro: int, start_date: str, end_date: str) -> str:
    return (
        "https://power.larc.nasa.gov/api/temporal/daily/point"
        + "?parameters=PRECTOTCORR&community=AG"
        + "&longitude=" + _micro_to_str(lon_micro)
        + "&latitude=" + _micro_to_str(lat_micro)
        + "&start=" + start_date.replace("-", "")
        + "&end=" + end_date.replace("-", "")
        + "&format=JSON"
    )


def _to_mm100(v) -> int:
    """mm (Decimal/int from JSON) -> integer hundredths of mm. -1 means missing."""
    if v is None or isinstance(v, bool):
        return -1
    if isinstance(v, int):
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    else:
        return -1
    if d < 0:  # NASA POWER fill value is -999
        return -1
    return int((d * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _summarize(name: str, values: list, expected_days: int) -> dict:
    """values: list of mm100 ints (-1 = missing), one per expected date."""
    valid = [x for x in values if x >= 0]
    days = len(valid)
    total = sum(valid) if days > 0 else 0
    mx = max(valid) if days > 0 else 0
    status = SRC_OK if (days == expected_days and len(values) == expected_days) else SRC_INCOMPLETE
    return {"name": name, "status": status, "days": days, "total_mm100": total, "max_mm100": mx}


def _unavailable(name: str) -> dict:
    return {"name": name, "status": SRC_UNAVAILABLE, "days": 0, "total_mm100": 0, "max_mm100": 0}


def _parse_open_meteo(body: bytes, dates: list) -> dict:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    daily = data["daily"]
    times = daily["time"]
    precip = daily["precipitation_sum"]
    if list(times) != dates or len(precip) != len(dates):
        # wrong window / shape: treat as incomplete, count nothing
        return _summarize(SOURCE_OPEN_METEO, [-1] * len(dates), len(dates))
    return _summarize(SOURCE_OPEN_METEO, [_to_mm100(x) for x in precip], len(dates))


def _parse_nasa_power(body: bytes, dates: list) -> dict:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    series = data["properties"]["parameter"]["PRECTOTCORR"]
    values = []
    for d in dates:
        key = d.replace("-", "")
        values.append(_to_mm100(series.get(key)) if key in series else -1)
    return _summarize(SOURCE_NASA_POWER, values, len(dates))


def _within(a: int, b: int, tol_abs: int, tol_pct: int) -> bool:
    diff = a - b if a >= b else b - a
    hi = a if a >= b else b
    return diff <= tol_abs or diff * 100 <= hi * tol_pct


def _component(a: int, b: int, threshold: int, below: bool) -> list:
    """Return [outcome, decided_value]. threshold == 0 means disabled."""
    if threshold <= 0:
        return [DISABLED, -1]

    def trig(v: int) -> bool:
        return v < threshold if below else v > threshold

    mean = (a + b) // 2
    ta = trig(a)
    tb = trig(b)
    if ta == tb:
        return [TRIGGERED if ta else NOT_TRIGGERED, mean]
    if _within(a, b, CROSS_TOL_ABS_MM100, CROSS_TOL_PCT):
        return [TRIGGERED if trig(mean) else NOT_TRIGGERED, mean]
    return [UNDECIDED, -1]


def _decide(sources: list, drought_mm100: int, flood_mm100: int, payout_wei: int) -> dict:
    """Deterministic source-disagreement rules -> decision.

    1. Both sources must be complete (status ok, every day present).
       Otherwise every enabled component is UNDECIDED (reason SOURCE_MISSING).
    2. Drought: cumulative total < drought trigger. Flood: max daily > flood trigger.
       Per component: sources agree -> that outcome; disagree but within the
       cross-source tolerance -> decide on the integer mean; disagree beyond
       tolerance -> UNDECIDED (reason SOURCES_DIVERGE).
    3. Any component TRIGGERED -> PAYOUT (full payout). All enabled components
       NOT_TRIGGERED -> NO_PAYOUT. Otherwise DEFER (no money moves).
    """
    by_name = {}
    for s in sources:
        by_name[s["name"]] = s
    ok = [n for n in SOURCE_NAMES if n in by_name and by_name[n]["status"] == SRC_OK]
    reason = REASON_NONE
    if len(ok) < 2:
        drought = [DISABLED if drought_mm100 <= 0 else UNDECIDED, -1]
        flood = [DISABLED if flood_mm100 <= 0 else UNDECIDED, -1]
        reason = REASON_SOURCE_MISSING
    else:
        a = by_name[SOURCE_OPEN_METEO]
        b = by_name[SOURCE_NASA_POWER]
        drought = _component(a["total_mm100"], b["total_mm100"], drought_mm100, True)
        flood = _component(a["max_mm100"], b["max_mm100"], flood_mm100, False)
        if drought[0] == UNDECIDED or flood[0] == UNDECIDED:
            reason = REASON_SOURCES_DIVERGE

    outcomes = [drought[0], flood[0]]
    if TRIGGERED in outcomes:
        decision = DECISION_PAYOUT
        reason = REASON_NONE
    elif UNDECIDED in outcomes:
        decision = DECISION_DEFER
    else:
        decision = DECISION_NO_PAYOUT
        reason = REASON_NONE
    return {
        "sources_ok": len(ok),
        "sources_used": ok,
        "drought": drought[0],
        "flood": flood[0],
        "decided_total_mm100": drought[1],
        "decided_max_mm100": flood[1],
        "decision": decision,
        "defer_reason": reason,
        "payout_wei": payout_wei if decision == DECISION_PAYOUT else 0,
    }


DECISION_KEYS = [
    "sources_ok",
    "sources_used",
    "drought",
    "flood",
    "decided_total_mm100",
    "decided_max_mm100",
    "decision",
    "defer_reason",
    "payout_wei",
]

IDENTITY_KEYS = ["policy_id", "lat_micro", "lon_micro", "start_date", "end_date", "expected_days"]


def _fetch_evidence(
    policy_id: str,
    lat_micro: int,
    lon_micro: int,
    start_date: str,
    end_date: str,
    drought_mm100: int,
    flood_mm100: int,
    payout_wei: int,
) -> dict:
    """Non-deterministic: fetch both sources and build the settlement evidence."""
    dates = _window_dates(start_date, end_date)
    sources = []

    try:
        r = gl.nondet.web.get(_open_meteo_url(lat_micro, lon_micro, start_date, end_date))
        if r.status == 200 and r.body is not None:
            sources.append(_parse_open_meteo(r.body, dates))
        else:
            sources.append(_unavailable(SOURCE_OPEN_METEO))
    except Exception:
        sources.append(_unavailable(SOURCE_OPEN_METEO))

    try:
        r = gl.nondet.web.get(_nasa_power_url(lat_micro, lon_micro, start_date, end_date))
        if r.status == 200 and r.body is not None:
            sources.append(_parse_nasa_power(r.body, dates))
        else:
            sources.append(_unavailable(SOURCE_NASA_POWER))
    except Exception:
        sources.append(_unavailable(SOURCE_NASA_POWER))

    ev = {
        "policy_id": policy_id,
        "lat_micro": lat_micro,
        "lon_micro": lon_micro,
        "start_date": start_date,
        "end_date": end_date,
        "expected_days": len(dates),
        "sources": sources,
    }
    ev.update(_decide(sources, drought_mm100, flood_mm100, payout_wei))
    return ev


def _evidence_matches(leader: dict, mine: dict, drought_mm100: int, flood_mm100: int, payout_wei: int) -> bool:
    """Validator equivalence check. Every settlement-driving field is bound."""
    if not isinstance(leader, dict):
        return False
    # 1. identity: exact
    for k in IDENTITY_KEYS:
        if leader.get(k) != mine[k]:
            return False
    # 2. per-source observations
    ls = leader.get("sources")
    if not isinstance(ls, list) or len(ls) != len(mine["sources"]):
        return False
    for lsrc, msrc in zip(ls, mine["sources"]):
        if not isinstance(lsrc, dict):
            return False
        if lsrc.get("name") != msrc["name"] or lsrc.get("status") != msrc["status"]:
            return False
        if lsrc.get("days") != msrc["days"]:
            return False
        for k in ["total_mm100", "max_mm100"]:
            lv = lsrc.get(k)
            if not isinstance(lv, int) or isinstance(lv, bool) or lv < 0:
                return False
            if not _within(lv, msrc[k], VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT):
                return False
    # 3. leader's decision must follow from leader's own numbers (no fake verdicts)
    recomputed = _decide(ls, drought_mm100, flood_mm100, payout_wei)
    for k in DECISION_KEYS:
        if leader.get(k) != recomputed[k]:
            return False
    # 4. and must equal the decision the validator reached independently
    for k in ["sources_ok", "sources_used", "drought", "flood", "decision", "defer_reason", "payout_wei"]:
        if leader.get(k) != mine[k]:
            return False
    return True


# --------------------------------------------------------------------------
# Storage types
# --------------------------------------------------------------------------


@allow_storage
@dataclass
class Policy:
    id: str
    farmer: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    drought_mm100: u256
    flood_mm100: u256
    payout: u256
    premium: u256
    status: str
    purchased_on: str
    settle_attempts: u32
    paid_amount: u256
    evidence: str


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


# --------------------------------------------------------------------------
# Contract
# --------------------------------------------------------------------------


class RainfallInsurance(gl.Contract):
    insurer: Address
    premium_rate_bps: u32
    pool_balance: u256  # GEN accounted to the pool (deposits + premiums - outflows)
    reserved: u256  # sum of payouts of ACTIVE policies
    total_premiums: u256
    total_paid_out: u256
    total_refunded: u256
    total_withdrawn: u256
    policy_count: u256
    policies: TreeMap[str, Policy]

    def __init__(self, premium_rate_bps: int):
        if premium_rate_bps <= 0 or premium_rate_bps > MAX_PREMIUM_RATE_BPS:
            raise gl.vm.UserError("Invalid premium rate")
        self.insurer = gl.message.sender_address
        self.premium_rate_bps = u32(premium_rate_bps)
        self.pool_balance = u256(0)
        self.reserved = u256(0)
        self.total_premiums = u256(0)
        self.total_paid_out = u256(0)
        self.total_refunded = u256(0)
        self.total_withdrawn = u256(0)
        self.policy_count = u256(0)

    # ---------------- internal helpers ----------------

    def _today(self) -> datetime.date:
        return datetime.datetime.now(datetime.timezone.utc).date()

    def _free_liquidity(self) -> int:
        return int(self.pool_balance) - int(self.reserved)

    def _premium_for(self, payout: int) -> int:
        # ceil(payout * rate / 10000), integers only
        return (payout * int(self.premium_rate_bps) + BPS - 1) // BPS

    def _send(self, to: Address, amount: int) -> None:
        if amount > 0:
            _Recipient(to).emit_transfer(value=u256(amount))

    def _get(self, policy_id: str) -> Policy:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy")
        return self.policies[policy_id]

    def _release(self, p: Policy) -> None:
        self.reserved = u256(int(self.reserved) - int(p.payout))

    # ---------------- insurer ----------------

    @gl.public.write.payable
    def fund_pool(self) -> int:
        if gl.message.sender_address != self.insurer:
            raise gl.vm.UserError("Only insurer")
        value = int(gl.message.value)
        if value <= 0:
            raise gl.vm.UserError("Send GEN to fund the pool")
        self.pool_balance = u256(int(self.pool_balance) + value)
        return int(self.pool_balance)

    @gl.public.write
    def withdraw(self, amount: int) -> int:
        if gl.message.sender_address != self.insurer:
            raise gl.vm.UserError("Only insurer")
        if amount <= 0:
            raise gl.vm.UserError("Invalid amount")
        if amount > self._free_liquidity():
            raise gl.vm.UserError("Amount exceeds unreserved funds")
        self.pool_balance = u256(int(self.pool_balance) - amount)
        self.total_withdrawn = u256(int(self.total_withdrawn) + amount)
        self._send(self.insurer, amount)
        return int(self.pool_balance)

    @gl.public.write
    def set_premium_rate(self, premium_rate_bps: int) -> None:
        if gl.message.sender_address != self.insurer:
            raise gl.vm.UserError("Only insurer")
        if premium_rate_bps <= 0 or premium_rate_bps > MAX_PREMIUM_RATE_BPS:
            raise gl.vm.UserError("Invalid premium rate")
        self.premium_rate_bps = u32(premium_rate_bps)

    # ---------------- farmer ----------------

    @gl.public.write.payable
    def buy_policy(
        self,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        drought_trigger_mm100: int,
        flood_trigger_mm100: int,
        payout: int,
    ) -> str:
        if lat_micro < -90000000 or lat_micro > 90000000:
            raise gl.vm.UserError("Invalid latitude")
        if lon_micro < -180000000 or lon_micro > 180000000:
            raise gl.vm.UserError("Invalid longitude")
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise gl.vm.UserError("end_date before start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise gl.vm.UserError("Coverage window too long")
        today = self._today()
        lead = (start - today).days
        if lead < MIN_LEAD_DAYS:
            raise gl.vm.UserError("Coverage must start after purchase date")
        if lead > MAX_LEAD_DAYS:
            raise gl.vm.UserError("Coverage starts too far ahead")
        if drought_trigger_mm100 < 0 or flood_trigger_mm100 < 0:
            raise gl.vm.UserError("Invalid trigger")
        if drought_trigger_mm100 == 0 and flood_trigger_mm100 == 0:
            raise gl.vm.UserError("At least one trigger required")
        if payout <= 0:
            raise gl.vm.UserError("Invalid payout")
        premium = self._premium_for(payout)
        if int(gl.message.value) != premium:
            raise gl.vm.UserError("Premium must equal " + str(premium))
        if self._free_liquidity() < payout:
            raise gl.vm.UserError("Insufficient pool liquidity")

        new_count = int(self.policy_count) + 1
        policy_id = str(new_count)
        self.policy_count = u256(new_count)
        self.pool_balance = u256(int(self.pool_balance) + premium)
        self.reserved = u256(int(self.reserved) + payout)
        self.total_premiums = u256(int(self.total_premiums) + premium)
        self.policies[policy_id] = Policy(
            id=policy_id,
            farmer=gl.message.sender_address,
            lat_micro=i64(lat_micro),
            lon_micro=i64(lon_micro),
            start_date=start_date,
            end_date=end_date,
            drought_mm100=u256(drought_trigger_mm100),
            flood_mm100=u256(flood_trigger_mm100),
            payout=u256(payout),
            premium=u256(premium),
            status=STATUS_ACTIVE,
            purchased_on=today.isoformat(),
            settle_attempts=u32(0),
            paid_amount=u256(0),
            evidence="",
        )
        return policy_id

    @gl.public.write
    def cancel_policy(self, policy_id: str) -> int:
        """Farmer cancels before coverage starts: full premium refund."""
        p = self._get(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        if gl.message.sender_address != p.farmer:
            raise gl.vm.UserError("Only policy holder")
        if self._today() >= datetime.date.fromisoformat(p.start_date):
            raise gl.vm.UserError("Coverage already started")
        refund = int(p.premium)
        p.status = STATUS_CANCELLED
        self._release(p)
        self.pool_balance = u256(int(self.pool_balance) - refund)
        self.total_refunded = u256(int(self.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- settlement ----------------

    @gl.public.write
    def settle(self, policy_id: str) -> dict:
        p = self._get(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        end = datetime.date.fromisoformat(p.end_date)
        opens = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS)
        deadline = opens + datetime.timedelta(days=CLAIM_WINDOW_DAYS)
        today = self._today()
        if today < opens:
            raise gl.vm.UserError("Settlement not open yet")
        if today > deadline:
            raise gl.vm.UserError("Settlement deadline passed")

        # copy everything the nondet block needs into plain locals
        pid = str(p.id)
        lat = int(p.lat_micro)
        lon = int(p.lon_micro)
        start_date = str(p.start_date)
        end_date = str(p.end_date)
        drought = int(p.drought_mm100)
        flood = int(p.flood_mm100)
        payout = int(p.payout)

        def leader_fn() -> dict:
            return _fetch_evidence(pid, lat, lon, start_date, end_date, drought, flood, payout)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_evidence(pid, lat, lon, start_date, end_date, drought, flood, payout)
            return _evidence_matches(leaders_res.calldata, mine, drought, flood, payout)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)

        # ---- deterministic post-consensus checks and money logic ----
        if (
            ev["policy_id"] != pid
            or ev["lat_micro"] != lat
            or ev["lon_micro"] != lon
            or ev["start_date"] != start_date
            or ev["end_date"] != end_date
            or ev["expected_days"] != len(_window_dates(start_date, end_date))
        ):
            raise gl.vm.UserError("Evidence does not match policy")
        recomputed = _decide(ev["sources"], drought, flood, payout)
        for k in DECISION_KEYS:
            if ev[k] != recomputed[k]:
                raise gl.vm.UserError("Evidence decision inconsistent")

        p.settle_attempts = u32(int(p.settle_attempts) + 1)
        p.evidence = json.dumps(ev, sort_keys=True)
        decision = recomputed["decision"]
        if decision == DECISION_PAYOUT:
            amount = recomputed["payout_wei"]
            p.status = STATUS_PAID_OUT
            p.paid_amount = u256(amount)
            self._release(p)
            self.pool_balance = u256(int(self.pool_balance) - amount)
            self.total_paid_out = u256(int(self.total_paid_out) + amount)
            self._send(p.farmer, amount)
        elif decision == DECISION_NO_PAYOUT:
            p.status = STATUS_SETTLED_NO_PAYOUT
            self._release(p)
        # DEFER: policy stays ACTIVE, reserve stays locked, evidence recorded
        return ev

    @gl.public.write
    def expire(self, policy_id: str) -> int:
        """After the claim window closes without settlement (sources missing or
        diverging the whole time), anyone can expire the policy: the premium is
        refunded to the farmer and the reserve is released."""
        p = self._get(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        end = datetime.date.fromisoformat(p.end_date)
        deadline = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS + CLAIM_WINDOW_DAYS)
        if self._today() <= deadline:
            raise gl.vm.UserError("Claim window still open")
        refund = int(p.premium)
        p.status = STATUS_EXPIRED_REFUNDED
        self._release(p)
        self.pool_balance = u256(int(self.pool_balance) - refund)
        self.total_refunded = u256(int(self.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- views ----------------

    @gl.public.view
    def quote_premium(self, payout: int) -> int:
        if payout <= 0:
            raise gl.vm.UserError("Invalid payout")
        return self._premium_for(payout)

    @gl.public.view
    def get_pool(self) -> dict:
        return {
            "insurer": self.insurer.as_hex,
            "premium_rate_bps": int(self.premium_rate_bps),
            "pool_balance": int(self.pool_balance),
            "reserved": int(self.reserved),
            "free_liquidity": self._free_liquidity(),
            "total_premiums": int(self.total_premiums),
            "total_paid_out": int(self.total_paid_out),
            "total_refunded": int(self.total_refunded),
            "total_withdrawn": int(self.total_withdrawn),
            "policy_count": int(self.policy_count),
        }

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        p = self._get(policy_id)
        end = datetime.date.fromisoformat(p.end_date)
        opens = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS)
        return {
            "id": p.id,
            "farmer": p.farmer.as_hex,
            "lat_micro": int(p.lat_micro),
            "lon_micro": int(p.lon_micro),
            "start_date": p.start_date,
            "end_date": p.end_date,
            "drought_trigger_mm100": int(p.drought_mm100),
            "flood_trigger_mm100": int(p.flood_mm100),
            "payout": int(p.payout),
            "premium": int(p.premium),
            "status": p.status,
            "purchased_on": p.purchased_on,
            "settle_opens": opens.isoformat(),
            "settle_deadline": (opens + datetime.timedelta(days=CLAIM_WINDOW_DAYS)).isoformat(),
            "settle_attempts": int(p.settle_attempts),
            "paid_amount": int(p.paid_amount),
            "evidence": p.evidence,
        }

    @gl.public.view
    def get_source_urls(self, policy_id: str) -> list:
        p = self._get(policy_id)
        lat = int(p.lat_micro)
        lon = int(p.lon_micro)
        return [
            _open_meteo_url(lat, lon, p.start_date, p.end_date),
            _nasa_power_url(lat, lon, p.start_date, p.end_date),
        ]
