(function () {
    "use strict";

    const button = document.getElementById("pushNotificationButton");
    const status = document.getElementById("pushNotificationStatus");

    if (!button || !status) return;

    const csrfMeta = document.querySelector('meta[name="csrf-token"]');
    const csrfToken = csrfMeta ? csrfMeta.content : "";

    function setStatus(message) {
        status.textContent = message;
    }

    function setSubscribedState(subscribed) {
        button.textContent = subscribed
            ? "Turn off notifications"
            : "Enable new mix notifications";
        button.dataset.subscribed = subscribed ? "true" : "false";
    }

    function base64UrlToBytes(value) {
        const padding = "=".repeat((4 - value.length % 4) % 4);
        const base64 = (value + padding)
            .replace(/-/g, "+")
            .replace(/_/g, "/");
        const raw = window.atob(base64);
        const bytes = new Uint8Array(raw.length);

        for (let i = 0; i < raw.length; i++) {
            bytes[i] = raw.charCodeAt(i);
        }
        return bytes;
    }

    async function postJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken
            },
            body: JSON.stringify(payload)
        });

        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(result.error || "Notification request failed.");
        }
        return result;
    }

    async function saveSubscription(subscription) {
        const data = subscription.toJSON();

        if (!data.keys || !data.keys.p256dh || !data.keys.auth) {
            throw new Error("The browser returned incomplete subscription data.");
        }

        await postJson("/api/push/subscribe", {
            endpoint: data.endpoint,
            keys: {
                p256dh: data.keys.p256dh,
                auth: data.keys.auth
            }
        });
    }

    async function enableNotifications(registration) {
        if (!window.isSecureContext) {
            throw new Error("Notifications require a secure HTTPS connection.");
        }

        const permission = await Notification.requestPermission();

        if (permission !== "granted") {
            throw new Error(
                permission === "denied"
                    ? "Notifications are blocked in your browser settings."
                    : "Notification permission was not granted."
            );
        }

        let subscription = await registration.pushManager.getSubscription();

        if (!subscription) {
            const response = await fetch("/api/push/public-key", {
                credentials: "same-origin",
                cache: "no-store"
            });

            const result = await response.json().catch(() => ({}));

            if (!response.ok || !result.publicKey) {
                throw new Error(
                    result.error || "The push service is not configured."
                );
            }

            subscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: base64UrlToBytes(result.publicKey)
            });
        }

        await saveSubscription(subscription);

        setSubscribedState(true);
        setStatus(
            "Notifications are enabled. We will alert you when a new mix is published."
        );
    }

    async function disableNotifications(registration) {
        const subscription = await registration.pushManager.getSubscription();

        if (!subscription) {
            setSubscribedState(false);
            setStatus("Notifications are already turned off.");
            return;
        }

        await postJson("/api/push/unsubscribe", {
            endpoint: subscription.endpoint
        });

        const removed = await subscription.unsubscribe();

        if (!removed) {
            throw new Error("The browser could not remove the subscription.");
        }

        setSubscribedState(false);
        setStatus("Notifications have been turned off.");
    }

    async function initialise() {
        if (
            !("serviceWorker" in navigator) ||
            !("PushManager" in window) ||
            !("Notification" in window)
        ) {
            button.disabled = true;
            setStatus("Push notifications are not supported by this browser.");
            return;
        }

        try {
            const registration = await navigator.serviceWorker.ready;
            const subscription =
                await registration.pushManager.getSubscription();

            setSubscribedState(Boolean(subscription));

            if (subscription) {
                setStatus("Notifications are enabled for this browser.");
            } else if (Notification.permission === "denied") {
                setStatus(
                    "Notifications are blocked in browser settings. Allow them to subscribe."
                );
            }
        } catch (error) {
            console.error("Could not check notification status:", error);
            setStatus("Could not check notification status. Reload the page.");
        }
    }

    button.addEventListener("click", async function () {
        button.disabled = true;
        setStatus("Updating notification settings...");

        try {
            const registration = await navigator.serviceWorker.ready;

            if (button.dataset.subscribed === "true") {
                await disableNotifications(registration);
            } else {
                await enableNotifications(registration);
            }
        } catch (error) {
            console.error("Push notification setup failed:", error);
            setStatus(error.message || "Could not update notification settings.");
        } finally {
            button.disabled = false;
        }
    });

    initialise();
})();
