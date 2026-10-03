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


import inspect  # noqa: E402

from app.scrapers import directv, directv_dai as dai  # noqa: E402
from app.scrapers.base import BaseScraper  # noqa: E402
from app.routes import api_sources  # noqa: E402,F401
from app.config_store import persist_source_cache_updates  # noqa: E402,F401

# The upstream APIs the hooks call. These run on every scrape and tune, even with
# DAI off, and upstream can rename them without any textual conflict in the patch.
assert callable(getattr(BaseScraper, '_update_cache', None)), 'BaseScraper._update_cache is gone'
assert isinstance(inspect.getattr_static(BaseScraper, 'cache'), property), 'BaseScraper.cache is no longer a property'
assert 'dai' in inspect.signature(directv._fetch_channel_playback).parameters, \
    '_fetch_channel_playback lost its dai parameter'
resolve_src = inspect.getsource(directv.DirectvScraper.resolve)
for hook in ('directv_dai.cached_url_usable(', 'dai=directv_dai.request_flags('):
    assert hook in resolve_src, f'resolve() is missing the {hook} hook'
save_src = inspect.getsource(api_sources.save_source_config)
assert 'directv_dai.clear_cache_if_toggled(' in save_src, 'save_source_config lost the cache-clear hook'
assert 'directv_dai.store_login_result(' in inspect.getsource(directv.run_directv_auth)
# The device ad id reads upstream's bridge-device list; if it's renamed or reshaped the
# patch would silently fall back to national-only ads, so fail the build instead.
from app import bridge_devices  # noqa: E402
assert callable(getattr(bridge_devices, 'known_devices', None)), 'bridge_devices.known_devices is gone'
assert 'address' in inspect.getsource(bridge_devices.known_devices) and 'host' in inspect.getsource(bridge_devices.known_devices), \
    'bridge_devices.known_devices no longer returns address/host entries'

# DAI streams take the same FastChannels relay path as every other DirecTV stream.
from app.routes import directv_proxy  # noqa: E402
assert directv_proxy._directv_browser_cdn_allowed('csm-e-dtv-livecomplex-eb.tls1.yospace.com'), \
    'Yospace playlists bypass the FastChannels relay'

# The toggle is wired into the DirecTV source settings.
keys = {field.key for field in directv.DirectvScraper.config_schema}
assert 'use_dai' in keys, 'use_dai toggle missing from the DirecTV config schema'
assert 'surround_audio' not in keys, 'the removed Surround sound toggle is back'

# DirecTV signs in end-to-end as its own Android TV app (device-code grant,
# refresh thereafter), not as the web browser client — so the bearer, DRM and ad
# session are all Android TV. The web login runs only as the one-time grant approver.
from app.scrapers import dtv_android  # noqa: E402
assert dtv_android._CLIENT_ID == 'UNIFIED_Android_TV_02', 'Android TV OAuth client id changed'
assert 'dtv_android.sign_in(' in inspect.getsource(directv.run_directv_auth), \
    'run_directv_auth no longer routes auth through the Android TV sign-in (refresh-or-grant)'
assert 'dtv_android.license_headers(' in inspect.getsource(directv.DirectvScraper.license_request_headers), \
    'DRM license headers are no longer the Android TV headers (app UA, no stream.directv.com Origin/Referer)'

