/**
 * Reusable stub ReviewGate for live-run scripts and future arbitration
 * scenario tests. Programmed with a verdict queue: each `runReview` call
 * consumes one entry, falling back to a trailing default when exhausted.
 *
 * Only covers the two ReviewGate members the orchestrator actually calls
 * (`runReview` + `arbitrationThreshold`); callers cast to `ReviewGate` via
 * `Pick<ReviewGate, ...>` and `as unknown as ReviewGate`.
 */

import type { ReviewGate, ReviewResult, ReviewVerdict } from "../../src/gates/review.js";
import type { TaskRecord } from "../../src/lib/state.js";
import type { CompletionSignal } from "../../src/session/manager.js";

export interface StubReviewGateOpts {
  /** Sequence of verdicts for successive runReview calls. */
  queue: readonly ReviewVerdict[];
  /** Fallback when the queue is exhausted. Default "approve". */
  defaultVerdict?: ReviewVerdict;
  /** arbitrationThreshold exposed to orchestrator. Default 1 (so first reject fires arbitration). */
  arbitrationThreshold?: number;
  /** Optional override for reject summaries. Keyed by call index (1-based). */
  rejectSummaryByCall?: Record<number, string>;
  /** Optional override for findings per call index (1-based). */
  rejectFindingsByCall?: Record<number, ReviewResult["findings"]>;
  /** Silence the default console.log. */
  silent?: boolean;
}

const DEFAULT_REJECT_FINDINGS: ReviewResult["findings"] = [
  {
    severity: "high",
    dimension: "correctness",
    description:
      "File created but missing the required `// HELLO-V2` trailer comment required by the spec.",
  },
];

const DEFAULT_REJECT_SUMMARY =
  "Missing mandatory trailer comment `// HELLO-V2`. Spec is explicit; Executor omitted it. Retry with directive to append the trailer on a final line.";

export class StubReviewGate
  implements Pick<ReviewGate, "runReview" | "arbitrationThreshold">
{
  readonly arbitrationThreshold: number;
  private callCount = 0;
  private readonly queue: ReviewVerdict[];
  private readonly defaultVerdict: ReviewVerdict;
  private readonly rejectSummaryByCall: Record<number, string>;
  private readonly rejectFindingsByCall: Record<number, ReviewResult["findings"]>;
  private readonly silent: boolean;

  constructor(opts: StubReviewGateOpts) {
    this.queue = [...opts.queue];
    this.defaultVerdict = opts.defaultVerdict ?? "approve";
    this.arbitrationThreshold = opts.arbitrationThreshold ?? 1;
    this.rejectSummaryByCall = opts.rejectSummaryByCall ?? {};
    this.rejectFindingsByCall = opts.rejectFindingsByCall ?? {};
    this.silent = opts.silent ?? false;
  }

  async runReview(
    _task: TaskRecord,
    _worktreePath: string,
    _completion: CompletionSignal,
  ): Promise<ReviewResult> {
    this.callCount += 1;
    const verdict = this.queue.shift() ?? this.defaultVerdict;
    if (!this.silent) {
      console.log(`  [stub-reviewer] call ${this.callCount} → ${verdict.toUpperCase()}`);
    }
    if (verdict === "approve") {
      return {
        verdict: "approve",
        riskScore: {
          correctness: 0.1,
          integration: 0.1,
          stateCorruption: 0.0,
          performance: 0.0,
          regression: 0.1,
          weighted: 0.08,
        },
        findings: [],
        summary: "All required elements present. Approved.",
      };
    }
    // reject / request_changes
    return {
      verdict,
      riskScore: {
        correctness: 0.8,
        integration: 0.3,
        stateCorruption: 0.1,
        performance: 0.1,
        regression: 0.2,
        weighted: 0.62,
      },
      findings: this.rejectFindingsByCall[this.callCount] ?? DEFAULT_REJECT_FINDINGS,
      summary: this.rejectSummaryByCall[this.callCount] ?? DEFAULT_REJECT_SUMMARY,
    };
  }

  /** Number of runReview calls made so far. */
  get callsMade(): number {
    return this.callCount;
  }
}
