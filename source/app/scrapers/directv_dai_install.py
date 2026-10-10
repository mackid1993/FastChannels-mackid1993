"""Wires DirecTV DAI into upstream FastChannels at runtime, from ONE line in create_app.

Upstream's files carry a single hook (``directv_dai_install.install(app)`` in
``create_app``, inserted by scripts/apply-patches.sh, never by a patch hunk). Everything
else is attached here, and as little of it as possible depends on upstream's internal
names:

- The relay is hooked at the HTTP layer, not by function name: a Flask ``after_request``
  swaps inserted AC-3 ads to their AAC twins in every DirecTV playlist response under
  ``/play/directv/``, a ``before_request`` serves inserted-ad AAC segments with the
  loudness cut, and the app User-Agent is added at the ``requests`` library level
  (``requests.Session.request``) for relay fetches to DirecTV CDN hosts. Upstream can
  rename or restructure its relay functions freely.
- The settings (DAI toggle, viewer-profile picker, "Capture advertising IDs") are our
  own panel, served by our own blueprint and saved through our own route. The script
  adds the panel inside each DirecTV source's settings box, and the same panel is at
  ``/directv-dai`` on its own page, so no upstream template function is touched.
- What can't be avoided is wrapped by name: the scraper's tune path (resolve, the
  channel authorization, the license fallback, the lineup rows), apply_auth_result
  and the code sign-in's result (for the household id).

Rules every hook follows:
- DAI off -> upstream's behavior, untouched.
- Any error in our code -> log it and fall back to upstream's behavior, so drift
  degrades to "DAI off", never to broken playback.
- Breakage is reported, never silent: a missing name (``missing()``), a wrap whose
  target changed shape (``_failed``), and at runtime a tune that should have been DAI
  but wasn't (``_note`` -> ``status()['runtime']``). CI (validate.sh / smoke_test.py)
  fails on the first two; the admin panel shows all three in red.

Module-level patches are applied once per process (create_app runs several times:
the web app, the RQ worker, run_directv_auth's own app); per-app pieces (hooks, the
blueprint) once per app.
"""
from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import logging
import re
import time
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

# EVERY upstream name the overlay touches, keyed by a ROLE that never changes. The
# overlay's code and its tests reach upstream only through these roles (up(role)), never
# by writing upstream's names elsewhere, so when upstream renames or moves something the
# whole fix is the one (module, attribute path) value of its role here.
#
# TARGETS: names install() wraps or changes in place.
TARGETS = {
    'channel_fetch': ('app.scrapers.directv', '_fetch_channel_playback'),
    'apply_auth_result': ('app.scrapers.directv', 'apply_auth_result'),
    'resolve': ('app.scrapers.directv', 'DirectvScraper.resolve'),
    'license_request': ('app.scrapers.directv', 'DirectvScraper.prepare_license_request'),
    'lineup_rows': ('app.scrapers.directv', 'DirectvScraper._fetch_allchannels_rows'),
    'code_signin_result': ('app.scrapers.directv_device_auth', '_result'),
    'relay_cdn_suffixes': ('app.routes.directv_proxy', '_DIRECTV_BROWSER_CDN_SUFFIXES'),
}
# USES: names the overlay's modules call or read without wrapping them.
USES = {
    'license_content_id': ('app.scrapers.directv', '_license_content_id_from_stream_url'),
    'auth_expired_error': ('app.scrapers.directv', 'DirectvAuthExpiredError'),
    'code_signin_method': ('app.scrapers.directv_device_auth', 'AUTH_METHOD'),
    'app_headers': ('app.scrapers.directv_device_auth', 'app_headers'),
    'app_user_agent': ('app.scrapers.directv_device_auth', 'APP_USER_AGENT'),
    'code_signin_client_id': ('app.scrapers.directv_device_auth', '_CLIENT_ID'),
    'playback_cache_ttl': ('app.scrapers.directv', '_PLAYBACK_CACHE_TTL'),
    'token_stale': ('app.scrapers.directv', 'DirectvScraper._token_stale'),
    'can_reauth': ('app.scrapers.directv', 'DirectvScraper.can_reauth'),
    'start_reauth': ('app.scrapers.directv', 'DirectvScraper._start_background_reauth'),
    'is_code_signin': ('app.scrapers.directv_device_auth', 'is_device_session'),
    'relay_cdn_allowed': ('app.routes.directv_proxy', '_directv_browser_cdn_allowed'),
    'scraper_cache': ('app.scrapers.base', 'BaseScraper.cache'),
    'scraper_update_cache': ('app.scrapers.base', 'BaseScraper._update_cache'),
    'known_devices': ('app.bridge_devices', 'known_devices'),
    'persist_cache': ('app.config_store', 'persist_source_cache_updates'),
    'persist_config': ('app.config_store', 'persist_source_config_updates'),
    'source_model': ('app.models', 'Source'),
    'db': ('app.extensions', 'db'),
}
# The relay's URL space. The Player app and Channels fetch these URLs, so they're the
# most stable contract upstream has; the HTTP-layer hooks key on this prefix only.
RELAY_PREFIX = '/play/directv/'
# Strings in upstream's files the wiring relies on without calling them by name:
# (file, text, what breaks without it). validate.sh, smoke_test.py and status() check these.
SOURCE_MARKERS = (
    ('app/templates/admin/sources.html', 'id="source-config-{{ source.id }}"',
     'the DAI panel is added inside each source\'s settings box (it is still at /directv-dai)'),
    ('app/scrapers/directv.py', 'directv:auth:refreshing:',
     "the profile swap waits on upstream's token-refresh lock (this key)"),
)
# The fetch arguments the DAI request needs by name (inspect.signature binds them).
# cookies and client_context are read too when upstream still passes them; the code
# sign-in has neither, so their removal mustn't turn DAI off.
_FETCH_ARGS = ('bearer_token', 'ccid')

_CHANNEL_AUTH_V2 = 'https://api.cld.dtvce.com/right/authorization/channel/v2'
# Each bridge device's own DAI session per channel ({address: {ccid: entry}}), beside
# upstream's per-channel directv_playback, so two boxes on one channel don't keep
# replacing each other's session (each needs its own: the device id is in the URL).
DEVICE_SESSIONS = 'dai_playback_by_device'
# A box's own session lives exactly as long as upstream keeps its per-channel playback
# (its _PLAYBACK_CACHE_TTL, sized under DirecTV's entitlement), never longer, so the
# license wrapper never hands out a play token upstream would already have refetched.
# If that role can't be read (missing() reports it), boxes simply don't reuse sessions.
def _device_session_ttl() -> float:
    try:
        return float(up('playback_cache_ttl'))
    except Exception:
        return 0.0
