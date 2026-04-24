You are working inside a harness-managed git worktree.

When you finish your task, you MUST:
1. Commit your changes with a short message.
2. Create directory `.harness/` if missing.
3. Write `.harness/completion.json` with this JSON shape (all fields required):

```
{
  "status": "success" | "failure",
  "commitSha": "<full sha of your final commit>",
  "summary": "<one sentence>",
  "filesChanged": ["path1", "path2"],
  "understanding": "<one-paragraph restatement of the task as you interpreted it>",
  "assumptions": ["<assumption 1>", "<assumption 2>"],
  "nonGoals": ["<thing you deliberately did not do 1>", "<thing 2>"],
  "confidence": {
    "scopeClarity": "clear" | "partial" | "unclear",
    "designCertainty": "obvious" | "alternatives_exist" | "guessing",
    "testCoverage": "verifiable" | "partial" | "untestable",
    "assumptions": [
      { "description": "<same as top-level assumption>", "impact": "high" | "low", "reversible": true | false }
    ],
    "openQuestions": ["<question the operator may need to answer>"]
  }
}
```

All enrichment fields are required. Be honest about uncertainty: if the scope is not fully clear, say so; if you are guessing on design, say so. Do not fabricate certainty.

The completion file is how the orchestrator knows you are done. If you do not write it, the task will be marked failed.
