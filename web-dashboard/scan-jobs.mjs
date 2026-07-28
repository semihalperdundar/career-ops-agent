#!/usr/bin/env node
// career-ops LOCAL high-volume scanner.
//
// Fetches FULL job lists directly from public ATS APIs (Greenhouse / Lever /
// SmartRecruiters / Ashby) for every tracked company, PLUS LinkedIn's guest jobs
// endpoint (no login) for broad coverage across all companies. Filters by title +
// location (config/locations.json), dedups against the tracker/history/pipeline,
// and appends new in-scope offers to data/pipeline.md.
//
// IMPORTANT: this machine needs the system CA store for TLS. Run with:
//   NODE_OPTIONS=--use-system-ca node web-dashboard/scan-jobs.mjs
// (the npm script "scan:local" sets this for you).

import { readFile, writeFile, appendFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const TODAY = new Date().toISOString().slice(0, 10);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getText(url, opts = {}) {
  try {
    const r = await fetch(url, { headers: { "User-Agent": UA, ...(opts.headers || {}) }, method: opts.method || "GET", body: opts.body });
    if (!r.ok) return { ok: false, status: r.status, text: "" };
    return { ok: true, status: r.status, text: await r.text() };
  } catch (e) {
    return { ok: false, status: 0, text: "", err: e.message };
  }
}
async function getJSON(url, opts) {
  const r = await getText(url, opts);
  if (!r.ok) return null;
  try { return JSON.parse(r.text); } catch { return null; }
}

// ---- config ----
// Capture only the indented "- \"value\"" items directly under `key:` (stop at the
// next line whose indentation is <= the key's — avoids swallowing sibling blocks).
function parseYamlBlock(yaml, key) {
  const lines = yaml.split("\n");
  const out = [];
  let inBlock = false, indent = -1;
  for (const line of lines) {
    if (!inBlock) {
      const m = line.match(/^(\s*)([\w]+):\s*$/);
      if (m && m[2] === key) { inBlock = true; indent = m[1].length; }
      continue;
    }
    if (line.trim() === "") continue;
    const cur = (line.match(/^(\s*)/)[1]).length;
    if (cur <= indent) break;
    const v = line.match(/-\s*"([^"]+)"/);
    if (v) out.push(v[1]);
  }
  return out;
}

// Over-broad keywords that create noise as standalone title matches.
const BROAD = new Set(["ai", "ml", "nlp", "mlops", "machine learning"]);

async function loadConfig() {
  const portals = await readFile(join(ROOT, "portals.yml"), "utf8");
  const positive = parseYamlBlock(portals, "positive").filter((p) => !BROAD.has(p.toLowerCase()));
  const negative = parseYamlBlock(portals, "negative");
  // tracked companies: grab name + careers_url + api lines
  const companies = [];
  const blocks = portals.split(/\n\s*-\s+name:\s*/).slice(1);
  for (const b of blocks) {
    const name = (b.match(/^(.+)/) || [])[1]?.trim();
    const careers = (b.match(/careers_url:\s*(\S+)/) || [])[1];
    const api = (b.match(/\n\s*api:\s*(\S+)/) || [])[1];
    const enabled = !/enabled:\s*false/.test(b);
    if (name && enabled && (careers || api)) companies.push({ name, careers, api });
  }
  const locations = JSON.parse(await readFile(join(ROOT, "config", "locations.json"), "utf8"));
  // User-added sources from the dashboard (config/sources.json).
  let custom = [];
  try {
    const sj = JSON.parse(await readFile(join(ROOT, "config", "sources.json"), "utf8"));
    custom = (Array.isArray(sj.custom) ? sj.custom : []).filter((s) => s.enabled !== false);
  } catch {}
  return { positive, negative, companies, locations, custom };
}