# Token lifecycle logic (pure, no network): "stale" (-> must authenticate) means only
# "no bearer", or past a known expiry. Separately, a warm Android TV session refreshes in
# the background on the app's ~55-min cadence (background_refresh_due) — re-minting the DRM
# activation token — while a tune recovers inline (refresh_in_place) and drm_reauth self-heals.
import time as _t  # noqa: E402
assert dtv_android.token_stale({}) is True, 'no bearer must be stale'
assert dtv_android.token_stale({'bearer_token': 'x'}) is False, 'a bearer with no tracked expiry must not be timer-stale'
assert dtv_android.token_stale({'bearer_token': 'x', 'dtv_android_expires_at': _t.time() + 3600}) is False, 'valid future expiry is not stale'
assert dtv_android.token_stale({'bearer_token': 'x', 'dtv_android_expires_at': _t.time() - 10}) is True, 'past expiry is stale'
assert dtv_android.token_expires_at({'expiresIn': 3600}) is not None, 'a duration expiry should parse'
assert dtv_android.token_expires_at({'exp': _t.time() + 3600}) is not None, 'an absolute expiry should parse'
assert dtv_android.token_expires_at({'expirationTime': 9.9e14}) is None, 'an absurd value must be rejected, not trusted as an expiry'
assert dtv_android.token_expires_at({}) is None, 'no expiry field -> None (ride until a real 401)'
assert callable(getattr(dtv_android, 'refresh_in_place', None)), 'the inline tune-time refresh (refresh_in_place) is missing'
assert 'dtv_android.refresh_in_place(' in inspect.getsource(directv.DirectvScraper.resolve), \
    'resolve() no longer refreshes inline + retries on an expired token, so a tune can fail instead of recovering'

# Background refresh keeps the DRM session warm like the app (re-minting the activation
# token) so a tune — even one right after the box was off for days — never meets a dead
# token. A fresh capture is not due; an old one is; Android TV only.
_warm = {'auth_method': 'dtv_android', 'bearer_token': 'x', 'refresh_token': 'r',
         'dtv_android_device_id': 'd'}
assert dtv_android.background_refresh_due({}) is False, 'nothing to refresh with -> not due'
assert dtv_android.background_refresh_due({**_warm, 'token_captured_at': _t.time()}) is False, \
    'a freshly-captured session is not due for a background refresh'
assert dtv_android.background_refresh_due({**_warm, 'token_captured_at': _t.time() - 4000}) is True, \
    'an old session must be due for a background refresh (keeps the DRM activation token warm)'
assert dtv_android.background_refresh_due({**_warm, 'token_captured_at': _t.time() - 4000, 'auth_method': 'web'}) is False, \
    'background refresh is Android-TV only'
# The refresh actually re-mints the DRM activation token: it sends reqParams=ACTIVATIONTOKEN
# (the app's own refresh param), without which authn-refreshgo returns no activation token.
assert "'ACTIVATIONTOKEN'" in inspect.getsource(dtv_android.refresh_session), \
    'refresh_session must send reqParams=ACTIVATIONTOKEN so the refresh re-mints the DRM activation token'
# pre_run_setup drives the background refresh (warm the session on the scrape cadence).
assert 'background_refresh_due(' in inspect.getsource(directv.DirectvScraper.pre_run_setup), \
    'pre_run_setup no longer kicks the background refresh that keeps the DRM session warm'
# drm_reauth self-heals by refreshing (re-minting the token), not by reusing a dead one.
assert 'refresh_session(' in inspect.getsource(dtv_android.drm_reauth), \
    'drm_reauth must refresh (re-mint the activation token) to recover, not reuse the expired token'

# The Android-TV client logic (device reading, app UA, channel/v2 request) lives in
# dtv_android so the Android login is portable; directv_dai (DAI) depends on it, never
# the reverse. keep_drm_session was reverted (it broke ad targeting).
assert 'directv_dai.' not in inspect.getsource(dtv_android) and 'import directv_dai' not in inspect.getsource(dtv_android), \
    'dtv_android must not depend on directv_dai (portable Android login; DAI depends on it)'
assert not hasattr(dai, 'player_headers') and not hasattr(dai, 'android_auth_request') and not hasattr(dai, 'keep_drm_session'), \
    'Android-TV client logic (and the reverted keep_drm_session) must not live in directv_dai'
assert 'keep_drm_session' not in inspect.getsource(directv_proxy.directv_browser_asset), \
    'keep_drm_session hook must be gone from the relay (reverted — it broke ad targeting)'
assert all(callable(getattr(dtv_android, n, None)) for n in ('_client_device', 'player_headers', 'android_auth_request')), \
    'the Android-TV client logic did not move into dtv_android'
