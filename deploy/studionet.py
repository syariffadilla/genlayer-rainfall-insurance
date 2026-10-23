"""Deploy / interact with RainfallInsurance on GenLayer Studionet using genlayer-py.

  python deploy/studionet.py deploy [--rate-bps 1000]
  python deploy/studionet.py fund --address 0x... --gen 10
  python deploy/studionet.py buy --address 0x... --start 2026-11-01 --end 2026-11-30 --drought-mm 60 --payout-gen 1
  python deploy/studionet.py read --address 0x... [--policy 1]

The deployer key is read from $GENLAYER_PRIVATE_KEY, or from --key-file
(created with a fresh random key if it does not exist). Never commit it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import studionet
from genlayer_py.types import TransactionStatus

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "rainfall_insurance.py"
GEN = 10**18


def load_account(key_file: str):
    key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not key:
        p = Path(key_file)
        if not p.exists():
            acct = Account.create()
            p.write_text(acct.key.hex())
            os.chmod(p, 0o600)
            print(f"created new deployer key at {p}")
        key = p.read_text().strip()
    return Account.from_key(key)


def wait(client, tx_hash):
    receipt = client.wait_for_transaction_receipt(
        transaction_hash=tx_hash, status=TransactionStatus.ACCEPTED, interval=3000, retries=60
    )
    return receipt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["deploy", "fund", "buy", "read", "faucet"])
    ap.add_argument("--key-file", default=str(ROOT.parent / ".studionet-deployer.key"))
    ap.add_argument("--address")
    ap.add_argument("--rate-bps", type=int, default=1000)
    ap.add_argument("--gen", type=float, default=1)
    ap.add_argument("--lat-micro", type=int, default=-7327400)
    ap.add_argument("--lon-micro", type=int, default=108220700)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--drought-mm", type=int, default=60)
    ap.add_argument("--flood-mm", type=int, default=0)
    ap.add_argument("--payout-gen", type=float, default=1)
    ap.add_argument("--policy")
    a = ap.parse_args()

    account = load_account(a.key_file)
    client = create_client(chain=studionet, account=account)
    print("account", account.address)

    if a.cmd == "faucet":
        print(client.fund_account(address=account.address, amount=int(a.gen * GEN)).hex())
        print("balance", client.get_balance(account.address))
    elif a.cmd == "deploy":
        tx = client.deploy_contract(code=CONTRACT.read_text(), args=[a.rate_bps])
        print("deploy tx", tx)
        r = wait(client, tx)
        data = r.get("data") or {}
        print("contract", data.get("contract_address") or r.get("to_address") or r.get("recipient"))
        print(json.dumps({k: str(v) for k, v in r.items() if k in ("status", "status_name", "hash", "tx_id", "result", "to_address", "recipient")}, indent=1))
    elif a.cmd == "fund":
        tx = client.write_contract(address=a.address, function_name="fund_pool", value=int(a.gen * GEN))
        print("fund tx", tx)
        r = wait(client, tx)
        print("status", r.get("status_name") or r.get("status"))
    elif a.cmd == "buy":
        payout = int(a.payout_gen * GEN)
        premium = client.read_contract(address=a.address, function_name="quote_premium", args=[payout])
        tx = client.write_contract(
            address=a.address,
            function_name="buy_policy",
            value=int(premium),
            args=[a.lat_micro, a.lon_micro, a.start, a.end, a.drought_mm * 100, a.flood_mm * 100, payout],
        )
        print("buy tx", tx, "premium", premium)
        r = wait(client, tx)
        print("status", r.get("status_name") or r.get("status"))
    elif a.cmd == "read":
        print(json.dumps(client.read_contract(address=a.address, function_name="get_pool"), indent=1, default=str))
        if a.policy:
            print(json.dumps(client.read_contract(address=a.address, function_name="get_policy", args=[a.policy]), indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
