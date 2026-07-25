# Podsearch

Podsearch is a locally transcribed, browser-searched archive of podcast
transcripts. A Mac mini refreshes Apple's current U.S. Top 100 podcast catalog,
reads episode RSS feeds, downloads audio temporarily, transcribes it with
Whisper.cpp and Metal, and exports a static SQLite catalog plus monthly
transcript databases with FTS5 search indexes.

The public site downloads the small compressed catalog first and opens it with
SQLite Wasm in a Web Worker. Transcript searches open one monthly shard at a
time and close it before opening the next; episode pages fetch only the month
containing that transcript. Search queries and full-transcript reads stay in
the visitor's browser, and downloaded databases are cached for 12 hours. The
Mac mini serves only static files.

Live site: <https://podsearch.merimerimeri.com>

## Architecture

```text
Apple Top 100 JSON + iTunes Lookup API
                 |
                 v
       Podcast RSS feeds (nightly)
                 |
                 v
    var/podsearch.sqlite3 (private working DB)
                 |
         whisper-cli + Metal
                 |
                 v
 public/data/catalog.sqlite3.gz
 public/data/transcripts/YYYY-MM.sqlite3.gz
                 |
       localhost static server
                 |
         Cloudflare Tunnel
                 |
                 v
 Browser Web Worker + SQLite Wasm + FTS5
```

The catalog contains podcast metadata, every indexed episode, transcript
availability, and a metadata FTS index. Monthly shards contain only completed
transcripts and their transcript FTS indexes. The public files exclude audio
URLs, failure records, local paths, and chart history.

This keeps initial navigation lightweight and bounds browser memory to the
catalog plus the largest monthly shard instead of loading the entire archive
into memory. A full-archive search may download every shard once, but processes
them serially and reuses the browser cache on later searches.

## Current data rules

- The chart source is Apple's U.S. Top Shows feed, refreshed nightly.
- Apple's official extended U.S. chart supplies current overall ranks through
  #200 so curated favorites outside the Top 100 can be placed accurately.
  Favorites outside that published range are shown as `Outside Top 200`.
- Podcasts remain in the public archive after leaving the Top 100. Their last
  known chart rank is retained, their feeds continue to refresh, and new
  transcripts stop unless the podcast is also in the curated favorites list.
- Shows that leave the Top 100 remain in historical search results, but new
  episodes are no longer ingested unless the show is also a favorite.
- Favorites are configured as Apple Podcasts URLs or numeric Apple podcast IDs
  in the root-level `favorites` list in `config.toml`.
- Episode identity is `(show_id, RSS guid)`, so overlapping refresh windows are
  safe.
- The initial ingestion cutoff is January 1, 2026.
- Audio is deleted after a successful transcript.
- The configured model is Whisper.cpp `ggml-large-v3-turbo-q5_0.bin`, selected
  after a local quality benchmark against `small.en`.

## Commands

```bash
# Validate local tools, FTS5, config, and storage
python3 -m podsearch --config config.toml doctor

# Refresh the current Top 100 plus favorites
python3 -m podsearch --config config.toml sync-catalog

# Ingest available 2026 episode metadata
python3 -m podsearch --config config.toml ingest --since 2026-01-01

# Transcribe a bounded batch locally
python3 -m podsearch --config config.toml transcribe --limit 25

# Build the public SQLite catalog, transcript shards, and static site
python3 -m podsearch --config config.toml build-site

# Serve the site at http://127.0.0.1:8091
python3 -m podsearch --config config.toml serve

# Run the ranked continuous backfill
PODSEARCH_BACKFILL_BATCH_SIZE=2 scripts/backfill-2026.sh
```

The backfill takes the newest two untranscribed episodes from chart rank #1,
then #2, through #100, and repeats from the top. Its chart-rank cursor is stored
in SQLite, so restarts preserve the rotation. The public database is rebuilt
after every podcast. Failed episodes are retained for inspection rather than
retried endlessly in the same run.

## Automation

Four LaunchAgents are installed:

- `com.merimeri.podsearch.server`: keeps the local static server running.
- `com.merimeri.podsearch.tunnel`: keeps the dedicated Cloudflare tunnel
  connected.
- `com.merimeri.podsearch.nightly`: runs daily at 2:30 AM local time.
- `com.merimeri.podsearch.backfill`: resumes the 2026 backfill at login and
  stops permanently once the queue is complete.

The nightly job always refreshes the catalog, ingests a three-day overlap
window, saves newly discovered episodes, and atomically rebuilds the public
database. While the historical backfill is running, the nightly job leaves
transcription to that worker. After the backfill finishes, nightly runs resume
transcribing newly discovered episodes normally.

Logs are stored in `var/logs/`.

## Cloudflare

The dedicated locally managed tunnel is named `podsearch`. Its configuration is
stored at `~/.cloudflared/podsearch.yml`, and its only published ingress rule is:

```yaml
hostname: podsearch.merimerimeri.com
service: http://127.0.0.1:8091
```

The tunnel has a final `http_status:404` catch-all rule.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q podsearch tests
npm run verify:site
```

The browser smoke test covers database download progress, FTS search,
highlighted results, full-transcript display, copy-to-clipboard, source links,
and the mobile breakpoint.

## Coverage caveats

Apple's chart feed identifies ranked shows, and the iTunes Lookup API supplies
their public RSS feed URLs. A chart show with no public feed cannot be
transcribed until an alternate feed is found. RSS retention also varies by
publisher, so "all of 2026" means all 2026 episodes still exposed by the
publisher's feed unless another archive source is added.

Generated transcripts may contain errors. Before operating a public archive of
full third-party transcripts at scale, confirm that its publication and reuse
model is appropriate for the podcast publishers involved.

## License

Podsearch is licensed under the [Apache License 2.0](LICENSE). Distributions
and derivative works must preserve the license and the attribution in
[NOTICE](NOTICE).
