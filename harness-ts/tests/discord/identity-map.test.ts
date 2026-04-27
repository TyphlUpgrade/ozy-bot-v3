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

// Wave E-δ MR3 — IdentityMap.lookupRole.
describe("IdentityMap.lookupRole (Wave E-δ MR3)", () => {
  const map = buildIdentityMap(
    configWithAgents({
      architect: { name: "Architect", avatar_url: "" },
    }),
  );

  it("returns the matching IdentityRole for each of the four role literals", () => {
    expect(map.lookupRole("architect")).toBe("architect");
    expect(map.lookupRole("reviewer")).toBe("reviewer");
    expect(map.lookupRole("executor")).toBe("executor");
    expect(map.lookupRole("orchestrator")).toBe("orchestrator");
  });

  it("is case-insensitive", () => {
    expect(map.lookupRole("Architect")).toBe("architect");
    expect(map.lookupRole("REVIEWER")).toBe("reviewer");
    expect(map.lookupRole("eXeCuToR")).toBe("executor");
  });

  it("trims whitespace before comparing", () => {
    expect(map.lookupRole("  architect  ")).toBe("architect");
  });

  it("returns null for unknown / mistyped names", () => {
    expect(map.lookupRole("reviewr")).toBeNull(); // common typo
    expect(map.lookupRole("operator")).toBeNull(); // not a role per IdentityRole
    expect(map.lookupRole("")).toBeNull();
    expect(map.lookupRole("random")).toBeNull();
  });

  it("does not consult DiscordConfig.agents — the agent KEY itself is the role", () => {
    // Even with a custom-named agent, lookupRole still maps from the role
    // literal itself, not the agent's display name.
    const m = buildIdentityMap(
      configWithAgents({
        architect: { name: "GandalfTheGrey", avatar_url: "" },
      }),
    );
    expect(m.lookupRole("architect")).toBe("architect");
    expect(m.lookupRole("GandalfTheGrey")).toBeNull();
  });
});
