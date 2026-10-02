"""Android TV client for DirecTV.

Signs the DirecTV session in as DirecTV's own Android TV app (OAuth client
``UNIFIED_Android_TV_02``) via its device-code grant, instead of the web browser the
upstream scraper authenticates as (``UNIFIED_DTV_WEB``). The resulting bearer is an
Android TV token, so channel/v2 authorization, DRM activation and license all run as
the app — matching what the bridge device (a Fire TV / Google TV / Android TV stick)
actually is, rather than a web browser wearing an Android-TV hat on one request.

No manual step: the Android TV app's device-code grant normally shows a code the user
types at directv.com/tvsigninv2 while signed in. We automate that in-server — run the
existing web sign-in once to get an authenticated account bearer, use it to approve the
Android TV user code (``account/device/grant/usercode/validate``), then poll the grant's
token endpoint for the Android TV access/refresh/activation tokens. The web sign-in is
only a one-time approver per grant; it is never the playback identity.

Protocol verified from the Android TV app bundle (its QR/device-code config):
  start:   POST https://api.cld.dtvce.com/account/device/grant/v2/devicecode
  approve: POST https://api.cld.dtvce.com/account/device/grant/usercode/validate
  poll:    GET  https://api.cld.dtvce.com/account/device/grant/tokens

The DRM bodies (activate/license) and channel/v2 are already identical to the app, so
only the bearer (and the license headers, see ``license_headers``) change. The result
dict mirrors the web login's so run_directv_auth / store_login_result persist it
unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid

import requests


logger = logging.getLogger(__name__)

_CLIENT_ID = 'UNIFIED_Android_TV_02'
_GRANT_BASE = 'https://api.cld.dtvce.com/account/device/grant'
_DEVICECODE_URL = f'{_GRANT_BASE}/v2/devicecode'
_TOKENS_URL = f'{_GRANT_BASE}/tokens'
# A real Android TV client signs in once and then refreshes its token — it never
# re-logs-in. After the first device-code grant we keep the refresh token + the device
# id and refresh here instead of re-granting. Verified live and against a web HAR
# capture: POST authn-refreshgo/v3/refresh?clientID=<id> with a form body of clientMake,
# clientModel and refresh_token (snake_case). clientMake/clientModel are client
# descriptors the endpoint accepts as-is; the Android TV identity is clientID.
_REFRESH_URL = 'https://api.cld.dtvce.com/authn-refreshgo/v3/refresh'
_CLIENT_MAKE = 'Google'
_CLIENT_MODEL = 'Chrome'

# The user approves the device code themselves in a browser at directv.com/tvsigninv2
# (the grant's approval step is the web page's, not an API we can call), so the poll
# waits long enough for them to open it and enter the code. The code expires in ~600 s;
# we poll just under that, with the code shown in the admin status the whole time.
_POLL_DEADLINE = 540
_POLL_MIN_INTERVAL = 5

# If the token response ever gives us an expiry, refresh a little before it (the way the
# app does, via RefreshTokenBeforeExpirationMinutes). DirecTV's response carries no
# expiry and the token is opaque, so in practice we don't refresh on a timer at all — the
# token rides until a real request is rejected (401), which the reactive re-auth path
# turns into a refresh. The 401 is the check; there is no blind auto-refresh.
_REFRESH_BUFFER = 600


def _app_headers() -> dict:
    """Android TV request headers: the app's own User-Agent, no browser
    Origin/Referer. player_user_agent() builds it from a bridge device's build
    properties; outside a device request it may be empty, which is fine here."""
    headers = {'Accept': 'application/json, text/plain, */*'}
    ua = player_user_agent()
    if ua:
        headers['User-Agent'] = ua
    return headers


def license_headers(config: dict | None = None) -> dict:
    """Headers for the DRM activate/license requests on an Android TV session: the
    app User-Agent, no stream.directv.com Origin/Referer. Replaces the web
    license_request_headers so the DRM device isn't a browser."""
    return _app_headers()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_MAX_TOKEN_LIFE = 30 * 86400  # sanity bound: an access-token expiry beyond this is a
#                               misread field, not a real expiry — ignore it and fall back.


