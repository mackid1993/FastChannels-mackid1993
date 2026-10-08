"""The bridge device a DirecTV DAI tune plays on: its identity and app User-Agent.

DAI targets the playback device the way DirecTV's Android TV app does, so this reads
the bridge device behind the current request (matched by IP to bridge_devices) over
adb: Android ID, limit_ad_tracking, Android version, model, board, manufacturer. Each
device's values are saved in directv_dai_devices.json beside the database, so a device
keeps one stable identity while it's asleep or adb is down.

The same properties build the app's player User-Agent, which the relay sends on the
Yospace master fetch and every playlist and segment (A/B tested for inserted ads).

Sign-in, refresh and DRM are upstream's (directv_device_auth); nothing here touches them.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

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