_ABS_URL = re.compile(r'https?://[^\s"]+')

# The scraper (config + channel names + a result slot) behind the current
# _fetch_channel_playback call. Set by the resolve()/prepare_license_request() wrappers,
# since upstream's fetch takes no config.
_TUNE = contextvars.ContextVar('directv_dai_tune', default=None)

_process_patched = False
_failed: list[str] = []   # wiring steps that raised or found a changed shape (named in status())


def _resolve(module_name: str, path: str):
    import importlib
    obj = importlib.import_module(module_name)
    for part in path.split('.'):
        obj = getattr(obj, part)
    return obj


def _where(role: str) -> tuple[str, str]:
    return TARGETS.get(role) or USES[role]


def up(role: str):
    """Upstream's object for a role (see TARGETS/USES), looked up now."""
    return _resolve(*_where(role))


def up_name(role: str) -> str:
    """The attribute name of a role's object (the last part of its path)."""
    return _where(role)[1].rpartition('.')[2]


def up_owner(role: str):
    """The module or class holding a role's object, to read or replace it in place."""
    import importlib
    module_name, path = _where(role)
    head = path.rpartition('.')[0]
    return _resolve(module_name, head) if head else importlib.import_module(module_name)


def up_set(role: str, value) -> None:
    setattr(up_owner(role), up_name(role), value)


def missing() -> list[str]:
    """TARGETS and USES that don't exist in this upstream (empty = everything present)."""
    gone = []
    for module_name, path in (*TARGETS.values(), *USES.values()):
        try:
            _resolve(module_name, path)
        except Exception:
            gone.append(f'{module_name}.{path}')
    return gone


def missing_markers(root: str) -> list[str]:
    """SOURCE_MARKERS not found under ``root`` (the app checkout), as readable messages."""
    import os
    gone = []
    for path, text, why in SOURCE_MARKERS:
        try:
            with open(os.path.join(root, path), encoding='utf-8') as f:
                if text in f.read():
                    continue
        except Exception:
            pass
        gone.append(f'{path} no longer has {text!r}: {why}')
    return gone


def missing_relay(app) -> list[str]:
    """The relay hooks need upstream to serve DirecTV under RELAY_PREFIX."""
    try:
        if any(r.rule.startswith(RELAY_PREFIX) for r in app.url_map.iter_rules()):
            return []
    except Exception:
        pass
    return [f'no upstream route under {RELAY_PREFIX} any more: the ad audio swap and loudness cut are off']


def status(app=None) -> dict:
    """What's not wired, for the admin panel's warning and for CI: upstream names that
    are gone, upstream strings that are gone, wiring steps that failed, and runtime
    problems (DAI on, yet a tune didn't get a DAI stream). Empty lists = all good."""
    import os
    root, markers, relay = '', [], []
    try:
        from flask import current_app
        app = app or current_app._get_current_object()
        root = os.path.dirname(app.root_path)
        relay = missing_relay(app)
    except Exception:
        pass
    if root:
        markers = missing_markers(root)
    return {'missing': missing(), 'markers': markers + relay,
            'failed': sorted(set(_failed + identity_problems())), 'runtime': runtime_warnings()}


def identity_problems() -> list[str]:
    """The one client identity the session presents must hold together: comscore_device
    names the device upstream's fixed app User-Agent presents. If upstream changes that
    string to a device directv_dai_device can't name, comscore_device would quietly fall
    back to each box's own values; say so instead (CI and the panel)."""
    try:
        from . import directv_dai_device
        ua = up('app_user_agent')
    except Exception:
        return []   # the role itself is gone: missing() names it
    try:
        if directv_dai_device.presented_device() is None:
            return [f"comscore_device: upstream's app User-Agent ({str(ua)[:120]!r}) names a "
                    'device directv_dai_device._UA_MANUFACTURERS does not map; comscore_device falls back to '
                    "each box's own values (add its board)"]
    except Exception:
        return ['comscore_device: could not read the device upstream\'s app User-Agent presents']
    return []


def install(app) -> None:
    """The one hook. Never raises: a failure here leaves upstream exactly as it was."""
    global _process_patched
    try:
        for name in missing():
            logger.error('[directv-dai] upstream moved %s; that part of DAI is not wired', name)
        if not _process_patched:
            _process_patched = True
            for step in (_patch_scraper, _patch_device_auth, _patch_relay_hosts, _patch_user_agent):
                _try(step)
        if 'directv_dai' not in app.extensions:
            app.extensions['directv_dai'] = True
            for step in (_register_relay_hooks, _register_admin, _register_migration):
                _try(step, app)
    except Exception:
        logger.exception('[directv-dai] install failed; DAI is off')


def _try(step, *args) -> None:
    try:
        step(*args)
    except Exception:
        _fail(step.__name__.lstrip('_'))
        logger.exception('[directv-dai] could not wire %s', step.__name__)


def _fail(what: str) -> None:
    _failed.append(what)
    logger.error('[directv-dai] wiring problem: %s', what)


def _wraps(original, wrapper):
    wrapper = functools.wraps(original)(wrapper)
    wrapper.__directv_dai__ = True
    return wrapper


def _already(fn) -> bool:
    return getattr(fn, '__directv_dai__', False) or getattr(getattr(fn, '__func__', None), '__directv_dai__', False)


# ── Runtime health: DAI on, but did the tune really get DAI? ──────────────────────
# Every hook falls back to upstream on trouble, so a broken hook looks like "DAI off".
# These notes make it visible: the admin panel shows a red line when the most recent
# problem is newer than the most recent good DAI tune. Stored in redis (shared by every
# gunicorn worker) when it's reachable, else per process.

_HEALTH_KEY = 'directv_dai:health'
_HEALTH_TTL = 7 * 24 * 3600
# problem kind -> (what the panel says, the good kind that clears it when newer)
_PROBLEMS = {
    'bypassed': ('DAI is on, but a tune never reached the DAI request (upstream changed its tune path)', 'ok'),
    'error': ('a DAI hook raised (see the server log)', 'ok'),
    'fallback': ('the last DAI tune fell back to the non-DAI stream', 'ok'),
    'ad_uncut': ('an inserted ad went out without the loudness cut', 'ad_cut'),
}
_local_health: dict[str, dict] = {}
_rdb = [None, 0.0]


