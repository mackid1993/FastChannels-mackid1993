"""DirecTV DAI: play the full-range HE-AAC rendition of each inserted ad.

Many of DirecTV's inserted-ad creatives are band-limited (~7 kHz, "AM radio")
in their AC-3 (Dolby Digital) encode while the *same* creative's HE-AAC encode
is full range. DirecTV ships the muffle itself — the Osprey and upstream play
the same AC-3 files — so no request flag, header or identity changes which file
is served. The only server-side lever is which *rendition* of the already-chosen
ad we hand the player.

A Yospace server-side ad-insertion session names each inserted creative
``u-<id>-c-<bitrate>-<n>-<seg|i>.mp4`` on the ad CDN
(``yospace01-directv.akamaized.net/dtv-prd/...``), where ``c`` is the AC-3
rendition and ``-i`` is that creative's own fMP4 init. The matching full-range
twin lives at the same path with ``a-96`` (HE-AAC 96 kbps) in place of
``c-<bitrate>``, and Yospace emits its own ``#EXT-X-MAP:URI`` for the ad init —
so swapping is purely a URI rewrite in the AC-3 audio media playlist: every ad
segment line and the ad's own EXT-X-MAP flip from the ``c`` twin to the ``a``
twin. Live programming segments live on ``dfwlive-*`` hosts with different
names and never match, so channel audio is untouched. The inserted ads stay
clear — Yospace's ``EXT-X-KEY:METHOD=NONE`` is left in place — so the swapped
AAC segments play as clear samples.

This is consistent with the overlay's fairness rules: it is the same creative,
DirecTV's own rendition of it, delivered and counted the same way — nothing is
skipped, suppressed or fabricated. The one trade-off is that swapped ads play
in stereo (the HE-AAC twin is 2.0); every measured AAC twin was full range, so
all inserted AC-3 ads are swapped rather than decoding each to detect the
muffle in the request path.
"""
from __future__ import annotations

import re

# An inserted Yospace ad segment or its init, in the AC-3 ('c') rendition.
# e.g. u-6600-c-384-1-0.mp4 (segment) / u-6600-c-384-1-i.mp4 (init).
_AD_AC3_FILE = re.compile(r'u-\d+-c-\d+-\d+-(?:\d+|i)\.mp4')
# The AC-3 rendition tag inside such a name; the full-range HE-AAC twin is a-96.
_AC3_RENDITION = re.compile(r'-c-\d+-')


def swap_muffled_ads(playlist: str) -> str:
    """Rewrite every inserted AC-3 ad URI in a DirecTV DAI media playlist — each
    ad segment and the ad's own ``#EXT-X-MAP`` init — to the creative's
    full-range HE-AAC twin. Returns the playlist unchanged when it contains no
    inserted AC-3 ad files (non-DAI, content-only, video, or the AAC playlist),
    so it is a safe no-op on every other stream."""
    if not playlist or not _AD_AC3_FILE.search(playlist):
        return playlist
    return _AD_AC3_FILE.sub(lambda m: _AC3_RENDITION.sub('-a-96-', m.group(0)), playlist)
