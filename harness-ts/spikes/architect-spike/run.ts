/**
 * Architect Spike Runner
 *
 * Purpose: validate the three-tier Architect hypothesis empirically before
 * committing to the 12-wave build plan. Spawns one fresh Architect SDK session
 * per escalation, feeds the escalation, captures the arbitration verdict
 * written to `.harness/arbitration.json`, records cost/latency/tokens.
 *
 * Kill criteria (pre-committed):
 *   - < 40% (2/5) resolve without operator → kill plan
 *   - < 40% directives graded "useful" → kill plan
 *   - > $0.50 avg per arbitration → cost escape
 *   - > 120s avg latency → too slow
 *
 * Proceed criteria:
 *   - ≥ 60% (3/5) resolve AND ≥ 60% useful directives.
 *
 * Run: `cd harness-ts && npx tsx spikes/architect-spike/run.ts`
 */

import { query } from "@anthropic-ai/claude-agent-sdk";
import type { SDKResultMessage, Options } from "@anthropic-ai/claude-agent-sdk";
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  existsSync,
  rmSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// --- Paths ---

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const PROMPT_PATH = join(__dirname, "prompt.md");
const ESCALATIONS_PATH = join(__dirname, "escalations.json");
const ESCALATIONS_PATH_PERSISTENT = join(__dirname, "escalations-persistent.json");
const SANDBOX_ROOT = join(__dirname, "sandbox");
const RESULTS_DIR = join(__dirname, "results");

// --- Config ---

const MODEL = "claude-sonnet-4-6";
const MAX_BUDGET_USD_PER_ESCALATION = 1.0;
const MAX_TURNS = 20;
const ENABLE_OMC = true;
const ENABLE_CAVEMAN = false;
const PERSISTENT_MODE = false;

const OMC_ADDENDUM = `

## OMC Subagents and Skills

You have access to OMC subagents (via the Task tool) and skills (via the Skill tool): architect, planner, code-reviewer, analyst, verifier, debugger, among others. Invoke them when specialist reasoning materially improves the verdict — not frivolously, since arbitration latency and cost matter. Typical useful cases:

- Complex design_decision with trade-offs → Task({subagent_type: "oh-my-claudecode:architect"}) for a soundness opinion
- Ambiguous scope or requirements → Task({subagent_type: "oh-my-claudecode:analyst"}) for requirements clarification
- Review arbitration where code quality is the dispute → Task({subagent_type: "oh-my-claudecode:code-reviewer"}) for independent review

Keep subagent calls scoped. Do not delegate the final verdict — you are the Architect and the verdict is yours. Subagents provide input; you decide.
`;

// --- Types ---

interface EscalationBase {
  id: string;
  shape: "executor_escalation" | "review_arbitration";
  projectContext: {
    name: string;
    description: string;
    nonGoals: string[];
  };
  phaseSpec: {
    phaseId: string;
    title: string;
    description: string;
    acceptanceCriteria: string[];
  };
}

interface ExecutorEscalation extends EscalationBase {
  shape: "executor_escalation";
  type: string;
  escalation: {
    type: string;
    question: string;
    context: string;
    options: string[];
    assessment: Record<string, unknown>;
  };
}

interface ReviewArbitration extends EscalationBase {
  shape: "review_arbitration";
  executorCompletion: Record<string, unknown>;
  diffSummary: string;
  reviewerVerdicts: Array<{
    round: number;
    verdict: string;
    feedback: string;
    severity: string;
  }>;
}

type Escalation = ExecutorEscalation | ReviewArbitration;

interface ArbitrationOutput {
  verdict: "retry_with_directive" | "plan_amendment" | "escalate_operator";
  rationale: string;
  directive: string | null;
  amendedSpec: string | null;
  escalationReason: string | null;
  nonGoalsPreserved: boolean;
  category: string;
}

interface RawResult {
  escalationId: string;
  shape: string;
  type: string;
  verdict: string;
  directive: string | null;
  amendedSpec: string | null;
  escalationReason: string | null;
  rationale: string | null;
  nonGoalsPreserved: boolean | null;
  category: string | null;
  fullArbitration: unknown;
  parseError: string | null;
  costUsd: number;
  latencyMs: number;
  numTurns: number;
  inputTokens: number;
  outputTokens: number;
  terminalReason: string | null;
  sessionSuccess: boolean;
  errors: string[];
  subagentInvocations?: Array<{ tool: string; subagent_type?: string; skill?: string }>;
  subagentInvocationCount?: number;
}

// --- Prompt Formatting ---

