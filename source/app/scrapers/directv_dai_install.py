"""Wires DirecTV DAI into upstream FastChannels at runtime, from ONE line in create_app.

Upstream's files carry a single hook (``directv_dai_install.install(app)`` at the end
of ``create_app``). Everything else is attached here by wrapping upstream's own
functions and views after they're defined, so upstream can rewrite the bodies of
those functions freely without a patch conflict. Rules every wrapper follows:

- DAI off -> the wrapper calls upstream's function untouched.
- Any error in our code -> log it and fall back to upstream's function, so drift
  degrades to "DAI off", never to broken playback.
- A target that's gone is logged at ERROR by install() and listed by ``missing()``;
  CI (validate.sh / smoke_test.py) fails loudly on it. Fix a moved target HERE.

Module-level patches are applied once per process (create_app runs several times:
the web app, the RQ worker, run_directv_auth's own app); per-app pieces (views, the
blueprint, the admin-page script) once per app.

TARGETS lists every upstream name this touches.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
from urllib.parse import quote, urljoin, urlsplit

import requests

logger = logging.getLogger(__name__)

# Every upstream name install() depends on: (module, attribute path). validate.sh and
# smoke_test.py check each one, so an upstream rename fails CI with its name.
TARGETS = (
    ('app.scrapers.directv', '_fetch_channel_playback'),
    ('app.scrapers.directv', '_license_content_id_from_stream_url'),
    ('app.scrapers.directv', 'DirectvAuthExpiredError'),
    ('app.scrapers.directv', 'apply_auth_result'),
    ('app.scrapers.directv', 'DirectvScraper.config_schema'),
    ('app.scrapers.directv', 'DirectvScraper.resolve'),
    ('app.scrapers.directv', 'DirectvScraper.prepare_license_request'),
    ('app.scrapers.directv', 'DirectvScraper._fetch_allchannels_rows'),
    ('app.scrapers.directv_device_auth', '_result'),
    ('app.scrapers.directv_device_auth', 'is_device_session'),
    ('app.scrapers.directv_device_auth', 'app_headers'),
    ('app.routes.directv_proxy', '_requests'),
    ('app.routes.directv_proxy', '_DIRECTV_BROWSER_CDN_SUFFIXES'),
    ('app.routes.directv_proxy', '_directv_browser_cdn_allowed'),
    ('app.routes.directv_proxy', '_directv_browser_proxyable_url'),
    ('app.routes.directv_proxy', '_directv_browser_asset_proxy_url'),
    ('app.routes.directv_proxy', '_rewrite_directv_browser_playlist'),
    ('app.routes.directv_proxy', 'directv_browser_asset'),
)
# Views found by URL rule (more stable than function names).
SAVE_CONFIG_RULE = '/api/sources/<int:source_id>/config'
# The admin sources page bits the injected script relies on.
TEMPLATE_MARKERS = ('function renderDirectvConfig', 'class="config-actions"')

_CHANNEL_AUTH_V2 = 'https://api.cld.dtvce.com/right/authorization/channel/v2'

# The scraper (config + cache) behind the current _fetch_channel_playback call. Set by
# the resolve()/prepare_license_request() wrappers, since upstream's fetch takes no config.
_TUNE = contextvars.ContextVar('directv_dai_tune', default=None)

_process_patched = False
_missing: list[str] = []


def _resolve(module_name: str, path: str):
    import importlib
    obj = importlib.import_module(module_name)
    for part in path.split('.'):
        obj = getattr(obj, part)
    return obj


def missing() -> list[str]:
    """TARGETS that don't exist in this upstream (empty = everything wired)."""
    gone = []
    for module_name, path in TARGETS:
        try:
            _resolve(module_name, path)
        except Exception:
            gone.append(f'{module_name}.{path}')
    return gone


