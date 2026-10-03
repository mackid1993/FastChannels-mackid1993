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

# The Android TV app refreshes every ~55 min (its bundled minRefreshTimeout) and re-mints
# its DRM activation token on each refresh (reqParams=ACTIVATIONTOKEN — see
# refresh_session), so DRM never meets the activation JWT's ~2 h expiry. We do the same:
# background_refresh_due()/pre_run_setup refresh proactively on that cadence, and the
# reactive paths (resolve + drm_reauth) refresh on an actual rejection. If a token response
# ever carries a real expiry we refresh this many seconds before it.
_REFRESH_BUFFER = 600
_REFRESH_INTERVAL = 55 * 60   # the app's minRefreshTimeout: keep the session warm so a tune
#                               never meets a token near its ~2 h activation-JWT expiry.
_DRM_REAUTH_COOLDOWN = 120    # single-flight window for drm_reauth across the sticks' retries


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


def background_refresh_due(config: dict | None) -> bool:
    """True when a *usable* Android TV session is old enough to refresh proactively, the
    way the app refreshes every ~55 min. Drives pre_run_setup's background refresh so the
    DRM activation token is re-minted before any tune needs it — and so a container that
    was off for days refreshes itself on the next scrape instead of meeting a dead token.
    False when there is nothing to refresh with (no bearer/refresh/device id); the reactive
    paths (resolve + drm_reauth) still cover a session that goes stale between scrapes."""
    config = config or {}
    if (config.get('auth_method') or '') != 'dtv_android':
        return False
    if not (config.get('bearer_token') and config.get('refresh_token')
            and config.get('dtv_android_device_id')):
        return False
    exp = _num(config.get('dtv_android_expires_at'))
    if exp:
        return time.time() > (exp - _REFRESH_BUFFER)
    captured = _num(config.get('token_captured_at')) or 0.0
    return (time.time() - captured) >= _REFRESH_INTERVAL


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
    # The Android TV app's own refresh builder (index.android.bundle, AuthenticationPath.
    # Refresh) sets exactly clientMake, clientModel and refresh_token (snake_case) in the
    # form body, PLUS reqParams=ACTIVATIONTOKEN; clientID stays in the query (verified
    # live). That one reqParams is what makes authn-refreshgo return a FRESH activationToken
    # in valuePairs — the app re-mints the DRM activation token on every refresh so DRM
    # survives the activation JWT's ~2 h expiry without a re-login. (Login/token requests
    # use reqParams=DEVICEID,AUTHGROUPS; the refresh sends exactly ['ACTIVATIONTOKEN'].
    # extraParams → repeated urlencoded reqParams fields. The refresh builder sets no
    # deviceClassID, so we send none either.)
    r = s.post(_REFRESH_URL, params={'clientID': _CLIENT_ID},
               data=[('clientMake', _CLIENT_MAKE), ('clientModel', _CLIENT_MODEL),
                     ('refresh_token', refresh_token), ('reqParams', 'ACTIVATIONTOKEN')],
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
    # The refresh now re-mints activationToken (reqParams=ACTIVATIONTOKEN above), so this is
    # normally present and fresh. If a response ever omits it, run_directv_auth keeps the
    # stored one — so a refresh is never worse than before for DRM.
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
    one is stored WITH its DRM activation token (the normal case — ATV clients don't
    re-log-in); otherwise (first sign-in, or the activation token was lost) runs the
    one-time device-code grant, the only path that restores the activation token. A
    failed refresh also falls back to a fresh grant so a revoked/expired refresh token
    still recovers."""
    cfg = _load_source_config(source_id, app)
    refresh = (cfg.get('refresh_token') or '').strip()
    device_id = (cfg.get('dtv_android_device_id') or '').strip()
    # The refresh re-mints the DRM activation token (reqParams=ACTIVATIONTOKEN — see
    # refresh_session), so a stored Android TV session recovers end to end by refreshing,
    # even when the activation token is missing or expired. A real ATV client never
    # re-logs-in. Fall through to the one-time device-code grant only when there is no
    # refreshable session, or the refresh token itself is dead.
    if refresh and device_id and cfg.get('auth_method') == 'dtv_android':
        try:
            if on_status:
                on_status('running', 'Refreshing Android TV session…')
            result = refresh_session(refresh, device_id, on_status=on_status)
            # Accept the refresh only if DRM stays usable: it re-minted an activation token,
            # or one is still stored (run_directv_auth keeps the stored one when the refresh
            # omits it). If neither, the session can't play, so fall through to the grant.
            # Testing "still stored" (not "the refresh omitted it") keeps a background
            # refresh from launching a device-code grant nobody is there to approve.
            if (result.get('activation_token') or '').strip() or (cfg.get('activation_token') or '').strip():
                return result
            logger.warning('[dtv-android] refresh returned no activation token and none is stored; '
                           'running the one-time device-code grant')
        except Exception as exc:
            logger.warning('[dtv-android] refresh failed (%s); re-granting via device code', exc)
    return capture_android_auth(username, password, on_status=on_status)


def drm_reauth(source, reason: str) -> bool:
    """Self-healing DRM-failure recovery for an Android TV session. The relay's upstream
    _directv_trigger_reauth calls this first (a one-line hook); returning True means we
    handled it and the caller skips its old web-client recovery (wipe the tokens + a
    browser re-login), which can't help an Android TV session. Returns False for a
    non-Android-TV session so that old path still runs.

    How it heals, with no manual step:
      * A DRM 403 is either a stale identity cookie (license 1009) or an expired activation
        token (activation 1002). Both are cured by minting a FRESH activation token, which
        a refresh now does (reqParams=ACTIVATIONTOKEN — see refresh_session).
      * So we refresh the session in place (new bearer + fresh activation token), drop the
        cached identity cookie, and let the player's next retry re-activate with the fresh
        token. Single-flight behind a redis lock so the sticks' 2-3 s retries don't fire
        concurrent refreshes (which rotate the refresh token and could trip reuse-detection).
      * Only if the refresh itself fails (the refresh token is dead or absent) can we not
        self-heal: we clear the dead activation token — safe here because we return True so
        there is no re-login storm — and log that the user must re-authenticate once (the
        one-time device-code grant in source settings), which mints a new token.
    """
    cfg = dict(source.config or {})
    if (cfg.get('auth_method') or '') != 'dtv_android':
        return False

    from ..routes import directv_proxy, play
    from ..extensions import db
    try:
        rdb = play._amazon_sht_redis()
    except Exception:
        rdb = None

    def _flush_redis_cookie() -> None:
        # Drop the cached identity cookie so the next license request re-activates with the
        # (now fresh) token. Best-effort; never crash the relay.
        try:
            if rdb is not None:
                sid, _ = directv_proxy._directv_device_session_id()
                rdb.delete(directv_proxy._directv_identity_cache_key(source.id, sid))
        except Exception:
            logger.debug('[dtv-android] could not flush cached DRM identity after %s', reason, exc_info=True)

    # Single-flight: one refresh per source across the 2-3 s retry storm and both sticks.
    # Share the key _start_background_reauth uses so a background refresh and a
    # DRM-triggered refresh can't run concurrently with the same refresh token (which could
    # trip DirecTV's refresh reuse-detection and revoke the session).
    have_lock = True
    if rdb is not None:
        try:
            have_lock = bool(rdb.set(f'directv:auth:refreshing:{source.name}', '1', nx=True, ex=_DRM_REAUTH_COOLDOWN))
        except Exception:
            have_lock = True  # redis down: don't wedge recovery, just proceed unlocked
    if not have_lock:
        logger.info('[dtv-android] %s — a DRM refresh is already in flight; dropping cached identity', reason)
        _flush_redis_cookie()
        return True

    rt = (cfg.get('refresh_token') or '').strip()
    did = (cfg.get('dtv_android_device_id') or '').strip()
    result = None
    if rt and did:
        try:
            result = refresh_session(rt, did)
        except Exception as exc:
            logger.warning('[dtv-android] DRM-recovery refresh failed after %s: %s', reason, exc)

    # Healed only if the refresh actually returned a fresh activation token — that is what
    # the next re-activation needs. The reqParams=ACTIVATIONTOKEN refresh is verified from
    # the app bundle, not live, so degrade honestly if DirecTV ever omits it.
    minted = bool(result and (result.get('activation_token') or '').strip())
    try:
        fresh = dict(source.config or {})
        if result:
            # Always persist the rotated bearer + refresh token so we never orphan the
            # rotated refresh token, even when no activation token came back.
            fresh['bearer_token'] = result['bearer_token']
            fresh['refresh_token'] = result.get('refresh_token') or rt
            fresh['token_captured_at'] = result['captured_at']
            if result.get('token_expires_at'):
                fresh['dtv_android_expires_at'] = result['token_expires_at']
        if minted:
            fresh['activation_token'] = result['activation_token']
        else:
            # No fresh token (refresh failed, or succeeded but returned none): the stored
            # token is dead. Clear it so the "Authenticate" button runs a fresh device-code
            # grant, which mints a new one. Safe here — we return True, so no re-login storm.
            fresh.pop('activation_token', None)
        fresh.pop('identity_cookie', None)
        fresh.pop('identity_cookie_expires_at', None)
        source.config = fresh
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.debug('[dtv-android] could not persist DRM recovery after %s', reason, exc_info=True)
    _flush_redis_cookie()

    if minted:
        logger.info('[dtv-android] %s — refreshed the Android TV session and re-minted the DRM '
                    'activation token; the next tune re-activates automatically', reason)
    elif result:
        logger.warning('[dtv-android] %s — refresh succeeded but returned no DRM activation token; '
                       're-authenticate once in source settings (Log out, then Authenticate)', reason)
    else:
        logger.warning('[dtv-android] %s and no working refresh token — re-authenticate once in '
                       'source settings (Log out, then Authenticate)', reason)
    return True


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