function formatExecutorEscalationPrompt(e: ExecutorEscalation): string {
  return [
    "# Tier-1 Escalation From Executor",
    "",
    "An Executor session working on a phase task in your declared project has written an escalation. Arbitrate per your systemPrompt. Write your verdict to `.harness/arbitration.json` in the current working directory.",
    "",
    "## Project Context",
    `**Name:** ${e.projectContext.name}`,
    `**Description:** ${e.projectContext.description}`,
    "**Non-Goals:**",
    ...e.projectContext.nonGoals.map((ng) => `- ${ng}`),
    "",
    "## Phase Specification",
    `**Phase ID:** ${e.phaseSpec.phaseId}`,
    `**Title:** ${e.phaseSpec.title}`,
    `**Description:** ${e.phaseSpec.description}`,
    "**Acceptance Criteria:**",
    ...e.phaseSpec.acceptanceCriteria.map((ac) => `- ${ac}`),
    "",
    "## Escalation Signal",
    "```json",
    JSON.stringify(e.escalation, null, 2),
    "```",
    "",
    "Produce your verdict now. Write `.harness/arbitration.json` and stop.",
  ].join("\n");
}

function formatReviewArbitrationPrompt(e: ReviewArbitration): string {
  return [
    "# Tier-1 Review Arbitration",
    "",
    "An Executor session completed a phase task in your declared project. The Reviewer has rejected the resulting merge 2 times with structured feedback. You are invoked to break the deadlock. Arbitrate per your systemPrompt. Write your verdict to `.harness/arbitration.json`.",
    "",
    "## Project Context",
    `**Name:** ${e.projectContext.name}`,
    `**Description:** ${e.projectContext.description}`,
    "**Non-Goals:**",
    ...e.projectContext.nonGoals.map((ng) => `- ${ng}`),
    "",
    "## Phase Specification",
    `**Phase ID:** ${e.phaseSpec.phaseId}`,
    `**Title:** ${e.phaseSpec.title}`,
    `**Description:** ${e.phaseSpec.description}`,
    "**Acceptance Criteria:**",
    ...e.phaseSpec.acceptanceCriteria.map((ac) => `- ${ac}`),
    "",
    "## Executor Completion Signal",
    "```json",
    JSON.stringify(e.executorCompletion, null, 2),
    "```",
    "",
    "## Diff Summary",
    e.diffSummary,
    "",
    "## Reviewer Rejection History",
    ...e.reviewerVerdicts.flatMap((rv) => [
      `### Round ${rv.round} — ${rv.verdict} (${rv.severity})`,
      rv.feedback,
      "",
    ]),
    "Produce your verdict now. Write `.harness/arbitration.json` and stop.",
  ].join("\n");
}

function formatPrompt(e: Escalation): string {
  return e.shape === "executor_escalation"
    ? formatExecutorEscalationPrompt(e)
    : formatReviewArbitrationPrompt(e);
}

// --- Validation ---

function parseArbitration(raw: unknown): ArbitrationOutput | { error: string } {
  if (!raw || typeof raw !== "object") {
    return { error: "Not an object" };
  }
  const obj = raw as Record<string, unknown>;
  const verdict = obj.verdict;
  if (
    verdict !== "retry_with_directive" &&
    verdict !== "plan_amendment" &&
    verdict !== "escalate_operator"
  ) {
    return { error: `Invalid verdict: ${String(verdict)}` };
  }
  if (typeof obj.rationale !== "string") {
    return { error: "Missing rationale" };
  }
  const directive = typeof obj.directive === "string" ? obj.directive : null;
  const amendedSpec = typeof obj.amendedSpec === "string" ? obj.amendedSpec : null;
  const escalationReason =
    typeof obj.escalationReason === "string" ? obj.escalationReason : null;

  // Enforce "exactly one non-null" rule
  const nonNullCount = [directive, amendedSpec, escalationReason].filter(
    (x) => x !== null,
  ).length;
  if (verdict === "retry_with_directive" && directive === null) {
    return { error: "retry_with_directive requires directive" };
  }
  if (verdict === "plan_amendment" && amendedSpec === null) {
    return { error: "plan_amendment requires amendedSpec" };
  }
  if (verdict === "escalate_operator" && escalationReason === null) {
    return { error: "escalate_operator requires escalationReason" };
  }
  if (nonNullCount > 1) {
    return { error: `Multiple resolution fields non-null (${nonNullCount})` };
  }

  return {
    verdict,
    rationale: obj.rationale,
    directive,
    amendedSpec,
    escalationReason,
    nonGoalsPreserved: obj.nonGoalsPreserved !== false,
    category: typeof obj.category === "string" ? obj.category : "unknown",
  };
}

// --- Per-escalation run ---