def install(app) -> None:
    """The one hook. Never raises: a failure here leaves upstream exactly as it was."""
    global _process_patched
    try:
        _missing[:] = missing()
        for name in _missing:
            logger.error('[directv-dai] upstream moved %s; that part of DAI is not wired', name)
        if not _process_patched:
            _process_patched = True
            for step in (_patch_scraper, _patch_device_auth, _patch_relay):
                _try(step)
        if 'directv_dai' not in app.extensions:
            app.extensions['directv_dai'] = True
            for step in (_patch_asset_view, _patch_save_view, _register_admin):
                _try(step, app)
    except Exception:
        logger.exception('[directv-dai] install failed; DAI is off')


def _try(step, *args) -> None:
    try:
        step(*args)
    except Exception:
        logger.exception('[directv-dai] could not wire %s', step.__name__)


def _wraps(original, wrapper):
    wrapper = functools.wraps(original)(wrapper)
    wrapper.__directv_dai__ = True
    return wrapper


def _already(fn) -> bool:
    return getattr(fn, '__directv_dai__', False) or getattr(getattr(fn, '__func__', None), '__directv_dai__', False)


# ── Scraper (app/scrapers/directv.py) ───────────────────────────────────────────

def _patch_scraper() -> None:
    from . import directv, directv_dai
    cls = directv.DirectvScraper

    if not any(getattr(f, 'key', None) == 'use_dai' for f in cls.config_schema):
        cls.config_schema = [*cls.config_schema, *directv_dai.CONFIG_FIELDS]

    # resolve(): drop a cached URL from the other DAI setting (or, with DAI on, another
    # device), then run upstream's resolve with this scraper as the tune context.
    orig_resolve = cls.resolve
    if not _already(orig_resolve):
        def resolve(self, raw_url, *a, **kw):
            try:
                dai = directv_dai.enabled(self.config)
                playback = self.cache.get('directv_playback') or {}
                stale = [k for k, v in playback.items()
                         if isinstance(v, dict) and not directv_dai.cached_url_usable(v, dai)]
                if stale:
                    self.cache['directv_playback'] = {k: v for k, v in playback.items() if k not in stale}
            except Exception:
                logger.exception('[directv-dai] cache check failed')
            token = _TUNE.set((self.config, self.cache.get('dai_channel_names')))
            try:
                return orig_resolve(self, raw_url, *a, **kw)
            finally:
                _TUNE.reset(token)
        cls.resolve = _wraps(orig_resolve, resolve)

    # prepare_license_request() (a classmethod): its fallback fetch uses the same session type.
    orig_license = cls.__dict__.get('prepare_license_request')
    if isinstance(orig_license, classmethod) and not _already(orig_license):
        fn = orig_license.__func__

        def prepare_license_request(klass, challenge, config, *a, **kw):
            token = _TUNE.set((config, None))
            try:
                return fn(klass, challenge, config, *a, **kw)
            finally:
                _TUNE.reset(token)
        cls.prepare_license_request = classmethod(_wraps(fn, prepare_license_request))

    # The lineup rows carry each channel's daiChannelName (the Yospace `net` flag).
    orig_rows = cls._fetch_allchannels_rows
    if not _already(orig_rows):
        def _fetch_allchannels_rows(self, *a, **kw):
            rows = orig_rows(self, *a, **kw)
            try:
                directv_dai.note_channels(self, rows)
            except Exception:
                logger.exception('[directv-dai] could not record channel names')
            return rows
        cls._fetch_allchannels_rows = _wraps(orig_rows, _fetch_allchannels_rows)

    # The channel authorization: with DAI on, the Android TV app's channel/v2 request.
    orig_fetch = directv._fetch_channel_playback
    if not _already(orig_fetch):
        sig = inspect.signature(orig_fetch)

        def _fetch_channel_playback(*args, **kwargs):
            tune = _TUNE.get()
            flags = None
            try:
                if tune:
                    bound = sig.bind(*args, **kwargs).arguments
                    flags = directv_dai.request_flags(tune[0], tune[1], bound['ccid'])
            except Exception:
                logger.exception('[directv-dai] could not build the DAI request; playing without DAI')
            if not flags:
                return orig_fetch(*args, **kwargs)
            try:
                result = _fetch_dai_playback(directv, bound, flags)
            except directv.DirectvAuthExpiredError:
                raise
            except Exception:
                logger.exception('[directv-dai] DAI authorization failed')
                result = None
            if result:
                return result
            logger.warning('[directv-dai] no DAI stream for this tune; playing without DAI')
            return orig_fetch(*args, **kwargs)
        directv._fetch_channel_playback = _wraps(orig_fetch, _fetch_channel_playback)

    # Every sign-in and refresh goes through apply_auth_result: add the DAI account values.
    orig_apply = directv.apply_auth_result
    if not _already(orig_apply):
        def apply_auth_result(cfg, result, *a, **kw):
            out = orig_apply(cfg, result, *a, **kw)
            try:
                directv_dai.store_login_result(cfg, result)
            except Exception:
                logger.exception('[directv-dai] could not store the DAI login values')
            return out
        directv.apply_auth_result = _wraps(orig_apply, apply_auth_result)


