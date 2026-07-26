# Podsearch

Podsearch is a locally transcribed, browser-searched archive of podcast
transcripts. A Mac mini refreshes Apple's current U.S. Top 100 podcast catalog,
reads episode RSS feeds, downloads audio temporarily, transcribes it with
Whisper.cpp and Metal, and exports a static SQLite catalog plus monthly
transcript databases with FTS5 search indexes.

The public site downloads one small compressed catalog and opens it with SQLite
Wasm in a Web Worker. Everything else — full episode descriptions, the global
transcript search index, and the monthly transcript shards — is read in place
over HTTP range requests, so a query costs pages rather than files. Search
queries and full-transcript reads stay in the visitor's browser. The Mac mini
serves only static files.

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
 public/data/manifest.json          (tiny, never cached)
 public/data/catalog.sqlite3.gz     (downloaded whole, then kept in OPFS)
 public/data/details.sqlite3        (range-read)
 public/data/search.sqlite3         (range-read)
 public/data/transcripts/YYYY-MM.sqlite3  (range-read)
                 |
       localhost static server (byte ranges, HTTP/1.1 keep-alive)
                 |
         Cloudflare Tunnel
                 |
                 v
 Browser Web Worker + SQLite Wasm + FTS5 + HTTP-range VFS
```

Only the catalog is downloaded. It holds podcast metadata, one row per episode
with a truncated snippet, transcript availability, and a metadata FTS index —
deliberately not full descriptions, per-episode URLs, or transcripts, because
every byte in it is paid for on the first visit by every visitor.

The other three databases are opened through a read-only SQLite VFS that
fetches 32 KiB blocks over HTTP range requests, with sequential readahead and a
block cache:

- `details.sqlite3` — full descriptions and per-episode links. An episode page
  reads one row: a couple of requests, tens of kilobytes, out of a file tens of
  megabytes large.
- `search.sqlite3` — a single contentless FTS5 index over every transcript. A
  search is one b-tree descent instead of a fan-out across shards.
- `transcripts/YYYY-MM.sqlite3` — full text plus a snippet-capable index. Only
  the shards holding results are touched, and only for the rows displayed.

The result is that search and episode cost scales with the size of the answer
rather than the size of the archive. Downloading every shard to run one search
does not scale: at full 2026 coverage that would be on the order of a gigabyte.

The public files exclude audio URLs, failure records, local paths, chart
history, and the per-row fingerprints used to drive incremental builds (those
live in `var/site-state/`).

### Freshness

`manifest.json` is served `no-store` and names the current version of every
database. The worker fetches it on each page load, and reuses the catalog
already stored in OPFS when the versions match — so a repeat visit costs about
a kilobyte instead of a multi-megabyte download. Because the pool takes
exclusive access handles and a worker survives in the back/forward cache, OPFS
is used purely as storage: the catalog bytes are read out and the pool is
released immediately, leaving it free for the next page.

Every published database is rebuilt incrementally and swapped in atomically. A
build that changes nothing rewrites nothing, and a backfill pass that completes
one episode touches only that episode's rows. If a file is republished while a
browser is reading it, the VFS notices the changed `ETag`, re-reads the
manifest, and retries against the new revision rather than mixing two files.

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

An optional fifth LaunchAgent, `com.merimeri.podsearch.remote-pull`, pulls
completed transcript bundles from a second Mac every five minutes.

The Mac mini can also run `com.merimeri.podsearch.secondary-backfill`. It
leases one old episode at a time to a second local Metal process, preventing
overlap with the primary ranked worker.

The nightly job always refreshes the catalog, ingests a three-day overlap
window, saves newly discovered episodes, and atomically rebuilds the public
database. While the historical backfill is running, the nightly job leaves
transcription to that worker. After the backfill finishes, nightly runs resume
transcribing newly discovered episodes normally.

Logs are stored in `var/logs/`.

### Optional second Mac mini worker

Install and start the concurrency-safe second local worker with:

```bash
python3 -m podsearch --config config.toml secondary-backfill-install
launchctl bootout \
  "gui/$(id -u)/com.merimeri.podsearch.secondary-backfill" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.merimeri.podsearch.secondary-backfill.plist"
