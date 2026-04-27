/**
 * Wave E-γ — outbound prompts existence + fence assertions.
 *
 * AC5: each prompt file exists, contains `<operator_input>` and
 * `<event_payload>` fence references, and is ≤80 lines (operator can iterate
 * via v2 files later, additive).
 */

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import type { OutboundRole } from "../../src/discord/outbound-response-generator.js";

const PROMPT_ROOT = join(process.cwd(), "config", "prompts", "outbound-response");
const ROLES: readonly OutboundRole[] = ["architect", "reviewer", "executor", "orchestrator"];

describe("outbound-response v1 prompt files", () => {
  for (const role of ROLES) {
    describe(`v1-${role}.md`, () => {
      const path = join(PROMPT_ROOT, `v1-${role}.md`);

      it("file exists", () => {
        expect(existsSync(path)).toBe(true);
      });

      it("contains required <operator_input> fence reference", () => {
        const text = readFileSync(path, "utf-8");
        expect(text).toContain("<operator_input>");
      });

      it("contains required <event_payload> fence reference", () => {
        const text = readFileSync(path, "utf-8");
        expect(text).toContain("<event_payload>");
      });

      it("is ≤80 lines (operator iterates via v2 files later)", () => {
        const text = readFileSync(path, "utf-8");
        const lines = text.split("\n").length;
        expect(lines).toBeLessThanOrEqual(80);
      });

      it("references the role identity in voice section", () => {
        const text = readFileSync(path, "utf-8").toLowerCase();
        expect(text).toContain(role);
      });
    });
  }
});