def _fetch_dai_playback(directv, args: dict, flags: dict) -> dict | None:
    """The Android TV app's channel authorization (channel/v2): its query, the device's
    app User-Agent, no browser Origin/Referer. Returns upstream's playback dict shape
    plus 'dai': True. Raises upstream's DirectvAuthExpiredError on an expired token,
    so upstream's re-auth runs exactly as on its own request."""
    from . import directv_dai, directv_dai_device
    ccid = args['ccid']
    session = requests.Session()
    session.headers.update({
        'Accept': '*/*',
        'Authorization': f"Bearer {args.get('bearer_token') or ''}",
        'User-Agent': directv_dai_device.player_user_agent() or getattr(directv, '_UA', 'okhttp'),
    })
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
            raise directv.DirectvAuthExpiredError(text or 'access token has expired')
        return None
    url = directv_dai.pick_stream_url(data.get('playbackData') or {}, flags)
    play_token = (data.get('dRights') or {}).get('playToken')
    if not url or not play_token:
        return None
    return {
        'fallback_url': url,
        'play_token': play_token,
        'license_content_id': directv._license_content_id_from_stream_url(url, ccid),
        'cached_at': time.time(),
        'dai': True,
    }


def _patch_device_auth() -> None:
    from . import directv_device_auth as auth

    # The grant's valuePairs carry the household id (hhid/u); upstream keeps only the
    # activation token from them, so carry the ids through for store_login_result.
    orig_result = auth._result
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
        auth._result = _wraps(orig_result, _result)

    # A session signed in by the old overlay (auth_method 'dtv_android') is the same
    # UNIFIED_Android_TV_02 grant: let upstream refresh it, which re-tags it 'device_code'.
    orig_is = auth.is_device_session
    if not _already(orig_is):
        def is_device_session(config, *a, **kw):
            cfg = config or {}
            return bool(orig_is(config, *a, **kw)
                        or (cfg.get('auth_method') == 'dtv_android' and cfg.get('refresh_token')))
        auth.is_device_session = _wraps(orig_is, is_device_session)


# ── Relay (app/routes/directv_proxy.py) ─────────────────────────────────────────

class _RelayRequests:
    """Stands in for directv_proxy's ``requests`` module: identical, except a GET to a
    DirecTV relay host from a bridge device carries the DirecTV app's User-Agent for that
    device (the Yospace master fetch and every playlist and segment)."""

    def __init__(self, real, allowed):
        self._real = real
        self._allowed = allowed

    def __getattr__(self, name):
        return getattr(self._real, name)

    def get(self, url, *args, **kwargs):
        try:
            from . import directv_dai_device
            ua = directv_dai_device.player_headers()
            if ua and self._allowed(urlsplit(str(url)).hostname or ''):
                kwargs['headers'] = {**(kwargs.get('headers') or {}), **ua}
        except Exception:
            logger.exception('[directv-dai] could not add the app User-Agent')
        return self._real.get(url, *args, **kwargs)


