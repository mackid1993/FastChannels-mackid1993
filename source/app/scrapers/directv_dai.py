"""DirecTV ad insertion (DAI): the opt-in 'Use DirecTV ad insertion (DAI)' toggle.

When on, playback uses the stream DirecTV's own apps use: the Yospace
ad-insertion session (``streamURL``) instead of ``fallbackStreamUrl``, carrying
the account's own ad-targeting values (DMA, privacy consent, household/profile
ids) plus this device's own ad ids.
When off, everything behaves exactly as before.

Everything lives in this module so the hooks in directv.py, api_sources.py and
the sources page stay one-liners.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import threading
import time
from urllib.parse import quote

import requests

from .base import ConfigField

logger = logging.getLogger(__name__)

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
    # yo.lpa/yo.lp: live-pause mode, which the yospace.pool=livepause session
    # needs (the Osprey sends yo.lpa; without them no ads were inserted).
    'yo.fr': 'true', 'yo.lpa': 'true', 'yo.lp': 'true', 'yo.av': '5',
    # The Android TV app's own live Yospace params. Its com.att.tv config tree sets
    # YSLiveParams = {yo.d.cp, yo.vm}; it does NOT send yo.cps for a live tune. yo.cps
    # is a Content Playback Spec from the app's default/iOS config tree only
    # ('b.lp.d.s.<min>-3630.0x.s.n' with a computed <min> we can't know), and sending
    # it pinned Yospace to a band-limited inserted-ad profile (muffled ~7 kHz) that the
    # real Android TV app — and the Osprey — never get. Leaving it out, per the repo
    # rule: if we don't know the value, don't invent one. yo.vm is the ad-macro map,
    # base64 JSON with APPBUNDLE com.att.tv, sent verbatim. yo.vm is a pinned snapshot of
    # the app's bundled value: the ${...} inside are Yospace server-side macros (the app
    # does not substitute them either), so the blob goes as-is and never varies per
    # session. It drifts only on a DirecTV app update — re-extract YSLiveParams.yo.vm from
    # the new index.android.bundle then, alongside _APP_USER_AGENT's version.
    'yo.sl': '3', 'yo.d.cp': 'true',
    'yo.vm': 'WwogIHsKICAgICJERVNJUkVEX0RVUkFUSU9OX1NFQ1MiOiAiJHtERVNJUkVEX0RVUkFUSU9OX1NFQ1N9IiwKICAgICJNRVRBREFUQV9DQUlEIjogIiR7TUVUQURBVEEuQURWRVJUSVNJTkdfSUR9IiwKICAgICJNRVRBREFUQV9CUkVBS0lEIjogIjAiLAogICAgIkFQUEJVTkRMRSI6ICJjb20uYXR0LnR2IiwKICAgICJJTlZFTlRPUllTVEFURSI6ICJhdXRvcGxheWVkIgogIH0KXQ==',
}

CONFIG_FIELD = ConfigField(
    'use_dai', 'Use DirecTV ad insertion (DAI)',
    field_type='toggle', default='false',
    help_text=(
        'On = play the stream DirecTV\'s own apps use, with DirecTV\'s '
        'local ads in commercial breaks. Off = the national feed '
        'without DirecTV\'s inserted ads.'
    ),
)

CONFIG_FIELDS = (CONFIG_FIELD,)



def enabled(config: dict | None) -> bool:
    return str((config or {}).get('use_dai', '')).strip().lower() in {'1', 'true', 'yes', 'on'}


# ── Stream URL ───────────────────────────────────────────────────────────────

# DirecTV's Android TV app authorizes live channels on channel/v2 (its bundled
# endpoint config), which returns playbackData.streamUrls: [{groupName: 'DAI' |
# 'Data Center', URLs: [...]}] instead of v1's streamURL/fallbackStreamUrl.
_CHANNEL_AUTH_V2 = 'https://api.cld.dtvce.com/right/authorization/channel/v2'


def android_auth_request(session, params: dict, dai: bool, default_url: str) -> str:
    """With DAI on, make the channel authorization request the Android TV app's,
    not the web player's: no browser Origin/Referer, the app's User-Agent for this
    bridge device, and its query (startOver=false; no timeShiftEnabled or
    dualManifest, which only the web sends), on channel/v2. Verified to return
    the same stream and play token. Returns the URL to request."""
    if not dai:
        return default_url
    for header in ('Origin', 'Referer'):
        session.headers.pop(header, None)
    ua = player_user_agent()
    if ua:
        session.headers['User-Agent'] = ua
    params.pop('timeShiftEnabled', None)
    params.pop('dualManifest', None)
    params.setdefault('startOver', 'false')
    return _CHANNEL_AUTH_V2


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
        fw_did = device_ad_flags(_client_device()).get('_fw_did')
        if fw_did:
            return f'_fw_did={quote(fw_did, safe=",:~")}' in url
        return '_fw_did=android_id:' not in url
    return True


def request_flags(config: dict, channel_names: dict | None, ccid: str) -> dict | None:
    """The DAI flags for one tune, or None when DAI is off."""
    if not enabled(config):
        return None
    return build_query(config, channel_names or {}, ccid)


def build_query(config: dict, dai_channel_names: dict, ccid: str) -> dict:
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
    if config.get('dai_profile_id'):
        q['profid'] = config['dai_profile_id']
    if config.get('dai_dma_id'):
        q['dma_location'] = config['dai_dma_id']
        q['dma_billing'] = config['dai_dma_id']
    if config.get('dai_gpp'):
        q['gpp'] = config['dai_gpp']
    if config.get('dai_gpp_sid'):
        q['gpp_sid'] = config['dai_gpp_sid']
    zip_code = config.get('dai_zip') or _account_zip(config)
    if zip_code:
        q['bZipCode'] = zip_code

    # The ad id, is_lat and comscore_device come from the playback device, the way
    # DirecTV's Android TV app sets them (see device_ad_flags). Without is_lat=0
    # the ad server sends national spots only (no local or political ads).
    device = device_ad_flags(_client_device())
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


_DEVICE_CACHE: dict[str, tuple[float, dict]] = {}
_DEVICE_TTL = 3600

# One adb read per bridge device per hour; every device-derived value below
# (Android ID, is_lat, comscore_device, User-Agent) comes from these properties.
_DEVICE_PROPS = (
    ('android_id', 'settings get secure android_id'),
    ('limit_ad_tracking', 'settings get secure limit_ad_tracking'),
    ('release', 'getprop ro.build.version.release'),
    ('model', 'getprop ro.product.model'),
    ('board', 'getprop ro.product.board'),
    ('manufacturer', 'getprop ro.product.manufacturer'),
)


def _client_device() -> dict:
    """Build properties of the bridge device making the current request.

    Each device's values are saved on disk (directv_dai_devices.json) and every
    session uses the saved values, so a device keeps one stable identity even
    while it's asleep or adb is down. A device seen for the first time is read
    over adb right away. After that it's re-checked in the background about once
    an hour; if it really changed (factory reset gives a new Android ID, ad
    tracking toggled, firmware update), the saved values are updated and logged.
    A failed re-check changes nothing. {} when the request isn't from a known
    bridge device, or outside a request."""
    try:
        from flask import has_request_context, request
        ip = (request.remote_addr or '').strip() if has_request_context() else ''
    except Exception:
        ip = ''
    if not ip:
        return {}
    address = _bridge_address(ip)
    if not address:
        return {}
    now = time.time()
    hit = _DEVICE_CACHE.get(address)
    if hit and now < hit[0]:
        return hit[1]
    props = _saved_devices().get(address) or {}
    if props:
        _DEVICE_CACHE[address] = (now + _DEVICE_TTL, props)
        threading.Thread(target=_recheck_device, args=(address, props), daemon=True).start()
        return props
    props = _read_device(address)
    if props:
        _store_device(address, props)
        logger.info('[directv-dai] saved device values for %s', address)
    else:
        logger.warning('[directv-dai] adb read failed for %s; this session has no device id', address)
    _DEVICE_CACHE[address] = (now + (_DEVICE_TTL if props else _RETRY_TTL), props)
    return props


def _recheck_device(address: str, saved: dict) -> None:
    """Background re-check: update the saved values only if the device changed."""
    fresh = _read_device(address)
    if fresh and fresh != saved:
        changed = sorted(k for k in fresh if fresh.get(k) != saved.get(k))
        _store_device(address, fresh)
        _DEVICE_CACHE[address] = (time.time() + _DEVICE_TTL, fresh)
        logger.info('[directv-dai] device %s changed (%s); saved values updated', address, ', '.join(changed))


def _store_device(address: str, props: dict) -> None:
    devices = _saved_devices()
    devices[address] = props
    _save_devices(devices)


_RETRY_TTL = 60


def _bridge_address(ip: str) -> str | None:
    try:
        from .. import bridge_devices
        return next((d['address'] for d in bridge_devices.known_devices()[0]
                     if d.get('host') == ip or d.get('address', '').split(':')[0] == ip), None)
    except Exception as exc:
        logger.debug('[directv-dai] bridge device lookup failed: %s', exc)
        return None


def _read_device(address: str) -> dict:
    try:
        subprocess.run(['adb', 'connect', address], capture_output=True, timeout=8)
        r = subprocess.run(['adb', '-s', address, 'shell', '; '.join(cmd for _, cmd in _DEVICE_PROPS)],
                           capture_output=True, text=True, timeout=10)
    except Exception as exc:
        logger.debug('[directv-dai] adb read failed for %s: %s', address, exc)
        return {}
    values = [ln.strip() for ln in (r.stdout or '').splitlines()]
    props = {key: (values[i] if i < len(values) and values[i] != 'null' else '')
             for i, (key, _) in enumerate(_DEVICE_PROPS)}
    return props if props['model'] and len(props['android_id']) >= 8 else {}


def _devices_file() -> str:
    """directv_dai_devices.json next to FastChannels' SQLite database (the /data
    volume), so saved device values survive restarts and image updates."""
    uri = ''
    try:
        from flask import current_app
        uri = current_app.config.get('SQLALCHEMY_DATABASE_URI') or ''
    except Exception:
        pass
    folder = os.path.dirname(uri[len('sqlite:///'):]) if uri.startswith('sqlite:///') else ''
    return os.path.join(folder or '/data', 'directv_dai_devices.json')


def _saved_devices() -> dict:
    try:
        with open(_devices_file(), encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_devices(devices: dict) -> None:
    path = _devices_file()
    try:
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(devices, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug('[directv-dai] could not save device values: %s', exc)


def device_ad_flags(props: dict) -> dict:
    """The device ad flags, the same way on every Android TV device (Fire TV,
    Google TV, Shield):
    - comscore_device = <systemName>_<Build.MANUFACTURER>_<Build.MODEL>, whitespace
      removed, as the Android TV app builds it (universalYospaceParameters);
      systemName is "Android".
    - _fw_did=android_id:<Android ID> with is_lat=0. DirecTV's own prefix for an
      Android device id; every Android TV device exposes the Android ID over adb.
      (The app sends the platform advertising id instead, which a Google TV/Shield
      only exposes through Play services; to treat every device alike, no device
      uses its advertising id.) is_lat=0 is what gets local and political ads.
    - A device that limits ad tracking (Fire OS limit_ad_tracking=1) gets the
      app's opted-out form: google_advertising_id:optout, adid=optout, is_lat=1."""
    if not props:
        return {}
    flags = {'comscore_device': re.sub(r'\s+', '', f"Android_{props.get('manufacturer', '')}_{props.get('model', '')}")}
    if props.get('limit_ad_tracking') == '1':
        flags.update({'is_lat': '1', '_fw_did': 'google_advertising_id:optout', 'adid': 'optout'})
    elif len(props.get('android_id', '')) >= 8:
        flags.update({'is_lat': '0', '_fw_did': f"android_id:{props['android_id']}"})
    return flags


