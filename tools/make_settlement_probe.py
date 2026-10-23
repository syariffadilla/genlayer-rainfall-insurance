"""Generate a throwaway probe contract that runs settle()'s exact nondet pipeline
(_fetch_evidence + _evidence_matches + gl.vm.run_nondet) on an arbitrary PAST
window, so the leader/validator path can be exercised live on Studionet today
(the real contract only settles a window after it has ended + 7 days).

The helper code is copied verbatim from contracts/rainfall_insurance.py.

    python tools/make_settlement_probe.py > /tmp/settlement_probe.py
"""

from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "contracts" / "rainfall_insurance.py").read_text()
HEAD = SRC.split("# --------------------------------------------------------------------------\n# Storage types")[0]

PROBE = '''
class SettlementProbe(gl.Contract):
    last: str

    def __init__(self):
        self.last = ""

    @gl.public.write
    def check(self, lat: int, lon: int, start_date: str, end_date: str, drought: int, flood: int, payout: int) -> dict:
        pid = "probe"

        def leader_fn() -> dict:
            return _fetch_evidence(pid, lat, lon, start_date, end_date, drought, flood, payout)

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = _fetch_evidence(pid, lat, lon, start_date, end_date, drought, flood, payout)
            return _evidence_matches(leaders_res.calldata, mine, drought, flood, payout)

        ev = gl.vm.run_nondet(leader_fn, validator_fn)
        self.last = json.dumps(ev, sort_keys=True)
        return ev

    @gl.public.view
    def get_last(self) -> str:
        return self.last
'''

if __name__ == "__main__":
    import sys

    sys.stdout.write(HEAD + PROBE)