async function runOne(
  e: Escalation,
  systemPrompt: string,
): Promise<RawResult> {
  // Prepare sandbox
  const sandbox = join(SANDBOX_ROOT, e.id);
  if (existsSync(sandbox)) rmSync(sandbox, { recursive: true, force: true });
  mkdirSync(join(sandbox, ".harness"), { recursive: true });

  const userPrompt = formatPrompt(e);

  const ac = new AbortController();

  // Build plugin set based on variant flags
  const enabledPlugins: Record<string, boolean> = {};
  if (ENABLE_OMC) enabledPlugins["oh-my-claudecode@omc"] = true;
  if (ENABLE_CAVEMAN) enabledPlugins["caveman@caveman"] = true;

  // Build systemPrompt with optional addendums
  let fullSystemPrompt = systemPrompt;
  if (ENABLE_OMC) fullSystemPrompt += OMC_ADDENDUM;

  // Build allowedTools — expand when OMC loaded
  const allowedTools = ENABLE_OMC
    ? ["Read", "Write", "Grep", "Glob", "Task", "Skill"]
    : ["Read", "Write"];

  const options: Options = {
    cwd: sandbox,
    abortController: ac,
    model: MODEL,
    permissionMode: "bypassPermissions",
    allowDangerouslySkipPermissions: true,
    settingSources: ["project"],
    ...(Object.keys(enabledPlugins).length > 0
      ? { settings: { enabledPlugins } as unknown as Options["settings"] }
      : {}),
    systemPrompt: {
      type: "preset",
      preset: "claude_code",
      append: fullSystemPrompt,
    },
    maxBudgetUsd: MAX_BUDGET_USD_PER_ESCALATION,
    maxTurns: MAX_TURNS,
    allowedTools,
    disallowedTools: [
      "Bash",
      "Edit",
      "WebFetch",
      "WebSearch",
      "CronCreate",
      "CronDelete",
      "CronList",
      "RemoteTrigger",
      "ScheduleWakeup",
      "TaskCreate",
    ],
    persistSession: false,
  };

  const start = Date.now();
  let sessionResult: SDKResultMessage | null = null;
  const errors: string[] = [];
  const invocations: Array<{ tool: string; subagent_type?: string; skill?: string }> = [];

  try {
    const q = query({ prompt: userPrompt, options });
    for await (const msg of q) {
      if (msg.type === "result") {
        sessionResult = msg as SDKResultMessage;
      }
      if (msg.type === "assistant") {
        const m = msg as unknown as { message?: { content?: unknown[] } };
        const content = m.message?.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (!block || typeof block !== "object") continue;
            const b = block as Record<string, unknown>;
            if (b.type !== "tool_use") continue;
            const name = b.name as string | undefined;
            if (name === "Task" || name === "Agent") {
              const input = b.input as Record<string, unknown> | undefined;
              invocations.push({ tool: name, subagent_type: input?.subagent_type as string | undefined });
            } else if (name === "Skill") {
              const input = b.input as Record<string, unknown> | undefined;
              invocations.push({ tool: "Skill", skill: input?.skill as string | undefined });
            }
          }
        }
      }
    }
  } catch (err) {
    errors.push(String((err as Error).message ?? err));
  }
  const latencyMs = Date.now() - start;

  // Read arbitration.json
  const arbPath = join(sandbox, ".harness", "arbitration.json");
  let parsedArb: ArbitrationOutput | null = null;
  let parseError: string | null = null;
  let fullArbitration: unknown = null;

  if (existsSync(arbPath)) {
    try {
      fullArbitration = JSON.parse(readFileSync(arbPath, "utf-8"));
      const parsed = parseArbitration(fullArbitration);
      if ("error" in parsed) {
        parseError = parsed.error;
      } else {
        parsedArb = parsed;
      }
    } catch (err) {
      parseError = `JSON parse: ${(err as Error).message}`;
    }
  } else {
    parseError = "arbitration.json not written";
  }

  // Extract SDK result fields
  let costUsd = 0;
  let numTurns = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let terminalReason: string | null = null;
  let sessionSuccess = false;
  if (sessionResult) {
    // SDKResultMessage has these fields via SDKResultSuccess | SDKResultError
    const r = sessionResult as unknown as {
      subtype: string;
      total_cost_usd: number;
      num_turns: number;
      usage: { input_tokens: number; output_tokens: number };
      terminal_reason?: string;
      errors?: string[];
    };
    costUsd = r.total_cost_usd ?? 0;
    numTurns = r.num_turns ?? 0;
    inputTokens = r.usage?.input_tokens ?? 0;
    outputTokens = r.usage?.output_tokens ?? 0;
    terminalReason = r.terminal_reason ?? null;
    sessionSuccess = r.subtype === "success";
    if (r.errors) errors.push(...r.errors);
  }

  return {
    escalationId: e.id,
    shape: e.shape,
    type: e.shape === "executor_escalation" ? e.type : "review_arbitration",
    verdict: parsedArb?.verdict ?? (parseError ? "PARSE_ERROR" : "NO_OUTPUT"),
    directive: parsedArb?.directive ?? null,
    amendedSpec: parsedArb?.amendedSpec ?? null,
    escalationReason: parsedArb?.escalationReason ?? null,
    rationale: parsedArb?.rationale ?? null,
    nonGoalsPreserved: parsedArb?.nonGoalsPreserved ?? null,
    category: parsedArb?.category ?? null,
    fullArbitration,
    parseError,
    costUsd,
    latencyMs,
    numTurns,
    inputTokens,
    outputTokens,
    terminalReason,
    sessionSuccess,
    errors,
    subagentInvocations: invocations,
    subagentInvocationCount: invocations.length,
  };
}