# DirecTV's Android TV app sends this User-Agent on its player's requests (Cronet,
# which the app has on): AnalyticsService::generateUserAgent() in libcpp_core.so,
# from Build.VERSION.RELEASE, Build.MODEL and Build.BOARD. APP_PROJECT_NAME is
# literal (DirecTV never fills that placeholder in) and there are two spaces
# before PureRN. 5.0.136.2002113867 is the app's versionName.
_APP_USER_AGENT = 'APP_PROJECT_NAME/5.0.136.2002113867 (Android {release}; {model}; {board})  PureRN/0.79.5'


def player_headers() -> dict:
    """{'User-Agent': <the DirecTV app UA>} for a bridge device, else {}."""
    ua = player_user_agent()
    return {'User-Agent': ua} if ua else {}


def player_user_agent() -> str | None:
    """The DirecTV app's User-Agent for the bridge device making the current
    request, or None when it isn't a known bridge device."""
    props = _client_device()
    if props.get('release') and props.get('model') and props.get('board'):
        return _APP_USER_AGENT.format(release=props['release'], model=props['model'], board=props['board'])
    return None


_ZIP_CACHE: dict[str, str] = {}


def _account_zip(config: dict) -> str | None:
    """The account's billing ZIP (for bZipCode) when login didn't store one yet,
    fetched once per process with the stored sign-in."""
    bearer = config.get('bearer_token')
    if not bearer:
        return None
    if bearer in _ZIP_CACHE:
        return _ZIP_CACHE[bearer] or None
    session = requests.Session()
    for c in config.get('cookies') or []:
        try:
            session.cookies.set(c['name'], c['value'], domain=c.get('domain') or None, path=c.get('path') or '/')
        except Exception:
            continue
    _ZIP_CACHE[bearer] = fetch_account_context(session, bearer).get('zip') or ''
    return _ZIP_CACHE[bearer] or None


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

