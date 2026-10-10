"""Overlay smoke test, run inside the freshly built image (cwd /app).

Imports the patched modules with the image's real dependencies and exercises the
overlay's code with synthetic inputs, so a merge that compiles but no longer works
still fails the build. No network access and no real account data.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.getcwd())


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def us_national_gpp(targeted_ad_opt_out: int) -> str:
    """A synthetic GPP string whose US-National (section 7) TargetedAdvertisingOptOut
    field is set to the given value (1 = opted out, 2 = did not opt out)."""
    bits = format(1, '06b')                      # Version
    bits += '01' * 6                             # the six notice fields
    bits += '10' * 2                             # SaleOptOut, SharingOptOut
    bits += format(targeted_ad_opt_out, '02b')   # TargetedAdvertisingOptOut
    bits += '0' * (-len(bits) % 8)
    section = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    return 'DBABLA~' + b64url(section)



# The app factory runs the one DAI hook. Point it at a throwaway database so the smoke
# container (no /data volume) can build the app.
import tempfile  # noqa: E402
_tmpdir = tempfile.mkdtemp()
os.environ['DATABASE_URL'] = os.environ.get('DATABASE_URL') or f'sqlite:///{_tmpdir}/smoke.db'

import inspect  # noqa: E402
import time as _t  # noqa: E402
import types as _types  # noqa: E402
from urllib.parse import parse_qs, quote as _quote, urlsplit  # noqa: E402

from app import create_app  # noqa: E402
from app.scrapers import directv_dai as dai, directv_dai_device as device  # noqa: E402
from app.scrapers import directv_dai_install as install  # noqa: E402
from app.scrapers import dtv_aac_ads, dtv_aac_gain  # noqa: E402

# Upstream's objects are looked up by ROLE (the keys of directv_dai_install's TARGETS and
# USES), never by name here: when upstream renames or moves something, its one entry there
# changes and these tests follow it.
up = install.up
directv = install.up_owner('channel_fetch')          # upstream's DirecTV scraper module
Scraper = install.up_owner('resolve')                # its scraper class
BaseScraper = install.up_owner('scraper_cache')
directv_proxy = install.up_owner('relay_cdn_allowed')

app = create_app()
create_app()   # a second app in the same process (the worker does this) must not double-wrap

# ── The one hook, and every upstream name it wires into ──────────────────────
# install() wraps upstream functions by name at runtime. If upstream renamed one, DAI
# would quietly turn off; fail the build with the exact name instead.
assert install.missing() == [], f'upstream moved DAI wiring targets: {install.missing()}'
# The lists are the spec of what's checked: a fix may rename an entry, never drop one.
_ROLES = {'app_user_agent', 'channel_fetch', 'apply_auth_result', 'resolve', 'license_request', 'lineup_rows',
          'code_signin_result', 'relay_cdn_suffixes', 'license_content_id', 'auth_expired_error',
          'code_signin_method', 'app_headers', 'is_code_signin', 'relay_cdn_allowed', 'scraper_cache',
          'scraper_update_cache', 'known_devices', 'persist_cache', 'persist_config', 'source_model', 'db',
          'code_signin_client_id', 'playback_cache_ttl', 'token_stale', 'can_reauth', 'start_reauth'}
assert set(install.TARGETS) | set(install.USES) >= _ROLES and len(install.SOURCE_MARKERS) >= 2, \
    f'an upstream role or marker was dropped from directv_dai_install (point it at the new name instead): {sorted(_ROLES - set(install.TARGETS) - set(install.USES))}'
assert any('refreshing' in text for path, text, _ in install.SOURCE_MARKERS), \
    'the refresh-lock marker is gone (the profile swap must wait on upstream\'s refresh lock)'
_init_src = inspect.getsource(create_app)
assert 'directv_dai_install.install(app)' in _init_src, 'the one DAI hook is gone from create_app'
_wired = [(f'{r} ({".".join(install.TARGETS[r])})', getattr(up(r), '__func__', up(r)))
          for r in install.TARGETS if callable(up(r))]   # every wrapped target (not the allowlist tuple)
assert len(_wired) >= 6, _wired
for _name, _fn in (*_wired, ('requests.Session.request', install.requests.Session.request)):
    assert getattr(_fn, '__directv_dai__', False), f'{_name} is not wired'
    assert not getattr(getattr(_fn, '__wrapped__', None), '__directv_dai__', False), f'{_name} was wrapped twice'
assert not getattr(up('is_code_signin'), '__directv_dai__', False), \
    'is_device_session is wrapped again (old sessions are re-tagged once instead)'
for _hook in ('_directv_dai_ad_segment', '_directv_dai_migrate'):
    assert [f.__name__ for f in app.before_request_funcs.get(None, [])].count(_hook) == 1, f'{_hook} not registered exactly once'
for _hook in ('_directv_dai_playlist', '_directv_dai_admin_script'):
    assert [f.__name__ for f in app.after_request_funcs.get(None, [])].count(_hook) == 1, f'{_hook} not registered exactly once'
# The same report the admin page shows: every name and upstream string the wiring relies
# on is present, and no wiring step raised.
with app.app_context():
    _st = install.status(app)
assert _st == {'missing': [], 'markers': [], 'failed': [], 'runtime': []}, f'DAI wiring is incomplete: {_st}'
assert callable(getattr(BaseScraper, '_update_cache', None)), 'BaseScraper._update_cache is gone'
assert isinstance(inspect.getattr_static(BaseScraper, 'cache'), property), 'BaseScraper.cache is no longer a property'
# The device ad id reads upstream's bridge-device list; if it's renamed or reshaped the
# patch would silently fall back to national-only ads, so fail the build instead.
# (The overlay reads it only through directv_dai_device.known_bridge_devices(); the end-to-end
# section below checks that returns a registered box.)
assert callable(getattr(device, 'known_bridge_devices', None)), 'directv_dai_device.known_bridge_devices is gone'

# The toggle is our panel's (config['use_dai']), not in upstream's schema; Yospace takes the relay.
keys = [field.key for field in Scraper.config_schema]
assert 'use_dai' not in keys, "use_dai is back in upstream's config_schema (the DAI panel owns it now)"
assert dai.CONFIG_FIELD['key'] == 'use_dai' and dai.CONFIG_FIELD['label'], dai.CONFIG_FIELD
assert 'surround_audio' not in keys, 'the removed Surround sound toggle is back'
assert up('relay_cdn_allowed')('csm-e-dtv-livecomplex-eb.tls1.yospace.com'), \
    'Yospace playlists bypass the FastChannels relay'
# Sign-in, refresh and DRM are upstream's now; the overlay must not bring its own back.
import importlib.util  # noqa: E402
assert importlib.util.find_spec('app.scrapers.dtv_android') is None, \
    'dtv_android is back: sign-in/refresh/DRM are upstream (directv_device_auth); the overlay is DAI only'
# The relay and the DAI authorization send upstream's own fixed DirecTV app User-Agent
# (the one its sign-in, refresh and DRM calls send), never the bare Custom-Exoplayer
# fallback, which stopped ad insertion.
assert str(up('app_user_agent')).startswith('APP_PROJECT_NAME/') and 'PureRN/' in up('app_user_agent'), up('app_user_agent')
assert device.player_user_agent() is None, 'outside a request there is no device to build a User-Agent for'
assert not hasattr(dai, 'keep_drm_session'), 'keep_drm_session was reverted (it broke ad targeting)'

# AAC ad swap: each inserted AC-3 ad (segment + its own EXT-X-MAP init) is rewritten to
# the creative's full-range HE-AAC twin; live (dfwlive-*) content is untouched; the clear
# ads keep METHOD=NONE.
_aac_pl = (
    '#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n'
    '#EXT-X-MAP:URI="https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-i.mp4"\n'
    'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-0.mp4\n'
    'https://dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4\n'
)
_aac_out = dtv_aac_ads.swap_muffled_ads(_aac_pl)
assert 'u-6600-a-96-1-i.mp4' in _aac_out and 'u-6600-a-96-1-0.mp4' in _aac_out, 'inserted AC-3 ad (and its init) not swapped to the AAC twin'
assert 'u-6600-c-384' not in _aac_out, 'an AC-3 ad rendition survived the swap'
assert 'dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4' in _aac_out, 'live content must not be touched'
assert _aac_out.count('a-96') == 2, 'exactly the ad segment and its init should flip'
assert dtv_aac_ads.swap_muffled_ads('#EXTM3U\nhttps://dfwlive/live/s.mp4\n') == '#EXTM3U\nhttps://dfwlive/live/s.mp4\n', 'a playlist with no inserted AC-3 ad must be unchanged'

# Stereo-downmix feature REMOVED 2026-10-03 (flawed: declaring CHANNELS="2" on a 5.1 AC-3
# bitstream made the stick/media3 do a thin fold). The AC-3 rendition is left stock; assert
# no trace of it comes back on an upstream merge / conflict fix.
assert not any(getattr(f, 'key', None) == 'stereo_downmix' for f in Scraper.config_schema), \
    'the removed stereo_downmix toggle is back'
for _mod in (dai, device, install, dtv_aac_ads, dtv_aac_gain):
    assert 'stereo_downmix' not in inspect.getsource(_mod), f'the removed stereo downmix is back in {_mod.__name__}'

# Inserted-ad codec is HE-AAC, not AAC-LC. The ad twins the swap targets, and the bitstream
# the gain module parses, are HE-AAC; an earlier "AAC-LC" relabel was wrong. Guard the label
# (and the removed stereo-downmix framing) in the overlay's own audio modules so a merge or an
# AI conflict fix can't quietly reintroduce either into the comments the CI AI reads.
for _mod in (dtv_aac_ads, dtv_aac_gain):
    _src = inspect.getsource(_mod)
    assert 'AAC-LC' not in _src, \
        f'{_mod.__name__}: inserted-ad twins are HE-AAC, not AAC-LC (the AAC-LC relabel was a hallucination)'
    assert 'HE-AAC' in _src, f'{_mod.__name__}: the HE-AAC codec label went missing'
    assert 'stereo-downmix' not in _src.lower(), \
        f'{_mod.__name__}: stereo-downmix was removed 2026-10-03 and must not describe current behavior'

# channel/v2's streamUrls groups map to the stream (DAI) and fallback (Data Center) URLs.
v2pb = {'streamUrls': [{'groupName': 'DAI', 'URLs': ['https://x.yospace.com/csm/a.m3u8?a=1', 'https://y.yospace.com/b']},
                       {'groupName': 'Data Center', 'URLs': ['https://cdn.example/c.m3u8']}]}
assert dai.pick_stream_url(v2pb, {'e': 'prod'}) == 'https://x.yospace.com/csm/a.m3u8?a=1&yospace.pool=livepause&e=prod'
assert dai.pick_stream_url(v2pb, None) == 'https://cdn.example/c.m3u8'
assert dai.enabled({'use_dai': 'true'}) is True
assert dai.enabled({'use_dai': False}) is False
assert dai.enabled(None) is False

# Consent decoding.
assert dai.gpp_targeted_ad_opt_out(us_national_gpp(1)) is True
assert dai.gpp_targeted_ad_opt_out(us_national_gpp(2)) is False
assert dai.gpp_targeted_ad_opt_out('') is None

# Ad flags come only from the account's own values.
config = {
    'use_dai': 'true',
    'dai_partner_profile_id': 'hh-1', 'dai_profile_id': 'prof-1', 'dai_dma_id': '999',
    'dai_gpp': us_national_gpp(1), 'dai_gpp_sid': '7', 'dai_zip': '10001',
}
query = dai.build_query(config, {'123': 'testnet'}, '123')
expected = {'hhid': 'hh-1', 'u': 'hh-1', 'dma_location': '999',
            'dma_billing': '999', 'is_lat': '1', 'gpp_sid': '7', 'bZipCode': '10001',
            '_fw_did': 'google_advertising_id:optout', 'adid': 'optout', 'net': 'testnet'}
for key, value in expected.items():
    assert query.get(key) == value, f'{key}: expected {value!r}, got {query.get(key)!r}'
# profid is only the picked profile's partnerProfileID1; a login's profileId is another id.
assert 'profid' not in query, 'a sign-in profileId was sent as profid (only the picker sets it)'

# A viewer profile id the old overlay's profile picker stored (dtv_android_profid, still in
# the config) is what the app sends as profid, and it wins over the
# web-login dai_profile_id.
assert dai.build_query({**config, 'dtv_android_profid': 'pp1-android'}, {}, '123')['profid'] == 'pp1-android', \
    'profid must come from dtv_android_profid (the chosen viewer profile) when present'

# Consent from a different GPP section must not be decoded with the US-National layout.
other = dai.build_query({**config, 'dai_gpp_sid': '8'}, {}, '123')
assert 'is_lat' not in other, 'is_lat derived from a non-US-National GPP section'
assert 'net' not in other

# Never identify as a desktop browser: that pulled web ad inventory (other markets'
# spots, band-limited audio) instead of the account's local TV ads.
assert query.get('d') == 'android_tv', "d must be DirecTV's Android TV device name (no d means no inserted ads)"
assert query.get('comscore_impl_type') == 'a' and query.get('comscore_platform') == 'android', \
    "must send the Android TV app's comScore values, never desktop (PC / b)"
assert 'us_privacy' not in query, 'the Android TV app omits us_privacy when it is null'
assert query.get('nielsen_platform') == 'plt,OTT' and query.get('nielsen_dev_group') == 'devgrp,STV', \
    'must send the Android TV Nielsen values, never desktop (plt,DSK / devgrp,DSK)'
assert query.get('m') == 'live' and query.get('yo.fr') == 'true', 'general client flags missing'
assert 'yo.lpa' not in query, 'yo.lpa must not be sent (Osprey-only flag the Android TV app omits; dropped 2026-10-03 — restore only if a live break comes back empty)'
assert 'yo.lp' not in query, 'yo.lp must not be sent (no DirecTV client — app or Osprey — sends it; dropped 2026-10-03)'
assert 'yo.po' not in query, "the web player's yo.po must not be sent"
assert query.get('yo.sl') == '3' and query.get('yo.d.cp') == 'true', \
    "the Android TV app's live Yospace params are missing"
assert 'yo.cps' not in query, \
    ("yo.cps must not be sent: it is a Content Playback Spec from the app's default/iOS "
     "config tree (not the Android TV tree, whose live params are just {yo.d.cp, yo.vm}), "
     "and our invented <min> pinned Yospace to a band-limited inserted-ad profile (muffled ~7 kHz)")
assert 'com.att.tv' in base64.b64decode(query.get('yo.vm', '')).decode(), \
    "yo.vm must carry the Android TV app's ad-macro map (APPBUNDLE com.att.tv)"
assert query.get('attnid') == 'dfw003' and query.get('p') == 'dfw', 'DirecTV app constants missing'
assert query.get('metr') == '1071', "metr must be DirecTV's TV device-class code"
assert 'comscore_device' not in query, 'comscore_device only comes from a real bridge device'

# A bridge device's own values take over from the account consent. We send the real
# platform advertising id the app sends, captured per device: google_advertising_id:<id>
# + adid with is_lat=0. A deleted/limited id gives the app's optout form. android_id is
# the absolute last resort when no advertising id can be read. comscore_device as the app
# builds it, for the device upstream's fixed app User-Agent presents (so every request in
# the session names one device); it never carries whitespace.
assert device.presented_device() == {'manufacturer': 'Google', 'model': 'Chromecast'}, \
    f"upstream's app User-Agent no longer names a device comscore_device can match: {up('app_user_agent')!r}"
_SHOWN = 'Android_Google_Chromecast'
fire = {'manufacturer': 'Amazon', 'model': 'AFTKRT', 'board': 'karat', 'release': '11',
        'limit_ad_tracking': '0', 'android_id': 'abcdef0123456789'}
_cap = {'advertising_id': 'bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f', 'optout': False}
assert dai.device_ad_flags(fire, _cap) == {'comscore_device': _SHOWN, 'is_lat': '0',
    '_fw_did': 'google_advertising_id:bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f',
    'adid': 'bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f'}
assert dai.device_ad_flags(fire, {'advertising_id': '', 'optout': True})['_fw_did'] == 'google_advertising_id:optout'
assert dai.device_ad_flags(fire, {'advertising_id': '00000000-0000-0000-0000-000000000000', 'optout': False}
    )['_fw_did'] == 'google_advertising_id:optout', 'an all-zero (deleted) id is the optout form, never sent as an id'
# No captured advertising id -> android_id last resort; limited tracking -> optout form.
assert dai.device_ad_flags(fire) == {'comscore_device': _SHOWN, 'is_lat': '0',
    '_fw_did': 'android_id:abcdef0123456789'}
assert dai.device_ad_flags({**fire, 'limit_ad_tracking': '1'})['_fw_did'] == 'google_advertising_id:optout'
shield = {'manufacturer': 'NVIDIA', 'model': 'SHIELD Android TV', 'android_id': 'abcdef0123456789'}
assert dai.device_ad_flags(shield) == {'comscore_device': _SHOWN, 'is_lat': '0',
    '_fw_did': 'android_id:abcdef0123456789'}
# If the presented device can't be read (upstream's UA changed to a board we don't map),
# comscore_device falls back to the bridge device's own values, whitespace removed.
_real_presented = device.presented_device
device.presented_device = lambda: None
assert dai.device_ad_flags(fire)['comscore_device'] == 'Android_Amazon_AFTKRT'
assert dai.device_ad_flags(shield)['comscore_device'] == 'Android_NVIDIA_SHIELDAndroidTV'
device.presented_device = _real_presented
assert dai.device_ad_flags({}) == {}
# Advertising-id capture, with adb mocked: Fire reads the id silently from settings;
# GMS (Ads activity present, box idle) reads it off the Ads screen; a playing box is
# skipped (the _PLAYING sentinel, never interrupted); neither yields -> None, so
# device_ad_flags keeps android_id. A deleted id captures as the optout form.
_real_adb = dai._adb_shell
_real_is_playing = dai._is_playing
dai._is_playing = lambda a: False
dai._adb_shell = lambda addr, cmd, timeout=20: 'A=BB650B6A-5432-4DD2-9C0D-C0E4D9F7127F\nL=0\n' if 'advertising_id' in cmd else ''
assert dai._read_fire_advertising_id('x') == {
    'advertising_id': 'bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f', 'optout': False, 'source': 'fire'}, \
    'a Fire id is normalized to lowercase'
dai._adb_shell = lambda addr, cmd, timeout=20: 'A=null\nL=1\n' if 'advertising_id' in cmd else ''
assert dai._read_fire_advertising_id('x') is None, 'a null settings id is not a Fire read'


def _gms_adb(addr, cmd, timeout=20):
    if 'advertising_id' in cmd:
        return 'A=null\nL=null\n'            # no settings mirror -> not Fire
    if 'query-activities' in cmd:
        return 'com.google.android.gms/.adid.settings.AdsSettingsActivity'
    return 'STATE=active\nGAID=bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f\n'


dai._adb_shell = _gms_adb
assert dai._capture_one('x') == {
    'advertising_id': 'bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f', 'optout': False, 'source': 'gms'}
dai._is_playing = lambda a: True   # a playing box is skipped, never interrupted
assert dai._capture_one('x') is dai._PLAYING, 'a playing box must be skipped, not captured'
dai._is_playing = lambda a: False
dai._adb_shell = lambda addr, cmd, timeout=20: ('A=null\nL=null\n' if 'advertising_id' in cmd
    else 'AdsSettingsActivity' if 'query-activities' in cmd else 'STATE=deleted\nGAID=\n')
assert dai._capture_one('x') == {'advertising_id': '', 'optout': True, 'source': 'gms'}
dai._adb_shell = lambda addr, cmd, timeout=20: ''   # nothing readable -> android_id fallback
assert dai._capture_one('x') is None
dai._adb_shell = _real_adb
dai._is_playing = _real_is_playing

# The playback gate itself (real parser, adb mocked): a playing box is detected (a PLAYING
# media session or any started audio player), an idle/screensaver box is not, and an
# unreadable box fails safe to "playing" so a stream is never risked.
_real_adb2 = dai._adb_shell
dai._adb_shell = lambda a, c, timeout=12: 'mWakefulness=Awake\n---\nstate=PlaybackState {state=3, position=1}\n---\n'
assert dai._is_playing('x') is True, 'a PLAYING media session means playing'
dai._adb_shell = lambda a, c, timeout=12: 'mWakefulness=Awake\n---\n(none)\n---\nAudioPlaybackConfiguration piid:9 state:started usage=USAGE_MEDIA\n'
assert dai._is_playing('x') is True, 'a started audio player means playing'
dai._adb_shell = lambda a, c, timeout=12: 'mWakefulness=Dreaming\n---\n---\n'
assert dai._is_playing('x') is False, 'the screensaver is idle'
dai._adb_shell = lambda a, c, timeout=12: 'mWakefulness=Awake\n---\nstate=PlaybackState {state=2}\n---\nAudioPlaybackConfiguration piid:9 state:idle usage=USAGE_MEDIA\n'
assert dai._is_playing('x') is False, 'awake with nothing started/playing is idle'
dai._adb_shell = lambda a, c, timeout=12: ''
assert dai._is_playing('x') is True, 'unreadable fails safe to playing (never interrupt)'
dai._adb_shell = _real_adb2
real_client_device = device._client_device
device._client_device = lambda: fire
dev = dai.build_query(config, {}, '123')
assert dev['is_lat'] == '0' and 'adid' not in dev and dev['comscore_device'] == _SHOWN, dev
_real_bridge = device._bridge_address
device._bridge_address = lambda ip: '10.0.0.9:5555' if ip == '10.0.0.9' else None
with app.test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.9'}):
    # The fixed app string goes out for a bridge box even before its identity is read.
    device._client_device = lambda: {}
    assert device.player_user_agent() == up('app_user_agent'), 'a bridge device must send upstream\'s app User-Agent'
    device._client_device = lambda: fire
with app.test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.99'}):
    assert device.player_user_agent() is None, 'a non-bridge requester must keep upstream\'s own User-Agent'
device._bridge_address = _real_bridge
dev_url = dai.pick_stream_url({'streamURL': 'https://x.yospace.com/a.m3u8?yo.up=u'}, dev)
assert dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True)
device._client_device = lambda: {**fire, 'android_id': 'ffffffffffffffff'}
assert not dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True), 'another device reused a session'
# A non-bridge requester (no bridge device -> fw_did is None) must not reuse a session
# pinned to a real advertising id; the shared optout session stays reusable.
device._client_device = lambda: {}
_gaid_url = dai.pick_stream_url({'streamURL': 'https://x.yospace.com/a.m3u8'},
    {'_fw_did': 'google_advertising_id:bb650b6a-5432-4dd2-9c0d-c0e4d9f7127f'})
assert not dai.cached_url_usable({'fallback_url': _gaid_url, 'dai': True}, True), \
    "a non-bridge requester must not reuse another device's real-GAID session"
_optout_url = dai.pick_stream_url({'streamURL': 'https://x.yospace.com/a.m3u8'},
    {'_fw_did': 'google_advertising_id:optout'})
assert dai.cached_url_usable({'fallback_url': _optout_url, 'dai': True}, True), \
    'the shared optout session is reusable by a non-bridge requester'
device._client_device = real_client_device
assert 'advertising_id' not in dict(device._DEVICE_PROPS), \
    'the hourly identity re-read never reads an advertising id; capture lives in directv_dai'

# Sessions always use a device's saved values (a stable identity). A new device is
# read right away; known devices are re-checked in the background and the saved
# values change only when the device really changed. A failed re-check changes nothing.
from flask import Flask  # noqa: E402
class _SyncThread:  # run background re-checks inline so the test is deterministic
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)
_tmp = tempfile.mkdtemp()
_orig = (device._bridge_address, device._read_device, device._devices_file)
_orig_thread = device.threading.Thread
device.threading.Thread = _SyncThread
device._bridge_address = lambda ip: '10.0.0.9:5555'
device._devices_file = lambda: os.path.join(_tmp, 'devices.json')
reset = {**fire, 'android_id': 'ffffffffffffffff'}
def _saved(addr='10.0.0.9:5555'):   # the stored identity, without its re-check stamp
    return {k: v for k, v in (device._saved_devices().get(addr) or {}).items() if k != 'checked_at'}
def _age(addr='10.0.0.9:5555'):     # make the next lookup due for its (hourly) re-check
    d = device._saved_devices(); d[addr]['checked_at'] = 0; device._save_devices(d)
with Flask('t').test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.9'}):
    device._DEVICE_CACHE.clear(); device._read_device = lambda a: dict(fire)
    assert device._client_device() == fire, 'new device not read'
    assert _saved() == fire and set(device._saved_devices()) == {'10.0.0.9:5555'}, 'new device not saved'
    # A fresh stamp: a lookup (e.g. after a worker recycle) does NOT re-run adb.
    _rereads = []
    device._DEVICE_CACHE.clear(); device._read_device = lambda a: (_rereads.append(a) or dict(fire))
    device._client_device()
    assert _rereads == [], 'a device checked within the hour was re-read'
    _age(); device._DEVICE_CACHE.clear(); device._read_device = lambda a: {}
    assert device._client_device() == fire and _saved() == fire, \
        'a failed re-check changed the saved identity'
    _age(); device._DEVICE_CACHE.clear(); device._read_device = lambda a: dict(reset)
    assert device._client_device() == fire, 'the session must use the saved values, not wait for the re-check'
    assert _saved() == reset, 'a reset device (new Android ID) was not updated'
    device._DEVICE_CACHE.clear()
    assert device._client_device() == reset, 'the updated identity is not used afterwards'
    os.remove(device._devices_file()); device._DEVICE_CACHE.clear(); device._read_device = lambda a: {}
    assert device._client_device() == {}, 'device values invented with nothing saved and no read'
device.threading.Thread = _orig_thread
device._bridge_address, device._read_device, device._devices_file = _orig
device._DEVICE_CACHE.clear()

# Values that weren't sourced from the account are omitted, never invented.
bare = dai.build_query({}, {}, '123')
for key in ('hhid', 'u', 'profid', 'dma_location', 'gpp', 'is_lat', 'adid'):
    assert key not in bare, f'{key} present without a sourced value'

assert dai.request_flags({}, {}, '123') is None, 'flags built with DAI off'
assert dai.request_flags(config, None, '123')['hhid'] == 'hh-1'

# Stream URL choice, the Yospace pool, and exact-key merging.
yo = 'https://x.yospace.com/csm/extlive/a/index.m3u8?yo.up=u&cdncpDevice=ABCDEF'
plain = 'https://cdn.example/index.m3u8'
pb = {'streamURL': yo, 'fallbackStreamUrl': plain}
assert dai.pick_stream_url(pb, None) == plain, 'DAI off must use the fallback stream'
url = dai.pick_stream_url(pb, {'e': 'prod', 'yo.up': 'other', 'hhid': 'a b', 'x': None})
assert url.startswith(yo + '&yospace.pool=livepause'), url
assert '&e=prod' in url, 'e=prod dropped by a substring match against cdncpDevice='
assert url.count('yo.up=') == 1, 'an existing param was duplicated'
assert '&hhid=a%20b' in url and '&x=' not in url
url2 = dai.pick_stream_url(pb, {'_fw_did': 'android_id:abc', 'nielsen_dev_group': 'devgrp,STV'})
assert '&_fw_did=android_id:abc' in url2 and '&nielsen_dev_group=devgrp,STV' in url2, \
    "':' and ',' must stay literal like DirecTV's clients send them"
assert dai.pick_stream_url({'fallbackStreamUrl': plain}, {'e': 'prod'}) == plain

# Cached URLs are reused only under the same toggle setting.
assert dai.cached_url_usable({'fallback_url': url, 'dai': True}, True)
assert not dai.cached_url_usable({'fallback_url': url, 'dai': True}, False)
assert not dai.cached_url_usable({'fallback_url': yo, 'dai': True}, True), 'pool-less Yospace URL reused'
assert not dai.cached_url_usable(None, False)

# Login helpers.
claims = b64url(json.dumps({'partnerProfileId': 'pp-9', 'profileId': 'p-9'}).encode())
assert dai.ids_from_bearer_jwt(f'h.{claims}.s') == ('pp-9', 'p-9')
assert dai.ids_from_bearer_jwt('not-a-jwt') == (None, None)
cfg = {}
dai.store_login_result(cfg, {'bearer_token': f'h.{claims}.s', 'dai_context': {'dma_id': '501'}})
assert cfg['dai_partner_profile_id'] == 'pp-9' and cfg['dai_dma_id'] == '501'
assert not {'dai_adid', 'dai_fw_did', 'dai_comscore_device'} & set(cfg), 'random device ids minted'
# A code sign-in session (opaque tokens) gets its DMA/consent fetched with its bearer, in
# the background: nothing slow runs inside upstream's read-modify-commit of the config.
_spawned_fetch = []
_real_spawn_si = device.spawn
_real_fac = dai.fetch_account_context
dai.fetch_account_context = lambda *a, **k: (_ for _ in ()).throw(AssertionError('account lookup ran inside apply_auth_result'))
device.spawn = lambda fn, *a: _spawned_fetch.append((fn.__name__, a))
try:
    cfg = {'use_dai': 'true'}
    dai.store_login_result(cfg, {'bearer_token': 'opaque', 'auth_method': 'device_code', 'partner_profile_id': 'pp-1'})
    assert _spawned_fetch == [('_fetch_account_values_after_signin', ('opaque',))] and cfg['dai_partner_profile_id'] == 'pp-1', (_spawned_fetch, cfg)
    # With DAI off, a sign-in makes no extra DirecTV requests (turning DAI on fetches them).
    _spawned_fetch.clear(); cfg = {}
    dai.store_login_result(cfg, {'bearer_token': 'opaque', 'auth_method': 'device_code', 'partner_profile_id': 'pp-1'})
    assert _spawned_fetch == [] and 'dai_dma_id' not in cfg and cfg['dai_partner_profile_id'] == 'pp-1', cfg
    # A sign-in to a different account (same id source) drops the previous account's
    # values and profile.
    _spawned_fetch.clear()
    cfg = {'dai_account': 'pp-1', 'dai_account_source': 'grant', 'dai_zip': '10965', 'dai_dma_id': '501',
           'dtv_android_profid': 'x', 'dtv_android_profile_id': 'p1'}
    dai.store_login_result(cfg, {'bearer_token': 'opaque', 'auth_method': 'device_code', 'partner_profile_id': 'pp-OTHER'})
    assert cfg['dai_account'] == 'pp-OTHER' and not {'dai_zip', 'dai_dma_id', 'dtv_android_profid'} & set(cfg), cfg
    cfg = {'dai_account': 'pp-1', 'dai_account_source': 'grant', 'dai_zip': '10965'}
    dai.store_login_result(cfg, {'bearer_token': 'opaque', 'partner_profile_id': 'pp-1'})
    assert cfg['dai_zip'] == '10965', 'the same account lost its values on a refresh'
    # Different id sources (code grant vs a web token's claims) aren't known to be one
    # account: market/ZIP are refetched, the chosen profile is KEPT and checked in the
    # background against the account's own profile list.
    _spawned_fetch.clear()
    _wclaims = b64url(json.dumps({'partnerProfileId': 'pp-WEB'}).encode())
    cfg = {'use_dai': 'true', 'dai_account': 'pp-1', 'dai_account_source': 'grant', 'dai_zip': '10965',
           'dai_dma_id': '501', 'dtv_android_profid': 'x', 'dtv_android_profile_id': 'p1'}
    dai.store_login_result(cfg, {'bearer_token': f'h.{_wclaims}.s', 'auth_method': 'curl_cffi'})
    assert cfg['dtv_android_profid'] == 'x' and cfg['dtv_android_profile_id'] == 'p1', \
        'a code/web id mismatch wiped the chosen profile on a guess'
    assert 'dai_zip' not in cfg and cfg['dai_account_source'] == 'jwt', cfg
    _names = [n for n, _ in _spawned_fetch]
    assert '_check_profile_after_signin' in _names and '_fetch_account_values_after_signin' in _names, _names
    # The check keeps a profile the account has, clears one it doesn't, keeps on an unreadable list.
    _real_lp, _real_sav2 = dai.list_profiles, dai._save_account_values
    _cleared = []
    dai._save_account_values = lambda b, u, wait=0: (_cleared.append(u) or True)
    try:
        dai.list_profiles = lambda b: {'profiles': [{'id': 'p1'}]}
        dai._check_profile_after_signin('b', 'p1'); assert _cleared == []
        dai.list_profiles = lambda b: {}
        dai._check_profile_after_signin('b', 'p1'); assert _cleared == [], 'an unreadable profile list cleared the profile'
        dai.list_profiles = lambda b: {'profiles': [{'id': 'p2'}]}
        dai._check_profile_after_signin('b', 'p1')
        assert _cleared == [{'dtv_android_profid': None, 'dtv_android_profile_id': None}], _cleared
    finally:
        dai.list_profiles, dai._save_account_values = _real_lp, _real_sav2
finally:
    dai.fetch_account_context = _real_fac
    device.spawn = _real_spawn_si


class _Scraper:
    def __init__(self):
        self.cache = {}
    def _update_cache(self, key, value):
        self.cache[key] = value
sc = _Scraper()
dai.note_channels(sc, [{'ccid': '1', 'daiChannelName': ' cnn '}, {'ccid': '2', 'daiChannelName': ''}, 'junk'])
assert sc.cache == {'dai_channel_names': {'1': 'cnn'}}, sc.cache

# The account lookup (market, ZIP, consent) is sent as the same app client as the session:
# upstream's app User-Agent, no web player Origin/Referer.
class _HdrSess:
    def __init__(self):
        self.seen = []
    def get(self, url, params=None, headers=None, **kw):
        self.seen.append(dict(headers or {}))
        return _types.SimpleNamespace(ok=False)
_hs = _HdrSess()
dai.fetch_account_context(_hs, 'B')
assert _hs.seen and all(h.get('User-Agent') == up('app_user_agent') and 'Origin' not in h and 'Referer' not in h
                        and h.get('Authorization') == 'Bearer B' for h in _hs.seen), _hs.seen

# The viewer-profile picker: the chosen profile's partnerProfileID1 becomes the Yospace
# profid (dtv_android_profid). Needed for ad targeting; must not be dropped again.
for _n in ('list_profiles', 'select_profile', 'profile_token_exchange', 'profiles_supported'):
    assert callable(getattr(dai, _n, None)), f'the viewer-profile picker lost {_n}'
assert dai._partner_profile_id1({'valuePairs': {'partnerProfileID1': ' pp1 '}}) == 'pp1'
assert dai._partner_profile_id1({'partnerProfileId1': 'pp2'}) == 'pp2'
assert dai._partner_profile_id1({}) == ''
assert dai.profiles_supported({'auth_method': 'device_code', 'refresh_token': 'r'})
assert not dai.profiles_supported({'auth_method': 'curl_cffi', 'refresh_token': 'r'})
assert any(r.rule == '/api/sources/<int:source_id>/directv-profile' for r in app.url_map.iter_rules()),     'the directv-profile route is not registered'
assert '/directv-profile' in install._ADMIN_JS and 'Run as DirecTV profile' in install._ADMIN_JS,     'the profile picker is missing from the DirecTV settings script'

# ── Behavioral wiring tests ──────────────────────────────────────────────────
# Drive upstream's own functions and routes through the runtime wiring with stubbed
# network, so the wrapping must really happen (not just exist) or the build fails.


class _Resp:
    """Minimal stand-in for a requests.Response."""
    def __init__(self, *, text='', content=b'', status=200, ctype='application/octet-stream',
                 url='', data=None):
        self.text, self.content, self.status_code, self.url = text, content, status, url
        self.headers = {'Content-Type': ctype}
        if content:
            self.headers['Content-Length'] = str(len(content))
        self._data = data

    def json(self):
        return self._data

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass


class _Session:
    """Records the channel authorization request upstream (or the DAI wiring) makes."""
    calls: list = []
    reply = None

    def __init__(self):
        self.headers, self.cookies = {}, _types.SimpleNamespace(set=lambda *a, **k: None)

    def get(self, url, params=None, **kw):
        _Session.calls.append((url, dict(params or {}), dict(self.headers)))
        return _Session.reply


_v2_ok = {'authorized': True, 'dRights': {'playToken': 'PT'},
          'playbackData': {'streamUrls': [
              {'groupName': 'DAI', 'URLs': ['https://csm-e.tls1.yospace.com/csm/extlive/x/master.m3u8?yo.up=u']},
              {'groupName': 'Data Center', 'URLs': ['https://dfwlive-c.directv.fastly-edge.com/master.m3u8']}]}}
_v1_ok = {'authorized': True, 'dRights': {'playToken': 'PT1'},
          'playbackData': {'fallbackStreamUrl': 'https://dfwlive-c.directv.fastly-edge.com/v1.m3u8',
                           'streamURL': 'https://csm-e.tls1.yospace.com/v1'}}


def _call_fetch():
    """Upstream's channel fetch, called with whichever of its arguments it still takes."""
    params = inspect.signature(up('channel_fetch').__wrapped__).parameters
    args = {'bearer_token': 'B', 'cookies': [], 'client_context': None, 'ccid': '123'}
    return up('channel_fetch')(**{k: v for k, v in args.items() if k in params})


