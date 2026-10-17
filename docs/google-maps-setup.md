# Google Maps setup

Property address search and the mini map are optional. With no browser key,
PropTally renders the structured address fields only, and every property can
still be created, edited and saved.

## Configuration

Set one browser key before starting the server:

```bash
export PROPTALLY_GOOGLE_MAPS_BROWSER_KEY="AIza..."
```

The key is read once in `create_app` and exposed to the page. It is public by
design and must be restricted. There is **no server key** and no server-side
Google call: autocomplete and Place Details run in the browser, and the map is
the Maps Embed API iframe.

An empty value disables the search box, the embed and the JavaScript module's
Google calls. The manual address form remains complete.

## Google Cloud setup

1. Create or select a Google Cloud project and enable billing.
2. Enable these APIs:
   - **Maps JavaScript API** (the `places` library used by the autocomplete data API)
   - **Places API (New)**
   - **Maps Embed API**
3. Create an API key and restrict it:
   - **Application restrictions:** HTTP referrers. Add `http://127.0.0.1:8141/*`,
     `http://localhost:8141/*`, and the production origin (for example
     `https://proptally.example.com/*`). Do not use an unrestricted key.
   - **API restrictions:** the three APIs above only.
4. Set quotas and billing alerts in the Cloud console. A browser key is
   necessarily visible to the browser; referrer restrictions and quotas are the
   controls that keep it from being used elsewhere.

## Data storage policy

The implementation follows Google's current terms:

- **Place ID** may be retained indefinitely. The map preview and the
  “Open in Google Maps” link use it, so a property keeps its pinned location even
  after coordinates expire.
- **Latitude and longitude** are a cache. Google permits caching Places API
  coordinates for 30 consecutive calendar days. `location_cached_at` records the
  fetch; reads older than 30 days are ignored. Run the maintenance command below
  so the rows themselves are cleared:

  ```bash
  python scripts/expire_location_cache.py --apply   # run daily from a scheduler
  ```

  The map never depends on coordinates; it uses the place ID.
- **Address fields** (`address_line1`, `address_line2`, `city`, `state`,
  `zip_code`) are the property's own, user-confirmed record. No separate
  `formatted_address` column is stored. The formatted address is shown as a
  confirmation immediately after selection. Confirm this interpretation with
  counsel before launch; Google's terms can change.

## Cost

India pricing list, effective 2024-08-01 (verify the current list before
budgeting):

| SKU | Free usage | Price after free cap | When it is used |
|---|---:|---:|---|
| Autocomplete Requests | 70,000/month | $0.85 / 1,000 | abandoned search sessions |
| Autocomplete Session Usage | unlimited | $0 | completed session |
| Place Details Essentials | 70,000/month | $1.50 / 1,000 | each selected suggestion |
| Maps Embed | unlimited | $0 | each map preview |
| Dynamic Maps | 70,000/month | $2.10 / 1,000 | **not used** |

A completed search session is free on the autocomplete side; the selection is
one Place Details Essentials request. The implementation requests only
`id`, `formattedAddress`, `addressComponents` and `location`, and never loads a
JavaScript map, so Dynamic Maps is not billed.

Example: 100,000 property setups/month is about 100,000 Place Details requests
and 200,000 autocomplete events, or roughly **$155.50/month** after the free
caps. Under 70,000 selections, the expected charge is **$0**.

## Applying the schema migration

New portfolios get the location columns when they are provisioned. Existing
portfolios need the migration applied explicitly:

```bash
python scripts/apply_tenant_migrations.py            # dry run
python scripts/apply_tenant_migrations.py --apply    # writes to live portfolio DBs
```

Do not run the `--apply` form against a working database without authorization.

## Manual verification checklist

Use a restricted development key and a test portfolio:

- [ ] Typing an address shows suggestions after 3 characters; no request is made
      on every keystroke.
- [ ] Selecting a suggestion fills street, city, state and ZIP, shows the
      formatted address and renders the map with a marker.
- [ ] Saving and reopening the property keeps the map and the
      “Open in Google Maps” link.
- [ ] Editing any address field clears the marker immediately; saving persists
      the property as a manual address with no place ID.
- [ ] “Clear location” detaches the pin without deleting the address.
- [ ] With `PROPTALLY_GOOGLE_MAPS_BROWSER_KEY` unset, the search box and map are
      absent and the property saves with a manual address.
- [ ] Blocking `maps.googleapis.com` or `www.google.com` leaves manual entry
      usable and shows the fallback message.
- [ ] A property with no place ID shows “No pinned location” and no iframe.
- [ ] The page is usable at a 390 px viewport; the map is at least 200 px high.
- [ ] A second portfolio cannot see the first portfolio's property or location.
- [ ] Cloud console usage shows the expected Autocomplete/Place Details calls and
      no Dynamic Maps loads.

## References

- Google Maps Platform Service Specific Terms (Google ID caching, Places API
  30-day coordinate cache): <https://cloud.google.com/maps-platform/terms/maps-service-terms>
- Places API usage, sessions and field masks:
  <https://developers.google.com/maps/documentation/places/web-service/usage-and-billing>
- Maps Embed API usage and billing:
  <https://developers.google.com/maps/documentation/embed/usage-and-billing>
- India pricing list:
  <https://developers.google.com/maps/billing-and-pricing/pricing-india>
