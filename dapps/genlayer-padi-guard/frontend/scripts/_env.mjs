// Shared helpers for the Node scripts (deploy / e2e / read).
// Keys are throwaway Studionet keys stored OUTSIDE the repository
// (default: ../../.studionet-padiguard-<role>.key relative to this repo root),
// or passed via PADIGUARD_<ROLE>_KEY. Never commit a key.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createAccount, generatePrivateKey } from "genlayer-js";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Throwaway private key (hex) for a role; created on first use. */
export function loadKeyHex(role) {
  const env = process.env[`PADIGUARD_${role.toUpperCase()}_KEY`];
  if (env) return env.trim();
  const file = process.env[`PADIGUARD_${role.toUpperCase()}_KEY_FILE`] ||
    path.join(REPO_ROOT, "..", `.studionet-padiguard-${role}.key`);
  if (!fs.existsSync(file)) {
    fs.writeFileSync(file, generatePrivateKey(), { mode: 0o600 });
    console.log(`created new throwaway ${role} key at ${file}`);
  }
  return fs.readFileSync(file, "utf8").trim();
}

export function loadKey(role) {
  return createAccount(loadKeyHex(role));
}

export function contractAddress() {
  const a = process.env.PADIGUARD_ADDRESS || process.argv.find((x) => /^0x[0-9a-fA-F]{40}$/.test(x));
  if (a) return a;
  const cfg = fs.readFileSync(path.join(REPO_ROOT, "frontend", "src", "config.js"), "utf8");
  const m = cfg.match(/CONTRACT_ADDRESS\s*=\s*"(0x[0-9a-fA-F]{40})"/);
  if (!m) throw new Error("no contract address: pass 0x... or set PADIGUARD_ADDRESS");
  return m[1];
}

export const json = (x) =>
  JSON.stringify(x, (_, v) => (typeof v === "bigint" ? v.toString() : v instanceof Map ? Object.fromEntries(v) : v), 1);
