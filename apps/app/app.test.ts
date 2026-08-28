// The fixed suite. Starts as a single sanity check so the quality gates have a
// green baseline on the empty shell; every ADW that adds behavior is expected
// to grow durable tests for it here (generated TDD suites live separately in
// tests/generated/ and are red by design until built).

import { describe, expect, test } from "bun:test";

describe("shell", () => {
  test("module graph loads", async () => {
    const mod = await import("./main.ts");
    expect(typeof mod.appRoot).toBe("function");
  });
});
