/**
 * A read-only SQLite VFS that reads remote databases over HTTP range requests.
 *
 * Downloading a whole database to answer one query does not scale: a full
 * archive search used to mean fetching every monthly shard. With this VFS
 * SQLite pulls only the pages a query actually touches, so the cost of a
 * lookup tracks the size of the answer rather than the size of the archive.
 *
 * The transport is injectable. In a Worker it defaults to a synchronous
 * XMLHttpRequest, the only way to satisfy SQLite's synchronous xRead contract;
 * tests supply a local transport instead.
 */

export const DEFAULT_BLOCK_SIZE = 32 * 1024;
const DEFAULT_CACHE_BYTES = 8 * 1024 * 1024;
// Caps the sequential readahead ramp at 1 MiB per request.
const MAX_READAHEAD_BLOCKS = 32;
const VFS_NAME = "podsearch-http";

/** Raised when the file was republished mid-read; the caller should reopen. */
export class RemoteFileChanged extends Error {
  constructor(url) {
    super(`Remote database changed while reading: ${url}`);
    this.name = "RemoteFileChanged";
    this.url = url;
  }
}

/**
 * Raised inside xRead when a needed block is not cached and the environment
 * cannot fetch it synchronously. The caller prefetches and re-runs the query.
 */
export class NeedsPrefetch extends Error {
  constructor(url) {
    super(`Remote database needs more data: ${url}`);
    this.name = "NeedsPrefetch";
    this.url = url;
  }
}

export class RangeUnsupported extends Error {
  constructor(url) {
    super(`Server did not honour a range request for ${url}`);
    this.name = "RangeUnsupported";
    this.url = url;
  }
}

/**
 * Whether this engine allows a synchronous XHR to return binary data.
 *
 * The spec permits it in a Worker, but WebKit throws InvalidAccessError
 * regardless, so Safari and every iOS browser take the asynchronous path.
 */
export function syncTransportUsable() {
  try {
    const request = new XMLHttpRequest();
    request.open("GET", self.location?.href || "/", false);
    request.responseType = "arraybuffer";
    return true;
  } catch {
    return false;
  }
}

