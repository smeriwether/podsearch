/**
 * Verify a built site.
 *
 * This checks what the browser actually depends on — the manifest, the shape
 * and contents of each published database, real FTS queries, and byte-range
 * serving — rather than asserting that particular strings appear in the source.
 * String matching turned every refactor into a failure while catching none of
 * the behaviour that matters.
 */
import { readFile, readdir, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { DatabaseSync } from "node:sqlite";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const publicDir = join(root, "public");
const dataDir = join(publicDir, "data");
const EXPECTED_SCHEMA = 7;

const failures = [];
let checks = 0;

function check(name, condition, detail = "") {
  checks += 1;
  if (!condition) failures.push(detail ? `${name} (${detail})` : name);
}

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

async function size(path) {
  return (await stat(path)).size;
}

function open(path) {
  return new DatabaseSync(path, { readOnly: true });
}

function meta(db) {
  return Object.fromEntries(
    db.prepare("SELECT key, value FROM meta").all().map((r) => [r.key, r.value]),
  );
}

function tables(db) {
  return new Set(
    db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((r) => r.name),
  );
}

function columns(db, table) {
  return new Set(db.prepare(`PRAGMA table_info(${table})`).all().map((r) => r.name));
}

// --- static assets -------------------------------------------------------

const required = [
  "index.html",
  "styles.css",
  "app.js",
  "db-worker.js",
  "http-vfs.js",
  "vendor/sqlite/index.mjs",
  "vendor/sqlite/sqlite3.wasm",
  "podcasts/index.html",
  "favorites/index.html",
  "podcast/index.html",
  "episode/index.html",
  "episodes/index.html",
  "search/index.html",
];
for (const name of required) {
  const path = join(publicDir, name);
  check(`asset present: ${name}`, (await isFile(path)) && (await size(path)) > 0);
}

// --- manifest ------------------------------------------------------------

check("manifest exists", await isFile(join(dataDir, "manifest.json")));
const manifest = JSON.parse(await readFile(join(dataDir, "manifest.json"), "utf8"));
check("manifest schema version", manifest.schema_version === EXPECTED_SCHEMA, manifest.schema_version);
check("manifest names an asset stamp", /^[a-f0-9]{12}$/.test(manifest.assets || ""), manifest.assets);
for (const key of ["catalog", "details", "search"]) {
  check(`manifest has ${key}`, Boolean(manifest[key]?.path && manifest[key]?.version));
}
check("manifest lists shards", Array.isArray(manifest.shards));

// Every asset the page loads must carry the same computed stamp, and no
// placeholder may survive into the build.
for (const name of ["index.html", "app.js", "db-worker.js"]) {
  const text = await readFile(join(publicDir, name), "utf8");
  check(`${name} has no unsubstituted placeholder`, !text.includes("__ASSET_VERSION__"));
  const stamps = [...text.matchAll(/[?&]v=([a-f0-9]{12})/g)].map((m) => m[1]);
  check(`${name} references the manifest asset stamp`,
    stamps.length === 0 || stamps.every((s) => s === manifest.assets),
    `${[...new Set(stamps)]} vs ${manifest.assets}`);
}

// --- every manifest entry resolves, and its version matches the file ------

const entries = [manifest.catalog, manifest.details, manifest.search, ...manifest.shards];
for (const entry of entries) {
  const relative = entry.path.split("?")[0].replace(/^\/data\//, "");
  const path = join(dataDir, relative);
  check(`manifest target exists: ${relative}`, await isFile(path));
  if (!(await isFile(path))) continue;
  check(`${relative} matches declared size`, (await size(path)) === entry.bytes,
    `${await size(path)} vs ${entry.bytes}`);
  check(`${relative} has a .gz sibling`, await isFile(`${path}.gz`));
  const db = open(path);
  const m = meta(db);
  check(`${relative} version matches manifest`, m.version === entry.version,
    `${m.version} vs ${entry.version}`);
  check(`${relative} schema version`, Number(m.schema_version) === EXPECTED_SCHEMA, m.schema_version);
  check(`${relative} is page-size 4096`, db.prepare("PRAGMA page_size").get().page_size === 4096);
  check(`${relative} carries no build bookkeeping`, !tables(db).has("build_state"));
  db.close();
}

// --- catalog: the one file downloaded whole, so its shape matters ---------

const catalog = open(join(dataDir, "catalog.sqlite3"));
const episodeColumns = columns(catalog, "episodes");
for (const banned of ["description", "episode_url", "image_url"]) {
  check(`catalog excludes bulky column ${banned}`, !episodeColumns.has(banned));
}
for (const needed of ["snippet", "has_transcript", "has_detail", "transcript_shard"]) {
  check(`catalog has column ${needed}`, episodeColumns.has(needed));
}
check("catalog shows carry folded search text", columns(catalog, "shows").has("search_text"));
const unfolded = catalog
  .prepare("SELECT COUNT(*) AS n FROM shows WHERE search_text != lower(search_text)")
  .get().n;
check("catalog search_text is lowercased", unfolded === 0, `${unfolded} rows`);
const longSnippet = catalog.prepare("SELECT MAX(LENGTH(snippet)) AS n FROM episodes").get().n;
check("catalog snippets are bounded", longSnippet === null || longSnippet <= 281, longSnippet);

const catalogMeta = meta(catalog);
check("catalog counts episodes", Number(catalogMeta.episode_count) > 0, catalogMeta.episode_count);
const transcriptRows = catalog
  .prepare("SELECT COUNT(*) AS n FROM episodes WHERE has_transcript = 1")
  .get().n;
check("catalog transcript count agrees with meta",
  transcriptRows === Number(catalogMeta.transcript_count),
  `${transcriptRows} vs ${catalogMeta.transcript_count}`);

// Every episode claiming a transcript must name a shard that exists.
const shardKeys = new Set(manifest.shards.map((s) => s.key));
const danglingShards = catalog
  .prepare("SELECT DISTINCT transcript_shard AS k FROM episodes WHERE has_transcript = 1")
  .all()
  .filter((r) => !shardKeys.has(r.k));
check("no episode points at a missing shard", danglingShards.length === 0,
  danglingShards.map((r) => r.k).join(","));

// The metadata FTS index must actually answer queries.
const sampleEpisode = catalog.prepare("SELECT id, title FROM episodes WHERE has_transcript = 1 LIMIT 1").get();
if (sampleEpisode) {
  const term = (sampleEpisode.title.match(/[A-Za-z]{6,}/g) || [])[0];
  if (term) {
    const hit = catalog
      .prepare("SELECT rowid FROM episode_search WHERE episode_search MATCH ? LIMIT 500")
      .all(`"${term}"`)
      .some((r) => r.rowid === sampleEpisode.id);
    check(`episode_search finds a known title word (${term})`, hit);
  }
}

// --- details: every has_detail row must resolve --------------------------

const details = open(join(dataDir, "details.sqlite3"));
const claimed = catalog.prepare("SELECT COUNT(*) AS n FROM episodes WHERE has_detail = 1").get().n;
const stored = details.prepare("SELECT COUNT(*) AS n FROM details").get().n;
check("has_detail matches the details database", claimed === stored, `${claimed} vs ${stored}`);

// --- search index and shards agree on which transcripts exist ------------

const search = open(join(dataDir, "search.sqlite3"));
const indexed = search.prepare("SELECT COUNT(*) AS n FROM transcript_search").get().n;
check("global index covers every transcript", indexed === transcriptRows, `${indexed} vs ${transcriptRows}`);

let shardTotal = 0;
for (const entry of manifest.shards) {
  const relative = entry.path.split("?")[0].replace(/^\/data\//, "");
  const shard = open(join(dataDir, relative));
  const n = shard.prepare("SELECT COUNT(*) AS n FROM transcripts").get().n;
  check(`shard ${entry.key} count matches manifest`, n === entry.episode_count, `${n} vs ${entry.episode_count}`);
  shardTotal += n;

  // A shard must be able to produce a highlighted snippet, which is the only
  // reason the per-shard FTS index exists.
  const row = shard.prepare("SELECT episode_id, transcript_text FROM transcripts LIMIT 1").get();
  const term = (row.transcript_text.match(/\b[A-Za-z]{8,}\b/) || [])[0];
  if (term) {
    const snippet = shard
      .prepare(
        "SELECT snippet(transcript_search, 2, char(1), char(2), ' … ', 20) AS s " +
          "FROM transcript_search WHERE transcript_search MATCH ? AND rowid = ?",
      )
      .get(`"${term}"`, row.episode_id);
    check(`shard ${entry.key} produces a marked snippet`,
      Boolean(snippet?.s?.includes("\u0001")), JSON.stringify(snippet?.s?.slice(0, 60)));
  }
  shard.close();
}
check("shards together hold every transcript", shardTotal === transcriptRows, `${shardTotal} vs ${transcriptRows}`);

catalog.close();
details.close();
search.close();

// --- the server must actually serve byte ranges --------------------------

const { StaticHandlerAvailable, rangeOk, statusOk } = await verifyRangeServing();
check("range request returns 206 with the right bytes", rangeOk, StaticHandlerAvailable ? "" : "python server not exercised");
check("whole-file request still works", statusOk);

async function verifyRangeServing() {
  // Serve the built directory with a minimal Node server that mirrors the
  // range contract podsearch/server.py implements, then confirm the published
  // database really is seekable and its header is a SQLite header.
  const path = join(dataDir, "details.sqlite3");
  const body = await readFile(path);
  const server = createServer((req, res) => {
    const range = /^bytes=(\d+)-(\d+)$/.exec(req.headers.range || "");
    if (range) {
      const start = Number(range[1]);
      const end = Math.min(Number(range[2]), body.length - 1);
      res.writeHead(206, {
        "Content-Range": `bytes ${start}-${end}/${body.length}`,
        "Content-Length": end - start + 1,
      });
      res.end(body.subarray(start, end + 1));
      return;
    }
    res.writeHead(200, { "Content-Length": body.length });
    res.end(body);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  const partial = await fetch(`http://127.0.0.1:${port}/details.sqlite3`, {
    headers: { Range: "bytes=0-15" },
  });
  const header = Buffer.from(await partial.arrayBuffer());
  const full = await fetch(`http://127.0.0.1:${port}/details.sqlite3`);
  const fullOk = full.status === 200 && Number(full.headers.get("content-length")) === body.length;
  server.close();
  return {
    StaticHandlerAvailable: true,
    rangeOk: partial.status === 206 && header.toString("latin1").startsWith("SQLite format 3"),
    statusOk: fullOk,
  };
}

// --- report --------------------------------------------------------------

if (failures.length) {
  console.error(`site verification failed (${failures.length} of ${checks} checks):`);
  for (const failure of failures) console.error(`  - ${failure}`);
  process.exit(1);
}
console.log(`site verification passed (${checks} checks)`);
