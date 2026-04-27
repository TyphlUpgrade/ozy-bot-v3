import { describe, it, expect } from "vitest";
import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

// Runtime import = any `import` that is NOT `import type`.
// The regex matches `import ` followed by anything that is NOT `type ` (negative look-ahead),
// then eventually `from "…/discord/…"`.  Multi-line flag so ^ works inside strings.
const RUNTIME_DISCORD_IMPORT = /^import\s+(?!type\s).*?from\s+["'][^"']*\/discord\//m;

async function tsFilesUnder(dir: string): Promise<string[]> {
  const entries = await readdir(dir, { recursive: true, withFileTypes: true });
  return entries
    .filter((e) => e.isFile() && e.name.endsWith(".ts"))
    .map((e) => join(e.path ?? dir, e.name));
}

describe("no-discord-leak", () => {
  it("src/lib/** has no runtime imports of discord/*", async () => {
    const files = await tsFilesUnder("src/lib");
    const violations: string[] = [];
    for (const f of files) {
      if (RUNTIME_DISCORD_IMPORT.test(await readFile(f, "utf-8"))) violations.push(f);
    }
    expect(violations).toEqual([]);
  });

  it("src/session/** has no runtime imports of discord/*", async () => {
    const files = await tsFilesUnder("src/session");
    const violations: string[] = [];
    for (const f of files) {
      if (RUNTIME_DISCORD_IMPORT.test(await readFile(f, "utf-8"))) violations.push(f);
    }
    expect(violations).toEqual([]);
  });
});
