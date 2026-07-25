const app = document.querySelector("#app");
const routePath = window.location.pathname.replace(/\/+$/, "") || "/";
const state = {
  ready: false,
  meta: {},
  shows: [],
  bytes: 0,
  cacheSource: "",
  requestId: 0,
  pending: new Map(),
};

renderInitialRoute();
document.addEventListener("keydown", focusSearch);

const worker = new Worker("/db-worker.js?v=20260725y", { type: "module" });
worker.onmessage = ({ data }) => {
  if (data.type === "progress") return;
  if (["search-progress", "episode-progress"].includes(data.type)) {
    state.pending.get(data.requestId)?.progress?.(data);
    return;
  }
  if (data.type === "search-cancelled") {
    const callback = state.pending.get(data.requestId);
    state.pending.delete(data.requestId);
    callback?.resolve({ cancelled: true, shows: [], episodes: [] });
    return;
  }
  if (data.type === "ready") {
    state.ready = true;
    state.meta = data.meta;
    state.shows = data.shows;
    state.bytes = data.bytes;
    state.cacheSource = data.source;
    renderRoute();
    return;
  }
  if (["search-results", "global-search-results", "episodes-results", "show", "episode"].includes(data.type)) {
    const callback = state.pending.get(data.requestId);
    state.pending.delete(data.requestId);
    callback?.resolve(data.rows ?? data.results ?? data.show ?? data.episode ?? null);
    return;
  }
  if (data.type === "error") {
    const callback = state.pending.get(data.requestId);
    if (callback) {
      state.pending.delete(data.requestId);
      callback.reject(new Error(data.message));
    } else {
      renderError(data.message);
    }
  }
};

function request(type, payload = {}, progress = null) {
  const requestId = ++state.requestId;
  return new Promise((resolve, reject) => {
    state.pending.set(requestId, { resolve, reject, progress });
    worker.postMessage({ type, requestId, ...payload });
  });
}

function renderInitialRoute() {
  if (routePath === "/") {
    renderHomeShell(false);
  } else if (routePath === "/search") {
    const query = new URLSearchParams(window.location.search).get("q") || "";
    renderSearchShell(query, true);
  } else {
    app.innerHTML = `
      <section class="loading-page" aria-live="polite">
        <span class="loader"></span><p>Opening the archive…</p>
      </section>
    `;
  }
}

async function renderRoute() {
  try {
    if (routePath === "/podcasts") {
      renderPodcastIndex(false);
    } else if (routePath === "/favorites") {
      renderPodcastIndex(true);
    } else if (routePath === "/podcast") {
      await renderPodcastPage();
    } else if (routePath === "/episode") {
      await renderEpisodePage();
    } else if (routePath === "/episodes") {
      await renderAllEpisodesPage();
    } else if (routePath === "/search") {
      await renderSearchPage();
    } else {
      await renderHome();
    }
  } catch (error) {
    renderError(error instanceof Error ? error.message : String(error));
  }
}

function renderHomeShell(ready) {
  document.title = "Podsearch — A searchable archive of spoken ideas";
  app.innerHTML = `
    <section class="hero">
      <h1>A SEARCHABLE ARCHIVE<br> OF SPOKEN IDEAS</h1>
      <p class="hero-copy">Full transcripts from Apple’s Top 100 podcasts and a handpicked favorites list. Free forever, with transcripts provided by MeriMeriMeri Software.</p>
      <form class="search-shell" id="search-form" role="search">
        <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"></circle><path d="m16.2 16.2 4.3 4.3"></path></svg>
        <input id="search-input" type="search" autocomplete="off" placeholder="Search podcasts, episodes, and transcripts…" aria-label="Search podcasts, episodes, and transcripts">
        <kbd>⌘ K</kbd>
      </form>
      <p class="index-summary${ready ? "" : " is-loading"}">${ready
        ? `${number(state.meta.transcript_count)} transcripts · ${number(state.meta.show_count)} podcasts`
        : "&nbsp;"}</p>
    </section>
    <section class="library" aria-labelledby="library-title">
      <div class="library-heading">
        <div><p class="section-kicker">The archive</p><h2 id="library-title">Latest transcripts</h2></div>
        <a class="section-link" href="/episodes/">All episodes →</a>
      </div>
      ${ready
        ? `<p class="result-meta" id="result-count">Loading transcripts…</p><div class="results" id="results"></div>`
        : `<p class="archive-loading" aria-live="polite"><span class="loader"></span> Loading the latest transcripts…</p>`}
    </section>
  `;
  bindHomeSearch();
}

