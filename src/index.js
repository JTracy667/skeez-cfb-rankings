// Skeez CFB Rankings — Cloudflare Containers Worker
import { Container, getContainer } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

export class CFBPowerRankings extends Container {
	defaultPort = 8003;
	sleepAfter = "20m";
	pingEndpoint = "ping";
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
};
