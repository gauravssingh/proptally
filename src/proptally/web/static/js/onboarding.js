/* Document onboarding upload: drag-and-drop, filename feedback and progress.
 *
 * The form still submits normally without this file. The script only upgrades the
 * experience: it moves the chosen file into the real input and reports upload
 * progress, then follows the server's redirect. No document content is read or
 * parsed here.
 */
(function () {
    "use strict";

    var form = document.querySelector("[data-onboarding-upload]");
    if (!form) return;

    var input = form.querySelector("[data-dropzone-input]");
    var zone = form.querySelector("[data-dropzone]");
    var filename = form.querySelector("[data-dropzone-filename]");
    var progress = form.querySelector("[data-upload-progress]");
    var bar = progress ? progress.querySelector(".pt-progress__fill, .pt-progress__bar, progress") : null;
    var percent = form.querySelector("[data-upload-percent]");
    var status = form.querySelector("[data-upload-status]");

    function formatSize(bytes) {
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return Math.ceil(bytes / 1024) + " KB";
        var megabytes = bytes / (1024 * 1024);
        // One decimal is enough to distinguish a 1.2 MB archive from a 1.9 MB one,
        // and a whole number reads better than "2.0 MB".
        var rounded = Math.round(megabytes * 10) / 10;
        return (Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)) + " MB";
    }

    function showName() {
        if (!input || !filename) return;
        var file = input.files && input.files[0];
        filename.textContent = file ? file.name + " · " + formatSize(file.size) : "";
        if (zone) zone.classList.toggle("has-file", Boolean(file));
    }

    if (input) input.addEventListener("change", showName);

    if (zone) {
        ["dragenter", "dragover"].forEach(function (name) {
            zone.addEventListener(name, function (event) {
                event.preventDefault();
                zone.classList.add("is-dragging");
            });
        });
        ["dragleave", "drop"].forEach(function (name) {
            zone.addEventListener(name, function (event) {
                event.preventDefault();
                zone.classList.remove("is-dragging");
            });
        });
        zone.addEventListener("drop", function (event) {
            var files = event.dataTransfer && event.dataTransfer.files;
            if (!files || !files.length || !input) return;
            var transfer = new DataTransfer();
            transfer.items.add(files[0]);
            input.files = transfer.files;
            showName();
        });
    }

    form.addEventListener("submit", function (event) {
        if (!window.XMLHttpRequest || !window.FormData || !input || !input.files.length) return;
        event.preventDefault();
        var data = new FormData(form);
        var xhr = new XMLHttpRequest();
        xhr.open("POST", form.action, true);
        if (progress) progress.hidden = false;
        if (status) status.textContent = "Uploading…";

        xhr.upload.addEventListener("progress", function (upload) {
            if (!upload.lengthComputable) return;
            var value = Math.round((upload.loaded / upload.total) * 100);
            if (bar) {
                if (bar.tagName === "PROGRESS") {
                    bar.value = value;
                } else {
                    bar.style.width = value + "%";
                }
            }
            if (percent) percent.textContent = value + "%";
            if (value >= 100 && status) status.textContent = "Validating archive…";
        });

        xhr.addEventListener("load", function () {
            var rejected = Boolean(xhr.responseURL && /[?&]error=/.test(xhr.responseURL));
            if (status) {
                status.textContent = rejected
                    ? "Archive not accepted. Opening the reason…"
                    : "Archive accepted. Opening processing status…";
            }
            if (xhr.responseURL) {
                window.location.href = xhr.responseURL;
            } else {
                window.location.reload();
            }
        });
        xhr.addEventListener("error", function () {
            if (status) status.textContent = "Upload failed. Check your connection and try again.";
            if (progress) progress.hidden = true;
        });
        xhr.send(data);
    });
})();
