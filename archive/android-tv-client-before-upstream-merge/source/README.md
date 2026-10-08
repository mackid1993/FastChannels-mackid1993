# source/ — readable copies of the overlay's own modules

This repo is a patch overlay (see the top-level `README.md`); it does not vendor
upstream FastChannels. The four modules below are the overlay's *own* code — files
upstream doesn't have, which `patches/` adds in full — copied here so you can read and
review them directly without applying the patch:

- `app/scrapers/directv_dai.py` — the DAI (DirecTV ad insertion) logic.
- `app/scrapers/dtv_android.py` — the Android TV client: signs the DirecTV session in as
  DirecTV's own Android TV app via the device-code grant (and refreshes thereafter,
  re-minting the DRM activation token), instead of the web browser client; also the
  viewer-profile picker.
- `app/scrapers/dtv_aac_ads.py` — the inserted-ad audio swap (each band-limited AC-3 ad to
  its full-range HE-AAC twin).
- `app/scrapers/dtv_aac_gain.py` — the inserted-ad loudness cut (lossless HE-AAC gain).

These are **generated copies**, kept in sync by `scripts/refresh-patches.sh` on every
patch refresh. The authoritative source is `patches/` — edit there (via the applied
checkout), not here. The upstream files the patch only adds one-line hooks to are not
copied (reading them is the patch diff plus the hook table in `AGENTS.md`).
