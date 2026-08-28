Create a Circle of Fifths application for guitar players that helps them
understand music theory.

Constraints (infrastructure only — every design decision is yours):
- TypeScript on Bun, no frameworks beyond what `bun build --target=browser` bundles.
- Grow the app in place at the paths declared in `app.manifest.yaml`:
  rooted at `apps/app/`, module graph reachable from `apps/app/main.ts`,
  durable tests in `apps/app/app.test.ts` (runnable by `bun test`).

What the app does, what it teaches, how it looks, and how it is structured
are entirely up to you. Design it, build it, test it, and deliver it.
