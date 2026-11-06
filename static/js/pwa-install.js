(function () {
    "use strict";

    let installPrompt = null;

    const button = document.getElementById("pwaInstallButton");
    const help = document.getElementById("pwaInstallHelp");
    const container = document.getElementById("pwaInstallContainer");

    if (!button || !help || !container) return;

    const isInstalled = function () {
        return window.matchMedia("(display-mode: standalone)").matches ||
            window.matchMedia("(display-mode: fullscreen)").matches ||
            navigator.standalone === true;
    };

    const hideInstallButton = function () {
        container.hidden = true;
    };

    if (isInstalled()) {
        hideInstallButton();
        return;
    }

    window.addEventListener("beforeinstallprompt", function (event) {
        event.preventDefault();
        installPrompt = event;
        button.textContent = "⬇ Install DJ MIX HUB";
        help.hidden = true;
    });

    window.addEventListener("appinstalled", function () {
        installPrompt = null;
        hideInstallButton();
    });

    button.addEventListener("click", async function () {
        if (installPrompt) {
            const promptEvent = installPrompt;
            installPrompt = null;

            try {
                await promptEvent.prompt();
                const choice = await promptEvent.userChoice;

                if (choice && choice.outcome === "accepted") {
                    help.textContent =
                        "DJ MIX HUB installation started successfully.";
                    help.hidden = false;
                } else {
                    help.textContent =
                        "Installation was not completed. You can try again later.";
                    help.hidden = false;
                }
            } catch (error) {
                help.textContent =
                    "Use your browser menu and select Install DJ MIX HUB.";
                help.hidden = false;
            }

            return;
        }

        const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
        const isEdge = /edg/i.test(navigator.userAgent);
        const isChrome = /chrome|chromium/i.test(navigator.userAgent) &&
            !isEdge;

        if (isIOS) {
            help.textContent =
                "On iPhone or iPad, tap Share in Safari, then choose " +
                "Add to Home Screen.";
        } else if (isEdge) {
            help.textContent =
                "In Microsoft Edge, open the menu (⋯), choose Apps, " +
                "then Install this site as an app.";
        } else if (isChrome) {
            help.textContent =
                "In Chrome, open the three-dot menu, choose " +
                "Cast, save and share, then Install page as app.";
        } else {
            help.textContent =
                "Open your browser menu and look for Install app, " +
                "Install this site, or Add to Home Screen.";
        }

        help.hidden = false;
    });
})();
