/**
 * Budget tracking — emits threshold crossings for cost monitoring.
 * Informational only in Phase 2A (O6 principle). No pipeline pausing.
 */

export interface BudgetThreshold {
  percent: number;
  label: string;
}

export const DEFAULT_THRESHOLDS: BudgetThreshold[] = [
  { percent: 0.50, label: "50%" },
  { percent: 0.80, label: "80%" },
];

export class BudgetTracker {
  private readonly triggered = new Set<number>();

  constructor(
    private readonly maxBudgetUsd: number,
    private readonly thresholds: BudgetThreshold[] = DEFAULT_THRESHOLDS,
  ) {}

  /**
   * Update with cumulative cost. Returns newly-crossed thresholds.
   * Each threshold fires at most once (deduplication via triggered set).
   */
  update(cumulativeCostUsd: number): BudgetThreshold[] {
    if (this.maxBudgetUsd <= 0) return [];

    const crossed: BudgetThreshold[] = [];
    const ratio = cumulativeCostUsd / this.maxBudgetUsd;

    for (const t of this.thresholds) {
      if (ratio >= t.percent && !this.triggered.has(t.percent)) {
        this.triggered.add(t.percent);
        crossed.push(t);
      }
    }

    return crossed;
  }
}
