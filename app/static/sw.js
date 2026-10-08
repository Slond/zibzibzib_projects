// Cache public assets only. Never retain authenticated pages, API responses,
// screenshots or VPN private keys on a shared browser.
const CACHE = 'zib-public-v2';
const ASSETS = ['/static/offline.html', '/static/icon-192.png', '/static/icon-512.png', '/static/manifest.json'];
self.addEventListener('install', event => {
    event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
    event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('finance-') || (key.startsWith('zib-public-') && key !== CACHE)).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
    const request = event.request;
    if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) return;
    if (ASSETS.includes(new URL(request.url).pathname)) {
        event.respondWith(caches.match(request).then(cached => cached || fetch(request)));
    } else if (request.mode === 'navigate') {
        event.respondWith(fetch(request).catch(() => caches.match('/static/offline.html')));
    }
});