sc = Scraper({'use_dai': 'true', 'bearer_token': 'B', 'dai_dma_id': '501', 'dtv_android_profid': 'pp-test'})
sc._cache = {}
_real_session = directv.requests.Session
directv.requests.Session = _Session
try:
    with app.test_request_context(environ_base={'REMOTE_ADDR': '203.0.113.9'}):
        # (a) DAI on: resolve() makes the Android TV app's channel/v2 request and plays the
        # Yospace session with the DAI flags; the cached entry is tagged dai=True.
        _Session.calls.clear(); _Session.reply = _Resp(data=_v2_ok)
        url = sc.resolve('directv://123/res')
        req_url, req_params, req_headers = _Session.calls[-1]
        assert req_url.endswith('/channel/v2'), req_url
        assert req_params.get('startOver') == 'false' and 'timeShiftEnabled' not in req_params, req_params
        assert 'Origin' not in req_headers and 'Referer' not in req_headers, 'DAI request kept the web Origin/Referer'
        q = parse_qs(urlsplit(url).query)
        assert 'yospace.com' in url and q.get('yospace.pool') == ['livepause'] and q.get('d') == ['android_tv'], url
        assert q.get('dma_location') == ['501'], url
        assert sc.cache['directv_playback']['123']['dai'] is True
        assert req_headers.get('Authorization') == 'Bearer B', \
            "the DAI request lost the bearer (did upstream rename _fetch_channel_playback's arguments?)"
        assert (install.health().get('ok') or {}).get('count'), 'a good DAI tune was not recorded'
        _dai_entry = dict(sc.cache['directv_playback']['123'])
        # (b) A cached entry from the other toggle state is not reused: DAI off refetches on
        # upstream's own v1 request and gets the non-DAI stream.
        _Session.calls.clear(); _Session.reply = _Resp(data=_v1_ok)
        sc.config['use_dai'] = 'false'
        url = sc.resolve('directv://123/res')
        assert _Session.calls and _Session.calls[-1][0].endswith('/channel/v1'), 'DAI off must use upstream untouched'
        assert url.endswith('/v1.m3u8'), url
        # The DAI entry has exactly upstream's playback keys (plus 'dai'), so upstream's
        # cache and license code read it like its own.
        assert set(_dai_entry) - {'dai', 'dai_targeting'} == set(sc.cache['directv_playback']['123']), \
            f"the DAI playback entry's keys differ from upstream's: {sorted(_dai_entry)} vs {sorted(sc.cache['directv_playback']['123'])}"
        # (c) An expired token on the DAI request raises upstream's own error, so upstream's
        # re-auth runs exactly as on its own request.
        _Session.reply = _Resp(data={'authorized': False, 'responseStatus': {'errorCode': '0015'}})
        tok = install._TUNE.set(({'use_dai': 'true'}, None))
        try:
            _call_fetch()
            raise AssertionError('an expired DAI authorization did not raise DirectvAuthExpiredError')
        except up('auth_expired_error'):
            pass
        finally:
            install._TUNE.reset(tok)
        # (d) Any other DAI failure falls back to upstream's request (DAI degrades to off).
        _Session.calls.clear(); _Session.reply = _Resp(status=500)
        tok = install._TUNE.set(({'use_dai': 'true'}, None))
        try:
            assert _call_fetch() is None
            assert [c[0].rsplit('/', 1)[-1] for c in _Session.calls] == ['v2', 'v1'], _Session.calls
        finally:
            install._TUNE.reset(tok)
        # (e) DAI on, but upstream's resolve no longer goes through the fetch we wrap: the
        # runtime check reports it (the DAI panel shows it in red); a good DAI tune clears it.
        _wrapped_fetch = up('channel_fetch')
        install.up_set('channel_fetch', _wrapped_fetch.__wrapped__)
        try:
            sc.config['use_dai'] = 'true'
            sc._cache = {}
            _Session.reply = _Resp(data=_v1_ok)
            sc.resolve('directv://123/res')
            assert any('never reached the DAI request' in w for w in install.runtime_warnings()), install.runtime_warnings()
        finally:
            install.up_set('channel_fetch', _wrapped_fetch)
        sc._cache = {}
        _Session.reply = _Resp(data=_v2_ok)
        sc.resolve('directv://123/res')
        assert install.runtime_warnings() == [], install.runtime_warnings()
        # (e2) A tune touches only its own channel's cache entry: another channel's entry
        # (say, another box's session) is neither dropped nor saved by a cache hit here.
        sc.cache['directv_playback']['999'] = {'fallback_url': 'https://other/x.m3u8', 'dai': False, 'cached_at': _t.time()}
        sc._pending_cache_updates.clear()
        sc.resolve('directv://123/res')
        assert '999' in sc.cache['directv_playback'], "a tune dropped another channel's cached session"
        assert 'directv_playback' not in sc._pending_cache_updates, 'a cache hit saved a cache rewrite'
        # (e3) Two boxes on one channel each keep their own DAI session (each has its own
        # device id in the URL): once both have tuned, re-tuning either reuses its own (no
        # new authorization), and each box's license request carries its own play token.
        _real_cba, _real_cdf = dai._current_bridge_address, dai._current_device_ad_flags
        _box = {'addr': ''}
        dai._current_bridge_address = lambda: _box['addr']
        dai._current_device_ad_flags = lambda: {
            'is_lat': '0', '_fw_did': 'android_id:' + _box['addr'].replace('.', '').replace(':', '').ljust(16, '0')}
        try:
            sc._cache = {}
            _boxes = (('10.0.0.1:5555', 'PT-A'), ('10.0.0.2:5555', 'PT-B'))
            for _addr, _tok in _boxes:
                _box['addr'] = _addr
                _Session.reply = _Resp(data={**_v2_ok, 'dRights': {'playToken': _tok}})
                sc.resolve('directv://123/res')
            _Session.calls.clear()
            for _addr, _tok in _boxes:
                _box['addr'] = _addr
                _u = sc.resolve('directv://123/res')
                assert dai._current_device_ad_flags()['_fw_did'] in _u, f'box {_addr} got another box\'s session'
            assert not _Session.calls, f'a box re-authorized although its own DAI session was saved: {_Session.calls}'
            _lic_cfg = {**sc.config, **sc.cache}
            for _addr, _tok in _boxes:
                _box['addr'] = _addr
                _body, _ = Scraper.prepare_license_request(b'challenge', _lic_cfg, channel_id='123')
                assert json.loads(_body)['authorizationToken'] == _tok, f'box {_addr} licensed with another session\'s token'
        finally:
            dai._current_bridge_address, dai._current_device_ad_flags = _real_cba, _real_cdf
