import { describe, it, expect } from "vitest";
import { BudgetTracker, DEFAULT_THRESHOLDS } from "../../src/lib/budget.js";

describe("BudgetTracker", () => {
  it("returns no thresholds at 0% cost", () => {
    const tracker = new BudgetTracker(10.0);
    expect(tracker.update(0)).toEqual([]);
  });

  it("crosses 50% threshold", () => {
    const tracker = new BudgetTracker(10.0);
    const crossed = tracker.update(5.0);
    expect(crossed).toHaveLength(1);
    expect(crossed[0].label).toBe("50%");
  });

  it("crosses 80% threshold", () => {
    const tracker = new BudgetTracker(10.0);
    tracker.update(5.0); // cross 50%
    const crossed = tracker.update(8.0);
    expect(crossed).toHaveLength(1);
    expect(crossed[0].label).toBe("80%");
  });

  it("crosses both thresholds in one update", () => {
    const tracker = new BudgetTracker(10.0);
    const crossed = tracker.update(9.0);
    expect(crossed).toHaveLength(2);
    expect(crossed.map((t) => t.label)).toEqual(["50%", "80%"]);
  });

  it("fires each threshold at most once (dedup)", () => {
    const tracker = new BudgetTracker(10.0);
    const first = tracker.update(6.0);
    expect(first).toHaveLength(1); // 50%
    const second = tracker.update(7.0);
    expect(second).toHaveLength(0); // already fired
  });

  it("supports custom thresholds", () => {
    const tracker = new BudgetTracker(100.0, [
      { percent: 0.25, label: "25%" },
      { percent: 0.75, label: "75%" },
    ]);
    const crossed = tracker.update(30.0);
    expect(crossed).toHaveLength(1);
    expect(crossed[0].label).toBe("25%");
  });

  it("handles zero maxBudget without division by zero", () => {
    const tracker = new BudgetTracker(0);
    expect(tracker.update(5.0)).toEqual([]);
  });
});