```

Its logs are `var/logs/secondary-backfill.out.log` and
`var/logs/secondary-backfill.err.log`. It pauses before starting another
episode if system memory headroom falls below 25%.

## Distributed backfill with a second Mac

The Mac mini remains the primary database owner. A remote worker never copies
its SQLite database back to the mini and never writes across the network.
Instead:

1. The MacBook asks the mini for a lean worker snapshot.
2. The mini leases the oldest 200 eligible episodes to that worker for 72
   hours. Its own newest-first ranked queue skips active leases.
3. The MacBook transcribes those leased episodes oldest-first using
   `large-v3-turbo-q5_0` on Metal.
4. Each completion becomes a checksummed JSON bundle in
   `var/worker-outbox/`.
5. The mini pulls bundles over SSH/rsync, imports only transcripts it still
   lacks, clears their leases, and rebuilds the static site.

Expired leases automatically return to the mini queue. Bundle imports are
idempotent, so even a race or retry cannot corrupt the primary database.

### 1. Prepare SSH over Tailscale

Both Macs need non-interactive SSH access to each other. Test both directions:

```bash
# From the MacBook Pro
ssh mac-mini.your-tailnet.ts.net true

# From the Mac mini
ssh macbook-pro.your-tailnet.ts.net true
```

Use macOS Remote Login or Tailscale SSH. The scripts use `BatchMode=yes`, so
password prompts will intentionally fail rather than hanging a background job.

### 2. Install the Mac mini pull job

On the Mac mini, pull the latest repository and install the optional
LaunchAgent:

```bash
cd /Users/merimerimeri/Development/podsearch
git pull

python3 -m podsearch --config config.toml remote-pull-install \
  --worker macbook-pro.your-tailnet.ts.net \
  --remote-repo /Users/merimerimeri/Development/podsearch \
  --interval 300

launchctl bootout "gui/$(id -u)/com.merimeri.podsearch.remote-pull" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/com.merimeri.podsearch.remote-pull.plist"
launchctl kickstart -k "gui/$(id -u)/com.merimeri.podsearch.remote-pull"
```

The pull log is `var/logs/remote-pull.out.log`; SSH or import failures are in
`var/logs/remote-pull.err.log`.

### 3. Start the MacBook worker

On the MacBook Pro:

```bash
git clone https://github.com/smeriwether/podsearch.git
cd podsearch

brew install whisper-cpp
mkdir -p "$HOME/.cache/whisper.cpp"
scp mac-mini.your-tailnet.ts.net:.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin \
  "$HOME/.cache/whisper.cpp/"

export PODSEARCH_MINI_HOST=mac-mini.your-tailnet.ts.net
scripts/macbook-backfill-worker.sh
```

The script verifies `whisper-cli` and the model before claiming work. It
downloads only a small leased queue database, not the mini's transcript
database. It attempts every episode in the current lease once, emits Whisper's
Metal/device output while it runs, and continues past a bad download or
transcription. Once the mini has emptied the MacBook outbox, rerun it to claim
the next oldest block.

Optional worker settings:

```bash
export PODSEARCH_WORKER_CLAIM_LIMIT=200
export PODSEARCH_WORKER_LEASE_HOURS=72
export PODSEARCH_WHISPER_MODEL="$HOME/.cache/whisper.cpp/ggml-large-v3-turbo-q5_0.bin"
export PODSEARCH_MINI_REPO=/Users/merimerimeri/Development/podsearch
export PODSEARCH_WORKER_ID=my-m4-macbook-pro
```

Multiple workers can run on the same MacBook by assigning each one a distinct
`PODSEARCH_WORKER_ID`. Each worker receives its own leased snapshot database,
while completed bundles safely share the same outbox.

The mini continues prioritizing the latest untranscribed episodes from the
front of the ranked queue while the MacBook works from the oldest leased
episodes at the back.

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

`npm run verify:site` inspects the built artifacts rather than the source text:
it checks that every manifest entry resolves to a file of the declared size and
version, that the catalog still excludes the bulky columns, that `has_detail`
and `has_transcript` agree with the details and transcript databases, that the
FTS indexes answer real queries and produce highlighted snippets, and that
byte-range serving works.

## Hosting requirements

The site needs a static host that supports HTTP range requests (`Accept-Ranges:
bytes` and `206 Partial Content`). The bundled server does, and Cloudflare
passes ranges through. Without range support the VFS falls back to fetching
whole databases, which works but gives up the main benefit.

Range responses are always served from the identity file — a gzip stream cannot
be seeked into — so each published database keeps a `.gz` sibling that is used
only for whole-file requests.

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
