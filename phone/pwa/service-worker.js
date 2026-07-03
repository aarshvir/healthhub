/*
 * HealthHub PWA service worker — live-first, offline-capable cockpit.
 *
 * The dashboard is a single self-contained HTML file rebuilt every cycle (all values baked in
 * server-side). So to "refresh and see the latest" we serve the SHELL network-first when online
 * (falling back to the last cached cycle offline), not cache-first. JSON artifacts are likewise
 * network-first. Offline, the cockpit opens on the last cached cycle and recomputes each value's
 * age/freshness on the device clock (§A rule 6 — never pretends a cached value is live).
 */
const CACHE = 'healthhub-v2';
const ASSETS = [
  'dashboard.html', 'index.html', 'manifest.json',
  'icon-192.png', 'icon-512.png', 'icon-maskable-512.png',
  'metrics.json', 'trend.json', 'wearables.json',
  'correlation.json', 'labs.json', 'reversal.json', 'health.json'
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

// Network-first for the HTML shell + JSON (freshest when online), cache fallback offline.
function networkFirst(request) {
  return fetch(request, { cache: 'no-store' }).then((resp) => {
    const copy = resp.clone();
    caches.open(CACHE).then((c) => c.put(request, copy));
    return resp;
  }).catch(() => caches.match(request).then((hit) => hit || caches.match('dashboard.html')));
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const isShell = event.request.mode === 'navigate'
    || url.pathname.endsWith('.html') || url.pathname.endsWith('/');
  const isData = url.pathname.endsWith('.json');
  if (isShell || isData) {
    event.respondWith(networkFirst(event.request));
  } else {
    event.respondWith(caches.match(event.request).then((hit) => hit || fetch(event.request)));
  }
});
