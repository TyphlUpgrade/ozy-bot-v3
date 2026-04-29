/**
 * Epistle renderer — multi-paragraph Discord message templates for the six
 * "epistle-eligible" OrchestratorEvent types (Wave E-α D3).
 *
 * renderEpistle(event, identity, ctx) → string
 *
 * All outputs are wrapped with truncateBody(…, 1900).  Caller injects
 * EpistleContext so timestamps are deterministic in tests; production callers
 * use defaultCtx() which captures wall-clock time at dispatch.
 *
 * Events not in the epistle-eligible set fall through to a compact single-line
 * fallback (mirrors pre-wave inline NOTIFIER_MAP format).
 */

import type { OrchestratorEvent } from "../orchestrator.js";
import type { IdentityRole } from "./identity.js";
import { formatFindingForOps } from "../lib/review-format.js";
import { sanitize, truncateRationale } from "../lib/text.js";

// --- Types ---

export interface EpistleContext {
  timestamp: string;
}

export function defaultCtx(): EpistleContext {
  return { timestamp: new Date().toISOString() };
}

// --- Internal helpers (mirrors notifier.ts helpers, no re-export needed) ---

function truncateBody(body: string, max = 1900): string {
  if (body.length <= max) return body;
  return body.slice(0, max - 1) + "…";
}