def note_channel(scraper, row: dict, ccid: str) -> None:
    """Record the per-channel DAI "net" tag (the lineup's daiChannelName) in the
    scraper's cache, where resolve() reads it. Called once per lineup row."""
    value = (row or {}).get('daiChannelName')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        return
    names = scraper.cache.get('dai_channel_names')
    names = names if isinstance(names, dict) else {}
    if names.get(ccid) != value.strip():
        scraper._update_cache('dai_channel_names', {**names, ccid: value.strip()})


# ── Login ────────────────────────────────────────────────────────────────────

def login_fields(session, bearer: str, token_data: dict | None) -> dict:
    """DAI values captured at login on the curl_cffi path: the household/profile
    ids the web client sends (hhid == u == partnerProfileId in real captures) from
    the token exchange's valuePairs, plus the account's DMA and consent string."""
    vp = (token_data or {}).get('valuePairs')
    vp = vp if isinstance(vp, dict) else {}
    return {
        'partner_profile_id': (vp.get('partnerProfileId') or '').strip() or None,
        'profile_id': (vp.get('profileId') or '').strip() or None,
        'dai_context': fetch_account_context(session, bearer),
    }


def login_fields_from_cookies(captured: dict) -> dict:
    """The same account context for the Playwright login path, fetched with the
    captured bearer and cookies in a throwaway requests session. hhid/u/profid
    come from the bearer's own claims later (see store_login_result)."""
    bearer = captured.get('bearer_token')
    if not bearer:
        return {}
    session = requests.Session()
    for c in captured.get('cookies') or []:
        try:
            session.cookies.set(c['name'], c['value'],
                                domain=c.get('domain') or None, path=c.get('path') or '/')
        except Exception:
            continue
    return {'dai_context': fetch_account_context(session, bearer)}


