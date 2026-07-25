import sqlite3InitModule from "/vendor/sqlite/index.mjs";

const CATALOG_URL = "/data/catalog.sqlite3";
const DATABASE_CACHE = "podsearch-database-v9";
const DATABASE_CACHE_TTL_MS = 6 * 60 * 60 * 1000;

let sqlite3;
let catalogDb;
let transcriptShards = [];
let activeSearchToken = 0;
let activeSearchRequestId = null;

const post = (type, payload = {}) => self.postMessage({ type, ...payload });

class SearchCancelled extends Error {}

async function initialize() {
  await deleteLegacyDatabaseCaches();
  sqlite3 = await sqlite3InitModule();
  const { bytes, source } = await loadDatabaseBytes(CATALOG_URL, {
    phase: "catalog",
  });
  catalogDb = deserializeDatabase(bytes);

  const metaRows = selectObjects(catalogDb, "SELECT key, value FROM meta");
  const meta = Object.fromEntries(metaRows.map(({ key, value }) => [key, value]));
  transcriptShards = selectObjects(
    catalogDb,
    `
      SELECT key, path, episode_count, database_bytes, compressed_bytes
      FROM transcript_shards
      ORDER BY key DESC
    `,
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
        apple_rank IS NULL,
        apple_rank,
        favorite_order IS NULL,
        favorite_order,
        chart_rank IS NULL,
        chart_rank,
        id
    `,
  );
  post("ready", {
    meta,
    shows,
    bytes: bytes.byteLength,
    source,
    shardCount: transcriptShards.length,
  });
}

async function deleteLegacyDatabaseCaches() {
  try {
    const cacheNames = await caches.keys();
    await Promise.all(
      cacheNames
        .filter(
          (name) =>
            name.startsWith("podsearch-database-") && name !== DATABASE_CACHE,
        )
        .map((name) => caches.delete(name)),
    );
  } catch {
    // Cache Storage is an optimization; database loading still works without it.
  }
}

async function loadDatabaseBytes(url, progress = {}) {
  let cache;
  try {
    cache = await caches.open(DATABASE_CACHE);
    const cached = await cache.match(url);
    const cachedAt = Number(cached?.headers.get("x-podsearch-cached-at") || 0);
    if (cached && Date.now() - cachedAt < DATABASE_CACHE_TTL_MS) {
      return {
        bytes: new Uint8Array(await cached.arrayBuffer()),
        source: "cache",
      };
    }
  } catch {
    cache = null;
  }

  const response = await fetch(url, { cache: "no-cache" });
  if (!response.ok) {
    throw new Error(`Database download failed with HTTP ${response.status}`);
  }
  const bytes = await readResponse(response, progress);
  if (cache) {
    const headers = new Headers({
      "content-type": "application/vnd.sqlite3",
      "x-podsearch-cached-at": String(Date.now()),
    });
    await cache.put(url, new Response(bytes, { headers })).catch(() => {});
  }
  return { bytes, source: "network" };
}

async function readResponse(response, progress) {
  if (!response.body) return new Uint8Array(await response.arrayBuffer());
  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.byteLength;
    post("progress", { ...progress, received });
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
  const rc = sqlite3.capi.sqlite3_deserialize(
    database.pointer,
    "main",
    pointer,
    bytes.byteLength,
    bytes.byteLength,
    0,
  );
  database.checkRc(rc);
  return database;
}

function selectObjects(database, sql, bind = []) {
  const resultRows = [];
  database.exec({ sql, bind, rowMode: "object", resultRows });
  return resultRows;
}

function searchTerms(input) {
  return input
    .normalize("NFKC")
    .match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu);
}

function ftsQuery(input) {
  const terms = searchTerms(input);
  if (!terms?.length) return "";
  return terms
    .slice(0, 12)
    .map((term) => `"${term.replaceAll('"', '""')}"*`)
    .join(" AND ");
}

function latestTranscripts({ limit = 30, offset = 0 }) {
  return selectObjects(
    catalogDb,
    `
      SELECT e.id, e.title, e.published_at, e.duration, e.status,
             e.has_transcript,
             s.id AS show_id, s.name AS show_name, s.artwork_url,
             s.chart_rank, s.apple_rank, s.in_top_100, s.favorite,
             s.favorite_order,
             COALESCE(
               NULLIF(substr(e.description, 1, 280), ''),
               'Open the full transcript.'
             ) AS snippet,
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

function searchShows(normalized, terms) {
  const showText = `
    lower(
      name || ' ' ||
      COALESCE(artist, '') || ' ' ||
      COALESCE(description, '')
    )
  `;
  const showWhere = terms.map(() => `${showText} LIKE ?`).join(" AND ");
  return selectObjects(
    catalogDb,
    `
      SELECT id, name, artist, description, artwork_url, chart_rank, apple_rank,
             in_top_100, ever_top_100, favorite, favorite_order,
             (SELECT COUNT(*) FROM episodes WHERE show_id = shows.id) AS episode_count,
             (SELECT COUNT(*) FROM episodes
                WHERE show_id = shows.id AND has_transcript = 1) AS transcript_count
      FROM shows
      WHERE ${showWhere}
      ORDER BY in_top_100 DESC, chart_rank IS NULL, chart_rank, name
      LIMIT 24
    `,
    terms.map((term) => `%${term.toLocaleLowerCase()}%`),
  );
}

function searchEpisodeMetadata(match, limit) {
  return selectObjects(
    catalogDb,
    `
      SELECT e.id,
             COALESCE(
               NULLIF(snippet(episode_search, 2, '<mark>', '</mark>', ' … ', 30), ''),
               substr(e.description, 1, 280),
               'Episode information'
             ) AS snippet,
             bm25(episode_search, 8.0, 4.0, 1.0) AS score
      FROM episode_search
      JOIN episodes e ON e.id = episode_search.rowid
      WHERE episode_search MATCH ?
      ORDER BY score, e.published_at DESC
      LIMIT ?
    `,
    [match, Number(limit)],
  );
}

async function searchTranscriptText(match, limit, requestId, searchToken) {
  const candidates = [];
  for (let index = 0; index < transcriptShards.length; index += 1) {
    if (searchToken !== activeSearchToken) throw new SearchCancelled();
    const shard = transcriptShards[index];
    post("search-progress", {
      requestId,
      completed: index,
      total: transcriptShards.length,
      shard: shard.key,
    });
    const { bytes } = await loadDatabaseBytes(shard.path, {
      requestId,
      phase: "search",
      shard: shard.key,
      completed: index,
      total: transcriptShards.length,
    });
    if (searchToken !== activeSearchToken) throw new SearchCancelled();
    const database = deserializeDatabase(bytes);
    try {
      candidates.push(
        ...selectObjects(
          database,
          `
            SELECT transcripts.episode_id AS id,
                   snippet(transcript_search, 2, '<mark>', '</mark>', ' … ', 30)
                     AS snippet,
                   bm25(transcript_search, 8.0, 4.0, 1.0) AS score
            FROM transcript_search
            JOIN transcripts
              ON transcripts.episode_id = transcript_search.rowid
            WHERE transcript_search MATCH ?
            ORDER BY score
            LIMIT ?
          `,
          [match, Number(limit)],
        ),
      );
    } finally {
      database.close();
    }
    post("search-progress", {
      requestId,
      completed: index + 1,
      total: transcriptShards.length,
      shard: shard.key,
    });
  }
  return candidates;
}

function enrichEpisodeCandidates(candidates, limit) {
  const bestByEpisode = new Map();
  for (const candidate of candidates) {
    const existing = bestByEpisode.get(candidate.id);
    if (!existing || Number(candidate.score) < Number(existing.score)) {
      bestByEpisode.set(candidate.id, candidate);
    }
  }
  const selected = [...bestByEpisode.values()]
    .sort((left, right) => Number(left.score) - Number(right.score))
    .slice(0, Number(limit));
  if (!selected.length) return [];

  const placeholders = selected.map(() => "?").join(",");
  const details = selectObjects(
    catalogDb,
    `
      SELECT e.id, e.title, e.published_at, e.duration, e.status,
             e.has_transcript,
             s.id AS show_id, s.name AS show_name, s.artwork_url,
             s.chart_rank, s.apple_rank, s.in_top_100, s.favorite,
             s.favorite_order
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      WHERE e.id IN (${placeholders})
    `,
    selected.map(({ id }) => Number(id)),
  );
  const detailsById = new Map(details.map((row) => [row.id, row]));
  return selected
    .map((candidate) => ({
      ...detailsById.get(candidate.id),
      snippet: candidate.snippet,
      score: candidate.score,
    }))
    .filter((row) => row.id)
    .sort(
      (left, right) =>
        Number(left.score) - Number(right.score) ||
        String(right.published_at || "").localeCompare(String(left.published_at || "")),
    );
}

async function globalSearch({ query = "", limit = 80, requestId }, searchToken) {
  const normalized = query.trim();
  const match = ftsQuery(normalized);
  if (!match) return { shows: [], episodes: [] };
  const terms = searchTerms(normalized).slice(0, 12);
  const shows = searchShows(normalized, terms);
  const metadataCandidates = searchEpisodeMetadata(match, limit);
  const transcriptCandidates = await searchTranscriptText(
    match,
    limit,
    requestId,
    searchToken,
  );
  if (searchToken !== activeSearchToken) throw new SearchCancelled();
  return {
    shows,
    episodes: enrichEpisodeCandidates(
      [...metadataCandidates, ...transcriptCandidates],
      limit,
    ),
  };
}

function episodesPage({ page = 1, perPage = 50 }) {
  const safePerPage = Math.min(50, Math.max(1, Number(perPage) || 50));
  const total = Number(
    selectObjects(catalogDb, "SELECT COUNT(*) AS total FROM episodes")[0]?.total || 0,
  );
  const totalPages = Math.max(1, Math.ceil(total / safePerPage));
  const safePage = Math.min(totalPages, Math.max(1, Number(page) || 1));
  const offset = (safePage - 1) * safePerPage;
  const rows = selectObjects(
    catalogDb,
    `
      SELECT e.id, e.title, e.published_at, e.duration, e.status,
             e.has_transcript,
             s.id AS show_id, s.name AS show_name, s.artwork_url,
             s.chart_rank, s.apple_rank, s.in_top_100, s.favorite,
             s.favorite_order,
             COALESCE(
               NULLIF(substr(e.description, 1, 280), ''),
               'Open episode details.'
             ) AS snippet
      FROM episodes e
      JOIN shows s ON s.id = e.show_id
      ORDER BY e.published_at IS NULL, e.published_at DESC, e.id DESC
      LIMIT ? OFFSET ?
    `,
    [safePerPage, offset],
  );
  return { rows, total, page: safePage, totalPages };
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
      SELECT id, title, description, episode_url, image_url, published_at,
             duration, status, has_transcript
      FROM episodes
      WHERE show_id = ?
      ORDER BY published_at DESC, id DESC
    `,
    [Number(id)],
  );
  return details;
}

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
  if (!details || !details.has_transcript || !details.transcript_shard) {
    return details;
  }

  const shard = transcriptShards.find(({ key }) => key === details.transcript_shard);
  if (!shard) {
    throw new Error(`Transcript shard ${details.transcript_shard} is unavailable`);
  }
  post("episode-progress", { requestId, shard: shard.key });
  const { bytes } = await loadDatabaseBytes(shard.path, {
    requestId,
    phase: "episode",
    shard: shard.key,
  });
  const database = deserializeDatabase(bytes);
  try {
    const transcript = selectObjects(
      database,
      "SELECT transcript_text FROM transcripts WHERE episode_id = ?",
      [Number(id)],
    )[0];
    if (!transcript) throw new Error("Transcript is missing from its archive shard");
    details.transcript_text = transcript.transcript_text;
  } finally {
    database.close();
  }
  return details;
}

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