def _patch_relay() -> None:
    from ..routes import directv_proxy as proxy
    from . import dtv_aac_ads, dtv_aac_gain

    if 'yospace.com' not in proxy._DIRECTV_BROWSER_CDN_SUFFIXES:
        proxy._DIRECTV_BROWSER_CDN_SUFFIXES = (*proxy._DIRECTV_BROWSER_CDN_SUFFIXES, 'yospace.com')

    if not isinstance(proxy._requests, _RelayRequests):
        proxy._requests = _RelayRequests(proxy._requests, proxy._directv_browser_cdn_allowed)

    # Every DAI audio playlist: swap each inserted AC-3 ad for its full-range AAC twin.
    orig_rewrite = proxy._rewrite_directv_browser_playlist
    if not _already(orig_rewrite):
        def _rewrite_directv_browser_playlist(text, *a, **kw):
            try:
                text = dtv_aac_ads.swap_muffled_ads(text)
            except Exception:
                logger.exception('[directv-dai] ad swap failed')
            return orig_rewrite(text, *a, **kw)
        proxy._rewrite_directv_browser_playlist = _wraps(orig_rewrite, _rewrite_directv_browser_playlist)

    # An inserted-ad creative always takes the relay (so its loudness cut runs), whatever CDN.
    orig_proxyable = proxy._directv_browser_proxyable_url
    if not _already(orig_proxyable):
        def _directv_browser_proxyable_url(raw_url, playlist_url, *a, **kw):
            try:
                resolved = urljoin(playlist_url, raw_url)
                if urlsplit(resolved).scheme in ('http', 'https') and dtv_aac_gain.is_ad_segment(resolved):
                    return proxy._directv_browser_asset_proxy_url(resolved)
            except Exception:
                logger.exception('[directv-dai] ad routing failed')
            return orig_proxyable(raw_url, playlist_url, *a, **kw)
        proxy._directv_browser_proxyable_url = _wraps(orig_proxyable, _directv_browser_proxyable_url)


def _find_endpoint(app, func=None, rule: str | None = None, method: str = 'GET') -> str | None:
    if rule:
        for r in app.url_map.iter_rules():
            if r.rule == rule and method in (r.methods or ()):
                return r.endpoint
    if func is not None:
        for endpoint, view in app.view_functions.items():
            if view is func or getattr(view, '__wrapped__', None) is func:
                return endpoint
    return None


def _patch_asset_view(app) -> None:
    """browser-asset: an inserted-ad AAC segment is fetched and cut here (−12 dB, lossless);
    every other asset goes to upstream's view untouched."""
    from flask import Response, abort, request
    from ..routes import directv_proxy as proxy
    from . import directv_dai_device, dtv_aac_gain

    endpoint = _find_endpoint(app, func=proxy.directv_browser_asset)
    if not endpoint:
        logger.error('[directv-dai] browser-asset view not found; ad loudness cut is off')
        return
    orig_view = app.view_functions[endpoint]
    if _already(orig_view):
        return

    def directv_browser_asset(*a, **kw):
        raw_url = (request.args.get('url') or '').strip()
        if not (raw_url and dtv_aac_gain.is_ad_segment(raw_url)
                and urlsplit(raw_url).scheme == 'https' and not request.headers.get('Range')):
            return orig_view(*a, **kw)
        headers = {'User-Agent': getattr(proxy, '_BROWSER_UA', 'Mozilla/5.0'), **directv_dai_device.player_headers()}
        try:
            r = requests.get(raw_url, headers=headers, timeout=(5, 30))
        except Exception as exc:
            logger.warning('[directv-dai] ad segment fetch failed: %s', exc)
            abort(502)
        body = r.content
        try:
            if r.status_code == 200:
                body = dtv_aac_gain.attenuate_ad_segment(body)
        except Exception:
            logger.exception('[directv-dai] ad loudness cut failed; sending the segment as is')
        return Response(body, status=r.status_code,
                        content_type=r.headers.get('Content-Type') or 'application/octet-stream',
                        headers={'Cache-Control': 'no-cache', 'Access-Control-Allow-Origin': '*'})
    app.view_functions[endpoint] = _wraps(orig_view, directv_browser_asset)


