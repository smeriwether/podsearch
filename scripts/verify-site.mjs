import { readFile, readdir, stat } from "node:fs/promises";
import { join } from "node:path";

const root = new URL("../", import.meta.url).pathname;
const required = [
  "public/index.html",
  "public/styles.css",
  "public/app.js",
  "public/db-worker.js",
  "public/podcasts/index.html",
  "public/favorites/index.html",
  "public/podcast/index.html",
  "public/episode/index.html",
  "public/episodes/index.html",
  "public/search/index.html",
  "public/vendor/sqlite/index.mjs",
  "public/vendor/sqlite/sqlite3.wasm",
  "public/data/catalog.sqlite3",
  "public/data/catalog.sqlite3.gz",
];

for (const name of required) {
  const details = await stat(join(root, name));
  if (!details.isFile() || details.size === 0) {
    throw new Error(`Missing or empty build artifact: ${name}`);
  }
}

const transcriptFiles = await readdir(join(root, "public/data/transcripts"));
if (!transcriptFiles.some((name) => name.endsWith(".sqlite3"))) {
  throw new Error("Build is missing transcript SQLite shards");
}
if (!transcriptFiles.some((name) => name.endsWith(".sqlite3.gz"))) {
  throw new Error("Build is missing compressed transcript SQLite shards");
}

const html = await readFile(join(root, "public/index.html"), "utf8");
for (const marker of ["Podsearch", "All Podcasts", "My Favorites", "MeriMeriMeri Software"]) {
  if (!html.includes(marker)) throw new Error(`index.html is missing ${marker}`);
}

const app = await readFile(join(root, "public/app.js"), "utf8");
for (const marker of [
  "Transcript pending.",
  "Transcript not scheduled.",
  "Former Top 100 podcast",
  "archive-loading",
  "showCard(show, true)",
  "showFavoriteBadge && show.favorite",
  "left.name.localeCompare(right.name",
  "left.apple_rank",
  "left.favorite_order",
  "if (left.apple_rank) return -1",
  "Outside Top 200",
  "show.in_top_100 && show.chart_rank",
  "limit: 20",
  "All episodes →",
  "perPage: 50",
  "renderAllEpisodesPage",
]) {
  if (!app.includes(marker)) throw new Error(`app.js is missing ${marker}`);
}
if (!app.includes('showPendingLabel && !row.has_transcript')) {
  throw new Error("All Episodes should label only episodes without transcripts");
}
if (app.toLocaleLowerCase().includes("transcript ready")) {
  throw new Error("Transcript-ready labels should not be shown");
}
if (app.includes("episode-status")) {
  throw new Error("Podcast episode lists should not show status pills");
}
if (app.includes('type="submit">Search</button>')) {
  throw new Error("Search bars should submit with Enter and not show a button");
}
if (app.includes('id="show-filter"')) {
  throw new Error("The homepage should not show a podcast filter");
}
if (app.includes("filtered.map(showCard)")) {
  throw new Error("Directory cards must not pass Array.map indexes as compact flags");
}

const worker = await readFile(join(root, "public/db-worker.js"), "utf8");
for (const marker of [
  'const CATALOG_URL = "/data/catalog.sqlite3"',
  "transcriptShards",
  "transcript_search",
  "database.close()",
  "searchTerms(normalized)",
  "episodesPage(data)",
]) {
  if (!worker.includes(marker)) {
    throw new Error(`db-worker.js is missing sharded-search behavior: ${marker}`);
  }
}
if (worker.includes('"/data/podsearch.sqlite3"')) {
  throw new Error("db-worker.js should not load the legacy monolithic database");
}
if (!worker.includes("const DATABASE_CACHE_TTL_MS = 6 * 60 * 60 * 1000")) {
  throw new Error("db-worker.js should cache databases for six hours");
}

console.log("site verification passed");
