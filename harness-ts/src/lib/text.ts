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