def _patch_save_view(app) -> None:
    """Saving the DirecTV settings: drop cached stream URLs when the DAI toggle flips, and
    capture the advertising id of any bridge device that doesn't have one yet."""
    from . import directv_dai

    endpoint = _find_endpoint(app, rule=SAVE_CONFIG_RULE, method='POST')
    if not endpoint:
        logger.error('[directv-dai] save-config view not found; the toggle will not clear the cache')
        return
    orig_view = app.view_functions[endpoint]
    if _already(orig_view):
        return

    def save_source_config(*a, **kw):
        source_id = kw.get('source_id') or (a[0] if a else None)
        old = None
        try:
            from ..models import Source
            src = Source.query.get(source_id)
            if src is not None and src.name == 'directv':
                old = dict(src.config or {})
        except Exception:
            logger.exception('[directv-dai] could not read the old DirecTV settings')
        resp = orig_view(*a, **kw)
        if old is not None:
            try:
                from ..models import Source
                src = Source.query.get(source_id)
                status = getattr(resp, 'status_code', None) or (resp[1] if isinstance(resp, tuple) and len(resp) > 1 else 200)
                if src is not None and int(status) < 400:
                    directv_dai.clear_cache_if_toggled(src, old, dict(src.config or {}))
            except Exception:
                logger.exception('[directv-dai] post-save step failed')
        return resp
    app.view_functions[endpoint] = _wraps(orig_view, save_source_config)


# ── Admin UI: our own blueprint + one script tag on the sources page ────────────

def _register_admin(app) -> None:
    from flask import Blueprint, Response, jsonify, request
    from . import directv_dai

    bp = Blueprint('directv_dai', __name__)

    @bp.route('/directv-dai/admin.js')
    def admin_js():
        return Response(_ADMIN_JS, mimetype='application/javascript',
                        headers={'Cache-Control': 'no-cache'})

    @bp.route('/api/sources/<int:source_id>/directv-capture-adids', methods=['GET', 'POST'])
    def directv_capture_adids(source_id):
        """GET: how many bridge devices still need an advertising-id capture. POST: capture
        them (a playing box is skipped, so a stream is never interrupted)."""
        from ..models import Source
        source = Source.query.get_or_404(source_id)
        if source.name != 'directv':
            return jsonify({'error': 'not a directv source'}), 400
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
        from ..models import Source
        source = Source.query.get_or_404(source_id)
        if source.name != 'directv':
            return jsonify({'error': 'not a directv source'}), 400
        cfg = dict(source.config or {})
        if not directv_dai.profiles_supported(cfg):
            return jsonify({'error': 'profiles need the DirecTV code sign-in', 'profiles': []}), 400
        if request.method == 'GET':
            info = directv_dai.list_profiles(cfg.get('bearer_token') or '')
            selected = (cfg.get('dtv_android_profile_id') or '').strip() or (info.get('current_id') or '')
            return jsonify({'profiles': info.get('profiles') or [], 'selected_id': selected,
                            'current_id': info.get('current_id') or ''})
        data = request.get_json(silent=True) or request.form
        profile_id = (data.get('profile_id') or '').strip()
        if not profile_id:
            return jsonify({'error': 'no profile_id'}), 400
        result = directv_dai.select_profile(source, profile_id, (data.get('profile_name') or '').strip())
        return jsonify(result), (200 if result.get('ok') else 400)

    app.register_blueprint(bp)

    tag = b'<script src="/directv-dai/admin.js"></script>'

    @app.after_request
    def _inject_admin_script(resp):
        try:
            if (resp.status_code == 200 and resp.mimetype == 'text/html'
                    and not resp.direct_passthrough and not resp.is_streamed):
                body = resp.get_data()
                if b'function renderDirectvConfig' in body and tag not in body:
                    i = body.rfind(b'</body>')
                    resp.set_data(body[:i] + tag + body[i:] if i >= 0 else body + tag)
        except Exception:
            logger.exception('[directv-dai] could not add the admin script')
        return resp


