/**
 * Wave R8 — read ANTHROPIC_API_KEY from a file outside `process.env`.
 *
 * Why this exists:
 *   - The harness's claude-agent-sdk path wraps the Claude Code CLI subprocess
 *     and SHOULD bill via the operator's Claude subscription
 *     (`~/.claude/.credentials.json`).
 *   - Direct Anthropic Messages API callers (Discord intent classifier,
 *     response generator, outbound LLM voice) DO need a real API key —
 *     subscription auth doesn't cover api.anthropic.com calls.
 *   - When `ANTHROPIC_API_KEY` lives in `process.env`, the CLI subprocess
 *     inherits it and silently flips to API billing. Cycle 4 leaked ~$30
 *     of Sonnet usage exactly this way.
 *   - Fix: keep the API key in a SEPARATE file (`.env.anthropic`) that is
 *     never sourced into the shell or `process.env`. Entry-point scripts
 *     read it explicitly + pass it to `new Anthropic({ apiKey })`.
 *     The CLI subprocess sees no `ANTHROPIC_API_KEY` and falls back to
 *     subscription auth.
 *
 * Convention:
 *   - File path: `<repo-root>/.env.anthropic`
 *   - File shape: `ANTHROPIC_API_KEY=sk-ant-...` (matching dotenv form for
 *     consistency, though only this single key is read).
 *   - File mode: 600. Gitignored via `.env.*` glob.
 *
 * NEVER export the value into `process.env` — that defeats the entire fix.
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
// scripts/lib/api-key.ts → repo root is three levels up from this file
// (lib → scripts → harness-ts → repo).
const DEFAULT_REPO_ROOT = dirname(dirname(dirname(__dirname)));

export interface ReadApiKeyOpts {
  /** Override the path. Defaults to `<repo-root>/.env.anthropic`. */
  path?: string;
  /** When true, fall back to `process.env.ANTHROPIC_API_KEY` if file missing.
   * Default false — explicit failure is preferable to a silent leak. */
  envFallback?: boolean;
}

/**
 * Read ANTHROPIC_API_KEY from `.env.anthropic`. Throws if missing unless
 * `envFallback: true`. Caller passes the returned string directly to
 * `new Anthropic({ apiKey })`.
 */
export function readAnthropicApiKey(opts: ReadApiKeyOpts = {}): string {
  const path = opts.path ?? join(DEFAULT_REPO_ROOT, ".env.anthropic");
  if (existsSync(path)) {
    const raw = readFileSync(path, "utf-8");
    for (const line of raw.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const eq = trimmed.indexOf("=");
      if (eq === -1) continue;
      const key = trimmed.slice(0, eq).trim();
      if (key !== "ANTHROPIC_API_KEY") continue;
      let value = trimmed.slice(eq + 1).trim();
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      if (!value) break;
      return value;
    }
  }
  if (opts.envFallback && process.env.ANTHROPIC_API_KEY) {
    // Used by tests / one-off probes that need the key but accept the leak.
    // Production scripts should NOT pass envFallback:true.
    return process.env.ANTHROPIC_API_KEY;
  }
  throw new Error(
    `ANTHROPIC_API_KEY not found in ${path}. ` +
    `Create the file with shape "ANTHROPIC_API_KEY=sk-ant-..." and chmod 600. ` +
    `See scripts/lib/api-key.ts for the full rationale.`,
  );
}
