/* Verify the SRS vintage label by EXECUTING the shipped render functions.
 *
 * A grep only proves the source text mentions a label; this proves the rendered markup
 * actually carries it, and — just as important — that an unknown vintage renders NO
 * label rather than a wrong one. Runs without a browser or a network.
 *
 * Usage:  node scripts/test_analytics_srs_label.js      (exit 0 = pass)
 */
const fs = require("fs");
const path = require("path");

const htmlPath = path.join(__dirname, "..", "analytics.html");
const html = fs.readFileSync(htmlPath, "utf8");

function extract(name) {
  const start = html.indexOf("function " + name);
  if (start < 0) throw new Error("function " + name + " not found in analytics.html");
  // Walk braces so we take exactly one function, not the rest of the file.
  let i = html.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < html.length; j++) {
    if (html[j] === "{") depth++;
    else if (html[j] === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
  }
  return html.slice(start, end);
}

let out = [];
global.document = {
  getElementById: (id) => ({ set innerHTML(v) { out.push({ id, v }); } }),
};
global.val = (t, k, d) => (t[k] === undefined || t[k] === null ? d : t[k]);
global.pctBadge = (x) => String(x);

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; console.log("  FAIL  " + name + (extra ? "\n          " + extra : "")); }
}

// ── renderSRS (the SRS tab) ─────────────────────────────────────────────────────
const srcSRS = extract("renderSRS");
eval(srcSRS);

// 1. Vintage known -> header + an explanatory note both carry it.
out = [];
global.teams = [{ name: "Georgia", srs: 16, srs_source_year: 2025 }];
renderSRS(null);
const srsHtml = out[out.length - 1].v;
check("SRS tab header carries the vintage", srsHtml.includes("SRS (2025)"),
      "rendered: " + srsHtml.slice(0, 160));
check("SRS tab explains the fallback", /has not published this season/i.test(srsHtml),
      "rendered: " + srsHtml.slice(0, 200));
check("SRS tab still renders the rows", srsHtml.includes("Georgia"));

// 2. Vintage unknown -> NO label, and never a wrong one.
out = [];
global.teams = [{ name: "Georgia", srs: 16 }];
renderSRS(null);
const srsNoVintage = out[out.length - 1].v;
check("unknown vintage renders no label at all",
      srsNoVintage.includes("<th>SRS</th>") && !srsNoVintage.includes("SRS ("),
      "rendered: " + srsNoVintage.slice(0, 160));

// ── renderComposite (the main table) ────────────────────────────────────────────
const srcComposite = extract("renderComposite");
eval(srcComposite);

out = [];
global.teams = [{ name: "Georgia", composite: 92.6, srs: 16, srs_source_year: 2025 }];
renderComposite(null);
const compHtml = out[out.length - 1].v;
check("composite table header carries the vintage", compHtml.includes("SRS (2025)"),
      "rendered: " + compHtml.slice(0, 200));

out = [];
global.teams = [{ name: "Georgia", composite: 92.6, srs: 16 }];
renderComposite(null);
const compNoVintage = out[out.length - 1].v;
check("composite table renders no label when vintage unknown",
      !compNoVintage.includes("SRS ("), "rendered: " + compNoVintage.slice(0, 200));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);