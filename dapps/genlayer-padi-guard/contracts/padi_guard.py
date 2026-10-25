# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
PadiGuard - a multi-insurer marketplace for parametric rainfall crop insurance.

This is the contract behind the PadiGuard web dApp. It builds on the author's
earlier single-insurer RainfallInsurance contract (same two-source settlement
consensus, copied unchanged) and adds:

  * Insurer pools: anyone can open a pool with its own pricing parameters and
    an optional coverage region (lat/lon bounding box), fund it and withdraw
    unreserved liquidity. Each pool keeps its own balance and reserve.
  * Risk-priced quotes from rainfall history: request_quote() asks validators
    to fetch the last 5 years of daily rainfall for the same location and
    calendar window (Open-Meteo ERA5 archive) and to agree on how many of
    those years would have triggered the policy. The premium rate is
    max(pool.min_rate, hit_rate + pool.loading). The quote, with its per-year
    evidence, is stored on-chain and is what buy_policy() charges.
  * Read views for a UI: stats, paginated pool and policy lists, per-farmer
    policy and quote lookups, per-pool policy lists.

Settlement (unchanged from RainfallInsurance): after the window closes anyone
calls settle(policy_id); validators re-fetch Open-Meteo ERA5 and NASA POWER
daily precipitation and bind every field that drives money.

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
BPS = 10000

# Quotes
QUOTE_YEARS = 5  # years of history used to price a quote
QUOTE_HISTORY_LAG_DAYS = 7  # a history year is usable once its window ended this long ago
QUOTE_VALID_DAYS = 3  # a quote can be used to buy for this many days
MAX_POOL_NAME = 64

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

MAX_PAGE = 50


# --------------------------------------------------------------------------
# Pure helpers shared with RainfallInsurance (settlement path, unchanged)
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
# New in PadiGuard: rainfall-history quotes (pure helpers)
# --------------------------------------------------------------------------


def _shift_years(d: datetime.date, years: int) -> datetime.date:
    """Same calendar day `years` years earlier; 29 Feb maps to 28 Feb."""
    y = d.year - years
    try:
        return datetime.date(y, d.month, d.day)
    except ValueError:
        return datetime.date(y, d.month, 28)


def _history_windows(start_date: str, end_date: str, today: datetime.date) -> list:
    """The QUOTE_YEARS most recent past copies of the coverage window whose end
    is at least QUOTE_HISTORY_LAG_DAYS before today, newest first.
    Returns [[start, end], ...] as ISO strings. Deterministic."""
    start = datetime.date.fromisoformat(start_date)
    end = datetime.date.fromisoformat(end_date)
    latest_ok = today - datetime.timedelta(days=QUOTE_HISTORY_LAG_DAYS)
    out = []
    k = 1
    while len(out) < QUOTE_YEARS and k <= QUOTE_YEARS + 3:
        s = _shift_years(start, k)
        e = _shift_years(end, k)
        if e <= latest_ok:
            out.append([s.isoformat(), e.isoformat()])
        k += 1
    return out


def _history_range(windows: list) -> list:
    """[first_start, last_end] covering every history window (one request)."""
    starts = [w[0] for w in windows]
    ends = [w[1] for w in windows]
    return [min(starts), max(ends)]


def _year_hit(total_mm100: int, max_mm100: int, drought_mm100: int, flood_mm100: int) -> bool:
    drought_hit = drought_mm100 > 0 and total_mm100 < drought_mm100
    flood_hit = flood_mm100 > 0 and max_mm100 > flood_mm100
    return drought_hit or flood_hit


def _summarize_years(by_date: dict, windows: list) -> list:
    years = []
    for w in windows:
        dates = _window_dates(w[0], w[1])
        values = [by_date[d] if d in by_date else -1 for d in dates]
        s = _summarize(SOURCE_OPEN_METEO, values, len(dates))
        years.append(
            {
                "start_date": w[0],
                "end_date": w[1],
                "expected_days": len(dates),
                "days": s["days"],
                "total_mm100": s["total_mm100"],
                "max_mm100": s["max_mm100"],
            }
        )
    return years


