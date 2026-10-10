"""DirecTV ad insertion (DAI): the opt-in 'Use DirecTV ad insertion (DAI)' toggle.

When on, playback uses the stream DirecTV's own apps use: the Yospace
ad-insertion session (``streamURL``) instead of ``fallbackStreamUrl``, carrying
the account's own ad-targeting values (DMA, privacy consent, household/profile
ids) plus this device's own ad ids.
When off, everything behaves exactly as before.

Everything lives in this module (plus directv_dai_device for the bridge device);
directv_dai_install wires it into upstream at runtime from one line in create_app.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote

import requests

from . import directv_dai_device as device

logger = logging.getLogger(__name__)


# Upstream's objects, by role (the registry is TARGETS/USES in directv_dai_install).
def _up(role):
    from .directv_dai_install import up
    return up(role)


def _up_name(role):
    from .directv_dai_install import up_name
    return up_name(role)


def _up_owner(role):
    from .directv_dai_install import up_owner
    return up_owner(role)

_LOCATION_URL = "https://api.cld.dtvce.com/right/location/area/v2/service/location"
_BASICINFO_URL = "https://api.cld.dtvce.com/profile/information/basicinfogo/service"

# Client flags for the DAI request, matching DirecTV's own Android TV app
# (com.att.tv, the leanback build). Its shared player code sets, for Android TV:
# d=android_tv, nielsen_platform=plt,OTT, nielsen_dev_group=devgrp,STV (the
# group it also uses for the Osprey), metr=1071 (its TV device-class code), and
# from its default live params comscore_impl_type=a and yo.fr=true. Its shared
# parameters add comscore_platform (the OS name, lowercased: android) and leave out
# us_privacy when it's null. p, e, m, at, ut, attnid, _fw_nielsen_app_id and the
# other yo.* flags are the same
# across DirecTV's apps. Playback here always ends on a TV through an Android TV
# device (Fire TV, Google TV/onn sticks and the Osprey are all Android TV).
#
# d matters: without a device name the ad server recognizes, Yospace inserts no
# ads at all. Never send the desktop identity (d=desktop, plt,DSK, devgrp,DSK,
# comScore "PC"/"b"): it pulled web ad inventory with other markets' spots and
# band-limited audio.
_CLIENT_PARAMS = {
    'd': 'android_tv',
    'p': 'dfw', 'e': 'prod', 'm': 'live', 'at': 'NOW.RR', 'ut': 'bbtv',
    'attnid': 'dfw003', 'metr': '1071',
    'nielsen_platform': 'plt,OTT', 'nielsen_dev_group': 'devgrp,STV',
    'comscore_platform': 'android', 'comscore_impl_type': 'a',
    '_fw_nielsen_app_id': 'P7CFE36DB-A8D9-4801-95FE-51C29101342C',
    # yo.lpa: dropped 2026-10-03. It's an Osprey-config flag the Android TV app does NOT
    # send, so sending it as the Android client was off-profile. It was only kept because
    # an older run saw no ad fill without it, but that predated the Android-login +
    # yospace.pool=livepause + channel/v2 path we run now, so that result no longer
    # applies. Removed to match the app; verify ads still fill on the next live break (if
    # a break ever comes back empty/503, this is the first thing to restore).
    # yo.lp also dropped 2026-10-03: no DirecTV client — app or Osprey — sends it.
    'yo.fr': 'true', 'yo.av': '5',
    # The Android TV app's own live Yospace params, as captured from com.att.tv 5.0.136 on
    # an Android TV box (mitmproxy on a debuggable build, 2026-10-10): yo.sl, yo.fr, yo.av,
    # yo.d.cp, yo.vm and yo.cps. yo.cps is the app's Content Playback Spec template
    # 'b.lp.d.s.<min>-3630.0x.s.n'; generateURIWithDynamicCPSFlag fills <min> with
    # minutesToSeconds(3) = 180 (its default), which the capture confirms. (An older note
    # blamed yo.cps for muffled ad audio; that was DirecTV's per-ad AC-3 encode, fixed on the
    # relay side by serving those ads' AAC rendition.) yo.vm is the ad-macro map, base64 JSON
    # with APPBUNDLE com.att.tv, sent verbatim: the ${...} inside are Yospace server-side
    # macros (the app does not substitute them either). Both drift only on a DirecTV app
    # update: re-extract YSLiveParams / the CPS template from the new index.android.bundle.
    # yo.cps is NOT sent (A/B, David 2026-10-10): with it, CNN breaks lost every car-dealer and
    # political ad (the local pool) within the first hour; restore only if a no-cps hour shows
    # the local ads didn't come back either.
    'yo.sl': '3', 'yo.d.cp': 'true',
    'yo.vm': 'WwogIHsKICAgICJERVNJUkVEX0RVUkFUSU9OX1NFQ1MiOiAiJHtERVNJUkVEX0RVUkFUSU9OX1NFQ1N9IiwKICAgICJNRVRBREFUQV9DQUlEIjogIiR7TUVUQURBVEEuQURWRVJUSVNJTkdfSUR9IiwKICAgICJNRVRBREFUQV9CUkVBS0lEIjogIjAiLAogICAgIkFQUEJVTkRMRSI6ICJjb20uYXR0LnR2IiwKICAgICJJTlZFTlRPUllTVEFURSI6ICJhdXRvcGxheWVkIgogIH0KXQ==',
}

# The toggle, shown by our own settings panel (directv_dai_install) and stored as
# config['use_dai']; it is not in upstream's config_schema, so upstream's settings save
# leaves it alone.
CONFIG_FIELD = {
    'key': 'use_dai', 'label': 'Use DirecTV ad insertion (DAI)', 'default': 'false',
    'help_text': (
        'On = play the stream DirecTV\'s own apps use, with DirecTV\'s local '
        'ads in commercial breaks. Off = the national feed without DirecTV\'s '
        'inserted ads. Requires the AH4C bridge (an Android device) — it '
        'does not work with Prismcast, which has no Android device to read. '
        'Turning this on captures each bridge device\'s advertising ID: a Fire '
        'TV is read silently; a Google TV/Android TV device briefly shows its Ads '
        'settings screen for a few seconds while it\'s read, and is skipped if '
        'it\'s playing. The ID is saved and reused. When you add a new device '
        'later, a "Capture advertising IDs" button appears to pull its ID.'
    ),
}


def enabled(config: dict | None) -> bool:
    return str((config or {}).get('use_dai', '')).strip().lower() in {'1', 'true', 'yes', 'on'}


_DAI_ON = [0.0, False]   # [valid until, value]
_DAI_ON_TTL = 15


def dai_on() -> bool:
    """Whether the DirecTV source has DAI on, for code with no scraper config at hand
    (the relay's User-Agent). Cached briefly; a toggle save resets it."""
    now = time.time()
    if now < _DAI_ON[0]:
        return _DAI_ON[1]
    try:
        Source = _up('source_model')
        with _up('db').session.no_autoflush:
            on = any(enabled(src.config) for src in Source.query.filter_by(name='directv').all())
    except Exception:
        on = _DAI_ON[1]
    _DAI_ON[:] = [now + _DAI_ON_TTL, on]
    return on


# ── Stream URL ───────────────────────────────────────────────────────────────

def _v2_playback(pb: dict) -> dict:
    groups = {g.get('groupName'): g.get('URLs') or [] for g in pb.get('streamUrls') or [] if isinstance(g, dict)}
    return {'streamURL': next(iter(groups.get('DAI') or []), None),
            'fallbackStreamUrl': next(iter(groups.get('Data Center') or []), None)}


def pick_stream_url(playback_data: dict, dai: dict | None) -> str | None:
    """The stream URL to play from a channel/v1 or channel/v2 ``playbackData``
    object. ``dai`` is the DAI flag dict from request_flags(), or None with DAI off."""
    pb = playback_data or {}
    if 'streamUrls' in pb:
        pb = _v2_playback(pb)
    if dai:
        url = pb.get('streamURL') or pb.get('fallbackStreamUrl')
    else:
        url = pb.get('fallbackStreamUrl') or pb.get('streamURL')
    # streamURL is a Yospace ad-insertion session URL. Yospace answers it with
    # 503 "Content not found" unless it names a session pool; livepause is the
    # one DirecTV's own apps use.
    if url and 'yospace.com' in url and 'yospace.pool=' not in url:
        url += ('&' if '?' in url else '?') + 'yospace.pool=livepause'
    # Add the account's DAI targeting values (DMA/consent, our device ids, per-channel
    # net) so ad insertion targets the right market
    # and honors the account's real ad-personalization choice. Only values we truly
    # have are sent; nothing is invented.
    if url and dai and 'yospace.com' in url:
        # Match on exact param keys, not a substring (a naive `'e=' in query`
        # would false-positive against the tail of cdncpDevice=/cdncpCtime= and
        # silently drop e=prod).
        query = url.split('?', 1)[1] if '?' in url else ''
        existing_keys = {pair.split('=', 1)[0] for pair in query.split('&') if pair}
        for k, v in dai.items():
            if v in (None, '') or k in existing_keys:
                continue
            # Leave ':' and ',' literal, as DirecTV's clients send them
            # (_fw_did=android_id:<id>, devgrp,STV). Percent-encoded, the ad
            # server doesn't recognize the device id and serves untargeted spots.
            url += f'&{quote(str(k), safe="")}={quote(str(v), safe=",:~")}'
            existing_keys.add(k)
        logger.info('[directv-dai] DAI session active: Yospace ad-insertion streamURL with the account + device targeting flags')
    return url


def cached_url_usable(cached: dict | None, dai: bool) -> bool:
    """A URL cached under the other DAI setting (or a bare Yospace URL cached
    before the pool fix) must not be served after the toggle changes. With DAI
    on, a cached ad session also belongs to one playback device (its device id
    is in the URL), so another device gets its own."""
    if not cached:
        return False
    url = cached.get('fallback_url') or ''
    if bool(cached.get('dai')) != dai or ('yospace.com' in url and 'yospace.pool=' not in url):
        return False
    if dai and 'yospace.com' in url:
        fw_did = _current_device_ad_flags().get('_fw_did')
        if fw_did:
            return f'_fw_did={quote(fw_did, safe=",:~")}' in url
        # Not a bridge device (e.g. Channels playing the URL itself): reuse only a session
        # not pinned to a specific device. Reject a cached android_id, a real advertising
        # id, or a device's own optout form; only the shared google_advertising_id:optout
        # (what a non-bridge requester produces itself from the account's consent) is fine.
        return ('_fw_did=android_id:' not in url
                and not re.search(r'_fw_did=google_advertising_id:(?!optout)', url)
                and '_fw_did=amazon_advertising_id:' not in url)
    return True


def request_flags(config: dict, channel_names: dict | None, ccid: str, zip_lookup: bool = True,
                  report: bool = True) -> dict | None:
    """The DAI flags for one tune, or None when DAI is off. ``zip_lookup=False`` for a
    request whose stream is never played (the license path's play-token fallback): it
    uses a stored ZIP but never waits on a lookup."""
    if not enabled(config):
        return None
    return build_query(config, channel_names or {}, ccid, zip_lookup=zip_lookup, report=report)


def build_query(config: dict, dai_channel_names: dict, ccid: str, zip_lookup: bool = True,
                report: bool = True) -> dict:
    """The DAI request flags: the general client flags (never a desktop device
    identity; see _CLIENT_PARAMS), the account's own household/profile/DMA, its
    real ad-personalization choice (is_lat derived from the account's GPP consent
    string), our device's own ad ids, and the per-channel net tag. Only values
    actually sourced from the account (or minted for our own device) are included."""
    q = dict(_CLIENT_PARAMS)
    pid = config.get('dai_partner_profile_id')
    if pid:
        q['hhid'] = pid
        q['u'] = pid
    # profid is the chosen viewer profile's partnerProfileID1, from the profile picker's
    # profiletoken exchange (select_profile -> dtv_android_profid; the key name predates the
    # move into this module). Nothing else is that value (a grant's or token's profileId is
    # the profile-manager id), so without a picked profile profid is omitted (fairness rule).
    profid = config.get('dtv_android_profid')
    if profid:
        q['profid'] = profid
    if report:
        _profid_check(config, bool(profid))
    if config.get('dai_dma_id'):
        q['dma_location'] = config['dai_dma_id']
        q['dma_billing'] = config['dai_dma_id']
    if config.get('dai_gpp'):
        q['gpp'] = config['dai_gpp']
    if config.get('dai_gpp_sid'):
        q['gpp_sid'] = config['dai_gpp_sid']
    zip_code = config.get('dai_zip') or (_account_zip(config) if zip_lookup else _known_zip(config))
    if zip_code:
        q['bZipCode'] = zip_code

    # The ad id, is_lat and comscore_device come from the playback device, the way
    # DirecTV's Android TV app sets them (see device_ad_flags), using the device's
    # captured real advertising id. Without is_lat=0 the ad server sends national spots
    # only (no local or political ads). This only reads the captured id; capture itself
    # is explicit (DAI toggle-on or the admin button), never triggered by a tune.
    device = _current_device_ad_flags()
    if device:
        q.update(device)
    else:
        # Not a bridge device (e.g. Channels playing the URL itself): fall back to
        # the account's consent, decoded from its US-National GPP section only.
        if str(config.get('dai_gpp_sid')) == '7' and config.get('dai_gpp'):
            opt_out = gpp_targeted_ad_opt_out(config['dai_gpp'])
            if opt_out is not None:
                q['is_lat'] = '1' if opt_out else '0'
                if opt_out:
                    q['_fw_did'] = 'google_advertising_id:optout'
                    q['adid'] = 'optout'
    net = (dai_channel_names or {}).get(ccid)
    if net:
        q['net'] = net
    return q


# The Fire TV build of the same app (com.att.tv for Fire OS, decompiled 2026-10-10) sends its
# own platform identity: yospaceParameters' Fire TV branch sets d=firetv, nielsen_platform
# plt,OTT, nielsen_dev_group DEVGRP_FIRETV (devgrp,STB) and the id via
# makeFWAdvertisingIdFn(ADID_PREFIX_FIRETV) (amazon_advertising_id:); its YSLiveParams yo.vm
# is the same ad-macro map with APPBUNDLE B01J62Q632 (its Amazon Appstore id). The player
# runs on exactly two platforms, so each box sends its own (David, 2026-10-10): a Fire TV
# box this, an Android TV box _CLIENT_PARAMS' android_tv values. Decided from the box's
# saved adb properties (no adb call at tune time).
_FIRE_TV_PARAMS = {
    'd': 'firetv', 'nielsen_platform': 'plt,OTT', 'nielsen_dev_group': 'devgrp,STB',
    'yo.vm': 'WwogIHsKICAgICJERVNJUkVEX0RVUkFUSU9OX1NFQ1MiOiAiJHtERVNJUkVEX0RVUkFUSU9OX1NFQ1N9IiwKICAgICJNRVRBREFUQV9DQUlEIjogIiR7TUVUQURBVEEuQURWRVJUSVNJTkdfSUR9IiwKICAgICJNRVRBREFUQV9CUkVBS0lEIjogIjAiLAogICAgIkFQUEJVTkRMRSI6ICJCMDFKNjJRNjMyIiwKICAgICJJTlZFTlRPUllTVEFURSI6ICJhdXRvcGxheWVkIgogIH0KXQo=',
}


def is_fire_tv(props: dict, ad_id: dict | None = None) -> bool:
    """A Fire OS box: Amazon-made, or its advertising id was read from Fire OS settings."""
    return (str((props or {}).get('manufacturer', '')).lower() == 'amazon'
            or (ad_id or {}).get('source') == 'fire')


def device_ad_flags(props: dict, ad_id: dict | None = None) -> dict:
    """The device ad flags the DirecTV Android TV app sends, built from this bridge
    device's own values. Priority, matching the app:
    - The device's real platform advertising id, when one was captured for it
      (``ad_id`` from capture_registered_devices): amazon_advertising_id:<id> for a
      Fire OS id, google_advertising_id:<id> for a Play Services id, + adid=<id> with is_lat=0 — exactly what the app sends. is_lat=0 is what
      gets local and political ads.
    - Its opted-out form when that advertising id was deleted/limited (``ad_id``
      optout, or Fire OS limit_ad_tracking=1): <prefix>optout,
      adid=optout, is_lat=1.
    - Absolute last resort, when no advertising id can be read: _fw_did=android_id:
      <Android ID> with is_lat=0. DirecTV's own prefix for an Android device id,
      which every Android device exposes over adb. We never invent a value.
    comscore_device = Android_<Build.MANUFACTURER>_<Build.MODEL>, whitespace removed,
    as the app builds it (universalYospaceParameters), of the bridge device itself (its
    build properties, read over adb). The ad session describes the box actually playing;
    upstream's fixed sign-in identity stays on sign-in, refresh and DRM, which never see
    these values (David, 2026-10-10: the box's own identity is what brought hyper-local
    ads; the Chromecast name on every box did not)."""
    if not props:
        return {}
    flags = {'comscore_device': re.sub(r'\s+', '', f"Android_{props.get('manufacturer', '')}_{props.get('model', '')}")}
    gaid = ((ad_id or {}).get('advertising_id') or '').strip()
    # An all-zero id is Android's "deleted / limited" sentinel, never a real id: treat
    # it as opt-out, and never send it as an advertising id.
    optout = bool((ad_id or {}).get('optout')) or gaid == _ZERO_GAID
    if gaid == _ZERO_GAID:
        gaid = ''
    # The id's own kind, from where it was captured: a Fire OS id (Settings.Secure) is an
    # Amazon advertising id, which DirecTV's Fire TV build sends as amazon_advertising_id:
    # (makeFWAdvertisingIdFn(ADID_PREFIX_FIRETV)); a Google Play Services id keeps
    # google_advertising_id:. Labelling an Amazon id as Google's hid it from the identity
    # match (David, 2026-10-10).
    fire = is_fire_tv(props, ad_id)
    if fire:
        flags.update(_FIRE_TV_PARAMS)
    prefix = 'amazon_advertising_id:' if fire else 'google_advertising_id:'
    if gaid and not optout:
        flags.update({'is_lat': '0', '_fw_did': f'{prefix}{gaid}', 'adid': gaid})
    elif optout or props.get('limit_ad_tracking') == '1':
        flags.update({'is_lat': '1', '_fw_did': f'{prefix}optout', 'adid': 'optout'})
    elif len(props.get('android_id', '')) >= 8:
        flags.update({'is_lat': '0', '_fw_did': f"android_id:{props['android_id']}"})
    return flags


# ── Device advertising-id capture (DAI) ────────────────────────────────────────
#
# The Android TV app sends the playback device's real platform advertising id
# (google_advertising_id:<id> + adid) with is_lat from the device's own ad-tracking
# choice. We match that per bridge device, read the way each platform exposes it:
#   - Fire OS mirrors it into Settings.Secure, so `settings get secure advertising_id`
#     reads it silently (most bridge sticks are Fire TV).
#   - GMS (Google TV / plain Android with Play Services) locks the id to an app
#     process: there is no settings mirror, and a shell cannot bind the GMS service
#     (ActivityManager refuses a non-app caller). The only root-free read is the
#     device's own Ads settings screen — open it, read the id off the view
#     hierarchy (uiautomator), restore. That briefly shows the screen on the box
#     (~3s), so it is done ONCE.
#   - Anything else: no advertising id is read, and device_ad_flags keeps android_id.
#
# Captured per device and reused from directv_dai_adids.json; NEVER re-read on a schedule
# (that would flash every Google TV box) and NEVER at a tune (that would interrupt the
# tuning box). Capture runs only from an explicit action: turning DAI on in the DAI panel
# (settings_changed -> clear_cache_if_toggled captures the uncaptured boxes) and the admin
# "Capture advertising IDs" button; a box that is playing is skipped. To force a
# fresh capture of all boxes, toggle DAI off and on.

_ZERO_GAID = '00000000-0000-0000-0000-000000000000'
_GAID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)
_ADS_ACTION = 'com.google.android.gms.settings.ADS_PRIVACY'

