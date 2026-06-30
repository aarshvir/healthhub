/*
 * HealthHub PWA service worker — offline-first cockpit.
 *
 * Caches the dashboard shell + the JSON artifacts so the cockpit opens with NO connection.
 * It serves the last successfully cached cycle output; the dashboard then recomputes each
 * value's age/freshness on the device clock (§A rule 6 — never pretends a cached value is
 * live). On reconnect, a fresh cycle overwrites the artifacts and the next load updates.
 */
const CACHE = 'healthhub-v1';
const ASSETS = [
  'dashboard.html',
  'manifest.json',
  'metrics.json',
  'trend.json',
  'wearables.json'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => Promise.allSettled(ASSETS.map((a) => c.add(a))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  // Network-first for the JSON artifacts (freshest when online), falling back to cache offline.
  // Cache-first for the shell so it always opens instantly.
  const url = new URL(event.request.url);
  const isData = url.pathname.endsWith('.json');
  if (isData) {
    event.respondWith(
      fetch(event.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
        return resp;
      }).catch(() => caches.match(event.request))
    );
  } else {
    event.respondWith(
      caches.match(event.request).then((hit) => hit || fetch(event.request))
    );
  }
});
