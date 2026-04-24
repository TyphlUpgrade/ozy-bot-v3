/**
 * Architect Decomposer Spike Runner
 *
 * Purpose: empirically test Architect decomposition mode before Wave B.5 smoke gate.
 * Specifically answers plan M.8: does the decomposer actually invoke OMC subagents
 * (`planner`, `architect`, `/team`) during project breakdown, or is OMC dead weight
 * here too (like in arbiter + reviewer)?
 *
 * 4 variants × 2 projects (medium + large) = 8 runs.
 *
 * Run: `npx tsx spikes/decomposer-spike/run.ts` from harness-ts/ root.
 * Toggle variants via ENABLE_OMC / ENABLE_CAVEMAN constants.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";
import type { SDKResultMessage, SDKMessage, Options } from "@anthropic-ai/claude-agent-sdk";
import {
  readFileSync,
  writeFileSync,
  mkdirSync,
  existsSync,
  rmSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const PROMPT_PATH = join(__dirname, "prompt.md");
const PROJECTS_PATH = join(__dirname, "projects.json");
const SANDBOX_ROOT = join(__dirname, "sandbox");
const RESULTS_DIR = join(__dirname, "results");

// --- Config (edit per variant) ---

const MODEL = "claude-sonnet-4-6";
const MAX_BUDGET_USD_PER_PROJECT = 5.0;
const MAX_TURNS = 40;
const ENABLE_OMC = true;
const ENABLE_CAVEMAN = false;
const FORCED_DELEGATION = false;
const PROJECT_FILTER: string[] = ["large"]; // empty = all; or ["large"] / ["medium"]

const FORCED_ADDENDUM = `

## MANDATORY DELEGATION PROTOCOL (Wave B.5 validation spike)

Before writing \`.project/plan.json\`, you MUST execute this review sequence. Do not skip it. Do not substitute your own review — the point is independent cross-check.

**Step 1: Draft your phase breakdown internally.** Plan phases, dependencies, acceptance criteria, decision boundaries. Do NOT write plan.json yet.

**Step 2: Invoke architect subagent for structural review.** Pass your draft as input:

\`\`\`
Task({
  subagent_type: "oh-my-claudecode:architect",
  description: "Review decomposition for structural issues",
  prompt: "Review this phase breakdown for architectural soundness. Identify: (a) missing phases required to meet stated acceptance criteria and non-goals, (b) bad dependency ordering, (c) phases that should split or merge, (d) critical-path errors. Return specific findings with phase IDs cited. Here is the project context and draft: <your project context + draft phase list verbatim>"
})
\`\`\`

**Step 3: Invoke critic subagent for completeness review.** Pass your draft + acceptance criteria:

\`\`\`
Task({
  subagent_type: "oh-my-claudecode:critic",
  description: "Review decomposition for gaps",
  prompt: "Pressure-test this phase breakdown against the operator's stated requirements. Find: (a) acceptance criteria without a corresponding phase, (b) non-goals that could be accidentally violated by phases as drafted, (c) operational concerns (observability, rollback testing, migration of in-flight state, performance baselining, post-cutover support) missing from phases, (d) acceptance criteria that are non-testable. Return specific findings with phase IDs cited. Here is the project context and draft: <your project context + draft phase list verbatim>"
})
\`\`\`

**Step 4: Revise draft addressing findings.** For each finding from architect or critic:
- Add missing phase OR extend existing phase acceptance criteria
- Reorder dependencies
- Split or merge phases as directed
- Update critical path if phase structure changed

You may disagree with a finding — if so, document in \`decompositionRationale\` WHY you rejected it.

**Step 5: Write plan.json.** With all findings integrated.

If either subagent invocation fails (tool error, session abort), retry once. If it fails again, proceed with your best single-pass draft and note the failure in \`decompositionRationale\`.

This mandatory protocol is for complex project decomposition (large scope). Single-pass decomposition is inadequate for plans covering 15+ phases — structural gaps cause downstream Executor escalations that cost more than the subagent consultation.
`;

// --- Types ---

interface Project {
  id: string;
  name: string;
  description: string;
  nonGoals: string[];
  constraints: string[];
  expectedPhaseRange: string;
  complexityClassification: "small" | "medium" | "large";
}

interface PhasePlan {
  phaseId: string;
  title: string;
  description: string;
  dependencies: string[];
  acceptanceCriteria: string[];
  decisionBoundaries: {
    executorDecides: string[];
    escalateToOperator: string[];
  };
  estimatedComplexity?: "small" | "medium" | "large";
}

interface ProjectPlan {
  projectId: string;
  projectDescription: string;
  nonGoals: string[];
  phases: PhasePlan[];
  criticalPath: string[];
  decompositionRationale: string;
}

interface SubagentInvocation {
  tool: string;
  subagent_type?: string;
  skill?: string;
}

interface RawResult {
  projectId: string;
  projectName: string;
  complexity: string;
  expectedPhaseRange: string;
  actualPhaseCount: number;
  nonGoalsPreservedCount: number;
  nonGoalsOperatorCount: number;
  nonGoalsAddedCount: number;
  criticalPathLength: number;
  allDependenciesValid: boolean;
  hasDecompositionRationale: boolean;
  subagentInvocations: SubagentInvocation[];
  subagentInvocationCount: number;
  fullPlan: unknown;
  parseError: string | null;
  costUsd: number;
  latencyMs: number;
  numTurns: number;
  inputTokens: number;
  outputTokens: number;
  terminalReason: string | null;
  sessionSuccess: boolean;
  errors: string[];
}

// --- Prompt formatting ---

function formatProjectPrompt(p: Project): string {
  return [
    "# Project Decomposition",
    "",
    "Decompose the following declared project into executable phases. Write the final plan to `.project/plan.json`. Stop after the file is written.",
    "",
    "## Project",
    `**Name:** ${p.name}`,
    `**Description:** ${p.description}`,
    "",
    "## Operator-Declared Non-Goals",
    ...p.nonGoals.map((ng, i) => `${i + 1}. ${ng}`),
    "",
    "## Constraints",
    ...p.constraints.map((c, i) => `${i + 1}. ${c}`),
    "",
    "Produce `.project/plan.json` with the schema from your systemPrompt. Expected phase count: ~" +
      p.expectedPhaseRange +
      ". If your decomposition lands substantially outside that range, explain why in `decompositionRationale`.",
  ].join("\n");
}

// --- Plan parsing ---

function parsePlan(raw: unknown): ProjectPlan | { error: string } {
  if (!raw || typeof raw !== "object") return { error: "Not an object" };
  const obj = raw as Record<string, unknown>;

  if (typeof obj.projectId !== "string") return { error: "Missing projectId" };
  if (typeof obj.projectDescription !== "string") return { error: "Missing projectDescription" };
  if (!Array.isArray(obj.nonGoals)) return { error: "Missing nonGoals array" };
  if (!Array.isArray(obj.phases)) return { error: "Missing phases array" };
  if (!Array.isArray(obj.criticalPath)) return { error: "Missing criticalPath array" };
  if (typeof obj.decompositionRationale !== "string") return { error: "Missing decompositionRationale" };

  // Basic phase validation
  for (const p of obj.phases as unknown[]) {
    if (!p || typeof p !== "object") return { error: "Phase is not an object" };
    const ph = p as Record<string, unknown>;
    if (typeof ph.phaseId !== "string") return { error: "Phase missing phaseId" };
    if (typeof ph.title !== "string") return { error: `Phase ${ph.phaseId} missing title` };
    if (typeof ph.description !== "string") return { error: `Phase ${ph.phaseId} missing description` };
    if (!Array.isArray(ph.dependencies)) return { error: `Phase ${ph.phaseId} missing dependencies` };
    if (!Array.isArray(ph.acceptanceCriteria)) return { error: `Phase ${ph.phaseId} missing acceptanceCriteria` };
  }

  return obj as unknown as ProjectPlan;
}

function validateDependencies(phases: PhasePlan[]): boolean {
  const ids = new Set(phases.map((p) => p.phaseId));
  for (const p of phases) {
    for (const dep of p.dependencies) {
      if (!ids.has(dep)) return false;
    }
  }
  return true;
}

function countNonGoalsPreserved(operatorNonGoals: string[], planNonGoals: string[]): {
  preserved: number;
  added: number;
} {
  const normalize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const opSet = new Set(operatorNonGoals.map(normalize));
  let preserved = 0;
  let added = 0;
  for (const ng of planNonGoals) {
    const n = normalize(ng);
    let matched = false;
    for (const op of opSet) {
      // Loose match: plan non-goal contains the operator non-goal's first significant phrase
      if (n.includes(op.split(" ").slice(0, 5).join(" ")) || op.includes(n.split(" ").slice(0, 5).join(" "))) {
        matched = true;
        break;
      }
    }
    if (matched) preserved++;
    else added++;
  }
  return { preserved, added };
}

// --- Subagent invocation tracking ---

function trackSubagentCalls(
  messages: SDKMessage[],
): SubagentInvocation[] {
  const invocations: SubagentInvocation[] = [];
  for (const msg of messages) {
    if (msg.type !== "assistant") continue;
    const m = msg as unknown as { message?: { content?: unknown[] } };
    const content = m.message?.content;
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      const b = block as Record<string, unknown>;
      if (b.type !== "tool_use") continue;
      const name = b.name as string | undefined;
      // Claude Code uses tool name "Agent" in tool_use blocks even though "Task" appears in the
      // registered tool list. Detect both names — Agent is the actual runtime name.
      if (name === "Task" || name === "Agent") {
        const input = b.input as Record<string, unknown> | undefined;
        const subagentType = input?.subagent_type as string | undefined;
        invocations.push({ tool: name, subagent_type: subagentType });
      } else if (name === "Skill") {
        const input = b.input as Record<string, unknown> | undefined;
        const skill = input?.skill as string | undefined;
        invocations.push({ tool: "Skill", skill });
      }
    }
  }
  return invocations;
}

// --- Per-project run ---

async function runOne(p: Project, systemPrompt: string): Promise<RawResult> {
  const sandbox = join(SANDBOX_ROOT, p.id);
  if (existsSync(sandbox)) rmSync(sandbox, { recursive: true, force: true });
  mkdirSync(join(sandbox, ".project"), { recursive: true });

  const userPrompt = formatProjectPrompt(p);

  const enabledPlugins: Record<string, boolean> = {};
  if (ENABLE_OMC) enabledPlugins["oh-my-claudecode@omc"] = true;
  if (ENABLE_CAVEMAN) enabledPlugins["caveman@caveman"] = true;

  const allowedTools = ENABLE_OMC
    ? ["Read", "Write", "Grep", "Glob", "Task", "Skill"]
    : ["Read", "Write", "Grep", "Glob"];

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
      append: systemPrompt,
    },
    maxBudgetUsd: MAX_BUDGET_USD_PER_PROJECT,
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
  const capturedMessages: SDKMessage[] = [];

  try {
    const q = query({ prompt: userPrompt, options });
    for await (const msg of q) {
      capturedMessages.push(msg);
      if (msg.type === "result") sessionResult = msg as SDKResultMessage;
    }
  } catch (err) {
    errors.push(String((err as Error).message ?? err));
  }
  const latencyMs = Date.now() - start;

  const planPath = join(sandbox, ".project", "plan.json");
  let parsedPlan: ProjectPlan | null = null;
  let parseError: string | null = null;
  let fullPlan: unknown = null;
  if (existsSync(planPath)) {
    try {
      fullPlan = JSON.parse(readFileSync(planPath, "utf-8"));
      const parsed = parsePlan(fullPlan);
      if ("error" in parsed) parseError = parsed.error;
      else parsedPlan = parsed;
    } catch (err) {
      parseError = `JSON parse: ${(err as Error).message}`;
    }
  } else {
    parseError = "plan.json not written";
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

  const subagentInvocations = trackSubagentCalls(capturedMessages);

  const phaseCount = parsedPlan?.phases.length ?? 0;
  const nonGoals = parsedPlan?.nonGoals ?? [];
  const { preserved, added } = countNonGoalsPreserved(p.nonGoals, nonGoals);

  return {
    projectId: p.id,
    projectName: p.name,
    complexity: p.complexityClassification,
    expectedPhaseRange: p.expectedPhaseRange,
    actualPhaseCount: phaseCount,
    nonGoalsPreservedCount: preserved,
    nonGoalsOperatorCount: p.nonGoals.length,
    nonGoalsAddedCount: added,
    criticalPathLength: parsedPlan?.criticalPath.length ?? 0,
    allDependenciesValid: parsedPlan ? validateDependencies(parsedPlan.phases) : false,
    hasDecompositionRationale: !!(parsedPlan?.decompositionRationale && parsedPlan.decompositionRationale.length > 20),
    subagentInvocations,
    subagentInvocationCount: subagentInvocations.length,
    fullPlan,
    parseError,
    costUsd,
    latencyMs,
    numTurns,
    inputTokens,
    outputTokens,
    terminalReason,
    sessionSuccess,
    errors,
  };
}

// --- Main ---

async function main() {
  if (!existsSync(PROMPT_PATH)) throw new Error(`Prompt not found: ${PROMPT_PATH}`);
  if (!existsSync(PROJECTS_PATH)) throw new Error(`Projects not found: ${PROJECTS_PATH}`);

  mkdirSync(RESULTS_DIR, { recursive: true });

  const baseSystemPrompt = readFileSync(PROMPT_PATH, "utf-8");
  const systemPrompt = FORCED_DELEGATION ? baseSystemPrompt + FORCED_ADDENDUM : baseSystemPrompt;
  const allProjects: Project[] = JSON.parse(readFileSync(PROJECTS_PATH, "utf-8"));
  const projects = PROJECT_FILTER.length > 0
    ? allProjects.filter((p) => PROJECT_FILTER.includes(p.id))
    : allProjects;

  const plugins: string[] = [];
  if (ENABLE_OMC) plugins.push("omc");
  if (ENABLE_CAVEMAN) plugins.push("caveman");
  const variant = plugins.length > 0 ? `${MODEL}+${plugins.join("+")}` : `${MODEL}-bare`;

  console.log(`Decomposer spike — ${projects.length} projects`);
  console.log(`Model: ${MODEL}`);
  console.log(`Plugins: ${plugins.length > 0 ? plugins.join(", ") : "none"}`);
  console.log(`Variant: ${variant}`);
  console.log(`Per-project budget: $${MAX_BUDGET_USD_PER_PROJECT.toFixed(2)}`);
  console.log("");

  const results: RawResult[] = [];
  for (const p of projects) {
    console.log(`>>> [${p.id}] ${p.name} (${p.complexityClassification}) — starting...`);
    const r = await runOne(p, systemPrompt);
    results.push(r);
    writeFileSync(join(RESULTS_DIR, `raw-${p.id}.json`), JSON.stringify(r, null, 2));
    const latencyStr = `${(r.latencyMs / 1000).toFixed(1)}s`;
    const costStr = `$${r.costUsd.toFixed(3)}`;
    const phasesStr = `phases=${r.actualPhaseCount}/expected ${r.expectedPhaseRange}`;
    const subagentsStr = `subagents=${r.subagentInvocationCount}`;
    console.log(
      `    ${phasesStr} ${subagentsStr} cost=${costStr} latency=${latencyStr} turns=${r.numTurns}`,
    );
    if (r.parseError) console.log(`    parseError: ${r.parseError}`);
    if (r.subagentInvocationCount > 0) {
      console.log(`    invocations: ${r.subagentInvocations.map((i) => i.subagent_type ?? i.skill ?? i.tool).join(", ")}`);
    }
    if (r.errors.length > 0) console.log(`    errors: ${r.errors.slice(0, 2).join("; ")}`);
    console.log("");
  }

  const totalCost = results.reduce((s, r) => s + r.costUsd, 0);
  const totalSubagents = results.reduce((s, r) => s + r.subagentInvocationCount, 0);
  const avgLatency = results.reduce((s, r) => s + r.latencyMs, 0) / results.length;
  const parseErrors = results.filter((r) => r.parseError).length;

  const summary = {
    runAt: new Date().toISOString(),
    model: MODEL,
    plugins,
    variant,
    totalProjects: results.length,
    parseErrorCount: parseErrors,
    totalSubagentInvocations: totalSubagents,
    totalCostUsd: totalCost,
    avgCostUsd: totalCost / results.length,
    avgLatencyMs: avgLatency,
    perProject: results.map((r) => ({
      id: r.projectId,
      complexity: r.complexity,
      phases: r.actualPhaseCount,
      expected: r.expectedPhaseRange,
      nonGoalsPreserved: `${r.nonGoalsPreservedCount}/${r.nonGoalsOperatorCount}`,
      nonGoalsAdded: r.nonGoalsAddedCount,
      criticalPathLen: r.criticalPathLength,
      depsValid: r.allDependenciesValid,
      hasRationale: r.hasDecompositionRationale,
      subagents: r.subagentInvocationCount,
      cost: r.costUsd,
      latencyMs: r.latencyMs,
      turns: r.numTurns,
      parseError: r.parseError,
    })),
  };

  writeFileSync(join(RESULTS_DIR, "summary.json"), JSON.stringify(summary, null, 2));

  console.log("=== SUMMARY ===");
  console.log(`Variant: ${variant}`);
  console.log(`Projects: ${results.length}`);
  console.log(`Parse errors: ${parseErrors}`);
  console.log(`TOTAL SUBAGENT INVOCATIONS: ${totalSubagents}`);
  console.log(`Total cost: $${totalCost.toFixed(3)}`);
  console.log(`Avg cost: $${(totalCost / results.length).toFixed(3)}`);
  console.log(`Avg latency: ${(avgLatency / 1000).toFixed(1)}s`);
  console.log(`Results: ${RESULTS_DIR}/summary.json`);
}

main().catch((err) => {
  console.error("Decomposer spike failed:", err);
  process.exit(1);
});
