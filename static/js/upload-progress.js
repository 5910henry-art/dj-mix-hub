(function () {
    "use strict";

    function initUploadProgress() {
        const form = document.querySelector("form[data-upload-progress]");
        if (!form) return;

        const button = form.querySelector('button[type="submit"]');
        const panel = document.getElementById("uploadProgress");
        const bar = document.getElementById("uploadProgressBar");
        const percent = document.getElementById("uploadPercent");
        const details = document.getElementById("uploadDetails");
        const status = document.getElementById("uploadStatus");

        if (!button || !panel || !bar || !percent || !details || !status) {
            return;
        }

        let uploading = false;

        form.addEventListener("submit", function (event) {
            if (uploading) {
                event.preventDefault();
                return;
            }

            event.preventDefault();

            if (!form.reportValidity()) return;

            const audio = form.querySelector('input[name="audio_file"]');
            const cover = form.querySelector('input[name="cover_image"]');

            if (!audio || !audio.files.length) {
                form.reportValidity();
                return;
            }

            const data = new FormData(form);
            const xhr = new XMLHttpRequest();

            uploading = true;
            button.disabled = true;
            button.textContent = "Uploading…";
            button.style.opacity = "0.65";
            button.style.cursor = "not-allowed";

            panel.hidden = false;
            bar.style.width = "0%";
            percent.textContent = "0%";
            details.textContent = "Preparing your files…";
            status.textContent = "Sending files to DJ MIX HUB…";

            xhr.open("POST", form.action || window.location.href, true);
            xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");

            xhr.upload.addEventListener("progress", function (e) {
                if (!e.lengthComputable) return;

                const progress = Math.min(
                    100,
                    Math.round((e.loaded / e.total) * 100)
                );

                const mbLoaded = (e.loaded / (1024 * 1024)).toFixed(1);
                const mbTotal = (e.total / (1024 * 1024)).toFixed(1);

                bar.style.width = progress + "%";
                percent.textContent = progress + "%";
                details.textContent =
                    mbLoaded + " MB of " + mbTotal + " MB sent";

                status.textContent = progress >= 100
                    ? "Files received. Saving your mix to storage…"
                    : "Uploading your files…";
            });

            xhr.addEventListener("load", function () {
                if (xhr.status >= 200 && xhr.status < 400) {
                    if (xhr.responseURL &&
                        xhr.responseURL !== window.location.href) {
                        window.location.assign(xhr.responseURL);
                        return;
                    }

                    if (xhr.responseURL &&
                        new URL(xhr.responseURL).pathname !==
                        new URL(window.location.href).pathname) {
                        window.location.assign(xhr.responseURL);
                        return;
                    }

                    document.open();
                    document.write(xhr.responseText);
                    document.close();
                    return;
                }

                showError("Upload failed. Please try again.");
            });

            xhr.addEventListener("error", function () {
                showError("Network error. Check your connection and try again.");
            });

            xhr.addEventListener("abort", function () {
                showError("Upload was cancelled.");
            });

            xhr.send(data);

            function showError(message) {
                uploading = false;
                button.disabled = false;
                button.textContent = "Try Upload Again";
                button.style.opacity = "1";
                button.style.cursor = "pointer";
                status.textContent = message;
                status.style.color = "#ffaaaa";
            }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initUploadProgress);
    } else {
        initUploadProgress();
    }
})();