finally:
    directv.requests.Session = _real_session

# Sign-in: every result through apply_auth_result gets the DAI account values; the grant's
# household id survives upstream's _result; an old-overlay session refreshes via upstream.
cfg = {}
up('apply_auth_result')(cfg, {'bearer_token': 'b', 'captured_at': 1.0, 'auth_method': 'curl_cffi',
                                'dai_context': {'dma_id': '501'}, 'partner_profile_id': 'pp-2'})
assert cfg['bearer_token'] == 'b' and cfg['dai_dma_id'] == '501' and cfg['dai_partner_profile_id'] == 'pp-2', cfg
_r = up('code_signin_result')({'access_token': 'a', 'valuePairs': {'partnerProfileId': 'pp-3', 'activationToken': ''}})
assert _r['partner_profile_id'] == 'pp-3' and _r['auth_method'] == 'device_code', _r

# The relay, through the real app's routes. The network is stubbed at the requests
# transport (HTTPAdapter.send), below every hook, so upstream's relay code and ours both
# run for real; the stub records the headers each fetch went out with.
import io as _io  # noqa: E402
from requests.adapters import HTTPAdapter as _HTTPAdapter  # noqa: E402
from requests.structures import CaseInsensitiveDict as _CID  # noqa: E402

_client = app.test_client()
_ASSET = '/play/directv/browser-asset?url='
_wire = {}      # url (no query) -> (status, content-type, body)
_wire_log = []  # (url, headers) of every fetch that reached the transport


