/**
 * Reviewer Spike Runner
 *
 * Purpose: validate Reviewer tier empirically before Wave A lands. Runs 5 scenarios
 * through fresh Reviewer SDK sessions across 4 variants (bare/OMC/caveman/both).
 *
 * Unlike architect-spike, Reviewer is ephemeral per review by design — each review
 * is a fresh session with no memory of prior reviews.
 *
 * Run: `npx tsx spikes/reviewer-spike/run.ts` from harness-ts/ root.
 * Toggle variants via ENABLE_OMC / ENABLE_CAVEMAN constants below.
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
const SCENARIOS_PATH = join(__dirname, "scenarios.json");
const SANDBOX_ROOT = join(__dirname, "sandbox");
const RESULTS_DIR = join(__dirname, "results");

// --- Config (edit per variant) ---

const MODEL = "claude-sonnet-4-6";
const MAX_BUDGET_USD_PER_REVIEW = 1.0;
const MAX_TURNS = 20;
const ENABLE_OMC = true;
const ENABLE_CAVEMAN = false;

const OMC_ADDENDUM = `

## OMC Subagents and Skills

You have access to OMC subagents (via the Task tool) and skills (via the Skill tool): code-reviewer, security-reviewer, test-engineer, verifier, debugger, analyst, among others. Invoke them when specialist reasoning materially improves the verdict — not frivolously, since review latency and cost matter. Typical useful cases:

- Suspected security vulnerability (injection, auth flaw, secret exposure) → Task({subagent_type: "oh-my-claudecode:security-reviewer"}) for specialist check
- Test coverage or test-quality concerns → Task({subagent_type: "oh-my-claudecode:test-engineer"}) for adequacy assessment
- Complex correctness logic or subtle bugs → Task({subagent_type: "oh-my-claudecode:code-reviewer"}) for contrarian second opinion

Keep subagent calls scoped. Do not delegate the final verdict — you are the Reviewer and the verdict is yours. Subagents provide input; you decide the risk scores and final verdict.
`;

// --- Types ---

interface Scenario {
  id: string;
  label: string;
  expectedVerdict: string;
  taskPrompt: string;
  acceptanceCriteria: string[];
  commitMessage: string;
  diffSummary: string;
  diffBody: string;
  executorCompletion: Record<string, unknown>;
  plantedDefect?: string;
}

interface ReviewFinding {
  severity: "critical" | "warning" | "note";
  file: string;
  line?: number | null;
  description: string;
  suggestion?: string | null;
}

interface ReviewOutput {
  verdict: "approve" | "reject" | "request_changes";
  riskScore: {
    correctness: number;
    integration: number;
    stateCorruption: number;
    performance: number;
    regression: number;
    weighted: number;
  };
  findings: ReviewFinding[];
  summary: string;
  criteriaAssessment: Array<{
    criterion: string;
    met: boolean;
    evidence: string;
  }>;
  category: string;
}

interface RawResult {
  scenarioId: string;
  label: string;
  expectedVerdict: string;
  verdict: string;
  weightedRisk: number | null;
  findingCount: number;
  criticalFindingCount: number;
  criteriaMetCount: number;
  criteriaTotal: number;
  category: string | null;
  summary: string | null;
  fullReview: unknown;
  parseError: string | null;
  costUsd: number;
  latencyMs: number;
  numTurns: number;
  inputTokens: number;
  outputTokens: number;
  terminalReason: string | null;
  sessionSuccess: boolean;
  errors: string[];
  subagentInvocations: Array<{ tool: string; subagent_type?: string; skill?: string }>;
  subagentInvocationCount: number;
}

// --- Prompt formatting ---

function formatScenarioPrompt(s: Scenario): string {
  return [
    "# Review Input",
    "",
    "Review the diff below against the acceptance criteria and completion signal. Produce a structured verdict per your systemPrompt. Write to `.harness/review.json` in the current working directory.",
    "",
    "## Task Prompt (what was requested)",
    s.taskPrompt,
    "",
    "## Acceptance Criteria",
    ...s.acceptanceCriteria.map((c, i) => `${i + 1}. ${c}`),
    "",
    "## Commit Message",
    "```",
    s.commitMessage,
    "```",
    "",
    "## Diff Summary",
    s.diffSummary,
    "",
    "## Diff",
    "```diff",
    s.diffBody,
    "```",
    "",
    "## Executor Completion Signal",
    "```json",
    JSON.stringify(s.executorCompletion, null, 2),
    "```",
    "",
    "Produce your verdict. Write `.harness/review.json` and stop.",
  ].join("\n");
}

// --- Validation ---

function parseReview(raw: unknown): ReviewOutput | { error: string } {
  if (!raw || typeof raw !== "object") return { error: "Not an object" };
  const obj = raw as Record<string, unknown>;

  const verdict = obj.verdict;
  if (
    verdict !== "approve" &&
    verdict !== "reject" &&
    verdict !== "request_changes"
  ) {
    return { error: `Invalid verdict: ${String(verdict)}` };
  }

  const rs = obj.riskScore as Record<string, unknown> | undefined;
  if (!rs || typeof rs !== "object") return { error: "Missing riskScore" };

  const dims = ["correctness", "integration", "stateCorruption", "performance", "regression", "weighted"];
  for (const d of dims) {
    if (typeof rs[d] !== "number") return { error: `Missing or non-numeric riskScore.${d}` };
  }

  const findings = Array.isArray(obj.findings) ? (obj.findings as ReviewFinding[]) : [];
  const criteriaAssessment = Array.isArray(obj.criteriaAssessment)
    ? (obj.criteriaAssessment as ReviewOutput["criteriaAssessment"])
    : [];

  return {
    verdict,
    riskScore: rs as ReviewOutput["riskScore"],
    findings,
    summary: typeof obj.summary === "string" ? obj.summary : "",
    criteriaAssessment,
    category: typeof obj.category === "string" ? obj.category : "unknown",
  };
}

// --- Per-scenario run ---

async function runOne(s: Scenario, systemPrompt: string): Promise<RawResult> {
  const sandbox = join(SANDBOX_ROOT, s.id);
  if (existsSync(sandbox)) rmSync(sandbox, { recursive: true, force: true });
  mkdirSync(join(sandbox, ".harness"), { recursive: true });

  const userPrompt = formatScenarioPrompt(s);

  const enabledPlugins: Record<string, boolean> = {};
  if (ENABLE_OMC) enabledPlugins["oh-my-claudecode@omc"] = true;
  if (ENABLE_CAVEMAN) enabledPlugins["caveman@caveman"] = true;

  let fullSystemPrompt = systemPrompt;
  if (ENABLE_OMC) fullSystemPrompt += OMC_ADDENDUM;

  const allowedTools = ENABLE_OMC
    ? ["Read", "Write", "Grep", "Glob", "Task", "Skill"]
    : ["Read", "Write", "Grep"];

  const ac = new AbortController();
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
    maxBudgetUsd: MAX_BUDGET_USD_PER_REVIEW,
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
      if (msg.type === "result") sessionResult = msg as SDKResultMessage;
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

  const reviewPath = join(sandbox, ".harness", "review.json");
  let parsedReview: ReviewOutput | null = null;
  let parseError: string | null = null;
  let fullReview: unknown = null;

  if (existsSync(reviewPath)) {
    try {
      fullReview = JSON.parse(readFileSync(reviewPath, "utf-8"));
      const parsed = parseReview(fullReview);
      if ("error" in parsed) parseError = parsed.error;
      else parsedReview = parsed;
    } catch (err) {
      parseError = `JSON parse: ${(err as Error).message}`;
    }
  } else {
    parseError = "review.json not written";
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

  const findingCount = parsedReview?.findings.length ?? 0;
  const criticalFindingCount =
    parsedReview?.findings.filter((f) => f.severity === "critical").length ?? 0;
  const criteriaAssessment = parsedReview?.criteriaAssessment ?? [];
  const criteriaMetCount = criteriaAssessment.filter((c) => c.met).length;

  return {
    scenarioId: s.id,
    label: s.label,
    expectedVerdict: s.expectedVerdict,
    verdict: parsedReview?.verdict ?? (parseError ? "PARSE_ERROR" : "NO_OUTPUT"),
    weightedRisk: parsedReview?.riskScore.weighted ?? null,
    findingCount,
    criticalFindingCount,
    criteriaMetCount,
    criteriaTotal: s.acceptanceCriteria.length,
    category: parsedReview?.category ?? null,
    summary: parsedReview?.summary ?? null,
    fullReview,
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

// --- Main ---

async function main() {
  if (!existsSync(PROMPT_PATH)) throw new Error(`Prompt not found: ${PROMPT_PATH}`);
  if (!existsSync(SCENARIOS_PATH)) throw new Error(`Scenarios not found: ${SCENARIOS_PATH}`);

  mkdirSync(RESULTS_DIR, { recursive: true });

  const systemPrompt = readFileSync(PROMPT_PATH, "utf-8");
  const scenarios: Scenario[] = JSON.parse(readFileSync(SCENARIOS_PATH, "utf-8"));

  const plugins: string[] = [];
  if (ENABLE_OMC) plugins.push("omc");
  if (ENABLE_CAVEMAN) plugins.push("caveman");
  const variant = plugins.length > 0 ? `${MODEL}+${plugins.join("+")}` : `${MODEL}-bare`;

  console.log(`Reviewer spike — ${scenarios.length} scenarios`);
  console.log(`Model: ${MODEL}`);
  console.log(`Plugins: ${plugins.length > 0 ? plugins.join(", ") : "none"}`);
  console.log(`Variant: ${variant}`);
  console.log(`Per-review budget: $${MAX_BUDGET_USD_PER_REVIEW.toFixed(2)}`);
  console.log("");

  const results: RawResult[] = [];
  for (const s of scenarios) {
    console.log(`>>> [${s.id}] ${s.label} — starting...`);
    const r = await runOne(s, systemPrompt);
    results.push(r);
    writeFileSync(join(RESULTS_DIR, `raw-${s.id}.json`), JSON.stringify(r, null, 2));
    const latencyStr = `${(r.latencyMs / 1000).toFixed(1)}s`;
    const costStr = `$${r.costUsd.toFixed(3)}`;
    const riskStr = r.weightedRisk !== null ? `risk=${r.weightedRisk.toFixed(2)}` : "risk=?";
    console.log(
      `    verdict=${r.verdict} ${riskStr} findings=${r.findingCount} (${r.criticalFindingCount} critical) criteria=${r.criteriaMetCount}/${r.criteriaTotal} subagents=${r.subagentInvocationCount} cost=${costStr} latency=${latencyStr} turns=${r.numTurns}`,
    );
    if (r.subagentInvocationCount > 0) {
      console.log(`    invocations: ${r.subagentInvocations.map((i) => i.subagent_type ?? i.skill ?? i.tool).join(", ")}`);
    }
    if (r.parseError) console.log(`    parseError: ${r.parseError}`);
    if (r.errors.length > 0) console.log(`    errors: ${r.errors.slice(0, 2).join("; ")}`);
    console.log("");
  }

  // Aggregate
  const totalCost = results.reduce((s, r) => s + r.costUsd, 0);
  const avgLatency = results.reduce((s, r) => s + r.latencyMs, 0) / results.length;
  const errorCount = results.filter(
    (r) => r.verdict === "NO_OUTPUT" || r.verdict === "PARSE_ERROR",
  ).length;

  // Verdict correctness: did the verdict match expectation?
  const verdictMatches = results.filter((r) => {
    if (r.expectedVerdict === "approve") return r.verdict === "approve";
    if (r.expectedVerdict === "reject_or_request_changes") {
      return r.verdict === "reject" || r.verdict === "request_changes";
    }
    if (r.expectedVerdict === "request_changes") {
      return r.verdict === "request_changes" || r.verdict === "reject";
    }
    return false;
  }).length;
  const verdictAccuracy = verdictMatches / results.length;

  const summary = {
    runAt: new Date().toISOString(),
    model: MODEL,
    plugins,
    variant,
    totalScenarios: results.length,
    verdictAccuracy,
    verdictMatches,
    errorCount,
    totalCostUsd: totalCost,
    avgCostUsd: totalCost / results.length,
    avgLatencyMs: avgLatency,
    perScenario: results.map((r) => ({
      id: r.scenarioId,
      label: r.label,
      expected: r.expectedVerdict,
      actual: r.verdict,
      match: (r.expectedVerdict === "approve" && r.verdict === "approve")
        || (r.expectedVerdict !== "approve" && (r.verdict === "reject" || r.verdict === "request_changes")),
      weightedRisk: r.weightedRisk,
      critical: r.criticalFindingCount,
      criteriaMet: `${r.criteriaMetCount}/${r.criteriaTotal}`,
      cost: r.costUsd,
      latencyMs: r.latencyMs,
    })),
  };

  writeFileSync(join(RESULTS_DIR, "summary.json"), JSON.stringify(summary, null, 2));

  console.log("=== SUMMARY ===");
  console.log(`Variant: ${variant}`);
  console.log(`Verdict accuracy: ${verdictMatches}/${results.length} (${(verdictAccuracy * 100).toFixed(0)}%)`);
  console.log(`Errored: ${errorCount}`);
  console.log(`Total cost: $${totalCost.toFixed(3)}`);
  console.log(`Avg cost: $${(totalCost / results.length).toFixed(3)}`);
  console.log(`Avg latency: ${(avgLatency / 1000).toFixed(1)}s`);
  console.log(`Results: ${RESULTS_DIR}/summary.json`);
}

main().catch((err) => {
  console.error("Reviewer spike failed:", err);
  process.exit(1);
});