function bindHomeSearch() {
  const form = document.querySelector("#search-form");
  const input = document.querySelector("#search-input");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = input.value.trim();
    if (query) window.location.assign(`/search/?q=${encodeURIComponent(query)}`);
  });
}

async function renderHome() {
  const rows = await request("search", {
    limit: 20,
    offset: 0,
  });
  renderHomeShell(true);
  renderTranscriptResults(rows);
}

function renderTranscriptResults(rows) {
  const results = document.querySelector("#results");
  const count = document.querySelector("#result-count");
  if (!results || !count) return;
  count.textContent = `${number(rows.length)} transcripts`;
  if (!rows.length) {
    results.innerHTML = `<div class="empty-state"><strong>No transcripts yet.</strong><span>The local backfill is still working through this podcast.</span></div>`;
    return;
  }
  results.innerHTML = rows.map(episodeResult).join("");
}

function renderSearchShell(query, preparing = false) {
  document.title = query ? `Search: ${query} — Podsearch` : "Search — Podsearch";
  app.innerHTML = `
    <section class="search-page">
      <p class="section-kicker">Search results</p>
      <h1>Find anything in the archive.</h1>
      <form class="search-shell search-page-form" id="results-search-form" role="search">
        <svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"></circle><path d="m16.2 16.2 4.3 4.3"></path></svg>
        <input class="global-search-input" id="results-search-input" type="search" value="${escapeHtml(query)}" autocomplete="off" placeholder="Search podcasts, episodes, and transcripts…" aria-label="Search podcasts, episodes, and transcripts">
      </form>
      <div class="search-results" id="search-results" aria-live="polite">
        ${preparing ? `<p class="search-preparing"><span class="loader"></span> Preparing the local search index…</p>` : ""}
      </div>
    </section>
  `;
}

async function renderSearchPage() {
  const initialQuery = new URLSearchParams(window.location.search).get("q") || "";
  renderSearchShell(initialQuery);
  const form = document.querySelector("#results-search-form");
  const input = document.querySelector("#results-search-input");
  const run = async () => {
    const query = input.value.trim();
    const url = query ? `/search/?q=${encodeURIComponent(query)}` : "/search/";
    window.history.replaceState({}, "", url);
    document.title = query ? `Search: ${query} — Podsearch` : "Search — Podsearch";
    const target = document.querySelector("#search-results");
    if (!query) {
      target.innerHTML = `<div class="empty-state"><strong>What are you looking for?</strong><span>Search podcast names, publishers, episode titles, or words spoken in a transcript.</span></div>`;
      return;
    }
    target.innerHTML = `<p class="search-preparing"><span class="loader"></span> Searching the local archive…</p>`;
    const results = await request(
      "global-search",
      { query, limit: 80 },
      ({ completed, total }) => {
        if (!total) return;
        target.innerHTML = `
          <p class="search-preparing">
            <span class="loader"></span>
            Searching transcript archive ${number(completed)} of ${number(total)}…
          </p>
        `;
      },
    );
    if (results.cancelled) return;
    renderGlobalSearchResults(results, query);
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    run();
  });
  await run();
}