# The relay sends the DirecTV app's own User-Agent, built from the bridge device's build
# properties (never the bare Custom-Exoplayer fallback, which stopped ad insertion).
assert not hasattr(dtv_android, 'PLAYER_USER_AGENT'), 'the fixed Custom-Exoplayer User-Agent is back'
assert dtv_android._APP_USER_AGENT.format(release='11', model='AFTKRT', board='karat') == \
    'APP_PROJECT_NAME/5.0.136.2002113867 (Android 11; AFTKRT; karat)  PureRN/0.79.5'
assert dtv_android.player_headers() == {}, 'outside a request there is no device to build a User-Agent for'
for fn in (directv_proxy.directv_browser_manifest, directv_proxy.directv_browser_asset):
    assert 'dtv_android.player_headers()' in inspect.getsource(fn), f'{fn.__name__} no longer sends the device User-Agent'
# AAC ad swap: each inserted AC-3 ad (segment + its own EXT-X-MAP init) is rewritten to
# the creative's full-range HE-AAC twin; live (dfwlive-*) content is untouched; the clear
# ads keep METHOD=NONE.
from app.scrapers import dtv_aac_ads  # noqa: E402
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
assert dtv_aac_ads.swap_muffled_ads(_aac_pl).count('a-96') == 2, 'exactly the ad segment and its init should flip'
assert dtv_aac_ads.swap_muffled_ads('#EXTM3U\nhttps://dfwlive/live/s.mp4\n') == '#EXTM3U\nhttps://dfwlive/live/s.mp4\n', 'a playlist with no inserted AC-3 ad must be unchanged'
assert 'dtv_aac_ads.swap_muffled_ads(' in inspect.getsource(directv_proxy.directv_browser_asset), \
    'the relay no longer swaps muffled inserted ads for their AAC twin'
# With DAI on, authorization is the Android TV app's request: channel/v2, no browser
# Origin/Referer, the app's query. DAI off leaves the web request untouched.
class _S:
    headers = {'Origin': 'o', 'Referer': 'r', 'User-Agent': 'web'}
_p = {'ccid': '1', 'timeShiftEnabled': 'true', 'dualManifest': 'false', 'daiEnabled': 'true'}
assert dtv_android.android_auth_request(_S, _p, True, 'v1-url').endswith('/channel/v2')
assert 'Origin' not in _S.headers and 'Referer' not in _S.headers, _S.headers
assert 'timeShiftEnabled' not in _p and 'dualManifest' not in _p and _p['startOver'] == 'false', _p
_p2 = {'timeShiftEnabled': 'true'}
assert dtv_android.android_auth_request(_S, _p2, False, 'v1-url') == 'v1-url' and _p2 == {'timeShiftEnabled': 'true'}
assert 'dtv_android.android_auth_request(' in inspect.getsource(directv._fetch_channel_playback)

# No stored credentials: the Android TV device-code grant is approved in a browser, so the
# config schema has no username/password and "configured" means a captured session.
assert not any(getattr(f, 'key', None) in ('username', 'password') for f in directv.DirectvScraper.config_schema), \
    'DirecTV should no longer have username/password config fields'
from app.source_config import is_source_config_complete  # noqa: E402
assert is_source_config_complete('directv', directv.DirectvScraper, {'bearer_token': 'x'}) is True, \
    '"configured" must mean a captured session'
assert is_source_config_complete('directv', directv.DirectvScraper, {}) is False, 'no session must read as not configured'
assert callable(getattr(api_sources, 'directv_logout', None)), 'the DirecTV logout endpoint is missing'
assert 'username and password must be saved first' not in inspect.getsource(api_sources.directv_auto_login), \
    'auto-login must not require stored credentials (the device-code grant needs none)'

# Stereo-downmix feature REMOVED 2026-10-03 (flawed: declaring CHANNELS="2" on a 5.1 AC-3
# bitstream made the stick/media3 do a thin fold). The AC-3 rendition is left stock; assert
# no trace of it comes back on an upstream merge / conflict fix.
assert not any(getattr(f, 'key', None) == 'stereo_downmix' for f in directv.DirectvScraper.config_schema), \
    'the removed stereo_downmix toggle is back'
