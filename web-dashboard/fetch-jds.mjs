#!/usr/bin/env node
// Fetch the full JD text for every pending offer in data/pipeline.md and cache it
// to batch/jd-cache.json, so the LLM scoring step doesn't have to hit the network.
// Run with system CA:  node --use-system-ca web-dashboard/fetch-jds.mjs

import { readFile, writeFile, mkdir, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, "..");
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const strip = (h) => (h || "")
  .replace(/<\s*br\s*\/?>/gi, "\n").replace(/<\/(p|li|div|h[1-6])>/gi, "\n")
  .replace(/<[^>]+>/g, " ")
  .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
  .replace(/&#39;|&#x27;/g, "'").replace(/&nbsp;/g, " ").replace(/&#8211;/g, "–").replace(/&#8217;/g, "'")
  .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();

async function getText(url, opts = {}) {
  try {
    const r = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "en", ...(opts.headers || {}) } });
    if (!r.ok) return { ok: false, status: r.status, text: "" };
    return { ok: true, status: r.status, text: await r.text() };
  } catch (e) { return { ok: false, status: 0, text: "", err: e.message }; }
}

async function fetchJD(url) {
  // Greenhouse → board API json {content}
  let m = url.match(/greenhouse\.io\/(?:embed\/job_app\?for=)?([^/]+)\/jobs\/(\d+)/) || url.match(/greenhouse\.io\/([^/]+)\/jobs\/(\d+)/);
  if (url.includes("greenhouse.io") && m) {
    const r = await getText(`https://boards-api.greenhouse.io/v1/boards/${m[1]}/jobs/${m[2]}`);
    if (r.ok) { try { const j = JSON.parse(r.text); if (j.content) return strip(j.content); } catch {} }
  }
  // Lever → posting API json
  m = url.match(/lever\.co\/([^/]+)\/([0-9a-f-]+)/);
  if (url.includes("lever.co") && m) {
    const r = await getText(`https://api.lever.co/v0/postings/${m[1]}/${m[2]}?mode=json`);
    if (r.ok) { try { const j = JSON.parse(r.text); return strip([j.description, ...(j.lists || []).map((l) => l.text + ": " + l.content)].join("\n")); } catch {} }
  }
  // LinkedIn → JSON-LD JobPosting description
  if (url.includes("linkedin.com/jobs/view")) {
    const r = await getText(url);
    if (r.ok) {
      const ld = r.text.match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/);
      if (ld) { try { const j = JSON.parse(ld[1]); if (j.description) return strip(j.description); } catch {} }
      const dm = r.text.match(/show-more-less-html__markup[^>]*>([\s\S]*?)<\/div>/);
      if (dm) return strip(dm[1]);
    }
  }
  // Generic: fetch HTML, strip
  const r = await getText(url);
  if (r.ok) {
    const body = r.text.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<style[\s\S]*?<\/style>/gi, "");
    const t = strip(body);
    if (t.length > 200) return t.slice(0, 6000);
  }
  return "";
}

async function main() {
  const md = await readFile(join(ROOT, "data", "pipeline.md"), "utf8");
  // Flexible header: main project "# Pipeline — Pending URLs", legacy "## Pendientes".
  const hParts = md.split(/#{1,3}\s*(Pipeline|Pending|Pendientes)/i);
  const sec = hParts.length > 2 ? hParts.slice(2).join("").split(/\n#{1,3}\s/)[0] : md;
  const offers = [];
  for (const line of sec.split("\n")) {
    const mm = line.match(/^-\s*\[ \]\s*(\S+)\s*\|\s*(.+)$/);
    if (!mm) continue;
    const parts = mm[2].split("|").map((s) => s.trim());
    offers.push({ url: mm[1], company: parts[0] || "", title: parts[1] || "", location: parts[2] || "" });
  }

  // Next report number = max existing report file number + 1.
  let base = 1;
  try {
    const files = await readdir(join(ROOT, "reports"));
    for (const f of files) { const m = f.match(/^(\d{3})-/); if (m) base = Math.max(base, parseInt(m[1], 10) + 1); }
  } catch {}
  console.log(`Fetching ${offers.length} JDs… (report numbering from ${String(base).padStart(3, "0")})`);
  const out = [];
  let ok = 0, empty = 0;
  for (let i = 0; i < offers.length; i++) {
    const o = offers[i];
    let jd = "";
    try { jd = await fetchJD(o.url); } catch {}
    jd = (jd || "").slice(0, 4000);
    if (jd.length > 150) ok++; else empty++;
    out.push({ idx: i, report: String(base + i).padStart(3, "0"), ...o, jd });
    if ((i + 1) % 20 === 0) console.log(`  …${i + 1}/${offers.length} (ok:${ok} empty:${empty})`);
    await sleep(250);
  }

  await mkdir(join(ROOT, "batch"), { recursive: true });
  await writeFile(join(ROOT, "batch", "jd-cache.json"), JSON.stringify(out, null, 1), "utf8");
  console.log(`\nDone. ${ok} with JD text, ${empty} empty/blocked. Wrote batch/jd-cache.json`);
}
main().catch((e) => { console.error(e); process.exit(1); });
