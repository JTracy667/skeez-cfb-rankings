// Skeez CFB Rankings — Cloudflare Containers Worker
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class CFBPowerRankings extends Container {
	defaultPort = 8003;
	sleepAfter = "20m";
	envVars = {
		CFBD_API_KEY: env.CFBD_API_KEY,
		PROPLINE_API_KEY: env.PROPLINE_API_KEY,
		THE_ODDS_API_KEY: env.THE_ODDS_API_KEY,
		// Match the Render service (render.yaml) so the cutover does not silently
		// slow the odds/grading refresh from hourly to app.py's 6h default — the
		// line-movement log and CLV tracking depend on that cadence.
		REFRESH_INTERVAL_SECONDS: "3600",
		// Shared secret the cron uses to call the anchor-gated pull, which is now
		// an admin-gated route (ops writes are locked; public pages are not).
		ADMIN_TOKEN: env.ADMIN_TOKEN,
	};
}

export default {
	async fetch(request, env) {
		return getContainer(env.CFBPOWER_RANKINGS).fetch(request);
	},

	// Deterministic refresh trigger. Containers sleep after 20m idle, which
	// stops app.py's background scheduler thread — so on this host the
	// Sun/Mon/Tue/Wed 21:00 PT (America/Los_Angeles) CFBD analytics sync would
	// otherwise depend on inbound traffic. These crons wake the container at the
	// anchor and the app's own due-check decides whether to pull
	// (POST /api/analytics/refresh-if-due is idempotent: no anchor, no pull).
	async scheduled(controller, env, ctx) {
		ctx.waitUntil((async () => {
			try {
				const container = getContainer(env.CFBPOWER_RANKINGS);
				const res = await container.fetch(
					new Request("http://cfb-container/api/analytics/refresh-if-due", {
						method: "POST",
						// This route is admin-gated (ops writes locked); the cron is
						// an authorised internal caller, so it presents the secret.
						headers: { "X-Admin-Token": env.ADMIN_TOKEN ?? "" },
					}),
				);
				console.log(`[cron] refresh-if-due -> ${res.status} ${await res.text()}`);
			} catch (e) {
				// Never throw out of a cron: a failed wake retries on the next
				// anchor / next inbound request.
				console.log(`[cron] refresh-if-due failed: ${e}`);
			}
		})());
	},
};