assert 'stereo_downmix' not in inspect.getsource(directv_proxy.directv_browser_manifest), \
    'the removed stereo-downmix master hook is back in the relay'
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
expected = {'hhid': 'hh-1', 'u': 'hh-1', 'profid': 'prof-1', 'dma_location': '999',
            'dma_billing': '999', 'is_lat': '1', 'gpp_sid': '7', 'bZipCode': '10001',
            '_fw_did': 'google_advertising_id:optout', 'adid': 'optout', 'net': 'testnet'}
for key, value in expected.items():
    assert query.get(key) == value, f'{key}: expected {value!r}, got {query.get(key)!r}'

# The Android TV login's chosen viewer profile (dtv_android_profid, set by the profile
# picker's profiletoken exchange) is what the app sends as profid, and it wins over the old
# web-login dai_profile_id. Guards the picker against a future drift fix silently dropping it.
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

# A bridge device's own values take over from the account consent, the same way on
# every device: android_id:<Android ID> + is_lat=0 (no platform advertising id on any
# device), comscore_device as the app builds it; limited tracking gets the optout form.
fire = {'manufacturer': 'Amazon', 'model': 'AFTKRT', 'board': 'karat', 'release': '11',
        'limit_ad_tracking': '0', 'android_id': 'abcdef0123456789'}
assert dai.device_ad_flags(fire) == {'comscore_device': 'Android_Amazon_AFTKRT', 'is_lat': '0',
    '_fw_did': 'android_id:abcdef0123456789'}
assert dai.device_ad_flags({**fire, 'limit_ad_tracking': '1'})['_fw_did'] == 'google_advertising_id:optout'
shield = {'manufacturer': 'NVIDIA', 'model': 'SHIELD Android TV', 'android_id': 'abcdef0123456789'}
assert dai.device_ad_flags(shield) == {'comscore_device': 'Android_NVIDIA_SHIELDAndroidTV', 'is_lat': '0',
    '_fw_did': 'android_id:abcdef0123456789'}
assert dai.device_ad_flags({}) == {}
assert 'advertising_id' not in dict(dtv_android._DEVICE_PROPS), 'no device reads a platform advertising id'
real_client_device = dtv_android._client_device
dtv_android._client_device = lambda: fire
dev = dai.build_query(config, {}, '123')
assert dev['is_lat'] == '0' and 'adid' not in dev and dev['comscore_device'] == 'Android_Amazon_AFTKRT', dev
assert dtv_android.player_user_agent() == 'APP_PROJECT_NAME/5.0.136.2002113867 (Android 11; AFTKRT; karat)  PureRN/0.79.5'
dev_url = dai.pick_stream_url({'streamURL': 'https://x.yospace.com/a.m3u8?yo.up=u'}, dev)
assert dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True)
dtv_android._client_device = lambda: {**fire, 'android_id': 'ffffffffffffffff'}
assert not dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True), 'another device reused a session'
dtv_android._client_device = real_client_device

# Sessions always use a device's saved values (a stable identity). A new device is
# read right away; known devices are re-checked in the background and the saved
# values change only when the device really changed. A failed re-check changes nothing.
import tempfile  # noqa: E402
from flask import Flask  # noqa: E402
class _SyncThread:  # run background re-checks inline so the test is deterministic
    def __init__(self, target, args=(), daemon=None):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)