def _fake_send(self, req, **kw):
    _wire_log.append((req.url, dict(req.headers)))
    status, ctype, body = _wire.get(req.url.split('?', 1)[0], (404, 'text/plain', b'not stubbed'))
    r = install.requests.Response()
    r.status_code, r.url, r.request, r.encoding = status, req.url, req, 'utf-8'
    r.headers = _CID({'Content-Type': ctype, 'Content-Length': str(len(body))})
    r.raw = _io.BytesIO(body)
    return r


def _ua_of(url):
    return next((h.get('User-Agent') for u, h in reversed(_wire_log) if u.split('?', 1)[0] == url), None)


_real_send = _HTTPAdapter.send
_real_client_device, _real_dai_on = device._client_device, dai.dai_on
_HTTPAdapter.send = _fake_send
device._client_device = lambda: {}
dai.dai_on = lambda: True
# "A bridge device" below = the stubbed _client_device returns its values.
_real_bridge3 = device._bridge_address
device._bridge_address = lambda ip: '10.0.0.9:5555' if device._client_device() else None
try:
    # (f) An inserted-ad AC-3 audio playlist is swapped to the AAC twin (segment AND its
    # EXT-X-MAP init) on the way out of upstream's relay; live content is left alone.
    _pl_url = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/audio.m3u8'
    _wire[_pl_url] = (200, 'application/vnd.apple.mpegurl', _aac_pl.encode())
    _body = _client.get(_ASSET + _quote(_pl_url, safe='')).get_data(as_text=True)
    assert 'u-6600-a-96-1-0.mp4' in _body and 'u-6600-a-96-1-i.mp4' in _body, \
        'the relay did not swap the inserted AC-3 ad (segment + its EXT-X-MAP init) to the AAC twin'
    assert 'u-6600-c-384' not in _body, 'an AC-3 ad rendition survived the relay swap'
    assert 'dfwlive-v2-c0p7-ms' in _body, 'the relay dropped the live (non-ad) content line'

    # (g) A master playlist passes through: the stock 5.1 CHANNELS="6" survives (the
    # downmix was removed) and nothing is turned into an AAC ad.
    _master = ('#EXTM3U\n'
               '#EXT-X-STREAM-INF:BANDWIDTH=6500000,CODECS="avc1.640028,ac-3",AUDIO="aud"\n'
               'https://yospace01-directv.akamaized.net/dtv-prd/x/variant-1080.m3u8\n'
               '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",CHANNELS="6",'
               'URI="https://yospace01-directv.akamaized.net/dtv-prd/x/audio.m3u8"\n')
    _m_url = 'https://yospace01-directv.akamaized.net/dtv-prd/x/master.m3u8'
    _wire[_m_url] = (200, 'application/vnd.apple.mpegurl', _master.encode())
    _mbody = _client.get(_ASSET + _quote(_m_url, safe='')).get_data(as_text=True)
    assert 'CHANNELS="6"' in _mbody and 'a-96' not in _mbody and 'BANDWIDTH=6500000' in _mbody, _mbody

    # (h) The ad segment URL exactly as upstream's relay rewrote it in (f) is served with
    # the loudness cut, once (not twice, despite two create_app calls); a Range request
    # bypasses the cut and gets the original bytes from upstream's view.
    _seg_url = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-a-96-1-0.mp4'
    _raw = b'\xff\xf1raw-aac-ad-bytes-stand-in'
    _wire[_seg_url] = (200, 'video/mp4', _raw)
    _relayed = next((ln.strip() for ln in _body.splitlines() if 'u-6600-a-96-1-0.mp4' in ln), '')
    assert _relayed.startswith('/'), f'upstream no longer relays the ad segment (it would skip the loudness cut): {_relayed}'
    _seen, _SENT = [], b'ATTENUATED-SENTINEL'
    _real_att = dtv_aac_gain.attenuate_ad_segment
    dtv_aac_gain.attenuate_ad_segment = lambda data, *a, **k: (_seen.append(data) or _SENT)
    try:
        _sr = _client.get(_relayed)
        assert _sr.get_data() == _SENT, 'the attenuated bytes did not reach the client'
        assert _seen == [_raw], f'attenuate_ad_segment must run exactly once on the segment bytes: {_seen}'
        _seen.clear()
        _rr = _client.get(_relayed, headers={'Range': 'bytes=0-15'})
        assert _seen == [], 'attenuation must be skipped on a Range request'
        assert _rr.get_data() == _raw, 'a Range fetch must stream the original bytes unchanged'
        # (i) An ad-shaped URL on a host outside upstream's DirecTV CDN allowlist is never
        # fetched by us (the relay is not an open proxy); upstream's view refuses it.
        _ad_other = 'https://unknown-ad-cdn.example.net/dtv-prd/7/u-6600-a-96-1-0.mp4'
        _wire_log.clear()
        assert _client.get(_ASSET + _quote(_ad_other, safe='')).status_code == 400
        assert _seen == [] and not _wire_log, 'an ad-shaped URL on an unknown host was fetched'
        # An inserted ad the relay doesn't cover (its host isn't on upstream's allowlist)
        # would play without the loudness cut: the DAI panel says so, until a cut ad clears it.
        _other_pl = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/other.m3u8'
        _wire[_other_pl] = (200, 'application/vnd.apple.mpegurl',
                            _aac_pl.replace('yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-',
                                            'unknown-ad-cdn.example.net/x/u-').encode())
        _client.get(_ASSET + _quote(_other_pl, safe=''))
        assert any('not relayed' in w for w in install.runtime_warnings()), install.runtime_warnings()
        _client.get(_relayed)
        assert install.runtime_warnings() == [], install.runtime_warnings()
    finally:
        dtv_aac_gain.attenuate_ad_segment = _real_att
    assert dtv_aac_gain.is_ad_segment(_seg_url) and not dtv_aac_gain.is_ad_segment(
        'https://dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4'), \
        'is_ad_segment must match only the inserted-ad AAC twin, never live content'

    # (j) From a bridge device with DAI on, the relay's fetches to DirecTV hosts carry the
    # app User-Agent; with DAI off (or from anything else) upstream's own goes out.
    device._client_device = lambda: fire
    _client.get(_ASSET + _quote(_m_url, safe=''))
    assert _ua_of(_m_url) == up('app_user_agent'), _wire_log[-1:]
    dai.dai_on = lambda: False
    _client.get(_ASSET + _quote(_m_url, safe=''))
    _upstream_ua = getattr(directv_proxy, '_BROWSER_UA', None)
    assert (_ua_of(_m_url) == _upstream_ua) if _upstream_ua else not (_ua_of(_m_url) or '').startswith('APP_PROJECT_NAME/'), \
        'DAI off must send upstream\'s User-Agent untouched'
    dai.dai_on = lambda: True
    with app.test_request_context('/admin/sources'):
        install.requests.get('https://yospace01-directv.akamaized.net/x.m3u8', timeout=1)
    assert not (_ua_of('https://yospace01-directv.akamaized.net/x.m3u8') or '').startswith('APP_PROJECT_NAME/'), \
        'the app User-Agent leaked outside the relay'
    with app.test_request_context(_ASSET + 'x'):
        install.requests.get('http://prismcast.local/x', timeout=1)
    assert not (_ua_of('http://prismcast.local/x') or '').startswith('APP_PROJECT_NAME/'), \
        'the app User-Agent leaked onto a non-DirecTV host'