# Robust on-device GMS capture: wake, leave the screensaver, open the Ads screen, then
# read the id off a FRESH uiautomator dump each pass (a stale /sdcard/ui.xml is removed
# first, so a dump that silently fails can never be read as the screen). "active" is
# decided by a real, non-zero advertising id being present (locale-independent); "deleted"
# by the Ads screen showing with no real id. Restores HOME. Prints STATE and GAID. The
# flash stays ~3s because it breaks the instant the screen is readable.
_GMS_CAPTURE_SH = r'''
ZERO=00000000-0000-0000-0000-000000000000
UUID='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
input keyevent KEYCODE_WAKEUP
input keyevent KEYCODE_HOME
am start -a com.google.android.gms.settings.ADS_PRIVACY >/dev/null 2>&1
got=""; state=""; j=0
while [ $j -lt 12 ]; do
  sleep 0.3
  rm -f /sdcard/ui.xml 2>/dev/null
  uiautomator dump /sdcard/ui.xml >/dev/null 2>&1
  [ -f /sdcard/ui.xml ] || { j=$((j+1)); continue; }
  got=$(grep -oE "$UUID" /sdcard/ui.xml 2>/dev/null | head -1)
  if [ -n "$got" ] && [ "$got" != "$ZERO" ]; then
    state=active; break
  fi
  if grep -q "advertising ID" /sdcard/ui.xml 2>/dev/null; then
    state=deleted; got=""; break
  fi
  got=""
  j=$((j+1))
done
input keyevent KEYCODE_HOME
echo "STATE=$state"
echo "GAID=$got"
'''

