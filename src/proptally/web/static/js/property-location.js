/*
 * Property location picker.
 *
 * Google Maps is optional. With no browser key this module does nothing and the
 * server-rendered address fields remain the complete form. With a key, it:
 *
 *   - loads the Maps JavaScript API only when the owner starts searching;
 *   - uses the Places Autocomplete Data API with an Autocomplete session token;
 *   - requests only the Essentials fields on selection;
 *   - renders the preview through the Maps Embed API (never a JS map load).
 *
 * The pure helpers are exposed on window.ProptallyLocation so they can be
 * exercised without a browser.
 */
(function () {
    "use strict";

    var mapsPromise = null;
    var placesPromise = null;

    function loadMaps(key) {
        if (window.google && window.google.maps && window.google.maps.importLibrary) {
            return Promise.resolve();
        }
        if (mapsPromise) {
            return mapsPromise;
        }
        mapsPromise = new Promise(function (resolve, reject) {
            var script = document.createElement("script");
            script.src = "https://maps.googleapis.com/maps/api/js?key=" +
                encodeURIComponent(key) +
                "&libraries=places&v=weekly&loading=async";
            script.async = true;
            script.defer = true;
            script.onload = function () { resolve(); };
            script.onerror = function () { reject(new Error("maps-load-failed")); };
            document.head.appendChild(script);
        });
        return mapsPromise;
    }

    function ensurePlaces(key) {
        return loadMaps(key).then(function () {
            if (!placesPromise) {
                placesPromise = window.google.maps.importLibrary("places");
            }
            return placesPromise;
        });
    }

    function part(components, type, shortName) {
        var found = (components || []).filter(function (item) {
            return (item.types || []).indexOf(type) !== -1;
        })[0];
        if (!found) {
            return "";
        }
        var longValue = found.longText || found.long_name || "";
        var shortValue = found.shortText || found.short_name || longValue;
        return shortName ? shortValue : longValue;
    }

    function mapAddressComponents(components, formattedAddress) {
        var street = [part(components, "street_number"), part(components, "route")]
            .filter(Boolean)
            .join(" ");
        var premise = part(components, "premise");
        var subpremise = part(components, "subpremise");
        var firstLine = formattedAddress
            ? String(formattedAddress).split(",")[0].trim()
            : "";
        var line1 = street || premise || firstLine;
        var line2 = subpremise || (premise && premise !== line1 ? premise : "");
        var city = part(components, "locality") ||
            part(components, "postal_town") ||
            part(components, "sublocality_level_1") ||
            part(components, "administrative_area_level_2");
        var state = part(components, "administrative_area_level_1", true) ||
            part(components, "administrative_area_level_1");
        return {
            address_line1: line1,
            address_line2: line2,
            city: city,
            state: state,
            zip_code: part(components, "postal_code"),
            formatted: formattedAddress || ""
        };
    }

    function normalise(value) {
        return String(value || "").trim().replace(/\s+/g, " ");
    }

    function addressChanged(previous, next) {
        var keys = ["address_line1", "address_line2", "city", "state", "zip_code"];
        return keys.some(function (key) {
            return normalise(previous[key]) !== normalise(next[key]);
        });
    }

    function embedUrl(key, placeId, latitude, longitude) {
        if (!key) {
            return "";
        }
        var query = "";
        if (placeId) {
            query = "place_id:" + encodeURIComponent(placeId);
        } else if (latitude !== "" && longitude !== "" && latitude != null && longitude != null) {
            query = encodeURIComponent(latitude) + "," + encodeURIComponent(longitude);
        } else {
            return "";
        }
        return "https://www.google.com/maps/embed/v1/place?key=" +
            encodeURIComponent(key) +
            "&q=" + query + "&zoom=16&maptype=roadmap";
    }

    function mapsLink(formattedAddress, placeId) {
        var url = "https://www.google.com/maps/search/?api=1&query=" +
            encodeURIComponent(formattedAddress || "");
        if (placeId) {
            url += "&query_place_id=" + encodeURIComponent(placeId);
        }
        return url;
    }

    function initPicker(root) {
        var key = root.getAttribute("data-maps-key") || "";
        if (!key) {
            return;
        }

        var search = root.querySelector("[data-location-search]");
        var status = root.querySelector("[data-location-status]");
        var list = root.querySelector("[data-location-suggestions]");
        var selectedBlock = root.querySelector("[data-location-selected]");
        var formatted = root.querySelector("[data-location-formatted]");
        var map = root.querySelector("[data-location-map]");
        var fallback = root.querySelector("[data-location-fallback]");
        var mapsLinkNode = root.querySelector("[data-location-maps-link]");
        var placeIdInput = root.querySelector("[data-location-place-id]");
        var latitudeInput = root.querySelector("[data-location-latitude]");
        var longitudeInput = root.querySelector("[data-location-longitude]");
        var sourceInput = root.querySelector("[data-location-source]");
        var clearFlag = root.querySelector("[data-location-clear-flag]");
        var clearButton = root.querySelector("[data-location-clear]");
        var addressInputs = root.querySelectorAll(
            'input[name="address_line1"], input[name="address_line2"], ' +
            'input[name="city"], input[name="state"], input[name="zip_code"]'
        );

        if (!search || !list || !selectedBlock) {
            return;
        }

        var sessionToken = null;
        var debounceTimer = null;
        var requestSequence = 0;
        var suggestions = [];
        var activeIndex = -1;
        var selected = Boolean(placeIdInput && placeIdInput.value);
        var populating = false;
        var iframeTimer = null;

        function setStatus(message) {
            if (status) {
                status.textContent = message || "";
            }
        }

        function closeSuggestions() {
            list.hidden = true;
            list.innerHTML = "";
            search.setAttribute("aria-expanded", "false");
            search.removeAttribute("aria-activedescendant");
            suggestions = [];
            activeIndex = -1;
        }

        function setField(name, value) {
            var input = root.querySelector('input[name="' + name + '"]');
            if (input) {
                input.value = value || "";
            }
        }

        function showFallback() {
            if (fallback) {
                fallback.hidden = false;
            }
            if (map) {
                map.hidden = true;
            }
        }

        function showMap(url) {
            if (!map) {
                return;
            }
            if (iframeTimer) {
                window.clearTimeout(iframeTimer);
            }
            map.innerHTML = "";
            if (fallback) {
                fallback.hidden = true;
            }
            map.hidden = false;
            if (!url) {
                showFallback();
                return;
            }
            if (navigator.onLine === false) {
                showFallback();
                return;
            }
            var frame = document.createElement("iframe");
            frame.src = url;
            frame.title = "Map showing the selected property location";
            frame.loading = "lazy";
            frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
            frame.setAttribute("allowfullscreen", "");
            var loaded = false;
            frame.addEventListener("load", function () {
                loaded = true;
                if (fallback) {
                    fallback.hidden = true;
                }
            });
            map.appendChild(frame);
            iframeTimer = window.setTimeout(function () {
                if (!loaded) {
                    showFallback();
                }
            }, 8000);
        }

        function clearSelection(options) {
            selected = false;
            sessionToken = null;
            if (placeIdInput) { placeIdInput.value = ""; }
            if (latitudeInput) { latitudeInput.value = ""; }
            if (longitudeInput) { longitudeInput.value = ""; }
            if (sourceInput) { sourceInput.value = "MANUAL"; }
            if (clearFlag) { clearFlag.value = "1"; }
            selectedBlock.hidden = true;
            if (map) {
                map.innerHTML = "";
            }
            if (fallback) {
                fallback.hidden = true;
            }
            if (formatted) {
                formatted.textContent = "";
            }
            if (options && options.clearAddress) {
                populating = true;
                addressInputs.forEach(function (input) { input.value = ""; });
                populating = false;
            }
            setStatus("Location cleared. The address is now manual; edit it below or search again.");
        }

        function rememberSelection(place, mapped) {
            selected = true;
            if (placeIdInput) { placeIdInput.value = place.id || ""; }
            if (sourceInput) { sourceInput.value = "GOOGLE"; }
            if (clearFlag) { clearFlag.value = ""; }
            var latitude = place.location && typeof place.location.lat === "function"
                ? place.location.lat() : null;
            var longitude = place.location && typeof place.location.lng === "function"
                ? place.location.lng() : null;
            if (latitudeInput) { latitudeInput.value = latitude == null ? "" : String(latitude); }
            if (longitudeInput) { longitudeInput.value = longitude == null ? "" : String(longitude); }

            if (formatted) {
                formatted.textContent = mapped.formatted || mapped.address_line1;
            }
            if (mapsLinkNode) {
                mapsLinkNode.href = mapsLink(mapped.formatted, place.id);
            }
            selectedBlock.hidden = false;
            showMap(embedUrl(key, place.id, latitude, longitude));
        }

        function choose(prediction) {
            closeSuggestions();
            setStatus("Loading the selected address…");
            var place = prediction.toPlace();
            place.fetchFields({
                fields: ["id", "formattedAddress", "addressComponents", "location"]
            }).then(function () {
                var mapped = mapAddressComponents(
                    place.addressComponents,
                    place.formattedAddress
                );
                populating = true;
                setField("address_line1", mapped.address_line1);
                setField("address_line2", mapped.address_line2);
                setField("city", mapped.city);
                setField("state", mapped.state);
                setField("zip_code", mapped.zip_code);
                populating = false;
                rememberSelection(place, mapped);
                setStatus(
                    "Location selected. Editing the address below clears the pin; " +
                    "search again to attach a new one."
                );
                // A session ends at fetchFields(). Start a fresh one.
                sessionToken = new window.__ptPlaces.AutocompleteSessionToken();
            }).catch(function () {
                setStatus("That location could not be loaded. Enter the address manually.");
            });
        }

        function renderSuggestions(items) {
            list.innerHTML = "";
            suggestions = items.slice(0, 5);
            activeIndex = -1;
            if (!suggestions.length) {
                list.hidden = true;
                setStatus("No matches. Enter the address manually.");
                return;
            }
            suggestions.forEach(function (item, index) {
                var prediction = item.placePrediction;
                if (!prediction) {
                    return;
                }
                var title = prediction.mainText ? prediction.mainText.toString() : "";
                var subtitle = prediction.secondaryText ? prediction.secondaryText.toString() : "";
                if (!title) {
                    title = prediction.text ? prediction.text.toString() : "";
                }
                var option = document.createElement("button");
                option.type = "button";
                option.className = "pt-location__option";
                option.setAttribute("role", "option");
                option.id = root.getAttribute("data-prefix") + "location-option-" + index;
                option.dataset.index = String(index);
                var titleNode = document.createElement("span");
                titleNode.className = "pt-location__option-title";
                titleNode.textContent = title;
                option.appendChild(titleNode);
                if (subtitle) {
                    var subtitleNode = document.createElement("span");
                    subtitleNode.className = "pt-location__option-sub";
                    subtitleNode.textContent = subtitle;
                    option.appendChild(subtitleNode);
                }
                option.addEventListener("mousedown", function (event) {
                    event.preventDefault();
                    choose(prediction);
                });
                list.appendChild(option);
            });
            list.hidden = suggestions.length === 0;
            search.setAttribute("aria-expanded", "true");
            setStatus(suggestions.length ? "Select the address that matches this property." : "");
        }

        function highlight(index) {
            var options = list.querySelectorAll(".pt-location__option");
            options.forEach(function (option) {
                option.classList.remove("is-active");
                option.setAttribute("aria-selected", "false");
            });
            if (index < 0 || index >= options.length) {
                activeIndex = -1;
                search.removeAttribute("aria-activedescendant");
                return;
            }
            activeIndex = index;
            options[index].classList.add("is-active");
            options[index].setAttribute("aria-selected", "true");
            search.setAttribute("aria-activedescendant", options[index].id);
        }

        function requestSuggestions(query) {
            var sequence = ++requestSequence;
            if (!sessionToken) {
                sessionToken = new window.__ptPlaces.AutocompleteSessionToken();
            }
            window.__ptPlaces.AutocompleteSuggestion
                .fetchAutocompleteSuggestions({
                    input: query,
                    sessionToken: sessionToken
                })
                .then(function (response) {
                    if (sequence !== requestSequence) {
                        return;
                    }
                    renderSuggestions((response && response.suggestions) || []);
                })
                .catch(function () {
                    if (sequence !== requestSequence) {
                        return;
                    }
                    closeSuggestions();
                    setStatus("Address search is unavailable. Enter the address manually.");
                });
        }

        function ensureSearchReady() {
            ensurePlaces(key).then(function (library) {
                window.__ptPlaces = library;
                if (!sessionToken) {
                    sessionToken = new library.AutocompleteSessionToken();
                }
            }).catch(function () {
                setStatus("Address search is unavailable. Enter the address manually.");
            });
        }

        search.addEventListener("focus", function () {
            ensureSearchReady();
        });

        search.addEventListener("input", function () {
            var query = search.value.trim();
            if (debounceTimer) {
                window.clearTimeout(debounceTimer);
            }
            if (query.length < 3) {
                closeSuggestions();
                sessionToken = null;
                setStatus("");
                return;
            }
            debounceTimer = window.setTimeout(function () {
                if (!window.__ptPlaces) {
                    ensurePlaces(key).then(function (library) {
                        window.__ptPlaces = library;
                        if (!sessionToken) {
                            sessionToken = new library.AutocompleteSessionToken();
                        }
                        requestSuggestions(query);
                    }).catch(function () {
                        setStatus("Address search is unavailable. Enter the address manually.");
                    });
                    return;
                }
                requestSuggestions(query);
            }, 300);
        });

        search.addEventListener("keydown", function (event) {
            if (event.key === "ArrowDown") {
                event.preventDefault();
                highlight(activeIndex + 1);
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                highlight(activeIndex - 1);
            } else if (event.key === "Enter" && activeIndex >= 0) {
                event.preventDefault();
                var item = suggestions[activeIndex];
                if (item && item.placePrediction) {
                    choose(item.placePrediction);
                }
            } else if (event.key === "Escape") {
                closeSuggestions();
            }
        });

        search.addEventListener("blur", function () {
            window.setTimeout(closeSuggestions, 150);
        });

        addressInputs.forEach(function (input) {
            input.addEventListener("input", function () {
                if (populating || !selected) {
                    return;
                }
                clearSelection();
            });
        });

        if (clearButton) {
            clearButton.addEventListener("click", function () {
                clearSelection();
                if (search) {
                    search.value = "";
                }
            });
        }

        if (selected) {
            selectedBlock.hidden = false;
            setStatus("This property already has a pinned location. Edit an address field to detach it.");
        }
    }

    function boot() {
        var roots = document.querySelectorAll("[data-location-picker]");
        Array.prototype.forEach.call(roots, initPicker);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }

    window.ProptallyLocation = {
        mapAddressComponents: mapAddressComponents,
        addressChanged: addressChanged,
        embedUrl: embedUrl,
        mapsLink: mapsLink,
        normalise: normalise
    };
})();