def token_expires_at(token_data: dict | None) -> float | None:
    """Absolute epoch (seconds) when the access token expires, read from the grant/
    refresh response so we can refresh just in time like the app does. The response
    field name wasn't recoverable from the (opaque-token) bundle, so accept the usual
    shapes — an absolute expiry or a duration — at the top level or in valuePairs, in
    seconds or milliseconds, but only within a sane window so an unrelated numeric field
    can't be mistaken for an expiry. Returns None when none is present or plausible
    (caller then falls back to the fixed window)."""
    td = token_data or {}
    vp = td.get('valuePairs') if isinstance(td.get('valuePairs'), dict) else {}
    now = time.time()
    for src in (td, vp):
        for k in ('exp', 'expireTime', 'expirationTime', 'expiresAt', 'expiry', 'accessTokenExpiration'):
            v = _num(src.get(k))
            if v is None:
                continue
            v = v / 1000 if v > 1e12 else v
            if now < v < now + _MAX_TOKEN_LIFE:
                return v
    for src in (td, vp):
        for k in ('expiresIn', 'expires_in', 'validFor', 'ttl', 'expiresInSeconds'):
            v = _num(src.get(k))
            if v is None or v <= 0:
                continue
            v = v / 1000 if v > 1e7 else v
            if v < _MAX_TOKEN_LIFE:
                return now + v
    return None


def token_stale(config: dict | None) -> bool:
    """Whether the stored session needs attention before a scrape. Stale only when there
    is no bearer at all — we do NOT refresh on a timer. The grant token is long-lived, so
    it rides until a tune's authorization actually rejects it, which resolve() recovers
    from inline (refresh + retry) so the tune still succeeds. If a real expiry ever shows
    up in the token response, refresh just before it."""
    config = config or {}
    if not config.get('bearer_token'):
        return True
    exp = _num(config.get('dtv_android_expires_at'))
    if exp:
        return time.time() > (exp - _REFRESH_BUFFER)
    return False


def _device_class_id() -> str:
    # A fresh id on the first sign-in registers this playback device; it is then
    # persisted (store_login_result) and reused by refresh_session, so the account
    # keeps one registered Android TV device instead of a new one on every refresh.
    return str(uuid.uuid4())


def start_device_code(session: requests.Session, device_class_id: str) -> dict:
    """Begin the Android TV device-code grant. Returns the user code to show, the
    verification URL, the device code to poll on, and the poll interval."""
    # creationFlow is sent by the app but its literal wasn't recoverable from the
    # bundle; the endpoint accepts the grant without it.
    body = {'clientId': _CLIENT_ID, 'deviceClassID': device_class_id}
    r = session.post(_DEVICECODE_URL, json=body, headers=_app_headers(), timeout=20)
    if r.status_code < 200 or r.status_code >= 300:
        logger.warning('[dtv-android] devicecode HTTP %s: %s', r.status_code, (r.text or '')[:300])
        raise _auth_error(f'device-code start failed HTTP {r.status_code}')
    data = r.json() if r.content else {}
    device_code = (data.get('deviceCode') or '').strip()
    user_code = (data.get('userCode') or '').strip()
    if not device_code or not user_code:
        logger.warning('[dtv-android] devicecode response missing fields: %s', sorted(data.keys()))
        raise _auth_error('device-code start response missing deviceCode/userCode')
    try:
        interval = max(_POLL_MIN_INTERVAL, int(float(data.get('pollingInterval') or _POLL_MIN_INTERVAL)))
    except Exception:
        interval = _POLL_MIN_INTERVAL
    return {'device_code': device_code, 'user_code': user_code, 'interval': interval,
            'display_url': (data.get('displayURL') or 'directv.com/tvsigninv2').strip(),
            # Full approval URL with the code embedded — clicking it lands on the
            # tvsigninv2 page pre-filled, so the user just confirms.
            'url': (data.get('url') or '').strip()}


