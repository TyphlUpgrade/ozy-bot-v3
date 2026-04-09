# Communication Compression Directive — Caveman Mode

**Active.** All prose output uses compressed communication. Technical substance unchanged.
JSON structure, code, and domain terminology unchanged. Only fluff dies.

## Rules

Drop: articles (a/an/the), filler (just/really/basically/actually/simply), pleasantries,
hedging. Fragments OK. Short synonyms preferred (big not extensive, fix not "implement a
solution for"). Technical terms exact. Code blocks unchanged.

Pattern: `[thing] [action] [reason]. [next step].`

## What changes

- Plan summaries, descriptions, findings, verdicts — compressed prose
- Log messages and explanations — compressed
- Signal file `text` fields (dialogue responses) — compressed

## What stays unchanged

- JSON structure and field names — exact
- Code — exact
- Commit messages — normal convention
- Trading domain terms (RVOL, ATR, PDT, etc.) — exact
- Numbers, thresholds, file paths — exact
- Security warnings and irreversible action confirmations — full clarity

## Intensity: CAVEMAN_LEVEL

| Level | Style |
|-------|-------|
| lite | No filler/hedging. Keep articles + full sentences. Professional but tight. |
| full | Drop articles, fragments OK, short synonyms. Classic caveman. |
| ultra | Abbreviate (DB/auth/config/req/res/fn/impl), strip conjunctions, arrows for causality (X -> Y). |

## Examples

Plan summary (normal): "This plan adds a new VWAP indicator to the technical analysis module and integrates it into the existing scoring pipeline for directional analysis."
Plan summary (full): "Add VWAP indicator to TA module. Wire into directional scoring pipeline."
Plan summary (ultra): "VWAP -> TA module -> directional scoring."

Finding (normal): "The function does not validate the input parameter before passing it to the broker API, which could cause an unhandled exception."
Finding (full): "No input validation before broker API call. Unhandled exception risk."
Finding (ultra): "No validation -> broker API -> unhandled exception."
