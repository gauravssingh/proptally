(function () {
    "use strict";

    var TITLES = {
        success: "Done",
        info: "Notice",
        warning: "Attention",
        error: "Not saved"
    };

    function ensureRegion() {
        var region = document.querySelector("[data-toast-region]");
        if (region) return region;
        region = document.createElement("div");
        region.className = "pt-toast-region";
        region.setAttribute("data-toast-region", "");
        region.setAttribute("aria-live", "polite");
        region.setAttribute("aria-atomic", "false");
        document.body.appendChild(region);
        return region;
    }

    function initToast(toast) {
        if (!toast || toast.dataset.toastReady === "true") return;
        toast.dataset.toastReady = "true";

        var region = toast.parentElement;
        var timer = null;
        var close = toast.querySelector("[data-toast-close]");

        function stop() {
            if (timer !== null) {
                window.clearTimeout(timer);
                timer = null;
            }
        }

        function dismiss() {
            if (toast.classList.contains("is-leaving")) return;
            toast.classList.add("is-leaving");
            window.setTimeout(function () {
                toast.remove();
                if (region && !region.querySelector("[data-toast]")) region.remove();
            }, 180);
        }

        function schedule() {
            stop();
            var delay = toast.classList.contains("pt-toast--error") ? 10000
                : toast.classList.contains("pt-toast--warning") ? 8000 : 6000;
            timer = window.setTimeout(dismiss, delay);
        }

        if (close) close.addEventListener("click", dismiss);
        toast.addEventListener("mouseenter", stop);
        toast.addEventListener("mouseleave", schedule);
        toast.addEventListener("focusin", stop);
        toast.addEventListener("focusout", schedule);
        schedule();
    }

    function show(options) {
        options = options || {};
        var variant = Object.prototype.hasOwnProperty.call(TITLES, options.variant)
            ? options.variant : "success";
        var toast = document.createElement("div");
        toast.className = "pt-toast pt-toast--" + variant;
        toast.setAttribute("data-toast", "");
        toast.setAttribute("role", (variant === "warning" || variant === "error") ? "alert" : "status");

        var icon = document.createElement("span");
        icon.className = "pt-toast__icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = variant === "error" || variant === "warning" ? "!" : (variant === "info" ? "i" : "✓");

        var body = document.createElement("div");
        body.className = "pt-toast__body";
        var title = document.createElement("strong");
        title.className = "pt-toast__title";
        title.textContent = options.title || TITLES[variant];
        var text = document.createElement("span");
        text.className = "pt-toast__text";
        text.textContent = options.text || "";
        body.appendChild(title);
        body.appendChild(text);

        var close = document.createElement("button");
        close.type = "button";
        close.className = "pt-toast__close";
        close.setAttribute("data-toast-close", "");
        close.setAttribute("aria-label", "Dismiss notification");
        close.textContent = "×";

        toast.appendChild(icon);
        toast.appendChild(body);
        toast.appendChild(close);
        ensureRegion().appendChild(toast);
        initToast(toast);
        return toast;
    }

    window.ProptallyToast = { show: show };

    var region = document.querySelector("[data-toast-region]");
    if (region) {
        Array.prototype.forEach.call(region.querySelectorAll("[data-toast]"), initToast);
    }

    // A toast is a one-shot message. Drop it from the URL so a refresh does not
    // replay a save that already happened.
    try {
        var url = new URL(window.location.href);
        var changed = false;
        ["message", "error", "posted", "allocated", "skipped"].forEach(function (key) {
            if (url.searchParams.has(key)) {
                url.searchParams.delete(key);
                changed = true;
            }
        });
        if (changed) window.history.replaceState(window.history.state, "", url);
    } catch (e) {
        // Older browsers simply keep the query string; the toast still works.
    }
})();