finally:
    _HTTPAdapter.send = _real_send
    device._client_device, dai.dai_on = _real_client_device, _real_dai_on
    device._bridge_address = _real_bridge3

# (k) The panel script is served, our page renders, and the script is added to upstream
# pages with source settings boxes (only those).
assert _client.get('/directv-dai/admin.js').status_code == 200
assert b'source-config-' in _client.get('/directv-dai/admin.js').get_data()
assert b'/directv-dai/admin.js' in _client.get('/directv-dai').get_data()
with app.test_request_context('/admin/sources'):
    from flask import Response as _Response  # noqa: E402
    _page = _Response('<html><div class="source-config hidden" id="source-config-3"></div></body></html>', mimetype='text/html')
    for fn in app.after_request_funcs.get(None, []):
        _page = fn(_page)
    assert b'/directv-dai/admin.js' in _page.get_data(), 'the DAI panel script was not added to the sources page'
    _other = _Response('<html></body></html>', mimetype='text/html')
    for fn in app.after_request_funcs.get(None, []):
        _other = fn(_other)
    assert b'directv-dai' not in _other.get_data(), 'the DAI script was injected into an unrelated page'

# (l) End to end on a real (throwaway) database: an old overlay session is re-tagged as
# upstream's code sign-in; our panel route reads and saves the toggle (cache clear + ad-id
# capture); upstream's own settings save leaves use_dai alone; the profile route answers;
# the real sources page gets the script; and nothing is reported broken.
db, Source = up('db'), up('source_model')
_real_list, _real_capture = dai.list_profiles, dai.capture_in_background
_captures = []
dai.list_profiles = lambda bearer: {'profiles': [{'id': 'p1', 'name': 'David', 'primary': True}], 'current_id': 'p1'}
dai.capture_in_background = lambda addrs=None: _captures.append(addrs) or 0
# Capture paths read a FRESH device list (a box added a moment ago is included); display
# reads the cached one.
_real_kbd2, _real_cbd2 = device.known_bridge_devices, device._cached_bridge_devices
device.known_bridge_devices = lambda: [{'address': '10.0.0.40:5555', 'host': '10.0.0.40'}]
device._cached_bridge_devices = lambda: []
try:
    with app.app_context():
        assert '10.0.0.40:5555' in dai.uncaptured_addresses(fresh=True), 'a capture missed a just-added box'
        assert dai.uncaptured_addresses() == [], 'the display path called ah4c'
finally:
    device.known_bridge_devices, device._cached_bridge_devices = _real_kbd2, _real_cbd2
try:
    with app.app_context():
        db.create_all()
        _src = Source(name='directv', display_name='DirecTV Stream',
                      config={'auth_method': 'dtv_android', 'refresh_token': 'r', 'bearer_token': 'b',
                              'dai_dma_id': '501', 'dtv_android_profid': 'pp-test'})
        db.session.add(_src)
        db.session.commit()
        _sid = _src.id
        install.migrate_old_sessions()
        assert db.session.get(Source, _sid).config['auth_method'] == up('code_signin_method'), \
            "an old overlay session was not re-tagged as upstream's code sign-in"
        assert up('is_code_signin')(db.session.get(Source, _sid).config)
    # The overlay's one read of upstream's bridge-device list returns a registered box.
    with app.app_context():
        from app import models as _models  # noqa: E402
        if hasattr(_models, 'BridgeDevice'):
            db.session.add(_models.BridgeDevice(address='10.0.0.9:5555', added_manually=True))
            db.session.commit()
        _devs = device.known_bridge_devices()
        assert all(d.get('address') and d.get('host') for d in _devs), _devs
        if hasattr(_models, 'BridgeDevice'):
            assert any(d['address'] == '10.0.0.9:5555' and d['host'] == '10.0.0.9' for d in _devs), \
                f'known_bridge_devices() misses a registered bridge device: {_devs}'
    _pg = _client.get(f'/api/sources/{_sid}/directv-dai')
    assert _pg.status_code == 200 and _pg.get_json()['code_signed_in'] is True and _pg.get_json()['use_dai'] is False, _pg.get_data()
    assert _client.post(f'/api/sources/{_sid}/directv-dai', data={'use_dai': 'true'}).status_code == 415, \
        'the DAI toggle accepted a form post (another site could flip it)'
    assert _client.post(f'/api/sources/{_sid}/directv-capture-adids').status_code == 415, \
        'the ad-id capture accepted a bare post (another site could trigger it)'
    _sv = _client.post(f'/api/sources/{_sid}/directv-dai', json={'use_dai': True})
    assert _sv.status_code == 200 and _sv.get_json()['use_dai'] is True, _sv.get_data()
    with app.app_context():
        assert dai.enabled(db.session.get(Source, _sid).config), 'saving the DAI toggle did not stick'
    assert _captures, 'turning DAI on did not run the ad-id capture'
    # Upstream's own DirecTV settings save, with exactly what its page sends, keeps keys
    # it doesn't know (use_dai).
    _save = next((r for r in app.url_map.iter_rules() if r.rule.endswith('/<int:source_id>/config')
                  and 'POST' in (r.methods or ())), None)
    assert _save is not None, "upstream's source settings save route is gone"
    _save_url = _save.rule.replace('<int:source_id>', str(_sid))
    _form = _client.get(_save_url).get_json() or {}
    _payload = dict(_form.get('values') or {})
    assert _payload, f"upstream's settings API returned no values to save: {_form}"
    _up = _client.post(_save_url, json=_payload)
    assert _up.status_code < 400, _up.get_data()
    with app.app_context():
        assert dai.enabled(db.session.get(Source, _sid).config), "upstream's settings save wiped the DAI toggle"
    _pr = _client.get(f'/api/sources/{_sid}/directv-profile')
    assert _pr.status_code == 200 and _pr.get_json()['profiles'][0]['id'] == 'p1', _pr.get_data()
    _page = _client.get('/admin/sources')
    assert _page.status_code == 200, f'/admin/sources answered {_page.status_code}'
    assert b'id="source-config-' in _page.get_data(), "the sources page no longer has settings boxes (source-config-<id>)"
    assert b'/directv-dai/admin.js' in _page.get_data(), 'the sources page did not get the DAI panel script'
    # The tune that opens the Yospace session, through upstream's real browser.m3u8 route:
    # the channel/v2 request and the Yospace master fetch both go out, the master with
    # the bridge device's app User-Agent (the one A/B tested for inserted ads).
    from app.models import Channel  # noqa: E402
    with app.app_context():
        db.session.add(Channel(source_id=_sid, source_channel_id='123', name='CNN', stream_url='directv://123/res'))
        db.session.commit()
    _yo_master = 'https://csm-e.tls1.yospace.com/csm/extlive/x/master.m3u8'
    _wire['https://api.cld.dtvce.com/right/authorization/channel/v2'] = (200, 'application/json', json.dumps(_v2_ok).encode())
    _wire[_yo_master] = (200, 'application/vnd.apple.mpegurl', _master.encode())
    _HTTPAdapter.send, device._client_device = _fake_send, (lambda: fire)
    _real_bridge4, device._bridge_address = device._bridge_address, (lambda ip: '10.0.0.9:5555' if ip == '10.0.0.9' else None)
    try:
        _wire_log.clear()
        _bm = _client.get('/play/directv/123/browser.m3u8', environ_base={'REMOTE_ADDR': '10.0.0.9'})
        assert _bm.status_code == 200 and b'#EXTM3U' in _bm.get_data(), (_bm.status_code, _bm.get_data()[:200])
        assert any(u.startswith('https://api.cld.dtvce.com/right/authorization/channel/v2') for u, _ in _wire_log), _wire_log
        assert (_ua_of(_yo_master) or '').startswith('APP_PROJECT_NAME/'), \
            f'the Yospace session-opening fetch lost the app User-Agent: {_ua_of(_yo_master)}'
    finally:
        _HTTPAdapter.send, device._client_device = _real_send, _real_client_device
        device._bridge_address = _real_bridge4
    _st = _client.get('/directv-dai/status').get_json()
    assert _st == {'missing': [], 'markers': [], 'failed': [], 'runtime': []}, _st
    assert _client.post(f'/api/sources/{_sid}/directv-dai', json={'use_dai': False}).get_json()['use_dai'] is False
