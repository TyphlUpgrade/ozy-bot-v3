/**
 * Ops-channel formatting helper for ReviewFinding objects.
 *
 * Extracted from src/gates/review.ts so that discord/epistle-templates.ts can
 * import it without creating a discord/ → gates/ runtime dependency.
 * src/gates/review.ts re-exports this function for back-compat.
 */

import type { ReviewFinding } from "../gates/review.js"; // type-only — no runtime coupling

/**
 * Render a single ReviewFinding as a one-line ops-channel string.
 *
 * Format: `[severity] file:line — description`
 * When `line` is absent, substitutes `?`.
 */
export function formatFindingForOps(f: ReviewFinding): string {
  const line = f.line !== undefined ? String(f.line) : "?";
  return `[${f.severity}] ${f.file}:${line} — ${f.description}`;
}
