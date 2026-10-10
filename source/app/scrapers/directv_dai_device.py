"""The bridge device a DirecTV DAI tune plays on: its identity and app User-Agent.

DAI targets the playback device the way DirecTV's Android TV app does, so this reads
the bridge device behind the current request (matched by IP to bridge_devices) over
adb: Android ID, limit_ad_tracking, Android version, model, board, manufacturer. Each
device's values are saved in directv_dai_devices.json beside the database, so a device
keeps one stable identity while it's asleep or adb is down.

The same properties build the app's player User-Agent, which the relay sends on the
Yospace master fetch and every playlist and segment with DAI on (A/B tested for
inserted ads).

Sign-in, refresh and DRM are upstream's (directv_device_auth); nothing here touches them.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
    over adb in the background (never in the request: adb can take seconds, and
    this runs on every relay fetch); until that read lands it has no values.
    After that it's re-checked in the background about once an hour; if it
    really changed (factory reset gives a new Android ID, ad tracking toggled,
    firmware update), the saved values are updated and logged. A failed read or
    re-check changes nothing. {} when the request isn't from a known bridge
    device, or outside a request."""
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
    saved = _saved_devices().get(address) or {}
    props = {k: v for k, v in saved.items() if k != 'checked_at'}
    if props:
        _DEVICE_CACHE[address] = (now + _DEVICE_TTL, props)
        # Re-checked about hourly (the stamp is on disk, so worker recycles don't re-run adb).
        if now - float(saved.get('checked_at') or 0) >= _DEVICE_TTL:
            spawn(_recheck_device, address, props)
        return props
    # First sight: hold off repeat reads for _RETRY_TTL, and read in the background, but
    # give the read a few seconds so this first session already carries the device's
    # identity (a stuck adb never holds the request longer than that).
    _DEVICE_CACHE[address] = (now + _RETRY_TTL, {})
    done = threading.Event()
    spawn(_first_read, address, done)
    done.wait(_FIRST_READ_WAIT)
    hit = _DEVICE_CACHE.get(address)
    return hit[1] if hit else {}


_FIRST_READ_WAIT = 4


def spawn(fn, *args) -> None:
    """Run fn in a daemon thread, inside the current Flask app context (the device
    store's path and the database come from the app). The overlay's one background
    runner."""
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = None

    def _run():
        # fn always runs (its own cleanup with it), even if the app context can't be
        # entered; it then just has no app context and fails/logs on its own.
        ctx = None
        try:
            if app is not None:
                try:
                    ctx = app.app_context()
                    ctx.push()
                except Exception:
                    ctx = None
                    logger.warning('[directv-dai] background %s: no app context', getattr(fn, '__name__', 'task'))
            fn(*args)
        except Exception as exc:
            logger.warning('[directv-dai] background %s failed: %s', getattr(fn, '__name__', 'task'), exc)
        finally:
            if ctx is not None:
                try:
                    ctx.pop()
                except Exception:
                    pass
    threading.Thread(target=_run, daemon=True).start()


def _first_read(address: str, done: threading.Event) -> None:
    try:
        props = _read_device(address)
        if props:
            _store_device(address, props)
            _DEVICE_CACHE[address] = (time.time() + _DEVICE_TTL, props)
            logger.info('[directv-dai] saved device values for %s', address)
        else:
            logger.warning('[directv-dai] adb read failed for %s; its sessions have no device id until a read works', address)
    finally:
        done.set()


def _recheck_device(address: str, saved: dict) -> None:
    """Background re-check of the device IDENTITY only (never its advertising id, which is
    captured once and kept): update the saved values only if the device changed."""
    fresh = _read_device(address)
    if fresh:
        _store_device(address, fresh)   # also stamps checked_at
    if fresh and fresh != saved:
        changed = sorted(k for k in fresh if fresh.get(k) != saved.get(k))
        _DEVICE_CACHE[address] = (time.time() + _DEVICE_TTL, fresh)
        logger.info('[directv-dai] device %s changed (%s); saved values updated', address, ', '.join(changed))
        # A different Android ID at the same address is a different (or factory-reset) box:
        # the advertising id captured there belongs to the old one. Stop sending it and ask
        # for a capture (never captured automatically: a capture can interrupt playback).
        if saved.get('android_id') and fresh.get('android_id') != saved.get('android_id'):
            try:
                from .directv_dai import mark_box_replaced
                mark_box_replaced(address)
            except Exception:
                logger.exception('[directv-dai] could not flag the replaced box %s', address)