_tmp = tempfile.mkdtemp()
_orig = (dtv_android._bridge_address, dtv_android._read_device, dtv_android._devices_file)
_orig_thread = dtv_android.threading.Thread
dtv_android.threading.Thread = _SyncThread
dtv_android._bridge_address = lambda ip: '10.0.0.9:5555'
dtv_android._devices_file = lambda: os.path.join(_tmp, 'devices.json')
reset = {**fire, 'android_id': 'ffffffffffffffff'}
_ctx = Flask('t').test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.9'})
with _ctx:
    dtv_android._DEVICE_CACHE.clear(); dtv_android._read_device = lambda a: dict(fire)
    assert dtv_android._client_device() == fire, 'new device not read'
    assert dtv_android._saved_devices() == {'10.0.0.9:5555': fire}, 'new device not saved'
    dtv_android._DEVICE_CACHE.clear(); dtv_android._read_device = lambda a: {}
    assert dtv_android._client_device() == fire and dtv_android._saved_devices()['10.0.0.9:5555'] == fire, \
        'a failed re-check changed the saved identity'
    dtv_android._DEVICE_CACHE.clear(); dtv_android._read_device = lambda a: dict(reset)
    assert dtv_android._client_device() == fire, 'the session must use the saved values, not wait for the re-check'
    assert dtv_android._saved_devices()['10.0.0.9:5555'] == reset, 'a reset device (new Android ID) was not updated'
    dtv_android._DEVICE_CACHE.clear()
    assert dtv_android._client_device() == reset, 'the updated identity is not used afterwards'
    os.remove(dtv_android._devices_file()); dtv_android._DEVICE_CACHE.clear(); dtv_android._read_device = lambda a: {}
    assert dtv_android._client_device() == {}, 'device values invented with nothing saved and no read'
dtv_android.threading.Thread = _orig_thread
dtv_android._bridge_address, dtv_android._read_device, dtv_android._devices_file = _orig
dtv_android._DEVICE_CACHE.clear()

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

class _Scraper:
    def __init__(self):
        self.cache = {}
    def _update_cache(self, key, value):
        self.cache[key] = value
sc = _Scraper()
dai.note_channel(sc, {'daiChannelName': ' cnn '}, '1')
dai.note_channel(sc, {'daiChannelName': ''}, '2')
assert sc.cache == {'dai_channel_names': {'1': 'cnn'}}, sc.cache
assert 'directv_dai.note_channel(self, ' in inspect.getsource(directv.DirectvScraper)

# ── Behavioral relay tests ────────────────────────────────────────────────────
# The assertions above grep inspect.getsource for each hook. That proves the call
# is still written, but not that it still RUNS: a conflict/drift fix that keeps the
# string and guts the work (an early return, `if False`, a dropped wrap) would pass
# them. These drive the actual Flask relay routes with a stubbed upstream so the
# swap, the loudness cut and the DRM self-heal must really happen or the build fails.
import types as _types  # noqa: E402
from urllib.parse import quote as _quote  # noqa: E402
from flask import Flask as _Flask  # noqa: E402

from app.scrapers import dtv_aac_gain  # noqa: E402
assert callable(getattr(dtv_aac_gain, 'attenuate_ad_segment', None)) and callable(
    getattr(dtv_aac_gain, 'is_ad_segment', None)), 'dtv_aac_gain lost attenuate_ad_segment/is_ad_segment'


class _Resp:
    """Minimal stand-in for a requests.Response the relay fetches from upstream."""
    def __init__(self, *, text='', content=b'', status=200,
                 ctype='application/octet-stream', url='', content_length=None):
        self.text, self.content, self.status_code = text, content, status
        self.headers, self.url = {'Content-Type': ctype}, url
        cl = content_length if content_length is not None else (len(content) if content else None)
        if cl is not None:
            self.headers['Content-Length'] = str(cl)

    def iter_content(self, chunk_size=65536):
        yield self.content

    def close(self):
        pass


_app = _Flask('smoke')
_app.testing = True
_app.register_blueprint(directv_proxy.directv_proxy_bp)
_client = _app.test_client()
_ASSET = '/play/directv/browser-asset?url='

