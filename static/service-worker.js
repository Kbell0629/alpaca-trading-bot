// Round-11 expansion item 16: PWA service worker.
// Minimal — just lets the dashboard install to home screen and
// stay open offline (showing last-cached state). We deliberately
// don't aggressively cache the dashboard HTML so users always see
// the latest version on online refresh.

const CACHE_VERSION = 'alpaca-bot-v1';
const STATIC_ASSETS = [
    '/static/manifest.json',
];

self.addEventListener('install', event => {
    event.waitUntil(
        caches.open(CACHE_VERSION)
            .then(cache => cache.addAll(STATIC_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener('activate', event => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys.filter(k => k !== CACHE_VERSION)
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', event => {
    // Network-first for everything except static assets.
    // Static: cache-first (offline survival).
    // API/HTML: network-first, fall back to cache only on failure.
    const url = new URL(event.request.url);
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(cached => cached || fetch(event.request))
        );
        return;
    }
    // For everything else, try network. Don't cache API responses
    // (they're per-user authenticated).
    event.respondWith(
        fetch(event.request).catch(() =>
            caches.match(event.request).then(c => {
                if (c) return c;
                // Round-12 audit fix: previous fallback returned plain-text
                // "Offline" with status 503 for EVERY uncached request. The
                // dashboard's JSON-consuming fetch() calls then tripped on
                // `response.json()` parse errors rather than getting a clean
                // "I'm offline" signal. Return a structured JSON envelope
                // for /api/* requests (the dashboard JS detects offline=true
                // on any response) and plain HTML for navigation requests
                // so the browser's own offline UI takes over cleanly.
                const isApi = url.pathname.startsWith('/api/');
                if (isApi) {
                    return new Response(
                        JSON.stringify({
                            error: 'offline',
                            offline: true,
                            message: 'No network connection. Reconnect to refresh.'
                        }),
                        {
                            status: 503,
                            headers: {'Content-Type': 'application/json'}
                        }
                    );
                }
                // Navigation / HTML request offline — render a minimal page
                // the user can read (vs. a broken-looking 503 error page).
                return new Response(
                    '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Offline</title>'
                    + '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    + '<style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
                    + 'background:#0a0e17;color:#e2e8f0;padding:40px;text-align:center;'
                    + 'line-height:1.5}h1{font-size:22px}button{margin-top:16px;padding:10px 24px;'
                    + 'background:#3b82f6;color:#fff;border:0;border-radius:6px;font-size:14px;'
                    + 'cursor:pointer}</style></head><body>'
                    + '<h1>📡 Offline</h1>'
                    + '<p>The trading bot dashboard needs a network connection.</p>'
                    + '<p>Your bot is still running on Railway — this is just the UI.</p>'
                    + '<button onclick="location.reload()">Retry</button>'
                    + '</body></html>',
                    {
                        status: 503,
                        headers: {'Content-Type': 'text/html; charset=utf-8'}
                    }
                );
            })
        )
    );
});

// Handle ntfy push notifications (item 19 backup channel).
self.addEventListener('push', event => {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'Trading Bot Alert';
    // Use the single SVG shipped in manifest.json; the previous hardcoded
    // icon-192.png never existed on disk (404 → browser falls back to a
    // generic app icon). SVG works in Chrome/Edge/Safari push notifs.
    const options = {
        body: data.body || '',
        icon: '/static/icon.svg',
        badge: '/static/icon.svg',
        tag: data.tag || 'trading-bot',
        data: { url: data.url || '/' },
    };
    event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    event.waitUntil(
        clients.openWindow(event.notification.data.url || '/')
    );
});