def _store_device(address: str, props: dict) -> None:
    from .directv_dai import _flock_polled
    lock = None
    try:
        lock = open(_devices_file() + '.lock', 'w')
        _flock_polled(lock)
    except Exception:
        lock = None
    try:
        devices = _saved_devices()
        devices[address] = {**props, 'checked_at': int(time.time())}
        _save_devices(devices)
    finally:
        if lock is not None:
            lock.close()


_RETRY_TTL = 60

# Request IP -> bridge address. The device list comes from upstream's bridge_devices,
# which reads the database and asks ah4c over HTTP (up to ~5 s when ah4c is slow or
# down), and the relay looks a requester up on every playlist and segment fetch. So the
# LIST is cached, not each IP:
# - Reads run in the background, one at a time; no lock is ever held across the read.
# - A list once read is served while a refresh runs after _BRIDGE_TTL.
# - When ah4c reports an error (upstream returns it, it doesn't raise), the ah4c tuners
#   from the last good list are kept (merged with what the database still lists), and
#   unknown requesters stop triggering reads until ah4c answers again.
# - An unknown requester (maybe a box added since) starts a refresh at most every
#   _BRIDGE_MISS_REFRESH per IP and waits at most _BRIDGE_MISS_WAIT for it.
_BRIDGE_TTL = 60
_BRIDGE_FAIL_HOLD = 60        # after an ah4c/database failure, misses don't re-read for this long
_BRIDGE_MISS_REFRESH = 10
_BRIDGE_MISS_WAIT = 1.5
_BRIDGE_FIRST_WAIT = 6        # the very first read in a process (ah4c's own timeout is 5 s)
_BRIDGE_LIST: list = [0.0, None]   # [read at, [{'address', 'host'}] or None]
_bridge_lock = threading.Lock()    # guards the in-flight slot only; never held across I/O
_bridge_inflight: list = [None]    # threading.Event of the running read, or None
_bridge_failed_at = [0.0]
_bridge_outage_logged = [False]
_bridge_miss_at: dict[str, float] = {}


def _match(devices, ip: str) -> str | None:
    return next((d['address'] for d in devices or []
                 if d['host'] == ip or d['address'].split(':')[0] == ip), None)


def _bridge_address(ip: str) -> str | None:
    address = _match(_cached_bridge_devices(), ip)
    if address:
        return address
    now = time.time()
    if (now - _bridge_miss_at.get(ip, 0) < _BRIDGE_MISS_REFRESH
            or now - _bridge_failed_at[0] < _BRIDGE_FAIL_HOLD):
        return None
    with _bridge_lock:   # never held across I/O
        _bridge_miss_at[ip] = now
        if len(_bridge_miss_at) > 1024:   # bounded: drop the oldest half
            for k in sorted(_bridge_miss_at, key=_bridge_miss_at.get)[:512]:
                _bridge_miss_at.pop(k, None)
    done = _start_bridge_refresh()
    if done is not None:
        done.wait(_BRIDGE_MISS_WAIT)
    return _match(_BRIDGE_LIST[1], ip)


def forget_bridge_devices() -> None:
    """Drop the cached device list, in memory and on disk (the next lookup reads it again)."""
    try:
        os.remove(_bridges_file())
    except OSError:
        pass
    _BRIDGE_LIST[:] = [0.0, None]
    _bridge_failed_at[0] = 0.0
    _bridge_outage_logged[0] = False
    _bridge_miss_at.clear()


def _cached_bridge_devices() -> list[dict]:
    if _BRIDGE_LIST[1] is None:
        _load_bridge_snapshot()   # another worker's (or a previous process's) last read
    read_at, devices = _BRIDGE_LIST
    if devices is not None:
        if time.time() - read_at > _BRIDGE_TTL:
            _start_bridge_refresh()
        return devices
    # Nothing read yet in this process: wait (bounded) for the first read.
    done = _start_bridge_refresh()
    if done is not None:
        done.wait(_BRIDGE_FIRST_WAIT)
    return _BRIDGE_LIST[1] or []


