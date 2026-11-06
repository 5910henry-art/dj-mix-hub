
const CACHE_NAME = "dj-mix-hub-pwa-v2";

const APP_ASSETS = [
    "/static/offline.html",
    "/static/css/style.css",
    "/static/manifest.json",
    "/static/pwa/icon-192.png",
    "/static/pwa/icon-512.png"
];

self.addEventListener("install", event => {
    event.waitUntil(
        caches.open(CACHE_NAME)
            .then(cache => cache.addAll(APP_ASSETS))
            .then(() => self.skipWaiting())
    );
});

self.addEventListener("activate", event => {
    event.waitUntil(
        caches.keys()
            .then(keys => Promise.all(
                keys.filter(key => key.startsWith("dj-mix-hub-pwa-") &&
                    key !== CACHE_NAME)
                    .map(key => caches.delete(key))
            ))
            .then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", event => {
    const request = event.request;

    if (request.method !== "GET") return;

    const url = new URL(request.url);

    if (url.origin !== self.location.origin) return;

    if (request.mode === "navigate") {
        event.respondWith(
            fetch(request).catch(async () => {
                return await caches.match("/static/offline.html");
            })
        );
        return;
    }

    const cacheable = APP_ASSETS.includes(url.pathname);

    if (cacheable) {
        event.respondWith(
            caches.match(request).then(cached => {
                if (cached) return cached;

                return fetch(request).then(response => {
                    if (response.ok) {
                        const copy = response.clone();
                        caches.open(CACHE_NAME)
                            .then(cache => cache.put(request, copy));
                    }
                    return response;
                });
            })
        );
    }
});
