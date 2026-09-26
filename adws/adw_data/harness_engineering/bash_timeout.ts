/**
 * bash_timeout.ts — every bash call gets a timeout, so a command that never
 * exits comes back to the agent as an error it can read, instead of hanging the
 * turn until the stall watchdog kills the whole run.
 *
 * WHY. pi's bash tool accepts `timeout` (seconds) but has no default. harn4
 * (2026-09-24) ran a probe whose `setInterval` kept bun alive; the call never
 * returned, the 900 s watchdog in agent_pi.py killed the RUN, and the agent
 * never learned why — after a hand-kill it simply retried the same hang. With a
 * timeout, pi kills the command's process tree and returns its output plus
 * "Command timed out after N seconds" as a tool error, in the same turn.
 *
 * WHAT. A `tool_call` hook (event.input is mutable before execution):
 *   - no timeout asked for       -> PI_BASH_TIMEOUT_SECONDS
 *   - a longer one asked for     -> clamped to PI_BASH_TIMEOUT_MAX_SECONDS
 * Both values come from agent_pi.py, which sets them below its own stall limit,
 * so the tool always times out before the watchdog fires. The fallbacks here
 * only matter when pi is run by hand with this extension.
 *
 * Loaded for every pi agent by agent_pi.run, not by roster: it registers no
 * tool, so `--tools` allowlists do not filter it.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

function seconds(name: string, fallback: number): number {
	const value = Number(process.env[name]);
	return Number.isFinite(value) && value > 0 ? value : fallback;
}

const DEFAULT_SECONDS = seconds("PI_BASH_TIMEOUT_SECONDS", 300);
const MAX_SECONDS = seconds("PI_BASH_TIMEOUT_MAX_SECONDS", 600);

export default function (pi: ExtensionAPI) {
	pi.on("tool_call", async (event) => {
		if (event.toolName !== "bash") return;
		const asked = (event.input as { timeout?: unknown }).timeout;
		const wanted = typeof asked === "number" && Number.isFinite(asked) && asked > 0 ? asked : DEFAULT_SECONDS;
		(event.input as { timeout?: number }).timeout = Math.min(wanted, MAX_SECONDS);
	});
}
