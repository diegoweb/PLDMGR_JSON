# local_icons

Hand-placed icons for payloads whose upstream repository ships no usable
artwork (no `icon0`, no logo, no banner — only screenshots or nothing at all).

`generate.py` picks these up automatically, before it scans the upstream repo,
so a payload that would otherwise fall back to the owner's GitHub avatar gets
its real branding instead.

## Naming

Any of these, in this priority order, with `.png`, `.jpg`, `.jpeg`, `.svg`,
`.webp` or `.gif` (case-insensitive):

| Pattern | Example |
|---|---|
| `<owner>-<repo>.<ext>` | `saawant12-orbit-store-ps5.svg` |
| `<repo>.<ext>` | `orbit-store-ps5.svg` |
| `<payload name>.<ext>` | `Orbit Store.svg` |

`<owner>-<repo>` wins when two payloads share a repo name.

## Rules

- Keep it square if you can. Non-square images are center-cropped, and very
  wide ones (banners) are drawn on a blurred backdrop.
- Anything over the PS5 WebKit limit of 12,288 characters per data URI is
  downscaled to 128px automatically (needs Pillow, see `requirements.txt`).
- Images under 48px are ignored for automatic discovery.
- Prefer SVG or PNG with transparency; these tiles sit on a dark background.
- To pin one of these to a specific line instead of relying on the filename,
  use `icon=local_icons/<file>` in `links.txt`, or `icon=none` to opt out.
