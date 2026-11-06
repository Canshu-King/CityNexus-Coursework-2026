const SW_VERSION = "citynexus-recovery-m3-a4-v1";
const CACHE_PREFIX = "citynexus-recovery-";
const CACHE = `${CACHE_PREFIX}${SW_VERSION}`;
const NAVIGATION_FALLBACK = "/v8/live";
const LEGACY_CACHE_NAMES = new Set([
  "citynexus-trip-operations-v1",
  "citynexus-pages",
  "citynexus-static",
]);

function isHtmlResponse(response) {
  const contentType = response?.headers?.get("content-type") ?? "";
  return response?.status === 200 && contentType.includes("text/html");
}

function isSafeAssetResponse(response) {
  return Boolean(response && response.status === 200 && response.type !== "opaque");
}

async function putIfSafe(cache, request, response, destination) {
  const safe =
    destination === "document" ? isHtmlResponse(response) : isSafeAssetResponse(response);
  if (!safe) return;
  await cache.put(request, response.clone());
}

async function precacheRecoveryShell() {
  const cache = await caches.open(CACHE);
  const requests = [
    new Request(NAVIGATION_FALLBACK, { cache: "reload" }),
    new Request("/manifest.webmanifest", { cache: "reload" }),
    new Request("/favicon.svg", { cache: "reload" }),
  ];
  await Promise.all(
    requests.map(async (request) => {
      try {
        const response = await fetch(request);
        const destination =
          request.url.endsWith(NAVIGATION_FALLBACK) ? "document" : "image";
        await putIfSafe(cache, request, response, destination);
      } catch {
        // Recovery precache is best-effort. A failed install fetch must not
        // prevent a newer worker from activating and replacing a poisoned one.
      }
    }),
  );
}

async function networkFirstDocument(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    await putIfSafe(cache, request, response, "document");
    return response;
  } catch (error) {
    const exact = await cache.match(request);
    if (exact && isHtmlResponse(exact)) return exact;

    const fallback = await cache.match(NAVIGATION_FALLBACK);
    if (fallback && isHtmlResponse(fallback)) return fallback;

    throw error;
  }
}

async function staleWhileRevalidateAsset(event) {
  const request = event.request;
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const network = fetch(request).then(async (response) => {
    await putIfSafe(cache, request, response, request.destination);
    return response;
  });

  if (cached && isSafeAssetResponse(cached)) {
    event.waitUntil(network.catch(() => undefined));
    return cached;
  }

  return network;
}

self.addEventListener("install", (event) => {
  event.waitUntil(precacheRecoveryShell());
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      const doomed = keys.filter(
        (key) =>
          (key.startsWith(CACHE_PREFIX) && key !== CACHE) ||
          LEGACY_CACHE_NAMES.has(key) ||
          key.startsWith("citynexus-trip-operations-v"),
      );
      await Promise.all(doomed.map((key) => caches.delete(key)));

      /* A seam for the gates, in the same spirit as the map's camera hook: cleanup that
         cannot be observed cannot be judged.  Written into this worker's own cache, because
         nothing outside it is allowed to be an answer to an unknown URL. */
      try {
        const cache = await caches.open(CACHE);
        await cache.put(
          "/__sw_activate_report__",
          new Response(
            JSON.stringify({
              script: self.location.href,
              version: SW_VERSION,
              deleted: doomed,
              kept: keys.filter((key) => !doomed.includes(key) && key !== CACHE),
            }),
            { headers: { "content-type": "application/json" } },
          ),
        );
      } catch {
        // A report is not worth failing activation over.
      }

      await self.clients.claim();

      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        client.postMessage({
          type: "CITYNEXUS_SW_ACTIVATED",
          version: SW_VERSION,
        });
      }
    })(),
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "CITYNEXUS_SW_GET_VERSION") {
    const payload = {
      type: "CITYNEXUS_SW_VERSION",
      version: SW_VERSION,
    };
    if (event.ports?.[0]) {
      event.ports[0].postMessage(payload);
    } else if (event.source?.postMessage) {
      event.source.postMessage(payload);
    }
  }
  if (event.data?.type === "SKIP_WAITING") {
    void self.skipWaiting();
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;

  const destination = request.destination;

  /* `destination` alone is not enough: a `fetch("/something.js")` from the page arrives with
     an empty destination, and the dev server answers that path with the SPA document.  The
     path test is what keeps that case from being served as code. */
  const askedForCode = /\.(?:js|mjs|cjs|css)$/i.test(url.pathname);

  // Executable resources are always network truth. If the network request
  // fails, let that failure propagate. Returning an HTML shell for a module or
  // worker request creates the MIME mismatch that previously blanked #root.
  if (
    askedForCode ||
    destination === "script" ||
    destination === "style" ||
    destination === "worker" ||
    destination === "serviceworker"
  ) {
    /* Two failures, both measured, define this branch.  Answering a module request with the
       app shell makes the browser parse HTML as JavaScript, the app half-boots, and that
       half-booted document gets written back into the cache -- a white screen that survives a
       reload.  Refusing to serve any cached code instead leaves a device with no network
       unable to start at all.  So: network first, cache only when the cached bytes really
       are code, and otherwise fail as itself. */
    const fromCacheOrFailure = async () => {
      const cached = await caches.match(request);
      return cached && !isHtmlResponse(cached) ? cached : Response.error();
    };
    event.respondWith(
      fetch(request)
        .then(async (response) => {
          if (isHtmlResponse(response)) return fromCacheOrFailure();
          const cache = await caches.open(CACHE);
          await putIfSafe(cache, request, response, destination);
          return response;
        })
        .catch(fromCacheOrFailure),
    );
    return;
  }

  // HTML fallback is legal only for document navigation.
  if (request.mode === "navigate" || destination === "document") {
    event.respondWith(networkFirstDocument(request));
    return;
  }

  if (destination === "image" || destination === "font") {
    event.respondWith(staleWhileRevalidateAsset(event));
    return;
  }

  // All other requests remain network-first and never borrow the app shell.
  event.respondWith(fetch(request));
});