def _redis():
    now = time.time()
    if _rdb[0] is not None or now < _rdb[1]:
        return _rdb[0]
    try:
        import redis as _redis_mod
        from flask import current_app
        _rdb[0] = _redis_mod.from_url(current_app.config['REDIS_URL'],
                                      socket_connect_timeout=1, socket_timeout=1)
        _rdb[0].ping()
    except Exception:
        _rdb[0] = None
        _rdb[1] = now + 60   # no redis: don't retry on every note
    return _rdb[0]


def _note(kind: str, detail: str = '') -> None:
    now = time.time()
    detail = (detail or '')[:300]
    try:
        rdb = _redis()
        if rdb is not None:
            pipe = rdb.pipeline(transaction=False)
            pipe.hset(_HEALTH_KEY, mapping={f'{kind}.at': now, f'{kind}.detail': detail})
            pipe.hincrby(_HEALTH_KEY, f'{kind}.count', 1)
            pipe.expire(_HEALTH_KEY, _HEALTH_TTL)
            pipe.execute()
            return
    except Exception:
        _rdb[0], _rdb[1] = None, time.time() + 60
    h = _local_health.setdefault(kind, {'count': 0})
    h.update(count=h['count'] + 1, at=now, detail=detail)


def health() -> dict:
    """{kind: {'at', 'count', 'detail'}} for ok, bypassed, error, fallback, ad_cut, ad_uncut."""
    out = {k: dict(v) for k, v in _local_health.items()}
    try:
        rdb = _redis()
        if rdb is not None:
            for key, value in (rdb.hgetall(_HEALTH_KEY) or {}).items():
                key = key.decode() if isinstance(key, bytes) else key
                value = value.decode() if isinstance(value, bytes) else value
                kind, _, field = key.rpartition('.')
                out.setdefault(kind, {})[field] = (int(value) if field == 'count'
                                                   else float(value) if field == 'at' else value)
    except Exception:
        pass
    return out


def runtime_warnings() -> list[str]:
    h = health()
    out = []
    for kind, (text, good) in _PROBLEMS.items():
        info = h.get(kind) or {}
        if float(info.get('at') or 0) > float((h.get(good) or {}).get('at') or 0):
            detail = info.get('detail') or ''
            out.append(f'{text}{": " + detail if detail else ""}')
    return out


# ── Scraper (app/scrapers/directv.py) ───────────────────────────────────────────

def _patch_scraper() -> None:
    from . import directv_dai

    # resolve(): drop a cached URL from the other DAI setting (or, with DAI on, another
    # device), run upstream's resolve with this scraper as the tune context, then check
    # the tune really got DAI.
    orig_resolve = up('resolve')
    if not _already(orig_resolve):
        def resolve(self, raw_url, *a, **kw):
            dai, names = False, None
            try:
                dai = directv_dai.enabled(self.config)
                cache = getattr(self, up_name('scraper_cache'))
                names = cache.get('dai_channel_names')
                # Only this channel's entry, and only in memory: upstream's own refetch
                # then saves the fresh one. Other channels' entries (maybe another box's
                # session) are left alone.
                ccid = _ccid(raw_url)
                playback = cache.get('directv_playback') or {}
                if dai:
                    mine = _device_session(cache, self.config, ccid)
                    if mine:   # this box's own session: upstream's cache check reuses it
                        playback = {**playback, ccid: mine}
                        cache['directv_playback'] = playback
                cached = playback.get(ccid)
                if isinstance(cached, dict) and not directv_dai.cached_url_usable(cached, dai):
                    cache['directv_playback'] = {k: v for k, v in playback.items() if k != ccid}
            except Exception:
                _note('error', 'resolve: cache check')
                logger.exception('[directv-dai] cache check failed')
            state = {'called': False, 'dai': False}
            token = _TUNE.set((self.config, names, state))
            try:
                url = orig_resolve(self, raw_url, *a, **kw)
            finally:
                _TUNE.reset(token)
            if dai:
                try:
                    _check_tune(self, raw_url, url, state)
                    if state.get('dai'):
                        _save_device_session(self, _ccid(raw_url))
                except Exception:
                    logger.exception('[directv-dai] tune check failed')
            return url
        up_set('resolve', _wraps(orig_resolve, resolve))

    # The license request (a classmethod): its fallback fetch uses the same session type.
    orig_license = up_owner('license_request').__dict__.get(up_name('license_request'))
    if not isinstance(orig_license, classmethod):
        _fail(f"{'.'.join(_where('license_request'))} is no longer a classmethod; "
              'its fallback fetch would skip DAI (adapt _patch_scraper)')
    elif not _already(orig_license):
        fn = orig_license.__func__
        lic_sig = inspect.signature(fn)
        if 'config' not in lic_sig.parameters:
            _fail(f"{'.'.join(_where('license_request'))} has no 'config' argument any more (adapt _patch_scraper)")
        else:
            def prepare_license_request(klass, *a, **kw):
                config = None
                try:
                    bound = lic_sig.bind(klass, *a, **kw)
                    config = bound.arguments.get('config')
                    channel_id = bound.arguments.get('channel_id')
                    # The license must carry the play token of the session THIS box plays.
                    mine = (_device_session(config, config, str(channel_id))
                            if channel_id and directv_dai.enabled(config) else None)
                    if mine:
                        config = {**config, 'directv_playback': {**(config.get('directv_playback') or {}),
                                                                 str(channel_id): mine}}
                        bound.arguments['config'] = config
                        a, kw = bound.args[1:], bound.kwargs
                except Exception:
                    logger.exception('[directv-dai] could not read the license request config')
                token = _TUNE.set((config, None, {'called': False, 'dai': False}) if config else None)
                try:
                    return fn(klass, *a, **kw)
                finally:
                    _TUNE.reset(token)
            up_set('license_request', classmethod(_wraps(fn, prepare_license_request)))

    # The lineup rows carry each channel's daiChannelName (the Yospace `net` flag).
    orig_rows = up('lineup_rows')
    if not _already(orig_rows):
        def _fetch_allchannels_rows(self, *a, **kw):
            rows = orig_rows(self, *a, **kw)
            try:
                directv_dai.note_channels(self, rows)
            except Exception:
                logger.exception('[directv-dai] could not record channel names')
            return rows
        up_set('lineup_rows', _wraps(orig_rows, _fetch_allchannels_rows))

    # The channel authorization: with DAI on, the Android TV app's channel/v2 request.
    orig_fetch = up('channel_fetch')
    if not _already(orig_fetch):
        sig = inspect.signature(orig_fetch)
        gone = [n for n in _FETCH_ARGS if n not in sig.parameters]
        if gone:
            _fail(f'{up_name("channel_fetch")} no longer takes {", ".join(gone)}; '
                  'DAI tunes fall back to non-DAI (adapt _fetch_dai_playback)')

        def channel_fetch(*args, **kwargs):
            tune = _TUNE.get()
            flags = bound = None
            if tune and not gone:
                try:
                    b = sig.bind(*args, **kwargs)
                    b.apply_defaults()
                    bound = b.arguments
                    flags = directv_dai.request_flags(tune[0], tune[1], bound['ccid'])
                except Exception:
                    _note('error', 'could not build the DAI request')
                    logger.exception('[directv-dai] could not build the DAI request; playing without DAI')
            if not flags:
                return orig_fetch(*args, **kwargs)
            state = tune[2] if len(tune) > 2 else {}
            state['called'] = True
            reason = 'no DAI stream for this channel'
            try:
                result = _fetch_dai_playback(bound, flags)
            except up('auth_expired_error'):
                raise
            except Exception as exc:
                logger.exception('[directv-dai] DAI authorization failed')
                result, reason = None, f'channel/v2 raised {type(exc).__name__}'
                _note('error', reason)
            if result:
                state['dai'] = True
                return result
            _note('fallback', reason if isinstance(reason, str) else '')
            logger.warning('[directv-dai] no DAI stream for this tune; playing without DAI')
            return orig_fetch(*args, **kwargs)
        up_set('channel_fetch', _wraps(orig_fetch, channel_fetch))

    # Every sign-in and refresh goes through apply_auth_result: add the DAI account values.
    orig_apply = up('apply_auth_result')
    if not _already(orig_apply):
        def apply_auth_result(cfg, result, *a, **kw):
            out = orig_apply(cfg, result, *a, **kw)
            try:
                directv_dai.store_login_result(cfg, result)
            except Exception:
                logger.exception('[directv-dai] could not store the DAI login values')
            return out
        up_set('apply_auth_result', _wraps(orig_apply, apply_auth_result))


