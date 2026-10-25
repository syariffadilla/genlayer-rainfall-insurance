// PadiGuard client library: thin wrapper around genlayer-js used by BOTH the
// browser app (src/main.js) and the Node scripts (scripts/*.mjs), so the
// live end-to-end script exercises the same code paths as the UI.
import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";

export const GEN = 10n ** 18n;
export const CHAIN = studionet;

// ------------------------------------------------------------------ units

/** "1.5" GEN -> wei (bigint), no floats. */
export function genToWei(s) {
  const str = String(s).trim();
  if (!/^\d+(\.\d{0,18})?$/.test(str)) throw new Error(`Invalid GEN amount: ${s}`);
  const [whole, frac = ""] = str.split(".");
  return BigInt(whole) * GEN + BigInt((frac + "0".repeat(18)).slice(0, 18));
}

/** wei -> "1.5" (trimmed). */
export function weiToGen(w, maxDecimals = 4) {
  const v = BigInt(w);
  const neg = v < 0n;
  const a = neg ? -v : v;
  const whole = a / GEN;
  let frac = (a % GEN).toString().padStart(18, "0").slice(0, maxDecimals).replace(/0+$/, "");
  return (neg ? "-" : "") + whole.toString() + (frac ? "." + frac : "");
}

/** "-7.3274" degrees -> micro-degrees int (no floats). */
export function degToMicro(s) {
  const str = String(s).trim();
  if (!/^-?\d{1,3}(\.\d{0,6})?$/.test(str)) throw new Error(`Invalid coordinate: ${s}`);
  const neg = str.startsWith("-");
  const [w, f = ""] = str.replace("-", "").split(".");
  const v = Number(w) * 1_000_000 + Number((f + "000000").slice(0, 6));
  return neg ? -v : v;
}

export function microToDeg(v) {
  const n = Number(v);
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  return `${sign}${Math.floor(a / 1e6)}.${String(a % 1e6).padStart(6, "0")}`;
}

/** "60" or "60.5" mm -> hundredths of mm (int). Empty -> 0 (disabled). */
export function mmToMm100(s) {
  const str = String(s ?? "").trim();
  if (str === "") return 0;
  if (!/^\d+(\.\d{0,2})?$/.test(str)) throw new Error(`Invalid rainfall amount: ${s}`);
  const [w, f = ""] = str.split(".");
  return Number(w) * 100 + Number((f + "00").slice(0, 2));
}

export const mm100ToMm = (v) => (Number(v) / 100).toFixed(2);
export const bpsToPct = (v) => (Number(v) / 100).toFixed(2) + " %";

// ------------------------------------------------------------------ clients

export function readClient() {
  return createClient({ chain: CHAIN });
}

/** Client signing with a local private key (burner / script account). */
export function keyClient(privateKey) {
  const account = createAccount(privateKey);
  return { client: createClient({ chain: CHAIN, account }), address: account.address, account };
}

export function newBurnerKey() {
  return generatePrivateKey();
}

/** Client signing through an injected wallet (MetaMask). */
export async function walletClient(provider) {
  const [address] = await provider.request({ method: "eth_requestAccounts" });
  await ensureStudionet(provider);
  const client = createClient({ chain: CHAIN, account: address, provider });
  return { client, address };
}

/**
 * Add/switch the wallet to GenLayer Studionet. Unlike genlayer-js connect(), this does
 * not require the GenLayer MetaMask Snap, which many wallets (or users) reject.
 */
async function ensureStudionet(provider) {
  const chainId = "0x" + CHAIN.id.toString(16);
  const current = await provider.request({ method: "eth_chainId" });
  if (current === chainId) return;
  try {
    await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId }] });
  } catch (e) {
    if (e?.code !== 4902 && !/unrecognized|not added|unknown chain/i.test(e?.message || "")) throw e;
    await provider.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId,
        chainName: CHAIN.name,
        rpcUrls: CHAIN.rpcUrls.default.http,
        nativeCurrency: CHAIN.nativeCurrency,
        blockExplorerUrls: CHAIN.blockExplorers?.default?.url ? [CHAIN.blockExplorers.default.url] : undefined,
      }],
    });
  }
}

/** Studio-only faucet (simulated balances). */
export async function studioFaucet(client, address, gen) {
  return client.request({ method: "sim_fundAccount", params: [address, Number(gen) * 1e18] });
}

export async function balanceOf(client, address) {
  return client.getBalance({ address });
}

// ------------------------------------------------------------------ calls

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** View call with a few retries: the public Studio RPC occasionally drops a request
 * (seen in the browser as a CORS/"Failed to fetch" error) right after a write. */
export async function read(client, address, functionName, args = [], attempts = 4) {
  let last;
  for (let i = 0; i < attempts; i++) {
    try {
      return await client.readContract({ address, functionName, args });
    } catch (e) {
      last = e;
      const msg = String(e?.details || e?.message || "");
      if (/execution failed/i.test(msg)) throw e; // contract-level error (e.g. unknown id): don't retry
      await sleep(1500 * (i + 1));
    }
  }
  throw last;
}

