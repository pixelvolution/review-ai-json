# Pixelvolution Trending Items (WordPress plugin)

Full-catalog trending setup for dispensary sites. Every product is a real
WordPress post (`pixel_item`) with a **fully built inner page** — description,
details table, live-menu link — and the shortcode surfaces whichever items are
**currently trending**, refreshed each morning by the central Pixelvolution
service. Cards link to the item's own inner page by default, keeping visitors
on the site.

The plugin is intentionally thin: no cron inside WordPress, no POS credentials
stored on the site, no raw sales data exposed. It only responds to
authenticated refresh calls from the central Cloudflare Worker.

## Shortcode

```
[pixel_trending count="6" columns="3" link="page" show_reason="yes"]
```

| Attribute | Default | Notes |
|---|---|---|
| `count` | `6` | Max items shown |
| `columns` | `3` | Grid columns on desktop (1–6) |
| `link` | `page` | `page` → item inner page, `embed` → Dutchie/Leafly listing |
| `show_reason` | `yes` | Show the "moving fast" style badge |

`[pixel_trend_deals]` is kept as an alias.

Owner-featured items sort first, then by trend score. Items the owner has
hidden never render.

## Inner pages

The refresh pipeline writes the full page content (description, details table,
menu-link button) into each item post. A theme can override the frame with its
own `single-pixel_item.php`; otherwise the plugin's fallback template is used.

**Hand edits are respected**: the builder hashes the content it writes. If a
page has been edited since (hash mismatch), refreshes update meta/score only
and never overwrite the page.

## REST API (called by the central Worker)

### `POST /wp-json/pixel-trends/v1/refresh`

```json
{
  "replace_trending": true,
  "items": [
    {
      "sku": "KOVA-1234",
      "name": "Blue Dream 3.5g",
      "trending": true,
      "trend_score": 87.4,
      "trend_reason": "moving fast",
      "image_url": "https://images.dutchie.com/…",
      "embed_link": "https://dutchie.com/…",
      "excerpt": "Short card text",
      "description_html": "<p>Full description…</p>",
      "details": { "Brand": "…", "THC": "24%", "Category": "Flower" }
    }
  ]
}
```

- Items upsert by `sku` — existing posts are updated in place, titles that the
  owner customized are not clobbered.
- With `replace_trending: true` (default) the payload's `trending: true` items
  become the complete trending set; everything else is un-flagged.
- Images are sideloaded into the media library once per source URL and set as
  the featured image.

Response: `{ "status": "success", "created": n, "updated": n, "skipped": n, "trending": n, "pulled_at": "…" }`

### `GET /wp-json/pixel-trends/v1/status`

Returns plugin version and the last run summary for the central monitor.

### Authentication (both routes)

| Header | Value |
|---|---|
| `X-Pixel-Token` | Site token (issued at activation, shown in Settings) |
| `X-Pixel-Timestamp` | Unix timestamp, ±5 minutes |
| `X-Pixel-Signature` | hex `HMAC-SHA256("{timestamp}.{raw body}", site_secret)` |

A leaked token alone is not replayable — requests must also carry a fresh
HMAC signature produced with the signing secret.

## Admin surface

- **Trend Items → Today's Picks** — current trending list with per-item
  *hide* and *feature* toggles. That's the entire ongoing owner interaction.
- **Trend Items → Settings** — copy-ready shortcode, site token + signing
  secret (with regenerate), and the refresh endpoint URL for registration.

## Install

Upload `dist/pixel-trends.zip` via **Plugins → Add New → Upload Plugin** (or
copy the `dist/pixel-trends/` directory into `wp-content/plugins/`) and
activate. Activation generates the site token and signing secret; register
those with the central Worker and the morning refreshes take it from there.

To rebuild the zip after changing the source: run `./build.sh` from the repo
root.