def _parse_history(body: bytes, windows: list) -> list:
    data = json.loads(body.decode("utf-8"), parse_float=Decimal)
    daily = data["daily"]
    times = daily["time"]
    precip = daily["precipitation_sum"]
    by_date = {}
    if len(times) == len(precip):
        for t, v in zip(times, precip):
            if isinstance(t, str):
                by_date[t] = _to_mm100(v)
    return _summarize_years(by_date, windows)


def _empty_years(windows: list) -> list:
    return _summarize_years({}, windows)


def _history_decide(status: str, years: list, drought_mm100: int, flood_mm100: int) -> dict:
    """Deterministic: which history years would have paid, and how many.
    Only meaningful when status == ok (every day of every year present)."""
    flags = []
    for y in years:
        flags.append(_year_hit(y["total_mm100"], y["max_mm100"], drought_mm100, flood_mm100))
    hits = len([f for f in flags if f])
    return {"hit_flags": flags, "hits": hits}


def _history_status(years: list) -> str:
    for y in years:
        if y["days"] != y["expected_days"]:
            return SRC_INCOMPLETE
    return SRC_OK


def _fetch_history(lat_micro: int, lon_micro: int, windows: list, drought_mm100: int, flood_mm100: int) -> dict:
    """Non-deterministic: fetch the history range once and summarise each year."""
    rng = _history_range(windows)
    years = _empty_years(windows)
    status = SRC_UNAVAILABLE
    try:
        r = gl.nondet.web.get(_open_meteo_url(lat_micro, lon_micro, rng[0], rng[1]))
        if r.status == 200 and r.body is not None:
            years = _parse_history(r.body, windows)
            status = _history_status(years)
    except Exception:
        years = _empty_years(windows)
        status = SRC_UNAVAILABLE
    ev = {
        "lat_micro": lat_micro,
        "lon_micro": lon_micro,
        "windows": windows,
        "source": SOURCE_OPEN_METEO,
        "status": status,
        "years": years,
    }
    ev.update(_history_decide(status, years, drought_mm100, flood_mm100))
    return ev


def _history_matches(leader: dict, mine: dict, drought_mm100: int, flood_mm100: int) -> bool:
    """Validator equivalence check for request_quote(): everything that drives
    the premium is bound (window list, per-year coverage, per-year totals within
    tolerance, per-year trigger flags and the hit count)."""
    if not isinstance(leader, dict):
        return False
    for k in ["lat_micro", "lon_micro", "windows", "source", "status"]:
        if leader.get(k) != mine[k]:
            return False
    ly = leader.get("years")
    if not isinstance(ly, list) or len(ly) != len(mine["years"]):
        return False
    for lyr, myr in zip(ly, mine["years"]):
        if not isinstance(lyr, dict):
            return False
        for k in ["start_date", "end_date", "expected_days", "days"]:
            if lyr.get(k) != myr[k]:
                return False
        for k in ["total_mm100", "max_mm100"]:
            lv = lyr.get(k)
            if not isinstance(lv, int) or isinstance(lv, bool) or lv < 0:
                return False
            if not _within(lv, myr[k], VALIDATOR_TOL_ABS_MM100, VALIDATOR_TOL_PCT):
                return False
    # leader's flags must follow from its own numbers ...
    recomputed = _history_decide(leader["status"], ly, drought_mm100, flood_mm100)
    if leader.get("hit_flags") != recomputed["hit_flags"] or leader.get("hits") != recomputed["hits"]:
        return False
    # ... and equal the validator's own
    if leader.get("hit_flags") != mine["hit_flags"] or leader.get("hits") != mine["hits"]:
        return False
    return True


