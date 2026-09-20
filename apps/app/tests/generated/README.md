# tests/generated/

The TDD phase writes exactly one file here — `<adw_id>.test.ts` — and it is red
by design until the build satisfies it. The fixed suite next door
(`apps/app/app.test.ts`) is not yours to edit.

## Get your DOM and your app from `../../test-dom`

```ts
import { describe, expect, test } from "bun:test";
import { loadApp, win } from "../../test-dom";

test("the wheel has twelve segments", async () => {
  await loadApp();                     // fresh #app, fresh render of the entry
  expect(document.querySelectorAll(".segment").length).toBe(12);
});
```

Do **not** install happy-dom yourself, and do **not** `import("../../main.ts")`
directly. Your suite runs in the SAME process as the fixed suite — that is the
command that grades the build:

    bun test apps/app/app.test.ts apps/app/tests/generated/<id>.test.ts

`bun test` shares one module registry across those files (no `--isolate`), so a
second `Object.defineProperty(globalThis, "document", ...)` throws at module
load and none of your tests run, and a bare `import` of the entry is a cache hit
that renders nothing. Both traps are invisible when your file runs alone.
`test-dom.ts` exists so you never meet them — read its header.

## Verify with the command that will grade you

    bun test apps/app/app.test.ts apps/app/tests/generated/<id>.test.ts

Your tests must FAIL, and the fixed suite's must still pass. Judging by your
file alone is what the gate no longer accepts.
