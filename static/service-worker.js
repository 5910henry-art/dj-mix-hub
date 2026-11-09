const CACHE_NAME = "dj-mix-hub-pwa-v3";

const APP_ASSETS = [
    "/static/offline.html",
    "/static/waking.html",
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
                keys.filter(key =>
                    key.startsWith("dj-mix-hub-pwa-") &&
                    key !== CACHE_NAME
                ).map(key => caches.delete(key))
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
        if (
            url.pathname === "/static/waking.html" ||
            url.pathname === "/static/offline.html"
        ) {
            event.respondWith(
                caches.match(url.pathname)
                    .then(cached => cached || fetch(request))
            );
            return;
        }

        event.respondWith((async () => {
            const controller = new AbortController();

            const timeout = setTimeout(
                () => controller.abort(),
                5000
            );

            try {
                const response = await fetch(request, {
                    signal: controller.signal
                });

                if (
                    response.status === 502 ||
                    response.status === 503 ||
                    response.status === 504
                ) {
                    const waking = await caches.match(
                        "/static/waking.html"
                    );

                    if (waking) return waking;
                }

                return response;

            } catch (error) {
                const waking = await caches.match(
                    "/static/waking.html"
                );

                if (waking) return waking;

                const offline = await caches.match(
                    "/static/offline.html"
                );

                if (offline) return offline;

                return Response.error();

            } finally {
                clearTimeout(timeout);
            }
        })());

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

self.addEventListener("push", event => {
    let data = {};

    try {
        data = event.data ? event.data.json() : {};
    } catch (error) {
        data = {
            body: event.data
                ? event.data.text()
                : "A new mix is available."
        };
    }

    event.waitUntil(
        self.registration.showNotification(
            data.title || "DJ MIX HUB",
            {
                body: data.body || "A new mix is available.",
                icon: "/static/pwa/icon-192.png",
                badge: "/static/pwa/icon-192.png",
                data: { url: data.url || "/" }
            }
        )
    );
});

self.addEventListener("notificationclick", event => {
    event.notification.close();

    const rawUrl =
        (event.notification.data && event.notification.data.url) || "/";

    const target = new URL(rawUrl, self.location.origin);

    if (target.origin !== self.location.origin) return;

    event.waitUntil(
        self.clients.matchAll({
            type: "window",
            includeUncontrolled: true
        }).then(windowClients => {
            for (const client of windowClients) {
                if (new URL(client.url).origin === self.location.origin) {
                    return client.focus();
                }
            }

            return self.clients.openWindow(target.href);
        })
    );
});
