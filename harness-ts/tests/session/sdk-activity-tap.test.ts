import { describe, it, expect, vi } from "vitest";
import { SDKClient } from "../../src/session/sdk.js";
import type {
  Query,
  SDKMessage,
  SDKResultSuccess,
  SDKSystemMessage,
} from "@anthropic-ai/claude-agent-sdk";

// --- Mock helpers (mirrors tests/session/sdk.test.ts style) ---

function mockQuery(messages: SDKMessage[]): Query {
  async function* gen(): AsyncGenerator<SDKMessage, void> {
    for (const msg of messages) {
      // Tiny await to ensure Date.now() can advance between yields on fast clocks.
      await Promise.resolve();
      yield msg;
    }
  }
  const g = gen();
  return Object.assign(g, {
    interrupt: vi.fn().mockResolvedValue(undefined),
    setPermissionMode: vi.fn().mockResolvedValue(undefined),
    setModel: vi.fn().mockResolvedValue(undefined),
    setMaxThinkingTokens: vi.fn().mockResolvedValue(undefined),
    applyFlagSettings: vi.fn().mockResolvedValue(undefined),
    initializationResult: vi.fn().mockResolvedValue({}),
    supportedCommands: vi.fn().mockResolvedValue([]),
    supportedModels: vi.fn().mockResolvedValue([]),
    supportedAgents: vi.fn().mockResolvedValue([]),
    mcpServerStatus: vi.fn().mockResolvedValue([]),
    contextUsage: vi.fn().mockResolvedValue({}),
    rewindFiles: vi.fn().mockResolvedValue({ canRewind: false }),
  }) as unknown as Query;
}

function makeSystemInit(): SDKSystemMessage {
  return {
    type: "system",
    subtype: "init",
    apiKeySource: "user",
    claude_code_version: "2.0.0",
    cwd: "/tmp/test",
    tools: ["Read"],
    mcp_servers: [],
    model: "claude-sonnet-4-6",
    permissionMode: "bypassPermissions",
  } as unknown as SDKSystemMessage;
}

function makeAssistant(sessionId: string): SDKMessage {
  return { type: "assistant", session_id: sessionId } as unknown as SDKMessage;
}

function makeResultSuccess(): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 5000,
    duration_api_ms: 4000,
    is_error: false,
    num_turns: 3,
    result: "ok",
    stop_reason: "end_turn",
    total_cost_usd: 0.01,
    usage: { input_tokens: 10, output_tokens: 5 },
    modelUsage: {},
    permission_denials: [],
    uuid: "msg-uuid-1" as SDKResultSuccess["uuid"],
    session_id: "session-tap-1",
  };
}

// --- Tests ---

describe("consumeStream — stall watchdog activity tap (commit 1/2)", () => {
  it("populates lastActivityAt on SessionResult after a successful stream", async () => {
    const before = Date.now();
    const messages: SDKMessage[] = [
      makeSystemInit(),
      makeAssistant("session-tap-1"),
      makeAssistant("session-tap-1"),
      makeResultSuccess(),
    ];
    const client = new SDKClient(() => mockQuery(messages));
    const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
    const result = await client.consumeStream(query);

    expect(result.lastActivityAt).toBeDefined();
    expect(result.lastActivityAt).toBeGreaterThanOrEqual(before);
    expect(result.lastActivityAt).toBeLessThanOrEqual(Date.now());
  });

  it("updates lastActivityAt monotonically across yielded messages", async () => {
    const observed: number[] = [];
    const messages: SDKMessage[] = [
      makeSystemInit(),
      makeAssistant("session-tap-2"),
      makeAssistant("session-tap-2"),
      makeResultSuccess(),
    ];
    const client = new SDKClient(() => mockQuery(messages));
    const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });

    // Sample Date.now() inside onMessage to mirror what consumeStream stamps.
    await client.consumeStream(query, () => observed.push(Date.now()));

    expect(observed.length).toBe(messages.length);
    for (let i = 1; i < observed.length; i++) {
      expect(observed[i]).toBeGreaterThanOrEqual(observed[i - 1]);
    }
  });

  it("populates lastActivityAt even when stream ends without result message", async () => {
    const before = Date.now();
    const messages: SDKMessage[] = [makeSystemInit()];
    const client = new SDKClient(() => mockQuery(messages));
    const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
    const result = await client.consumeStream(query);

    expect(result.success).toBe(false);
    expect(result.errors).toContain("Stream ended without result message");
    expect(result.lastActivityAt).toBeDefined();
    expect(result.lastActivityAt).toBeGreaterThanOrEqual(before);
  });

  it.todo("orchestrator watchdog interval — commit 2");
});
