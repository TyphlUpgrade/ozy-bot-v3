import { describe, it, expect, vi } from "vitest";
import {
  SDKClient,
  classifyMessage,
  parseResult,
  type QueryFn,
  type SessionConfig,
} from "../../src/session/sdk.js";
import type {
  Query,
  SDKMessage,
  SDKResultSuccess,
  SDKResultError,
  SDKSystemMessage,
  SDKAssistantMessage,
} from "@anthropic-ai/claude-agent-sdk";

// --- Mock helpers ---

/** Create an async generator that yields messages then returns */
function mockQuery(messages: SDKMessage[]): Query {
  async function* gen(): AsyncGenerator<SDKMessage, void> {
    for (const msg of messages) {
      yield msg;
    }
  }
  const g = gen();
  // Query extends AsyncGenerator, add stub methods
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

function makeResultSuccess(overrides?: Partial<SDKResultSuccess>): SDKResultSuccess {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 5000,
    duration_api_ms: 4000,
    is_error: false,
    num_turns: 3,
    result: "Task completed",
    stop_reason: "end_turn",
    total_cost_usd: 0.05,
    usage: { input_tokens: 1000, output_tokens: 500 },
    modelUsage: {},
    permission_denials: [],
    uuid: "msg-uuid-1" as SDKResultSuccess["uuid"],
    session_id: "session-123",
    ...overrides,
  };
}

function makeResultError(overrides?: Partial<SDKResultError>): SDKResultError {
  return {
    type: "result",
    subtype: "error_during_execution",
    duration_ms: 2000,
    duration_api_ms: 1500,
    is_error: true,
    num_turns: 1,
    stop_reason: null,
    total_cost_usd: 0.01,
    usage: { input_tokens: 200, output_tokens: 50 },
    modelUsage: {},
    permission_denials: [],
    errors: ["API rate limit exceeded"],
    uuid: "msg-uuid-2" as SDKResultError["uuid"],
    session_id: "session-456",
    ...overrides,
  };
}

function makeSystemInit(): SDKSystemMessage {
  return {
    type: "system",
    subtype: "init",
    apiKeySource: "user",
    claude_code_version: "2.0.0",
    cwd: "/tmp/test",
    tools: ["Read", "Edit", "Bash"],
    mcp_servers: [],
    model: "claude-sonnet-4-6",
    permissionMode: "bypassPermissions",
  } as unknown as SDKSystemMessage;
}

// --- Tests ---

describe("classifyMessage", () => {
  it("classifies result success", () => {
    expect(classifyMessage(makeResultSuccess())).toBe("result_success");
  });

  it("classifies result error", () => {
    expect(classifyMessage(makeResultError())).toBe("result_error");
  });

  it("classifies system init", () => {
    expect(classifyMessage(makeSystemInit())).toBe("system_init");
  });

  it("classifies assistant messages", () => {
    const msg = { type: "assistant", session_id: "s1" } as unknown as SDKMessage;
    expect(classifyMessage(msg)).toBe("assistant");
  });

  it("classifies user messages", () => {
    const msg = { type: "user" } as unknown as SDKMessage;
    expect(classifyMessage(msg)).toBe("user");
  });

  it("classifies unknown types as other", () => {
    const msg = { type: "stream_event" } as unknown as SDKMessage;
    expect(classifyMessage(msg)).toBe("other");
  });
});

describe("parseResult", () => {
  it("parses success result", () => {
    const result = parseResult(makeResultSuccess());
    expect(result.sessionId).toBe("session-123");
    expect(result.success).toBe(true);
    expect(result.result).toBe("Task completed");
    expect(result.totalCostUsd).toBe(0.05);
    expect(result.numTurns).toBe(3);
    expect(result.usage.input_tokens).toBe(1000);
    expect(result.usage.output_tokens).toBe(500);
    expect(result.errors).toHaveLength(0);
  });

  it("parses error result", () => {
    const result = parseResult(makeResultError());
    expect(result.sessionId).toBe("session-456");
    expect(result.success).toBe(false);
    expect(result.errors).toContain("API rate limit exceeded");
    expect(result.totalCostUsd).toBe(0.01);
  });
});

