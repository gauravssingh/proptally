(function () {
    "use strict";

    var viewer = document.querySelector("[data-pdf-viewer]");
    if (!viewer || !window.pdfjsLib) return;

    window.pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.js";

    var viewportEl = viewer.querySelector("[data-pdf-viewport]");
    var pageEl = viewer.querySelector("[data-pdf-page-container]");
    var canvas = viewer.querySelector("[data-pdf-canvas]");
    var highlightsEl = viewer.querySelector("[data-pdf-highlights]");
    var pageLabel = viewer.querySelector("[data-pdf-page-label]");
    var zoomLabel = viewer.querySelector("[data-pdf-zoom]");
    var statusEl = viewer.querySelector("[data-pdf-status]");
    var pdfUrl = viewer.getAttribute("data-pdf-url");
    var currentPage = Math.max(1, parseInt(viewer.getAttribute("data-pdf-page") || "1", 10));
    var zoom = 1;
    var pdf = null;
    var page = null;
    var viewport = null;
    var renderTask = null;
    var currentLocation = null;

    function setStatus(text) {
        if (statusEl) statusEl.textContent = text;
    }

    function setZoomLabel() {
        if (zoomLabel) zoomLabel.textContent = Math.round(zoom * 100) + "%";
    }

    function clearHighlights() {
        if (highlightsEl) highlightsEl.textContent = "";
    }

    function drawHighlights(location) {
        clearHighlights();
        if (!highlightsEl || !location || location.status !== "AVAILABLE" || !location.rects) return;
        if (location.page && location.page !== currentPage) return;
        if (!viewport || !page) return;

        var height = Number(location.page_height) || (page.view[3] - page.view[1]);
        location.rects.forEach(function (rect) {
            var pdfRect = [rect.x0, height - rect.y1, rect.x1, height - rect.y0];
            var box = viewport.convertToViewportRectangle(pdfRect);
            var x0 = Math.min(box[0], box[2]);
            var x1 = Math.max(box[0], box[2]);
            var y0 = Math.min(box[1], box[3]);
            var y1 = Math.max(box[1], box[3]);
            var node = document.createElement("div");
            node.className = "pt-pdf-highlight";
            node.style.left = x0 + "px";
            node.style.top = y0 + "px";
            node.style.width = Math.max(1, x1 - x0) + "px";
            node.style.height = Math.max(1, y1 - y0) + "px";
            highlightsEl.appendChild(node);
        });

        var first = highlightsEl.querySelector(".pt-pdf-highlight");
        if (first) {
            var top = first.offsetTop;
            viewportEl.scrollTo({
                top: Math.max(0, top - viewportEl.clientHeight / 2),
                behavior: "smooth"
            });
        }
    }

    function updatePageLabel() {
        if (!pageLabel) return;
        pageLabel.textContent = pdf
            ? "Page " + currentPage + " / " + pdf.numPages
            : "Page " + currentPage;
    }

    function renderPage() {
        if (!pdf || !canvas) return Promise.resolve();
        return pdf.getPage(currentPage).then(function (nextPage) {
            page = nextPage;
            viewport = page.getViewport({scale: zoom});
            var ratio = window.devicePixelRatio || 1;
            canvas.width = Math.floor(viewport.width * ratio);
            canvas.height = Math.floor(viewport.height * ratio);
            canvas.style.width = viewport.width + "px";
            canvas.style.height = viewport.height + "px";
            pageEl.style.width = viewport.width + "px";
            pageEl.style.height = viewport.height + "px";
            updatePageLabel();
            setZoomLabel();

            if (renderTask) {
                try { renderTask.cancel(); } catch (e) { /* a completed task */ }
            }
            var context = canvas.getContext("2d");
            renderTask = page.render({
                canvasContext: context,
                viewport: viewport,
                transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null
            });
            return renderTask.promise.then(function () {
                drawHighlights(currentLocation);
            });
        }).catch(function (error) {
            if (error && error.name === "RenderingCancelledException") return;
            setStatus("This PDF could not be rendered.");
        });
    }

    function focusLocation(location) {
        currentLocation = location;
        if (location.page && location.page !== currentPage) {
            currentPage = location.page;
            renderPage().then(function () {
                drawHighlights(currentLocation);
            });
        } else {
            drawHighlights(currentLocation);
        }
    }

    function selectEvidence(item, location) {
        Array.prototype.forEach.call(document.querySelectorAll("[data-evidence-id]"), function (row) {
            row.classList.remove("is-selected");
        });
        if (item) item.classList.add("is-selected");

        if (!location || location.status === "UNAVAILABLE") {
            clearHighlights();
            setStatus("Source location unavailable for this value.");
            return;
        }
        if (location.status === "AMBIGUOUS") {
            clearHighlights();
            setStatus("Multiple source locations; the value is not highlighted.");
            return;
        }
        if (location.status !== "AVAILABLE" || !location.rects || !location.rects.length) {
            clearHighlights();
            setStatus("Source location unavailable for this value.");
            return;
        }
        var count = Number(location.candidate_count) || 1;
        setStatus(count > 1
            ? "Highlighted " + count + " source occurrences."
            : "Highlighted source text.");
        focusLocation(location);
    }

    viewer.querySelectorAll("[data-pdf-prev], [data-pdf-next]").forEach(function (button) {
        button.addEventListener("click", function () {
            if (!pdf) return;
            var next = currentPage + (button.hasAttribute("data-pdf-next") ? 1 : -1);
            if (next < 1 || next > pdf.numPages) return;
            currentPage = next;
            currentLocation = null;
            clearHighlights();
            setStatus("Select an evidence value to highlight its source.");
            renderPage();
        });
    });

    viewer.querySelectorAll("[data-pdf-zoom-in], [data-pdf-zoom-out]").forEach(function (button) {
        button.addEventListener("click", function () {
            var next = zoom + (button.hasAttribute("data-pdf-zoom-in") ? 0.25 : -0.25);
            zoom = Math.min(2.5, Math.max(0.5, Math.round(next * 100) / 100));
            renderPage();
        });
    });

    document.querySelectorAll("[data-evidence-select]").forEach(function (button) {
        button.addEventListener("click", function () {
            var item = button.closest("[data-evidence-id]");
            var url = item && item.getAttribute("data-evidence-location");
            if (!url) return;
            Array.prototype.forEach.call(document.querySelectorAll("[data-evidence-id]"), function (row) {
                row.classList.remove("is-selected");
            });
            if (item) item.classList.add("is-selected");

            window.fetch(url, {
                headers: {"Accept": "application/json"},
                credentials: "same-origin"
            }).then(function (response) {
                if (!response.ok) throw new Error("location request failed");
                return response.json();
            }).then(function (location) {
                selectEvidence(item, location);
            }).catch(function () {
                selectEvidence(item, null);
            });
        });
    });

    setStatus("Loading document…");
    window.pdfjsLib.getDocument({url: pdfUrl}).promise.then(function (document) {
        pdf = document;
        if (currentPage > pdf.numPages) currentPage = pdf.numPages;
        return renderPage();
    }).then(function () {
        setStatus("Select an evidence value to highlight its source.");
    }).catch(function () {
        setStatus("This PDF could not be loaded. Use the file link below.");
    });
})();
