"""DAI smoke test, run inside the freshly built image (cwd /app).

Imports the patched modules with the image's real dependencies and exercises the
DAI helpers with synthetic inputs, so a merge that compiles but no longer works
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


from app.scrapers import directv  # noqa: E402
from app.routes import api_sources  # noqa: E402,F401

scraper = directv.DirectvScraper

keys = {field.key for field in scraper.config_schema}
assert 'use_dai' in keys, 'use_dai toggle missing from the DirecTV config schema'
assert scraper.uses_dai({'use_dai': 'true'}) is True
assert scraper.uses_dai({'use_dai': False}) is False
assert scraper.uses_dai(None) is False

assert directv._gpp_targeted_ad_opt_out(us_national_gpp(1)) is True
assert directv._gpp_targeted_ad_opt_out(us_national_gpp(2)) is False
assert directv._gpp_targeted_ad_opt_out('') is None

config = {
    'dai_partner_profile_id': 'hh-1', 'dai_profile_id': 'prof-1', 'dai_dma_id': '999',
    'dai_gpp': us_national_gpp(1), 'dai_gpp_sid': '7',
    'dai_adid': 'ad-1', 'dai_fw_did': 'fw-1', 'dai_comscore_device': 'cs-1',
}
query = scraper._build_dai_query(config, {'123': 'testnet'}, '123')
expected = {'hhid': 'hh-1', 'u': 'hh-1', 'profid': 'prof-1', 'dma_location': '999',
            'dma_billing': '999', 'is_lat': '1', 'gpp_sid': '7', 'adid': 'ad-1',
            '_fw_did': 'fw-1', 'comscore_device': 'cs-1', 'net': 'testnet'}
for key, value in expected.items():
    assert query.get(key) == value, f'{key}: expected {value!r}, got {query.get(key)!r}'

# Consent from a different GPP section must not be decoded with the US-National layout.
other = scraper._build_dai_query({**config, 'dai_gpp_sid': '8'}, {}, '123')
assert 'is_lat' not in other, 'is_lat derived from a non-US-National GPP section'
assert 'net' not in other

# Values that weren't sourced from the account are omitted, never invented.
bare = scraper._build_dai_query({}, {}, '123')
for key in ('hhid', 'u', 'profid', 'dma_location', 'gpp', 'is_lat', 'adid'):
    assert key not in bare, f'{key} present without a sourced value'

claims = b64url(json.dumps({'partnerProfileId': 'pp-9', 'profileId': 'p-9'}).encode())
assert directv._ids_from_bearer_jwt(f'h.{claims}.s') == ('pp-9', 'p-9')
assert directv._ids_from_bearer_jwt('not-a-jwt') == (None, None)

minted = directv._ensure_dai_device_ids({})
assert set(minted) == {'dai_adid', 'dai_fw_did', 'dai_comscore_device'}
assert directv._ensure_dai_device_ids({k: 'x' for k in minted}) == {}

print('DAI smoke test passed.')