_adid_lock = threading.Lock()
_adid_inflight: set[str] = set()
# Addresses last tried with no readable id (adb unreachable, or a non-English deleted
# screen): kept out of "uncaptured" for a while so the capture button doesn't nag
# forever, then retried. In memory, so a restart retries once.
_adid_tried: dict[str, float] = {}
_ADID_TRIED_TTL = 6 * 3600


def _adids_file() -> str:
    """directv_dai_adids.json, beside the device store. Kept separate from
    the device-identity store (directv_dai_device) so that store's hourly re-read can
    never clobber a captured id."""
    return os.path.join(os.path.dirname(device._devices_file()), 'directv_dai_adids.json')


def _saved_adids() -> dict:
    try:
        with open(_adids_file(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_adids(adids: dict) -> None:
    path = _adids_file()
    # Unique temp name per process+thread so two concurrent captures can't interleave
    # into the same temp file before the atomic os.replace.
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(adids, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug('[directv-dai] could not save advertising ids: %s', exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _flock_polled(f, timeout: float = 5.0) -> bool:
    """Exclusive flock, polled with a (gevent-friendly) sleep instead of a blocking call."""
    try:
        import fcntl
    except Exception:
        return False
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


@contextlib.contextmanager
def _adids_file_lock():
    """Exclusive cross-process lock for the store's read-modify-write. The app runs several
    gunicorn workers, so the thread lock alone can't stop two workers interleaving a
    read-modify-write and losing an entry. Best-effort: if the OS lock can't be taken, the
    thread lock still serializes writes within a worker."""
    f = None
    try:
        f = open(_adids_file() + '.lock', 'w')
        _flock_polled(f)
        yield
    finally:
        if f is not None:
            try:
                f.close()   # closing the fd releases the flock
            except Exception:
                pass


def _store_adid(address: str, entry: dict) -> bool:
    """Merge one device's captured id into the store (read-modify-write), so a capture adds
    to the file and never overwrites the other devices' entries. Returns True if it actually
    changed, ignoring the capture timestamp. Guarded by the thread lock AND a cross-process
    file lock, because the app runs multiple gunicorn workers: without the file lock two
    workers capturing at once could interleave their read-modify-writes and lose an entry."""
    keys = ('advertising_id', 'optout', 'source')
    with _adid_lock, _adids_file_lock():
        adids = _saved_adids()
        prev = adids.get(address)
        changed = not prev or any(prev.get(k) != entry.get(k) for k in keys)
        adids[address] = entry
        _save_adids(adids)
    return changed


def mark_box_replaced(address: str) -> None:
    """A different box now answers at ``address`` (new Android ID). Its saved advertising
    id is the old box's: replace it with a marker, so no session sends it (the new box goes
    out with its own android_id until captured), the capture button shows it as pending, and
    the panel says the box changed. The next capture of that box clears the marker."""
    with _adid_lock, _adids_file_lock():
        adids = _saved_adids()
        prev = adids.get(address) or {}
        if prev.get('replaced_at') and not prev.get('source'):
            return
        adids[address] = {'replaced_at': int(time.time())}
        _save_adids(adids)
    logger.warning('[directv-dai] %s is a different box now; press "Capture advertising IDs" '
                   'for it (its old advertising id is no longer sent)', address)


def replaced_boxes() -> list[str]:
    """Addresses flagged by mark_box_replaced and not captured since."""
    return sorted(a for a, e in _saved_adids().items()
                  if isinstance(e, dict) and e.get('replaced_at') and not e.get('source'))


def _adb_shell(address: str, shell_cmd: str, timeout: int = 20) -> str:
    """Run one adb shell command on a bridge device; '' on any failure."""
    try:
        subprocess.run(['adb', 'connect', address], capture_output=True, timeout=8)
        r = subprocess.run(['adb', '-s', address, 'shell', shell_cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout or ''
    except Exception as exc:
        logger.debug('[directv-dai] adb command failed for %s: %s', address, exc)
        return ''


def _reachable(address: str) -> bool:
    """A fast adb reachability check (short timeouts) so a powered-off box fails quickly, and
    so the capture result can tell 'couldn't reach the box' apart from 'reachable but no
    advertising id'."""
    try:
        subprocess.run(['adb', 'connect', address], capture_output=True, timeout=5)
        r = subprocess.run(['adb', '-s', address, 'shell', 'echo ok'],
                           capture_output=True, text=True, timeout=5)
        return 'ok' in (r.stdout or '')
    except Exception:
        return False


# Sentinel: the GMS screen read was skipped because the box is playing (defer, don't
# interrupt). Distinct from None (no advertising id) so callers can report it.
_PLAYING = object()


def _is_playing(address: str) -> bool:
    """True when the box is actively playing audio/video, so the GMS screen capture must
    not run (it would interrupt playback). A box asleep or on its screensaver is not
    playing. Signals: a media session in the PLAYING state, or an actively started media
    audio player. Fail-safe is to NOT capture: on any doubt here, treat as playing."""
    out = _adb_shell(address, 'dumpsys power 2>/dev/null | grep mWakefulness; echo ---; '
                              'dumpsys media_session 2>/dev/null; echo ---; '
                              'dumpsys audio 2>/dev/null', timeout=12)
    if not out:
        return True  # couldn't tell -> don't risk interrupting
    power, _, rest = out.partition('---')
    if 'Dreaming' in power or 'Asleep' in power or 'Dozing' in power:
        return False  # screensaver / asleep: safe to capture
    media_session, _, audio = rest.partition('---')
    if 'state=PlaybackState {state=3' in media_session:  # PlaybackState.STATE_PLAYING
        return True
    # Any actively started audio player means something is playing. On a bridge box that
    # only ever runs the stream, treat ANY started player (not just USAGE_MEDIA) as playing,
    # so a muted or oddly-tagged stream can't slip through and get its screen interrupted.
    if 'state:started' in audio:
        return True
    return False


def _read_fire_advertising_id(address: str) -> dict | None:
    """Fire OS (and any build that mirrors it): a silent Settings.Secure read."""
    out = _adb_shell(address, 'echo A=$(settings get secure advertising_id); '
                              'echo L=$(settings get secure limit_ad_tracking)')
    aid = lat = ''
    for ln in out.splitlines():
        if ln.startswith('A='):
            aid = ln[2:].strip().lower()
        elif ln.startswith('L='):
            lat = ln[2:].strip()
    if not (_GAID_RE.fullmatch(aid) or aid == _ZERO_GAID):
        return None
    optout = aid == _ZERO_GAID or lat == '1'
    return {'advertising_id': '' if optout else aid, 'optout': optout, 'source': 'fire'}


def _read_gms_advertising_id(address: str):
    """GMS (Google TV / plain Android with Play Services): read the id off the Ads settings
    screen. Only attempted when that screen exists on the box. It briefly shows the screen
    (~3s) and interrupts whatever the box is displaying, so it runs only from an explicit
    action (DAI toggle-on or the admin capture button), never at tune time, and is GATED:
    if the box is currently playing, it is skipped (returns the _PLAYING sentinel) so an
    active stream is never interrupted. Returns a reading dict, None (no id), or _PLAYING."""
    probe = _adb_shell(address, f'cmd package query-activities -a {_ADS_ACTION} 2>/dev/null')
    if 'AdsSettingsActivity' not in probe:
        return None
    if _is_playing(address):
        logger.info('[directv-dai] skipped advertising-id capture for %s: box is playing', address)
        return _PLAYING
    b64 = base64.b64encode(_GMS_CAPTURE_SH.encode()).decode()
    out = _adb_shell(address, f'echo {b64} | base64 -d > /data/local/tmp/dai_cap.sh; '
                              'sh /data/local/tmp/dai_cap.sh; rm -f /data/local/tmp/dai_cap.sh',
                     timeout=45)
    state = gaid = ''
    for ln in out.splitlines():
        if ln.startswith('STATE='):
            state = ln[6:].strip()
        elif ln.startswith('GAID='):
            gaid = ln[5:].strip().lower()
    if state == 'active' and _GAID_RE.fullmatch(gaid) and gaid != _ZERO_GAID:
        return {'advertising_id': gaid, 'optout': False, 'source': 'gms'}
    if state == 'deleted':
        return {'advertising_id': '', 'optout': True, 'source': 'gms'}
    return None


def _capture_one(address: str):
    """This bridge device's real advertising id + opt-out, trying each platform's own
    channel in turn. Returns a reading dict, None (no id -> keep android_id), or the
    _PLAYING sentinel (GMS read skipped because the box is in use). The silent Fire read
    is never gated; only the GMS screen read is."""
    return _read_fire_advertising_id(address) or _read_gms_advertising_id(address)


def _capture_and_store(address: str) -> str:
    """Capture one device and persist it, under the single-flight guard so the same box is
    never captured twice at once (a batch and a button press can't double-run it). Returns
    'captured', 'playing' (skipped, box in use), or 'none' (no advertising id readable)."""
    if not address:
        return 'none'
    with _adid_lock:
        if address in _adid_inflight:
            return 'none'
        _adid_inflight.add(address)
    rdb, lock_key = _redis(), f'directv_dai:capture:{address}'
    lock_value = f'{os.getpid()}:{time.time_ns()}'
    try:
        busy = rdb is not None and not rdb.set(lock_key, lock_value, nx=True, ex=180)
    except Exception:
        busy, rdb = False, None
    if busy:
        # Another worker is capturing this box right now: clear OUR mark (so a later run
        # here isn't blocked) and leave its lock alone.
        with _adid_lock:
            _adid_inflight.discard(address)
        return 'busy'
    try:
        if not _reachable(address):
            return 'unreachable'   # box off / adb down; kept pending so a re-run picks it up
        res = _capture_one(address)
        if res is _PLAYING:
            return 'playing'   # still pending: keep it, re-run when idle
        if res is None:
            with _adid_lock:
                _adid_tried[address] = time.time()   # reachable but nothing readable: stop nagging a while
            return 'none'
        entry = {'advertising_id': res['advertising_id'], 'optout': res['optout'],
                 'source': res['source'], 'captured_at': int(time.time())}
        if _store_adid(address, entry):
            logger.info('[directv-dai] captured advertising id for %s (%s, %s)',
                        address, res['source'], 'optout' if res['optout'] else 'present')
        with _adid_lock:
            _adid_tried.pop(address, None)
        return 'captured'
    finally:
        with _adid_lock:
            _adid_inflight.discard(address)
        if rdb is not None:
            try:
                _release_lock(rdb, lock_key, lock_value)   # only if still ours
            except Exception:
                pass


def _bridge_addresses(fresh: bool = True) -> list[str]:
    """Bridge device addresses: a fresh read for an explicit action (capture), the cached
    list (no ah4c call) for display."""
    try:
        devices = device.known_bridge_devices() if fresh else device._cached_bridge_devices()
        return [d['address'] for d in devices]
    except Exception as exc:
        logger.debug('[directv-dai] could not list bridge devices: %s', exc)
        return []


def _bust_playback_cache() -> None:
    """Drop the DirecTV source's cached Yospace stream URLs so the next tune refetches with
    the just-captured advertising id (a cached URL carries whatever id was current when it
    was built). cached_url_usable already refetches on an id mismatch; this makes it
    explicit so there's no window where a stale URL is served after a capture."""
    try:
        persist_source_cache_updates = _up('persist_cache')
        Source = _up('source_model')
        src = Source.query.filter_by(name='directv').first()
        if src:
            persist_source_cache_updates(src.id, {'directv_playback': {}, 'dai_playback_by_device': {}})
    except Exception as exc:
        logger.debug('[directv-dai] could not clear playback cache after capture: %s', exc)


def capture_registered_devices(addresses: list[str] | None = None) -> dict:
    """Capture + persist the advertising id for every registered bridge device (or the
    given ones), serially. Returns counts {'captured', 'playing', 'none'}. Runs ONLY from
    an explicit action (DAI toggle-on, or the admin capture button) — never on a schedule
    and never at tune time. Must run inside a Flask app context (device list + store)."""
    addrs = addresses if addresses is not None else _bridge_addresses()
    counts = {'captured': 0, 'playing': 0, 'unreachable': 0, 'none': 0, 'busy': 0}
    for addr in addrs:
        counts[_capture_and_store(addr)] += 1
    if counts['captured']:
        # A new advertising id was stored; clear cached stream URLs so the next tune uses it.
        _bust_playback_cache()
    return counts


# capture_in_background(UNCAPTURED): the devices with no id yet, worked out in the
# background from a FRESH device list (so a box added a moment ago is included and the
# save request never waits on ah4c).
UNCAPTURED = object()


def capture_in_background(addresses: list[str] | None = None) -> int:
    """Run capture_registered_devices in a daemon thread with the current Flask app
    context (the device list and the store need one). Returns how many devices it will
    visit (-1 for UNCAPTURED, which is resolved in the background)."""
    if addresses is UNCAPTURED:
        device.spawn(_capture_uncaptured)
        return -1
    if addresses is None:
        addresses = _bridge_addresses()
    addresses = list(addresses or [])
    if addresses:
        device.spawn(capture_registered_devices, addresses)
    return len(addresses)


def _capture_uncaptured() -> dict:
    """Background: capture every device with no advertising id yet, from a fresh list."""
    return capture_registered_devices(uncaptured_addresses(fresh=True))


def uncaptured_addresses(fresh: bool = False) -> list[str]:
    """Registered bridge devices with no captured advertising id yet (new boxes), used to
    show the admin capture button only when there is something to capture. Excludes a box
    we recently tried and couldn't read (unreachable / non-English deleted) so the button
    doesn't nag forever; it reappears after _ADID_TRIED_TTL. Needs an app context.
    ``fresh`` reads the device list now (for a capture); otherwise the cached list (for
    display, no ah4c call)."""
    saved = _saved_adids()
    now = time.time()
    with _adid_lock:
        recent = {a for a, t in _adid_tried.items() if now - t < _ADID_TRIED_TTL}
    captured = {a for a, e in saved.items() if isinstance(e, dict) and e.get('source')}
    return [a for a in _bridge_addresses(fresh=fresh) if a and a not in captured and a not in recent]


def _current_bridge_address() -> str | None:
    try:
        from flask import has_request_context, request
        ip = (request.remote_addr or '').strip() if has_request_context() else ''
    except Exception:
        ip = ''
    return device._bridge_address(ip) if ip else None


def _current_device_ad_flags() -> dict:
    """device_ad_flags for the bridge device making the current request, using the
    advertising id captured for it earlier. Reads the store only — a tune NEVER triggers a
    capture (capture is explicit: DAI toggle-on or the admin button)."""
    address = _current_bridge_address()
    return device_ad_flags(device._client_device(), _saved_adids().get(address or ''))


# The billing ZIP (bZipCode) is part of the account's targeting and every DAI session must
# carry it. This is the app's own cgnatEnabled branch of universalYospaceParameters (it adds
# the account's clientContext zipCode when the feature flag is on; the bundled default is
# off and a captured Android TV tune went without it). A bridge has no location source and
# sits behind whatever IP its host has, so we take the app's fallback deliberately: a real
# account value, never invented. Sign-in and turning DAI on fetch it before any tune. If a tune still finds none
# stored (an older session, or that lookup failed), it looks it up right then:
# - the location lookup runs in the background, one per sign-in (single-flight); every
#   tune that needs it, the first included, waits for it at most _ZIP_DEADLINE in total
#   (a hard bound: the wait is on an Event, not on requests' per-phase timeouts);
# - waiters are released the moment the ZIP is known; saving it (dai_zip) comes after;
# - a lookup that failed (no answer) is retried after _ZIP_RETRY; an account the location
#   service answers for with no ZIP is not asked again for that sign-in (_ZIP_NONE_RETRY).
# A ZIP is never invented: a tune that can't get one goes without bZipCode.
_ZIP_DEADLINE = 6
_ZIP_HTTP_TIMEOUT = (3, 3)
_ZIP_RETRY = 30
_ZIP_NONE_RETRY = 3600
_ZIP_FOUND: dict[str, str] = {}          # bearer tag -> zip (this process)
_ZIP_FAILED: dict[str, tuple[float, float]] = {}   # bearer tag -> (when, retry after)
_ZIP_INFLIGHT: dict[str, threading.Event] = {}
_zip_lock = threading.Lock()


def _bearer_tag(bearer: str) -> str:
    import hashlib
    return hashlib.sha256(bearer.encode()).hexdigest()[:12]


def _known_zip(config: dict) -> str | None:
    """A ZIP this process already looked up for the config's sign-in (no network)."""
    bearer = str(config.get('bearer_token') or '')
    if not bearer:
        return None
    with _zip_lock:
        return _ZIP_FOUND.get(_bearer_tag(bearer))


# The values a DAI session was opened with that decide its ads. A cached session is only
# reused while these still match what a tune would send now, so a session opened before
# the account's ZIP/DMA/consent arrived, under another account, profile or opt-out, is
# replaced on the next tune instead of serving untargeted ads for the cache's lifetime.
_SIG_KEYS = ('hhid', 'u', 'profid', 'dma_location', 'dma_billing', 'gpp', 'gpp_sid',
             'bZipCode', '_fw_did', 'adid', 'is_lat')


def _profid_check(config: dict, have: bool) -> None:
    """Every DAI session must carry profid. Without one, start the default-profile pick
    (rate-limited) and report it in red on the panel until a tune carries one."""
    try:
        from .directv_dai_install import _note
        if have or not profiles_supported(config):
            # A web (email/password) session can't run viewer profiles: nothing to report.
            _note('profid_ok')
            return
        _note('no_profid', 'no viewer profile set yet; running as the account\'s default profile '
                           'automatically (or pick one in the DirecTV settings)')
        _start_auto_pick(config, config.get('bearer_token'), force=False)
    except Exception:
        logger.debug('[directv-dai] profid check failed', exc_info=True)


def targeting_values(flags: dict | None) -> dict:
    """The ad-deciding values a DAI session was opened with (stored with it)."""
    return {k: str(v) for k, v in (flags or {}).items() if k in _SIG_KEYS and v not in (None, '')}


def session_current(entry: dict | None, config: dict, names: dict | None, ccid: str) -> bool:
    """Whether a cached DAI session still carries what a tune would send. Read-only and no
    network. A value this process can't know yet (e.g. a ZIP another worker looked up and
    hasn't been saved) never makes a session stale; only a value that is known now and
    differs from, or is missing in, the session does (it was opened before the ZIP/DMA/
    profile arrived, under another account, or for another opt-out state)."""
    if not isinstance(entry, dict) or not entry.get('dai'):
        return True
    if 'yospace.com' not in (entry.get('fallback_url') or ''):
        return True   # this channel has no DAI stream; nothing targeted to compare
    had = entry.get('dai_targeting')
    if not isinstance(had, dict):
        return False  # opened before sessions recorded their targeting: replace once
    now = targeting_values(request_flags(config, names, ccid, zip_lookup=False, report=False))
    return all(had.get(k) == v for k, v in now.items())


def _account_zip(config: dict) -> str | None:
    """The account's billing ZIP for a tune whose config has no dai_zip (see above)."""
    bearer = str(config.get('bearer_token') or '')
    if not bearer:
        return None
    tag = _bearer_tag(bearer)
    with _zip_lock:
        if tag in _ZIP_FOUND:
            return _ZIP_FOUND[tag]
        when, hold = _ZIP_FAILED.get(tag, (0.0, 0.0))
        if time.time() - when < hold:
            return None
        done = _ZIP_INFLIGHT.get(tag)
        start = done is None
        if start:
            done = _ZIP_INFLIGHT[tag] = threading.Event()
    if start:
        try:
            device.spawn(_zip_lookup, bearer, tag, done)
        except Exception:
            logger.warning('[directv-dai] could not start the billing ZIP lookup', exc_info=True)
            with _zip_lock:
                _ZIP_INFLIGHT.pop(tag, None)
            done.set()
    done.wait(_ZIP_DEADLINE)
    with _zip_lock:
        return _ZIP_FOUND.get(tag)


def _zip_lookup(bearer: str, tag: str, done: threading.Event) -> None:
    """Background: one location lookup for a sign-in. Releases the waiting tunes as soon
    as the outcome is known, then saves what it found."""
    ctx: dict = {}
    try:
        account = requests.Session()
        account.headers.update(_up('app_headers')())
        ctx = fetch_location_context(account, bearer, timeout=_ZIP_HTTP_TIMEOUT) or {}
    except Exception as exc:
        logger.warning('[directv-dai] billing ZIP lookup failed (%s)', exc)
    zip_code = ctx.get('zip')
    with _zip_lock:
        if zip_code:
            if len(_ZIP_FOUND) > 64:
                _ZIP_FOUND.clear()
            _ZIP_FOUND[tag] = zip_code
        elif ctx:
            # The location service answered (a DMA came back) but has no ZIP for this
            # account: asking again won't change that until the next sign-in.
            _ZIP_FAILED[tag] = (time.time(), _ZIP_NONE_RETRY)
        else:
            _ZIP_FAILED[tag] = (time.time(), _ZIP_RETRY)
        if len(_ZIP_FAILED) > 64:
            now = time.time()
            for k in [k for k, (w, h) in _ZIP_FAILED.items() if now - w >= h]:
                del _ZIP_FAILED[k]
        _ZIP_INFLIGHT.pop(tag, None)
    done.set()
    if zip_code:
        _save_account_values(bearer, {f'dai_{k}': v for k, v in ctx.items() if v})
    elif ctx:
        logger.warning('[directv-dai] the account has no billing ZIP in its location data; '
                       'tunes go without bZipCode until the next sign-in')
    else:
        logger.warning('[directv-dai] no answer from the billing ZIP lookup; this tune goes without '
                       'bZipCode (retried in %ss)', _ZIP_RETRY)


def _save_account_values(bearer: str, updates: dict, wait: float = 0) -> bool:
    """Merge account targeting values into the DirecTV source (persist_config), only while
    that sign-in is the stored one. With ``wait``, first wait up to that long for the
    sign-in to be stored (sign-in commits its config after our hook returns)."""
    if not updates:
        return False
    deadline = time.time() + wait
    try:
        db, Source = _up('db'), _up('source_model')
        while True:
            db.session.expire_all()
            src = Source.query.filter_by(name='directv').first()
            current = src is not None and (src.config or {}).get('bearer_token') == bearer
            if current or time.time() >= deadline:
                break
            time.sleep(0.5)
        if not current:
            logger.info('[directv-dai] account values not saved: that sign-in is no longer the stored one')
            return False
        if _up('persist_config')(src.id, updates):
            logger.info('[directv-dai] saved the account targeting values (%s)', ', '.join(sorted(updates)))
            return True
        logger.warning('[directv-dai] could not save the account targeting values (%s)', ', '.join(sorted(updates)))
    except Exception:
        logger.warning('[directv-dai] could not save the account targeting values', exc_info=True)
    return False


def gpp_targeted_ad_opt_out(gpp: str) -> bool | None:
    """Decode the US-National (GPP section id 7) consent string and report whether
    the account has opted OUT of targeted advertising.

    Returns True (opted out), False (not opted out), or None if it can't be read.
    The US-National section's opt-out fields use 0=N/A, 1=Opted Out, 2=Did Not
    Opt Out. Validated against a real capture: an account with ad personalization
    turned off decodes to TargetedAdvertisingOptOut=1, i.e. is_lat=1.
    """
    try:
        section = (gpp or '').partition('~')[2]
        if not section:
            return None
        b64 = section.replace('-', '+').replace('_', '/')
        b64 += '=' * (-len(b64) % 4)
        bits = ''.join(f'{byte:08b}' for byte in base64.b64decode(b64))
        pos = 6  # Version(6)
        # 6 notice fields (2 bits each), then SaleOptOut, SharingOptOut,
        # TargetedAdvertisingOptOut (2 bits each).
        pos += 6 * 2  # the notice fields
        pos += 2 * 2  # SaleOptOut, SharingOptOut
        tao = int(bits[pos:pos + 2], 2)
        return tao == 1
    except Exception:
        return None


# ── Scrape ───────────────────────────────────────────────────────────────────

_CCID_KEYS = ('ccid', 'ccId', 'channelId', 'channel_id', 'id')


def note_channels(scraper, rows) -> None:
    """Record each lineup row's DAI "net" tag (daiChannelName) in the scraper's cache,
    where a tune reads it. Called with every AllChannels page of rows; writes the cache
    only when something changed."""
    names = getattr(scraper, _up_name('scraper_cache')).get('dai_channel_names')
    names = dict(names) if isinstance(names, dict) else {}
    changed = False
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ccid = next((str(row[k]).strip() for k in _CCID_KEYS if row.get(k) not in (None, '')), '')
        value = row.get('daiChannelName')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not ccid or not isinstance(value, str) or not value.strip():
            continue
        if names.get(ccid) != value.strip():
            names[ccid] = value.strip()
            changed = True
    if changed:
        getattr(scraper, _up_name('scraper_update_cache'))('dai_channel_names', names)


# ── Login ────────────────────────────────────────────────────────────────────

# The account values that came from a lookup and the chosen viewer profile belong to one
# DirecTV account. dai_account records which (its partnerProfileId, and dai_account_source
# where that id came from), so a sign-in to a different account doesn't send another
# account's market, ZIP or profile (see _note_account).
_ACCOUNT_FETCH_WAIT = 30


def store_login_result(cfg: dict, result: dict) -> None:
    """Add the account's own DAI values to a login result's config. Runs inside upstream's
    apply_auth_result on every sign-in and refresh, i.e. between upstream reading the
    source config and committing it, so it makes NO network calls (anything slow here
    would widen that window and revert writes made meanwhile). Values the result carries
    are set in place; the account lookup (DMA/ZIP/consent), when needed, runs in the
    background and is merged with persist_config once this sign-in is stored. Until it
    lands, a tune looks the ZIP up itself (_account_zip), so even the first tune of a new
    sign-in carries bZipCode."""
    partner_profile_id = result.get('partner_profile_id')
    profile_id = result.get('profile_id')
    # Where the household id came from: the code sign-in's grant (valuePairs) or a web
    # token's own claims. The two have not been confirmed to carry the same value for one
    # account, so ids are only compared within the same source (see below).
    id_source = 'grant' if partner_profile_id else ''
    # Paths with no token-exchange valuePairs fall back to the bearer JWT's own claims
    # (the code sign-in's tokens are opaque, so there it's the valuePairs or nothing).
    if not partner_profile_id or not profile_id:
        jwt_pp, jwt_pf = ids_from_bearer_jwt(result.get('bearer_token') or '')
        if not partner_profile_id and jwt_pp:
            id_source = 'jwt'
        partner_profile_id = partner_profile_id or jwt_pp
        profile_id = profile_id or jwt_pf
    bearer = result.get('bearer_token')
    if partner_profile_id:
        _note_account(cfg, partner_profile_id, id_source, bearer)
        cfg['dai_partner_profile_id'] = partner_profile_id
    if profile_id:
        cfg['dai_profile_id'] = profile_id
    for k, v in (result.get('dai_context') or {}).items():
        if v:
            cfg[f'dai_{k}'] = v
    # Only with DAI on: with it off, sign-in makes no extra DirecTV requests (turning DAI
    # on fetches them then; see settings_changed). A code session refetches on every
    # sign-in/refresh, as before; any session missing a value fetches it.
    if (enabled(cfg) and bearer and 'dai_context' not in result
            and (result.get('auth_method') == 'device_code' or not cfg.get('dai_dma_id')
                 or not cfg.get('dai_zip'))):
        try:
            device.spawn(_fetch_account_values_after_signin, bearer)
        except Exception:
            logger.warning('[directv-dai] could not start the account lookup after sign-in', exc_info=True)
    _start_auto_pick(cfg, bearer)


# The looked-up account values (refetched automatically on the next sign-in or tune) and
# the chosen viewer profile (picked by a person, so never dropped on a guess).
_LOOKUP_VALUES = ('dai_dma_id', 'dai_zip', 'dai_gpp', 'dai_gpp_sid')
_PROFILE_VALUES = ('dtv_android_profid', 'dtv_android_profile_id')


def _note_account(cfg: dict, account_id: str, id_source: str, bearer) -> None:
    """Record which DirecTV account the stored values belong to, and react to a change:
    - same id source, different id: a different account for certain; drop its looked-up
      values and its chosen profile (warning);
    - different id sources with different ids (a code sign-in after a web sign-in, or the
      reverse): not known to be a different account, so the looked-up values are dropped
      (they are refetched at once, so nothing is lost) and the chosen profile is kept but
      checked against this account's own profile list in the background, dropped only if
      it isn't one of them (warning either way)."""
    owner = str(cfg.get('dai_account') or '')
    owner_source = str(cfg.get('dai_account_source') or '')
    cfg['dai_account'] = account_id
    if id_source:
        cfg['dai_account_source'] = id_source
    if not owner or owner == account_id:
        return
    certain = bool(owner_source and id_source and owner_source == id_source)
    dropped = [k for k in _LOOKUP_VALUES if cfg.pop(k, None) is not None]
    cfg.pop('dai_profile_id', None)   # comes from this sign-in's own result, if anywhere
    if certain:
        dropped += [k for k in _PROFILE_VALUES if cfg.pop(k, None) is not None]
        logger.warning('[directv-dai] signed in to a different DirecTV account; dropped the previous '
                       "account's values (%s)", ', '.join(dropped) or 'none')
        return
    logger.warning('[directv-dai] the household id differs between the %s and %s sign-in; refetching the '
                   'market/ZIP/consent (%s) and checking the chosen profile against this account',
                   owner_source or 'earlier', id_source or 'new', ', '.join(dropped) or 'none')
    if cfg.get('dtv_android_profile_id') and bearer:
        try:
            device.spawn(_check_profile_after_signin, str(bearer), str(cfg['dtv_android_profile_id']))
        except Exception:
            logger.warning('[directv-dai] could not start the profile check', exc_info=True)


def _check_profile_after_signin(bearer: str, profile_id: str) -> None:
    """Background: keep the chosen profile only if this account has it. An unreadable
    profile list keeps it (it is never dropped on a guess) and says so."""
    info = list_profiles(bearer)
    ids = {p.get('id') for p in (info.get('profiles') or [])}
    if not ids:
        logger.warning('[directv-dai] could not read the account\'s profiles; the chosen profile is kept '
                       'unverified (re-pick it in the DirecTV settings if ads look untargeted)')
        return
    if profile_id in ids:
        logger.info('[directv-dai] the chosen DirecTV profile belongs to this account; kept')
        return
    if _save_account_values(bearer, {'dtv_android_profid': None, 'dtv_android_profile_id': None},
                            wait=_ACCOUNT_FETCH_WAIT):
        logger.warning('[directv-dai] the chosen DirecTV profile is not on this account; cleared it '
                       '(pick one in the DirecTV settings)')


def _auto_pick_profile(bearer: str) -> None:
    """Background: with no viewer profile picked, run as the account's DEFAULT profile
    (its primary; every account has one), so every DAI session carries a real profid
    (partnerProfileID1). Uses select_profile, so it waits on upstream's refresh
    lock, verifies a rotated refresh token and clears cached streams. A pick made in the
    panel always wins: this never replaces one."""
    db, Source = _up('db'), _up('source_model')
    for attempt in range(6):
        db.session.expire_all()
        src = Source.query.filter_by(name='directv').first()
        cfg = dict((src.config if src else None) or {})
        if not src or cfg.get('dtv_android_profid') or not enabled(cfg) or not profiles_supported(cfg):
            return
        if cfg.get('bearer_token') != bearer:
            time.sleep(5)   # sign-in commits after our hook; wait for it
            continue
        info = list_profiles(bearer)
        profiles = info.get('profiles') or []
        pick = (next((p for p in profiles if p.get('primary')), None)
                or next((p for p in profiles if p['id'] == info.get('current_id')), None)
                or (profiles[0] if len(profiles) == 1 else None))
        if not pick:
            logger.warning('[directv-dai] no viewer profile to run as automatically; pick one in the '
                           'DirecTV settings so ads carry profid')
            return
        res = select_profile(src, pick['id'], pick.get('name') or '')
        if res.get('ok'):
            logger.info('[directv-dai] running as the account\'s default profile automatically (profid set)')
            return
        if 'refresh is in flight' not in (res.get('error') or ''):
            logger.warning('[directv-dai] automatic profile pick failed: %s', res.get('error'))
            return
        time.sleep(10)


_AUTO_PICK_RETRY = 300
_auto_pick_at = [0.0]


def _start_auto_pick(cfg: dict, bearer, force: bool = True) -> None:
    """Start the default-profile pick (sign-in, DAI turned on, or a tune that found no
    profid: at most every _AUTO_PICK_RETRY per process for the tune path)."""
    if not (bearer and enabled(cfg) and not cfg.get('dtv_android_profid') and profiles_supported(cfg)):
        return
    now = time.time()
    if not force and now - _auto_pick_at[0] < _AUTO_PICK_RETRY:
        return
    _auto_pick_at[0] = now
    try:
        device.spawn(_auto_pick_profile, str(bearer))
    except Exception:
        logger.warning('[directv-dai] could not start the automatic profile pick', exc_info=True)


def _fetch_account_values_after_signin(bearer: str) -> None:
    """Background, after a sign-in: the account's DMA/ZIP/consent, merged into the source
    once that sign-in is stored (persist_config, so nothing written meanwhile is lost)."""
    account = requests.Session()
    account.headers.update(_up('app_headers')())
    ctx = fetch_account_context(account, bearer)
    updates = {f'dai_{k}': v for k, v in ctx.items() if v}
    if not updates.get('dai_zip'):
        logger.warning('[directv-dai] sign-in: the account lookup returned no billing ZIP; '
                       'the first tune looks it up again')
    if updates.get('dai_zip'):
        with _zip_lock:
            _ZIP_FOUND[_bearer_tag(bearer)] = updates['dai_zip']
    _save_account_values(bearer, updates, wait=_ACCOUNT_FETCH_WAIT)


def fetch_location_context(session, bearer: str, timeout: float = 15) -> dict:
    """The account's DMA and billing ZIP from the location service ({} on any failure)."""
    ctx: dict = {}
    try:
        app = dict(_up('app_headers')() or {})
    except Exception:
        app = {}
    hdrs = {**app, 'Accept': 'application/json, text/plain, */*', 'Authorization': f'Bearer {bearer}'}
    try:
        r = session.get(_LOCATION_URL, params={'includeTVOD': 'false'}, headers=hdrs, timeout=timeout)
        if r.ok:
            data = r.json()
            dma = _find_first_key(data, 'dmaId')
            if dma:
                ctx['dma_id'] = str(dma)
            billing = data.get('billingDmas') if isinstance(data, dict) else None
            zip_code = _find_first_key(billing, 'zipcode') or _find_first_key(data, 'zipcode')
            if zip_code:
                ctx['zip'] = str(zip_code)
        else:
            logger.warning('[directv-dai] location lookup HTTP %s', r.status_code)
    except Exception as exc:
        logger.warning('[directv-dai] location lookup failed: %s', exc)
    return ctx


def fetch_account_context(session, bearer: str, timeout: float = 15) -> dict:
    """Best-effort fetch of the account's real DMA and privacy-consent string, the
    values DirecTV's apps put on their DAI session request. Sent as the same client as
    the rest of the session: upstream's app headers (its fixed app User-Agent), no web
    player Origin/Referer. Any failure returns {} — DAI playback still works, it just
    omits the fields we couldn't source rather than inventing them."""
    ctx: dict = {}
    try:
        app = dict(_up('app_headers')() or {})
    except Exception:
        app = {}
    hdrs = {**app, 'Accept': 'application/json, text/plain, */*', 'Authorization': f'Bearer {bearer}'}
    ctx.update(fetch_location_context(session, bearer, timeout=timeout))
    try:
        r = session.get(_BASICINFO_URL, params={'requestIds': 'true', 'requestShortIds': 'true'},
                        headers=hdrs, timeout=timeout)
        if r.ok:
            data = r.json()
            gpp = _find_first_key(data, 'gpp')
            gpp_sid = _find_first_key(data, 'gpp_sid')
            if gpp:
                ctx['gpp'] = str(gpp)
            if gpp_sid is not None:
                ctx['gpp_sid'] = str(gpp_sid)
    except Exception as exc:
        logger.debug('[directv-dai] basicinfo lookup failed: %s', exc)
    return ctx


def ids_from_bearer_jwt(bearer: str) -> tuple[str | None, str | None]:
    """Best-effort (partnerProfileId, profileId) read from the bearer JWT's own
    claims, for auth paths that don't surface the token-exchange valuePairs (the
    Playwright fallback). No signature verification — we only read claims from a
    token DirecTV issued to us, never trusting it for authorization — and no value
    is logged. Returns (None, None) if the token can't be parsed or lacks them."""
    try:
        seg = bearer.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return None, None
    flat = {k.lower(): v for k, v in claims.items() if isinstance(v, (str, int))}

    def pick(*names: str) -> str | None:
        for n in names:
            v = flat.get(n.lower())
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    return (pick('partnerProfileId', 'partnerprofileid', 'ppid'),
            pick('profileId', 'profileid', 'pid'))


def _find_first_key(obj, key):
    """Depth-first search for the first value under `key` anywhere in a JSON tree."""
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            found = _find_first_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_first_key(v, key)
            if found is not None:
                return found
    return None


# ── Viewer profiles (the Yospace `profid`) ─────────────────────────────────────
# A DirecTV account has several viewer profiles, like Netflix. The Android TV app runs as
# one of them and sends THAT profile's ad id as the Yospace `profid`. The device-code
# grant gives the household id (partnerProfileId -> hhid/u) but no profile id, so without
# this `profid` is never sent. The app gets it by POSTing the chosen profile's profileID to
# the profiletoken endpoint and reading valuePairs.partnerProfileID1. It does NOT swap the
# account bearer per profile (the launch and profile-switch paths in the app bundle only
# store partnerProfileID1), so only `profid` changes per profile. We do exactly that.
_PROFILES_URL = 'https://api.cld.dtvce.com/profile/user/manager/v1/profiles'
_PROFILE_TOKEN_URL = 'https://api.cld.dtvce.com/ac/authn/profiletoken/v1/tokens'
# Covers the worst case: the exchange (30 s) plus three saves, each of which can wait on
# upstream's source lock and SQLite busy retries. Released at once when done.
_PROFILE_LOCK_TTL = 600


def _profile_headers() -> dict:
    return _up('app_headers')()


def _profile_client_id() -> str:
    """The code sign-in's own client id (upstream's, by role), so the profile exchange is
    always made as the same app client the session was issued to. No fallback: a rename
    upstream fails validate.sh/smoke_test.py by name instead of sending a stale id."""
    return str(_up('code_signin_client_id'))


def profiles_supported(config: dict | None) -> bool:
    """Profiles need the Android TV code sign-in (the profiletoken exchange is that
    client's); a web email/password session can't run them."""
    return bool(_up('is_code_signin')(config or {}))


def list_profiles(bearer: str) -> dict:
    """The account's viewer profiles and which one is current, from the Android TV app's
    profile-manager endpoint. Read-only: a plain GET with the account bearer, no token
    rotation and no profile switch. Returns
    ``{'profiles': [{'id', 'name', 'primary'}], 'current_id': <id>}`` or ``{}`` on any
    failure, so the picker just shows nothing rather than breaking the settings page."""
    bearer = (bearer or '').strip()
    if not bearer:
        return {}
    try:
        r = requests.get(_PROFILES_URL,
                         headers={**_profile_headers(), 'Authorization': f'Bearer {bearer}'}, timeout=20)
    except Exception as exc:
        logger.debug('[directv-dai] profile list failed: %s', exc)
        return {}
    if r.status_code < 200 or r.status_code >= 300:
        logger.warning('[directv-dai] profiles HTTP %s', r.status_code)
        return {}
    data = r.json() if r.content else {}
    profiles = []
    for p in (data.get('profiles') or []):
        if not isinstance(p, dict):
            continue
        pid = (p.get('profileID') or '').strip()
        if pid:
            profiles.append({'id': pid,
                             'name': (p.get('profileName') or '').strip() or 'Profile',
                             'primary': bool(p.get('isPrimaryProfile'))})
    return {'profiles': profiles, 'current_id': (data.get('currentProfileID') or '').strip()}


def _partner_profile_id1(token_data: dict) -> str:
    """The profile-scoped id the app sends as `profid`, read from a profiletoken response
    (``partnerProfileID1``, capital ID, in the live response; the lowercase variant is
    accepted too). Searched in valuePairs first, then the top level."""
    vp = token_data.get('valuePairs') if isinstance(token_data.get('valuePairs'), dict) else {}
    for src in (vp, token_data):
        for k in ('partnerProfileID1', 'partnerProfileId1'):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ''


def profile_token_exchange(profile_id: str, refresh_token: str) -> dict:
    """The Android TV app's profiletoken exchange for one viewer profile. Request shape
    from the app bundle: ``POST`` JSON ``{profileId, refreshToken, clientId}``. The response
    carries ``valuePairs.partnerProfileID1``, the app's Yospace `profid` for that profile.
    Raises on a non-success response so a caller never persists a dead result."""
    profile_id = (profile_id or '').strip()
    refresh_token = (refresh_token or '').strip()
    if not profile_id or not refresh_token:
        raise RuntimeError('profile token exchange needs a profile id and a refresh token')
    body = {'profileId': profile_id, 'refreshToken': refresh_token, 'clientId': _profile_client_id()}
    r = requests.post(_PROFILE_TOKEN_URL, json=body,
                      headers={**_profile_headers(), 'Content-Type': 'application/json'}, timeout=30)
    if r.status_code < 200 or r.status_code >= 300:
        logger.warning('[directv-dai] profiletoken HTTP %s: %s', r.status_code, (r.text or '')[:200])
        raise RuntimeError(f'profile token exchange failed HTTP {r.status_code}')
    data = r.json() if r.content else {}
    if data.get('errorCode') or data.get('success') is False:
        logger.warning('[directv-dai] profiletoken returned an error (fields=%s)', sorted(data.keys()))
        raise RuntimeError('profile token exchange returned an error')
    return data


def _redis():
    """The install module's shared redis client (1 s timeouts), or None."""
    from .directv_dai_install import _redis as shared
    return shared()


def select_profile(source, profile_id: str, profile_name: str = '') -> dict:
    """Run the DirecTV session as the given viewer profile: exchange the profile at the
    profiletoken endpoint, store its ``partnerProfileID1`` as the Yospace `profid`
    (``dtv_android_profid``, which build_query reads), and clear the cached Yospace
    playback so the next tune uses it. The account session (bearer/refresh/activation) is
    left as-is, as the app does. Single-flight behind upstream's own refresh lock
    (``directv:auth:refreshing:<name>``) so a concurrent refresh can't race the exchange
    and trip DirecTV's refresh reuse-detection. Returns ``{'ok', 'profid', 'error'}``;
    never raises."""
    profile_id = (profile_id or '').strip()
    cfg = dict(source.config or {})
    if not profiles_supported(cfg):
        return {'ok': False, 'profid': False, 'error': 'profiles need the DirecTV code sign-in'}
    if not profile_id:
        return {'ok': False, 'profid': False, 'error': 'no profile selected'}
    rt = (cfg.get('refresh_token') or '').strip()
    if not rt:
        return {'ok': False, 'profid': False, 'error': 'no refresh token: sign in first'}

    rdb = _redis()
    lock_key = f'directv:auth:refreshing:{source.name}'
    # Our own value (upstream writes '1'), so the release below drops only our lock.
    lock_value = f'directv-dai-profile:{os.getpid()}:{time.time_ns()}'
    have_lock = True
    if rdb is not None:
        try:
            have_lock = bool(rdb.set(lock_key, lock_value, nx=True, ex=_PROFILE_LOCK_TTL))
        except Exception:
            have_lock = True  # redis down: don't wedge a user action
    if not have_lock:
        return {'ok': False, 'profid': False, 'error': 'a token refresh is in flight; try again in a moment'}

    try:
        try:
            data = profile_token_exchange(profile_id, rt)
        except Exception as exc:
            return {'ok': False, 'profid': False, 'error': str(exc)}
        profid = _partner_profile_id1(data)
        # Merged into the live row under upstream's per-source lock (persist_config),
        # never a commit of the copy read at the start: a refresh or a DRM self-heal
        # that wrote the config meanwhile is kept.
        # The picked profile is saved only together with its profid: without one, the
        # panel must not show a profile that ads aren't requested as.
        updates = {'dtv_android_profile_id': profile_id, 'dtv_android_profid': profid} if profid else {}
        # Persist a rotated refresh token only if the exchange returned one; otherwise
        # leave the account session untouched (the profile-scoped bearer it mints isn't
        # adopted by the app, so we don't adopt it either).
        vp = data.get('valuePairs') if isinstance(data.get('valuePairs'), dict) else {}
        new_refresh = (data.get('refresh_token') or data.get('refreshToken')
                       or vp.get('refreshToken') or '').strip()
        if new_refresh:
            updates['refresh_token'] = new_refresh
        saved = not updates
        attempts = 3 if new_refresh else 1
        for attempt in range(attempts if updates else 0):
            try:
                saved = (_up('persist_config')(source.id, updates)
                         and _saved_as(source.id, updates.get('refresh_token')))
            except Exception:
                saved = False
                logger.warning('[directv-dai] could not persist profile selection', exc_info=True)
            if saved:
                break
            if attempt < attempts - 1:
                time.sleep(1 + attempt)
        if not saved and new_refresh:
            logger.error('[directv-dai] the profile exchange rotated the refresh_token but it could not be '
                         'saved; if DirecTV retired the old one, sign in again')
        if not saved:
            return {'ok': False, 'profid': False, 'error': 'could not save the selection; try again'}
    finally:
        # Drop the lock only if it is still ours (it may have expired and been taken by
        # upstream's refresh meanwhile): an atomic compare-and-delete. Then, success or
        # not, queue the refresh upstream may have skipped while we held it.
        released = rdb is None
        if rdb is not None:
            try:
                released = bool(_release_lock(rdb, lock_key, lock_value))
            except Exception:
                logger.warning('[directv-dai] could not release the profile-switch lock; it expires in %ss',
                               _PROFILE_LOCK_TTL, exc_info=True)
        if released:
            _requeue_stale_refresh(source)
    # The next tune opens a session with the new profid (nothing to clear if none was saved).
    if not profid:
        logger.warning('[directv-dai] profile "%s": exchange succeeded but no partnerProfileID1 in the '
                       'response; nothing changed', (profile_name or '').strip() or 'the selected profile')
        return {'ok': False, 'profid': False,
                'error': 'the profile exchange returned no profile id (nothing was changed to a bad value)'}
    try:
        persist_source_cache_updates = _up('persist_cache')
        persist_source_cache_updates(source.id, {'directv_playback': {}, 'dai_playback_by_device': {}})
    except Exception:
        logger.debug('[directv-dai] could not clear cached streams after the profile switch', exc_info=True)

    who = (profile_name or '').strip() or 'the selected profile'
    if profid:
        logger.info('[directv-dai] now running as DirecTV profile "%s" (profid set)', who)
        return {'ok': True, 'profid': True, 'error': ''}
    logger.warning('[directv-dai] profile "%s": exchange succeeded but no partnerProfileID1 in the '
                   'response; profid left unset', who)
    return {'ok': False, 'profid': False,
            'error': 'the profile exchange returned no profile id (nothing was changed to a bad value)'}


_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"


def _saved_as(source_id, refresh_token: str | None) -> bool:
    """Re-read the row: a save reported as done must really hold the rotated token
    (upstream's commit retry can report success after a rollback dropped the change)."""
    if not refresh_token:
        return True
    db, Source = _up('db'), _up('source_model')
    db.session.expire_all()
    src = db.session.get(Source, source_id)
    return bool(src) and (src.config or {}).get('refresh_token') == refresh_token


def _release_lock(rdb, key: str, value: str):
    """Delete key only if it still holds value, atomically (Lua); a plain compare-then-
    delete fallback for a client without scripting."""
    try:
        return rdb.eval(_RELEASE_LUA, 1, key, value)
    except Exception:   # no scripting (old client, ACL, or a stub): compare-then-delete
        held = rdb.get(key)
        if (held.decode() if isinstance(held, bytes) else held) == value:
            rdb.delete(key)
            return 1
        return 0


def _requeue_stale_refresh(source) -> None:
    """While the profile exchange held upstream's refresh lock, upstream's own background
    re-auth would have seen "already in progress" and skipped. If the session went stale
    meanwhile, queue that refresh now (upstream's own path) instead of waiting for its
    15-minute watchdog. Only the age check (_token_stale) is re-run: a token DirecTV
    rejected outright (0015) during that window is left to upstream's next tune, which
    re-detects it, or its watchdog. Best-effort."""
    try:
        Source = _up('source_model')
        src = Source.query.get(source.id) or source
        scraper = _up_owner('resolve')(dict(src.config or {}))
        if _up('token_stale')(scraper) and _up('can_reauth')(scraper.config):
            _up('start_reauth')(scraper)
            logger.info('[directv-dai] queued the token refresh deferred by the profile switch')
    except Exception:
        logger.warning('[directv-dai] could not re-check the token after the profile switch', exc_info=True)


# ── Settings ─────────────────────────────────────────────────────────────────

def settings_changed(source, old: dict, current: dict) -> None:
    """After the DAI panel saves the toggle: drop cached URLs on a flip, capture new
    devices' ad ids, and fetch the account's targeting values if they're missing."""
    _DAI_ON[0] = 0.0
    if enabled(current) and not enabled(old):
        _start_auto_pick(current, current.get('bearer_token'))
    # Never fails the save: the toggle is already stored.
    try:
        clear_cache_if_toggled(source, old, current)
    except Exception:
        logger.error('[directv-dai] after the DAI toggle: could not clear the cached streams or start '
                     'the advertising-id capture', exc_info=True)
    if (enabled(current) and not enabled(old) and current.get('bearer_token')
            and not (current.get('dai_dma_id') and current.get('dai_zip'))):
        # In the save request itself, so the values are stored before the next tune. Only
        # when DAI is turned on (a re-save doesn't repeat the lookup), and never fails the
        # save: the toggle is already stored, and a tune still finding no dai_zip looks it
        # up itself (_account_zip).
        try:
            _fetch_missing_account_context(source.id)
        except Exception:
            logger.warning('[directv-dai] turning DAI on: could not fetch the account targeting values',
                           exc_info=True)


_TURN_ON_TIMEOUT = (4, 4)


def _fetch_missing_account_context(source_id: int) -> None:
    """DAI was turned on for a session whose sign-in ran with DAI off: fetch the
    account's DMA/ZIP/consent now (the same request sign-in makes), with its bearer."""
    persist_source_config_updates = _up('persist_config')
    Source = _up('source_model')
    src = Source.query.get(source_id)
    cfg = dict((src.config if src else None) or {})
    if not cfg.get('bearer_token') or (cfg.get('dai_dma_id') and cfg.get('dai_zip')):
        return
    account = requests.Session()
    account.headers.update(_up('app_headers')())
    ctx = fetch_account_context(account, cfg['bearer_token'], timeout=_TURN_ON_TIMEOUT)
    updates = {f'dai_{k}': v for k, v in ctx.items() if v}
    if not updates.get('dai_zip'):
        logger.warning('[directv-dai] turning DAI on: the account lookup returned no billing ZIP; '
                       'the first tune will look it up again')
    if updates:
        if persist_source_config_updates(source_id, updates):
            logger.info('[directv-dai] fetched the account targeting values (%s)', ', '.join(sorted(updates)))
        else:
            logger.warning('[directv-dai] could not save the account targeting values (%s)',
                           ', '.join(sorted(updates)))


def clear_cache_if_toggled(source, old: dict, current: dict) -> None:
    """The toggle picks which stream URL resolve() caches (for 55 minutes), so
    drop every cached one when it changes; the next tune fetches under the new setting."""
    if getattr(source, 'name', None) != 'directv':
        return
    if enabled(old) != enabled(current):
        persist_source_cache_updates = _up('persist_cache')
        persist_source_cache_updates(source.id, {'directv_playback': {}, 'dai_playback_by_device': {}})
    if enabled(current):
        # Whenever DAI is saved on, capture the advertising id of any bridge device that
        # doesn't have one yet (all of them right after turning DAI on). Background, skipping
        # any box that's currently playing. This save-time pass is the ONLY automatic
        # trigger -- a tune never captures (a Google TV box is never interrupted mid-stream).
        capture_in_background(UNCAPTURED)
