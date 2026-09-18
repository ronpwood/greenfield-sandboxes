// The fixed suite. Starts as a sanity check plus a RENDER SMOKE so the quality
// gates have a green baseline on the empty shell; every ADW that adds behavior
// is expected to grow durable tests for it here (generated TDD suites live
// separately in tests/generated/ and are red by design until built).
//
// ── Why this file uses happy-dom, and what that buys you ─────────────────────
// `bun test` has no DOM, so main.ts's module-scope render would never execute
// under test. This file installs a REAL DOM implementation (happy-dom) before
// the first import of ./main.ts, so "renders into #app on import" exercises the
// actual render path.
//
// It deliberately replaced a hand-rolled stub, which caused two shipped browser
// crashes on 2026-09-18. A hand-rolled double is an attractive nuisance: it
// looks like a DOM, so agents code against it — including against its internals,
// which do not exist in a browser. One arm called `el.classNameSet.add(...)` (an
// implementation detail of the old stub); another ran
// `Object.defineProperty(globalThis, "document", ...)` in main.ts to cooperate
// with it. Both passed `bun test` and both threw on page load.
//
// happy-dom removes that whole class of bug, because THE TEST FAILS THE SAME WAY
// THE BROWSER DOES. Verified against both crashed arms: their real main.ts files
// now fail here with the exact errors the browser reported.
//
// TWO RULES, and they are the point of this file:
//   1. NEVER reference test-only identifiers from production code. If a property
//      is not on the real DOM, it is not yours to call. `document`, `Element`
//      and friends behave here as they do in a browser — use them.
//   2. NEVER weaken this file to make a suite pass. Extend it, or fix the code.
//      A render test that cannot fail is worse than none, because it reports the
//      crash it has stopped looking for as a pass.

import { describe, expect, test } from "bun:test";
import { Window } from "happy-dom";

const win = new Window({ url: "http://localhost/" });

// `configurable: false` is load-bearing and matches a real browser, where
// `window.document` cannot be redefined. Without it, production code that calls
// Object.defineProperty(globalThis, "document", ...) would pass here and throw
// on page load — which is exactly how one arm shipped a crash.
Object.defineProperty(globalThis, "document", {
  value: win.document,
  configurable: false,
  writable: false,
  enumerable: true,
});
(globalThis as unknown as { window: unknown }).window = win;

document.body.innerHTML = `<div id="app"></div>`;

describe("shell", () => {
  test("module graph loads", async () => {
    const mod = await import("./main.ts");
    expect(typeof mod.appRoot).toBe("function");
  });

  test("renders into #app on import", async () => {
    await import("./main.ts");
    // main.ts writes to #app at module scope. Empty means the render path never
    // ran — either the entry stopped rendering, or rendering threw.
    const app = document.getElementById("app");
    expect(app).not.toBeNull();
    expect(app!.textContent).not.toBe("");
  });
});