function renderGlobalSearchResults(results, query) {
  const target = document.querySelector("#search-results");
  const total = results.shows.length + results.episodes.length;
  if (!total) {
    target.innerHTML = `<div class="empty-state"><strong>No results for “${escapeHtml(query)}.”</strong><span>Try fewer words or a different spelling.</span></div>`;
    return;
  }
  target.innerHTML = `
    <p class="search-count">${number(total)} results for “${escapeHtml(query)}”</p>
    ${results.shows.length ? `
      <section class="search-group" aria-labelledby="podcast-results-title">
        <div class="library-heading"><div><p class="section-kicker">Podcasts</p><h2 id="podcast-results-title">${number(results.shows.length)} matches</h2></div></div>
        <div class="podcast-grid search-podcast-grid">${results.shows.map((show) => showCard(show, true)).join("")}</div>
      </section>
    ` : ""}
    ${results.episodes.length ? `
      <section class="search-group" aria-labelledby="episode-results-title">
        <div class="library-heading"><div><p class="section-kicker">Episodes & transcripts</p><h2 id="episode-results-title">${number(results.episodes.length)} matches</h2></div></div>
        <div class="results">${results.episodes.map(episodeResult).join("")}</div>
      </section>
    ` : ""}
  `;
}

function episodeResult(row, showPendingLabel = false) {
  return `
    <article class="result-card">
      <a class="result-open" href="/episode/?id=${row.id}">
        ${artwork(row.artwork_url, row.show_name, "result-artwork")}
        <span class="result-body">
          <span class="result-show">${row.favorite ? "★ " : ""}${escapeHtml(row.show_name)}</span>
          <strong class="result-title">${escapeHtml(row.title)}</strong>
          <span class="result-snippet">${safeSnippet(row.snippet)}</span>
          <span class="result-footer">${escapeHtml(formatDate(row.published_at))}${row.duration ? ` · ${escapeHtml(formatDuration(row.duration))}` : ""}</span>
          ${showPendingLabel && !row.has_transcript ? `<span class="result-pending-label">Transcript Coming Soon</span>` : ""}
        </span>
        <span class="result-arrow" aria-hidden="true">→</span>
      </a>
    </article>
  `;
}

async function renderAllEpisodesPage() {
  const requestedPage = positiveInteger(
    new URLSearchParams(window.location.search).get("page"),
    1,
  );
  const result = await request("episodes", {
    page: requestedPage,
    perPage: 50,
  });
  const { rows, total, page, totalPages } = result;
  if (page !== requestedPage) {
    window.history.replaceState({}, "", page > 1 ? `/episodes/?page=${page}` : "/episodes/");
  }
  document.title = `${page > 1 ? `Page ${page} — ` : ""}All Episodes — Podsearch`;
  const first = total ? ((page - 1) * 50) + 1 : 0;
  const last = total ? first + rows.length - 1 : 0;
  app.innerHTML = `
    <section class="episodes-page">
      <div class="page-intro episodes-intro">
        <p class="section-kicker">The complete feed</p>
        <h1>All Episodes</h1>
        <p>Every indexed episode, ordered by release date.</p>
      </div>
      <div class="library-heading episodes-heading">
        <p class="result-meta">${total ? `${number(first)}–${number(last)} of ${number(total)} episodes` : "No episodes"}</p>
        <p class="page-number">Page ${number(page)} of ${number(totalPages)}</p>
      </div>
      <div class="results">
        ${rows.length
          ? rows.map((row) => episodeResult(row, true)).join("")
          : `<div class="empty-state"><strong>No episodes yet.</strong><span>The feeds will refresh again tonight.</span></div>`}
      </div>
      ${pagination(page, totalPages)}
    </section>
  `;
}

function pagination(page, totalPages) {
  if (totalPages <= 1) return "";
  const previous = page > 1 ? page - 1 : null;
  const next = page < totalPages ? page + 1 : null;
  const href = (value) => value === 1 ? "/episodes/" : `/episodes/?page=${value}`;
  return `
    <nav class="pagination" aria-label="Episode pages">
      ${previous
        ? `<a href="${href(previous)}">← Previous</a>`
        : `<span aria-disabled="true">← Previous</span>`}
      <span>Page ${number(page)} of ${number(totalPages)}</span>
      ${next
        ? `<a href="${href(next)}">Next →</a>`
        : `<span aria-disabled="true">Next →</span>`}
    </nav>
  `;
}

function positiveInteger(value, fallback) {
  const numberValue = Number(value);
  return Number.isInteger(numberValue) && numberValue > 0 ? numberValue : fallback;
}