def store_login_result(cfg: dict, result: dict) -> None:
    """Persist the account's own DAI values from a login result into the source
    config (used only when the toggle is on)."""
    partner_profile_id = result.get('partner_profile_id')
    profile_id = result.get('profile_id')
    # The Playwright path has no token-exchange valuePairs, so fall back to the
    # same ids from the bearer JWT's own claims. Also a safety net for curl_cffi.
    if not partner_profile_id or not profile_id:
        jwt_pp, jwt_pf = ids_from_bearer_jwt(result.get('bearer_token') or '')
        partner_profile_id = partner_profile_id or jwt_pp
        profile_id = profile_id or jwt_pf
    if partner_profile_id:
        cfg['dai_partner_profile_id'] = partner_profile_id
    if profile_id:
        cfg['dai_profile_id'] = profile_id
    for k, v in (result.get('dai_context') or {}).items():
        cfg[f'dai_{k}'] = v
    # The Android TV sign-in's stable device id, so later refreshes reuse the same
    # registered device instead of granting a new one each time.
    if result.get('dtv_android_device_id'):
        cfg['dtv_android_device_id'] = result['dtv_android_device_id']


def fetch_account_context(session, bearer: str) -> dict:
    """Best-effort fetch of the account's real DMA and privacy-consent string, the
    values the web client puts on its DAI session request. Uses the already-authed
    login session. Any failure returns {} — DAI playback still works, it just
    omits the fields we couldn't source rather than inventing them."""
    ctx: dict = {}
    hdrs = {
        'Authorization': f'Bearer {bearer}',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://stream.directv.com',
        'Referer': 'https://stream.directv.com/',
    }
    try:
        r = session.get(_LOCATION_URL, params={'includeTVOD': 'false'}, headers=hdrs, timeout=15)
        if r.ok:
            data = r.json()
            dma = _find_first_key(data, 'dmaId')
            if dma:
                ctx['dma_id'] = str(dma)
            billing = data.get('billingDmas') if isinstance(data, dict) else None
            zip_code = _find_first_key(billing, 'zipcode') or _find_first_key(data, 'zipcode')
            if zip_code:
                ctx['zip'] = str(zip_code)
    except Exception as exc:
        logger.debug('[directv-dai] location lookup failed: %s', exc)
    try:
        r = session.get(_BASICINFO_URL, params={'requestIds': 'true', 'requestShortIds': 'true'},
                        headers=hdrs, timeout=15)
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


# ── Settings ─────────────────────────────────────────────────────────────────

def clear_cache_if_toggled(source, old: dict, current: dict) -> None:
    """The toggle picks which stream URL resolve() caches (for 55 minutes), so
    drop every cached one when it changes; the next tune fetches under the new setting."""
    if getattr(source, 'name', None) == 'directv' and enabled(old) != enabled(current):
        from ..config_store import persist_source_cache_updates
        persist_source_cache_updates(source.id, {'directv_playback': {}})
