/**
 * Message accumulator — debounces rapid NL messages so a split operator
 * thought arrives as a single coherent payload.
 *
 * Rules:
 *   - Messages from the same (userId, channelId) within `debounceMs` are
 *     concatenated (space-separated). Each new push resets the timer.
 *   - Messages beginning with `!` are commands: they bypass the accumulator.
 *     Before the command is flushed, any pending NL payload from the same
 *     (userId, channelId) is flushed first so the command is not ordered
 *     behind a stale half-thought.
 *   - Different users and different channels are tracked independently.
 *   - `flushAll()` drains every pending key immediately (used on shutdown).
 *
 * Concurrency notes (single event loop):
 *   - The Map value stores `{userId, channelId, text, timer}`; `flushAll`
 *     iterates values directly so it never has to re-parse a composite key.
 *   - Each timer callback re-checks the Map for its own `timer` reference
 *     before invoking `onFlush`. If `flushAll` fired first (or the entry was
 *     re-scheduled with a new timer), the stale timer's callback exits
 *     without double-invoking `onFlush`.
 */

export type AccumulatorFlush = (userId: string, channelId: string, text: string) => void;

export interface MessageAccumulatorOptions {
  /** Debounce window. Defaults to 2000ms; override via config.discord.accumulator_debounce_ms. */
  debounceMs?: number;
}

interface PendingEntry {
  userId: string;
  channelId: string;
  text: string;
  timer: ReturnType<typeof setTimeout>;
}

const DEFAULT_DEBOUNCE_MS = 2000;
// Non-printable ASCII record separator — keeps key safe from any realistic userId/channelId content.
const KEY_SEP = "\x1f";

function pendingKey(userId: string, channelId: string): string {
  return `${userId}${KEY_SEP}${channelId}`;
}

export class MessageAccumulator {
  private readonly debounceMs: number;
  private readonly onFlush: AccumulatorFlush;
  private readonly pending: Map<string, PendingEntry> = new Map();

  constructor(onFlush: AccumulatorFlush, options: MessageAccumulatorOptions = {}) {
    this.debounceMs = options.debounceMs ?? DEFAULT_DEBOUNCE_MS;
    this.onFlush = onFlush;
  }

  push(userId: string, channelId: string, text: string): void {
    if (text.startsWith("!")) {
      this.flushKey(userId, channelId);
      this.onFlush(userId, channelId, text);
      return;
    }

    const key = pendingKey(userId, channelId);
    const existing = this.pending.get(key);
    const combined = existing ? `${existing.text} ${text}` : text;
    if (existing) clearTimeout(existing.timer);

    // Placeholder; real timer set below so entry.timer === timer holds.
    const entry: PendingEntry = { userId, channelId, text: combined, timer: undefined as unknown as ReturnType<typeof setTimeout> };
    const timer = setTimeout(() => {
      // Stale-fire guard: another call may have re-scheduled or flushed first.
      const current = this.pending.get(key);
      if (!current || current.timer !== timer) return;
      this.pending.delete(key);
      this.onFlush(current.userId, current.channelId, current.text);
    }, this.debounceMs);
    entry.timer = timer;
    this.pending.set(key, entry);
  }

  private flushKey(userId: string, channelId: string): void {
    const key = pendingKey(userId, channelId);
    const entry = this.pending.get(key);
    if (!entry) return;
    clearTimeout(entry.timer);
    this.pending.delete(key);
    this.onFlush(entry.userId, entry.channelId, entry.text);
  }

  /** Drain all pending entries (used on shutdown). Iterates values directly so keys are never reparsed. */
  flushAll(): void {
    const entries = [...this.pending.values()];
    this.pending.clear();
    for (const entry of entries) {
      clearTimeout(entry.timer);
      this.onFlush(entry.userId, entry.channelId, entry.text);
    }
  }

  /** Number of pending (debounced) entries — diagnostics only. */
  get pendingCount(): number {
    return this.pending.size;
  }
}