function renderPodcastIndex(favoritesOnly) {
  const title = favoritesOnly ? "My Favorites" : "All Podcasts";
  document.title = `${title} — Podsearch`;
  const shows = state.shows
    .filter((show) => !favoritesOnly || show.favorite)
    .sort(favoritesOnly
      ? (left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: "base" })
      : (left, right) => {
          if (left.apple_rank && right.apple_rank) return left.apple_rank - right.apple_rank;
          if (left.apple_rank) return -1;
          if (right.apple_rank) return 1;
          if (left.favorite_order && right.favorite_order) {
            return left.favorite_order - right.favorite_order;
          }
          if (left.favorite_order) return -1;
          if (right.favorite_order) return 1;
          if (left.chart_rank && right.chart_rank) return left.chart_rank - right.chart_rank;
          return left.id - right.id;
        });
  app.innerHTML = `
    <section class="page-intro">
      <p class="section-kicker">${favoritesOnly ? "Handpicked" : "The complete index"}</p>
      <h1>${title}</h1>
      <p>${favoritesOnly
        ? "A personal collection of podcasts worth returning to, listed alphabetically."
        : "Apple Podcasts’ current U.S. Top 100 in chart order, with ranked favorites through #200, followed by former chart members and other handpicked favorites."}</p>
      <label class="directory-search">
        <span class="sr-only">Search podcasts</span>
        <input id="podcast-search" type="search" placeholder="Filter this podcast list…" autocomplete="off">
      </label>
    </section>
    <section class="podcast-directory" aria-label="${title}">
      <p class="directory-count" id="directory-count">${number(shows.length)} podcasts</p>
      <div class="podcast-grid" id="podcast-grid"></div>
    </section>
  `;
  const input = document.querySelector("#podcast-search");
  const paint = () => {
    const query = input.value.trim().toLocaleLowerCase();
    const filtered = shows.filter((show) =>
      `${show.name} ${show.artist || ""}`.toLocaleLowerCase().includes(query)
    );
    document.querySelector("#directory-count").textContent = `${number(filtered.length)} podcasts`;
    document.querySelector("#podcast-grid").innerHTML = filtered.length
      ? filtered.map((show) => showCard(show, false, !favoritesOnly)).join("")
      : favoritesOnly && !query
        ? `<div class="empty-state"><strong>No favorites yet.</strong><span>Your handpicked list will appear here once it’s added.</span></div>`
        : `<div class="empty-state"><strong>No podcasts found.</strong><span>Try another name or publisher.</span></div>`;
  };
  input.addEventListener("input", paint);
  paint();
}

