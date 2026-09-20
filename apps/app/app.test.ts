// The fixed suite. Starts as a sanity check plus a RENDER SMOKE so the quality
// gates have a green baseline on the empty shell; every ADW that adds behavior
// is expected to grow durable tests for it here (generated TDD suites live
// separately in tests/generated/ and are red by design until built).
//
// ── The DOM lives in ./test-dom.ts, and so does the reason ──────────────────
// This file used to install happy-dom itself. It no longer does, because the
// command that grades a build runs this file and a generated suite in ONE
// process with ONE module registry:
//
//     bun test apps/app/app.test.ts apps/app/tests/generated/<id>.test.ts
//
// A generated suite that copies a visible `new Window(...)` /
// `Object.defineProperty(globalThis, "document", ...)` out of this file dies at
// module load when the two run together, and a bare `import("./main.ts")` in
// the second file is a cache hit that renders nothing. Both traps are invisible
// to a suite that passes alone. `./test-dom.ts` installs the DOM exactly once
// and hands out fresh app instances through `loadApp()`; read its header before
// writing tests, and import it from generated suites as `../../test-dom`.
//
// It replaced a hand-rolled DOM stub, which caused two shipped browser crashes
// on 2026-09-18. A hand-rolled double is an attractive nuisance: it looks like a
// DOM, so agents code against it — including against its internals, which do not
// exist in a browser. One arm called `el.classNameSet.add(...)` (an
// implementation detail of the old stub); another ran
// `Object.defineProperty(globalThis, "document", ...)` in main.ts to cooperate
// with it. Both passed `bun test` and both threw on page load.
//
// happy-dom removes that whole class of bug, because THE TEST FAILS THE SAME WAY
// THE BROWSER DOES. Verified against both crashed arms: their real main.ts files
// fail here with the exact errors the browser reported.
//
// TWO RULES, and they are the point of this file:
//   1. NEVER reference test-only identifiers from production code. If a property
//      is not on the real DOM, it is not yours to call. `document`, `Element`
//      and friends behave here as they do in a browser — use them.
//   2. NEVER weaken this file, or ./test-dom.ts, to make a suite pass. Extend
//      it, or fix the code. A render test that cannot fail is worse than none,
//      because it reports the crash it has stopped looking for as a pass.

import { describe, expect, test } from "bun:test";
import { loadApp } from "./test-dom.ts";

describe("shell", () => {
  test("module graph loads", async () => {
    const mod = await loadApp();
    expect(typeof mod.appRoot).toBe("function");
  });

  test("renders into #app on import", async () => {
    await loadApp();
    // main.ts writes to #app at module scope. Empty means the render path never
    // ran — either the entry stopped rendering, or rendering threw.
    const app = document.getElementById("app");
    expect(app).not.toBeNull();
    expect(app!.textContent).not.toBe("");
  });
});
