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

import logging
import time
import uuid

import requests

from . import directv_dai

logger = logging.getLogger(__name__)

_CLIENT_ID = 'UNIFIED_Android_TV_02'
_GRANT_BASE = 'https://api.cld.dtvce.com/account/device/grant'
_DEVICECODE_URL = f'{_GRANT_BASE}/v2/devicecode'
_TOKENS_URL = f'{_GRANT_BASE}/tokens'
# A real Android TV client signs in once and then refreshes its token — it never
# re-logs-in. After the first device-code grant we keep the refresh token + the
# device id and refresh here instead of re-granting. Exact refresh body wasn't
# recoverable from the bundle; this mirrors the token-exchange convention
# (authn-refreshgo/v3). If a live refresh 4xx's, adjust from its response.
_REFRESH_URL = 'https://api.cld.dtvce.com/authn-refreshgo/v3/tokens'

# The user approves the device code themselves in a browser at directv.com/tvsigninv2
# (the grant's approval step is the web page's, not an API we can call), so the poll
# waits long enough for them to open it and enter the code. The code expires in ~600 s;
# we poll just under that, with the code shown in the admin status the whole time.
_POLL_DEADLINE = 540
_POLL_MIN_INTERVAL = 5


def _app_headers() -> dict:
    """Android TV request headers: the app's own User-Agent, no browser
    Origin/Referer. player_user_agent() builds it from a bridge device's build
    properties; outside a device request it may be empty, which is fine here."""
    headers = {'Accept': 'application/json, text/plain, */*'}
    ua = directv_dai.player_user_agent()
    if ua:
        headers['User-Agent'] = ua
    return headers


def license_headers(config: dict | None = None) -> dict:
    """Headers for the DRM activate/license requests on an Android TV session: the
    app User-Agent, no stream.directv.com Origin/Referer. Replaces the web
    license_request_headers so the DRM device isn't a browser."""
    return _app_headers()


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
            'display_url': (data.get('displayURL') or 'directv.com/tvsigninv2').strip()}


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
    _status('running', f"In a browser, go to {dc['display_url']} and enter code "
                       f"{dc['user_code']} (signed in to your DirecTV account), then approve this device.")
    token_data = poll_tokens(session, dc['device_code'], dc['interval'])

    bearer = (token_data.get('access_token') or token_data.get('accessToken') or '').strip()
    if not bearer:
        raise _auth_error('Android TV grant returned no access token')
    refresh = (token_data.get('refresh_token') or token_data.get('refreshToken') or '').strip()
    activation = directv._normalize_activation_token(
        ((token_data.get('valuePairs') or {}).get('activationToken') or '').strip()
    )
    account = requests.Session()
    account.headers.update(_app_headers())
    _status('success', 'Captured DirecTV Android TV session.')
    return {
        'bearer_token': bearer,
        'refresh_token': refresh,
        'activation_token': activation,
        'cookies': [],
        'captured_at': time.time(),
        'auth_method': 'dtv_android',
        'dtv_android_device_id': device_class_id,
        **directv_dai.login_fields(account, bearer, token_data),
    }


def refresh_session(refresh_token: str, device_class_id: str, *, on_status=None) -> dict:
    """Refresh an Android TV session's bearer with its stored refresh token — no web
    sign-in, no device code, no re-approval (a real ATV client never re-logs-in). Keeps
    the same device id so the grant stays one registered device."""
    from . import directv  # lazy

    s = requests.Session()
    r = s.post(_REFRESH_URL, data=[
        ('clientID', _CLIENT_ID),
        ('refreshToken', refresh_token),
        ('deviceClassID', device_class_id),
        ('reqParams', 'DEVICEID'),
        ('reqParams', 'AUTHGROUPS'),
    ], headers={**_app_headers(), 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}, timeout=30)
    if r.status_code < 200 or r.status_code >= 300:
        logger.warning('[dtv-android] refresh HTTP %s: %s', r.status_code, (r.text or '')[:300])
        raise _auth_error(f'token refresh failed HTTP {r.status_code}')
    token_data = r.json() if r.content else {}
    bearer = (token_data.get('access_token') or token_data.get('accessToken') or '').strip()
    if not bearer:
        raise _auth_error('token refresh returned no access token')
    new_refresh = (token_data.get('refresh_token') or token_data.get('refreshToken') or refresh_token).strip()
    # activationToken may be absent on refresh; run_directv_auth keeps the stored one
    # when this is empty, so the DRM identity survives a refresh.
    activation = directv._normalize_activation_token(
        ((token_data.get('valuePairs') or {}).get('activationToken') or '').strip()
    )
    account = requests.Session()
    account.headers.update(_app_headers())
    return {
        'bearer_token': bearer,
        'refresh_token': new_refresh,
        'activation_token': activation,
        'cookies': [],
        'captured_at': time.time(),
        'auth_method': 'dtv_android',
        'dtv_android_device_id': device_class_id,
        **directv_dai.login_fields(account, bearer, token_data),
    }


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