_ADMIN_JS = r"""
// DirecTV DAI: adds the viewer-profile picker, the DAI toggle and the "Capture advertising IDs" button to the
// DirecTV source settings by wrapping upstream's renderDirectvConfig (no template edit).
(function () {
  const orig = window.renderDirectvConfig;
  if (typeof orig !== 'function') { console.warn('[directv-dai] renderDirectvConfig not found'); return; }
  const esc = s => (typeof escapeHtml === 'function') ? escapeHtml(String(s))
    : String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

  function daiHtml(sourceId, schema, values) {
    const field = (schema || []).find(f => f.key === 'use_dai') || {};
    const checked = ['1', 'true', 'yes', 'on'].includes(String((values || {}).use_dai ?? field.default ?? '').trim().toLowerCase());
    const label = field.label || 'Use DirecTV ad insertion (DAI)';
    return `
      <div class="fields-grid" style="margin-top:0.9rem" id="directv-dai-${sourceId}">
        <div class="field field-full">
          <div class="config-toggle-field">
            <div class="config-toggle-text">
              <label for="cfg-${sourceId}-use_dai">${esc(label)}</label>
              <div class="help">${esc(field.help_text || '')}</div>
            </div>
            <label class="toggle" title="${esc(label)}">
              <input type="checkbox" id="cfg-${sourceId}-use_dai" data-key="use_dai" data-secret="false"${checked ? ' checked' : ''}>
              <span class="slider"></span>
            </label>
          </div>
        </div>
        <div class="field field-full">
          <button class="btn btn-run" type="button" id="directv-adid-btn-${sourceId}" style="display:none"
                  onclick="directvCaptureAdids(${sourceId})">Capture advertising IDs</button>
          <span id="directv-adid-status-${sourceId}" style="color:var(--text-muted)"></span>
        </div>
      </div>`;
  }

  // The viewer-profile picker: shown once the source has a code sign-in session.
  function profileHtml(sourceId) {
    return `
      <div class="field field-full" style="margin-top:0.9rem" id="directv-profile-${sourceId}">
        <label for="directv-profile-select-${sourceId}" style="font-weight:600">Run as DirecTV profile</label>
        <div class="help">FastChannels requests ads as this viewer profile &mdash; its ad id becomes DirecTV's <code>profid</code>. Changing it clears the cached stream so the next tune uses it.</div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">
          <select id="directv-profile-select-${sourceId}" disabled style="min-width:180px;padding:6px">
            <option>Loading profiles&hellip;</option>
          </select>
          <button class="btn btn-run" type="button" id="directv-profile-btn-${sourceId}"
                  onclick="directvSelectProfile(${sourceId})" disabled>Use profile</button>
          <span id="directv-profile-status-${sourceId}" style="color:var(--text-muted)"></span>
        </div>
      </div>`;
  }

  window.renderDirectvConfig = function (sourceId, schema, values, cfg) {
    let html = orig.apply(this, arguments);
    try {
      const signedIn = !!(cfg && cfg.directv && cfg.directv.code_signed_in);
      const extra = (signedIn ? profileHtml(sourceId) : '') + daiHtml(sourceId, schema, values);
      const at = html.lastIndexOf('<div class="config-actions">');
      html = at >= 0 ? html.slice(0, at) + extra + html.slice(at) : html + extra;
      setTimeout(() => { directvLoadAdidStatus(sourceId); if (signedIn) directvLoadProfiles(sourceId); }, 0);
    } catch (e) { console.warn('[directv-dai]', e); }
    return html;
  };

  window.directvLoadProfiles = async function (sourceId) {
    const sel = document.getElementById(`directv-profile-select-${sourceId}`);
    const btn = document.getElementById(`directv-profile-btn-${sourceId}`);
    const statusEl = document.getElementById(`directv-profile-status-${sourceId}`);
    if (!sel) return;
    try {
      const r = await fetch(`/api/sources/${sourceId}/directv-profile`);
      const d = await r.json().catch(() => ({}));
      const profiles = d.profiles || [];
      if (!r.ok || !profiles.length) {
        sel.innerHTML = `<option>${esc(r.ok ? 'No profiles found' : (d.error || 'Could not load profiles'))}</option>`;
        return;
      }
      sel.innerHTML = profiles.map(p =>
        `<option value="${esc(p.id)}"${p.id === d.selected_id ? ' selected' : ''}>` +
        `${esc(p.name)}${p.primary ? ' (primary)' : ''}${p.id === d.selected_id ? ' — running' : ''}</option>`
      ).join('');
      sel.disabled = false;
      if (btn) btn.disabled = false;
      if (statusEl) statusEl.textContent = '';
    } catch (e) {
      sel.innerHTML = '<option>Could not load profiles</option>';
    }
  };

  window.directvSelectProfile = async function (sourceId) {
    const sel = document.getElementById(`directv-profile-select-${sourceId}`);
    const btn = document.getElementById(`directv-profile-btn-${sourceId}`);
    const statusEl = document.getElementById(`directv-profile-status-${sourceId}`);
    if (!sel || !sel.value) return;
    const name = (sel.options[sel.selectedIndex]?.text || '').replace(/ \(primary\)| — running/g, '');
    if (btn) btn.disabled = true;
    if (statusEl) { statusEl.style.color = 'var(--text-muted)'; statusEl.textContent = 'Switching…'; }
    try {
      const r = await fetch(`/api/sources/${sourceId}/directv-profile`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile_id: sel.value, profile_name: name}),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        if (statusEl) { statusEl.style.color = 'var(--success-soft)'; statusEl.innerHTML = '&#10003; Running as this profile.'; }
        directvLoadProfiles(sourceId);
      } else if (statusEl) {
        statusEl.style.color = 'var(--danger)';
        statusEl.textContent = d.error || 'Could not switch profile';
      }
    } catch (e) {
      if (statusEl) { statusEl.style.color = 'var(--danger)'; statusEl.textContent = 'Network error'; }
    } finally {
      if (btn) btn.disabled = false;
    }
  };

  window.directvLoadAdidStatus = async function (sourceId) {
    const btn = document.getElementById(`directv-adid-btn-${sourceId}`);
    if (!btn) return;
    try {
      const r = await fetch(`/api/sources/${sourceId}/directv-capture-adids`);
      const d = await r.json().catch(() => ({}));
      if (d.enabled && d.pending > 0) {
        btn.textContent = `Capture advertising IDs (${d.pending} new device${d.pending > 1 ? 's' : ''})`;
        btn.style.display = '';
      } else {
        btn.style.display = 'none';
      }
    } catch (e) { btn.style.display = 'none'; }
  };

  window.directvCaptureAdids = async function (sourceId) {
    const btn = document.getElementById(`directv-adid-btn-${sourceId}`);
    const statusEl = document.getElementById(`directv-adid-status-${sourceId}`);
    if (btn) btn.disabled = true;
    if (statusEl) statusEl.textContent = 'Capturing… (a Google TV/Android TV device briefly shows its Ads screen; a playing device is skipped)';
    try {
      const r = await fetch(`/api/sources/${sourceId}/directv-capture-adids`, {method: 'POST'});
      const d = await r.json().catch(() => ({}));
      if (d.enabled === false) { directvLoadAdidStatus(sourceId); return; }
      if (!r.ok || !d.ok) {
        if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">${esc((d && d.error) || 'Capture failed')}</span>`;
        return;
      }
      const parts = [`Captured ${d.captured} device${d.captured === 1 ? '' : 's'}`];
      if (d.playing) parts.push(`${d.playing} skipped — ${d.playing === 1 ? 'a device is' : 'devices are'} playing (re-run when idle)`);
      if (d.unreachable) parts.push(`${d.unreachable} couldn't be reached (check ${d.unreachable === 1 ? "it's" : "they're"} on and connected)`);
      if (d.none) parts.push(`${d.none} with no advertising ID`);
      if (statusEl) {
        const ok = !d.playing && !d.unreachable && !d.none;
        statusEl.innerHTML = ok
          ? '<span style="color:var(--success-soft)">&#10003; ' + esc(parts.join('; ')) + '.</span>'
          : esc(parts.join('; ')) + '.';
      }
    } catch (e) {
      if (statusEl) statusEl.innerHTML = '<span style="color:var(--danger)">Network error</span>';
    } finally {
      if (btn) btn.disabled = false;
      directvLoadAdidStatus(sourceId);
    }
  };
})();
"""