_real_requests = directv_proxy._requests  # `import requests as _requests` — the real module
# Under the test client there IS a request context, so player_headers() would try to match a
# bridge device (DB/adb). Stub the device lookup so the relay never reaches for either.
_real_client_device = dtv_android._client_device
dtv_android._client_device = lambda: {}
try:
    # (a) An inserted-ad AC-3 audio playlist is swapped to the full-range AAC twin — segment
    # AND its own EXT-X-MAP init — THROUGH the relay; live (dfwlive) content is left alone.
    _pl = ('#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n'
           '#EXT-X-MAP:URI="https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-i.mp4"\n'
           'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-c-384-1-0.mp4\n'
           'https://dfwlive-v2-c0p7-ms.directv.fastly-edge.com/live/seg-1.mp4\n')
    _pl_url = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/audio.m3u8'
    directv_proxy._requests = _types.SimpleNamespace(
        get=lambda url, **kw: _Resp(text=_pl, ctype='application/vnd.apple.mpegurl', url=_pl_url))
    _body = _client.get(_ASSET + _quote(_pl_url, safe='')).get_data(as_text=True)
    # Filenames survive the percent-encoding of the rewritten proxy URLs (only '/' and ':' encode).
    assert 'u-6600-a-96-1-0.mp4' in _body and 'u-6600-a-96-1-i.mp4' in _body, \
        'the relay did not swap the inserted AC-3 ad (segment + its EXT-X-MAP init) to the AAC twin'
    assert 'u-6600-c-384' not in _body, 'an AC-3 ad rendition survived the relay swap'
    assert 'dfwlive-v2-c0p7-ms' in _body, 'the relay dropped the live (non-ad) content line'

    # (b) A master playlist passes through untouched by the swap: the stock 5.1 CHANNELS="6"
    # survives (the downmix was removed) and nothing is turned into an AAC ad.
    _master = ('#EXTM3U\n'
               '#EXT-X-STREAM-INF:BANDWIDTH=6500000,CODECS="avc1.640028,ac-3",AUDIO="aud"\n'
               'https://yospace01-directv.akamaized.net/dtv-prd/x/variant-1080.m3u8\n'
               '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",CHANNELS="6",'
               'URI="https://yospace01-directv.akamaized.net/dtv-prd/x/audio.m3u8"\n')
    _m_url = 'https://yospace01-directv.akamaized.net/dtv-prd/x/master.m3u8'
    directv_proxy._requests = _types.SimpleNamespace(
        get=lambda url, **kw: _Resp(text=_master, ctype='application/vnd.apple.mpegurl', url=_m_url))
    _mbody = _client.get(_ASSET + _quote(_m_url, safe='')).get_data(as_text=True)
    assert 'CHANNELS="6"' in _mbody, 'the master AC-3 channel count was altered (downmix must stay removed)'
    assert 'a-96' not in _mbody, 'the swap must not touch a master playlist'
    assert 'BANDWIDTH=6500000' in _mbody, 'the master STREAM-INF was mangled'

    # (c) Codec-gated loudness: an inserted-ad audio segment reaches attenuate_ad_segment and
    # the attenuated bytes reach the client — on a plain fetch AND on a byte-range request (the
    # edit is length-preserving, so the range is served from the attenuated full segment, NOT
    # bypassed). A large (video-sized) segment streams through untouched. The trigger is the
    # audio codec via dtv_aac_gain, not the ad URL.
    _seg_url = 'https://yospace01-directv.akamaized.net/dtv-prd/7/7/0/02001/u-6600-a-96-1-0.mp4'
    _raw = b'\xff\xf1raw-aac-ad-bytes-stand-in'
    directv_proxy._requests = _types.SimpleNamespace(
        get=lambda url, **kw: _Resp(content=_raw, ctype='video/mp4', url=_seg_url))
    _seen, _SENT = [], b'ATTENUATED-SENTINEL-0123456789'
    _real_att = dtv_aac_gain.attenuate_ad_segment
    dtv_aac_gain.attenuate_ad_segment = lambda data, *a, **k: (_seen.append(data) or _SENT)
    # The passthrough streamer is wired up by the app factory, which the smoke test doesn't
    # run, so stub it to echo the upstream bytes (the large/video stream-through path).
    from flask import Response as _Response  # noqa: E402
    _real_stream = directv_proxy._stream_upstream_response
    directv_proxy._stream_upstream_response = lambda up, **kw: _Response(
        up.content, status=kw.get('status', 200), content_type=kw.get('content_type'))
    try:
        _sr = _client.get(_ASSET + _quote(_seg_url, safe=''))
        assert _sr.get_data() == _SENT, 'the attenuated bytes did not reach the client (gain hook gutted?)'
        assert _seen == [_raw], 'attenuate_ad_segment was not called with the upstream segment bytes'
        _seen.clear()
        _rr = _client.get(_ASSET + _quote(_seg_url, safe=''), headers={'Range': 'bytes=0-9'})
        assert _seen == [_raw], 'a Range request must still be attenuated (fetch full, then slice)'
        assert _rr.status_code == 206 and _rr.get_data() == _SENT[0:10], \
            'a Range fetch must return the requested slice of the attenuated segment'
        _seen.clear()
        _big_url = 'https://yospace01-directv.akamaized.net/dtv-prd/x/video-seg.mp4'
        directv_proxy._requests = _types.SimpleNamespace(
            get=lambda url, **kw: _Resp(content=b'VIDEOBYTES', ctype='video/mp4', url=_big_url,
                                        content_length=50_000_000))
        _vr = _client.get(_ASSET + _quote(_big_url, safe=''))
        assert _seen == [], 'a large (video) segment must stream through, never be attenuated'
        assert _vr.get_data() == b'VIDEOBYTES', 'the video segment bytes were altered'
    finally:
        dtv_aac_gain.attenuate_ad_segment = _real_att
        directv_proxy._stream_upstream_response = _real_stream
    # Codec-gated: attenuate_ad_segment no-ops (returns the bytes unchanged) on non-AAC input,
    # so AC-3 content and national ads are never touched.
    assert dtv_aac_gain.attenuate_ad_segment(b'\x00' * 256) == b'\x00' * 256, \
        'attenuate_ad_segment must return non-AAC bytes unchanged (AC-3 content must pass through)'