def _bearer_tag(config) -> str:
    """Ties a saved session to the sign-in it was made with (a new token = new sessions)."""
    return hashlib.sha256(str((config or {}).get('bearer_token') or '').encode()).hexdigest()[:12]


def _device_session(cache, config, ccid: str) -> dict | None:
    """The requesting bridge box's own saved DAI session for this channel, if still good."""
    from . import directv_dai
    address = directv_dai._current_bridge_address()
    mine = (((cache or {}).get(DEVICE_SESSIONS) or {}).get(address) or {}).get(ccid) if address else None
    if (not isinstance(mine, dict) or mine.get('bearer_tag') != _bearer_tag(config)
            or time.time() - float(mine.get('cached_at') or 0) >= _device_session_ttl()
            or not directv_dai.cached_url_usable(mine, True)):
        return None
    return {k: v for k, v in mine.items() if k != 'bearer_tag'}


def _save_device_session(scraper, ccid: str) -> None:
    """After a tune made a new DAI session, keep it under this box too (expired ones pruned)."""
    from . import directv_dai
    address = directv_dai._current_bridge_address()
    cache = getattr(scraper, up_name('scraper_cache'))
    entry = (cache.get('directv_playback') or {}).get(ccid)
    if not address or not isinstance(entry, dict) or not entry.get('dai'):
        return
    now, ttl = time.time(), _device_session_ttl()
    store = {a: {c: e for c, e in (m or {}).items()
                 if isinstance(e, dict) and now - float(e.get('cached_at') or 0) < ttl}
             for a, m in (cache.get(DEVICE_SESSIONS) or {}).items()}
    store.setdefault(address, {})[ccid] = {**entry, 'bearer_tag': _bearer_tag(scraper.config)}
    getattr(scraper, up_name('scraper_update_cache'))(DEVICE_SESSIONS, {a: m for a, m in store.items() if m})


def _ccid(raw_url) -> str:
    """The channel id in upstream's directv://<ccid>/<resource> stream URL."""
    return str(raw_url or '').split('://', 1)[-1].split('/', 1)[0]


def _check_tune(scraper, raw_url: str, url, state: dict) -> None:
    """DAI is on: record whether this tune really got a DAI stream."""
    if isinstance(url, str) and 'yospace.com' in url:
        _note('ok')
        return
    if state.get('called'):
        return   # our DAI request ran and fell back; it noted why
    # Not fetched this time: a cache hit of a DAI entry is fine (a channel with no DAI
    # stream caches its non-Yospace URL tagged dai=True); anything else means upstream's
    # resolve no longer goes through the fetch we wrap.
    cached = (getattr(scraper, up_name('scraper_cache')).get('directv_playback') or {}).get(_ccid(raw_url))
    if isinstance(cached, dict) and cached.get('dai'):
        return
    _note('bypassed', f'resolve() returned without calling {up_name("channel_fetch")}')


def _fetch_dai_playback(args: dict, flags: dict) -> dict | None:
    """The Android TV app's channel authorization (channel/v2): its query, the device's
    app User-Agent, no browser Origin/Referer. Returns upstream's playback dict shape
    plus 'dai': True. Raises upstream's DirectvAuthExpiredError on an expired token,
    so upstream's re-auth runs exactly as on its own request."""
    from . import directv_dai, directv_dai_device
    scraper_module = up_owner('channel_fetch')
    ccid = args['ccid']
    session = requests.Session()
    session.headers.update({'Accept': '*/*', 'Authorization': f"Bearer {args.get('bearer_token') or ''}"})
    # The bridge device's own app User-Agent; for any other requester, upstream's own.
    ua = directv_dai_device.player_user_agent() or getattr(scraper_module, '_UA', None)
    if ua:
        session.headers['User-Agent'] = ua
    for c in args.get('cookies') or []:
        try:
            session.cookies.set(c['name'], c['value'], domain=c.get('domain') or None, path=c.get('path') or '/')
        except Exception:
            continue
    params = {'ccid': ccid, 'proximity': 'O', 'reserveCTicket': 'true', 'daiEnabled': 'true',
              'startOver': 'false', 'abrEnabled': 'true'}
    if args.get('client_context'):
        params['clientContext'] = args['client_context']
    r = session.get(_CHANNEL_AUTH_V2, params=params, timeout=15)
    if r.status_code != 200:
        logger.warning('[directv-dai] channel/v2 HTTP %s for ccid=%s', r.status_code, ccid)
        return None
    data = r.json()
    if not data.get('authorized'):
        status = data.get('responseStatus') or {}
        code = str(status.get('errorCode') or '').strip()
        text = str(status.get('errorText') or status.get('message') or '').strip()
        logger.warning('[directv-dai] channel/v2 not authorized for ccid=%s errorCode=%s', ccid, code or '?')
        if code == '0015' or 'access token has expired' in text.lower():
            raise up('auth_expired_error')(text or 'access token has expired')
        return None
    url = directv_dai.pick_stream_url(data.get('playbackData') or {}, flags)
    play_token = (data.get('dRights') or {}).get('playToken')
    if not url or not play_token:
        return None
    return {
        'fallback_url': url,
        'play_token': play_token,
        'license_content_id': up('license_content_id')(url, ccid),
        'cached_at': time.time(),
        'dai': True,
    }


