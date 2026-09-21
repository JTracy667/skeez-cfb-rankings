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
					new Request("http://cfb-container/api/analytics/refresh-if-due", { method: "POST" }),
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