finally:
    dai.list_profiles, dai.capture_in_background = _real_list, _real_capture

# (m) The real Fire TV path, end to end. With DAI on, a tune of a DRM-bridge channel goes:
# ah4c -> the server's /play/fc-player/directv/<ccid>.m3u8 -> FC Player is told to open the
# SERVER's browser.m3u8 -> every playlist, segment and license request comes back through
# the server, which applies the ad swap, the loudness cut and the app User-Agent. This
# checks that whole walk: nothing in a playlist the player gets points straight at
# DirecTV, the inserted ad is swapped and cut on the way through, live segments pass
# through untouched, and every fetch to DirecTV carries the box's app User-Agent with the
# DAI flags on the Yospace session. (Upstream's own bridge route is driven when its
# names are there; if upstream reshapes it, that one step is skipped with a note, since
# it isn't the overlay's code.)
_m_master = ('#EXTM3U\n'
             '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",CHANNELS="6",URI="audio.m3u8"\n'
             '#EXT-X-STREAM-INF:BANDWIDTH=6500000,CODECS="avc1.640028,ac-3",AUDIO="aud"\n'
             'video-1080.m3u8\n')
_m_audio = ('#EXTM3U\n#EXT-X-TARGETDURATION:6\n'
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://live"\n'
            '#EXTINF:6.0,\nhttps://dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4\n'
            '#EXT-X-KEY:METHOD=NONE\n'
            '#EXT-X-MAP:URI="https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-i.mp4"\n'
            '#EXTINF:6.0,\nhttps://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-0.mp4\n')
_m_yo = 'https://csm-e.tls1.yospace.com/csm/extlive/x/'
_m_ad = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-a-96-1-0.mp4'
_m_live = 'https://dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4'
_m_raw_ad, _m_live_bytes = b'\xff\xf1raw-ad', b'LIVE-ENCRYPTED-BYTES'
_wire.update({
    'https://api.cld.dtvce.com/right/authorization/channel/v2': (200, 'application/json', json.dumps(_v2_ok).encode()),
    _m_yo + 'master.m3u8': (200, 'application/vnd.apple.mpegurl', _m_master.encode()),
    _m_yo + 'audio.m3u8': (200, 'application/vnd.apple.mpegurl', _m_audio.encode()),
    _m_ad: (200, 'video/mp4', _m_raw_ad),
    _m_live: (200, 'video/mp4', _m_live_bytes),
})
_BOX = {'REMOTE_ADDR': '10.0.0.9'}
_real_att = dtv_aac_gain.attenuate_ad_segment
_cut = []
_HTTPAdapter.send, device._client_device = _fake_send, (lambda: fire)
dtv_aac_gain.attenuate_ad_segment = lambda data, *a, **k: (_cut.append(data) or b'CUT:' + data)
try:
    with app.app_context():
        _s = db.session.get(Source, _sid)
        _s.config = {**(_s.config or {}), 'use_dai': 'true', 'dtv_android_profid': 'pp-test'}
        _ch = Channel.query.filter_by(source_id=_sid, source_channel_id='123').first()
        if hasattr(_ch, 'requires_drm_bridge'):
            _ch.requires_drm_bridge = True
        db.session.commit()
    dai._DAI_ON[0] = 0.0
    _pscu = up('persist_cache')
    with app.app_context():
        _pscu(_sid, {'directv_playback': {}, 'dai_playback_by_device': {}})

    # 1. The bridge hands FC Player the server's own browser.m3u8 (not a DirecTV URL).
    _manifest = '/play/directv/123/browser.m3u8'
    try:
        from app import fc_player_bridge as _fcb  # noqa: E402
        from app.models import AppSettings as _AS  # noqa: E402
        _given = {}
        _orig_fcb = (_fcb.trigger_channel, _fcb.hardware_bridge_active)
        _fcb.trigger_channel = lambda manifest_url, license_url=None, **kw: (_given.update(url=manifest_url, lic=license_url) or True)
        _fcb.hardware_bridge_active = lambda *a, **k: True
        _orig_enc = _AS.effective_fc_player_bridge_encoder_url
        _AS.effective_fc_player_bridge_encoder_url = lambda self: 'http://encoder.local/stream1'
        try:
            _client.get('/play/fc-player/directv/123.m3u8?adb=10.0.0.9:5555', environ_base=_BOX)
        finally:
            _fcb.trigger_channel, _fcb.hardware_bridge_active = _orig_fcb
            _AS.effective_fc_player_bridge_encoder_url = _orig_enc
        if _given.get('url'):
            _parts = urlsplit(_given['url'])
            assert _parts.path.startswith('/play/directv/'), f'the bridge no longer hands FC Player a server URL: {_given["url"]}'
            _manifest = _parts.path + (f'?{_parts.query}' if _parts.query else '')
        else:
            print('note: upstream\'s bridge route did not trigger in the throwaway setup; walking from browser.m3u8')
    except (ImportError, AttributeError) as _exc:
        print(f'note: upstream\'s bridge route changed shape ({_exc}); walking from browser.m3u8')

    def _lines_and_uris(text):
        uris = [m for m in __import__('re').findall(r'URI="([^"]+)"', text)]
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith('#')]
        return lines + [u for u in uris if not u.startswith('skd://')]

    # 2. The master the player gets: every reference goes back through the server.
    _wire_log.clear()
    _mr = _client.get(_manifest, environ_base=_BOX)
    assert _mr.status_code == 200, (_mr.status_code, _mr.get_data()[:200])
    _mtext = _mr.get_data(as_text=True)
    _refs = _lines_and_uris(_mtext)
    assert _refs and all(r.startswith('/') for r in _refs), f'the master points the player straight at DirecTV: {_refs}'
    _yo_req = next(u for u, _ in _wire_log if u.startswith(_m_yo + 'master.m3u8'))
    _q = parse_qs(urlsplit(_yo_req).query)
    for _k, _v in (('d', 'android_tv'), ('yospace.pool', 'livepause'), ('is_lat', '0'), ('dma_location', '501')):
        assert _q.get(_k) == [_v], f'the Yospace session lost {_k}={_v}: {_q.get(_k)}'
    assert _q.get('_fw_did') and _q.get('comscore_device') == [_SHOWN], _q

    # 3. The audio playlist: the inserted AC-3 ad (and its init) became the AAC twin, and
    # everything, live and ad, still goes through the server.
    _audio_ref = next(r for r in _refs if 'audio.m3u8' in __import__('urllib.parse').parse.unquote(r))
    _atext = _client.get(_audio_ref, environ_base=_BOX).get_data(as_text=True)
    _arefs = _lines_and_uris(_atext)
    assert all(r.startswith('/') for r in _arefs), f'the audio playlist points the player straight at DirecTV: {_arefs}'
    assert 'u-6600-c-384' not in _atext and 'u-6600-a-96-1-0.mp4' in _atext and 'u-6600-a-96-1-i.mp4' in _atext, _atext
    _ad_ref = next(r for r in _arefs if 'u-6600-a-96-1-0.mp4' in r)
    _live_ref = next(r for r in _arefs if 'seg-1.mp4' in r)

    # 4. The ad segment comes back cut (once); 5. the live segment comes back untouched.
    _wire_log.clear()
    assert _client.get(_ad_ref, environ_base=_BOX).get_data() == b'CUT:' + _m_raw_ad, 'the ad segment was not cut on the server'
    assert _cut == [_m_raw_ad], f'the loudness cut must run exactly once: {_cut}'
    assert _client.get(_live_ref, environ_base=_BOX).get_data() == _m_live_bytes, 'a live segment was altered'

    # 6. Every fetch the server made to DirecTV on the player's behalf carried the box's
    # app User-Agent.
    for _u, _h in _wire_log:
        assert (_h.get('User-Agent') or '').startswith('APP_PROJECT_NAME/'), f'{_u} went out without the app User-Agent'
    assert install.runtime_warnings() == [], install.runtime_warnings()
finally:
    _HTTPAdapter.send, device._client_device = _real_send, _real_client_device
    dtv_aac_gain.attenuate_ad_segment = _real_att

# (n) Hardening, one check per fix.
import threading as _th  # noqa: E402
# 1. The bridge-device list. Upstream returns ah4c's error rather than raising, so the
# stubs below return (devices, error) exactly as upstream's known_devices() does.
# - The first read is waited for; after that a stale list is served while a background
#   refresh runs, and no lock is held across a read (a box never waits on ah4c).
# - ah4c down: the last good ah4c tuners are kept (merged with what the database lists),
#   and unknown requesters stop triggering reads for a while.
# - An unknown requester triggers at most one read per IP per few seconds.
_real_kd, _real_spawn = up('known_devices'), device.spawn
_spawned, _reads = [], []
def _kd(devs, err=None):
    def f():
        _reads.append(1)
        return ([{'address': a, 'host': a.rsplit(':', 1)[0]} for a in devs], err)
    return f
