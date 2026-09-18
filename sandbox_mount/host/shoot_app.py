#!/usr/bin/env python3
"""Screenshot a sandbox's live app and report its console errors.

WHY THIS IS HOST-SIDE. The agents on the VM read source, not pixels, so nothing
in the chain can tell you what the app LOOKS like — or, until 2026-09-18,
whether it renders at all. `happy-dom` now catches render crashes inside the fix
loop, so this is no longer the crash detector; it is the record of what was
actually delivered, captured before teardown destroys the VM.

It exists because judging a fan-out from source is judging the wrong artifact.
On 2026-09-18 two arms had green tests, zero type errors and a reviewer's
sign-off on most requirements, and threw a TypeError on page load. One look at
the page said more than either envelope did.

Requires playwright on the HOST (never on the VM — that is the expensive path
this deliberately avoids):  uv run --with playwright shoot_app.py ...
and a one-time `playwright install chromium`.

Usage:
    shoot_app.py <url> <out.png> [--full] [--json]

Exit 0 whether or not the page has console errors — this is an OBSERVATION, not
a gate. It prints them, and `--json` makes them machine-readable, so a judge can
record "shipped a crash" as a fact. A non-zero exit is reserved for "I could not
look at all".
"""

from __future__ import annotations

import json
import sys


def shoot(url: str, out: str, full: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("shoot_app: playwright is not installed on this host.", file=sys.stderr)
        print("  uv run --with playwright sandbox_mount/host/shoot_app.py ...", file=sys.stderr)
        print("  then once: uv run --with playwright playwright install chromium", file=sys.stderr)
        raise SystemExit(2)

    console: list[dict] = []
    pageerrors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # Both channels matter and they catch different things. `pageerror` is an
        # uncaught exception that reached the window; `console` is what something
        # logged. WHICH ONE FIRES DEPENDS ON THE SERVER: measured 2026-09-18, a
        # module-scope TypeError under Bun's dev server arrives on CONSOLE, because
        # Bun's HMR client catches it and logs it — `pageerror` stays empty. Watch
        # both, and never treat an empty `page_errors` as proof the app is healthy.
        page.on("console", lambda m: console.append({"type": m.type, "text": m.text})
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: pageerrors.append(str(e)))

        page.goto(url, wait_until="networkidle", timeout=60_000)
        # Module-scope render happens after load; give it a beat before judging.
        page.wait_for_timeout(1500)
        title = page.title()
        # Did anything actually render into the page?
        body_len = page.evaluate("document.body.innerText.trim().length")
        page.screenshot(path=out, full_page=full)
        browser.close()

    errors = [c for c in console if c["type"] == "error"]
    return {
        "url": url,
        "screenshot": out,
        "title": title,
        "body_text_chars": body_len,
        "console_errors": errors,
        "console_warnings": [c for c in console if c["type"] == "warning"],
        "page_errors": pageerrors,
        "clean": not errors and not pageerrors,
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__.strip().split("Usage:")[-1].strip(), file=sys.stderr)
        return 2
    url, out = args
    result = shoot(url, out, full="--full" in argv)

    if "--json" in argv:
        print(json.dumps(result, indent=2))
        return 0

    print(f"   title   {result['title']}")
    print(f"   render  {result['body_text_chars']} chars of visible text")
    print(f"   shot    {result['screenshot']}")
    # The most robust signal in this whole script. A rendered app has text; zero
    # means the entry never got far enough to write any, whatever the error
    # channels say. It is the check that does not depend on how the dev server
    # chooses to report an exception.
    if result["body_text_chars"] == 0:
        print("   !! NOTHING RENDERED — 0 chars of visible text; the entry did not complete")
    if result["clean"]:
        print("   console CLEAN — no errors, no uncaught exceptions")
    else:
        for e in result["page_errors"]:
            print(f"   !! uncaught: {e.splitlines()[0][:140]}")
        for e in result["console_errors"]:
            print(f"   !! console:  {e['text'].splitlines()[0][:140]}")
        print("   (observation, not a gate — exit stays 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