def poll_tokens(session: requests.Session, device_code: str, interval: int) -> dict:
    """Poll the grant's token endpoint until the user approves the code and it yields
    tokens. Returns the raw token_data (access_token, refresh_token, valuePairs). The
    poll is keyed on the deviceCode alone (unauthenticated, the way the app polls)."""
    deadline = time.time() + _POLL_DEADLINE
    params = {'clientID': _CLIENT_ID, 'deviceCode': device_code}
    while time.time() < deadline:
        r = session.get(_TOKENS_URL, params=params, headers=_app_headers(), timeout=20)
        data = r.json() if r.content else {}
        if data.get('access_token') or data.get('accessToken'):
            return data
        time.sleep(interval)
    raise _auth_error('device code was not approved in time — start the sign-in again')


def capture_android_auth(username: str, password: str, *, on_status=None) -> dict:
    """First Android TV sign-in. Start a device-code grant and wait for the user to
    approve it in a browser at directv.com/tvsigninv2 — exactly like signing in a real
    Android TV device. After this one approval the session refreshes forever
    (refresh_session), so the user never signs in again. username/password are unused
    here (the user approves in their own browser); kept for the call signature."""
    from . import directv  # lazy: directv imports this module

    def _status(state: str, detail: str = '') -> None:
        if on_status:
            try:
                on_status(state, detail)
            except Exception:
                pass

    session = requests.Session()
    device_class_id = _device_class_id()
    dc = start_device_code(session, device_class_id)
    link = dc['url'] or dc['display_url']
    _status('running', f"To finish Android TV sign-in, open {link} in a browser signed in to "
                       f"your DirecTV account and approve this device — code {dc['user_code']}.")
    token_data = poll_tokens(session, dc['device_code'], dc['interval'])
    # Log field names only (never values) so the token's real expiry field is visible in
    # the logs and token_expires_at can be confirmed/extended without hammering DTV.
    logger.info('[dtv-android] grant token fields: top=%s vp=%s',
                sorted(token_data.keys()), sorted((token_data.get('valuePairs') or {}).keys()))

    bearer = (token_data.get('access_token') or token_data.get('accessToken') or '').strip()
    if not bearer:
        raise _auth_error('Android TV grant returned no access token')
    refresh = (token_data.get('refresh_token') or token_data.get('refreshToken') or '').strip()
    activation = directv._normalize_activation_token(
        ((token_data.get('valuePairs') or {}).get('activationToken') or '').strip()
    )
    _status('success', 'Captured DirecTV Android TV session.')
    return {
        'bearer_token': bearer,
        'refresh_token': refresh,
        'activation_token': activation,
        'cookies': [],
        'captured_at': time.time(),
        'auth_method': 'dtv_android',
        'dtv_android_device_id': device_class_id,
        'token_expires_at': token_expires_at(token_data),
        'token_data': token_data,
    }