try:
    device.forget_bridge_devices()
    install.up_set('known_devices', _kd(['10.0.0.5:5555', '10.0.0.8:5555']))
    assert device._bridge_address('10.0.0.5') == '10.0.0.5:5555' and len(_reads) == 1
    assert device._bridge_address('10.0.0.5') == '10.0.0.5:5555' and len(_reads) == 1, 'a cached list was re-read'
    # Stale list: served at once, the refresh goes to the background.
    device.spawn = lambda fn, *a: _spawned.append(fn)
    device._BRIDGE_LIST[0] -= device._BRIDGE_TTL + 1
    _reads.clear()
    assert device._bridge_address('10.0.0.5') == '10.0.0.5:5555', 'the last good list was not served while stale'
    assert _reads == [] and len(_spawned) == 1, (_reads, _spawned)
    # While that read is in flight, another box's lookup neither waits nor starts a second.
    assert device._bridge_address('10.0.0.8') == '10.0.0.8:5555' and len(_spawned) == 1, _spawned
    # ah4c down: upstream answers with only the database devices plus an error.
    install.up_set('known_devices', _kd(['10.0.0.9:5555'], "Couldn't read ah4c's tuner list: timeout"))
    _spawned.pop()()
    assert {d['address'] for d in device._BRIDGE_LIST[1]} == {'10.0.0.5:5555', '10.0.0.8:5555', '10.0.0.9:5555'}, \
        f'an ah4c outage dropped the last known ah4c tuners: {device._BRIDGE_LIST[1]}'
    assert device._bridge_address('10.0.0.8') == '10.0.0.8:5555'
    _reads.clear()
    assert device._bridge_address('10.0.0.77') is None and _reads == [] and not _spawned, \
        'an unknown requester triggered a read while ah4c was failing'
    # ah4c back: a fresh list replaces the merged one.
    device._bridge_failed_at[0] = 0.0
    install.up_set('known_devices', _kd(['10.0.0.6:5555']))
    device.spawn = _real_spawn
    assert device._bridge_address('10.0.0.6') == '10.0.0.6:5555', 'a newly added box was not recognized'
    _reads.clear()
    device._bridge_address('10.0.0.78'); device._bridge_address('10.0.0.78')
    assert len(_reads) == 1, f'an unknown requester re-read the list on every fetch: {len(_reads)}'
    # A read that can't even start doesn't leave lookups stuck behind it.
    device.forget_bridge_devices()
    device.spawn = lambda fn, *a: (_ for _ in ()).throw(RuntimeError('no threads'))
    assert device._bridge_address('10.0.0.6') is None and device._bridge_inflight[0] is None
finally:
    install.up_set('known_devices', _real_kd)
    device.spawn = _real_spawn
    device.forget_bridge_devices()

# 2. Every DAI session carries the billing ZIP. Sign-in and turning DAI on store it; a
# tune that still finds none fetches it right then (location lookup only, single-flight,
# saved), so even the first tune of a new sign-in carries bZipCode.
_real_flc = dai.fetch_location_context
_lookups = []
try:
    dai.fetch_location_context = lambda s, b, timeout=15: (_lookups.append(b) or {'zip': '10965', 'dma_id': '501'})
    dai._ZIP_FOUND.clear(); dai._ZIP_FAILED.clear()
    _real_sav = dai._save_account_values
    dai._save_account_values = lambda *a, **k: True
    _q = dai.build_query({'use_dai': 'true', 'bearer_token': 'zip-bearer'}, {}, '123')
    assert _q.get('bZipCode') == '10965', f'the first tune of a sign-in went without bZipCode: {_q.get("bZipCode")}'
    dai.build_query({'use_dai': 'true', 'bearer_token': 'zip-bearer'}, {}, '123')
    assert _lookups == ['zip-bearer'], f'the ZIP was looked up again on every tune: {_lookups}'
    # Concurrent first tunes share one lookup.
    dai._ZIP_FOUND.clear(); _lookups.clear()
    _gate = _th.Event()
    dai.fetch_location_context = lambda s, b, timeout=15: (_gate.wait(2), _lookups.append(b), {'zip': '10965'})[2]
    _out = []
    _ts = [_th.Thread(target=lambda: _out.append(dai._account_zip({'bearer_token': 'zip-2'}))) for _ in range(3)]
    for _x in _ts: _x.start()
    _t.sleep(0.2); _gate.set()
    for _x in _ts: _x.join(5)
    assert _out == ['10965'] * 3 and _lookups == ['zip-2'], (_out, _lookups)
    # A failed lookup: that tune goes without (never invented), retried shortly after.
    dai._ZIP_FOUND.clear(); dai._ZIP_FAILED.clear(); _lookups.clear()
    dai.fetch_location_context = lambda s, b, timeout=15: (_lookups.append(b) or {})
    assert 'bZipCode' not in dai.build_query({'bearer_token': 'zip-3'}, {}, '1')
    dai.build_query({'bearer_token': 'zip-3'}, {}, '1')
    assert len(_lookups) == 1, 'a failed ZIP lookup was hammered on every tune'
    assert dai._ZIP_FAILED[dai._bearer_tag('zip-3')][1] == dai._ZIP_RETRY
    # The service answered but has no ZIP for the account: not asked again this sign-in.
    _lookups.clear()
    dai.fetch_location_context = lambda s, b, timeout=15: (_lookups.append(b) or {'dma_id': '501'})
    dai._account_zip({'bearer_token': 'zip-4'})
    assert dai._ZIP_FAILED[dai._bearer_tag('zip-4')][1] == dai._ZIP_NONE_RETRY, 'a ZIP-less account is retried every 30 s'
    # A hard deadline: a hung lookup never holds a tune past _ZIP_DEADLINE, and the tune
    # that started it waits too (it goes without bZipCode, never an invented one).
    _hang = _th.Event()
    dai.fetch_location_context = lambda s, b, timeout=15: (_hang.wait(10), {'zip': '10965'})[1]
    _real_dl, dai._ZIP_DEADLINE = dai._ZIP_DEADLINE, 0.5
    _t0 = _t.time()
    assert dai._account_zip({'bearer_token': 'zip-5'}) is None
    assert _t.time() - _t0 < 2, f'a hung ZIP lookup held the tune {_t.time() - _t0:.1f}s'
    _hang.set(); dai._ZIP_DEADLINE = _real_dl
    # Waiters are released as soon as the ZIP is known, before the (slow) save.
    _save_gate = _th.Event()
    dai._save_account_values = lambda *a, **k: _save_gate.wait(5)
    dai.fetch_location_context = lambda s, b, timeout=15: {'zip': '10965'}
    _t0 = _t.time()
    assert dai._account_zip({'bearer_token': 'zip-6'}) == '10965'
    assert _t.time() - _t0 < 2, 'the tune waited for the ZIP to be saved'
    _save_gate.set()
    dai._save_account_values = lambda *a, **k: True
    # The license path's play-token fallback never waits on a ZIP lookup.
    _lookups.clear()
    dai.fetch_location_context = lambda s, b, timeout=15: (_lookups.append(b) or {'zip': '10965'})
    assert 'bZipCode' not in dai.request_flags({'use_dai': 'true', 'bearer_token': 'zip-7'}, {}, '1', zip_lookup=False)
    assert _lookups == [], 'the license fallback looked the ZIP up'
    assert dai.build_query({'dai_zip': '10965', 'bearer_token': 'zip-3'}, {}, '1')['bZipCode'] == '10965'
    dai._save_account_values = _real_sav
    # Sign-in starts the account lookup even for a non-code session missing the ZIP; once
    # the sign-in is stored it is merged in, and a config write made meanwhile survives.
    _real_fac, _real_spawn_z = dai.fetch_account_context, device.spawn
    _bg_fn = []
    dai.fetch_account_context = lambda session, bearer, timeout=15: {'zip': '10965', 'dma_id': '501'}
    device.spawn = lambda fn, *a: _bg_fn.append((fn, a))
    try:
        _c = {'use_dai': 'true', 'dai_dma_id': '501'}
        dai.store_login_result(_c, {'bearer_token': 'web', 'auth_method': 'curl_cffi'})
        assert [f.__name__ for f, _ in _bg_fn] == ['_fetch_account_values_after_signin'], _bg_fn
        with app.app_context():
            _keep = dict(db.session.get(Source, _sid).config)
            up('persist_config')(_sid, {'bearer_token': 'web', 'dtv_android_profid': 'written-meanwhile'})
            _bg_fn[0][0](*_bg_fn[0][1])
            db.session.expire_all()
            _cfg = db.session.get(Source, _sid).config
            assert _cfg.get('dai_zip') == '10965' and _cfg.get('dtv_android_profid') == 'written-meanwhile', _cfg
            # A lookup for a sign-in that is no longer stored is not saved.
            assert dai._save_account_values('stale-bearer', {'dai_zip': '99999'}) is False
            _s = db.session.get(Source, _sid); _s.config = _keep; db.session.commit()
    finally:
        dai.fetch_account_context, device.spawn = _real_fac, _real_spawn_z
    # The ZIP a tune looks up is saved to the live source row (inside a request, as a tune is).
    dai._ZIP_FOUND.clear(); dai._ZIP_FAILED.clear()
    dai.fetch_location_context = lambda s, b, timeout=15: {'zip': '10965', 'dma_id': '501'}
    with app.app_context():
        _keep = dict(db.session.get(Source, _sid).config)
        up('persist_config')(_sid, {'bearer_token': 'zip-save'})
        assert dai._account_zip({'bearer_token': 'zip-save'}) == '10965'
        for _ in range(40):   # saved in the background, after the tune is released
            db.session.expire_all()
            if db.session.get(Source, _sid).config.get('dai_zip') == '10965':
                break
            _t.sleep(0.1)
        db.session.expire_all()
        assert db.session.get(Source, _sid).config.get('dai_zip') == '10965', 'a tune\'s ZIP lookup was not saved'
        _s = db.session.get(Source, _sid)
        _s.config = _keep
        db.session.commit()
    # Turning DAI on looks the account values up once, and a failed lookup never fails the save.
    _real_fmac, _real_cib = dai._fetch_missing_account_context, dai.capture_in_background
    _fmac = []
    try:
        dai.capture_in_background = lambda addrs=None: 0
        dai._fetch_missing_account_context = lambda sid: (_fmac.append(sid), (_ for _ in ()).throw(RuntimeError('boom')))
        with app.app_context():
            _s = db.session.get(Source, _sid)
            _on = {'use_dai': 'true', 'bearer_token': 'b'}
            dai.settings_changed(_s, {}, _on)   # raises inside: must not propagate
            dai.settings_changed(_s, _on, _on)  # a re-save with DAI already on: no lookup
            assert _fmac == [_sid], f'turning DAI on looked the account up {_fmac}'
            # A failing cache clear / capture start doesn't fail the save either.
            _real_ccit = dai.clear_cache_if_toggled
            dai.clear_cache_if_toggled = lambda *a: (_ for _ in ()).throw(RuntimeError('db locked'))
            try:
                dai.settings_changed(_s, {}, {'use_dai': 'true'})
            finally:
                dai.clear_cache_if_toggled = _real_ccit
    finally:
        dai._fetch_missing_account_context, dai.capture_in_background = _real_fmac, _real_cib
finally:
    dai.fetch_location_context = _real_flc
    dai._ZIP_FOUND.clear(); dai._ZIP_FAILED.clear()

# 3. A box's own DAI session lives exactly as long as upstream's playback cache.
assert install._device_session_ttl() == float(up('playback_cache_ttl')), \
    (install._device_session_ttl(), up('playback_cache_ttl'))

# 5. The profile exchange uses upstream's own code sign-in client id (by role, no fallback).
assert dai._profile_client_id() == str(up('code_signin_client_id')) and dai._profile_client_id(), dai._profile_client_id()