// Given an arbitrary careers-page URL, fetch it and try to discover an embedded
// ATS board (Greenhouse / Lever / Ashby / SmartRecruiters) so we can scan it.
async function discoverAts(pageUrl) {
  if (!pageUrl) return null;
  const r = await getText(pageUrl);
  if (!r.ok) return null;
  const h = r.text;
  let m = h.match(/job-boards\.greenhouse\.io\/([a-z0-9_-]+)/i)
    || h.match(/boards\.greenhouse\.io\/(?:embed\/job_board\?for=)?([a-z0-9_-]+)/i)
    || h.match(/boards-api\.greenhouse\.io\/v1\/boards\/([a-z0-9_-]+)/i);
  if (m) return `https://boards-api.greenhouse.io/v1/boards/${m[1]}/jobs`;
  m = h.match(/jobs\.lever\.co\/([a-z0-9_-]+)/i) || h.match(/api\.lever\.co\/v0\/postings\/([a-z0-9_-]+)/i);
  if (m) return `https://jobs.lever.co/${m[1]}`;
  m = h.match(/jobs\.ashbyhq\.com\/([a-z0-9_-]+)/i) || h.match(/api\.ashbyhq\.com\/posting-api\/job-board\/([a-z0-9_-]+)/i);
  if (m) return `https://jobs.ashbyhq.com/${m[1]}`;
  m = h.match(/(?:jobs|api)\.smartrecruiters\.com\/(?:v1\/companies\/)?([a-zA-Z0-9_-]+)/);
  if (m) return `https://api.smartrecruiters.com/v1/companies/${m[1]}/postings`;
  m = h.match(/([a-z0-9-]+)\.recruitee\.com/i);
  if (m) return `https://${m[1]}.recruitee.com/api/offers/`;
  m = h.match(/([a-z0-9-]+)\.jobs\.personio\.com/i);
  if (m) return `https://${m[1]}.jobs.personio.com/xml`;
  return null;
}

// Fallback: derive a slug from a company name and probe every ATS provider for a
// live board. Returns a careers/api URL that atsFor() can parse, or null.
function slugify(s) {
  return (s || "").toLowerCase()
    .replace(/\b(the|group|holding|nederland|netherlands|bv|nv|inc|llc|ltd|gmbh|careers|jobs)\b/g, "")
    .replace(/[^a-z0-9]+/g, "").trim();
}
async function atsBySlug(slug) {
  if (!slug) return null;
  const gh = await getJSON(`https://boards-api.greenhouse.io/v1/boards/${slug}/jobs`);
  if (gh?.jobs?.length) return `https://boards-api.greenhouse.io/v1/boards/${slug}/jobs`;
  const lv = await getJSON(`https://api.lever.co/v0/postings/${slug}?mode=json`);
  if (Array.isArray(lv) && lv.length) return `https://jobs.lever.co/${slug}`;
  const ash = await getJSON(`https://api.ashbyhq.com/posting-api/job-board/${slug}`);
  if (ash?.jobs?.length) return `https://jobs.ashbyhq.com/${slug}`;
  const rec = await getJSON(`https://${slug}.recruitee.com/api/offers/`);
  if (Array.isArray(rec?.offers) && rec.offers.length) return `https://${slug}.recruitee.com`;
  const sm = await getJSON(`https://api.smartrecruiters.com/v1/companies/${slug}/postings?limit=10`);
  if (sm && (sm.totalFound || sm.content?.length)) return `https://api.smartrecruiters.com/v1/companies/${slug}/postings`;
  const pe = await getText(`https://${slug}.jobs.personio.com/xml`);
  if (pe.ok && /<position>/i.test(pe.text)) return `https://${slug}.jobs.personio.com`;
  return null;
}

