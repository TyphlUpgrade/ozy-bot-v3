/**
 * Direct verification that OMC plugin loading + Task tool + subagent invocation works
 * in the SDK session environment. Bypass all decomposer logic.
 *
 * Run: `npx tsx spikes/decomposer-spike/verify-omc.ts` from harness-ts/ root.
 */

import { query } from "@anthropic-ai/claude-agent-sdk";
import type { SDKMessage, Options } from "@anthropic-ai/claude-agent-sdk";
import { mkdirSync, rmSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const SANDBOX = join(__dirname, "sandbox", "verify-omc");
if (existsSync(SANDBOX)) rmSync(SANDBOX, { recursive: true, force: true });
mkdirSync(SANDBOX, { recursive: true });

const PROMPT = `Test OMC plugin + Task subagent availability.

Do exactly three things:

1. List the subagent types available to you via the Task tool. Report as JSON array.

2. Invoke this:
\`\`\`
Task({
  subagent_type: "oh-my-claudecode:planner",
  description: "OMC verification probe",
  prompt: "Return exactly this string with no other text: OMC_VERIFIED_PLANNER"
})
\`\`\`
Report what Task returned.

3. Invoke this:
\`\`\`
Task({
  subagent_type: "oh-my-claudecode:architect",
  description: "OMC verification probe",
  prompt: "Return exactly this string with no other text: OMC_VERIFIED_ARCHITECT"
})
\`\`\`
Report what Task returned.

If any Task call fails (subagent type not found, tool error, etc.), report the exact error. Do not work around failures. Do not redirect to different tools. Report verbatim.`;

const options: Options = {
  cwd: SANDBOX,
  model: "claude-sonnet-4-6",
  permissionMode: "bypassPermissions",
  allowDangerouslySkipPermissions: true,
  settingSources: ["project"],
  settings: {
    enabledPlugins: { "oh-my-claudecode@omc": true },
  } as unknown as Options["settings"],
  maxBudgetUsd: 2.0,
  maxTurns: 15,
  allowedTools: ["Read", "Write", "Task", "Skill"],
  disallowedTools: [
    "Bash", "Edit", "WebFetch", "WebSearch",
    "CronCreate", "CronDelete", "CronList",
    "RemoteTrigger", "ScheduleWakeup", "TaskCreate",
  ],
  persistSession: false,
};

async function main() {
  console.log("Probing OMC plugin + Task tool availability...\n");

  const messages: SDKMessage[] = [];
  const toolCalls: Array<{ name: string; input: unknown }> = [];
  let systemInitTools: string[] = [];

  try {
    const q = query({ prompt: PROMPT, options });
    for await (const msg of q) {
      messages.push(msg);

      if (msg.type === "system" && (msg as unknown as { subtype?: string }).subtype === "init") {
        const m = msg as unknown as { tools?: string[] };
        systemInitTools = m.tools ?? [];
      }

      if (msg.type === "assistant") {
        const m = msg as unknown as { message?: { content?: unknown[] } };
        const content = m.message?.content;
        if (Array.isArray(content)) {
          for (const block of content) {
            if (block && typeof block === "object") {
              const b = block as Record<string, unknown>;
              if (b.type === "tool_use") {
                toolCalls.push({ name: b.name as string, input: b.input });
              }
            }
          }
        }
      }

      if (msg.type === "result") {
        const r = msg as unknown as { total_cost_usd?: number; num_turns?: number; subtype?: string };
        console.log("\n=== SESSION RESULT ===");
        console.log(`subtype: ${r.subtype}`);
        console.log(`cost: $${(r.total_cost_usd ?? 0).toFixed(3)}`);
        console.log(`turns: ${r.num_turns}`);
      }
    }
  } catch (err) {
    console.error("Session error:", err);
  }

  console.log("\n=== TOOLS AVAILABLE AT SESSION INIT ===");
  console.log(`Total tools registered: ${systemInitTools.length}`);
  console.log("Full list:");
  for (const t of systemInitTools) console.log(`  - ${t}`);

  console.log("\n=== TOOL CALLS MADE ===");
  console.log(`Total: ${toolCalls.length}`);
  for (const c of toolCalls) {
    const subagent = (c.input as { subagent_type?: string })?.subagent_type;
    const skill = (c.input as { skill?: string })?.skill;
    console.log(`  - ${c.name}${subagent ? ` (subagent_type=${subagent})` : ""}${skill ? ` (skill=${skill})` : ""}`);
  }

  // Check for OMC-specific MCP tools
  const omcMcpTools = systemInitTools.filter((t) => t.includes("oh-my-claudecode") || t.includes("omc"));
  console.log("\n=== OMC MCP TOOL PRESENCE CHECK ===");
  if (omcMcpTools.length > 0) {
    console.log("OMC MCP tools detected:");
    for (const t of omcMcpTools) console.log(`  + ${t}`);
  } else {
    console.log("NO OMC-prefixed tools in registered tool list");
  }

  // Check for Task tool presence
  const hasTask = systemInitTools.includes("Task");
  console.log(`\nTask tool in registered list: ${hasTask ? "YES" : "NO"}`);
}

main().catch((err) => {
  console.error("Verify failed:", err);
  process.exit(1);
});
