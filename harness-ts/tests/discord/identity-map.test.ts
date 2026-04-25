import { describe, it, expect } from "vitest";
import { buildIdentityMap } from "../../src/discord/identity-map.js";
import type { DiscordConfig } from "../../src/lib/config.js";

function configWithAgents(agents: DiscordConfig["agents"]): Pick<DiscordConfig, "agents"> {
  return { agents };
}

describe("buildIdentityMap", () => {
  it("looks up agent key by username (case-insensitive)", () => {
    const map = buildIdentityMap(
      configWithAgents({
        architect: { name: "Architect", avatar_url: "" },
        reviewer: { name: "Reviewer", avatar_url: "" },
      }),
    );
    expect(map.lookup("Architect")).toBe("architect");
    expect(map.lookup("ARCHITECT")).toBe("architect");
    expect(map.lookup("architect")).toBe("architect");
    expect(map.lookup("Reviewer")).toBe("reviewer");
  });

  it("returns null on miss", () => {
    const map = buildIdentityMap(
      configWithAgents({
        architect: { name: "Architect", avatar_url: "" },
      }),
    );
    expect(map.lookup("Unknown")).toBeNull();
    expect(map.lookup("")).toBeNull();
  });

  it("throws on duplicate (lowercased) usernames", () => {
    expect(() =>
      buildIdentityMap(
        configWithAgents({
          architect: { name: "Bot", avatar_url: "" },
          reviewer: { name: "BOT", avatar_url: "" },
        }),
      ),
    ).toThrow(/duplicate username/i);
  });

  it("returns an empty map when agents section is empty", () => {
    const map = buildIdentityMap(configWithAgents({}));
    expect(map.entries.size).toBe(0);
    expect(map.lookup("anyone")).toBeNull();
  });

  it("trims whitespace before comparing usernames", () => {
    const map = buildIdentityMap(
      configWithAgents({
        architect: { name: "  Architect  ", avatar_url: "" },
      }),
    );
    expect(map.lookup("Architect")).toBe("architect");
    expect(map.lookup("  architect  ")).toBe("architect");
  });

  it("is idempotent — building twice from the same config produces equivalent maps", () => {
    const cfg = configWithAgents({
      architect: { name: "Architect", avatar_url: "" },
      reviewer: { name: "Reviewer", avatar_url: "" },
      executor: { name: "Executor", avatar_url: "" },
    });
    const a = buildIdentityMap(cfg);
    const b = buildIdentityMap(cfg);
    expect(a.entries.size).toBe(b.entries.size);
    expect(a.lookup("Architect")).toBe(b.lookup("Architect"));
    expect(a.lookup("Reviewer")).toBe(b.lookup("Reviewer"));
    expect(a.lookup("Executor")).toBe(b.lookup("Executor"));
  });
});
