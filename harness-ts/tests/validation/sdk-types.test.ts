/**
 * SDK type conformance tests — compile-time verification that our wrapper's
 * assumptions about @anthropic-ai/claude-agent-sdk types are correct.
 *
 * These tests do NOT call the real SDK at runtime. They use TypeScript's type
 * system to assert that the shapes we rely on actually exist in the SDK's
 * published type definitions. A compile error here means our wrapper has drifted
 * from the real SDK types.
 *
 * Pattern: construct a typed object, access fields through our wrapper's lens,
 * confirm everything satisfies the expected types without `any` escape hatches
 * in the assertions themselves.
 */

import { describe, it, expect } from "vitest";
import type {
  Options,
  Query,
  SDKMessage,
  SDKResultError,
  SDKResultSuccess,
  SDKResultMessage,
} from "@anthropic-ai/claude-agent-sdk";
import type {
  QueryFn,
  SessionConfig,
  SessionResult,
} from "../../src/session/sdk.js";
import { classifyMessage, parseResult } from "../../src/session/sdk.js";

// ---------------------------------------------------------------------------
// 1. SDKResultSuccess field access
//    Verify every field parseResult() reads exists at the correct type on the
//    real SDKResultSuccess type.
// ---------------------------------------------------------------------------

describe("SDKResultSuccess field conformance", () => {
  it("has type: 'result'", () => {
    // Compile-time check: "result" must be assignable to SDKResultSuccess["type"]
    const _type: SDKResultSuccess["type"] = "result";
    expect(_type).toBe("result");
  });

  it("has subtype: 'success'", () => {
    const _subtype: SDKResultSuccess["subtype"] = "success";
    expect(_subtype).toBe("success");
  });

  it("has session_id: string", () => {
    const _sessionId: SDKResultSuccess["session_id"] = "test-session";
    expect(_sessionId).toBe("test-session");
  });

  it("has total_cost_usd: number", () => {
    const _cost: SDKResultSuccess["total_cost_usd"] = 0.5;
    expect(_cost).toBe(0.5);
  });

  it("has num_turns: number", () => {
    const _turns: SDKResultSuccess["num_turns"] = 3;
    expect(_turns).toBe(3);
  });

  it("has result: string", () => {
    const _result: SDKResultSuccess["result"] = "Done";
    expect(_result).toBe("Done");
  });

  it("has usage with input_tokens and output_tokens", () => {
    // Verify the type structure without dereferencing through an empty shell object.
    // Cast through the usage type directly — if the fields don't exist on
    // NonNullableUsage, this assignment won't compile.
    const _inputTokens: number = (0 as SDKResultSuccess["usage"]["input_tokens"]);
    const _outputTokens: number = (0 as SDKResultSuccess["usage"]["output_tokens"]);
    expect(_inputTokens).toBe(0);
    expect(_outputTokens).toBe(0);
  });

  it("has optional terminal_reason", () => {
    // terminal_reason is optional — type must accept undefined
    const _defined: SDKResultSuccess["terminal_reason"] = "completed";
    const _undef: SDKResultSuccess["terminal_reason"] = undefined;
    expect(_defined).toBe("completed");
    expect(_undef).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 2. SDKResultError subtype union
//    The SDK defines 4 specific error subtypes — not just the string "error".
//    Verify all 4 are part of the union.
// ---------------------------------------------------------------------------

describe("SDKResultError subtype union covers all 4 variants", () => {
  it("accepts error_during_execution as subtype", () => {
    const _subtype: SDKResultError["subtype"] = "error_during_execution";
    expect(_subtype).toBe("error_during_execution");
  });

  it("accepts error_max_turns as subtype", () => {
    const _subtype: SDKResultError["subtype"] = "error_max_turns";
    expect(_subtype).toBe("error_max_turns");
  });

  it("accepts error_max_budget_usd as subtype", () => {
    const _subtype: SDKResultError["subtype"] = "error_max_budget_usd";
    expect(_subtype).toBe("error_max_budget_usd");
  });

  it("accepts error_max_structured_output_retries as subtype", () => {
    const _subtype: SDKResultError["subtype"] =
      "error_max_structured_output_retries";
    expect(_subtype).toBe("error_max_structured_output_retries");
  });

  it("has errors: string[] field", () => {
    const _errors: SDKResultError["errors"] = ["test error"];
    expect(_errors).toEqual(["test error"]);
  });
});

// ---------------------------------------------------------------------------
// 3. classifyMessage handles all error subtypes via subtype !== "success"
//    Our classifier returns "result_error" for every error subtype without
//    enumerating them — verify that pattern works correctly at runtime for
//    all 4 actual SDK error subtypes.
// ---------------------------------------------------------------------------

describe("classifyMessage catches all SDKResultError subtypes", () => {
  const errorSubtypes: SDKResultError["subtype"][] = [
    "error_during_execution",
    "error_max_turns",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
  ];

  for (const subtype of errorSubtypes) {
    it(`classifies subtype '${subtype}' as result_error`, () => {
      const msg = { type: "result", subtype } as unknown as SDKMessage;
      expect(classifyMessage(msg)).toBe("result_error");
    });
  }

  it("classifies subtype 'success' as result_success (not an error)", () => {
    const msg = {
      type: "result",
      subtype: "success",
    } as unknown as SDKMessage;
    expect(classifyMessage(msg)).toBe("result_success");
  });
});

// ---------------------------------------------------------------------------
// 4. Options type accepts our SessionConfig mapping
//    Every Options field we write in spawnSession() must be a valid key with
//    the correct value type.
// ---------------------------------------------------------------------------

describe("Options type accepts all fields written by spawnSession", () => {
  it("accepts cwd: string", () => {
    const opts = { cwd: "/tmp/worktree" } satisfies Partial<Options>;
    expect(opts.cwd).toBe("/tmp/worktree");
  });

  it("accepts abortController: AbortController", () => {
    const ac = new AbortController();
    const opts = { abortController: ac } satisfies Partial<Options>;
    expect(opts.abortController).toBe(ac);
  });

  it("accepts permissionMode from SessionConfig union", () => {
    // Our SessionConfig uses Options["permissionMode"] so this is always in sync
    const mode: Options["permissionMode"] = "bypassPermissions";
    const opts = { permissionMode: mode } satisfies Partial<Options>;
    expect(opts.permissionMode).toBe("bypassPermissions");
  });

  it("accepts allowDangerouslySkipPermissions: boolean", () => {
    const opts = {
      allowDangerouslySkipPermissions: true,
    } satisfies Partial<Options>;
    expect(opts.allowDangerouslySkipPermissions).toBe(true);
  });

  it("accepts model: string", () => {
    const opts = { model: "claude-sonnet-4-6" } satisfies Partial<Options>;
    expect(opts.model).toBe("claude-sonnet-4-6");
  });

  it("accepts maxBudgetUsd: number", () => {
    const opts = { maxBudgetUsd: 2.5 } satisfies Partial<Options>;
    expect(opts.maxBudgetUsd).toBe(2.5);
  });

  it("accepts maxTurns: number", () => {
    const opts = { maxTurns: 10 } satisfies Partial<Options>;
    expect(opts.maxTurns).toBe(10);
  });

  it("accepts allowedTools: string[]", () => {
    const opts = {
      allowedTools: ["Read", "Edit"],
    } satisfies Partial<Options>;
    expect(opts.allowedTools).toEqual(["Read", "Edit"]);
  });

  it("accepts disallowedTools: string[]", () => {
    const opts = {
      disallowedTools: ["Bash"],
    } satisfies Partial<Options>;
    expect(opts.disallowedTools).toEqual(["Bash"]);
  });

  it("accepts sessionId: string", () => {
    const opts = {
      sessionId: "550e8400-e29b-41d4-a716-446655440000",
    } satisfies Partial<Options>;
    expect(opts.sessionId).toBeDefined();
  });

  it("accepts persistSession: boolean", () => {
    const opts = { persistSession: false } satisfies Partial<Options>;
    expect(opts.persistSession).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 5. Options.systemPrompt accepts preset+append shape
//    Our spawnSession() writes:
//      { type: "preset", preset: "claude_code", append: string }
//    Verify this object literal satisfies the SDK's systemPrompt union type.
// ---------------------------------------------------------------------------

describe("Options.systemPrompt accepts preset+append shape", () => {
  it("accepts the exact object our spawnSession writes", () => {
    const systemPromptValue = {
      type: "preset" as const,
      preset: "claude_code" as const,
      append: "You are a code reviewer.",
    } satisfies NonNullable<Options["systemPrompt"]>;
    expect(systemPromptValue.type).toBe("preset");
    expect(systemPromptValue.preset).toBe("claude_code");
    expect(systemPromptValue.append).toBe("You are a code reviewer.");
  });

  it("accepts preset without append (append is optional)", () => {
    const systemPromptValue = {
      type: "preset" as const,
      preset: "claude_code" as const,
    } satisfies NonNullable<Options["systemPrompt"]>;
    expect(systemPromptValue.preset).toBe("claude_code");
  });

  it("accepts a plain string system prompt", () => {
    const opts = {
      systemPrompt: "You are a helpful assistant.",
    } satisfies Partial<Options>;
    expect(typeof opts.systemPrompt).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// 6. Options.resume field exists for session resumption
//    resumeSession() passes resume: sessionId to spawnSession(), which writes
//    options.resume = config.resume. Verify the field is on Options.
// ---------------------------------------------------------------------------

describe("Options.resume field exists", () => {
  it("accepts resume: string", () => {
    const opts = { resume: "session-abc-123" } satisfies Partial<Options>;
    expect(opts.resume).toBe("session-abc-123");
  });

  it("resume is optional (accepts omission)", () => {
    // Should compile without resume — the field is optional
    const opts = { cwd: "/tmp" } satisfies Partial<Options>;
    const _resume: string | undefined = opts.resume;
    expect(_resume).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 7. Options.settingSources accepts ["project"]
//    Our default is settingSources: ["project"]. SettingSource is
//    'user' | 'project' | 'local' — verify "project" is a valid member.
// ---------------------------------------------------------------------------

describe("Options.settingSources accepts ['project']", () => {
  it("accepts ['project'] array", () => {
    const opts = {
      settingSources: ["project"],
    } satisfies Partial<Options>;
    expect(opts.settingSources).toEqual(["project"]);
  });

  it("accepts all three SettingSource values in one array", () => {
    const opts = {
      settingSources: ["user", "project", "local"],
    } satisfies Partial<Options>;
    expect(opts.settingSources).toHaveLength(3);
  });

  it("settingSources is optional (accepts empty options object)", () => {
    const opts = {} satisfies Partial<Options>;
    const _sources: Options["settingSources"] = opts.settingSources;
    expect(_sources).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 8. QueryFn type is compatible with the SDK's query() signature
//    Our QueryFn is: (params: { prompt: string; options?: Options }) => Query
//    The SDK's query() is: (_params: { prompt: string | AsyncIterable<...>; options?: Options }) => Query
//    A QueryFn-typed function must be assignable to any callable that accepts
//    our narrower prompt type.
// ---------------------------------------------------------------------------

describe("QueryFn type is compatible with SDK query() shape", () => {
  it("QueryFn accepts { prompt: string, options?: Options } and returns Query", () => {
    // Create a mock implementation that satisfies QueryFn
    async function* fakeGen(): AsyncGenerator<SDKMessage, void> {
      // yields nothing
    }
    const fakeQuery: QueryFn = (_params) => fakeGen() as unknown as Query;
    const result = fakeQuery({ prompt: "hello" });
    // result must be assignable to Query (AsyncGenerator<SDKMessage, void>)
    const _q: Query = result;
    expect(_q).toBeDefined();
  });

  it("QueryFn passes options through correctly", () => {
    let capturedOptions: Options | undefined;
    const fn: QueryFn = (params) => {
      capturedOptions = params.options;
      return {} as unknown as Query;
    };
    fn({ prompt: "test", options: { cwd: "/tmp", maxTurns: 5 } });
    expect(capturedOptions?.cwd).toBe("/tmp");
    expect(capturedOptions?.maxTurns).toBe(5);
  });
});

// ---------------------------------------------------------------------------
// 9. SDKMessage has .type field
//    classifyMessage() branches on msg.type. Verify .type is present on the
//    SDKMessage union discriminant.
// ---------------------------------------------------------------------------

describe("SDKMessage has .type discriminant field", () => {
  it("type field is accessible on SDKMessage", () => {
    // Compile-time check: "result" must be assignable to SDKMessage["type"]
    const _type: SDKMessage["type"] = "result";
    expect(_type).toBe("result");
  });

  it("SDKResultSuccess.type is 'result'", () => {
    const _type: SDKResultSuccess["type"] = "result";
    expect(_type).toBe("result");
  });

  it("SDKResultError.type is 'result'", () => {
    const _type: SDKResultError["type"] = "result";
    expect(_type).toBe("result");
  });
});

// ---------------------------------------------------------------------------
// 10. SessionResult fields map correctly from SDK types
//     parseResult() constructs SessionResult from SDKResultMessage fields.
//     Verify the mapping produces the correct types at runtime using real
//     SDKResultMessage-shaped objects (no `any` in construction).
// ---------------------------------------------------------------------------

describe("SessionResult fields map correctly from SDK types", () => {
  const successMsg: SDKResultSuccess = {
    type: "result",
    subtype: "success",
    session_id: "sess-001",
    total_cost_usd: 0.12,
    num_turns: 4,
    usage: { input_tokens: 800, output_tokens: 300, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
    result: "Done",
    terminal_reason: "completed",
    duration_ms: 3000,
    duration_api_ms: 2500,
    is_error: false,
    stop_reason: "end_turn",
    modelUsage: {},
    permission_denials: [],
    uuid: "uuid-success-1" as SDKResultSuccess["uuid"],
  };

  const errorMsg: SDKResultError = {
    type: "result",
    subtype: "error_during_execution",
    session_id: "sess-002",
    total_cost_usd: 0.03,
    num_turns: 1,
    usage: { input_tokens: 200, output_tokens: 50, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
    errors: ["Tool execution failed"],
    terminal_reason: "model_error",
    duration_ms: 1000,
    duration_api_ms: 800,
    is_error: true,
    stop_reason: null,
    modelUsage: {},
    permission_denials: [],
    uuid: "uuid-error-1" as SDKResultError["uuid"],
  };

  it("maps session_id -> sessionId", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.sessionId).toBe("sess-001");
  });

  it("maps success subtype -> success: true", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.success).toBe(true);
  });

  it("maps error subtype -> success: false", () => {
    const result: SessionResult = parseResult(errorMsg);
    expect(result.success).toBe(false);
  });

  it("maps result field from SDKResultSuccess", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.result).toBe("Done");
  });

  it("maps errors field from SDKResultError", () => {
    const result: SessionResult = parseResult(errorMsg);
    expect(result.errors).toContain("Tool execution failed");
  });

  it("maps total_cost_usd -> totalCostUsd", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.totalCostUsd).toBe(0.12);
  });

  it("maps num_turns -> numTurns", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.numTurns).toBe(4);
  });

  it("maps usage.input_tokens correctly", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.usage.input_tokens).toBe(800);
  });

  it("maps usage.output_tokens correctly", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.usage.output_tokens).toBe(300);
  });

  it("maps terminal_reason from success message", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.terminalReason).toBe("completed");
  });

  it("maps terminal_reason from error message", () => {
    const result: SessionResult = parseResult(errorMsg);
    expect(result.terminalReason).toBe("model_error");
  });

  it("success result has empty errors array", () => {
    const result: SessionResult = parseResult(successMsg);
    expect(result.errors).toHaveLength(0);
  });

  it("SDKResultMessage union includes both success and error", () => {
    // Both should be assignable to SDKResultMessage without cast
    const _success: SDKResultMessage = successMsg;
    const _error: SDKResultMessage = errorMsg;
    expect(_success.type).toBe("result");
    expect(_error.type).toBe("result");
  });
});
