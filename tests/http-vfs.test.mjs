import assert from "node:assert/strict";
import test from "node:test";

import {
  asyncFetchRange,
  RangeUnsupported,
} from "../site/http-vfs.js";


function responseWithHeaders(body, init) {
  return new Response(body, init);
}


test("accepts a valid partial response", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () =>
    responseWithHeaders(new Uint8Array([1, 2, 3, 4]), {
      status: 206,
      headers: {
        "Content-Range": "bytes 8-11/100",
        ETag: '"revision"',
      },
    });

  const result = await asyncFetchRange("/db.sqlite3", 8, 11);
  assert.equal(result.status, 206);
  assert.deepEqual([...result.bytes], [1, 2, 3, 4]);
  assert.equal(result.etag, '"revision"');
});


test("cancels a whole response instead of buffering it", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  let cancelled = false;
  globalThis.fetch = async () =>
    responseWithHeaders(
      new ReadableStream({
        cancel() {
          cancelled = true;
        },
      }),
      { status: 200, headers: { "Content-Length": "740000000" } },
    );

  await assert.rejects(
    asyncFetchRange("/db.sqlite3", 0, 32767),
    RangeUnsupported,
  );
  assert.equal(cancelled, true);
});


test("rejects malformed partial responses before reading them", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  let cancelled = false;
  globalThis.fetch = async () =>
    responseWithHeaders(
      new ReadableStream({
        cancel() {
          cancelled = true;
        },
      }),
      {
        status: 206,
        headers: { "Content-Range": "bytes 9-12/100" },
      },
    );

  await assert.rejects(
    asyncFetchRange("/db.sqlite3", 8, 11),
    RangeUnsupported,
  );
  assert.equal(cancelled, true);
});


test("rejects a partial response whose body is truncated", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  globalThis.fetch = async () =>
    responseWithHeaders(new Uint8Array([1, 2]), {
      status: 206,
      headers: { "Content-Range": "bytes 8-11/100" },
    });

  await assert.rejects(
    asyncFetchRange("/db.sqlite3", 8, 11),
    RangeUnsupported,
  );
});