// --- Aggregation ---

interface Summary {
  runAt: string;
  model: string;
  totalEscalations: number;
  resolvedCount: number;
  escalatedCount: number;
  errorCount: number;
  resolutionRate: number;
  totalCostUsd: number;
  avgCostUsd: number;
  avgLatencyMs: number;
  p99LatencyMs: number;
  killCriteriaHit: string[];
  proceedCriteriaMet: string[];
  perEscalation: Array<{
    id: string;
    shape: string;
    verdict: string;
    category: string | null;
    costUsd: number;
    latencyMs: number;
    terminalReason: string | null;
  }>;
}

function summarize(results: RawResult[]): Summary {
  const resolved = results.filter(
    (r) =>
      r.verdict === "retry_with_directive" || r.verdict === "plan_amendment",
  );
  const escalated = results.filter((r) => r.verdict === "escalate_operator");
  const errored = results.filter(
    (r) => r.verdict === "NO_OUTPUT" || r.verdict === "PARSE_ERROR",
  );
  const total = results.length;
  const totalCost = results.reduce((s, r) => s + r.costUsd, 0);
  const avgCost = total > 0 ? totalCost / total : 0;
  const avgLatency =
    total > 0 ? results.reduce((s, r) => s + r.latencyMs, 0) / total : 0;
  const sortedLat = [...results.map((r) => r.latencyMs)].sort((a, b) => a - b);
  const p99Idx = Math.max(0, Math.floor(sortedLat.length * 0.99) - 1);
  const p99Latency = sortedLat[p99Idx] ?? 0;
  const resolutionRate = total > 0 ? resolved.length / total : 0;

  const killCriteriaHit: string[] = [];
  const proceedCriteriaMet: string[] = [];

  if (resolutionRate < 0.4)
    killCriteriaHit.push(
      `resolution_rate_below_40pct (${(resolutionRate * 100).toFixed(0)}%)`,
    );
  if (avgCost > 0.5)
    killCriteriaHit.push(`avg_cost_above_threshold ($${avgCost.toFixed(3)})`);
  if (avgLatency > 120_000)
    killCriteriaHit.push(
      `avg_latency_above_threshold (${(avgLatency / 1000).toFixed(1)}s)`,
    );

  if (resolutionRate >= 0.6)
    proceedCriteriaMet.push(
      `resolution_rate_at_least_60pct (${(resolutionRate * 100).toFixed(0)}%)`,
    );
  if (avgCost <= 0.3) proceedCriteriaMet.push(`avg_cost_within_budget`);
  if (avgLatency <= 60_000) proceedCriteriaMet.push(`avg_latency_within_budget`);

  return {
    runAt: new Date().toISOString(),
    model: MODEL,
    totalEscalations: total,
    resolvedCount: resolved.length,
    escalatedCount: escalated.length,
    errorCount: errored.length,
    resolutionRate,
    totalCostUsd: totalCost,
    avgCostUsd: avgCost,
    avgLatencyMs: avgLatency,
    p99LatencyMs: p99Latency,
    killCriteriaHit,
    proceedCriteriaMet,
    perEscalation: results.map((r) => ({
      id: r.escalationId,
      shape: r.shape,
      verdict: r.verdict,
      category: r.category,
      costUsd: r.costUsd,
      latencyMs: r.latencyMs,
      terminalReason: r.terminalReason,
    })),
  };
}

// --- Persistent Mode ---

