// TESTS-ONLY — production callers use defaultCtx() from epistle-templates.ts (wall-clock).
// Importing this file from src/** is forbidden (ESLint rule deferred — no eslint config in
// harness-ts at iter-4).

import type { EpistleContext } from "../../../src/discord/epistle-templates.js";

export const FIXED_EPISTLE_TIMESTAMP = "2026-04-26T20:00:00.000Z";
export const frozenCtx = (): EpistleContext => ({ timestamp: FIXED_EPISTLE_TIMESTAMP });
