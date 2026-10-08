# Archive: the full Android TV client patch (before upstream merged it)

A frozen copy of the overlay as it stood on 2026-10-08, just before kineticman merged a
version of the Android TV code sign-in into upstream `development`
(`app/scrapers/directv_device_auth.py`). **CI does not apply anything in this folder.**
Only `patches/` is applied.

- `0001-*.patch`: the full patch as it was then. It holds the Android TV client
  (`dtv_android.py`: device-code grant, refresh, DRM self-heal, channel/v2, profile
  picker, per-device app User-Agent), DAI, the ad-id capture and the inserted-ad audio
  fix, plus every textual hook into upstream's files. It applies to upstream `main` as
  of `c96f72b` (5.5.1).
- `source/`: readable copies of the overlay's own modules from that patch.
- `validate.sh`, `smoke_test.py`: the checks that went with it.

The git tag `pre-dai-only-trim` marks the same state of this repo.

To bring a piece back, copy it out of here and into the current module.
`dtv_android.list_profiles`/`select_profile` is the viewer-profile picker, and
`drm_reauth` is the single-flight DRM recovery.