# 6. If upstream's app User-Agent names a device comscore_device can't match, it is reported.
_real_presented = device.presented_device
try:
    device.presented_device = lambda: None
    assert any(f.startswith('comscore_device') for f in install.status(app)['failed']), install.status(app)
finally:
    device.presented_device = _real_presented
assert not any(f.startswith('comscore_device') for f in install.status(app)['failed'])

# 4 + 7. The profile switch merges into the live config under upstream's lock (a token
# written meanwhile survives), releases only its own refresh lock, re-queues a refresh
# it may have deferred, and refuses while upstream's refresh holds the lock.
class _FakeRedis:
    def __init__(self):
        self.d = {}
    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.d:
            return False
        self.d[k] = v.encode() if isinstance(v, str) else v
        return True
    def get(self, k):
        return self.d.get(k)
    def delete(self, k):
        self.d.pop(k, None)
_fr = _FakeRedis()
_real = (dai._redis, dai.profile_token_exchange, dai._requeue_stale_refresh)
_requeued = []
try:
    dai._redis = lambda: _fr
    dai._requeue_stale_refresh = lambda src: _requeued.append(src.id)
    def _exchange(profile_id, rt):
        with app.app_context():   # a refresh lands while the exchange is in flight
            up('persist_config')(_sid, {'activation_token': 'ROTATED', 'bearer_token': 'NEW-BEARER'})
        return {'valuePairs': {'partnerProfileID1': 'pp1-profile'}}
    dai.profile_token_exchange = _exchange
    with app.app_context():
        _src = db.session.get(Source, _sid)
        _res = dai.select_profile(_src, 'p1', 'David')
        assert _res['ok'], _res
        db.session.expire_all()
        _c = db.session.get(Source, _sid).config
        assert _c.get('dtv_android_profid') == 'pp1-profile' and _c.get('dtv_android_profile_id') == 'p1', _c
        assert _c.get('activation_token') == 'ROTATED' and _c.get('bearer_token') == 'NEW-BEARER', \
            'the profile switch overwrote a token written during the exchange'
        assert _fr.d == {}, f'the profile switch left its refresh lock behind: {_fr.d}'
        assert _requeued == [_sid], 'a refresh deferred by the profile switch was not re-queued'
        _fr.d['directv:auth:refreshing:directv'] = b'1'   # upstream's refresh holds the lock
        _res = dai.select_profile(db.session.get(Source, _sid), 'p1', 'David')
        assert not _res['ok'] and 'refresh' in _res['error'], _res
        assert _fr.d['directv:auth:refreshing:directv'] == b'1', "the profile switch released upstream's lock"
        # A failed exchange still releases its lock and re-queues the deferred refresh.
        del _fr.d['directv:auth:refreshing:directv']
        _requeued.clear()
        dai.profile_token_exchange = lambda *a: (_ for _ in ()).throw(RuntimeError('HTTP 500'))
        _res = dai.select_profile(db.session.get(Source, _sid), 'p1', 'David')
        assert not _res['ok'] and _fr.d == {} and _requeued == [_sid], (_res, _fr.d, _requeued)
finally:
    dai._redis, dai.profile_token_exchange, dai._requeue_stale_refresh = _real
# 7b. The re-queue itself goes through upstream's own refresh path, only when stale.
_real_sr = up('start_reauth')
_started = []
try:
    install.up_set('start_reauth', lambda self: _started.append(1))
    with app.app_context():
        _s = db.session.get(Source, _sid)
        _s.config = {**_s.config, 'token_captured_at': _t.time()}
        db.session.commit()
        dai._requeue_stale_refresh(_s)
        assert _started == [], 'a fresh session was refreshed'
        _s.config = {**_s.config, 'token_captured_at': 0}
        db.session.commit()
        dai._requeue_stale_refresh(_s)
        assert _started == [1], "a stale session deferred by the profile switch was not refreshed"
finally:
    install.up_set('start_reauth', _real_sr)

# The profile save's retry never sleeps after its last attempt.
_real_pc, _real_sleep = up('persist_config'), dai.time.sleep
_sleeps = []
try:
    _fr2 = _FakeRedis()
    dai._redis = lambda: _fr2
    dai.profile_token_exchange = lambda *a: {'valuePairs': {'partnerProfileID1': 'pp'}, 'refreshToken': 'NEW'}
    dai._requeue_stale_refresh = lambda src: None
    install.up_set('persist_config', lambda *a, **k: False)
    dai.time.sleep = lambda n: _sleeps.append(n)
    with app.app_context():
        _r = dai.select_profile(db.session.get(Source, _sid), 'p1', 'David')
    assert not _r['ok'] and _sleeps == [1, 2], f'retry sleeps: {_sleeps}'
finally:
    install.up_set('persist_config', _real_pc)
    dai.time.sleep = _real_sleep
    dai._redis, dai.profile_token_exchange, dai._requeue_stale_refresh = _real

# (o) Every DAI session carries profid: with none picked, the account's DEFAULT (primary)
# profile is picked automatically; a pick made in the panel is never replaced; a tune with
# no profid starts the pick (rate-limited) and is reported.
_real = (dai.list_profiles, dai.select_profile, device.spawn)
_picked, _sp = [], []
try:
    dai.list_profiles = lambda b: {'profiles': [{'id': 'kid', 'name': 'Kid', 'primary': False},
                                                {'id': 'main', 'name': 'David', 'primary': True}],
                                   'current_id': 'kid'}
    dai.select_profile = lambda src, pid, name='': (_picked.append(pid) or {'ok': True})
    with app.app_context():
        _s = db.session.get(Source, _sid)
        _keep = dict(_s.config)
        _s.config = {**_keep, 'use_dai': 'true', 'auth_method': up('code_signin_method'),
                     'refresh_token': 'r', 'bearer_token': 'pick-b'}
        _s.config.pop('dtv_android_profid', None)
        db.session.commit()
        dai._auto_pick_profile('pick-b')
        assert _picked == ['main'], f'the default (primary) profile was not picked: {_picked}'
        _s = db.session.get(Source, _sid); _s.config = {**_s.config, 'dtv_android_profid': 'chosen'}; db.session.commit()
        _picked.clear(); dai._auto_pick_profile('pick-b')
        assert _picked == [], 'the automatic pick replaced a profile picked in the panel'
        _s = db.session.get(Source, _sid); _s.config = _keep; db.session.commit()
    # A tune with no profid starts the pick (once per window) and reports it.
    device.spawn = lambda fn, *a: _sp.append(fn.__name__)
    dai._auto_pick_at[0] = 0.0
    _cfg = {'use_dai': 'true', 'auth_method': up('code_signin_method'), 'refresh_token': 'r', 'bearer_token': 'b'}
    dai.build_query(_cfg, {}, '1'); dai.build_query(_cfg, {}, '1')
    assert _sp.count('_auto_pick_profile') == 1, _sp
    assert any('without profid' in w for w in install.runtime_warnings()), install.runtime_warnings()
    dai.build_query({**_cfg, 'dtv_android_profid': 'pp'}, {}, '1')
    assert not any('without profid' in w for w in install.runtime_warnings()), 'a tune with profid did not clear the warning'
finally:
    dai.list_profiles, dai.select_profile, device.spawn = _real

# (p) A cached DAI session is reused only while it carries current targeting. A value
# this process can't know yet (another worker's unsaved ZIP) never makes it stale; a
# value known now that is missing or different does. The check never starts anything.
_yo = 'https://x.yospace.com/csm/a.m3u8?yospace.pool=livepause'
_cfg = {'use_dai': 'true', 'dai_partner_profile_id': 'hh', 'dai_dma_id': '501', 'dtv_android_profid': 'pp'}
_full = {'hhid': 'hh', 'u': 'hh', 'profid': 'pp', 'dma_location': '501', 'dma_billing': '501', 'bZipCode': '10965'}
_e = lambda t: {'fallback_url': _yo, 'dai': True, 'dai_targeting': t}
_sp2, _real_sp2 = [], device.spawn
device.spawn = lambda fn, *a: _sp2.append(fn.__name__)
try:
    assert dai.session_current(_e(_full), _cfg, {}, '1'), 'a session from another worker (ZIP not known here) was dropped'
    assert dai.session_current(_e(_full), {**_cfg, 'dai_zip': '10965'}, {}, '1')
    assert not dai.session_current(_e({k: v for k, v in _full.items() if k != 'bZipCode'}), {**_cfg, 'dai_zip': '10965'}, {}, '1'), \
        'a session opened before the ZIP arrived was reused'
    assert not dai.session_current(_e({k: v for k, v in _full.items() if k != 'profid'}), _cfg, {}, '1'), \
        'a session opened before the profile was set was reused'
    assert not dai.session_current(_e({**_full, 'hhid': 'other', 'u': 'other'}), _cfg, {}, '1'), 'another account\'s session was reused'
    assert not dai.session_current({'fallback_url': _yo, 'dai': True}, _cfg, {}, '1'), 'an untagged (old) session was kept'
    assert dai.session_current({'fallback_url': 'https://dfwlive/x.m3u8', 'dai': True}, _cfg, {}, '1'), 'a no-DAI channel entry was dropped'
    assert dai.session_current(_e(_full), {k: v for k, v in _cfg.items() if k != 'dtv_android_profid'}, {}, '1') is not None
    assert _sp2 == [], f'the read-only session check started background work: {_sp2}'
finally:
    device.spawn = _real_sp2

# (q) A box another worker is capturing is reported 'busy', and this worker's in-progress
# mark is cleared, so a later capture here is not skipped forever.
class _Busy:
    def set(self, *a, **k): return False
    def delete(self, *a): raise AssertionError("deleted the other worker's capture lock")
_real_r, _real_reach = dai._redis, dai._reachable
try:
    dai._redis = lambda: _Busy()
    dai._reachable = lambda a: (_ for _ in ()).throw(AssertionError('captured a box another worker holds'))
    assert dai._capture_and_store('10.0.0.50:5555') == 'busy'
    assert '10.0.0.50:5555' not in dai._adid_inflight, 'the losing worker kept the box marked in progress'
    dai._redis = lambda: None
    dai._reachable = lambda a: False
    assert dai._capture_and_store('10.0.0.50:5555') == 'unreachable', 'a later capture on this worker was skipped'
finally:
    dai._redis, dai._reachable = _real_r, _real_reach

# (r) Two boxes' sessions saved at the same moment (different workers) both survive:
# the read-merge-write is serialized and reads only the per-box key.
_real_lc = up('load_cache')
_seen_keys = []
def _lc(name, keys=None, exclude=None):
    _seen_keys.append(keys)
    return _real_lc(name, keys=keys, exclude=exclude)
try:
    install.up_set('load_cache', _lc)
    with app.app_context():
        up('persist_cache')(_sid, {install.DEVICE_SESSIONS: {}})
    _now = _t.time()
    def _save(addr):
        with app.app_context():
            install._persist_device_session(addr, '123', {'fallback_url': 'u', 'dai': True, 'cached_at': _now})
    _ths = [_th.Thread(target=_save, args=(f'10.0.1.{i}:5555',)) for i in range(6)]
    for _x in _ths: _x.start()
    for _x in _ths: _x.join(30)
    with app.app_context():
        up('db').session.expire_all()
        _st = (_real_lc('directv', keys=[install.DEVICE_SESSIONS]) or {}).get(install.DEVICE_SESSIONS) or {}
    assert len(_st) == 6, f'concurrent per-box session saves lost entries: {sorted(_st)}'
    assert all(k == [install.DEVICE_SESSIONS] for k in _seen_keys), f'the save read more than its own key: {_seen_keys}'
finally:
    install.up_set('load_cache', _real_lc)

print('Overlay smoke test passed.')