def _patch_device_auth() -> None:
    # The grant's valuePairs carry the household id (hhid/u); upstream keeps only the
    # activation token from them, so carry the ids through for store_login_result.
    orig_result = up('code_signin_result')
    if not _already(orig_result):
        def _result(token_data, *a, **kw):
            out = orig_result(token_data, *a, **kw)
            try:
                vp = (token_data or {}).get('valuePairs')
                vp = vp if isinstance(vp, dict) else {}
                for src, dst in (('partnerProfileId', 'partner_profile_id'), ('profileId', 'profile_id')):
                    if str(vp.get(src) or '').strip():
                        out[dst] = str(vp[src]).strip()
            except Exception:
                logger.exception('[directv-dai] could not read the sign-in ids')
            return out
        up_set('code_signin_result', _wraps(orig_result, _result))


# ── Relay: hosts, User-Agent, ad audio (all at the HTTP layer) ─────────────────────

def _patch_relay_hosts() -> None:
    """Yospace playlists take upstream's relay (so the HTTP-layer hooks see them)."""
    suffixes = up('relay_cdn_suffixes')
    if 'yospace.com' not in suffixes:
        up_set('relay_cdn_suffixes', (*suffixes, 'yospace.com'))


def _relay_host(host: str) -> bool:
    """Upstream's own DirecTV CDN allowlist (with yospace.com added)."""
    try:
        return bool(up('relay_cdn_allowed')(host or ''))
    except Exception:
        return False


def _relay_user_agent(url) -> str | None:
    """The DirecTV app User-Agent for a relay fetch: inside a request under
    RELAY_PREFIX, to a DirecTV CDN host, with DAI on, from a bridge device."""
    from flask import has_request_context, request
    if not has_request_context() or not request.path.startswith(RELAY_PREFIX):
        return None
    if not _relay_host(urlsplit(str(url)).hostname or ''):
        return None
    from . import directv_dai, directv_dai_device
    if not directv_dai.dai_on():
        return None
    return directv_dai_device.player_user_agent()


def _patch_user_agent() -> None:
    """Every relay fetch (the Yospace master, each playlist and segment) carries the
    bridge device's DirecTV app User-Agent. Hooked on the requests library, which
    upstream's relay always goes through, so its code can change freely."""
    orig = requests.Session.request
    if _already(orig):
        return

    def request(self, method, url, *a, **kw):
        try:
            if str(method).upper() == 'GET' and len(a) < 3:   # headers not passed positionally
                ua = _relay_user_agent(url)
                if ua:
                    headers = {k: v for k, v in (kw.get('headers') or {}).items()
                               if str(k).lower() != 'user-agent'}
                    kw['headers'] = {**headers, 'User-Agent': ua}
        except Exception:
            logger.exception('[directv-dai] could not add the app User-Agent')
        return orig(self, method, url, *a, **kw)
    requests.Session.request = _wraps(orig, request)


def _register_relay_hooks(app) -> None:
    from flask import Response, request
    from . import dtv_aac_ads, dtv_aac_gain

    @app.before_request
    def _directv_dai_ad_segment():
        """An inserted-ad AAC segment (an allowlisted DirecTV CDN, full fetch) is served
        here with the −12 dB loudness cut; everything else goes to upstream's view."""
        try:
            if request.method != 'GET' or not request.path.startswith(RELAY_PREFIX) or request.headers.get('Range'):
                return None
            raw = (request.args.get('url') or '').strip()
            if not raw or not dtv_aac_gain.is_ad_segment(raw):
                return None
            parts = urlsplit(raw)
            if parts.scheme != 'https' or not _relay_host(parts.hostname or ''):
                return None
            return _serve_ad_segment(raw, Response)
        except Exception:
            _note('error', 'ad segment')
            logger.exception('[directv-dai] ad segment handling failed; upstream serves it')
            return None

    @app.after_request
    def _directv_dai_playlist(resp):
        """Every DirecTV playlist the relay serves: swap each inserted AC-3 ad (segments
        and its own EXT-X-MAP init) for its full-range AAC twin. Live content is untouched."""
        try:
            if (request.path.startswith(RELAY_PREFIX) and resp.status_code == 200
                    and 'mpegurl' in (resp.mimetype or '').lower()
                    and not resp.direct_passthrough and not resp.is_streamed):
                text = resp.get_data(as_text=True)
                swapped = dtv_aac_ads.swap_muffled_ads(text)
                if swapped != text:
                    resp.set_data(swapped)
                    # Upstream relays what its CDN allowlist covers; an inserted ad left
                    # as an absolute URL plays direct, so its loudness cut can't run.
                    for host in sorted({urlsplit(u).hostname or '' for u in _ABS_URL.findall(swapped)
                                        if dtv_aac_gain.is_ad_segment(u)}):
                        _note('ad_uncut', f'ads on {host} are not relayed (not on the DirecTV CDN allowlist)')
        except Exception:
            _note('error', 'ad swap')
            logger.exception('[directv-dai] ad swap failed')
        return resp


