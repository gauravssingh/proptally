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

    function updateField(field, result) {
        var value = field.querySelector(".pt-field__value");
        if (value) value.textContent = result.display;

        var extracted = field.querySelector(".pt-field__extracted");
        if (!extracted && value && value.parentElement) {
            extracted = document.createElement("span");
            extracted.className = "pt-field__extracted pt-fs-caption pt-text-muted";
            value.parentElement.appendChild(extracted);
        }
        if (extracted) extracted.textContent = "Extracted: " + result.original_display;

        var badges = field.querySelector(".pt-field__badges");
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
                var field = form.closest(".pt-field");
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