interface PersistentPhase {
  id: string;
  shape: "executor_escalation" | "review_arbitration";
  type?: string;
  phaseSpec: EscalationBase["phaseSpec"];
  escalation?: ExecutorEscalation["escalation"];
  executorCompletion?: Record<string, unknown>;
  diffSummary?: string;
  reviewerVerdicts?: Array<{
    round: number;
    verdict: string;
    feedback: string;
    severity: string;
  }>;
}

interface PersistentSpikeFile {
  project: EscalationBase["projectContext"];
  phases: PersistentPhase[];
}

function formatFirstCallPrompt(
  project: EscalationBase["projectContext"],
  phase: PersistentPhase,
): string {
  const header = [
    "# Persistent Architect Session — Project Kickoff",
    "",
    "You are arbitrating ALL tier-1 escalations for a single declared project. This is the first arbitration call. Subsequent calls will resume this session with only the next phase's escalation — the project context you receive now must be retained across calls.",
    "",
    "## Project Context (retain across all subsequent calls)",
    `**Name:** ${project.name}`,
    `**Description:** ${project.description}`,
    "**Non-Goals:**",
    ...project.nonGoals.map((ng) => `- ${ng}`),
    "",
    `## Current Phase: ${phase.id}`,
    `**Phase ID:** ${phase.phaseSpec.phaseId}`,
    `**Title:** ${phase.phaseSpec.title}`,
    `**Description:** ${phase.phaseSpec.description}`,
    "**Acceptance Criteria:**",
    ...phase.phaseSpec.acceptanceCriteria.map((ac) => `- ${ac}`),
    "",
  ];
  if (phase.shape === "executor_escalation" && phase.escalation) {
    header.push("## Escalation Signal");
    header.push("```json");
    header.push(JSON.stringify(phase.escalation, null, 2));
    header.push("```");
  } else if (phase.shape === "review_arbitration") {
    header.push("## Executor Completion Signal");
    header.push("```json");
    header.push(JSON.stringify(phase.executorCompletion, null, 2));
    header.push("```");
    header.push("");
    header.push("## Diff Summary");
    header.push(phase.diffSummary ?? "");
    header.push("");
    header.push("## Reviewer Rejection History");
    for (const rv of phase.reviewerVerdicts ?? []) {
      header.push(`### Round ${rv.round} — ${rv.verdict} (${rv.severity})`);
      header.push(rv.feedback);
      header.push("");
    }
  }
  header.push("");
  header.push(
    `Produce your verdict for ${phase.id}. Write it to \`.harness/arbitration-${phase.id}.json\`. Stop after writing the file. Retain project context in memory for the next phase.`,
  );
  return header.join("\n");
}

function formatSubsequentCallPrompt(phase: PersistentPhase): string {
  const lines = [
    "# Next Phase Arbitration",
    "",
    "You remain in the same project from prior calls. Retain all project context (name, description, non-goals) and prior arbitration decisions in memory. Arbitrate this new phase.",
    "",
    `## Phase: ${phase.id}`,
    `**Phase ID:** ${phase.phaseSpec.phaseId}`,
    `**Title:** ${phase.phaseSpec.title}`,
    `**Description:** ${phase.phaseSpec.description}`,
    "**Acceptance Criteria:**",
    ...phase.phaseSpec.acceptanceCriteria.map((ac) => `- ${ac}`),
    "",
  ];
  if (phase.shape === "executor_escalation" && phase.escalation) {
    lines.push("## Escalation Signal");
    lines.push("```json");
    lines.push(JSON.stringify(phase.escalation, null, 2));
    lines.push("```");
  } else if (phase.shape === "review_arbitration") {
    lines.push("## Executor Completion Signal");
    lines.push("```json");
    lines.push(JSON.stringify(phase.executorCompletion, null, 2));
    lines.push("```");
    lines.push("");
    lines.push("## Diff Summary");
    lines.push(phase.diffSummary ?? "");
    lines.push("");
    lines.push("## Reviewer Rejection History");
    for (const rv of phase.reviewerVerdicts ?? []) {
      lines.push(`### Round ${rv.round} — ${rv.verdict} (${rv.severity})`);
      lines.push(rv.feedback);
      lines.push("");
    }
  }
  lines.push("");
  lines.push(
    `Produce your verdict for ${phase.id}. Write it to \`.harness/arbitration-${phase.id}.json\`. Stop after writing the file.`,
  );
  return lines.join("\n");
}

interface PersistentCallResult extends RawResult {
  callIndex: number;
  cumulativeCostUsd: number;
  cumulativeTurns: number;
}