finally:
    directv_proxy._requests = _real_requests
    dtv_android._client_device = _real_client_device

# (d) A DRM 403 on an Android TV session self-heals by REFRESHING (re-minting the activation
# token), not by the web client's wipe-tokens-and-re-login. Drive the relay's
# _directv_trigger_reauth and prove drm_reauth took over: refresh_session is called once and
# the web token-wipe path (which zeroes token_captured_at) is never reached.
from app.routes import play as _play  # noqa: E402
_real_sht = getattr(_play, '_amazon_sht_redis', None)
_real_refresh = dtv_android.refresh_session
_refresh_calls = []


def _fake_refresh(rt, did, *a, **k):
    _refresh_calls.append((rt, did))
    return {'bearer_token': 'NEW-BEARER', 'refresh_token': 'ROTATED-REFRESH',
            'captured_at': _t.time(), 'activation_token': 'FRESH-ACT', 'token_expires_at': None}


_play._amazon_sht_redis = lambda: None          # no redis in the --rm smoke container
dtv_android.refresh_session = _fake_refresh
try:
    _src = _types.SimpleNamespace(id=1, name='directv', config={
        'auth_method': 'dtv_android', 'refresh_token': 'OLD-REFRESH',
        'dtv_android_device_id': 'dev-1', 'bearer_token': 'OLD-BEARER',
        'activation_token': 'OLD-ACT', 'identity_cookie': 'OLD-CK', 'token_captured_at': 123.0})
    directv_proxy._directv_trigger_reauth(_src, 'license 1009 (smoke)')
    assert len(_refresh_calls) == 1, 'drm_reauth did not refresh the Android TV session (re-mint the token)'
    assert _src.config.get('bearer_token') == 'NEW-BEARER' and _src.config.get('activation_token') == 'FRESH-ACT', \
        'the re-minted bearer/activation token was not persisted'
    assert 'identity_cookie' not in _src.config, 'the stale identity cookie was not dropped'
    assert _src.config.get('token_captured_at') != 0, \
        'the web token-wipe recovery ran for an Android TV session (it must be skipped)'
finally:
    dtv_android.refresh_session = _real_refresh
    if _real_sht is not None:
        _play._amazon_sht_redis = _real_sht

print('Overlay smoke test passed.')
