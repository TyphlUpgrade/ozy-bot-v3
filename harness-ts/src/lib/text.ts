/**
 * Shared text-handling helpers — ID sanitization (O4 path-traversal defense),
 * markdown/mention neutralization for Discord echoes, secret redaction for
 * prompt previews. Canonical source; Discord modules and orchestrator ingest
 * both import from here.
 */

const SAFE_ID_RE = /^[a-zA-Z0-9_-]+$/;
const MAX_ID_LEN = 128;

/** Return the id unchanged if it's safe for use in file paths; null otherwise. */
export function sanitizeTaskId(raw: string): string | null {
  if (!SAFE_ID_RE.test(raw)) return null;
  if (raw.length > MAX_ID_LEN) return null;
  return raw;
}

// --- Sanitization for Discord echoes ---

const MAX_FIELD_LEN = 500;

const SECRET_PATTERNS: readonly RegExp[] = [
  /sk-[a-zA-Z0-9_-]{20,}/g,
  /AKIA[0-9A-Z]{16}/g,
  /ghp_[A-Za-z0-9]{30,}/g,
  /xox[baprs]-[A-Za-z0-9-]{10,}/g,
  /(?:[A-Za-z0-9+/]{40,}=*)/g,
];

export function redactSecrets(raw: string): string {
  let out = raw;
  for (const p of SECRET_PATTERNS) out = out.replace(p, "[REDACTED]");
  return out;
}

/**
 * Neutralize Discord-meaningful sequences in untrusted text so it cannot ping
 * everyone, break out of the surrounding code span, or inject arbitrary
 * markdown. Keeps output human-readable.
 */
export function sanitize(raw: string, maxLen: number = MAX_FIELD_LEN): string {
  const stripped = raw
    .replace(/@(everyone|here)/g, "@​$1") // zero-width joiner neutralizes mention
    .replace(/`/g, "\\`");                 // don't escape the surrounding code span
  if (stripped.length <= maxLen) return stripped;
  return `${stripped.slice(0, maxLen)}…`;
}

const RATIONALE_MAX_LEN = 1024;
// ESC + CSI / OSC + control chars we never want embedded in operator-facing
// reasons. Keeps \n and \t — those are legitimately useful in multi-line text.
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

/**
 * Cap + strip hostile characters from an Architect-supplied rationale before
 * embedding in task.lastError / task_failed.reason / project_failed.reason.
 * The Architect is trusted, but its output can still contain model-generated
 * control chars or ANSI escapes that would corrupt Discord / TTY output.
 */
export function truncateRationale(raw: string, maxLen: number = RATIONALE_MAX_LEN): string {
  const stripped = raw.replace(CONTROL_RE, "");
  if (stripped.length <= maxLen) return stripped;
  return `${stripped.slice(0, maxLen)}…`;
}
