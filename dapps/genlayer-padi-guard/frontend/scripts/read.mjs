// Read-only: print the live state of the deployed contract (no key needed).
//   node scripts/read.mjs [0xContract]
import { contractAddress, json } from "./_env.mjs";
import { api, readClient } from "../src/lib/padiguard.js";

const A = contractAddress();
const c = readClient();
console.log("contract", A);
console.log("stats", json(await api.stats(c, A)));
console.log("pools", json(await api.pools(c, A, 0, 50)));
const ps = await api.policies(c, A, 0, 50);
console.log("policies", json(ps.map(({ evidence, ...p }) => p)));