// ---- filters ----
function titleOk(title, positive, negative) {
  const t = " " + title.toLowerCase() + " ";
  if (negative.some((n) => t.includes(n.toLowerCase()))) return false;
  return positive.some((p) => t.includes(p.toLowerCase()));
}
function locationOk(locText, locations) {
  const t = (locText || "").toLowerCase();
  if (!t) return { ok: true, why: "unknown" }; // keep, flag
  const isRemote = /\bremote\b|anywhere|work from home|hybrid/.test(t);
  // "US-only" = mentions US locations but none of YOUR configured regions (and not a generic intl/remote tag)
  const intlHit = /\b(eu|europe|emea|remote|worldwide|anywhere)\b/.test(t) ||
    (locations.regions || []).some((r) => [r.country, ...(r.cities || []), ...(r.keywords || [])].filter(Boolean).some((k) => t.includes(String(k).toLowerCase())));
  const usOnly = /\b(united states|usa|u\.s\.|new york|san francisco|california|remote - us|us only|austin|boston|chicago)\b/.test(t) && !intlHit;
  for (const r of locations.regions) {
    if (r.enabled === false) continue;
    const keys = [r.country, ...(r.cities || []), ...(r.keywords || [])].filter(Boolean).map((s) => s.toLowerCase());
    if (keys.some((k) => t.includes(k))) return { ok: true, why: "region" };
  }
  if (isRemote && locations.remote_ok && !usOnly) return { ok: true, why: "remote" };
  return { ok: false, why: usOnly ? "us-only" : "out-of-scope" };
}
function normRole(s) { return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\b(senior|junior|lead|staff|principal|m w d|m f x|f m d)\b/g, "").trim(); }
function normCompany(s) { return (s || "").toLowerCase().replace(/\b(inc|llc|ltd|gmbh|bv|nv|group|technologies|the)\b/g, "").replace(/[^a-z0-9]+/g, "").trim(); }

// ---- ATS fetchers (return [{title, company, url, location}]) ----
async function fromGreenhouse(api, name) {
  const slug = (api.match(/boards\/([^/]+)\/jobs/) || [])[1];
  const data = await getJSON(`https://boards-api.greenhouse.io/v1/boards/${slug}/jobs`);
  if (!data || !data.jobs) return null;
  return data.jobs.map((j) => ({ title: j.title, company: name, url: j.absolute_url, location: j.location?.name || "" }));
}
async function fromLever(slug, name) {
  const data = await getJSON(`https://api.lever.co/v0/postings/${slug}?mode=json`);
  if (!Array.isArray(data)) return null;
  return data.map((p) => ({ title: p.text, company: name, url: p.hostedUrl, location: p.categories?.location || p.workplaceType || "" }));
}
async function fromSmartRecruiters(slug, name) {
  const out = [];
  for (let offset = 0; offset < 200; offset += 100) {
    const data = await getJSON(`https://api.smartrecruiters.com/v1/companies/${slug}/postings?limit=100&offset=${offset}`);
    if (!data || !data.content) break;
    for (const p of data.content) {
      const loc = [p.location?.city, p.location?.region, p.location?.country].filter(Boolean).join(", ") + (p.location?.remote ? " (remote)" : "");
      out.push({ title: p.name, company: name, url: `https://jobs.smartrecruiters.com/${slug}/${p.id}`, location: loc });
    }
    if (data.content.length < 100) break;
  }
  return out.length ? out : null;
}
async function fromAshby(slug, name) {
  const data = await getJSON(`https://api.ashbyhq.com/posting-api/job-board/${slug}?includeCompensation=false`, { method: "GET" });
  if (!data || !data.jobs) return null;
  return data.jobs.map((j) => ({ title: j.title, company: name, url: j.jobUrl || j.applyUrl, location: j.location || (j.address?.postalAddress?.addressLocality) || "" }));
}
// Recruitee — very common in the Netherlands. Public JSON: {slug}.recruitee.com/api/offers/
async function fromRecruitee(slug, name) {
  const data = await getJSON(`https://${slug}.recruitee.com/api/offers/`);
  if (!data || !Array.isArray(data.offers)) return null;
  return data.offers.map((o) => ({
    title: o.title,
    company: name,
    url: o.careers_url || o.careers_apply_url || `https://${slug}.recruitee.com/o/${o.slug}`,
    location: [o.city, o.country].filter(Boolean).join(", ") || o.location || "",
  }));
}
// Personio — common in DACH/NL. Public XML feed: {slug}.jobs.personio.com/xml
async function fromPersonio(slug, name) {
  const r = await getText(`https://${slug}.jobs.personio.com/xml`);
  if (!r.ok) return null;
  const out = [];
  for (const block of r.text.split(/<position>/i).slice(1)) {
    const get = (tag) => { const m = block.match(new RegExp(`<${tag}>(?:<!\\[CDATA\\[)?([\\s\\S]*?)(?:\\]\\]>)?</${tag}>`, "i")); return m ? m[1].trim() : ""; };
    const id = get("id"); const title = get("name"); const office = get("office");
    if (title) out.push({ title, company: name, url: `https://${slug}.jobs.personio.com/job/${id}`, location: office });
  }
  return out.length ? out : null;
}

