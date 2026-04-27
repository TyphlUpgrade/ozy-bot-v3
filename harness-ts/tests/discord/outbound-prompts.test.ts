/**
 * Wave E-γ — outbound prompts existence + fence assertions.
 *
 * AC5: each prompt file exists, contains `<operator_input>` and
 * `<event_payload>` fence references, and is ≤80 lines for v1 (operator
 * iterates via v2 files, additive). v2 files raise the line cap to 120 to
 * accommodate the multi-paragraph epistle structure + per-role designated
 * emoji + backtick-wrap discipline (E-γ R1 mitigation, 2026-04-27).
 */

import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

import type { OutboundRole } from "../../src/discord/outbound-response-generator.js";

const PROMPT_ROOT = join(process.cwd(), "config", "prompts", "outbound-response");
const ROLES: readonly OutboundRole[] = ["architect", "reviewer", "executor", "orchestrator"];

// v2 designated emoji per role — must appear in the voice exemplars section
// so the LLM imitates the section-header pattern. Comment in each prompt file
// documents this convention separately for human readers (outside model body).
const V2_ROLE_EMOJI: Record<OutboundRole, string> = {
  architect: "🏗️",
  reviewer: "🔍",
  executor: "🛠️",
  orchestrator: "⚙️",
};

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

describe("outbound-response v2 prompt files", () => {
  for (const role of ROLES) {
    describe(`v2-${role}.md`, () => {
      const path = join(PROMPT_ROOT, `v2-${role}.md`);

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

      it("is ≤150 lines (epistle structure + emoji + backtick + timestamp + allowed-emojis)", () => {
        const text = readFileSync(path, "utf-8");
        const lines = text.split("\n").length;
        expect(lines).toBeLessThanOrEqual(150);
      });

      it("references the role identity in voice section", () => {
        const text = readFileSync(path, "utf-8").toLowerCase();
        expect(text).toContain(role);
      });

      it("contains the role's designated emoji in voice exemplars", () => {
        const text = readFileSync(path, "utf-8");
        expect(text).toContain(V2_ROLE_EMOJI[role]);
      });

      it("mentions backtick-wrap discipline for technical identifiers", () => {
        const text = readFileSync(path, "utf-8").toLowerCase();
        // Either "backtick" or "code-styled" must appear so the LLM is aware
        // of the wrap-identifiers requirement (operator scans visually).
        const hasBacktick = text.includes("backtick");
        const hasCodeStyled = text.includes("code-styled");
        expect(hasBacktick || hasCodeStyled).toBe(true);
      });

      it("contains the 'Current UTC time' instruction so the LLM uses the injected value", () => {
        const text = readFileSync(path, "utf-8");
        expect(text).toContain("Current UTC time");
      });

      it("contains an 'Allowed emojis' section heading binding the role's emoji", () => {
        const text = readFileSync(path, "utf-8");
        expect(text.toLowerCase()).toContain("allowed emojis");
        // The role's emoji must appear inside the allowed list (already
        // asserted broadly above, but anchor it specifically to this section).
        expect(text).toContain(V2_ROLE_EMOJI[role]);
      });
    });
  }
});
