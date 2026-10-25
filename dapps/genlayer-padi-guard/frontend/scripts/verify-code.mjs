// Check that the code deployed at the contract address is byte-identical to contracts/padi_guard.py.
//   node scripts/verify-code.mjs [0xContract]
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { REPO_ROOT, contractAddress } from "./_env.mjs";
import { readClient } from "../src/lib/padiguard.js";

const A = contractAddress();
const onchain = await readClient().getContractCode(A);
const local = fs.readFileSync(path.join(REPO_ROOT, "contracts", "padi_guard.py"), "utf8");
const h = (s) => crypto.createHash("sha256").update(s).digest("hex");
console.log("contract      ", A);
console.log("on-chain sha256", h(onchain));
console.log("local sha256   ", h(local));
console.log(onchain === local ? "MATCH" : "DIFFERENT");
process.exit(onchain === local ? 0 : 1);