/** Asynchronous ranged GET, usable everywhere. */
export async function asyncFetchRange(url, start, end) {
  const response = await fetch(url, { headers: { Range: `bytes=${start}-${end}` } });
  if (!response.ok) throw new Error(`HTTP ${response.status} for ${url}`);
  if (response.status !== 206) {
    // Do not materialize a potentially enormous response merely to discover
    // that the host ignored Range. The caller will perform one intentional
    // whole-file fallback instead.
    await response.body?.cancel();
    throw new RangeUnsupported(url);
  }
  const contentRange = response.headers.get("Content-Range") || "";
  if (!validContentRange(contentRange, start, end)) {
    await response.body?.cancel();
    throw new RangeUnsupported(url);
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  const returned = parseContentRange(contentRange);
  if (!returned || bytes.length !== returned.end - returned.start + 1) {
    throw new RangeUnsupported(url);
  }
  return {
    bytes,
    status: response.status,
    etag: response.headers.get("ETag") || "",
    contentRange,
  };
}

/** Synchronous ranged GET. Only valid inside a Worker, and not on WebKit. */
export function xhrTransport(url, start, end) {
  let request;
  try {
    request = new XMLHttpRequest();
    request.open("GET", url, false);
    request.responseType = "arraybuffer";
    request.setRequestHeader("Range", `bytes=${start}-${end}`);
    request.send(null);
  } catch {
    // Some engines allow responseType to be assigned during the capability
    // probe but reject the actual synchronous request.
    throw new RangeUnsupported(url);
  }
  if (request.status !== 206) {
    if (request.status === 200) throw new RangeUnsupported(url);
    throw new Error(`HTTP ${request.status} for ${url}`);
  }
  const contentRange = request.getResponseHeader("Content-Range") || "";
  if (!validContentRange(contentRange, start, end)) {
    throw new RangeUnsupported(url);
  }
  const bytes = new Uint8Array(request.response);
  const returned = parseContentRange(contentRange);
  if (!returned || bytes.length !== returned.end - returned.start + 1) {
    throw new RangeUnsupported(url);
  }
  return {
    bytes,
    status: request.status,
    etag: request.getResponseHeader("ETag") || "",
    contentRange,
  };
}

/** Merge overlapping or touching byte spans so each becomes one request. */
function mergeSpans(spans) {
  if (!spans.length) return [];
  const sorted = [...spans].sort((a, b) => a[0] - b[0]);
  const merged = [sorted[0].slice()];
  for (const [start, end] of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    if (start <= last[1] + 1) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

function parseContentRange(contentRange) {
  const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec((contentRange || "").trim());
  if (!match) return null;
  const parsed = {
    start: Number(match[1]),
    end: Number(match[2]),
    total: Number(match[3]),
  };
  return parsed.start <= parsed.end && parsed.end < parsed.total ? parsed : null;
}

function validContentRange(contentRange, requestedStart, requestedEnd) {
  const parsed = parseContentRange(contentRange);
  return Boolean(
    parsed &&
      parsed.start === requestedStart &&
      parsed.end <= requestedEnd,
  );
}

function parseTotalSize(contentRange) {
  return parseContentRange(contentRange)?.total ?? null;
}

/**
 * One remote database file: block cache, request coalescing, and detection of
 * the file being republished underneath an open read.
 */
class RemoteFile {
  constructor(url, { transport, asyncFetch, blockSize, cacheBytes }) {
    this.url = url;
    // transport is null when the engine refuses synchronous binary XHR.
    this.transport = transport;
    this.asyncFetch = asyncFetch || asyncFetchRange;
    this.blockSize = blockSize;
    this.maxBlocks = Math.max(8, Math.floor(cacheBytes / blockSize));
    this.blocks = new Map();
    this.size = null;
    this.etag = null;
    this.bytesFetched = 0;
    this.requestCount = 0;
    // SQLite asks for one page at a time, so a long sequential scan (reading a
    // transcript blob, say) would otherwise become one request per page. Track
    // adjacency and ramp a readahead window up while access stays sequential.
    this.nextSequentialBlock = -1;
    this.readAhead = 1;
    // Ranges a faulted read asked for but could not fetch synchronously.
    this.pending = [];
  }

  open() {
    // The first ranged read doubles as the size probe: Content-Range carries
    // the total, so no extra HEAD round trip is needed.
    this.fetchRun(0, 1);
    if (this.size === null) throw new RangeUnsupported(this.url);
    return this;
  }

  read(offset, length) {
    const out = new Uint8Array(length);
    if (this.size !== null && offset >= this.size) return { bytes: out, short: length };
    const available = this.size === null ? length : Math.min(length, this.size - offset);
    const firstBlock = Math.floor(offset / this.blockSize);
    const lastBlock = Math.floor((offset + available - 1) / this.blockSize);
    this.ensure(firstBlock, lastBlock);

    let written = 0;
    for (let index = firstBlock; index <= lastBlock; index += 1) {
      const block = this.blocks.get(index);
      if (!block) break;
      const blockStart = index * this.blockSize;
      const from = Math.max(0, offset - blockStart);
      const to = Math.min(block.length, offset + available - blockStart);
      if (to <= from) continue;
      out.set(block.subarray(from, to), written);
      written += to - from;
      // Refresh this block's position in the LRU ordering.
      this.blocks.delete(index);
      this.blocks.set(index, block);
    }
    return { bytes: out, short: length - written };
  }

  /** Fetch every missing block in [first, last] using as few requests as possible. */
  ensure(first, last) {
    let runStart = null;
    for (let index = first; index <= last + 1; index += 1) {
      const missing = index <= last && !this.blocks.has(index);
      if (missing && runStart === null) {
        runStart = index;
      } else if (!missing && runStart !== null) {
        this.fetchRun(runStart, index - runStart);
        runStart = null;
      }
    }
  }

  fetchRun(firstBlock, blockCount) {
    if (!this.transport) {
      // No synchronous transport: note what was wanted and let the caller
      // prefetch it, then replay the query.
      this.want(firstBlock, blockCount);
      throw new NeedsPrefetch(this.url);
    }
    if (firstBlock === this.nextSequentialBlock) {
      this.readAhead = Math.min(this.readAhead * 2, MAX_READAHEAD_BLOCKS);
    } else {
      this.readAhead = 1;
    }
    const wanted = blockCount + this.readAhead - 1;
    const start = firstBlock * this.blockSize;
    let end = start + wanted * this.blockSize - 1;
    if (this.size !== null) end = Math.min(end, this.size - 1);
    if (end < start) return;
    this.nextSequentialBlock = Math.floor(end / this.blockSize) + 1;

    const result = this.transport(this.url, start, end);
    this.requestCount += 1;
    this.bytesFetched += result.bytes.length;

    if (this.etag === null) {
      this.etag = result.etag || "";
    } else if (result.etag && result.etag !== this.etag) {
      // The build republished this file between reads, so cached blocks may
      // belong to the previous revision and the read cannot be trusted.
      this.blocks.clear();
      this.etag = result.etag;
      throw new RemoteFileChanged(this.url);
    }

    const payload = result.bytes;
    let payloadStart = start;
    if (result.status !== 206) throw new RangeUnsupported(this.url);
    const total = parseTotalSize(result.contentRange);
    if (total === null) throw new RangeUnsupported(this.url);
    this.size = total;

    for (let offset = 0; offset + payloadStart < payloadStart + payload.length; offset += this.blockSize) {
      if ((payloadStart + offset) % this.blockSize !== 0) break;
      const index = (payloadStart + offset) / this.blockSize;
      const slice = payload.subarray(offset, offset + this.blockSize);
      if (!slice.length) break;
      this.blocks.set(index, slice);
    }
    this.evict();
  }

  /** Record a wanted span, growing it by the current readahead window. */
  want(firstBlock, blockCount) {
    const readAhead = firstBlock === this.nextSequentialBlock
      ? Math.min(this.readAhead * 2, MAX_READAHEAD_BLOCKS)
      : 1;
    this.readAhead = readAhead;
    const start = firstBlock * this.blockSize;
    let end = start + (blockCount + readAhead - 1) * this.blockSize - 1;
    if (this.size !== null) end = Math.min(end, this.size - 1);
    if (end < start) return;
    this.nextSequentialBlock = Math.floor(end / this.blockSize) + 1;
    this.pending.push([start, end]);
  }

  /**
   * Fetch everything a faulted query asked for. Returns whether anything was
   * retrieved, so the caller can stop replaying when no progress is possible.
   */
  async prefetch() {
    const spans = mergeSpans(this.pending);
    this.pending = [];
    if (!spans.length) return false;
    const results = await Promise.all(
      spans.map(([start, end]) => this.asyncFetch(this.url, start, end)),
    );
    for (let index = 0; index < spans.length; index += 1) {
      this.absorb(results[index], spans[index][0]);
    }
    return true;
  }

  absorb(result, start) {
    this.requestCount += 1;
    this.bytesFetched += result.bytes.length;
    if (this.etag === null) {
      this.etag = result.etag || "";
    } else if (result.etag && result.etag !== this.etag) {
      this.blocks.clear();
      this.etag = result.etag;
      throw new RemoteFileChanged(this.url);
    }
    let payload = result.bytes;
    let payloadStart = start;
    if (result.status !== 206) throw new RangeUnsupported(this.url);
    const total = parseTotalSize(result.contentRange);
    if (total === null) throw new RangeUnsupported(this.url);
    this.size = total;
    for (let offset = 0; offset < payload.length; offset += this.blockSize) {
      if ((payloadStart + offset) % this.blockSize !== 0) break;
      const slice = payload.subarray(offset, offset + this.blockSize);
      if (!slice.length) break;
      this.blocks.set((payloadStart + offset) / this.blockSize, slice);
    }
    this.evict();
  }

  /** Learn the file size before SQLite ever opens it. */
  async openAsync() {
    const result = await this.asyncFetch(this.url, 0, this.blockSize - 1);
    this.absorb(result, 0);
    if (this.size === null) throw new RangeUnsupported(this.url);
    return this;
  }

  evict() {
    while (this.blocks.size > this.maxBlocks) {
      const oldest = this.blocks.keys().next().value;
      if (oldest === undefined) break;
      this.blocks.delete(oldest);
    }
  }
}

/** Install the VFS and return a function that opens remote databases. */
export function installHttpVfs(sqlite3, options = {}) {
  const capi = sqlite3.capi;
  const wasm = sqlite3.wasm;
  const vfsName = options.vfsName || VFS_NAME;

  if (capi.sqlite3_vfs_find(vfsName)) {
    return remoteOpener(sqlite3, vfsName);
  }

  const blockSize = options.blockSize || DEFAULT_BLOCK_SIZE;
  const cacheBytes = options.cacheBytes || DEFAULT_CACHE_BYTES;
  const transport =
    options.transport !== undefined
      ? options.transport
      : syncTransportUsable()
        ? xhrTransport
        : null;
  const asyncFetch = options.asyncFetch || asyncFetchRange;

  const registry = new Map();
  const openFiles = new Map();
  let handleCounter = 0;
  let pendingError = null;

  const vfs = new capi.sqlite3_vfs();
  const io = new capi.sqlite3_io_methods();
  vfs.$iVersion = 2;
  vfs.$szOsFile = capi.sqlite3_file.structInfo.sizeof;
  vfs.$mxPathname = 512;
  vfs.$zName = wasm.allocCString(vfsName);
  io.$iVersion = 1;
  vfs.addOnDispose(vfs.$zName, io);

  const defaultPointer = capi.sqlite3_vfs_find(null);
  if (defaultPointer) {
    const fallback = new capi.sqlite3_vfs(defaultPointer);
    vfs.$xRandomness = fallback.$xRandomness;
    vfs.$xSleep = fallback.$xSleep;
    vfs.$xCurrentTime = fallback.$xCurrentTime;
    vfs.$xCurrentTimeInt64 = fallback.$xCurrentTimeInt64;
    fallback.dispose();
  }

  const ioMethods = {
    xClose(pFile) {
      const file = openFiles.get(pFile);
      if (file) {
        file.blocks.clear();
        openFiles.delete(pFile);
      }
      return 0;
    },
    xRead(pFile, pDest, n, offset64) {
      const file = openFiles.get(pFile);
      if (!file) return capi.SQLITE_IOERR_READ;
      try {
        const { bytes, short } = file.read(Number(offset64), Number(n));
        wasm.heap8u().set(bytes, Number(pDest));
        return short > 0 ? capi.SQLITE_IOERR_SHORT_READ : 0;
      } catch (error) {
        pendingError = error;
        return capi.SQLITE_IOERR_READ;
      }
    },
    xWrite() {
      return capi.SQLITE_READONLY;
    },
    xTruncate() {
      return capi.SQLITE_READONLY;
    },
    xSync() {
      return 0;
    },
    xFileSize(pFile, pSize64) {
      const file = openFiles.get(pFile);
      if (!file || file.size === null) return capi.SQLITE_IOERR_FSTAT;
      wasm.poke(pSize64, BigInt(file.size), "i64");
      return 0;
    },
    xLock() {
      return 0;
    },
    xUnlock() {
      return 0;
    },
    xCheckReservedLock(pFile, pOut) {
      wasm.poke(pOut, 0, "i32");
      return 0;
    },
    xFileControl() {
      return capi.SQLITE_NOTFOUND;
    },
    xSectorSize() {
      return blockSize;
    },
    xDeviceCharacteristics() {
      // Declaring the file immutable frees SQLite from journal and locking
      // bookkeeping it could not perform over HTTP anyway.
      return capi.SQLITE_IOCAP_IMMUTABLE;
    },
  };

  const vfsMethods = {
    xOpen(pVfs, zName, pFile, flags, pOutFlags) {
      const name = zName ? wasm.cstrToJs(zName) : "";
      const file = registry.get(name);
      if (!file) return capi.SQLITE_CANTOPEN;
      try {
        if (file.size === null) file.open();
      } catch (error) {
        pendingError = error;
        return capi.SQLITE_CANTOPEN;
      }
      const handle = new capi.sqlite3_file(pFile);
      handle.$pMethods = io.pointer;
      handle.dispose();
      openFiles.set(pFile, file);
      if (pOutFlags) wasm.poke(pOutFlags, capi.SQLITE_OPEN_READONLY, "i32");
      return 0;
    },
    xDelete() {
      return capi.SQLITE_READONLY;
    },
    xAccess(pVfs, zName, flags, pOut) {
      // Journals, WAL files and hot-journal probes never exist for a remote
      // read-only database; saying so plainly stops SQLite asking again.
      const name = zName ? wasm.cstrToJs(zName) : "";
      wasm.poke(pOut, registry.has(name) ? 1 : 0, "i32");
      return 0;
    },
    xFullPathname(pVfs, zName, nOut, pOut) {
      const encoded = new TextEncoder().encode(wasm.cstrToJs(zName));
      if (encoded.length + 1 > nOut) return capi.SQLITE_CANTOPEN;
      const heap = wasm.heap8u();
      heap.set(encoded, Number(pOut));
      heap[Number(pOut) + encoded.length] = 0;
      return 0;
    },
    xGetLastError(pVfs, nOut, pOut) {
      const message = pendingError ? String(pendingError.message || pendingError) : "";
      const encoded = new TextEncoder().encode(message.slice(0, Math.max(0, Number(nOut) - 1)));
      const heap = wasm.heap8u();
      heap.set(encoded, Number(pOut));
      heap[Number(pOut) + encoded.length] = 0;
      return 0;
    },
  };

  if (!vfs.$xRandomness) {
    vfsMethods.xRandomness = (pVfs, nOut, pOut) => {
      const heap = wasm.heap8u();
      for (let i = 0; i < nOut; i += 1) heap[Number(pOut) + i] = (Math.random() * 255000) & 255;
      return nOut;
    };
  }
  if (!vfs.$xSleep) vfsMethods.xSleep = () => 0;
  if (!vfs.$xCurrentTime) {
    vfsMethods.xCurrentTime = (pVfs, pOut) => {
      wasm.poke(pOut, 2440587.5 + Date.now() / 86400000, "double");
      return 0;
    };
  }
  if (!vfs.$xCurrentTimeInt64) {
    vfsMethods.xCurrentTimeInt64 = (pVfs, pOut) => {
      wasm.poke(pOut, BigInt(Date.now()) + 210866760000000n, "i64");
      return 0;
    };
  }

  sqlite3.vfs.installVfs({
    io: { struct: io, methods: ioMethods },
    vfs: { struct: vfs, methods: vfsMethods },
  });

  sqlite3.podsearchHttpVfs = {
    vfsName,
    registry,
    blockSize,
    cacheBytes,
    transport,
    asyncFetch,
    nextHandle: () => `remote-${handleCounter++}`,
    takeError: () => {
      const error = pendingError;
      pendingError = null;
      return error;
    },
  };
  return remoteOpener(sqlite3, vfsName);
}

function remoteOpener(sqlite3, vfsName) {
  const state = sqlite3.podsearchHttpVfs;

  /**
   * Open a remote database. The URL is registered under an opaque handle so a
   * `?v=` cache-busting query is never mistaken for SQLite URI parameters.
   */
  return async function openRemote(url, options = {}) {
    const handle = state.nextHandle();
    const transport =
      options.transport !== undefined ? options.transport : state.transport;
    const file = new RemoteFile(url, {
      transport,
      asyncFetch: options.asyncFetch || state.asyncFetch,
      blockSize: options.blockSize || state.blockSize,
      cacheBytes: options.cacheBytes || state.cacheBytes,
    });
    // Probe asynchronously even when synchronous XHR is available. If a host
    // ignores Range, fetch() lets us cancel the body before a multi-hundred-MB
    // response is allocated; the synchronous transport cannot.
    await file.openAsync();
    state.registry.set(handle, file);
    let db;
    try {
      db = new sqlite3.oo1.DB({ filename: handle, flags: "r", vfs: vfsName });
    } catch (error) {
      state.registry.delete(handle);
      throw state.takeError() || error;
    }
    // A generous page cache is cheap memory that buys away network round
    // trips, which are by far the dominant cost here.
    db.exec("PRAGMA cache_size = -8000");
    const closeUnderlying = db.close.bind(db);
    db.close = () => {
      closeUnderlying();
      state.registry.delete(handle);
    };
    db.remoteStats = () => ({
      url,
      bytesFetched: file.bytesFetched,
      requestCount: file.requestCount,
      size: file.size,
    });
    db.takeRemoteError = state.takeError;
    db.prefetchPending = () => file.prefetch();
    db.usesSyncTransport = Boolean(transport);
    return db;
  };
}
