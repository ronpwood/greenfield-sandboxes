#!/usr/bin/env -S uv run
# /// script
# dependencies = ["playwright"]
# ///
"""Load the app in a real browser, drive it, and fail on what only a browser can see.

WHY THIS EXISTS, AND WHAT IT DOES NOT DO.

`happy-dom` (in the fixed suite) closed the *crash-on-load* class in 2026-09-18's
fix loop, and it was the right call: a real DOM fails the way a browser does. But
happy-dom does no **layout** and no **hit-testing** — `getBoundingClientRect`
returns zeros and nothing occludes anything. Fan-out 3 shipped six arms with lint,
typecheck and every test green, and two of them did not work:

  gf3-5  One SVG large-arc-flag (`1` on a 30 degree wedge) made one slice sweep
         ~330 degrees and paint over the entire ring. All 12 key slices carry
         `cursor: pointer`; eleven of them became completely unreachable. THIS
         SCRIPT CATCHES THAT (assertion C, verified against the harvested tree).

  gf3-6  Its wheel labels sit above the sectors with no `pointer-events: none`
         and no handler of their own, so every click is swallowed. THIS SCRIPT
         DOES NOT CATCH THAT, and the reason is worth writing down: those sectors
         carry no `cursor: pointer`, no role, no handler — from a machine's view
         there is no control there, only a drawing. The wheel is not broken, it is
         ABSENT. A gate cannot detect a missing feature; that is what the rubric
         and the reviewer are for. Do not "fix" this by widening the interactive
         heuristic to every SVG path — that trades a real signal for noise.

So the honest scope is five things nothing else in the chain checks:
  A  the REAL bundle loads in a REAL browser with no uncaught error
  B  it renders something
  C  no interactive element is completely unreachable (occlusion)
  D  clicking the controls throws nothing and does not blank the page
  E  a ring of pie/annular sectors is not drawn the long way round

Assertion E was added after `fixval-20260919-250a64` PASSED this gate and
shipped a twelve-slice radial wheel whose wedges each swept 330 degrees
instead of 30 (`large-arc-flag=1` on a 30-degree chord). C did not see it, and
the reason generalises: in gf3-5 ONE broken slice covered the other eleven, so
reachability collapsed and C fired. When ALL of them are broken identically,
each still has a topmost sliver and every control tests as reachable. **C
catches asymmetric breakage; E catches symmetric breakage.** E is deliberately
narrow -- three or more sibling arcs sharing a radius, each with the flag set
while spanning under 90 degrees -- because rounded corners carry flag 0 and a
lone decorative arc is not three of them. Validated both ways on live VMs:
it fails fixval, and passes the two apps known to be correct.

Assertion D is new signal outright: every gate we have is load-time, and part B
item 11 ("survives interaction") has until now been a manual judgement.

FALSE POSITIVES ARE THE FAILURE MODE TO FEAR. `typescript/no-explicit-any` was
measured and REJECTED for `run_verify` because it failed working code; the same
bar applies here. Two deliberate concessions:

  * Occlusion samples a GRID inside each element's box and passes if ANY point
    reaches it. A wedge-shaped <button> has its box centre near the wheel hub —
    centre-only testing flags gf3-2, which works fine. Measured, not guessed.
  * Only elements that are *detectably* interactive are considered: <button>,
    <a href>, <input>, <select>, <textarea>, [role=button], [onclick], or
    `cursor: pointer`. Anything vaguer produces noise.

Usage:
    ./render_smoke.py <app_dir> [--json] [--max-clicks N]      # or: uv run render_smoke.py ...

Do NOT run it as `python3 render_smoke.py`: the shebang is `uv run`, which is
what installs the PEP-723 dependencies, and a bare interpreter skips that. It
used to exit 2 ("could not look") in that case, which a fix loop IGNORES -- so
an agent could believe it had checked its work while never seeing the page, and
one did, sixteen times. The script now re-execs itself under `uv run` instead.

Exit 0 = pass. Exit 1 = a real failure the builder must fix. Exit 2 = could not
look at all (no browser, server never came up) — an infrastructure problem, and
deliberately a DIFFERENT code so a fix loop is never spent on our own breakage.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# A crashed page still renders the shell's <div id="app"></div> and whatever
# static chrome index.html carries, so ">0 chars" proves nothing. 40 is well
# under every real arm (582-1476 chars measured across fan-out 3) and well over
# an empty shell.
MIN_TEXT_CHARS = 40

# Bound the interaction pass so a 200-control app cannot stall a fix loop.
DEFAULT_MAX_CLICKS = 25

SERVER_BOOT_TIMEOUT = 25.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connects(port: int) -> bool:
    """Is anything listening on this port, on EITHER address family?

    Load-bearing, and it cost real debugging time: `bun index.html` binds the
    IPv6 loopback ONLY. Measured with bun 1.3.0 --

        127.0.0.1 -> refused      localhost -> 200      [::1] -> 200

    so a probe hardcoded to 127.0.0.1 waits out its whole timeout against a
    server that has been up and serving since millisecond four. Resolve the name
    and try every family it gives back, exactly as a browser does.
    """
    try:
        infos = socket.getaddrinfo("localhost", port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]
    for family, socktype, proto, _canon, addr in infos:
        with socket.socket(family, socktype, proto) as s:
            s.settimeout(0.3)
            if s.connect_ex(addr) == 0:
                return True
    return False


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:      # server died; no point waiting out the clock
            return False
        if _connects(port):
            return True
        time.sleep(0.15)
    return False


# Collected in the page so one round trip does the whole sweep. Returns the
# interactive elements, whether each is reachable, and a stable handle for
# clicking. Mirrors what a user can actually hit.
PROBE_JS = r"""
() => {
  const INTERACTIVE = 'button, a[href], input, select, textarea, [role="button"], [onclick]';
  const isInteractive = (el) => {
    if (el.matches(INTERACTIVE)) return true;
    try { return getComputedStyle(el).cursor === 'pointer'; } catch { return false; }
  };
  const isVisible = (el, r) => {
    if (r.width < 4 || r.height < 4) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (parseFloat(cs.opacity || '1') < 0.05) return false;
    if (r.bottom < 0 || r.right < 0) return false;
    if (r.top > innerHeight || r.left > innerWidth) return false;
    return true;
  };

  // Only the OUTERMOST interactive element in a chain is a control. `cursor:
  // pointer` inherits, so a <button><span>C</span></button> otherwise reports
  // the span as its own control and then flags it as "covered by" its own
  // parent -- noise, and measured noise: it fired on gf3-2, which works.
  const all = [...document.querySelectorAll('*')];
  const candidates = all.filter(el => {
    if (!isInteractive(el)) return false;
    for (let p = el.parentElement; p; p = p.parentElement) {
      if (isInteractive(p)) return false;
    }
    return true;
  });

  // Reachable == some point exists where a real click lands on this control or
  // on something inside it. Sample the control's own box AND each descendant's
  // box: a wedge-shaped <button> has its box centre over the wheel hub while its
  // own label sits in the painted area, so box-only sampling calls a working
  // control unreachable.
  const FRACS = [0.5, 0.2, 0.8, 0.35, 0.65, 0.08, 0.92];
  const probe = (el, r, seen) => {
    for (const fx of FRACS) {
      for (const fy of FRACS) {
        const x = Math.min(innerWidth - 1, Math.max(0, r.left + r.width * fx));
        const y = Math.min(innerHeight - 1, Math.max(0, r.top + r.height * fy));
        const hit = document.elementFromPoint(x, y);
        if (!hit) continue;
        if (hit === el || el.contains(hit)) return true;
        if (!seen.occluder) {
          seen.occluder = hit.tagName.toLowerCase() +
            (hit.id ? '#' + hit.id : '') +
            (hit.getAttribute && hit.getAttribute('class')
              ? '.' + String(hit.getAttribute('class')).split(/\s+/).slice(0, 2).join('.') : '');
        }
      }
    }
    return false;
  };

  const out = [];
  candidates.forEach((el, i) => {
    const r = el.getBoundingClientRect();
    if (!isVisible(el, r)) return;
    const seen = { occluder: null };
    let reachable = probe(el, r, seen);
    if (!reachable) {
      for (const d of el.querySelectorAll('*')) {
        const dr = d.getBoundingClientRect();
        if (dr.width < 2 || dr.height < 2) continue;
        if (probe(el, dr, seen)) { reachable = true; break; }
      }
    }
    el.setAttribute('data-smoke-id', String(i));
    const label = (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 40);
    out.push({
      id: String(i),
      tag: el.tagName.toLowerCase(),
      cls: (el.getAttribute('class') || '').slice(0, 40),
      label, reachable, occluder: seen.occluder,
    });
  });
  // E: pie sectors that sweep the wrong way round.
  //
  // MEASURED on fixval-20260919-250a64, which this gate PASSED: all twelve
  // wedges of a twelve-slice wheel carried `A 198 198 0 1 1` -- large-arc-flag=1 on
  // endpoints 30 degrees apart, so each wedge swept 330 degrees instead of 30
  // and every slice painted over the whole wheel.
  //
  // Assertion C missed it for a specific reason worth keeping in mind: in
  // gf3-5 ONE slice was broken and covered the other eleven, so reachability
  // collapsed and C fired. When ALL of them are broken identically each still
  // has a topmost sliver, so every control tests as reachable. C catches
  // ASYMMETRIC breakage; this catches symmetric breakage.
  //
  // Intent is not recoverable from one path -- a genuine 330 degree wedge is
  // legal -- so the check is on SIBLINGS: N>=3 sectors sharing a centre and a
  // radius are a wheel, and a wheel's slices sum to ~360. Twelve correct
  // wedges sum to 360; twelve broken ones sum to 3960. That ratio is the
  // signal, and nothing legitimate lands near it.
  const ARC_MIN_RADIUS = 20;     // ignore rounded corners and other small curvature
  const ARC_MAX_SMALL = 90;      // a slice wider than this may legitimately take the long way
  const toDeg = (rad) => rad * 180 / Math.PI;

  // Walk a path's commands tracking the current point, because an arc's start
  // is wherever the previous command left off. Only M/L/H/V/A/Z are followed
  // exactly; any other command just resets tracking, which makes this give up
  // rather than guess. Both sector shapes we draw are covered: a pie wedge
  // (M centre, L rim, A) and an annulus (M rim, A, L, A).
  const arcsOf = (d) => {
    const out = [];
    const toks = String(d).match(/[A-Za-z]|-?[0-9.]+(?:e-?[0-9]+)?/gi) || [];
    let i = 0, cx = 0, cy = 0, sx = 0, sy = 0, cmd = '';
    const num = () => parseFloat(toks[i++]);
    while (i < toks.length) {
      if (/[A-Za-z]/.test(toks[i])) cmd = toks[i++];
      if (i >= toks.length && !/[Zz]/.test(cmd)) break;
      const rel = cmd === cmd.toLowerCase();
      const C = cmd.toUpperCase();
      if (C === 'M') { const x = num(), y = num(); cx = rel ? cx + x : x; cy = rel ? cy + y : y; sx = cx; sy = cy; cmd = rel ? 'l' : 'L'; }
      else if (C === 'L') { const x = num(), y = num(); cx = rel ? cx + x : x; cy = rel ? cy + y : y; }
      else if (C === 'H') { const x = num(); cx = rel ? cx + x : x; }
      else if (C === 'V') { const y = num(); cy = rel ? cy + y : y; }
      else if (C === 'Z') { cx = sx; cy = sy; }
      else if (C === 'A') {
        const rx = num(), ry = num(); num();            // radii, x-rotation
        const laf = num() === 1; num();                 // large-arc, sweep
        const ex = num(), ey = num();
        const x = rel ? cx + ex : ex, y = rel ? cy + ey : ey;
        out.push({ rx, ry, laf, chord: Math.hypot(x - cx, y - cy) });
        cx = x; cy = y;
      } else return out;                                 // a curve: stop guessing
    }
    return out;
  };

  // The fault signature, stated narrowly on purpose: three or more sibling arcs
  // that share a radius, each set large-arc-flag=1, and each spans well under a
  // half-circle the short way. Rounded corners carry flag 0 and never qualify;
  // a lone decorative 300-degree arc is not three of them.
  const groups = {};
  for (const path of document.querySelectorAll('path')) {
    for (const a of arcsOf(path.getAttribute('d') || '')) {
      const r = a.rx;
      if (!(r >= ARC_MIN_RADIUS)) continue;
      if (Math.abs(a.rx - a.ry) / Math.max(a.rx, a.ry, 1) > 0.02) continue;  // circular only
      if (!a.laf) continue;
      if (!(a.chord <= 2 * r)) continue;
      const small = 2 * toDeg(Math.asin(Math.min(1, a.chord / (2 * r))));
      if (small >= ARC_MAX_SMALL) continue;
      const key = String(Math.round(r));
      (groups[key] = groups[key] || { n: 0, total: 0, small: 0 });
      groups[key].n += 1;
      groups[key].total += 360 - small;          // what the flag makes it draw
      groups[key].small += small;                // what it plainly meant to draw
    }
  }
  const sectorFaults = [];
  for (const [key, g] of Object.entries(groups)) {
    if (g.n < 3) continue;                     // not a ring of slices
    sectorFaults.push({
      radius: key, slices: g.n,
      degreesDrawn: Math.round(g.total),
      each: Math.round(g.total / g.n),
      intended: Math.round(g.small / g.n),
    });
  }

  return { controls: out, sectorFaults,
           textLength: (document.body.innerText || '').trim().length };
}
"""


def _reexec_under_uv() -> None:
    """Re-run this script through `uv run`, which installs the PEP-723 header.

    The shebang is `#!/usr/bin/env -S uv run`, so `./render_smoke.py` gets
    playwright and `python3 render_smoke.py` does not — the interpreter is
    invoked directly and the header is just a comment. MEASURED on run
    `fixval-20260919-250a64`: the builder, told to run this gate, chose
    `python3 adws/adw_modules/render_smoke.py` and got exit 2 SIXTEEN times.

    Exit 2 means "could not look", which a fix loop deliberately ignores — so
    the agent believed it had checked its work sixteen times while never once
    seeing the page. A silent skip is worse than a failure, and the invocation
    is not the agent's mistake to make: recover it here instead.

    Guarded by an env var so a re-exec can never recurse, and falling through
    (rather than failing) when uv is absent, so the original ImportError
    message still explains itself.
    """
    if os.environ.get("RENDER_SMOKE_REEXEC") == "1":
        return
    from shutil import which
    if not which("uv"):
        return
    os.environ["RENDER_SMOKE_REEXEC"] = "1"
    print("render_smoke: no playwright under this interpreter — re-running via `uv run`.",
          file=sys.stderr)
    proc = subprocess.run(["uv", "run", os.path.abspath(__file__), *sys.argv[1:]])
    raise SystemExit(proc.returncode)


def run(app_dir: str, max_clicks: int) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _reexec_under_uv()          # does not return when it can recover
        print("render_smoke: playwright is not installed.", file=sys.stderr)
        print("  Run it as `uv run adws/adw_modules/render_smoke.py <app-dir>`,", file=sys.stderr)
        print("  or `./adws/adw_modules/render_smoke.py <app-dir>` — NOT `python3 ...`,", file=sys.stderr)
        print("  which bypasses the PEP-723 header that installs playwright.", file=sys.stderr)
        raise SystemExit(2)

    app = Path(app_dir)
    if not (app / "index.html").is_file():
        print(f"render_smoke: no index.html in {app}", file=sys.stderr)
        raise SystemExit(2)

    port = _free_port()
    # Same command observe.just uses, for the same reason quality.py writes its
    # command blocks down: the gate must exercise what actually gets served.
    env = {**os.environ, "PORT": str(port)}
    server = subprocess.Popen(["bun", "index.html"], cwd=str(app), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    report: dict = {"errors": [], "console": [], "unreachable": [], "click_failures": []}
    try:
        if not _wait_for_port(port, server, SERVER_BOOT_TIMEOUT):
            out = ""
            if server.stdout:
                try:
                    out = server.stdout.read()[:2000]
                except Exception:
                    pass
            print(f"render_smoke: dev server never answered on :{port}\n{out}", file=sys.stderr)
            raise SystemExit(2)

        with sync_playwright() as p:
            # --no-sandbox: the VM runs this as an unprivileged user with no
            # user-namespace support; without it chromium refuses to start.
            browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            # A modal dialog blocks every later command, so never let one sit.
            page.on("dialog", lambda d: d.dismiss())
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            page.on("console", lambda m: (report["console"].append(m.text)
                                          if m.type == "error" else None))

            page.goto(f"http://localhost:{port}/", wait_until="load", timeout=30_000)
            page.wait_for_timeout(600)      # let a rAF/microtask render settle

            probe = page.evaluate(PROBE_JS)
            # Freeze what happened BEFORE any click. Without this an error raised
            # by a click handler is reported as "on load" and sends the builder
            # looking in the wrong place -- caught by mutation-testing this file.
            report["load_errors"] = list(report["errors"])
            report["load_console"] = list(report["console"])
            report["textLength"] = probe["textLength"]
            report["controlCount"] = len(probe["controls"])
            report["unreachable"] = [c for c in probe["controls"] if not c["reachable"]]
            # Assertion E. Read BEFORE the interaction pass, like the rest: a
            # wheel drawn wrong is wrong on arrival, and a click that redraws it
            # must not be able to launder the fault.
            report["sectorFaults"] = probe.get("sectorFaults", [])

            # D: drive it. Clicking is what part B item 11 measures by hand, and
            # nothing in the chain has ever done it.
            clickable = [c for c in probe["controls"] if c["reachable"]][:max_clicks]
            for c in clickable:
                before = len(report["errors"]) + len(report["console"])
                try:
                    page.click(f'[data-smoke-id="{c["id"]}"]',
                               timeout=1500, force=False, no_wait_after=True)
                    page.wait_for_timeout(60)
                except Exception:
                    # A control that cannot be clicked in 1.5s is not itself a
                    # defect (animations, transient overlays); C already covers
                    # genuine unreachability. Only THROWN errors count here.
                    continue
                if len(report["errors"]) + len(report["console"]) > before:
                    report["click_failures"].append({
                        "control": f'{c["tag"]}.{c["cls"]} "{c["label"]}"',
                        "error": (report["errors"] + report["console"])[-1][:300],
                    })
            report["textAfter"] = page.evaluate(
                "() => (document.body.innerText || '').trim().length")
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
    return report


def verdict(r: dict) -> tuple[bool, list[str]]:
    failures: list[str] = []
    load_errors = r.get("load_errors", r["errors"])
    load_console = r.get("load_console", r["console"])
    if load_errors:
        failures.append("Uncaught error(s) on load:\n  " + "\n  ".join(load_errors[:5]))
    if load_console and not load_errors:
        failures.append("console.error on load:\n  " + "\n  ".join(load_console[:5]))
    if r.get("textLength", 0) < MIN_TEXT_CHARS:
        failures.append(f"Page rendered {r.get('textLength', 0)} chars of text "
                        f"(minimum {MIN_TEXT_CHARS}) — it did not draw.")
    if r["unreachable"]:
        lines = [f'  {c["tag"]}.{c["cls"]} "{c["label"]}" — every point is covered '
                 f'by {c["occluder"]}' for c in r["unreachable"][:8]]
        failures.append(
            f"{len(r['unreachable'])} interactive element(s) are completely unreachable — "
            f"a user cannot click them:\n" + "\n".join(lines))
    if r["click_failures"]:
        lines = [f'  {c["control"]} -> {c["error"]}' for c in r["click_failures"][:5]]
        failures.append("Clicking a control raised an error (NOT on load — it needs "
                        "interaction to reproduce):\n" + "\n".join(lines))
    if r.get("textAfter", 1) < MIN_TEXT_CHARS <= r.get("textLength", 0):
        failures.append("The page went blank during the interaction pass.")
    for g in r.get("sectorFaults", []):
        failures.append(
            f"{g['slices']} arcs of radius {g['radius']} set the SVG large-arc-flag to 1 "
            f"while spanning only ~{g['intended']}deg between their endpoints, so each one "
            f"draws the LONG way round at ~{g['each']}deg instead "
            f"({g['degreesDrawn']}deg in total, and a circle is 360). Every slice is "
            f"painting over the whole ring. Set the flag to 0 for any sector under 180deg "
            f"— e.g. `const large = Math.abs(to - from) > 180 ? 1 : 0`.")
    return (not failures), failures


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    max_clicks = DEFAULT_MAX_CLICKS
    for a in sys.argv[1:]:
        if a.startswith("--max-clicks"):
            max_clicks = int(a.split("=", 1)[1]) if "=" in a else max_clicks

    report = run(args[0], max_clicks)
    ok, failures = verdict(report)

    if "--json" in sys.argv:
        print(json.dumps({"passed": ok, "failures": failures, **report}, indent=2))
    else:
        print(f"render_smoke: {report.get('controlCount', 0)} interactive element(s), "
              f"{report.get('textLength', 0)} chars rendered")
        if ok:
            print("render_smoke: PASS — loads, draws, every control reachable, "
                  "no error while driving it")
        else:
            for f in failures:
                print(f"render_smoke: FAIL — {f}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