async function runPersistent(
  systemPrompt: string,
): Promise<PersistentCallResult[]> {
  if (!existsSync(ESCALATIONS_PATH_PERSISTENT)) {
    throw new Error(`Persistent escalations not found: ${ESCALATIONS_PATH_PERSISTENT}`);
  }

  const spikeFile: PersistentSpikeFile = JSON.parse(
    readFileSync(ESCALATIONS_PATH_PERSISTENT, "utf-8"),
  );

  // Shared sandbox across all calls
  const sandbox = join(SANDBOX_ROOT, "persistent");
  if (existsSync(sandbox)) rmSync(sandbox, { recursive: true, force: true });
  mkdirSync(join(sandbox, ".harness"), { recursive: true });

  const enabledPlugins: Record<string, boolean> = {};
  if (ENABLE_OMC) enabledPlugins["oh-my-claudecode@omc"] = true;
  if (ENABLE_CAVEMAN) enabledPlugins["caveman@caveman"] = true;

  let fullSystemPrompt = systemPrompt;
  if (ENABLE_OMC) fullSystemPrompt += OMC_ADDENDUM;

  const allowedTools = ENABLE_OMC
    ? ["Read", "Write", "Grep", "Glob", "Task", "Skill"]
    : ["Read", "Write"];

  let sessionId: string | null = null;
  let cumulativeCostUsd = 0;
  let cumulativeTurns = 0;
  const results: PersistentCallResult[] = [];

  for (let i = 0; i < spikeFile.phases.length; i++) {
    const phase = spikeFile.phases[i]!;
    const isFirst = i === 0;
    const userPrompt = isFirst
      ? formatFirstCallPrompt(spikeFile.project, phase)
      : formatSubsequentCallPrompt(phase);

    const ac = new AbortController();
    const baseOptions: Options = {
      cwd: sandbox,
      abortController: ac,
      model: MODEL,
      permissionMode: "bypassPermissions",
      allowDangerouslySkipPermissions: true,
      settingSources: ["project"],
      ...(Object.keys(enabledPlugins).length > 0
        ? { settings: { enabledPlugins } as unknown as Options["settings"] }
        : {}),
      systemPrompt: {
        type: "preset",
        preset: "claude_code",
        append: fullSystemPrompt,
      },
      maxBudgetUsd: MAX_BUDGET_USD_PER_ESCALATION,
      maxTurns: MAX_TURNS,
      allowedTools,
      disallowedTools: [
        "Bash",
        "Edit",
        "WebFetch",
        "WebSearch",
        "CronCreate",
        "CronDelete",
        "CronList",
        "RemoteTrigger",
        "ScheduleWakeup",
        "TaskCreate",
      ],
      persistSession: true,
    };

    const options: Options =
      !isFirst && sessionId ? { ...baseOptions, resume: sessionId } : baseOptions;

    const start = Date.now();
    let sessionResult: SDKResultMessage | null = null;
    const errors: string[] = [];
    try {
      const q = query({ prompt: userPrompt, options });
      for await (const msg of q) {
        if (msg.type === "result") {
          sessionResult = msg as SDKResultMessage;
        }
        if (
          sessionId === null &&
          "session_id" in msg &&
          typeof msg.session_id === "string"
        ) {
          sessionId = msg.session_id;
        }
      }
    } catch (err) {
      errors.push(String((err as Error).message ?? err));
    }
    const latencyMs = Date.now() - start;

    // Read phase-scoped arbitration file
    const arbPath = join(sandbox, ".harness", `arbitration-${phase.id}.json`);
    let parsedArb: ArbitrationOutput | null = null;
    let parseError: string | null = null;
    let fullArbitration: unknown = null;
    if (existsSync(arbPath)) {
      try {
        fullArbitration = JSON.parse(readFileSync(arbPath, "utf-8"));
        const parsed = parseArbitration(fullArbitration);
        if ("error" in parsed) parseError = parsed.error;
        else parsedArb = parsed;
      } catch (err) {
        parseError = `JSON parse: ${(err as Error).message}`;
      }
    } else {
      parseError = `arbitration-${phase.id}.json not written`;
    }

    let costUsd = 0;
    let numTurns = 0;
    let inputTokens = 0;
    let outputTokens = 0;
    let terminalReason: string | null = null;
    let sessionSuccess = false;
    if (sessionResult) {
      const r = sessionResult as unknown as {
        subtype: string;
        total_cost_usd: number;
        num_turns: number;
        usage: { input_tokens: number; output_tokens: number };
        terminal_reason?: string;
        errors?: string[];
      };
      costUsd = r.total_cost_usd ?? 0;
      numTurns = r.num_turns ?? 0;
      inputTokens = r.usage?.input_tokens ?? 0;
      outputTokens = r.usage?.output_tokens ?? 0;
      terminalReason = r.terminal_reason ?? null;
      sessionSuccess = r.subtype === "success";
      if (r.errors) errors.push(...r.errors);
    }

    cumulativeCostUsd += costUsd;
    cumulativeTurns += numTurns;

    const result: PersistentCallResult = {
      escalationId: phase.id,
      shape: phase.shape,
      type: phase.type ?? phase.shape,
      verdict: parsedArb?.verdict ?? (parseError ? "PARSE_ERROR" : "NO_OUTPUT"),
      directive: parsedArb?.directive ?? null,
      amendedSpec: parsedArb?.amendedSpec ?? null,
      escalationReason: parsedArb?.escalationReason ?? null,
      rationale: parsedArb?.rationale ?? null,
      nonGoalsPreserved: parsedArb?.nonGoalsPreserved ?? null,
      category: parsedArb?.category ?? null,
      fullArbitration,
      parseError,
      costUsd,
      latencyMs,
      numTurns,
      inputTokens,
      outputTokens,
      terminalReason,
      sessionSuccess,
      errors,
      callIndex: i + 1,
      cumulativeCostUsd,
      cumulativeTurns,
    };
    results.push(result);
    writeFileSync(
      join(RESULTS_DIR, `raw-${phase.id}.json`),
      JSON.stringify(result, null, 2),
    );

    const latencyStr = `${(latencyMs / 1000).toFixed(1)}s`;
    const costStr = `$${costUsd.toFixed(3)}`;
    const cumStr = `$${cumulativeCostUsd.toFixed(3)}`;
    console.log(
      `  [${i + 1}/${spikeFile.phases.length}] ${phase.id} ${phase.shape}: verdict=${result.verdict} cost=${costStr} cum=${cumStr} latency=${latencyStr} turns=${numTurns} input=${inputTokens} output=${outputTokens}`,
    );
    if (parseError) console.log(`    parseError: ${parseError}`);
    if (errors.length > 0) console.log(`    errors: ${errors.slice(0, 2).join("; ")}`);
  }

  return results;
}