describe("SDKClient", () => {
  function makeClient(queryFn?: QueryFn): SDKClient {
    return new SDKClient(queryFn ?? (() => mockQuery([makeResultSuccess()])));
  }

  describe("spawnSession", () => {
    it("calls query with correct options", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);

      const config: SessionConfig = {
        prompt: "fix the bug",
        cwd: "/tmp/worktree",
        model: "claude-sonnet-4-6",
        maxBudgetUsd: 1.0,
        settingSources: ["project"],
      };

      client.spawnSession(config);

      expect(queryFn).toHaveBeenCalledOnce();
      const call = queryFn.mock.calls[0][0];
      expect(call.prompt).toBe("fix the bug");
      expect(call.options.cwd).toBe("/tmp/worktree");
      expect(call.options.model).toBe("claude-sonnet-4-6");
      expect(call.options.maxBudgetUsd).toBe(1.0);
      expect(call.options.settingSources).toEqual(["project"]);
      expect(call.options.permissionMode).toBe("bypassPermissions");
      expect(call.options.allowDangerouslySkipPermissions).toBe(true);
    });

    it("returns query and abort controller", () => {
      const client = makeClient();
      const { query, abortController } = client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
      });
      expect(query).toBeTruthy();
      expect(abortController).toBeInstanceOf(AbortController);
    });

    it("uses provided abort controller", () => {
      const client = makeClient();
      const ac = new AbortController();
      const result = client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
        abortController: ac,
      });
      expect(result.abortController).toBe(ac);
    });

    it("appends system prompt to preset", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
        systemPrompt: "You are a code reviewer.",
      });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.systemPrompt).toEqual({
        type: "preset",
        preset: "claude_code",
        append: "You are a code reviewer.",
      });
    });

    it("passes resume option for session resume", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({
        prompt: "continue",
        cwd: "/tmp",
        resume: "session-old-123",
      });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.resume).toBe("session-old-123");
    });
  });

  describe("consumeStream", () => {
    it("collects result from stream", async () => {
      const messages: SDKMessage[] = [makeSystemInit(), makeResultSuccess()];
      const client = makeClient(() => mockQuery(messages));
      const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const result = await client.consumeStream(query);

      expect(result.success).toBe(true);
      expect(result.sessionId).toBe("session-123");
      expect(result.totalCostUsd).toBe(0.05);
    });

    it("calls onMessage for each message", async () => {
      const messages: SDKMessage[] = [makeSystemInit(), makeResultSuccess()];
      const client = makeClient(() => mockQuery(messages));
      const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const seen: string[] = [];
      await client.consumeStream(query, (msg) => seen.push(msg.type));

      expect(seen).toEqual(["system", "result"]);
    });

    it("captures model from system_init message into SessionResult.modelName (WA-5)", async () => {
      const messages: SDKMessage[] = [makeSystemInit(), makeResultSuccess()];
      const client = makeClient(() => mockQuery(messages));
      const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const result = await client.consumeStream(query);
      expect(result.modelName).toBe("claude-sonnet-4-6");
    });

    it("returns error result when stream has error", async () => {
      const messages: SDKMessage[] = [makeResultError()];
      const client = makeClient(() => mockQuery(messages));
      const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const result = await client.consumeStream(query);

      expect(result.success).toBe(false);
      expect(result.errors).toContain("API rate limit exceeded");
    });

    it("handles stream ending without result", async () => {
      const messages: SDKMessage[] = [makeSystemInit()];
      const client = makeClient(() => mockQuery(messages));
      const { query } = client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const result = await client.consumeStream(query);

      expect(result.success).toBe(false);
      expect(result.errors).toContain("Stream ended without result message");
    });
  });

  describe("abort/register", () => {
    it("aborts registered session", () => {
      const client = makeClient();
      const ac = new AbortController();
      client.registerController("s1", ac);
      expect(client.activeSessionCount).toBe(1);

      const aborted = client.abortSession("s1");
      expect(aborted).toBe(true);
      expect(ac.signal.aborted).toBe(true);
      expect(client.activeSessionCount).toBe(0);
    });

    it("returns false for unknown session", () => {
      const client = makeClient();
      expect(client.abortSession("nonexistent")).toBe(false);
    });

    it("unregister removes controller", () => {
      const client = makeClient();
      client.registerController("s1", new AbortController());
      expect(client.activeSessionCount).toBe(1);
      client.unregisterController("s1");
      expect(client.activeSessionCount).toBe(0);
    });
  });

  describe("resumeSession", () => {
    it("calls spawnSession with resume option", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.resumeSession("old-session", { prompt: "continue work", cwd: "/tmp" });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.resume).toBe("old-session");
    });
  });

  // Wave 1 pre-requisites
  describe("Wave 1: plugins / hooks / disallowedTools", () => {
    it("Item 1: passes enabledPlugins via options.settings", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
        enabledPlugins: {
          "oh-my-claudecode@omc": true,
          "caveman@caveman": true,
        },
      });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.settings).toBeDefined();
      expect((opts.settings as { enabledPlugins?: Record<string, boolean> }).enabledPlugins).toEqual({
        "oh-my-claudecode@omc": true,
        "caveman@caveman": true,
      });
    });

    it("Item 1: omits settings when enabledPlugins empty or missing", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.settings).toBeUndefined();
    });

    it("Item 2: always sets options.hooks to empty object by default", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.hooks).toBeDefined();
      expect(opts.hooks).toEqual({});
    });

    it("Item 2: passes custom hooks through when provided", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      const customHooks = { PreToolUse: [{ matcher: "Bash", hooks: [] }] };
      client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
        hooks: customHooks as unknown as Partial<Record<string, unknown[]>>,
      });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.hooks).toEqual(customHooks);
    });

    it("Item 3: propagates disallowedTools to options", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({
        prompt: "test",
        cwd: "/tmp",
        disallowedTools: ["CronCreate", "RemoteTrigger"],
      });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.disallowedTools).toEqual(["CronCreate", "RemoteTrigger"]);
    });

    it("Item 3: omits disallowedTools when not provided", () => {
      const queryFn = vi.fn().mockReturnValue(mockQuery([makeResultSuccess()]));
      const client = new SDKClient(queryFn);
      client.spawnSession({ prompt: "test", cwd: "/tmp" });
      const opts = queryFn.mock.calls[0][0].options;
      expect(opts.disallowedTools).toBeUndefined();
    });
  });
});
