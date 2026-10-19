(function () {
    "use strict";

    var forms = document.querySelectorAll("form[data-record-field-form]");
    if (!forms.length || !window.fetch || !window.FormData) return;

    function notify(variant, text) {
        if (window.ProptallyToast) {
            window.ProptallyToast.show({ variant: variant, text: text });
        }
    }

    function editedBadge() {
        var badge = document.createElement("span");
        badge.className = "pt-badge pt-badge--info";
        badge.textContent = "EDITED";
        return badge;
    }

    function part(field, name) {
        return field.querySelector(".pt-extracted-field__" + name)
            || field.querySelector(".pt-field__" + name);
    }

    function updateField(field, result) {
        var value = part(field, "value");
        if (value) value.textContent = result.display;

        var extracted = part(field, "extracted");
        if (!extracted && value && value.parentElement) {
            extracted = document.createElement("span");
            extracted.className = field.classList.contains("pt-extracted-field")
                ? "pt-extracted-field__extracted pt-fs-caption pt-text-muted"
                : "pt-field__extracted pt-fs-caption pt-text-muted";
            value.parentElement.appendChild(extracted);
        }
        if (extracted) extracted.textContent = "Extracted: " + result.original_display;

        var badges = part(field, "badges");
        if (badges) {
            var hasEdited = Array.prototype.some.call(
                badges.querySelectorAll(".pt-badge"),
                function (item) { return item.textContent.trim().toUpperCase() === "EDITED"; }
            );
            if (!hasEdited) badges.appendChild(editedBadge());
        }

        var details = field.querySelector("details");
        if (details) details.open = false;
    }

    Array.prototype.forEach.call(
        document.querySelectorAll("[data-record-field-cancel]"),
        function (button) {
            button.addEventListener("click", function () {
                var details = button.closest("details");
                if (details) details.open = false;
            });
        }
    );

    Array.prototype.forEach.call(
        document.querySelectorAll("details.pt-extracted-field__edit, details.pt-field__edit"),
        function (details) {
            var read = details.closest(".pt-extracted-field__read, .pt-field__read");

            function sync() {
                if (read) read.classList.toggle("is-editing", details.open);
                var action = details.open ? "Cancel editing" : "Edit value";
                var summary = details.querySelector("summary");
                if (summary) {
                    summary.setAttribute("aria-label", action);
                    summary.setAttribute("title", action);
                }
                if (!details.open) return;
                var input = details.querySelector(
                    "form input:not([type=hidden]), form select, form textarea"
                );
                if (input) {
                    window.requestAnimationFrame(function () { input.focus(); });
                }
            }

            details.addEventListener("toggle", sync);
            sync();
        }
    );

    Array.prototype.forEach.call(forms, function (form) {
        form.addEventListener("submit", function (event) {
            if (form.dataset.saving === "true") {
                event.preventDefault();
                return;
            }

            var action = event.submitter && event.submitter.name === "action"
                ? event.submitter.value : "save";
            // Conflict resolution changes the shape of the whole field, so it keeps
            // the ordinary form submit.
            if (action === "resolve") return;

            event.preventDefault();
            form.dataset.saving = "true";
            form.setAttribute("aria-busy", "true");

            var data = new FormData(form);
            data.set("action", action);

            window.fetch(form.getAttribute("action"), {
                method: "POST",
                body: data,
                credentials: "same-origin",
                headers: {
                    "Accept": "application/json",
                    "X-Requested-With": "fetch"
                }
            }).then(function (response) {
                var type = response.headers.get("content-type") || "";
                if (type.indexOf("application/json") === -1) {
                    window.location.reload();
                    return null;
                }
                return response.json().then(function (body) {
                    return { response: response, body: body };
                });
            }).then(function (result) {
                if (!result) return;
                if (!result.response.ok || !result.body.ok) {
                    notify("error", result.body.error || "That value could not be saved.");
                    return;
                }
                var field = form.closest('[id^="field-"]');
                if (field) updateField(field, result.body);
                notify("success", result.body.message);
            }).catch(function () {
                notify("error", "That value could not be saved. Check your connection and try again.");
            }).finally(function () {
                form.dataset.saving = "false";
                form.removeAttribute("aria-busy");
            });
        });
    });
})();
