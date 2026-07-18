// Runtime transport test: drive the generated client with a mock fetch and
// assert Wreath's wire conventions. Run with a TypeScript-aware Node (>=22.6
// with type stripping). Exits non-zero on the first failed assertion.
import assert from "node:assert/strict";

import { createWreathClient, WreathApiError } from "./generated/client.ts";

type Captured = {
  url: string;
  method: string;
  headers: Headers;
  body: string | null;
};

function mockFetch(
  captured: Captured[],
  response: () => Response,
): typeof globalThis.fetch {
  return (async (input: string | URL | Request, init?: RequestInit) => {
    const url = input instanceof URL ? input.toString() : String(input);
    captured.push({
      url,
      method: String(init?.method),
      headers: new Headers(init?.headers),
      body: (init?.body as string | undefined) ?? null,
    });
    return response();
  }) as typeof globalThis.fetch;
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function main(): Promise<void> {
  // --- success path: path escaping, query omission, header merge, decode ---
  {
    const captured: Captured[] = [];
    const client = createWreathClient({
      baseUrl: "https://api.example.com/",
      fetch: mockFetch(captured, () => jsonResponse(200, { name: "ok", total: 1 })),
      headers: async () => ({ authorization: "Bearer t" }),
    });
    const result = await client.getItem({ itemId: 42, expand: false, traceId: "a b/c" });
    assert.equal(captured.length, 1);
    const call = captured[0];
    // Trailing slash on baseUrl is normalized; path id is encoded; a defined
    // false value IS sent (only undefined is omitted); the header merges in.
    assert.equal(
      call.url,
      "https://api.example.com/items/42?expand=false&trace_id=a+b%2Fc",
    );
    assert.equal(call.method, "GET");
    assert.equal(call.headers.get("authorization"), "Bearer t");
    assert.deepEqual(result, { name: "ok", total: 1 });
  }

  // --- query omission when a value is undefined ---
  {
    const captured: Captured[] = [];
    const client = createWreathClient({
      baseUrl: "https://api.example.com",
      fetch: mockFetch(captured, () => jsonResponse(200, { items: [], total: 0 })),
    });
    await client.getItems({ limit: 5 });
    assert.equal(captured[0].url, "https://api.example.com/items?limit=5");
    // cursor was undefined and must not appear.
    assert.ok(!captured[0].url.includes("cursor"));
  }

  // --- body serialization on a mutation ---
  {
    const captured: Captured[] = [];
    const client = createWreathClient({
      baseUrl: "https://api.example.com",
      fetch: mockFetch(captured, () => jsonResponse(200, {})),
    });
    await client.createItem({
      name: "widget",
      price: 2.5,
      priority: "high",
      kind: "premium",
      tags: [],
      metadata: {},
    });
    assert.equal(captured[0].method, "POST");
    assert.equal(captured[0].headers.get("content-type"), "application/json");
    assert.deepEqual(JSON.parse(captured[0].body ?? ""), {
      name: "widget",
      price: 2.5,
      priority: "high",
      kind: "premium",
      tags: [],
      metadata: {},
    });
  }

  // --- structured error on non-2xx, preserving status and headers ---
  {
    const client = createWreathClient({
      baseUrl: "https://api.example.com",
      fetch: mockFetch([], () =>
        new Response(JSON.stringify({ detail: "nope" }), {
          status: 404,
          statusText: "Not Found",
          headers: { "content-type": "application/json", "x-trace": "z" },
        }),
      ),
    });
    await assert.rejects(
      () => client.getItem({ itemId: 1, expand: false, traceId: "" }),
      (error: unknown) => {
        assert.ok(error instanceof WreathApiError);
        assert.equal(error.status, 404);
        assert.equal(error.statusText, "Not Found");
        assert.equal(error.headers.get("x-trace"), "z");
        assert.deepEqual(error.body, { detail: "nope" });
        return true;
      },
    );
  }

  console.log("mock-fetch transport checks passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