function showCard(show, compact = false, showFavoriteBadge = false) {
  return `
    <article class="podcast-card${compact ? " search-podcast-card" : ""}">
      <a href="/podcast/?id=${show.id}">
        <span class="artwork-wrap">
          ${artwork(show.artwork_url, show.name, "podcast-artwork")}
          ${showFavoriteBadge && show.favorite ? `<span class="favorite-badge">♥ Favorite</span>` : ""}
          ${show.in_top_100 && show.chart_rank
            ? `<span class="rank-badge">#${show.chart_rank}</span>`
            : show.apple_rank
              ? `<span class="rank-badge">#${show.apple_rank}</span>`
            : show.ever_top_100 && show.chart_rank
              ? `<span class="rank-badge former-rank-badge">Last #${show.chart_rank}</span>`
              : show.favorite
                ? `<span class="rank-badge outside-rank-badge">Outside Top 200</span>`
                : ""}
        </span>
        <span class="podcast-card-copy">
          <strong>${escapeHtml(show.name)}</strong>
          <span>${escapeHtml(show.artist || "")}</span>
          <small>${number(show.episode_count)} episodes · ${number(show.transcript_count)} transcripts</small>
        </span>
      </a>
    </article>
  `;
}

async function renderPodcastPage() {
  const id = new URLSearchParams(window.location.search).get("id");
  if (!id) return renderNotFound("Podcast not found");
  const show = await request("show", { id });
  if (!show) return renderNotFound("Podcast not found");
  document.title = `${show.name} — Podsearch`;
  const genres = parseGenres(show.genres_json);
  const formerChartNotice = !show.in_top_100 && show.ever_top_100 && !show.favorite
    ? `<aside class="archive-notice"><strong>Former Top 100 podcast</strong><p>This podcast is no longer in Apple Podcasts’ current U.S. Top 100. Its archive stays searchable and new episodes remain indexed, but new transcripts are not scheduled.</p></aside>`
    : "";
  app.innerHTML = `
    <section class="detail-hero">
      ${artwork(show.artwork_url, show.name, "detail-artwork")}
      <div class="detail-copy">
        <p class="breadcrumbs"><a href="/podcasts/">All Podcasts</a>${show.in_top_100 && show.chart_rank
          ? ` <span>→</span> #${show.chart_rank}`
          : show.apple_rank
            ? ` <span>→</span> #${show.apple_rank}`
          : show.ever_top_100 && show.chart_rank
            ? ` <span>→</span> Last chart rank #${show.chart_rank}`
            : show.favorite
              ? ` <span>→</span> Outside Apple’s Top 200`
              : ""}</p>
        <h1>${escapeHtml(show.name)}</h1>
        <p class="detail-byline">${escapeHtml(show.artist || "")}</p>
        ${formerChartNotice}
        ${genres.length ? `<p class="genre-list">${genres.map((genre) => `<span>${escapeHtml(genre)}</span>`).join("")}</p>` : ""}
        ${show.description ? `<div class="detail-description">${formatParagraphs(show.description, 3)}</div>` : ""}
        <div class="detail-actions">
          ${show.apple_url ? `<a class="primary-link" href="${safeUrl(show.apple_url)}" target="_blank" rel="noopener">View on Apple Podcasts ↗</a>` : ""}
          ${show.homepage_url ? `<a href="${safeUrl(show.homepage_url)}" target="_blank" rel="noopener">Podcast website ↗</a>` : ""}
        </div>
      </div>
    </section>
    <section class="episode-library">
      <div class="library-heading">
        <div><p class="section-kicker">${number(show.transcript_count)} transcripts</p><h2>All episodes</h2></div>
        <p>${number(show.episode_count)} episodes</p>
      </div>
      <div class="episode-list">
        ${show.episodes.length ? show.episodes.map(episodeRow).join("") : `<div class="empty-state"><strong>No episodes yet.</strong><span>This feed will be checked again during the nightly refresh.</span></div>`}
      </div>
    </section>
  `;
}

function episodeRow(episode) {
  return `
    <article class="episode-row">
      <a href="/episode/?id=${episode.id}">
        <span class="episode-copy">
          <strong>${escapeHtml(episode.title)}</strong>
          <span>${escapeHtml(formatDate(episode.published_at))}${episode.duration ? ` · ${escapeHtml(formatDuration(episode.duration))}` : ""}</span>
        </span>
        <span class="result-arrow" aria-hidden="true">→</span>
      </a>
    </article>
  `;
}

async function renderEpisodePage() {
  const id = new URLSearchParams(window.location.search).get("id");
  if (!id) return renderNotFound("Episode not found");
  const episode = await request("episode", { id }, () => {
    const loadingText = document.querySelector(".loading-page p");
    if (loadingText) loadingText.textContent = "Loading this transcript…";
  });
  if (!episode) return renderNotFound("Episode not found");
  document.title = `${episode.title} — Podsearch`;
  const source = episode.episode_url || episode.apple_url;
  const hasTranscript = episode.status === "transcribed" && episode.transcript_text;
  const transcriptionActive = Boolean(episode.in_top_100 || episode.favorite);
  const copyTranscript = hasTranscript ? sentenceLines(episode.transcript_text).join("\n") : "";
  app.innerHTML = `
    <article class="episode-page">
      <div class="episode-heading">
        ${artwork(episode.image_url || episode.show_artwork_url, episode.show_name, "episode-artwork")}
        <div>
          <p class="breadcrumbs"><a href="/podcast/?id=${episode.show_id}">${escapeHtml(episode.show_name)}</a></p>
          <h1>${escapeHtml(episode.title)}</h1>
          <p class="episode-meta">${escapeHtml(formatDate(episode.published_at))}${episode.duration ? ` · ${escapeHtml(formatDuration(episode.duration))}` : ""}</p>
          ${episode.description ? `<div class="detail-description">${formatParagraphs(episode.description, 2)}</div>` : ""}
          ${source ? `<a class="primary-link" href="${safeUrl(source)}" target="_blank" rel="noopener">Open original episode ↗</a>` : ""}
        </div>
      </div>
      <section class="transcript-section">
        <div class="transcript-heading">
          <div><p class="section-kicker">Full text</p><h2>Transcript</h2></div>
          ${hasTranscript ? `<button class="copy-button" id="copy-transcript" type="button">Copy entire transcript</button>` : ""}
        </div>
        ${hasTranscript
          ? `<div class="transcript" id="transcript-text">${sentenceLines(episode.transcript_text).map((sentence) => `<p>${escapeHtml(sentence)}</p>`).join("")}</div>`
          : transcriptionActive
            ? `<div class="transcript-pending"><strong>Transcript pending.</strong><p>This episode is in the local transcription queue. Check back as the backfill progresses.</p></div>`
            : `<div class="transcript-pending inactive"><strong>Transcript not scheduled.</strong><p>This podcast is no longer in Apple Podcasts’ current U.S. Top 100, so this episode is not in the transcription queue.</p></div>`}
      </section>
    </article>
  `;
  if (hasTranscript) {
    document.querySelector("#copy-transcript").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.textContent = "Copying…";
      const copied = await copyText(copyTranscript);
      button.textContent = copied ? "Copied" : "Select transcript to copy";
      setTimeout(() => {
        button.textContent = "Copy entire transcript";
      }, 1600);
    });
  }
}

function sentenceLines(value) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return [];
  if (typeof Intl.Segmenter === "function") {
    const segmenter = new Intl.Segmenter("en", { granularity: "sentence" });
    return [...segmenter.segment(text)].map(({ segment }) => segment.trim()).filter(Boolean);
  }
  return text.match(/[^.!?]+(?:[.!?]+["”’)]*|$)/g)?.map((item) => item.trim()).filter(Boolean) || [text];
}

function formatParagraphs(value, sentencesPerParagraph) {
  const sentences = sentenceLines(value);
  const paragraphs = [];
  for (let index = 0; index < sentences.length; index += sentencesPerParagraph) {
    paragraphs.push(sentences.slice(index, index + sentencesPerParagraph).join(" "));
  }
  return paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`).join("");
}

