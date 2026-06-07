"""
pwa_service.py — PWA Service Worker 生成器
生成 sw.js 到项目 static/ 目录，供 dashboard PWA 使用。

Cache 策略
----------
  - App Shell (HTML / JS / CSS)   → CacheFirst
  - API / dynamic data             → NetworkFirst + fallback to cache
  - Static assets (fonts/icons)   → StaleWhileRevalidate
  - manifest.json                  → CacheFirst (always fresh on activate)

使用
----
  python -m scripts.pwa_service
  → 将 static/sw.js 写入 static/ 目录
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATIC_DIR = ROOT / "static"
STATIC_DIR.mkdir(exist_ok=True)

SW_PATH = STATIC_DIR / "sw.js"

# ── Service Worker 源码 ────────────────────────────────────────────────────────

SERVICE_WORKER_SOURCE = r"""
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
"""

# ── 主入口 ────────────────────────────────────────────────────────────────────

def generate_sw(out_path: Path | str = SW_PATH) -> str:
    """将 service worker 源码写入目标路径，返回写入内容。"""
    content = SERVICE_WORKER_SOURCE.strip() + "\n"
    Path(out_path).write_text(content, encoding="utf-8")
    return content


if __name__ == "__main__":
    content = generate_sw()
    print(f"[pwa_service] Service worker written to: {SW_PATH}")
    print(f"[pwa_service] Size: {len(content):,} bytes")
