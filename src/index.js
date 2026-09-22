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
		// Hourly refresh so the odds/grading cadence (line-movement log, CLV
		// tracking) stays at 3600s instead of app.py's 6h default.
		REFRESH_INTERVAL_SECONDS: "3600",
		// Shared secret the cron uses to call the anchor-gated pull, which is now
		// an admin-gated route (ops writes are locked; public pages are not).
		ADMIN_TOKEN: env.ADMIN_TOKEN,
		// Deployed image tag, surfaced by /api/health as `build`. Lets a
		// post-deploy check prove the NEW image is serving rather than a warm
		// instance of the previous one (see api_health docstring).
		BUILD_TAG: env.BUILD_TAG ?? "dev",
		// Freshness guard: pull whenever the analytics data is older than
		// FRESHNESS_MAX_AGE_HOURS, independent of the anchor schedule. This is what
		// makes a missed anchor degrade to "stale until the next visitor" instead of
		// a multi-day gap. Rollback = set FRESHNESS_GUARD=0 as a Worker secret
		// (restores anchor-only behaviour) and redeploy.
		FRESHNESS_GUARD: env.FRESHNESS_GUARD ?? "1",
		FRESHNESS_MAX_AGE_HOURS: env.FRESHNESS_MAX_AGE_HOURS ?? "12",
		// D1 live write-path (D1_SCHEMA_SPEC §5): appends odds_snapshots,
		// rankings_daily, closing_lines and model_predictions to D1 cfb-history.
		// Flag OFF == pre-D1 behaviour; rollback = set this to "0" and redeploy.
		// The token is D1-scoped (account D1:Edit only) and is read from
		// CF_D1_TOKEN first by d1_store.py — never hand the container a token
		// that can edit DNS, zone settings or Workers.
		D1_WRITE_ENABLED: env.D1_WRITE_ENABLED ?? "1",
		CF_D1_TOKEN: env.CF_D1_TOKEN,
		CF_ACCOUNT_ID: env.CF_ACCOUNT_ID ?? "",
		CF_D1_DB_ID: env.CF_D1_DB_ID ?? "",
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