function atsFor(c) {
  const u = (c.api || c.careers || "").toLowerCase();
  if (u.includes("greenhouse.io")) { const slug = (u.match(/greenhouse\.io\/(?:v1\/boards\/)?([^/?]+)/) || [])[1]; return { kind: "greenhouse", slug, fetch: () => fromGreenhouse(c.api || `https://boards-api.greenhouse.io/v1/boards/${slug}/jobs`, c.name) }; }
  if (u.includes("lever.co")) { const slug = (u.match(/lever\.co\/(?:v0\/postings\/)?([^/?]+)/) || [])[1]; return { kind: "lever", slug, fetch: () => fromLever(slug, c.name) }; }
  if (u.includes("smartrecruiters")) { const slug = (u.match(/companies\/([^/?]+)/) || u.match(/smartrecruiters\.com\/([^/?]+)/) || [])[1]; return { kind: "smartrecruiters", slug, fetch: () => fromSmartRecruiters(slug, c.name) }; }
  if (u.includes("ashbyhq")) { const slug = (u.match(/ashbyhq\.com\/(?:job-board\/)?([^/?]+)/) || [])[1]; return { kind: "ashby", slug, fetch: () => fromAshby(slug, c.name) }; }
  if (u.includes("recruitee.com")) { const slug = (u.match(/([a-z0-9-]+)\.recruitee\.com/) || u.match(/recruitee\.com\/(?:c\/)?([^/?]+)/) || [])[1]; return { kind: "recruitee", slug, fetch: () => fromRecruitee(slug, c.name) }; }
  if (u.includes("personio")) { const slug = (u.match(/([a-z0-9-]+)\.jobs\.personio\.com/) || u.match(/personio\.[a-z]+\/[^/]*\/([a-z0-9-]+)/) || [])[1]; return { kind: "personio", slug, fetch: () => fromPersonio(slug, c.name) }; }
  return null;
}

