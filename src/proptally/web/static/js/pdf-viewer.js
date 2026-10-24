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
    var fitToWidth = viewer.hasAttribute("data-pdf-fit-width");
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

        // The server stores rectangles in the visible, rotated page space,
        // measured from the top-left corner, together with the rotated page
        // size. Scale them directly: pdf.js already rendered the rotated page.
        var pageWidth = Number(location.page_width);
        var pageHeight = Number(location.page_height);
        if (!pageWidth || !pageHeight) return;
        var scaleX = viewport.width / pageWidth;
        var scaleY = viewport.height / pageHeight;
        location.rects.forEach(function (rect) {
            var x0 = rect.x0 * scaleX;
            var x1 = rect.x1 * scaleX;
            var y0 = rect.y0 * scaleY;
            var y1 = rect.y1 * scaleY;
            var node = document.createElement("div");
            node.className = "pt-pdf-highlight";
            node.style.left = Math.min(x0, x1) + "px";
            node.style.top = Math.min(y0, y1) + "px";
            node.style.width = Math.max(1, Math.abs(x1 - x0)) + "px";
            node.style.height = Math.max(1, Math.abs(y1 - y0)) + "px";
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
            if (fitToWidth && viewportEl) {
                // Fit the page to the pane, not to the browser's default 100%.
                // The viewport has its own padding; subtract it so the page edge
                // lands on the visible edge rather than under the gutter.
                var styles = window.getComputedStyle(viewportEl);
                var available = viewportEl.clientWidth -
                    parseFloat(styles.paddingLeft || 0) -
                    parseFloat(styles.paddingRight || 0);
                var natural = page.getViewport({scale: 1});
                if (available > 0 && natural.width > 0) {
                    zoom = Math.min(2.5, Math.max(0.25, available / natural.width));
                }
            }
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
            fitToWidth = false;
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

    var resizeTimer = null;
    window.addEventListener("resize", function () {
        if (!fitToWidth || !pdf) return;
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
            renderPage();
        }, 150);
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
