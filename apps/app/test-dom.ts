// The shared test DOM. BOTH the fixed suite and EVERY generated suite import
// this — never `happy-dom` directly, and never a bare `import` of the entry.
//
// ── Why this file exists ────────────────────────────────────────────────────
// `quality.tests()` grades a build with ONE command, and it is one process:
//
//     bun test apps/app/app.test.ts apps/app/tests/generated/<id>.test.ts
//
// `bun test` shares a single module registry across the files on its argv — it
// isolates only under `--isolate`, which that command does not pass. Two
// consequences follow, and on 2026-09-20 one arm spent 1.14M tokens and 34 of
// its 35 tool calls rediscovering them instead of building the app:
//
//   1. A SECOND `Object.defineProperty(globalThis, "document", ...)` throws
//      "Attempting to change value of a readonly property" AT MODULE LOAD, so
//      not one test in that file runs. A suite that is green alone dies here.
//   2. `await import("./main.ts")` from the second file is a CACHE HIT. The
//      entry's module-scope render does not run again, `#app` stays empty, and
//      the test fails for a reason that has nothing to do with the code.
//
// Neither is discoverable from a suite that passes on its own, and neither is
// the builder's to fix: `app.test.ts` is not its file to edit. So the shell
// ships the answer instead of making every run find it.
//
// ── What a generated suite writes ───────────────────────────────────────────
//     import { loadApp, win } from "../../test-dom";   // from tests/generated/
//
//     test("the wheel has twelve segments", async () => {
//       await loadApp();                               // fresh #app, fresh render
//       expect(document.querySelectorAll(".segment").length).toBe(12);
//     });
//
// Call `loadApp()` in each test that needs a rendered app. It is cheap, and it
// is the only correct way to get one.
//
// ── TWO RULES, unchanged, and they are the point ────────────────────────────
//   1. NEVER reference test-only identifiers from production code. Everything
//      installed below exists in a real browser under the same name; anything
//      NOT on the real DOM is not yours to call. That rule is what keeps this
//      file honest — a double you can code against is worse than no double,
//      because it reports the crash it stopped looking for as a pass.
//   2. NEVER weaken this file, or `app.test.ts`, to make a suite pass. Extend
//      it, or fix the code. `configurable: false` on `document` is
//      load-bearing: it matches a real browser, where `window.document` cannot
//      be redefined, and without it production code that redefines `document`
//      passes here and throws on page load — exactly how one arm shipped a
//      crash on 2026-09-18. A suite that thinks it needs its own DOM needs
//      `loadApp()`.

import { Window } from "happy-dom";

export const win = new Window({ url: "http://localhost/" });

Object.defineProperty(globalThis, "document", {
  value: win.document,
  configurable: false,
  writable: false,
  enumerable: true,
});
(globalThis as unknown as { window: unknown }).window = win;

// The names a browser puts on the global scope. Without them a suite that
// dispatches a real event — the only way to click an SVG node, which has no
// `.click()` — fails with "MouseEvent is not defined", and its author invents
// a workaround for a problem the browser does not have. Installing them is
// FIDELITY, not convenience: every name here is a real browser global, so
// Rule 1 gets stricter, not looser.
//
// Assignment is guarded because bun defines some of these itself, and a global
// it owns is already the right one.
for (const name of [
  "Event", "CustomEvent", "MouseEvent", "KeyboardEvent", "PointerEvent",
  "Node", "Element", "HTMLElement", "SVGElement", "DocumentFragment",
  "HTMLInputElement", "HTMLSelectElement", "HTMLButtonElement",
  "getComputedStyle", "requestAnimationFrame", "cancelAnimationFrame",
  "localStorage", "sessionStorage", "navigator", "location", "history",
  "matchMedia", "ResizeObserver", "IntersectionObserver",
]) {
  const value = (win as unknown as Record<string, unknown>)[name];
  if (value === undefined) continue;
  try {
    Object.defineProperty(globalThis, name, {
      value, configurable: true, writable: true, enumerable: false,
    });
  } catch {
    // A global bun owns and will not surrender. Its own is fine.
  }
}

/** A fresh, empty `#app` — the same container `index.html` serves. */
export function resetDom(): void {
  win.document.body.innerHTML = `<div id="app"></div>`;
}

let instance = 0;

/**
 * Reset `#app`, then evaluate the entry as a FRESH module, and return its exports.
 *
 * The query suffix is what makes it fresh. Bun keys its module registry on the
 * resolved specifier, so `./main.ts?instance=2` is a different module from
 * `./main.ts?instance=1` and its module-scope render runs again. A bare
 * `import("./main.ts")` in the second test file of one `bun test` command is a
 * cache hit that renders nothing — trap (2) above.
 *
 * A fresh instance also resets whatever module-level state the app keeps, so
 * tests do not leak into each other.
 */
export async function loadApp(): Promise<typeof import("./main.ts")> {
  resetDom();
  return (await import(`./main.ts?instance=${++instance}`)) as typeof import("./main.ts");
}

resetDom();
