"""DirecTV: optionally declare the AC-3 audio rendition as stereo in the master.

Opt-in toggle ("Downmix DirecTV audio to stereo"), off by default. When on, the master
playlist we proxy declares DirecTV's AC-3 audio rendition ``CHANNELS="2"`` instead of
``"6"``. On Android 11 the stick's media3 sets the AC-3 decoder's channel count from the
master's CHANNELS attribute: with ``CHANNELS="6"`` the platform decoder does its own
band-limited 5.1->2.0 downmix (muffled, and a few dB quieter after the fold-down), while
``CHANNELS="2"`` makes it emit AC-3's own full-range Lo/Ro stereo downmix -- fuller and
louder. That lifts the quiet AC-3 programming toward the (hot) inserted-ad level, so a
bridge stick with a stereo-only (PCM) encoder can raise the whole feed uniformly on the
LinkPi/receiver without the ads blasting.

Only the master's AC-3 ``#EXT-X-MEDIA`` CHANNELS attribute changes -- the 384 kbps AC-3
segments, the codec, the STREAM-INF lines and the other renditions are untouched, and
media playlists pass through unchanged. Leave it off for an output that passes AC-3
through to an AV receiver (keep 5.1). Lives in its own module; directv.py/directv_proxy.py
get one-line hooks.
"""
from __future__ import annotations

import re

from .base import ConfigField

CONFIG_FIELDS = (
    ConfigField(
        'stereo_downmix', 'Downmix DirecTV audio to stereo',
        field_type='toggle', default='false',
        help_text=(
            'On = declare DirecTV audio as stereo so a bridge stick with a stereo-only '
            '(PCM) output plays a fuller, louder full-range downmix instead of the muffled, '
            'quieter in-decoder 5.1->2.0 one, which also closes the loudness gap to the '
            'inserted ads. Off (default) = keep the 5.1 declaration for AC-3 passthrough to a receiver.'
        )),
)

# The AC-3 audio rendition line in a master playlist, and its CHANNELS attribute.
_AC3_MEDIA_RE = re.compile(r'(#EXT-X-MEDIA:[^\r\n]*?TYPE=AUDIO[^\r\n]*?GROUP-ID="AC3[^"]*"[^\r\n]*)', re.IGNORECASE)
_CHANNELS_RE = re.compile(r'CHANNELS="\d+"', re.IGNORECASE)


def enabled(config: dict | None) -> bool:
    return str((config or {}).get('stereo_downmix', '')).strip().lower() in {'1', 'true', 'yes', 'on'}


def stereo_downmix_master(master: str, config: dict | None) -> str:
    """With the toggle on, rewrite the AC-3 audio rendition's CHANNELS to "2" in the master
    so the bridge player's AC-3 decoder emits AC-3's own full-range stereo downmix. A no-op
    when the toggle is off or the playlist has no AC-3 audio rendition (so it's safe on any
    master or media playlist)."""
    if not enabled(config) or 'TYPE=AUDIO' not in (master or '') or 'AC3' not in master:
        return master
    return _AC3_MEDIA_RE.sub(lambda m: _CHANNELS_RE.sub('CHANNELS="2"', m.group(1)), master)