def _serve_ad_segment(raw_url: str, Response):
    from . import dtv_aac_gain
    headers = {}
    browser_ua = getattr(up_owner('relay_cdn_allowed'), '_BROWSER_UA', None)   # optional
    if browser_ua:
        headers['User-Agent'] = browser_ua   # the app UA replaces it (see _patch_user_agent)
    try:
        r = requests.get(raw_url, headers=headers, timeout=(5, 30))
    except Exception as exc:
        logger.warning('[directv-dai] ad segment fetch failed (%s); upstream serves it', exc)
        return None
    body = r.content
    if r.status_code == 200:
        try:
            cut = dtv_aac_gain.attenuate_ad_segment(body)
            if cut != body:
                _note('ad_cut')
            else:
                _note('ad_uncut', 'an ad segment could not be parsed for the cut')
            body = cut
        except Exception:
            logger.exception('[directv-dai] ad loudness cut failed; sending the segment as is')
    return Response(body, status=r.status_code,
                    content_type=r.headers.get('Content-Type') or 'application/octet-stream',
                    headers={'Cache-Control': 'no-cache', 'Access-Control-Allow-Origin': '*'})


# ── Old sessions: the pre-2026-10-08 overlay tagged its sign-in 'dtv_android' ─────

def migrate_old_sessions() -> None:
    """The old overlay signed in with the same UNIFIED_Android_TV_02 grant upstream uses
    now, but tagged it auth_method 'dtv_android'. Re-tag it to upstream's tag, so
    upstream refreshes it and nobody signs in again. Needs an app context."""
    for src in up('source_model').query.filter_by(name='directv').all():
        cfg = src.config or {}
        if cfg.get('auth_method') == 'dtv_android' and cfg.get('refresh_token'):
            up('persist_config')(src.id, {'auth_method': up('code_signin_method')})
            logger.info("[directv-dai] re-tagged the old overlay sign-in as upstream's code sign-in")


def _register_migration(app) -> None:
    """Runs migrate_old_sessions once per process: at startup when the database is
    ready, else on the first request."""
    done = [False]

    def attempt():
        db = up('db')
        try:
            from sqlalchemy import inspect as sa_inspect
            Source = up('source_model')
            ready = sa_inspect(db.engine).has_table(Source.__tablename__)
        except Exception:
            logger.debug('[directv-dai] database not ready; session re-tag deferred', exc_info=True)
            return
        if not ready:
            return
        try:
            migrate_old_sessions()
            done[0] = True
            _failed[:] = [f for f in _failed if not f.startswith('migrate_old_sessions')]
        except Exception:
            # Reported (status()/the panel): an old 'dtv_android' session that isn't
            # re-tagged won't refresh through upstream's code sign-in.
            _fail("migrate_old_sessions (an old 'dtv_android' sign-in could not be re-tagged; see the server log)")
            logger.exception('[directv-dai] could not re-tag the old overlay sign-in')
            try:
                db.session.rollback()
            except Exception:
                pass

    try:
        with app.app_context():
            attempt()
            db = up('db')
            db.session.remove()
    except Exception:
        logger.debug('[directv-dai] session re-tag deferred to the first request', exc_info=True)

    @app.before_request
    def _directv_dai_migrate():
        if not done[0]:
            done[0] = True   # one try per process, even if it fails
            attempt()


# ── Admin UI: our own blueprint, panel and page ─────────────────────────────────

def _register_admin(app) -> None:
    from flask import Blueprint, Response, jsonify, request
    from . import directv_dai

    bp = Blueprint('directv_dai', __name__)

    def _directv_source(source_id):
        Source = up('source_model')
        source = Source.query.get_or_404(source_id)
        if source.name != 'directv':
            return None
        return source

    @bp.route('/directv-dai/admin.js')
    def admin_js():
        return Response(_ADMIN_JS, mimetype='application/javascript',
                        headers={'Cache-Control': 'no-cache'})

    @bp.route('/directv-dai')
    def dai_page():
        return Response(_PAGE_HTML, mimetype='text/html')

    @bp.route('/directv-dai/status')
    def dai_status():
        return jsonify(status())

    @bp.route('/directv-dai/sources')
    def dai_sources():
        Source = up('source_model')
        return jsonify({'sources': [{'id': s.id, 'name': s.display_name or s.name}
                                    for s in Source.query.filter_by(name='directv').all()]})

    @bp.route('/api/sources/<int:source_id>/directv-dai', methods=['GET', 'POST'])
    def directv_dai_settings(source_id):
        """GET: everything the DAI panel shows. POST {use_dai}: save the toggle, drop the
        cached stream URLs, and (DAI on) capture new devices' ad ids and fetch the
        account's targeting values if sign-in didn't."""
        source = _directv_source(source_id)
        if source is None:
            return jsonify({'error': 'not a directv source'}), 400
        if request.method == 'POST':
            if not request.is_json:
                return jsonify({'error': 'expected JSON'}), 415
            persist_source_config_updates = up('persist_config')
            data = request.get_json(silent=True) or {}
            want = str(data.get('use_dai', '')).strip().lower() in {'1', 'true', 'yes', 'on'}
            old = dict(source.config or {})
            if not persist_source_config_updates(source.id, {'use_dai': 'true' if want else 'false'}):
                return jsonify({'error': 'could not save the setting; try again'}), 409
            Source = up('source_model')
            source = Source.query.get(source_id)
            directv_dai.settings_changed(source, old, dict(source.config or {}))
        cfg = dict(source.config or {})
        on = directv_dai.enabled(cfg)
        field = directv_dai.CONFIG_FIELD
        return jsonify({
            'ok': True, 'use_dai': on, 'label': field['label'], 'help': field['help_text'],
            'code_signed_in': directv_dai.profiles_supported(cfg),
            'pending': len(directv_dai.uncaptured_addresses()) if on else 0,
            'issues': _issue_lines(),
        })

    @bp.route('/api/sources/<int:source_id>/directv-capture-adids', methods=['GET', 'POST'])
    def directv_capture_adids(source_id):
        """GET: how many bridge devices still need an advertising-id capture. POST: capture
        them (a playing box is skipped, so a stream is never interrupted)."""
        source = _directv_source(source_id)
        if source is None:
            return jsonify({'error': 'not a directv source'}), 400
        if request.method == 'POST' and not request.is_json:
            return jsonify({'error': 'expected JSON'}), 415
        if not directv_dai.enabled(dict(source.config or {})):
            return jsonify({'enabled': False, 'ok': True, 'pending': 0})
        if request.method == 'GET':
            return jsonify({'enabled': True, 'pending': len(directv_dai.uncaptured_addresses())})
        counts = directv_dai.capture_registered_devices(directv_dai.uncaptured_addresses())
        return jsonify({'enabled': True, 'ok': True, **counts,
                        'pending': len(directv_dai.uncaptured_addresses())})

    @bp.route('/api/sources/<int:source_id>/directv-profile', methods=['GET', 'POST'])
    def directv_profile(source_id):
        """GET: the account's viewer profiles and which one ads are requested as. POST: run
        as a chosen profile (its ad id becomes the Yospace `profid`)."""
        source = _directv_source(source_id)
        if source is None:
            return jsonify({'error': 'not a directv source'}), 400
        cfg = dict(source.config or {})
        if not directv_dai.profiles_supported(cfg):
            return jsonify({'error': 'profiles need the DirecTV code sign-in', 'profiles': []}), 400
        if request.method == 'GET':
            info = directv_dai.list_profiles(cfg.get('bearer_token') or '')
            selected = (cfg.get('dtv_android_profile_id') or '').strip() or (info.get('current_id') or '')
            return jsonify({'profiles': info.get('profiles') or [], 'selected_id': selected,
                            'current_id': info.get('current_id') or ''})
        if not request.is_json:
            return jsonify({'error': 'expected JSON'}), 415
        data = request.get_json(silent=True) or {}
        profile_id = (data.get('profile_id') or '').strip()
        if not profile_id:
            return jsonify({'error': 'no profile_id'}), 400
        result = directv_dai.select_profile(source, profile_id, (data.get('profile_name') or '').strip())
        return jsonify(result), (200 if result.get('ok') else 400)

    app.register_blueprint(bp)

    tag = b'<script src="/directv-dai/admin.js"></script>'
    marker = b'id="source-config-'

    @app.after_request
    def _directv_dai_admin_script(resp):
        """Adds the panel script to upstream pages that have source settings boxes."""
        try:
            if (resp.status_code == 200 and resp.mimetype == 'text/html'
                    and not resp.direct_passthrough and not resp.is_streamed
                    and not request.path.startswith('/directv-dai')):
                body = resp.get_data()
                if marker in body and tag not in body:
                    i = body.rfind(b'</body>')
                    resp.set_data(body[:i] + tag + body[i:] if i >= 0 else body + tag)
        except Exception:
            logger.exception('[directv-dai] could not add the admin script')
        return resp


