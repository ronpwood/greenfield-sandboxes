// The fixed suite. Starts as a sanity check plus a RENDER SMOKE so the quality
// gates have a green baseline on the empty shell; every ADW that adds behavior
// is expected to grow durable tests for it here (generated TDD suites live
// separately in tests/generated/ and are red by design until built).
//
// ── The render smoke, and your obligation to it ──────────────────────────────
// `bun test` has no DOM, so main.ts's module-scope render is skipped unless
// something provides `document`. The stub below is that something, and it is
// why "renders into #app on import" executes the REAL render path instead of
// just proving the file parses. The typecheck gate catches undeclared names and
// wrong types; it cannot catch code that type-checks and then throws while
// drawing. This test is what catches that.
//
// It only exercises what it implements. When the UI grows — createElement,
// SVG, appendChild, addEventListener — EXTEND THIS STUB so the render path
// keeps running under test. Do not delete this test, and do not weaken it into
// a tautology, to make a suite go green: a vacuous smoke test is worse than
// none, because it reports the crash it is no longer looking for as a pass.
//
// The stub is installed at module scope, before any import of ./main.ts, on
// purpose: a module body runs once on first import, so the first import wins.
// Keep every import of ./main.ts dynamic (`await import`) and below this point.

import { describe, expect, test } from "bun:test";

const appEl = { textContent: "" };

(globalThis as any).document = {
  getElementById: (id: string) => (id === "app" ? appEl : null),
};

describe("shell", () => {
  test("module graph loads", async () => {
    const mod = await import("./main.ts");
    expect(typeof mod.appRoot).toBe("function");
  });

  test("renders into #app on import", async () => {
    await import("./main.ts");
    // main.ts writes to #app at module scope. Empty means the render path
    // never ran — either the stub no longer satisfies it, or rendering broke.
    expect(appEl.textContent).not.toBe("");
  });
});