// --- Main ---

async function main() {
  if (!existsSync(PROMPT_PATH)) {
    throw new Error(`Prompt not found: ${PROMPT_PATH}`);
  }

  mkdirSync(RESULTS_DIR, { recursive: true });
  const systemPrompt = readFileSync(PROMPT_PATH, "utf-8");

  const plugins: string[] = [];
  if (ENABLE_OMC) plugins.push("omc");
  if (ENABLE_CAVEMAN) plugins.push("caveman");
  const variant = plugins.length > 0 ? `${MODEL}+${plugins.join("+")}` : `${MODEL}-bare`;
  const mode = PERSISTENT_MODE ? "persistent" : "ephemeral";

  console.log(`Architect spike — mode: ${mode}`);
  console.log(`Model: ${MODEL}`);
  console.log(`Plugins: ${plugins.length > 0 ? plugins.join(", ") : "none"}`);
  console.log(`Variant: ${variant}-${mode}`);
  console.log(
    `Per-call budget: $${MAX_BUDGET_USD_PER_ESCALATION.toFixed(2)}`,
  );
  console.log("");

  if (PERSISTENT_MODE) {
    const persistentResults = await runPersistent(systemPrompt);

    const total = persistentResults.length;
    const resolved = persistentResults.filter(
      (r) => r.verdict === "retry_with_directive" || r.verdict === "plan_amendment",
    ).length;
    const escalated = persistentResults.filter((r) => r.verdict === "escalate_operator").length;
    const errored = persistentResults.filter(
      (r) => r.verdict === "NO_OUTPUT" || r.verdict === "PARSE_ERROR",
    ).length;
    const totalCost = persistentResults[total - 1]?.cumulativeCostUsd ?? 0;
    const avgLatency =
      persistentResults.reduce((s, r) => s + r.latencyMs, 0) / (total || 1);

    // Cost + token growth curve
    const growth = persistentResults.map((r) => ({
      call: r.callIndex,
      phaseId: r.escalationId,
      inputTokens: r.inputTokens,
      outputTokens: r.outputTokens,
      costUsd: r.costUsd,
      cumulativeCostUsd: r.cumulativeCostUsd,
      latencyMs: r.latencyMs,
      verdict: r.verdict,
    }));

    const summary = {
      runAt: new Date().toISOString(),
      model: MODEL,
      mode: "persistent",
      plugins,
      totalCalls: total,
      resolvedCount: resolved,
      escalatedCount: escalated,
      errorCount: errored,
      resolutionRate: total > 0 ? resolved / total : 0,
      totalCostUsd: totalCost,
      avgCostUsd: total > 0 ? totalCost / total : 0,
      avgLatencyMs: avgLatency,
      growth,
    };
    writeFileSync(
      join(RESULTS_DIR, "summary.json"),
      JSON.stringify(summary, null, 2),
    );
    console.log("");
    console.log("=== PERSISTENT-MODE SUMMARY ===");
    console.log(`Calls: ${total}`);
    console.log(`Resolved: ${resolved}/${total} (${((resolved / total) * 100).toFixed(0)}%)`);
    console.log(`Escalated: ${escalated}`);
    console.log(`Errored: ${errored}`);
    console.log(`Total cost: $${totalCost.toFixed(3)}`);
    console.log(`Avg cost: $${(totalCost / total).toFixed(3)}`);
    console.log(`Avg latency: ${(avgLatency / 1000).toFixed(1)}s`);
    console.log("");
    console.log("Growth curve (call, inputTokens, outputTokens, costUsd, cumulative):");
    for (const g of growth) {
      console.log(
        `  #${g.call} ${g.phaseId}: in=${g.inputTokens} out=${g.outputTokens} $${g.costUsd.toFixed(3)} cum=$${g.cumulativeCostUsd.toFixed(3)}`,
      );
    }
    return;
  }

  if (!existsSync(ESCALATIONS_PATH)) {
    throw new Error(`Escalations not found: ${ESCALATIONS_PATH}`);
  }

  const escalations: Escalation[] = JSON.parse(
    readFileSync(ESCALATIONS_PATH, "utf-8"),
  );

  console.log(`Running Architect spike: ${escalations.length} escalations`);
  console.log(`Model: ${MODEL}`);
  console.log(`Plugins: ${plugins.length > 0 ? plugins.join(", ") : "none"}`);
  console.log(`Variant: ${variant}`);
  console.log(
    `Per-escalation budget: $${MAX_BUDGET_USD_PER_ESCALATION.toFixed(2)}`,
  );
  console.log("");

  const results: RawResult[] = [];
  for (const e of escalations) {
    console.log(`>>> [${e.id}] ${e.shape} — starting...`);
    const result = await runOne(e, systemPrompt);
    results.push(result);
    writeFileSync(
      join(RESULTS_DIR, `raw-${e.id}.json`),
      JSON.stringify(result, null, 2),
    );
    const latencyStr = `${(result.latencyMs / 1000).toFixed(1)}s`;
    const costStr = `$${result.costUsd.toFixed(3)}`;
    const subagents = result.subagentInvocationCount ?? 0;
    console.log(
      `    verdict=${result.verdict} cost=${costStr} latency=${latencyStr} turns=${result.numTurns} subagents=${subagents}`,
    );
    if (subagents > 0 && result.subagentInvocations) {
      console.log(`    invocations: ${result.subagentInvocations.map((i) => i.subagent_type ?? i.skill ?? i.tool).join(", ")}`);
    }
    if (result.parseError) console.log(`    parseError: ${result.parseError}`);
    if (result.errors.length > 0) console.log(`    errors: ${result.errors.join("; ")}`);
    console.log("");
  }

  const summary = summarize(results);
  writeFileSync(
    join(RESULTS_DIR, "summary.json"),
    JSON.stringify(summary, null, 2),
  );

  console.log("=== SUMMARY ===");
  console.log(
    `Resolved (retry_with_directive | plan_amendment): ${summary.resolvedCount}/${summary.totalEscalations}`,
  );
  console.log(`Escalated to operator: ${summary.escalatedCount}`);
  console.log(`Errored (no output / parse error): ${summary.errorCount}`);
  console.log(`Resolution rate: ${(summary.resolutionRate * 100).toFixed(0)}%`);
  console.log(`Total cost: $${summary.totalCostUsd.toFixed(3)}`);
  console.log(`Avg cost: $${summary.avgCostUsd.toFixed(3)}`);
  console.log(`Avg latency: ${(summary.avgLatencyMs / 1000).toFixed(1)}s`);
  console.log("");
  if (summary.killCriteriaHit.length > 0) {
    console.log("KILL CRITERIA HIT:");
    for (const k of summary.killCriteriaHit) console.log(`  - ${k}`);
  }
  if (summary.proceedCriteriaMet.length > 0) {
    console.log("PROCEED CRITERIA MET:");
    for (const p of summary.proceedCriteriaMet) console.log(`  - ${p}`);
  }
  console.log("");
  console.log(`Results: ${RESULTS_DIR}/summary.json`);
  console.log("Next: manual + second-pass grading per RESULTS.md.");
}

main().catch((err) => {
  console.error("Spike runner failed:", err);
  process.exit(1);
});