def _issue_lines() -> list[str]:
    from . import directv_dai
    st = status()
    runtime = st['runtime'] if directv_dai.dai_on() else []   # stale once DAI is off
    return [*(f'upstream renamed or removed {n}' for n in st['missing']), *st['markers'],
            *(f'could not wire {n}' for n in st['failed']), *runtime]


_PAGE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DirecTV DAI</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 24px 16px; background: #111; color: #eee; }
  main { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 1.3rem; } a { color: #8ab4f8; }
  .directv-dai-panel { border: 1px solid #333; border-radius: 8px; padding: 12px 16px; margin: 16px 0; }
  .help { color: #aaa; font-size: 0.9rem; margin-top: 4px; }
  button { padding: 6px 12px; } select { padding: 6px; }
</style></head>
<body><main>
<h1>DirecTV ad insertion (DAI)</h1>
<p class="help">The same settings also appear inside the DirecTV source's settings on <a href="/admin/sources">Sources</a>.</p>
<div id="directv-dai-standalone"></div>
</main>
<script src="/directv-dai/admin.js"></script>
</body></html>
"""

_ADMIN_JS = r"""
// DirecTV DAI settings: the DAI toggle, the viewer-profile picker and the "Capture
// advertising IDs" button, as our own panel. It's added inside each DirecTV source's
// settings box on the sources page (found by its source-config-<id> id; no upstream
// function is wrapped), and rendered on its own at /directv-dai.
(function () {
  if (window.__directvDai) return;
  window.__directvDai = true;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const getJson = async (url, opts) => {
    const r = await fetch(url, opts);
    return [r, await r.json().catch(() => ({}))];
  };
  let sourcesPromise = null;
  const sources = () => sourcesPromise || (sourcesPromise = getJson('/directv-dai/sources')
    .then(([, d]) => d.sources || []).catch(() => []));

  function mount(host, id, before) {
    const el = document.createElement('div');
    el.id = `directv-dai-${id}`;
    el.className = 'directv-dai-panel field field-full';
    el.style.marginTop = '0.9rem';
    el.innerHTML = '<div class="help">Loading DirecTV DAI settings&hellip;</div>';
    if (before && before.parentNode) before.parentNode.insertBefore(el, before); else host.appendChild(el);
    load(id);
  }

  async function mountInline() {
    const list = await sources();
    for (const s of list) {
      const box = document.getElementById(`source-config-${s.id}`);
      if (!box || !box.children.length || document.getElementById(`directv-dai-${s.id}`)) continue;
      mount(box, s.id, box.querySelector('.config-actions'));
    }
  }

  async function load(id) {
    const el = document.getElementById(`directv-dai-${id}`);
    if (!el) return;
    try {
      const [r, d] = await getJson(`/api/sources/${id}/directv-dai`);
      if (!r.ok) { el.innerHTML = `<div class="help">${esc(d.error || 'DAI settings unavailable')}</div>`; return; }
      render(el, id, d);
    } catch (e) {
      el.innerHTML = '<div class="help">DAI settings unavailable</div>';
    }
  }

  function render(el, id, d) {
    const issues = (d.issues || []).length
      ? '<div style="color:var(--danger,#e55);margin-bottom:8px"><strong>DAI wiring needs an update for this FastChannels version:</strong><ul style="margin:4px 0 0 18px">'
        + d.issues.map(i => `<li>${esc(i)}</li>`).join('') + '</ul></div>'
      : '';
    const profile = d.code_signed_in ? `
      <div style="margin-top:0.9rem">
        <label for="directv-profile-select-${id}" style="font-weight:600">Run as DirecTV profile</label>
        <div class="help">FastChannels requests ads as this viewer profile &mdash; its ad id becomes DirecTV's <code>profid</code>. Changing it clears the cached stream so the next tune uses it.</div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">
          <select id="directv-profile-select-${id}" disabled style="min-width:180px;padding:6px"><option>Loading profiles&hellip;</option></select>
          <button class="btn btn-run" type="button" id="directv-profile-btn-${id}" disabled>Use profile</button>
          <span id="directv-profile-status-${id}" style="color:var(--text-muted)"></span>
        </div>
      </div>` : '';
    const capture = d.use_dai && d.pending > 0 ? `
      <div style="margin-top:0.9rem">
        <button class="btn btn-run" type="button" id="directv-adid-btn-${id}">Capture advertising IDs (${d.pending} new device${d.pending > 1 ? 's' : ''})</button>
        <span id="directv-adid-status-${id}" style="color:var(--text-muted)"></span>
      </div>` : `<span id="directv-adid-status-${id}" style="color:var(--text-muted)"></span>`;
    el.innerHTML = `${issues}
      <div class="config-toggle-field">
        <div class="config-toggle-text">
          <label for="directv-dai-toggle-${id}">${esc(d.label)}</label>
          <div class="help">${esc(d.help)}</div>
        </div>
        <label class="toggle" title="${esc(d.label)}">
          <input type="checkbox" id="directv-dai-toggle-${id}"${d.use_dai ? ' checked' : ''}>
          <span class="slider"></span>
        </label>
      </div>
      <div class="help" id="directv-dai-toggle-status-${id}"></div>
      ${profile}${capture}`;
    el.querySelector(`#directv-dai-toggle-${id}`).addEventListener('change', e => saveToggle(id, e.target.checked));
    const pbtn = el.querySelector(`#directv-profile-btn-${id}`);
    if (pbtn) { pbtn.addEventListener('click', () => selectProfile(id)); loadProfiles(id); }
    const cbtn = el.querySelector(`#directv-adid-btn-${id}`);
    if (cbtn) cbtn.addEventListener('click', () => captureAdids(id));
  }

  async function saveToggle(id, on) {
    const st = document.getElementById(`directv-dai-toggle-status-${id}`);
    if (st) st.textContent = 'Saving…';
    try {
      const [r, d] = await getJson(`/api/sources/${id}/directv-dai`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({use_dai: on}),
      });
      if (!r.ok) { if (st) st.textContent = d.error || 'Could not save'; return; }
      render(document.getElementById(`directv-dai-${id}`), id, d);
      const st2 = document.getElementById(`directv-dai-toggle-status-${id}`);
      if (st2) st2.textContent = on ? 'Saved: DAI is on. The next tune uses it.' : 'Saved: DAI is off.';
    } catch (e) { if (st) st.textContent = 'Network error'; }
  }

  async function loadProfiles(id) {
    const sel = document.getElementById(`directv-profile-select-${id}`);
    const btn = document.getElementById(`directv-profile-btn-${id}`);
    if (!sel) return;
    try {
      const [r, d] = await getJson(`/api/sources/${id}/directv-profile`);
      const profiles = d.profiles || [];
      if (!r.ok || !profiles.length) {
        sel.innerHTML = `<option>${esc(r.ok ? 'No profiles found' : (d.error || 'Could not load profiles'))}</option>`;
        return;
      }
      sel.innerHTML = profiles.map(p =>
        `<option value="${esc(p.id)}"${p.id === d.selected_id ? ' selected' : ''}>` +
        `${esc(p.name)}${p.primary ? ' (primary)' : ''}${p.id === d.selected_id ? ' — running' : ''}</option>`).join('');
      sel.disabled = false;
      if (btn) btn.disabled = false;
    } catch (e) { sel.innerHTML = '<option>Could not load profiles</option>'; }
  }

  async function selectProfile(id) {
    const sel = document.getElementById(`directv-profile-select-${id}`);
    const btn = document.getElementById(`directv-profile-btn-${id}`);
    const st = document.getElementById(`directv-profile-status-${id}`);
    if (!sel || !sel.value) return;
    const name = (sel.options[sel.selectedIndex]?.text || '').replace(/ \(primary\)| — running/g, '');
    if (btn) btn.disabled = true;
    if (st) { st.style.color = 'var(--text-muted)'; st.textContent = 'Switching…'; }
    try {
      const [r, d] = await getJson(`/api/sources/${id}/directv-profile`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile_id: sel.value, profile_name: name}),
      });
      if (r.ok && d.ok) {
        if (st) { st.style.color = 'var(--success-soft,#6c6)'; st.innerHTML = '&#10003; Running as this profile.'; }
        loadProfiles(id);
      } else if (st) { st.style.color = 'var(--danger,#e55)'; st.textContent = d.error || 'Could not switch profile'; }
    } catch (e) {
      if (st) { st.style.color = 'var(--danger,#e55)'; st.textContent = 'Network error'; }
    } finally { if (btn) btn.disabled = false; }
  }

  async function captureAdids(id) {
    const btn = document.getElementById(`directv-adid-btn-${id}`);
    const st = document.getElementById(`directv-adid-status-${id}`);
    if (btn) btn.disabled = true;
    if (st) st.textContent = 'Capturing… (a Google TV/Android TV device briefly shows its Ads screen; a playing device is skipped)';
    let message = '';
    try {
      const [r, d] = await getJson(`/api/sources/${id}/directv-capture-adids`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
      });
      if (r.ok && d.ok && d.enabled !== false) {
        const parts = [`Captured ${d.captured} device${d.captured === 1 ? '' : 's'}`];
        if (d.playing) parts.push(`${d.playing} skipped — ${d.playing === 1 ? 'a device is' : 'devices are'} playing (re-run when idle)`);
        if (d.unreachable) parts.push(`${d.unreachable} couldn't be reached (check ${d.unreachable === 1 ? "it's" : "they're"} on and connected)`);
        if (d.none) parts.push(`${d.none} with no advertising ID`);
        message = parts.join('; ') + '.';
      } else if (d.enabled !== false) { message = d.error || 'Capture failed'; }
    } catch (e) { message = 'Network error'; }
    await load(id);
    const st2 = document.getElementById(`directv-adid-status-${id}`);
    if (st2) st2.textContent = message;
  }

  window.directvDaiStandalone = async function () {
    const host = document.getElementById('directv-dai-standalone');
    if (!host) return;
    const list = await sources();
    if (!list.length) { host.innerHTML = '<p class="help">No DirecTV source is set up yet.</p>'; return; }
    for (const s of list) {
      const h = document.createElement('h2');
      h.style.fontSize = '1.05rem';
      h.textContent = s.name;
      host.appendChild(h);
      mount(host, s.id, null);
    }
  };

  const start = () => {
    if (document.getElementById('directv-dai-standalone')) { window.directvDaiStandalone(); return; }
    let queued = false;
    const check = () => { queued = false; mountInline(); };
    new MutationObserver(() => { if (!queued) { queued = true; setTimeout(check, 50); } })
      .observe(document.body, {childList: true, subtree: true});
    check();
  };
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
})();
"""