def _rate_for(hits: int, years: int, min_rate_bps: int, loading_bps: int) -> int:
    """Premium rate in bps: historical hit rate + loading, floored at the pool
    minimum, capped at 100 %."""
    loss_bps = hits * BPS // years if years > 0 else BPS
    rate = loss_bps + loading_bps
    if rate < min_rate_bps:
        rate = min_rate_bps
    if rate > BPS:
        rate = BPS
    return rate


def _premium(payout: int, rate_bps: int) -> int:
    # ceil(payout * rate / 10000), integers only
    return (payout * rate_bps + BPS - 1) // BPS


# --------------------------------------------------------------------------
# Storage types
# --------------------------------------------------------------------------


@allow_storage
@dataclass
class Pool:
    id: str
    insurer: Address
    name: str
    min_rate_bps: u32
    loading_bps: u32
    lat_min: i64
    lat_max: i64
    lon_min: i64
    lon_max: i64
    accepting: bool
    balance: u256  # deposits + premiums - payouts - refunds - withdrawals
    reserved: u256  # sum of payouts of ACTIVE policies in this pool
    total_deposits: u256
    total_premiums: u256
    total_paid_out: u256
    total_refunded: u256
    total_withdrawn: u256
    policy_count: u32
    active_policies: u32


@allow_storage
@dataclass
class Quote:
    id: str
    pool_id: str
    requester: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    drought_mm100: u256
    flood_mm100: u256
    payout: u256
    rate_bps: u32
    premium: u256
    hits: u32
    years: u32
    created_on: str
    valid_until: str
    policy_id: str  # "" until used
    history: str  # JSON of the agreed history evidence


@allow_storage
@dataclass
class Policy:
    id: str
    pool_id: str
    quote_id: str
    farmer: Address
    lat_micro: i64
    lon_micro: i64
    start_date: str
    end_date: str
    drought_mm100: u256
    flood_mm100: u256
    payout: u256
    premium: u256
    rate_bps: u32
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


