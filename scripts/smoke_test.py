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

# The toggle is wired into the DirecTV source settings.
keys = {field.key for field in directv.DirectvScraper.config_schema}
assert 'use_dai' in keys, 'use_dai toggle missing from the DirecTV config schema'
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
    'dai_gpp': us_national_gpp(1), 'dai_gpp_sid': '7',
    'dai_adid': 'ad-1', 'dai_fw_did': 'fw-1', 'dai_comscore_device': 'cs-1',
}
query = dai.build_query(config, {'123': 'testnet'}, '123')
expected = {'hhid': 'hh-1', 'u': 'hh-1', 'profid': 'prof-1', 'dma_location': '999',
            'dma_billing': '999', 'is_lat': '1', 'gpp_sid': '7', 'adid': 'ad-1',
            '_fw_did': 'fw-1', 'comscore_device': 'cs-1', 'net': 'testnet'}
for key, value in expected.items():
    assert query.get(key) == value, f'{key}: expected {value!r}, got {query.get(key)!r}'

# Consent from a different GPP section must not be decoded with the US-National layout.
other = dai.build_query({**config, 'dai_gpp_sid': '8'}, {}, '123')
assert 'is_lat' not in other, 'is_lat derived from a non-US-National GPP section'
assert 'net' not in other

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
assert {'dai_adid', 'dai_fw_did', 'dai_comscore_device'} <= set(cfg)
assert dai.ensure_device_ids(cfg) == {}, 'device ids re-minted'

names = {}
dai.note_channel(names, {'daiChannelName': ' cnn '}, '1')
dai.note_channel(names, {'daiChannelName': ''}, '2')
assert names == {'1': 'cnn'}

print('DAI smoke test passed.')