function shortTaskId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-3)}` : id;
}

function shortProjectId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

// --- Role emoji map ---

// Match v2 outbound-response prompt convention (config/prompts/outbound-response/v2-*.md).
// Section header emoji per role; LLM voice transformer uses the same map per role.
const ROLE_EMOJI: Record<IdentityRole, string> = {
  executor: "🛠️",
  reviewer: "🔍",
  architect: "🏗️",
  orchestrator: "⚙️",
};

/** Format an ISO timestamp to "HH:MM UTC" for section headers. */
function formatTs(iso: string): string {
  return `${iso.slice(11, 16)} UTC`;
}

// --- Epistle renderer ---

/**
 * Render a multi-paragraph epistle for epistle-eligible events, or a compact
 * single-line fallback for all other event types.
 *
 * Epistle-eligible events: session_complete, task_done, merge_result,
 * task_failed, escalation_needed, project_failed, review_mandatory.
 */
export function renderEpistle(
  event: OrchestratorEvent,
  identity: IdentityRole,
  ctx: EpistleContext,
): string {
  const emoji = ROLE_EMOJI[identity];
  const ts = ctx.timestamp;

  switch (event.type) {
    case "session_complete": {
      if (event.success) {
        const id = shortTaskId(event.taskId);
        // "Session complete" + "success" on same line — integration test :237 regex
        // `/Session complete.*success/i` requires both tokens on one line (`.` ≠ newline).
        return truncateBody(`Session complete for \`${id}\`: success`);
      } else {
        const errors = event.errors ?? [];
        const errSummary = errors.length > 0
          ? sanitize(errors.join("; "), 200)
          : "(no error detail)";
        const tr = event.terminalReason ? ` [${sanitize(event.terminalReason, 64)}]` : "";
        const id = shortTaskId(event.taskId);
        // Phase A pin :309 — "failure — boom1; boom2 [budget_exceeded]" exact substring
        // (em-dash U+2014 + space; semicolon-space joining errors; bracketed terminalReason)
        return truncateBody(
          `Session complete for \`${id}\`: failure — ${errSummary}${tr}`,
        );
      }
    }

    case "task_done": {
      const id = shortTaskId(event.taskId);
      const lvl = event.responseLevelName
        ? ` (response level: ${sanitize(event.responseLevelName, 40)})`
        : "";
      // Wave E-α commit 2: structured form when summary or filesChanged present (D1 R-IT5-5).
      // Compact form preserved for backward compat — existing /response level: reviewed/ pin (:323)
      // is satisfied by `lvl` appearing in both branches.
      if (event.summary || (event.filesChanged && event.filesChanged.length > 0)) {
        const lines = [`${emoji} **Task Complete** — ${formatTs(ts)}`, ""];
        lines.push(`Task \`${id}\` completed${lvl}.`);
        if (event.summary) {
          lines.push("", `**Summary:** ${sanitize(event.summary, 500)}`);
        }
        if (event.filesChanged && event.filesChanged.length > 0) {
          lines.push("", "**Files changed:**");
          for (const f of event.filesChanged.slice(0, 10)) {
            lines.push(`- \`${sanitize(f, 200)}\``);
          }
          if (event.filesChanged.length > 10) {
            lines.push(`- *+${event.filesChanged.length - 10} more*`);
          }
        }
        return truncateBody(lines.join("\n"));
      }
      // Compact form when no structured data.
      return truncateBody(`Task \`${id}\` complete${lvl}`);
    }

    case "merge_result": {
      const id = shortTaskId(event.taskId);
      const status = event.result.status;
      const head = `Merge result for \`${id}\`: **${sanitize(status, 40)}**`;
      let tail = "";
      if (status === "merged") {
        const sha = event.result.commitSha;
        if (sha) tail = ` (${sha.slice(0, 7)})`;
      } else if (status === "test_failed" || status === "error") {
        const err = event.result.error;
        if (err) tail = ` — ${sanitize(err, 200)}`;
      } else if (status === "rebase_conflict") {
        const files = event.result.conflictFiles ?? [];
        const n = files.length;
        const first3 = files.slice(0, 3).map((f) => sanitize(f, 80)).join(", ");
        tail = ` — ${n} files: ${first3}`;
      }
      return truncateBody(head + tail);
    }

    case "task_failed": {
      const attempt = event.attempt ?? 0;
      return truncateBody(
        `Task \`${shortTaskId(event.taskId)}\` **FAILED** (attempt ${attempt}): ${sanitize(event.reason)}`,
      );
    }

    case "escalation_needed": {
      const id = shortTaskId(event.taskId);
      const t = sanitize(event.escalation.type, 40);
      const q = sanitize(event.escalation.question ?? event.escalation.type);
      const opts = event.escalation.options && event.escalation.options.length > 0
        ? `\nOptions: ${event.escalation.options.map((o) => sanitize(o, 80)).join(" | ")}`
        : "";
      const ctx2 = event.escalation.context
        ? `\nContext: ${sanitize(event.escalation.context, 300)}`
        : "";
      return truncateBody(`**ESCALATION** \`${id}\` (${t}): ${q}${opts}${ctx2}`);
    }

    case "project_failed": {
      const phase = event.failedPhase
        ? ` at phase \`${sanitize(event.failedPhase, 40)}\``
        : "";
      const reason = truncateRationale(event.reason, 1024);
      return truncateBody(
        `Project \`${shortProjectId(event.projectId)}\` **FAILED**${phase}: ${sanitize(reason)}`,
      );
    }

    case "review_mandatory": {
      const id = shortTaskId(event.taskId);
      const projId = shortProjectId(event.projectId);
      const lines = [`${emoji} **Review Required** — ${formatTs(ts)}`, ""];
      lines.push(`Mandatory review firing for \`${id}\` in project \`${projId}\`.`);
      if (event.reviewSummary) {
        lines.push("", `- **Summary:** ${sanitize(event.reviewSummary, 400)}`);
      }
      if (event.reviewFindings && event.reviewFindings.length > 0) {
        lines.push("", "**Findings:**");
        for (const f of event.reviewFindings) {
          lines.push(`- ${formatFindingForOps(f)}`);
        }
      }
      return truncateBody(lines.join("\n"));
    }

    case "nudge_check": {
      // Wave E-δ §N4 — deterministic opener strings per status; observations
      // rendered as bullets when 2+, otherwise inline; closing defaults to
      // `nextAction` or "I'll check again at the next interval."
      // Section header emoji uses ROLE_EMOJI[identity] which already reflects
      // sourceAgent (resolveIdentity reads sourceAgent for nudge_check).
      const observations = (event.observations ?? []).slice(0, 5);
      let opener: string;
      switch (event.status) {
        case "stagnant": {
          // Inline duration when we have a single observation matching the
          // §N4 form "no events in {duration}"; otherwise fall back to bare
          // opener and let observations carry the detail.
          const inline = observations.length === 1 ? observations[0] : null;
          opener = inline
            ? `No progress on this in ${sanitize(inline.replace(/^no events in /, ""), 80)}.`
            : `No progress on this in some time.`;
          break;
        }
        case "progressing": {
          // Try to surface "last task {id} done {duration} ago" from the
          // observations (introspector emits it as obs[0] when present).
          const recent = observations.find((o) => o.startsWith("last task "));
          if (recent) {
            // recent = "last task {id} done {duration} ago"
            // Reformat as §N4 opener: "Things are moving — last task {id} completed {duration} ago."
            opener = `Things are moving — ${sanitize(recent.replace(/ done /, " completed "), 200)}.`;
          } else {
            opener = `Things are moving.`;
          }
          break;
        }
        case "blocked": {
          const detail = event.nextAction
            ? sanitize(event.nextAction, 200)
            : "awaiting input";
          opener = `Stuck — ${detail}.`;
          break;
        }
      }
      // For status=blocked, the opener already absorbed `nextAction` ("Stuck — {nextAction}.").
      // Re-using it as the closing line produces a near-duplicate paragraph; fall back to the
      // interval default in that case.
      const closing = event.nextAction && event.status !== "blocked"
        ? sanitize(event.nextAction, 200)
        : "I'll check again at the next interval.";
      const lines = [`${emoji} **Nudge Check** — ${formatTs(ts)}`, ""];
      lines.push(opener);
      // Observations: render as bullets when 2+; for stagnant/progressing the
      // single observation is already absorbed into the opener so we skip the
      // duplicate inline render. blocked uses observations only when no
      // nextAction is provided.
      const showAsBullets = observations.length >= 2;
      if (showAsBullets) {
        lines.push("", "**Observations:**");
        for (const obs of observations) {
          lines.push(`- ${sanitize(obs, 200)}`);
        }
      }
      lines.push("", closing);
      return truncateBody(lines.join("\n"));
    }

    default: {
      // Compact single-line fallback for non-epistle-eligible events.
      // This path is not called by NOTIFIER_MAP for these events (they keep
      // their own inline format lambdas), but renderEpistle must be total.
      return truncateBody(`[${event.type}]`);
    }
  }
}