/** Pull the useful bits out of a GenLayer receipt. */
export function summarizeReceipt(receipt) {
  const lr0 = receipt?.consensus_data?.leader_receipt;
  const lr = Array.isArray(lr0) ? lr0[0] : lr0;
  let error = null;
  let returned = null;
  const res = lr?.result;
  if (res && typeof res === "object") {
    if (res.status === "return") returned = res.payload?.readable ?? null;
    else error = res.payload?.readable ?? res.payload ?? res.status;
  } else if (typeof res === "string") {
    returned = res;
  }
  if (!error && lr?.execution_result && lr.execution_result !== "SUCCESS") {
    error = lr?.genvm_result?.stderr || lr.execution_result;
  }
  return {
    hash: receipt?.hash,
    status: receipt?.status_name ?? receipt?.statusName ?? String(receipt?.status),
    consensus: receipt?.result_name ?? receipt?.resultName ?? null,
    execution: lr?.execution_result ?? null,
    ok: (lr?.execution_result ?? "SUCCESS") === "SUCCESS" && !error,
    returned,
    error,
    votes: receipt?.consensus_data?.votes ?? null,
  };
}

/**
 * Send a write transaction and wait until it is ACCEPTED.
 * onUpdate(stage, info) is called with "sent" (hash) and "accepted" (summary).
 */
export async function write(client, address, functionName, args = [], value = 0n, onUpdate = () => {}, opts = {}) {
  const hash = await client.writeContract({ address, functionName, args, value: BigInt(value) });
  onUpdate("sent", { hash });
  const receipt = await client.waitForTransactionReceipt({
    hash,
    status: TransactionStatus.ACCEPTED,
    interval: opts.interval ?? 4000,
    retries: opts.retries ?? 150,
  });
  const summary = summarizeReceipt(receipt);
  summary.hash = summary.hash || hash;
  onUpdate("accepted", summary);
  return summary;
}

// ------------------------------------------------------------------ contract API

export const api = {
  stats: (c, a) => read(c, a, "get_stats"),
  pools: (c, a, offset = 0, limit = 50) => read(c, a, "list_pools", [offset, limit]),
  pool: (c, a, id) => read(c, a, "get_pool", [String(id)]),
  insurerPools: (c, a, who) => read(c, a, "get_insurer_pools", [who]),
  policies: (c, a, offset = 0, limit = 50) => read(c, a, "list_policies", [offset, limit]),
  policy: (c, a, id) => read(c, a, "get_policy", [String(id)]),
  farmerPolicies: (c, a, who) => read(c, a, "get_farmer_policies", [who]),
  farmerQuotes: (c, a, who) => read(c, a, "get_farmer_quotes", [who]),
  poolPolicies: (c, a, id) => read(c, a, "get_pool_policies", [String(id)]),
  quote: (c, a, id) => read(c, a, "get_quote", [String(id)]),
  sourceUrls: (c, a, id) => read(c, a, "get_source_urls", [String(id)]),
  quoteSourceUrl: (c, a, id) => read(c, a, "get_quote_source_url", [String(id)]),

  createPool: (c, a, p, value, cb) =>
    write(c, a, "create_pool", [p.name, p.minRateBps, p.loadingBps, p.latMin, p.latMax, p.lonMin, p.lonMax], value, cb),
  fundPool: (c, a, id, value, cb) => write(c, a, "fund_pool", [String(id)], value, cb),
  withdraw: (c, a, id, amount, cb) => write(c, a, "withdraw", [String(id), BigInt(amount)], 0n, cb),
  setPoolAccepting: (c, a, id, on, cb) => write(c, a, "set_pool_accepting", [String(id), Boolean(on)], 0n, cb),
  setPoolPricing: (c, a, id, minRateBps, loadingBps, cb) =>
    write(c, a, "set_pool_pricing", [String(id), minRateBps, loadingBps], 0n, cb),
  requestQuote: (c, a, t, cb) =>
    write(
      c,
      a,
      "request_quote",
      [String(t.poolId), t.latMicro, t.lonMicro, t.startDate, t.endDate, t.droughtMm100, t.floodMm100, BigInt(t.payoutWei)],
      0n,
      cb,
    ),
  buyPolicy: (c, a, quoteId, premiumWei, cb) => write(c, a, "buy_policy", [String(quoteId)], BigInt(premiumWei), cb),
  cancelPolicy: (c, a, id, cb) => write(c, a, "cancel_policy", [String(id)], 0n, cb),
  settle: (c, a, id, cb) => write(c, a, "settle", [String(id)], 0n, cb),
  expire: (c, a, id, cb) => write(c, a, "expire", [String(id)], 0n, cb),
};

/** Settlement phase of a policy on a given UTC date (mirrors the contract rules). */
export function settlementPhase(policy, todayIso = new Date().toISOString().slice(0, 10)) {
  if (policy.status !== "ACTIVE") return "CLOSED";
  if (todayIso < policy.start_date) return "BEFORE_COVERAGE"; // cancel allowed
  if (todayIso < policy.settle_opens) return "COVERAGE_OR_WAITING_DATA";
  if (todayIso <= policy.settle_deadline) return "SETTLE_OPEN";
  return "EXPIRABLE";
}