class PadiGuard(gl.Contract):
    pool_count: u256
    quote_count: u256
    policy_count: u256
    pools: TreeMap[str, Pool]
    quotes: TreeMap[str, Quote]
    policies: TreeMap[str, Policy]
    farmer_policies: TreeMap[str, DynArray[str]]
    farmer_quotes: TreeMap[str, DynArray[str]]
    pool_policies: TreeMap[str, DynArray[str]]
    insurer_pools: TreeMap[str, DynArray[str]]

    def __init__(self):
        self.pool_count = u256(0)
        self.quote_count = u256(0)
        self.policy_count = u256(0)

    # ---------------- internal helpers ----------------

    def _today(self) -> datetime.date:
        return datetime.datetime.now(datetime.timezone.utc).date()

    def _send(self, to: Address, amount: int) -> None:
        if amount > 0:
            _Recipient(to).emit_transfer(value=u256(amount))

    def _pool(self, pool_id: str) -> Pool:
        if pool_id not in self.pools:
            raise gl.vm.UserError("Unknown pool")
        return self.pools[pool_id]

    def _policy(self, policy_id: str) -> Policy:
        if policy_id not in self.policies:
            raise gl.vm.UserError("Unknown policy")
        return self.policies[policy_id]

    def _quote(self, quote_id: str) -> Quote:
        if quote_id not in self.quotes:
            raise gl.vm.UserError("Unknown quote")
        return self.quotes[quote_id]

    def _only_insurer(self, pool: Pool) -> None:
        if gl.message.sender_address != pool.insurer:
            raise gl.vm.UserError("Only pool insurer")

    def _free(self, pool: Pool) -> int:
        return int(pool.balance) - int(pool.reserved)

    def _index_append(self, index: TreeMap[str, DynArray[str]], key: str, value: str) -> None:
        index.get_or_insert_default(key).append(value)

    def _index_list(self, index: TreeMap[str, DynArray[str]], key: str) -> list:
        if key not in index:
            return []
        return [str(x) for x in index[key]]

    def _release(self, pool: Pool, p: Policy) -> None:
        pool.reserved = u256(int(pool.reserved) - int(p.payout))
        pool.active_policies = u32(int(pool.active_policies) - 1)

    def _check_rates(self, min_rate_bps: int, loading_bps: int) -> None:
        if min_rate_bps <= 0 or min_rate_bps > BPS:
            raise gl.vm.UserError("Invalid minimum rate")
        if loading_bps < 0 or loading_bps > BPS:
            raise gl.vm.UserError("Invalid loading")

    def _check_terms(
        self,
        pool: Pool,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        drought_mm100: int,
        flood_mm100: int,
        payout: int,
    ) -> None:
        if not bool(pool.accepting):
            raise gl.vm.UserError("Pool is not accepting new policies")
        if lat_micro < -90000000 or lat_micro > 90000000:
            raise gl.vm.UserError("Invalid latitude")
        if lon_micro < -180000000 or lon_micro > 180000000:
            raise gl.vm.UserError("Invalid longitude")
        if (
            lat_micro < int(pool.lat_min)
            or lat_micro > int(pool.lat_max)
            or lon_micro < int(pool.lon_min)
            or lon_micro > int(pool.lon_max)
        ):
            raise gl.vm.UserError("Location outside pool coverage region")
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if end < start:
            raise gl.vm.UserError("end_date before start_date")
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise gl.vm.UserError("Coverage window too long")
        lead = (start - self._today()).days
        if lead < MIN_LEAD_DAYS:
            raise gl.vm.UserError("Coverage must start after purchase date")
        if lead > MAX_LEAD_DAYS:
            raise gl.vm.UserError("Coverage starts too far ahead")
        if drought_mm100 < 0 or flood_mm100 < 0:
            raise gl.vm.UserError("Invalid trigger")
        if drought_mm100 == 0 and flood_mm100 == 0:
            raise gl.vm.UserError("At least one trigger required")
        if payout <= 0:
            raise gl.vm.UserError("Invalid payout")
        if self._free(pool) < payout:
            raise gl.vm.UserError("Insufficient pool liquidity")

    # ---------------- insurers ----------------

    @gl.public.write.payable
    def create_pool(
        self,
        name: str,
        min_rate_bps: int,
        loading_bps: int,
        lat_min: int,
        lat_max: int,
        lon_min: int,
        lon_max: int,
    ) -> str:
        """Open a new insurer pool. msg.value (optional) is the first deposit."""
        if not isinstance(name, str) or len(name.strip()) == 0 or len(name) > MAX_POOL_NAME:
            raise gl.vm.UserError("Invalid pool name")
        self._check_rates(min_rate_bps, loading_bps)
        if lat_min < -90000000 or lat_max > 90000000 or lat_min > lat_max:
            raise gl.vm.UserError("Invalid latitude range")
        if lon_min < -180000000 or lon_max > 180000000 or lon_min > lon_max:
            raise gl.vm.UserError("Invalid longitude range")
        deposit = int(gl.message.value)
        new_count = int(self.pool_count) + 1
        pool_id = str(new_count)
        self.pool_count = u256(new_count)
        self.pools[pool_id] = Pool(
            id=pool_id,
            insurer=gl.message.sender_address,
            name=name.strip(),
            min_rate_bps=u32(min_rate_bps),
            loading_bps=u32(loading_bps),
            lat_min=i64(lat_min),
            lat_max=i64(lat_max),
            lon_min=i64(lon_min),
            lon_max=i64(lon_max),
            accepting=True,
            balance=u256(deposit),
            reserved=u256(0),
            total_deposits=u256(deposit),
            total_premiums=u256(0),
            total_paid_out=u256(0),
            total_refunded=u256(0),
            total_withdrawn=u256(0),
            policy_count=u32(0),
            active_policies=u32(0),
        )
        self._index_append(self.insurer_pools, gl.message.sender_address.as_hex, pool_id)
        return pool_id

    @gl.public.write.payable
    def fund_pool(self, pool_id: str) -> int:
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        value = int(gl.message.value)
        if value <= 0:
            raise gl.vm.UserError("Send GEN to fund the pool")
        pool.balance = u256(int(pool.balance) + value)
        pool.total_deposits = u256(int(pool.total_deposits) + value)
        return int(pool.balance)

    @gl.public.write
    def withdraw(self, pool_id: str, amount: int) -> int:
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        if amount <= 0:
            raise gl.vm.UserError("Invalid amount")
        if amount > self._free(pool):
            raise gl.vm.UserError("Amount exceeds unreserved funds")
        pool.balance = u256(int(pool.balance) - amount)
        pool.total_withdrawn = u256(int(pool.total_withdrawn) + amount)
        self._send(pool.insurer, amount)
        return int(pool.balance)

    @gl.public.write
    def set_pool_pricing(self, pool_id: str, min_rate_bps: int, loading_bps: int) -> None:
        """Applies to quotes requested after this call."""
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        self._check_rates(min_rate_bps, loading_bps)
        pool.min_rate_bps = u32(min_rate_bps)
        pool.loading_bps = u32(loading_bps)

    @gl.public.write
    def set_pool_accepting(self, pool_id: str, accepting: bool) -> None:
        """Open/close the pool for new quotes and purchases. Existing policies
        are unaffected."""
        pool = self._pool(pool_id)
        self._only_insurer(pool)
        pool.accepting = bool(accepting)

    # ---------------- farmers: quote ----------------

    @gl.public.write
    def request_quote(
        self,
        pool_id: str,
        lat_micro: int,
        lon_micro: int,
        start_date: str,
        end_date: str,
        drought_trigger_mm100: int,
        flood_trigger_mm100: int,
        payout: int,
    ) -> dict:
        """Price a policy from the last QUOTE_YEARS years of rainfall at this
        location for the same calendar window. Validators fetch the history
        themselves and must agree on every year's numbers and trigger flags."""
        pool = self._pool(pool_id)
        self._check_terms(
            pool, lat_micro, lon_micro, start_date, end_date, drought_trigger_mm100, flood_trigger_mm100, payout
        )
        today = self._today()
        windows = _history_windows(start_date, end_date, today)
        if len(windows) != QUOTE_YEARS:
            raise gl.vm.UserError("Not enough rainfall history for this window")

        lat = int(lat_micro)
        lon = int(lon_micro)
        drought = int(drought_trigger_mm100)
        flood = int(flood_trigger_mm100)

        def leader_fn() -> dict:
            return _fetch_history(lat, lon, windows, drought, flood)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_history(lat, lon, windows, drought, flood)
            return _history_matches(leaders_res.calldata, mine, drought, flood)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)

        # ---- deterministic post-consensus checks ----
        if ev["lat_micro"] != lat or ev["lon_micro"] != lon or ev["windows"] != windows:
            raise gl.vm.UserError("History evidence does not match request")
        if ev["status"] != SRC_OK or _history_status(ev["years"]) != SRC_OK:
            raise gl.vm.UserError("Rainfall history incomplete or unavailable, try again later")
        recomputed = _history_decide(ev["status"], ev["years"], drought, flood)
        if ev["hit_flags"] != recomputed["hit_flags"] or ev["hits"] != recomputed["hits"]:
            raise gl.vm.UserError("History evidence inconsistent")

        hits = recomputed["hits"]
        rate = _rate_for(hits, QUOTE_YEARS, int(pool.min_rate_bps), int(pool.loading_bps))
        premium = _premium(payout, rate)
        new_count = int(self.quote_count) + 1
        quote_id = str(new_count)
        self.quote_count = u256(new_count)
        valid_until = today + datetime.timedelta(days=QUOTE_VALID_DAYS)
        self.quotes[quote_id] = Quote(
            id=quote_id,
            pool_id=pool_id,
            requester=gl.message.sender_address,
            lat_micro=i64(lat),
            lon_micro=i64(lon),
            start_date=start_date,
            end_date=end_date,
            drought_mm100=u256(drought),
            flood_mm100=u256(flood),
            payout=u256(payout),
            rate_bps=u32(rate),
            premium=u256(premium),
            hits=u32(hits),
            years=u32(QUOTE_YEARS),
            created_on=today.isoformat(),
            valid_until=valid_until.isoformat(),
            policy_id="",
            history=json.dumps(ev, sort_keys=True),
        )
        self._index_append(self.farmer_quotes, gl.message.sender_address.as_hex, quote_id)
        return self._quote_view(self.quotes[quote_id])

    # ---------------- farmers: buy / cancel ----------------

    @gl.public.write.payable
    def buy_policy(self, quote_id: str) -> str:
        """Buy the policy described by an unused, unexpired quote. msg.value
        must equal the quoted premium."""
        q = self._quote(quote_id)
        if gl.message.sender_address != q.requester:
            raise gl.vm.UserError("Only the quote requester can buy")
        if q.policy_id != "":
            raise gl.vm.UserError("Quote already used")
        today = self._today()
        if today > datetime.date.fromisoformat(q.valid_until):
            raise gl.vm.UserError("Quote expired")
        pool = self._pool(q.pool_id)
        payout = int(q.payout)
        self._check_terms(
            pool,
            int(q.lat_micro),
            int(q.lon_micro),
            q.start_date,
            q.end_date,
            int(q.drought_mm100),
            int(q.flood_mm100),
            payout,
        )
        premium = int(q.premium)
        if int(gl.message.value) != premium:
            raise gl.vm.UserError("Premium must equal " + str(premium))

        new_count = int(self.policy_count) + 1
        policy_id = str(new_count)
        self.policy_count = u256(new_count)
        pool.balance = u256(int(pool.balance) + premium)
        pool.reserved = u256(int(pool.reserved) + payout)
        pool.total_premiums = u256(int(pool.total_premiums) + premium)
        pool.policy_count = u32(int(pool.policy_count) + 1)
        pool.active_policies = u32(int(pool.active_policies) + 1)
        q.policy_id = policy_id
        self.policies[policy_id] = Policy(
            id=policy_id,
            pool_id=q.pool_id,
            quote_id=quote_id,
            farmer=gl.message.sender_address,
            lat_micro=q.lat_micro,
            lon_micro=q.lon_micro,
            start_date=q.start_date,
            end_date=q.end_date,
            drought_mm100=q.drought_mm100,
            flood_mm100=q.flood_mm100,
            payout=u256(payout),
            premium=u256(premium),
            rate_bps=q.rate_bps,
            status=STATUS_ACTIVE,
            purchased_on=today.isoformat(),
            settle_attempts=u32(0),
            paid_amount=u256(0),
            evidence="",
        )
        self._index_append(self.farmer_policies, gl.message.sender_address.as_hex, policy_id)
        self._index_append(self.pool_policies, q.pool_id, policy_id)
        return policy_id

    @gl.public.write
    def cancel_policy(self, policy_id: str) -> int:
        """Farmer cancels before coverage starts: full premium refund."""
        p = self._policy(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        if gl.message.sender_address != p.farmer:
            raise gl.vm.UserError("Only policy holder")
        if self._today() >= datetime.date.fromisoformat(p.start_date):
            raise gl.vm.UserError("Coverage already started")
        pool = self._pool(p.pool_id)
        refund = int(p.premium)
        p.status = STATUS_CANCELLED
        self._release(pool, p)
        pool.balance = u256(int(pool.balance) - refund)
        pool.total_refunded = u256(int(pool.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- settlement ----------------

    @gl.public.write
    def settle(self, policy_id: str) -> dict:
        p = self._policy(policy_id)
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

        pool = self._pool(p.pool_id)
        p.settle_attempts = u32(int(p.settle_attempts) + 1)
        p.evidence = json.dumps(ev, sort_keys=True)
        decision = recomputed["decision"]
        if decision == DECISION_PAYOUT:
            amount = recomputed["payout_wei"]
            p.status = STATUS_PAID_OUT
            p.paid_amount = u256(amount)
            self._release(pool, p)
            pool.balance = u256(int(pool.balance) - amount)
            pool.total_paid_out = u256(int(pool.total_paid_out) + amount)
            self._send(p.farmer, amount)
        elif decision == DECISION_NO_PAYOUT:
            p.status = STATUS_SETTLED_NO_PAYOUT
            self._release(pool, p)
        # DEFER: policy stays ACTIVE, reserve stays locked, evidence recorded
        return ev

    @gl.public.write
    def expire(self, policy_id: str) -> int:
        """After the claim window closes without settlement (sources missing or
        diverging the whole time), anyone can expire the policy: the premium is
        refunded to the farmer and the reserve is released."""
        p = self._policy(policy_id)
        if p.status != STATUS_ACTIVE:
            raise gl.vm.UserError("Policy not active")
        end = datetime.date.fromisoformat(p.end_date)
        deadline = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS + CLAIM_WINDOW_DAYS)
        if self._today() <= deadline:
            raise gl.vm.UserError("Claim window still open")
        pool = self._pool(p.pool_id)
        refund = int(p.premium)
        p.status = STATUS_EXPIRED_REFUNDED
        self._release(pool, p)
        pool.balance = u256(int(pool.balance) - refund)
        pool.total_refunded = u256(int(pool.total_refunded) + refund)
        self._send(p.farmer, refund)
        return refund

    # ---------------- view builders ----------------

    def _pool_view(self, pool: Pool) -> dict:
        return {
            "id": pool.id,
            "insurer": pool.insurer.as_hex,
            "name": pool.name,
            "min_rate_bps": int(pool.min_rate_bps),
            "loading_bps": int(pool.loading_bps),
            "lat_min": int(pool.lat_min),
            "lat_max": int(pool.lat_max),
            "lon_min": int(pool.lon_min),
            "lon_max": int(pool.lon_max),
            "accepting": bool(pool.accepting),
            "balance": int(pool.balance),
            "reserved": int(pool.reserved),
            "free_liquidity": self._free(pool),
            "total_deposits": int(pool.total_deposits),
            "total_premiums": int(pool.total_premiums),
            "total_paid_out": int(pool.total_paid_out),
            "total_refunded": int(pool.total_refunded),
            "total_withdrawn": int(pool.total_withdrawn),
            "policy_count": int(pool.policy_count),
            "active_policies": int(pool.active_policies),
        }

    def _quote_view(self, q: Quote) -> dict:
        return {
            "id": q.id,
            "pool_id": q.pool_id,
            "requester": q.requester.as_hex,
            "lat_micro": int(q.lat_micro),
            "lon_micro": int(q.lon_micro),
            "start_date": q.start_date,
            "end_date": q.end_date,
            "drought_trigger_mm100": int(q.drought_mm100),
            "flood_trigger_mm100": int(q.flood_mm100),
            "payout": int(q.payout),
            "rate_bps": int(q.rate_bps),
            "premium": int(q.premium),
            "hits": int(q.hits),
            "years": int(q.years),
            "created_on": q.created_on,
            "valid_until": q.valid_until,
            "policy_id": q.policy_id,
            "history": q.history,
        }

    def _policy_view(self, p: Policy) -> dict:
        end = datetime.date.fromisoformat(p.end_date)
        opens = end + datetime.timedelta(days=SETTLEMENT_DELAY_DAYS)
        return {
            "id": p.id,
            "pool_id": p.pool_id,
            "quote_id": p.quote_id,
            "farmer": p.farmer.as_hex,
            "lat_micro": int(p.lat_micro),
            "lon_micro": int(p.lon_micro),
            "start_date": p.start_date,
            "end_date": p.end_date,
            "drought_trigger_mm100": int(p.drought_mm100),
            "flood_trigger_mm100": int(p.flood_mm100),
            "payout": int(p.payout),
            "premium": int(p.premium),
            "rate_bps": int(p.rate_bps),
            "status": p.status,
            "purchased_on": p.purchased_on,
            "settle_opens": opens.isoformat(),
            "settle_deadline": (opens + datetime.timedelta(days=CLAIM_WINDOW_DAYS)).isoformat(),
            "settle_attempts": int(p.settle_attempts),
            "paid_amount": int(p.paid_amount),
            "evidence": p.evidence,
        }

    def _page(self, count: int, offset: int, limit: int) -> list:
        if offset < 0 or limit <= 0 or limit > MAX_PAGE:
            raise gl.vm.UserError("Invalid page (limit 1-50, offset >= 0)")
        first = offset + 1
        last = offset + limit
        if last > count:
            last = count
        return [str(i) for i in range(first, last + 1)]

    # ---------------- views ----------------

    @gl.public.view
    def get_stats(self) -> dict:
        balance = 0
        reserved = 0
        premiums = 0
        paid = 0
        for i in range(1, int(self.pool_count) + 1):
            pool = self.pools[str(i)]
            balance += int(pool.balance)
            reserved += int(pool.reserved)
            premiums += int(pool.total_premiums)
            paid += int(pool.total_paid_out)
        return {
            "pool_count": int(self.pool_count),
            "quote_count": int(self.quote_count),
            "policy_count": int(self.policy_count),
            "total_balance": balance,
            "total_reserved": reserved,
            "total_premiums": premiums,
            "total_paid_out": paid,
            "quote_years": QUOTE_YEARS,
            "quote_valid_days": QUOTE_VALID_DAYS,
            "settlement_delay_days": SETTLEMENT_DELAY_DAYS,
            "claim_window_days": CLAIM_WINDOW_DAYS,
        }

    @gl.public.view
    def get_pool(self, pool_id: str) -> dict:
        return self._pool_view(self._pool(pool_id))

    @gl.public.view
    def list_pools(self, offset: int, limit: int) -> list:
        return [self._pool_view(self.pools[i]) for i in self._page(int(self.pool_count), offset, limit)]

    @gl.public.view
    def get_quote(self, quote_id: str) -> dict:
        return self._quote_view(self._quote(quote_id))

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        return self._policy_view(self._policy(policy_id))

    @gl.public.view
    def list_policies(self, offset: int, limit: int) -> list:
        return [self._policy_view(self.policies[i]) for i in self._page(int(self.policy_count), offset, limit)]

    @gl.public.view
    def get_farmer_policies(self, farmer: str) -> list:
        key = Address(farmer).as_hex
        return [self._policy_view(self.policies[i]) for i in self._index_list(self.farmer_policies, key)]

    @gl.public.view
    def get_farmer_quotes(self, farmer: str) -> list:
        key = Address(farmer).as_hex
        return [self._quote_view(self.quotes[i]) for i in self._index_list(self.farmer_quotes, key)]

    @gl.public.view
    def get_pool_policies(self, pool_id: str) -> list:
        self._pool(pool_id)
        return [self._policy_view(self.policies[i]) for i in self._index_list(self.pool_policies, pool_id)]

    @gl.public.view
    def get_insurer_pools(self, insurer: str) -> list:
        key = Address(insurer).as_hex
        return [self._pool_view(self.pools[i]) for i in self._index_list(self.insurer_pools, key)]

    @gl.public.view
    def get_source_urls(self, policy_id: str) -> list:
        p = self._policy(policy_id)
        lat = int(p.lat_micro)
        lon = int(p.lon_micro)
        return [
            _open_meteo_url(lat, lon, p.start_date, p.end_date),
            _nasa_power_url(lat, lon, p.start_date, p.end_date),
        ]

    @gl.public.view
    def get_quote_source_url(self, quote_id: str) -> str:
        q = self._quote(quote_id)
        ev = json.loads(q.history)
        rng = _history_range(ev["windows"])
        return _open_meteo_url(int(q.lat_micro), int(q.lon_micro), rng[0], rng[1])
