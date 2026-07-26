import sqlite3InitModule from "/vendor/sqlite/index.mjs";
import { installHttpVfs, RemoteFileChanged } from "/http-vfs.js?v=__ASSET_VERSION__";

const MANIFEST_URL = "/data/manifest.json";
const OPFS_VFS_NAME = "podsearch-opfs";
const CATALOG_FILE = "/catalog.sqlite3";
const EXPECTED_SCHEMA = 7;
// Only the catalog is downloaded. Everything else is read page by page over
// HTTP ranges, so these stay open to keep their block caches warm.
const MAX_OPEN_REMOTE = 4;
const OPFS_ATTEMPTS = 3;
const OPFS_RETRY_MS = 200;

let sqlite3;
let catalogDb;
let manifest;
let openRemote;
let opfsPool = null;
let catalogSource = "network";
let opfsPaused = false;
const remoteDatabases = new Map();

let activeSearchToken = 0;
let activeSearchRequestId = null;

const post = (type, payload = {}) => self.postMessage({ type, ...payload });

class SearchCancelled extends Error {}

// --------------------------------------------------------------------------
// Startup
// --------------------------------------------------------------------------

async function initialize() {
  sqlite3 = await sqlite3InitModule({ print: () => {}, printErr: () => {} });
  openRemote = installHttpVfs(sqlite3);
  manifest = await fetchManifest();
  if (manifest.schema_version !== EXPECTED_SCHEMA) {
    throw new Error(
      `This page is out of date (archive format ${manifest.schema_version}). Reload to continue.`,
    );
  }

  opfsPool = await openOpfsPool();
  catalogDb = await openCatalog();

  const meta = Object.fromEntries(
    selectObjects(catalogDb, "SELECT key, value FROM meta").map(({ key, value }) => [
      key,
      value,
    ]),
  );
  const shows = selectObjects(
    catalogDb,
    `
      SELECT id, name, artist, artwork_url, chart_rank, apple_rank, in_top_100,
             ever_top_100, favorite, favorite_order,
             (SELECT COUNT(*) FROM episodes WHERE show_id = shows.id) AS episode_count,
             (SELECT COUNT(*) FROM episodes
                WHERE show_id = shows.id AND has_transcript = 1) AS transcript_count
      FROM shows
      ORDER BY
        apple_rank IS NULL, apple_rank,
        favorite_order IS NULL, favorite_order,
        chart_rank IS NULL, chart_rank,
        id
    `,
  );
  post("ready", {
    meta,
    shows,
    source: catalogSource,
    bytes: manifest.catalog.bytes,
    shardCount: manifest.shards.length,
  });
}

