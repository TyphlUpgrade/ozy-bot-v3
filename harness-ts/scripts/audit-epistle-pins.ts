/**
 * Audit script — verifies that every `.toContain("X")` literal in
 * tests/discord/notifier.test.ts is covered by at least one rendered epistle
 * from SMOKE_FIXTURES.
 *
 * Usage: npm run audit:epistle-pins
 *
 * Regex intentionally handles single+double quotes only.
 * If backtick template literals introduced in notifier.test.ts, tighten regex.
 */

import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { SMOKE_FIXTURES } from "./live-discord-smoke.js";
import { renderEpistle } from "../src/discord/epistle-templates.js";
import { resolveIdentity } from "../src/discord/identity.js";
import { frozenCtx } from "../tests/discord/fixtures/epistle-timestamp.js";

// Regex intentionally handles single+double quotes only.
// If backtick template literals introduced in notifier.test.ts, tighten regex.
const TO_CONTAIN_RE = /\.toContain\(["']([^"']+)["']\)/g;

async function main(): Promise<void> {
  const src = await readFile("tests/discord/notifier.test.ts", "utf-8");
  const pins = new Set<string>();
  let m: RegExpExecArray | null;
  while ((m = TO_CONTAIN_RE.exec(src)) !== null) pins.add(m[1]);

  const rendered: string[] = [];
  for (const fx of SMOKE_FIXTURES) {
    rendered.push(renderEpistle(fx, resolveIdentity(fx), frozenCtx()));
  }
  const all = rendered.join("\n");

  const missing: string[] = [];
  for (const pin of pins) {
    if (!all.includes(pin)) missing.push(pin);
  }
  if (missing.length > 0) {
    console.error("[audit] missing pins:", missing);
    process.exit(1);
  }
  console.log(`[audit] all ${pins.size} pins covered across ${SMOKE_FIXTURES.length} fixtures`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => {
    console.error("[audit] FATAL", e);
    process.exit(2);
  });
}
