"""DAI smoke test, run inside the freshly built image (cwd /app).

Imports the patched modules with the image's real dependencies and exercises the
DAI code with synthetic inputs, so a merge that compiles but no longer works
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
assert {'dai', 'dai_extra'} <= set(inspect.signature(directv._fetch_channel_playback).parameters), \
    '_fetch_channel_playback lost its dai/dai_extra parameters'
resolve_src = inspect.getsource(directv.DirectvScraper.resolve)
for hook in ('directv_dai.cached_url_usable(', 'directv_dai.request_flags(', 'dai=directv_dai.enabled('):
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

# Inserted ads keep the channel's DRM key in force (no decoder teardown at ad boundaries).
wv = '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:x",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"'
media = (f'#EXTM3U\n{wv}\n#EXTINF:2,\nc1.m4a\n#EXT-X-DISCONTINUITY\n#EXT-X-KEY:METHOD=NONE\n'
         '#EXT-X-MAP:URI="ad-i.mp4"\n#EXTINF:2,\nad1.mp4\n')
kept = dai.keep_drm_session(media)
assert 'METHOD=NONE' not in kept and 'ad1.mp4' in kept and kept.count('SAMPLE-AES') == 1, kept
plain = '#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:2,\na.ts\n'
assert dai.keep_drm_session(plain) == plain, 'touched a playlist without a Widevine key'
assert 'directv_dai.keep_drm_session(' in inspect.getsource(directv_proxy.directv_browser_asset), \
    'the relay no longer keeps the DRM session through inserted ads'
# The relay sends the DirecTV app's own User-Agent, built from the bridge device's
# build properties (never the bare Custom-Exoplayer fallback, which stopped ad insertion).
assert not hasattr(dai, 'PLAYER_USER_AGENT'), 'the fixed Custom-Exoplayer User-Agent is back'
assert dai._APP_USER_AGENT.format(release='11', model='AFTKRT', board='karat') == \
    'APP_PROJECT_NAME/5.0.136.2002113867 (Android 11; AFTKRT; karat)  PureRN/0.79.5'
assert dai.player_user_agent() is None, 'outside a request there is no device to build a User-Agent for'
for fn in (directv_proxy.directv_browser_manifest, directv_proxy.directv_browser_asset):
    assert 'directv_dai.player_user_agent()' in inspect.getsource(fn), f'{fn.__name__} no longer sends the device User-Agent'
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
assert query.get('yo.lpa') == 'true' and query.get('yo.lp') == 'true', 'live-pause flags missing (no ads without them)'
assert 'yo.po' not in query, "the web player's yo.po must not be sent"
assert query.get('yo.sl') == '3' and query.get('yo.d.cp') == 'true' and query.get('yo.cps'), \
    "the Android TV app's live Yospace params are missing"
assert 'com.att.tv' in base64.b64decode(query.get('yo.vm', '')).decode(), \
    "yo.vm must carry the Android TV app's ad-macro map (APPBUNDLE com.att.tv)"
assert query.get('attnid') == 'dfw003' and query.get('p') == 'dfw', 'DirecTV app constants missing'
assert query.get('metr') == '1071', "metr must be DirecTV's TV device-class code"
assert 'comscore_device' not in query, 'no minted comScore device id'

# A bridge device's own Android ID and ad-tracking setting take over from the
# account consent: DirecTV's android_id fallback with is_lat=0 is what gets local ads.
real_client_device = dai._client_device
dai._client_device = lambda: {'is_lat': '0', '_fw_did': 'android_id:abcdef0123456789'}
dev = dai.build_query(config, {}, '123')
assert dev['is_lat'] == '0' and dev['_fw_did'] == 'android_id:abcdef0123456789' and 'adid' not in dev, dev
dev_url = dai.pick_stream_url({'streamURL': 'https://x.yospace.com/a.m3u8?yo.up=u'}, True, dev)
assert dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True)
dai._client_device = lambda: {'is_lat': '0', '_fw_did': 'android_id:ffffffffffffffff'}
assert not dai.cached_url_usable({'fallback_url': dev_url, 'dai': True}, True), 'another device reused a session'
dai._client_device = real_client_device

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
assert dai.pick_stream_url(pb, False, None) == plain, 'DAI off must use the fallback stream'
url = dai.pick_stream_url(pb, True, {'e': 'prod', 'yo.up': 'other', 'hhid': 'a b', 'x': None})
assert url.startswith(yo + '&yospace.pool=livepause'), url
assert '&e=prod' in url, 'e=prod dropped by a substring match against cdncpDevice='
assert url.count('yo.up=') == 1, 'an existing param was duplicated'
assert '&hhid=a%20b' in url and '&x=' not in url
url2 = dai.pick_stream_url(pb, True, {'_fw_did': 'android_id:abc', 'nielsen_dev_group': 'devgrp,STV'})
assert '&_fw_did=android_id:abc' in url2 and '&nielsen_dev_group=devgrp,STV' in url2, \
    "':' and ',' must stay literal like DirecTV's clients send them"
assert dai.pick_stream_url({'fallbackStreamUrl': plain}, True, {'e': 'prod'}) == plain

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

names = {}
dai.note_channel(names, {'daiChannelName': ' cnn '}, '1')
dai.note_channel(names, {'daiChannelName': ''}, '2')
assert names == {'1': 'cnn'}

print('DAI smoke test passed.')