def _start_bridge_refresh():
    """Start a background read unless one is running. Returns the running read's Event."""
    with _bridge_lock:
        done = _bridge_inflight[0]
        if done is not None:
            return done
        done = _bridge_inflight[0] = threading.Event()

    def _run():
        try:
            _read_bridge_devices()
        finally:
            _bridge_inflight[0] = None
            done.set()
    try:
        spawn(_run)
    except Exception:
        logger.exception('[directv-dai] could not start the bridge-device read')
        _bridge_inflight[0] = None
        done.set()
    return done


def _read_bridge_devices() -> bool:
    """One read of upstream's device list into the cache. False when ah4c (or the
    database) failed; then the last good list's devices are kept."""
    try:
        devices, error = bridge_device_list()
    except Exception as exc:
        devices, error = [], f'{type(exc).__name__}: {exc}'
    now = time.time()
    if error:
        _bridge_failed_at[0] = now
        kept = {d['address']: d for d in (_BRIDGE_LIST[1] or [])}
        kept.update({d['address']: d for d in devices})
        _BRIDGE_LIST[:] = [now, list(kept.values())]
        if not _bridge_outage_logged[0]:
            _bridge_outage_logged[0] = True
            logger.warning('[directv-dai] bridge device list incomplete (%s); keeping the last known '
                           'devices until it answers again', error)
        return False
    if _bridge_outage_logged[0]:
        _bridge_outage_logged[0] = False
        logger.info('[directv-dai] bridge device list readable again')
    _bridge_failed_at[0] = 0.0
    _BRIDGE_LIST[:] = [now, devices]
    _save_bridge_snapshot(now, devices)
    return True


def _bridges_file() -> str:
    return os.path.join(os.path.dirname(_devices_file()), 'directv_dai_bridges.json')


def _load_bridge_snapshot() -> None:
    """Seed this process's list from the last good read on disk (every gunicorn worker
    and each recycled worker starts with it, so none waits on ah4c to recognize a box)."""
    try:
        with open(_bridges_file(), encoding='utf-8') as f:
            data = json.load(f)
        devices = [d for d in data.get('devices') or []
                   if isinstance(d, dict) and d.get('address') and d.get('host')]
        if _BRIDGE_LIST[1] is None:
            _BRIDGE_LIST[:] = [float(data.get('read_at') or 0), devices]
    except Exception:
        pass


def _save_bridge_snapshot(read_at: float, devices: list) -> None:
    path = _bridges_file()
    try:
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'read_at': read_at, 'devices': devices}, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug('[directv-dai] could not save the bridge device list: %s', exc)


def known_bridge_devices() -> list[dict]:
    """Every bridge device upstream knows (HDMI Capture, ah4c tuners, remembered boxes) as
    [{'address': 'host:port', 'host': 'host'}]."""
    return bridge_device_list()[0]


def bridge_device_list() -> tuple[list[dict], str | None]:
    """(devices, error): upstream's device list and its ah4c error (upstream returns the
    error rather than raising when ah4c can't be asked). The overlay's only read of
    upstream's device list, so if upstream reshapes it, this is the one place to adapt."""
    from .directv_dai_install import up
    raw, error = up('known_devices')()
    out = []
    for d in raw:
        address = str((d or {}).get('address') or '').strip()
        if address:
            out.append({'address': address, 'host': str(d.get('host') or address.rsplit(':', 1)[0])})
    return out, (str(error) if error else None)


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
        tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(devices, f)
        os.replace(tmp, path)
    except Exception as exc:
        logger.debug('[directv-dai] could not save device values: %s', exc)


def player_user_agent() -> str | None:
    """The DirecTV app User-Agent the relay and the DAI authorization send for the
    bridge device making the current request: upstream's own fixed app string
    (directv_device_auth.APP_USER_AGENT), the one its sign-in, refresh and DRM calls
    send, so DirecTV sees one unchanging client on the session's every request.
    None when the request isn't from a known bridge device. The string is fixed, so
    it doesn't wait for the box's identity to be read: being a bridge device is enough."""
    try:
        from flask import has_request_context, request
        ip = (request.remote_addr or '').strip() if has_request_context() else ''
    except Exception:
        ip = ''
    if not ip or not _bridge_address(ip):
        return None
    from .directv_dai_install import up
    return up('app_user_agent')


