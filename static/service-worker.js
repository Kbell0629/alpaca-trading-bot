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
            caches.match(event.request).then(c => c || new Response('Offline', {status: 503}))
        )
    );
});

// Handle ntfy push notifications (item 19 backup channel).
self.addEventListener('push', event => {
    const data = event.data ? event.data.json() : {};
    const title = data.title || 'Trading Bot Alert';
    const options = {
        body: data.body || '',
        icon: '/static/icon-192.png',
        badge: '/static/icon-192.png',
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