// ---- LinkedIn guest jobs ----
function parseLinkedIn(html) {
  const cards = [];
  for (const block of html.split(/<li>/).slice(1)) {
    const url = (block.match(/href="(https:\/\/[a-z]{0,3}\.?linkedin\.com\/jobs\/view\/[^"?]+)/) || [])[1];
    const title = (block.match(/base-search-card__title">\s*([\s\S]*?)<\/h3>/) || [])[1]?.replace(/<[^>]+>/g, "").trim();
    const company = (block.match(/hidden-nested-link[^>]*>\s*([\s\S]*?)<\/a>/) || block.match(/base-search-card__subtitle"[^>]*>\s*([\s\S]*?)<\//) || [])[1]?.replace(/<[^>]+>/g, "").trim();
    const location = (block.match(/job-search-card__location"[^>]*>\s*([\s\S]*?)<\//) || [])[1]?.replace(/<[^>]+>/g, "").trim();
    if (url && title) cards.push({ title, company: company || "?", url: url.split("?")[0], location: location || "" });
  }
  return cards;
}
async function fromLinkedIn(roles, locs) {
  const out = [];
  for (const loc of locs) {
    for (const role of roles) {
      for (let start = 0; start <= 50; start += 25) {
        const u = `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=${encodeURIComponent(role)}&location=${encodeURIComponent(loc)}&start=${start}`;
        const r = await getText(u);
        if (!r.ok) { if (r.status === 429) await sleep(1500); break; }
        const cards = parseLinkedIn(r.text);
        out.push(...cards);
        if (cards.length < 5) break;
        await sleep(400);
      }
    }
  }
  return out;
}

// ---- dedup sources ----
async function loadDedup() {
  const seenUrls = new Set();
  const compRole = new Set();
  try {
    const hist = await readFile(join(ROOT, "data", "scan-history.tsv"), "utf8");
    for (const line of hist.split("\n")) { const u = line.split("\t")[0]; if (u && u.startsWith("http")) seenUrls.add(u.split("?")[0]); }
  } catch {}
  try {
    const pipe = await readFile(join(ROOT, "data", "pipeline.md"), "utf8");
    for (const m of pipe.matchAll(/(https?:\/\/\S+)/g)) seenUrls.add(m[1].split("?")[0]);
  } catch {}
  try {
    const apps = await readFile(join(ROOT, "data", "applications.md"), "utf8");
    for (const line of apps.split("\n")) {
      if (!line.trim().startsWith("|")) continue;
      const c = line.split("|").map((x) => x.trim());
      if (c.length > 4 && /^\d+$/.test(c[1])) compRole.add(normCompany(c[3]) + "::" + normRole(c[4]));
    }
  } catch {}
  return { seenUrls, compRole };
}

// ---- main ----
async function main() {
  const { positive, negative, companies, locations, custom } = await loadConfig();
  const { seenUrls, compRole } = await loadDedup();
  const roles = ["Data Analyst", "Data Scientist", "Business Intelligence Analyst", "Analytics Engineer", "Business Analyst"];
  // LinkedIn search locations are derived from your enabled regions in
  // config/locations.json — nothing about your target market is hardcoded here.
  const liLocs = [];
  { const seen = new Set();
    for (const r of (locations.regions || []).filter((r) => r.enabled !== false)) {
      const s = (r.cities || []).length && r.cities.length <= 2 ? `${r.cities[0]}, ${r.country || r.label}` : (r.country || r.label);
      const k = (s || "").toLowerCase();
      if (s && !seen.has(k)) { seen.add(k); liLocs.push(s); }
    }
    if (!liLocs.length) liLocs.push("Remote");
  }

  const stats = { found: 0, title: 0, location: 0, dup: 0, added: 0, apiOk: [], apiFail: [], custom: [] };
  const candidates = [];

  // ---- user-added custom sources (config/sources.json) ----
  // ATS boards → scan directly. Careers pages → discover an embedded ATS board.
  // LinkedIn search URLs → extract keywords+location into extra LinkedIn queries.
  const liExtra = [];
  for (const s of custom) {
    const kind = s.kind || (/greenhouse|lever|smartrecruiters|ashbyhq|recruitee\.com|personio/i.test(s.url || "") ? "ats" : /linkedin/i.test(s.url || "") ? "linkedin" : "careers");
    if (kind === "ats") {
      companies.push({ name: s.name || "Custom", careers: s.url, api: "" });
      stats.custom.push(`${s.name}(ats)`);
    } else if (kind === "linkedin") {
      try {
        const u = new URL(s.url);
        const kw = u.searchParams.get("keywords");
        const loc = u.searchParams.get("location");
        if (kw) { liExtra.push({ role: kw, loc: loc || liLocs[0] }); stats.custom.push(`${s.name}(linkedin)`); }
      } catch { /* not a parseable URL */ }
    } else {
      // 1) look for an ATS board embedded in the careers page HTML
      let found = await discoverAts(s.url);
      // 2) fallback: guess a slug from the name and probe every ATS provider
      if (!found) found = await atsBySlug(slugify(s.name || ""));
      if (found) { companies.push({ name: s.name || "Custom", careers: found, api: "" }); stats.custom.push(`${s.name}(careers→ats)`); }
      else stats.custom.push(`${s.name}(no-ats-found)`);
    }
  }

  // ATS APIs
  for (const c of companies) {
    const ats = atsFor(c);
    if (!ats || !ats.slug) continue;
    let rows = null;
    try { rows = await ats.fetch(); } catch { rows = null; }
    if (rows && rows.length) { stats.apiOk.push(`${c.name}(${ats.kind}:${rows.length})`); candidates.push(...rows); }
    else stats.apiFail.push(`${c.name}(${ats.kind}:${ats.slug})`);
  }
  // LinkedIn guest
  let li = [];
  try { li = await fromLinkedIn(roles, liLocs); } catch {}
  candidates.push(...li);
  stats.linkedin = li.length;
  // extra LinkedIn searches from custom sources
  for (const ex of liExtra) {
    try { const more = await fromLinkedIn([ex.role], [ex.loc]); candidates.push(...more); stats.linkedin += more.length; } catch {}
  }

  // filter + dedup
  const decode = (s) => (s || "").replace(/&amp;/g, "&").replace(/&#8211;/g, "–").replace(/&#x27;|&#39;/g, "'").replace(/&quot;/g, '"').replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim();
  const newOffers = [];
  const seenThisRun = new Set();
  const seenCompRoleRun = new Set();
  for (const o of candidates) {
    stats.found++;
    o.title = decode(o.title); o.company = decode(o.company); o.location = decode(o.location);
    if (!o.url || !o.title) continue;
    const url = o.url.split("?")[0];
    if (seenThisRun.has(url)) continue;
    if (!titleOk(o.title, positive, negative)) { stats.title++; continue; }
    const loc = locationOk(o.location, locations);
    if (!loc.ok) { stats.location++; continue; }
    if (seenUrls.has(url)) { stats.dup++; continue; }
    const cr = normCompany(o.company) + "::" + normRole(o.title);
    if (compRole.has(cr) || seenCompRoleRun.has(cr)) { stats.dup++; continue; } // already tracked OR same company+role this run (kills province-spam)
    seenThisRun.add(url);
    seenCompRoleRun.add(cr);
    newOffers.push({ ...o, url, locWhy: loc.why });
  }

  // write
  if (newOffers.length) {
    const pipePath = join(ROOT, "data", "pipeline.md");
    let pipe = await readFile(pipePath, "utf8");
    const lines = newOffers.map((o) => `- [ ] ${o.url} | ${o.company} | ${o.title} | ${o.location || "?"}${o.locWhy === "unknown" ? " (location?)" : ""}`).join("\n");
    pipe = pipe.replace(/(##\s*Pendientes\s*\n(?:<!--[\s\S]*?-->\n)?)/, `$1${lines}\n`);
    await writeFile(pipePath, pipe, "utf8");
    const hist = newOffers.map((o) => `${o.url}\t${TODAY}\tlocal-scan\t${o.title}\t${o.company}\tadded`).join("\n") + "\n";
    await appendFile(join(ROOT, "data", "scan-history.tsv"), hist, "utf8");
    stats.added = newOffers.length;
  }

  // summary
  console.log(`\n=== Local scan ${TODAY} ===`);
  if (stats.custom.length) console.log(`Custom sources: ${stats.custom.join(", ")}`);
  console.log(`ATS APIs OK: ${stats.apiOk.join(", ") || "none"}`);
  if (stats.apiFail.length) console.log(`ATS failed: ${stats.apiFail.join(", ")}`);
  console.log(`LinkedIn guest cards: ${stats.linkedin}`);
  console.log(`Candidates: ${stats.found} | skipped title: ${stats.title} | skipped location: ${stats.location} | dedup: ${stats.dup}`);
  console.log(`>>> NEW in-scope added to pipeline: ${stats.added}`);
  const byLoc = {};
  for (const o of newOffers) { const k = /remote/i.test(o.location) ? "Remote" : ((o.location || "").split(",").pop().trim() || "Other"); (byLoc[k] ||= []).push(o); }
  for (const [k, arr] of Object.entries(byLoc)) {
    console.log(`\n  [${k}] ${arr.length}`);
    arr.slice(0, 40).forEach((o) => console.log(`   • ${o.company} | ${o.title} | ${o.location}`));
  }
}

main().catch((e) => { console.error("scan error:", e); process.exit(1); });
