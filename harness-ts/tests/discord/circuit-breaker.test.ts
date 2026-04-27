/**
 * Wave E-γ — PerRoleCircuitBreaker tests.
 *
 * In-memory only (no file persistence). 3 consecutive failures opens; success
 * between failures resets the counter. Per-role independent state.
 */

import { describe, it, expect } from "vitest";

import { PerRoleCircuitBreaker } from "../../src/discord/llm-budget.js";
import type { OutboundRole } from "../../src/discord/outbound-response-generator.js";

const ALL_ROLES: readonly OutboundRole[] = [
  "architect",
  "reviewer",
  "executor",
  "orchestrator",
];

describe("PerRoleCircuitBreaker", () => {
  it("isClosed returns true initially for all roles", () => {
    const cb = new PerRoleCircuitBreaker();
    for (const role of ALL_ROLES) {
      expect(cb.isClosed(role)).toBe(true);
    }
  });

  it("opens breaker after exactly 3 consecutive failures", () => {
    const cb = new PerRoleCircuitBreaker();
    cb.recordFailure("executor");
    expect(cb.isClosed("executor")).toBe(true);
    cb.recordFailure("executor");
    expect(cb.isClosed("executor")).toBe(true);
    cb.recordFailure("executor");
    expect(cb.isClosed("executor")).toBe(false);
  });

  it("recordSuccess resets the consecutive-failure counter", () => {
    const cb = new PerRoleCircuitBreaker();
    cb.recordFailure("reviewer");
    cb.recordFailure("reviewer");
    cb.recordSuccess("reviewer");
    cb.recordFailure("reviewer");
    cb.recordFailure("reviewer");
    expect(cb.isClosed("reviewer")).toBe(true);
    cb.recordFailure("reviewer");
    expect(cb.isClosed("reviewer")).toBe(false);
  });

  it("per-role independence: opening one role does not open another", () => {
    const cb = new PerRoleCircuitBreaker();
    cb.recordFailure("executor");
    cb.recordFailure("executor");
    cb.recordFailure("executor");
    expect(cb.isClosed("executor")).toBe(false);
    expect(cb.isClosed("architect")).toBe(true);
    expect(cb.isClosed("reviewer")).toBe(true);
    expect(cb.isClosed("orchestrator")).toBe(true);
  });

  it("once open, stays open across further failures (no reset)", () => {
    const cb = new PerRoleCircuitBreaker();
    for (let i = 0; i < 3; i++) cb.recordFailure("orchestrator");
    expect(cb.isClosed("orchestrator")).toBe(false);
    cb.recordFailure("orchestrator");
    cb.recordSuccess("orchestrator"); // success while open is a no-op
    expect(cb.isClosed("orchestrator")).toBe(false);
  });
});