def refresh_session(refresh_token: str, device_class_id: str, *, on_status=None) -> dict:
    """Refresh an Android TV session's bearer with its stored refresh token — no web
    sign-in, no device code, no re-approval (a real ATV client never re-logs-in). Keeps
    the same device id so the grant stays one registered device."""
    from . import directv  # lazy

    s = requests.Session()
    # Verified shape: clientID in the query string; clientMake, clientModel and
    # refresh_token (snake_case) in the form body.
    r = s.post(_REFRESH_URL, params={'clientID': _CLIENT_ID},
               data=[('clientMake', _CLIENT_MAKE), ('clientModel', _CLIENT_MODEL),
                     ('refresh_token', refresh_token)],
               headers={**_app_headers(), 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, timeout=30)
    if r.status_code < 200 or r.status_code >= 300:
        logger.warning('[dtv-android] refresh HTTP %s: %s', r.status_code, (r.text or '')[:300])
        raise _auth_error(f'token refresh failed HTTP {r.status_code}')
    token_data = r.json() if r.content else {}
    logger.info('[dtv-android] refresh token fields: top=%s vp=%s',
                sorted(token_data.keys()), sorted((token_data.get('valuePairs') or {}).keys()))
    bearer = (token_data.get('access_token') or token_data.get('accessToken') or '').strip()
    if not bearer:
        raise _auth_error('token refresh returned no access token')
    new_refresh = (token_data.get('refresh_token') or token_data.get('refreshToken') or refresh_token).strip()
    # activationToken may be absent on refresh; run_directv_auth keeps the stored one
    # when this is empty, so the DRM identity survives a refresh.
    activation = directv._normalize_activation_token(
        ((token_data.get('valuePairs') or {}).get('activationToken') or '').strip()
    )
    return {
        'bearer_token': bearer,
        'refresh_token': new_refresh,
        'activation_token': activation,
        'cookies': [],
        'captured_at': time.time(),
        'auth_method': 'dtv_android',
        'dtv_android_device_id': device_class_id,
        'token_expires_at': token_expires_at(token_data),
        'token_data': token_data,
    }


def refresh_in_place(scraper) -> bool:
    """Refresh the Android TV session right now and write the new tokens onto the
    scraper's config so the in-flight request (a tune) can retry and succeed. Used by
    resolve() when a channel authorization is rejected for an expired token, so the tune
    doesn't fail. Returns True on success, False to let the caller fall back to the
    normal background re-auth. Any error is swallowed into False — never worse than the
    existing behavior."""
    cfg = scraper.config or {}
    rt = (cfg.get('refresh_token') or '').strip()
    did = (cfg.get('dtv_android_device_id') or '').strip()
    if not rt or not did or cfg.get('auth_method') != 'dtv_android':
        return False
    try:
        result = refresh_session(rt, did)
    except Exception as exc:
        logger.warning('[dtv-android] inline refresh failed: %s', exc)
        return False
    updates = {
        'bearer_token': result['bearer_token'],
        'refresh_token': result.get('refresh_token') or rt,
        'token_captured_at': result['captured_at'],
    }
    if result.get('activation_token'):
        updates['activation_token'] = result['activation_token']
    if result.get('token_expires_at'):
        updates['dtv_android_expires_at'] = result['token_expires_at']
    for k, v in updates.items():
        scraper.config[k] = v          # in-memory, so the retry uses the new bearer
        scraper._update_config(k, v)    # queued for the play route to persist
    logger.info('[dtv-android] refreshed session inline for a tune (bearer rotated)')
    return True


def _load_source_config(source_id: int, app=None) -> dict:
    """Read the DirecTV source config (refresh token + device id) to decide refresh vs
    a fresh grant. Read-only; mirrors run_directv_auth's app handling so it works both
    on the admin thread (app passed) and the RQ worker (app is None)."""
    def _query() -> dict:
        from ..models import Source
        src = Source.query.get(source_id)
        return dict((src.config if src else None) or {})
    try:
        from flask import has_app_context
        if has_app_context():
            return _query()
    except Exception:
        pass
    if app is None:
        from app import create_app
        app = create_app()
    with app.app_context():
        return _query()


def sign_in(source_id: int, username: str, password: str, *, app=None, on_status=None) -> dict:
    """Entry point for run_directv_auth. Refreshes an existing Android TV session when
    one is stored (the normal case — ATV clients don't re-log-in); otherwise runs the
    one-time device-code grant. A failed refresh falls back to a fresh grant so a
    revoked/expired refresh token still recovers."""
    cfg = _load_source_config(source_id, app)
    refresh = (cfg.get('refresh_token') or '').strip()
    device_id = (cfg.get('dtv_android_device_id') or '').strip()
    if refresh and device_id and cfg.get('auth_method') == 'dtv_android':
        try:
            if on_status:
                on_status('running', 'Refreshing Android TV session…')
            return refresh_session(refresh, device_id, on_status=on_status)
        except Exception as exc:
            logger.warning('[dtv-android] refresh failed (%s); re-granting via device code', exc)
    return capture_android_auth(username, password, on_status=on_status)


def _auth_error(msg: str) -> Exception:
    """Raise the scraper's own auth error type so run_directv_auth reports it like any
    other sign-in failure. Imported lazily to avoid a circular import at module load."""
    from .directv import DirectvAuthError
    return DirectvAuthError(msg)


# ── Android TV device identity ────────────────────────────────────────────────
# Moved here from directv_dai so the Android login is self-contained: reading the
# bridge device, the app's User-Agent and the channel/v2 request are all part of
# BEING the Android TV client. DAI builds on these; they do not depend on DAI.

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


# ── Android TV channel authorization (channel/v2) ─────────────────────────────
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
