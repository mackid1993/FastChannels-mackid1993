# source/ — readable copies of the overlay's own modules

This repo is a patch overlay (see the top-level `README.md`); it does not vendor
upstream FastChannels. The modules below are the overlay's *own* code — files
upstream doesn't have, which `patches/` adds in full — copied here so you can read and
review them directly without applying the patch:

- `app/scrapers/directv_dai.py` — the DAI (DirecTV ad insertion) logic and the
  per-device advertising-id capture.
- `app/scrapers/directv_dai_device.py` — the bridge device behind a tune: its identity
  (Android ID, ad-tracking setting, model…) and the DirecTV app User-Agent built from it.
- `app/scrapers/directv_dai_install.py` — the wiring: attaches all of the above to
  upstream at runtime from the single line in `create_app`, plus the DAI settings panel.
- `app/scrapers/dtv_aac_ads.py` — the inserted-ad audio swap (each band-limited AC-3 ad to
  its full-range HE-AAC twin).
- `app/scrapers/dtv_aac_gain.py` — the inserted-ad loudness cut (lossless HE-AAC gain).

These are **generated copies**, kept in sync by `scripts/refresh-patches.sh` on every
patch refresh. The authoritative source is `patches/` — edit there (via the applied
checkout), not here. The overlay's only change to an upstream file is one line in `app/__init__.py`, inserted
by `scripts/insert_hook.py` (see `AGENTS.md`, "Where the hooks live").