function renderNotFound(title) {
  document.title = `${title} — Podsearch`;
  app.innerHTML = `<section class="not-found"><p class="section-kicker">404</p><h1>${title}</h1><a class="primary-link" href="/podcasts/">Browse all podcasts</a></section>`;
}

function renderError(message) {
  app.innerHTML = `<section class="not-found"><p class="section-kicker">Archive unavailable</p><h1>Something went wrong.</h1><p>${escapeHtml(message)}</p><a class="primary-link" href="/">Try again</a></section>`;
}

function focusSearch(event) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    const input = document.querySelector("#search-input, #results-search-input");
    if (input) {
      event.preventDefault();
      input.focus();
    }
  }
}

async function copyText(value) {
  try {
    const copied = await Promise.race([
      navigator.clipboard.writeText(value).then(() => true),
      new Promise((resolve) => setTimeout(() => resolve(false), 500)),
    ]);
    if (copied) return true;
  } catch {
    // Fall through to the selection-based copy path.
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  return copied;
}

function artwork(url, name, className) {
  return url
    ? `<img class="${className}" src="${safeUrl(url)}" alt="${escapeHtml(name)} artwork" loading="lazy">`
    : `<span class="${className} artwork-placeholder" aria-hidden="true">${escapeHtml((name || "P").slice(0, 1))}</span>`;
}

function safeSnippet(value) {
  return escapeHtml(value || "Open this result.")
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

function safeUrl(value) {
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "#";
  } catch {
    return "#";
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function parseGenres(value) {
  try {
    const parsed = JSON.parse(value || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(date);
}

function formatDuration(value) {
  if (!value) return "";
  if (/^\d+:\d\d(?::\d\d)?$/.test(value)) return value;
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return value;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function number(value) {
  return new Intl.NumberFormat("en-US").format(Number(value || 0));
}