async function fetchManifest() {
  const response = await fetch(MANIFEST_URL, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Could not load the archive index (HTTP ${response.status})`);
  }
  return response.json();
}

async function openOpfsPool() {
  // The pool takes exclusive sync access handles, so following a link can
  // briefly race the previous page's worker, which has not released them yet.
  // A couple of short retries turn that transient loss into a cache hit
  // instead of a needless multi-megabyte download.
  for (let attempt = 0; attempt < OPFS_ATTEMPTS; attempt += 1) {
    try {
      // opfs-sahpool is deliberate: the plain OPFS VFS needs cross-origin
      // isolation, which would require COEP headers that break the podcast
      // artwork loaded from Apple's CDN.
      const pool = await sqlite3.installOpfsSAHPoolVfs({
        name: OPFS_VFS_NAME,
        forceReinitIfPreviouslyFailed: attempt > 0,
      });
      await pool.reserveMinimumCapacity(4);
      return pool;
    } catch {
      if (attempt < OPFS_ATTEMPTS - 1) {
        await new Promise((resolve) => setTimeout(resolve, OPFS_RETRY_MS));
      }
    }
  }
  // Private windows and browsers without OPFS land here and simply pay the
  // download on every visit.
  return null;
}

/**
 * Open the catalog, reusing the copy already in OPFS when the manifest says it
 * is current. A repeat visit then costs one small manifest request instead of
 * a multi-megabyte download.
 */
async function openCatalog() {
  const bytes = (await catalogBytesFromOpfs()) || (await downloadAndStoreCatalog());
  return deserializeDatabase(bytes);
}

/** Return the stored catalog if OPFS holds the revision the manifest names. */
async function catalogBytesFromOpfs() {
  if (!opfsPool || !opfsPool.getFileNames().includes(CATALOG_FILE)) return null;
  let db;
  try {
    db = new sqlite3.oo1.DB({ filename: CATALOG_FILE, vfs: opfsPool.vfsName });
    const stored = selectObjects(db, "SELECT value FROM meta WHERE key = 'version'")[0];
    db.close();
    db = null;
    if (stored?.value !== manifest.catalog.version) {
      opfsPool.unlink(CATALOG_FILE);
      return null;
    }
    const bytes = opfsPool.exportFile(CATALOG_FILE);
    catalogSource = "opfs";
    return bytes;
  } catch {
    return null;
  } finally {
    try {
      db?.close();
    } catch {
      // ignore
    }
    releaseOpfs();
  }
}

async function downloadAndStoreCatalog() {
  const bytes = await downloadCatalog();
  catalogSource = "network";
  if (opfsPool) {
    try {
      opfsPool.importDb(CATALOG_FILE, bytes);
    } catch {
      // Storing is an optimisation; the download already succeeded.
    }
    releaseOpfs();
  }
  return bytes;
}

async function downloadCatalog() {
  const response = await fetch(manifest.catalog.path, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`Catalog download failed with HTTP ${response.status}`);
  }
  return readWithProgress(response, manifest.catalog.bytes);
}

async function readWithProgress(response, expectedBytes) {
  if (!response.body) return new Uint8Array(await response.arrayBuffer());
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  let lastPosted = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    // The wire carries gzip, so `received` runs ahead of the compressed size;
    // clamp so the reported fraction never exceeds one.
    if (received - lastPosted > 65536) {
      lastPosted = received;
      post("loading", {
        received,
        total: expectedBytes,
        ratio: expectedBytes ? Math.min(1, received / expectedBytes) : 0,
      });
    }
  }
  const bytes = new Uint8Array(received);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function deserializeDatabase(bytes) {
  const pointer = sqlite3.wasm.allocFromTypedArray(bytes);
  const database = new sqlite3.oo1.DB();
  database.onclose = { after: () => sqlite3.wasm.dealloc(pointer) };
  database.checkRc(
    sqlite3.capi.sqlite3_deserialize(
      database.pointer,
      "main",
      pointer,
      bytes.byteLength,
      bytes.byteLength,
      0,
    ),
  );
  return database;
}

/**
 * Give the OPFS pool back as soon as startup is done.
 *
 * opfs-sahpool takes exclusive sync access handles for as long as it is
 * installed, and a dedicated worker survives in the back/forward cache. Holding
 * the pool for the worker's lifetime therefore locked out the next same-origin
 * page, which then re-downloaded the catalog. This is a multi-page site, so the
 * pool is used only as durable storage: read the bytes, hand it straight back,
 * and run the catalog from memory.
 */
function releaseOpfs() {
  if (!opfsPool || opfsPaused) return;
  try {
    opfsPool.pauseVfs();
    opfsPaused = true;
  } catch {
    // Something still holds a file; the next page falls back to the network.
  }
}

async function resumeOpfs() {
  if (!opfsPool || !opfsPaused) return;
  try {
    await opfsPool.unpauseVfs();
    opfsPaused = false;
  } catch {
    opfsPool = null;
  }
}

// --------------------------------------------------------------------------
// Remote databases
// --------------------------------------------------------------------------

function remote(url) {
  const cached = remoteDatabases.get(url);
  if (cached) {
    remoteDatabases.delete(url);
    remoteDatabases.set(url, cached);
    return cached;
  }
  const db = openRemote(url);
  remoteDatabases.set(url, db);
  while (remoteDatabases.size > MAX_OPEN_REMOTE) {
    const oldest = remoteDatabases.keys().next().value;
    closeRemote(oldest);
  }
  return db;
}

function closeRemote(url) {
  const db = remoteDatabases.get(url);
  remoteDatabases.delete(url);
  try {
    db?.close();
  } catch {
    // ignore
  }
}

/**
 * Run a query against a remote database, tolerating the file being republished
 * mid-read. The backfill rebuilds these files constantly, so a read that spans
 * a swap has to be retried against the new revision rather than surfacing a
 * corruption error.
 */
async function withRemote(urlFor, run) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const url = urlFor();
    const db = remote(url);
    try {
      return run(db);
    } catch (error) {
      const cause = db.takeRemoteError?.() || error;
      closeRemote(url);
      if (attempt === 0 && cause instanceof RemoteFileChanged) {
        manifest = await fetchManifest();
        continue;
      }
      throw cause;
    }
  }
  throw new Error("The archive changed while it was being read. Please try again.");
}

const detailsUrl = () => manifest.details.path;
const searchUrl = () => manifest.search.path;
const shardUrl = (key) => {
  const shard = manifest.shards.find((entry) => entry.key === key);
  if (!shard) throw new Error(`Transcript archive ${key} is unavailable`);
  return shard.path;
};

// --------------------------------------------------------------------------
// Query helpers
// --------------------------------------------------------------------------

function selectObjects(database, sql, bind = []) {
  const resultRows = [];
  database.exec({ sql, bind, rowMode: "object", resultRows });
  return resultRows;
}

function searchTerms(input) {
  return input.normalize("NFKC").match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || [];
}

/** Mirrors fold_search_text() in site.py so LIKE comparisons are symmetric. */
function foldSearchText(value) {
  return String(value ?? "")
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .normalize("NFKC")
    .toLowerCase();
}

function ftsQuery(input) {
  const terms = searchTerms(input);
  if (!terms.length) return "";
  return terms
    .slice(0, 12)
    .map((term) => `"${term.replaceAll('"', '""')}"*`)
    .join(" AND ");
}

// --------------------------------------------------------------------------
// Catalog queries
// --------------------------------------------------------------------------

const EPISODE_COLUMNS = `
  e.id, e.title, e.published_at, e.duration, e.has_transcript, e.has_detail,
  e.transcript_shard, s.id AS show_id, s.name AS show_name, s.artwork_url,
  s.chart_rank, s.apple_rank, s.in_top_100, s.favorite, s.favorite_order
`;

function latestTranscripts({ limit = 30, offset = 0 }) {
  return selectObjects(
    catalogDb,
    `
      SELECT ${EPISODE_COLUMNS},
             COALESCE(NULLIF(e.snippet, ''), 'Open the full transcript.') AS snippet,
             0 AS score
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      WHERE e.has_transcript = 1
      ORDER BY e.published_at DESC, e.id DESC
      LIMIT ? OFFSET ?
    `,
    [Number(limit), Number(offset)],
  );
}

function episodesPage({ page = 1, perPage = 50 }) {
  const safePerPage = Math.min(50, Math.max(1, Number(perPage) || 50));
  const total = Number(
    selectObjects(catalogDb, "SELECT COUNT(*) AS total FROM episodes")[0]?.total || 0,
  );
  const totalPages = Math.max(1, Math.ceil(total / safePerPage));
  const safePage = Math.min(totalPages, Math.max(1, Number(page) || 1));
  const rows = selectObjects(
    catalogDb,
    `
      SELECT ${EPISODE_COLUMNS},
             COALESCE(NULLIF(e.snippet, ''), 'Open episode details.') AS snippet
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      ORDER BY e.published_at IS NULL, e.published_at DESC, e.id DESC
      LIMIT ? OFFSET ?
    `,
    [safePerPage, (safePage - 1) * safePerPage],
  );
  return { rows, total, page: safePage, totalPages, perPage: safePerPage };
}

function show(id) {
  const details = selectObjects(
    catalogDb,
    `
      SELECT s.*,
             (SELECT COUNT(*) FROM episodes WHERE show_id = s.id) AS episode_count,
             (SELECT COUNT(*) FROM episodes
                WHERE show_id = s.id AND has_transcript = 1) AS transcript_count
      FROM shows s
      WHERE s.id = ?
    `,
    [Number(id)],
  )[0];
  if (!details) return null;
  details.episodes = selectObjects(
    catalogDb,
    `
      SELECT id, title, snippet, published_at, duration, has_transcript
      FROM episodes
      WHERE show_id = ?
      ORDER BY published_at DESC, id DESC
    `,
    [Number(id)],
  );
  return details;
}

function searchShows(terms) {
  if (!terms.length) return [];
  const where = terms.map(() => "search_text LIKE ? ESCAPE '\\'").join(" AND ");
  return selectObjects(
    catalogDb,
    `
      SELECT id, name, artist, description, artwork_url, chart_rank, apple_rank,
             in_top_100, ever_top_100, favorite, favorite_order,
             (SELECT COUNT(*) FROM episodes WHERE show_id = shows.id) AS episode_count,
             (SELECT COUNT(*) FROM episodes
                WHERE show_id = shows.id AND has_transcript = 1) AS transcript_count
      FROM shows
      WHERE ${where}
      ORDER BY in_top_100 DESC, chart_rank IS NULL, chart_rank, name
      LIMIT 24
    `,
    terms.map((term) => `%${escapeLike(foldSearchText(term))}%`),
  );
}

function escapeLike(value) {
  return value.replace(/[\\%_]/g, (character) => `\\${character}`);
}

function searchEpisodeMetadata(match, limit) {
  return selectObjects(
    catalogDb,
    `
      SELECT episode_search.rowid AS id,
             snippet(episode_search, 2, char(1), char(2), ' … ', 30) AS snippet,
             bm25(episode_search, 8.0, 4.0, 1.0) AS score
      FROM episode_search
      WHERE episode_search MATCH ?
      ORDER BY score
      LIMIT ?
    `,
    [match, Number(limit)],
  );
}

// --------------------------------------------------------------------------
// Search
// --------------------------------------------------------------------------

/**
 * One descent into the global contentless index replaces the old fan-out that
 * downloaded every monthly shard.
 */
function searchTranscripts(match, limit) {
  return withRemote(searchUrl, (db) =>
    selectObjects(
      db,
      `
        SELECT rowid AS id, bm25(transcript_search, 8.0, 4.0, 1.0) AS score
        FROM transcript_search
        WHERE transcript_search MATCH ?
        ORDER BY score
        LIMIT ?
      `,
      [match, Number(limit)],
    ),
  );
}

/**
 * Snippets come from the monthly shards, but only for the handful of results
 * actually being rendered.
 */
async function snippetsForHits(hits, match, requestId, searchToken) {
  const byShard = new Map();
  for (const hit of hits) {
    if (!hit.transcript_shard) continue;
    if (!byShard.has(hit.transcript_shard)) byShard.set(hit.transcript_shard, []);
    byShard.get(hit.transcript_shard).push(Number(hit.id));
  }
  const snippets = new Map();
  let completed = 0;
  for (const [key, ids] of byShard) {
    if (searchToken !== activeSearchToken) throw new SearchCancelled();
    post("search-progress", {
      requestId,
      completed,
      total: byShard.size,
      shard: key,
    });
    const placeholders = ids.map(() => "?").join(",");
    const rows = await withRemote(
      () => shardUrl(key),
      (db) =>
        selectObjects(
          db,
          `
            SELECT rowid AS id,
                   snippet(transcript_search, 2, char(1), char(2), ' … ', 30) AS snippet
            FROM transcript_search
            WHERE transcript_search MATCH ? AND rowid IN (${placeholders})
          `,
          [match, ...ids],
        ),
    );
    for (const row of rows) snippets.set(Number(row.id), row.snippet);
    completed += 1;
    post("search-progress", {
      requestId,
      completed,
      total: byShard.size,
      shard: key,
    });
  }
  return snippets;
}

function episodeDetailsFor(ids) {
  if (!ids.length) return new Map();
  const placeholders = ids.map(() => "?").join(",");
  const rows = selectObjects(
    catalogDb,
    `
      SELECT ${EPISODE_COLUMNS}, e.snippet AS fallback_snippet
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      WHERE e.id IN (${placeholders})
    `,
    ids.map(Number),
  );
  return new Map(rows.map((row) => [row.id, row]));
}

async function globalSearch({ query = "", limit = 80, requestId }, searchToken) {
  const match = ftsQuery(query.trim());
  if (!match) return { shows: [], episodes: [] };

  const shows = searchShows(searchTerms(query.trim()).slice(0, 12));
  const metadataHits = searchEpisodeMetadata(match, limit);
  const transcriptHits = await searchTranscripts(match, limit);
  if (searchToken !== activeSearchToken) throw new SearchCancelled();

  const best = new Map();
  for (const hit of [...metadataHits, ...transcriptHits]) {
    const id = Number(hit.id);
    const existing = best.get(id);
    if (!existing || Number(hit.score) < Number(existing.score)) {
      best.set(id, { id, score: Number(hit.score), snippet: hit.snippet || null });
    }
  }
  const ranked = [...best.values()]
    .sort((left, right) => left.score - right.score)
    .slice(0, Number(limit));
  if (!ranked.length) return { shows, episodes: [] };

  const details = episodeDetailsFor(ranked.map((hit) => hit.id));
  const withShards = ranked
    .map((hit) => ({ ...hit, ...details.get(hit.id) }))
    .filter((row) => row.id !== undefined);

  // Only transcript hits that lack a metadata snippet need shard access.
  const needSnippets = withShards.filter((row) => !row.snippet && row.has_transcript);
  const snippets = await snippetsForHits(needSnippets, match, requestId, searchToken);
  if (searchToken !== activeSearchToken) throw new SearchCancelled();

  const episodes = withShards
    .map((row) => ({
      ...row,
      snippet:
        row.snippet ||
        snippets.get(Number(row.id)) ||
        row.fallback_snippet ||
        "Open this result.",
    }))
    .sort(
      (left, right) =>
        left.score - right.score ||
        String(right.published_at || "").localeCompare(String(left.published_at || "")),
    );
  return { shows, episodes };
}

// --------------------------------------------------------------------------
// Episode page
// --------------------------------------------------------------------------

async function episode(id, requestId) {
  const details = selectObjects(
    catalogDb,
    `
      SELECT e.*, s.name AS show_name, s.artist, s.apple_url,
             s.artwork_url AS show_artwork_url, s.chart_rank, s.apple_rank,
             s.in_top_100, s.ever_top_100, s.favorite, s.favorite_order
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      WHERE e.id = ?
    `,
    [Number(id)],
  )[0];
  if (!details) return null;

  if (details.has_detail) {
    post("episode-progress", { requestId, stage: "details" });
    const rows = await withRemote(detailsUrl, (db) =>
      selectObjects(
        db,
        "SELECT description, episode_url, image_url FROM details WHERE episode_id = ?",
        [Number(id)],
      ),
    );
    Object.assign(details, rows[0] || {});
  }

  if (details.has_transcript && details.transcript_shard) {
    post("episode-progress", { requestId, stage: "transcript" });
    const rows = await withRemote(
      () => shardUrl(details.transcript_shard),
      (db) =>
        selectObjects(
          db,
          "SELECT transcript_text FROM transcripts WHERE episode_id = ?",
          [Number(id)],
        ),
    );
    if (!rows.length) throw new Error("Transcript is missing from its archive shard");
    details.transcript_text = rows[0].transcript_text;
  }
  return details;
}

// --------------------------------------------------------------------------
// Message loop
// --------------------------------------------------------------------------

self.onmessage = async ({ data }) => {
  try {
    if (!catalogDb) throw new Error("Podcast catalog is not ready");
    if (data.type === "search") {
      post("search-results", {
        requestId: data.requestId,
        rows: latestTranscripts(data),
      });
    } else if (data.type === "global-search") {
      if (activeSearchRequestId !== null) {
        post("search-cancelled", { requestId: activeSearchRequestId });
      }
      activeSearchRequestId = data.requestId;
      const searchToken = ++activeSearchToken;
      const results = await globalSearch(data, searchToken);
      if (searchToken !== activeSearchToken) throw new SearchCancelled();
      activeSearchRequestId = null;
      post("global-search-results", { requestId: data.requestId, results });
    } else if (data.type === "episodes") {
      post("episodes-results", {
        requestId: data.requestId,
        results: episodesPage(data),
      });
    } else if (data.type === "show") {
      post("show", { requestId: data.requestId, show: show(data.id) });
    } else if (data.type === "episode") {
      post("episode", {
        requestId: data.requestId,
        episode: await episode(data.id, data.requestId),
      });
    }
  } catch (error) {
    if (error instanceof SearchCancelled) return;
    if (data.requestId === activeSearchRequestId) activeSearchRequestId = null;
    post("error", {
      requestId: data.requestId,
      message: error instanceof Error ? error.message : String(error),
    });
  }
};

initialize().catch((error) => {
  post("error", { message: error instanceof Error ? error.message : String(error) });
});
