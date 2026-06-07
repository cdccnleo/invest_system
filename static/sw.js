const CACHE_VERSION = 'v1';
const STATIC_CACHE  = `investpilot-static-${CACHE_VERSION}`;
const DYNAMIC_CACHE = `investpilot-dynamic-${CACHE_VERSION}`;

const PRECACHE_URLS = [
  '/',
  '/manifest.json',
];

// ── Install ───────────────────────────────────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE)
      .then(cache => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

// ── Activate ─────────────────────────────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(key => key !== STATIC_CACHE && key !== DYNAMIC_CACHE)
          .map(key => caches.delete(key))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch ─────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET and chrome-extension requests
  if (request.method !== 'GET' || url.protocol === 'chrome-extension:') return;

  // Skip Streamlit's internal WebSocket & live-reload endpoints
  if (url.pathname.startsWith('/_stcore/')) return;

  // ── Static assets (fonts, images, JS/CSS) → StaleWhileRevalidate ────────────
  if (
    url.pathname.startsWith('/static/') ||
    url.pathname.match(/\.(js|css|png|jpg|jpeg|svg|ico|woff2?|ttf)$/)
  ) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // ── API calls (market data, quotes) → NetworkFirst ─────────────────────────
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.match(/(_stcore|health|quotes|market)/)
  ) {
    event.respondWith(networkFirst(request));
    return;
  }

  // ── App Shell (HTML pages) → CacheFirst ────────────────────────────────────
  if (request.mode === 'navigate') {
    event.respondWith(cacheFirst(request));
    return;
  }

  // ── Default: NetworkOnly ───────────────────────────────────────────────────
  // (let request pass through without caching)
});

// ── Cache strategies ──────────────────────────────────────────────────────────

/** CacheFirst — 静态资源优先读缓存，无缓存则网络获取 */
async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(STATIC_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return new Response('Offline', { status: 503, statusText: 'Service Unavailable' });
  }
}

/** NetworkFirst — API/动态数据优先网络，网络失败回退缓存 */
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(DYNAMIC_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: 'Offline' }), {
      status: 503,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

/** StaleWhileRevalidate — 先返回缓存，同时后台更新缓存 */
async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);

  const fetchPromise = fetch(request).then(response => {
    if (response.ok) cache.put(request, response.clone());
    return response;
  }).catch(() => null);

  return cached || fetchPromise;
}
