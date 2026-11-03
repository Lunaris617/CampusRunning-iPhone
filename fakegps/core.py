"""Core device management and location simulation via pymobiledevice3.

Handles iOS version detection and delegates to the appropriate API:
- iOS < 17: DtSimulateLocation (lockdown-based)
- iOS 17+: LocationSimulation via DvtProvider through tunneld
"""

import asyncio
import subprocess
import threading
import concurrent.futures
import os
import sys
import shutil
import time
import shlex
import socket
import tempfile
from dataclasses import dataclass


@dataclass
class DeviceInfo:
    udid: str
    name: str
    ios_version: str


# ── Internal Helpers ──


async def _get_lockdown(serial=None):
    from pymobiledevice3.lockdown import create_using_usbmux
    return await create_using_usbmux(serial=serial)


async def _get_lockdown_via_tunneld(udid=None):
    """Get an RSD client for *udid* through a running tunneld (iOS 17+ DVT).

    Two upstream behaviours make "the device is not reachable" look like
    something else, and both are handled here:

    * ``get_tunneld_device_by_udid`` returns ``None`` when tunneld has no
      tunnel for the UDID.  Returning that ``None`` to the caller made
      ``DvtProvider(None)`` fail with ``'NoneType' object has no attribute
      'product_version'``, which says nothing about the real cause.
    * ``get_tunneld_devices`` silently drops tunnels whose RSD handshake
      fails, and ``get_tunneld_device_by_udid`` raises ``IndexError`` when it
      filtered the only tunnel out — the old code reported that as
      "tunneld started but the device channel was not ready".
    """
    from pymobiledevice3.tunneld.api import (
        get_tunneld_devices, get_tunneld_device_by_udid, TUNNELD_DEFAULT_ADDRESS
    )
    last_error = None
    for _ in range(6):
        try:
            if udid:
                device = await get_tunneld_device_by_udid(udid, TUNNELD_DEFAULT_ADDRESS)
                if device is not None:
                    return device
                last_error = ConnectionError(f"tunneld has no tunnel for {udid}")
            else:
                devices = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
                if devices:
                    return devices[0]
                last_error = ConnectionError("No devices found via tunneld")
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(1.0)
    raise ConnectionError(
        "No usable device tunnel — unlock the iPhone, unplug and replug the USB "
        "cable, wait for tunneld to list the device, then retry "
        f"(last error: {last_error!r})"
    ) from last_error


async def _query_device_info(lockdown):
    """Extract (version, udid) from a lockdown client with multiple fallbacks."""
    version = "0"
    udid = None
    try:
        info = lockdown.short_info or {}
        version = info.get("ProductVersion") or ""
        udid = info.get("UniqueDeviceID") or lockdown.udid
    except Exception:
        pass
    if not version or version == "0":
        try:
            version = lockdown.product_version or "0"
        except Exception:
            pass
    if not udid:
        udid = lockdown.udid
    return version, udid


def _ios_major(version_str):
    try:
        return int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return 0


async def _get_device_session_context(serial=None):
    """Unified device info: query version/udid, return (major, udid).

    Eliminates the duplicated lockdown-query-close pattern that was
    repeated in set_location, clear_location, and play_gpx_file.
    """
    lockdown = await _get_lockdown(serial=serial)
    try:
        version, udid = await _query_device_info(lockdown)
    finally:
        await lockdown.close()
    return _ios_major(version), udid


async def _open_dvt_session(udid):
    """Open DvtProvider + LocationSimulation for iOS 17+.

    Returns ``(provider, loc_sim, rsd)``.  The RSD (the tunnel connection) is
    returned too, because ``DtxServiceProvider.close()`` only closes the DTX
    connection on top of it — the RSD itself is never closed by the provider.
    Leaking one RSD per call is what makes the second and every later open fail
    on iOS 17+, so the caller has to hand it back to _close_dvt_session.
    """
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    rsd = await _get_lockdown_via_tunneld(udid)
    try:
        provider = DvtProvider(rsd)
        await provider.__aenter__()
    except Exception:
        await _close_quietly(rsd)
        raise
    try:
        loc_sim = LocationSimulation(provider)
        await loc_sim.__aenter__()
    except Exception:
        try:
            await provider.__aexit__(None, None, None)
        except Exception:
            pass
        await _close_quietly(rsd)
        raise
    return provider, loc_sim, rsd


async def _close_quietly(obj):
    """Close *obj* if it exists, swallowing every error."""
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        pass


async def _close_dvt_session(provider, loc_sim, rsd=None):
    """Safely close a DVT session and the tunnel connection under it."""
    if loc_sim:
        try:
            await loc_sim.__aexit__(None, None, None)
        except Exception:
            pass
    if provider:
        try:
            await provider.__aexit__(None, None, None)
        except Exception:
            pass
    await _close_quietly(rsd)


# ── Public API ──


async def list_connected_devices():
    """List all connected iOS devices."""
    from pymobiledevice3.usbmux import list_devices
    devices = await list_devices()
    result = []
    for dev in devices:
        name = ""
        ios_version = ""
        udid = dev.serial
        try:
            lockdown = await _get_lockdown(serial=dev.serial)
            info = lockdown.short_info or {}
            name = info.get("DeviceName") or ""
            ios_version = info.get("ProductVersion") or ""
            udid = info.get("UniqueDeviceID") or dev.serial
            # Fallback: fresh query via get_value
            if not name or not ios_version:
                try:
                    name = name or await lockdown.get_value("DeviceName") or ""
                    ios_version = ios_version or await lockdown.get_value("ProductVersion") or ""
                except Exception:
                    pass
            if not name:
                try:
                    name = lockdown.product_type or ""
                except Exception:
                    pass
            if not name:
                name = f"iPhone ({dev.serial[:8]}...)"
            if not ios_version:
                ios_version = "Unknown"
            await lockdown.close()
        except Exception:
            name = name or f"Device ({dev.serial[:8]}...)"
            ios_version = ios_version or "Unknown"
        result.append(DeviceInfo(udid=udid, name=name, ios_version=ios_version))
    return result


def list_android_devices():
    """Return Android devices visible through the optional ADB executable."""
    adb = shutil.which("adb")
    if not adb:
        return []
    try:
        lines = subprocess.run([adb, "devices", "-l"], capture_output=True,
                               text=True, timeout=8, check=False).stdout.splitlines()
        return [line.split()[0] for line in lines[1:] if "\tdevice" in line]
    except (OSError, subprocess.SubprocessError):
        return []


_tunneld_process = None
_tunneld_error = ""

TUNNELD_HOST = "127.0.0.1"
TUNNELD_PORT = 49151


def _is_frozen():
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _tunneld_log_path():
    """Where tunneld stdout/stderr is kept for post-mortem debugging."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CampusRunning-tunneld.log")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/CampusRunning-tunneld.log")
    return os.path.expanduser("~/.campusrunning-tunneld.log")


def _tunneld_command():
    """Command line that starts the pymobiledevice3 tunneld server.

    Inside a frozen bundle ``sys.executable`` is the CampusRunning binary itself,
    so it is re-executed with the ``--tunneld`` helper flag (handled in
    run_gui.py).  Using ``-m pymobiledevice3`` there would launch a second
    GUI window instead of the daemon.
    """
    if _is_frozen():
        return [sys.executable, "--tunneld"]
    return [sys.executable, "-m", "pymobiledevice3", "remote", "tunneld"]


def _is_elevated():
    """True when the current process already has root/admin rights."""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return True


def _spawn_detached(command):
    """Spawn tunneld as a detached background process (already elevated).

    Output goes to _tunneld_log_path() so a failing daemon can be diagnosed.
    """
    global _tunneld_process
    try:
        log_fh = open(_tunneld_log_path(), "ab")
    except OSError:
        log_fh = subprocess.DEVNULL
    kwargs = {"stdout": log_fh, "stderr": log_fh,
              "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    try:
        _tunneld_process = subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False
    finally:
        if log_fh is not subprocess.DEVNULL:
            log_fh.close()


def _launchdaemon_plist(command, log_path, label):
    """Generate a LaunchDaemon plist XML for the tunneld helper.

    launchd keeps the daemon alive independently of the osascript
    privileged-helper session that installed it.
    """
    args_xml = "\n".join("        <string>" + a + "</string>" for a in command)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "    <key>Label</key>\n    <string>" + label + "</string>\n"
        "    <key>ProgramArguments</key>\n    <array>\n"
        + args_xml + "\n    </array>\n"
        "    <key>RunAtLoad</key>\n    <true/>\n"
        "    <key>KeepAlive</key>\n    <false/>\n"
        "    <key>StandardOutPath</key>\n    <string>" + log_path + "</string>\n"
        "    <key>StandardErrorPath</key>\n    <string>" + log_path + "</string>\n"
        "</dict>\n</plist>\n"
    )


def ensure_tunneld():
    """Make sure the pymobiledevice3 tunneld server is up (iOS 17+).

    tunneld needs root/admin rights to create the tunnel interface, so when
    the app itself is not elevated it is started with elevation:

    - macOS:   native administrator password prompt (osascript)
    - Windows: UAC elevation prompt (ShellExecuteW "runas")

    Returns True once the server answers on 127.0.0.1:49151.  On failure
    the module-level _tunneld_error explains why (shown to the user).
    """
    global _tunneld_error
    _tunneld_error = ""

    if _tunnel_port_ready():
        return True

    command = _tunneld_command()
    log_path = _tunneld_log_path()

    if _is_elevated():
        if not _spawn_detached(command):
            _tunneld_error = "Failed to spawn the tunneld helper process"
            return False
    elif sys.platform == "darwin":
        # Launch via LaunchDaemon plist + launchctl.  The old nohup+&
        # approach failed because osascript's privileged helper kills
        # background children when its session exits.  With launchctl
        # the daemon is managed by launchd and survives independently.
        plist_label = "com.campusrunning.tunneld"
        plist_dest = "/Library/LaunchDaemons/" + plist_label + ".plist"
        plist_content = _launchdaemon_plist(command, log_path, plist_label)

        # Write plist to temp (no elevation needed for /tmp).
        plist_tmp = os.path.join(tempfile.gettempdir(), plist_label + ".plist")
        try:
            with open(plist_tmp, "w") as fh:
                fh.write(plist_content)
        except OSError:
            _tunneld_error = "Could not write the tunneld launch configuration"
            return False

        # Privileged step: install plist + (re)start the daemon.
        # bootout first (ignore failure if not loaded), then bootstrap.
        shell_command = (
            "cp " + shlex.quote(plist_tmp) + " " + shlex.quote(plist_dest)
            + " && launchctl bootout system/" + plist_label + " 2>/dev/null; "
            + "launchctl bootstrap system " + shlex.quote(plist_dest)
        )
        apple_script = ('do shell script "'
                        + shell_command.replace('"', '\\"')
                        + '" with administrator privileges')
        try:
            prompt = subprocess.run(
                ["osascript", "-e", apple_script],
                capture_output=True, text=True, timeout=180, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            _tunneld_error = "Could not show the administrator password prompt"
            return False
        if prompt.returncode != 0:
            # User cancelled the password prompt (or osascript failed).
            _tunneld_error = ("Administrator password prompt was cancelled — "
                              "click Set Location again and enter your Mac password "
                              "when the system dialog appears")
            return False
    elif os.name == "nt":
        # UAC elevation on Windows.  SW_HIDE (0) keeps the helper invisible.
        try:
            import ctypes
            params = " ".join('"%s"' % arg for arg in command[1:])
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", command[0], params, None, 0)
            if result <= 32:
                # User declined the UAC prompt or the launch failed.
                _tunneld_error = ("UAC elevation was declined — click Set Location "
                                  "again and approve the administrator prompt")
                return False
        except Exception:
            _tunneld_error = "Could not launch the elevated tunneld helper"
            return False
    else:
        # Linux and others: best effort without elevation.
        if not _spawn_detached(command):
            _tunneld_error = "Failed to spawn the tunneld helper process"
            return False

    # Wait until the RSD endpoint answers (first tunnel can take a while).
    for _ in range(25):
        if _tunnel_port_ready():
            return True
        time.sleep(0.8)
    _tunneld_error = ("tunneld did not come up on 127.0.0.1:49151 — "
                      "see " + log_path + " for details")
    return False


def _tunnel_port_ready():
    """Check the local RSD endpoint, not just a matching process name."""
    try:
        with socket.create_connection((TUNNELD_HOST, TUNNELD_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def run_tunneld_forever():
    """Run the tunneld server in the foreground (--tunneld helper mode).

    This is what the frozen bundle executes when re-launched (elevated) by
    ensure_tunneld(), so the daemon runs without opening a second GUI.
    """
    from pymobiledevice3.remote.common import TunnelProtocol
    from pymobiledevice3.tunneld.server import TunneldRunner

    # Pin TCP explicitly.  Do NOT use TunnelProtocol.DEFAULT here: upstream
    # defines it as
    #     DEFAULT = TCP if sys.version_info >= (3, 13) else QUIC
    # and the frozen bundle ships Python 3.12, so DEFAULT resolves to QUIC.
    # Apple removed QUIC tunnel support in iOS 18.2+, which makes every
    # WiFi/remoted tunnel attempt fail with QuicProtocolNotSupportedError.
    # Only the WiFi path consumes this value -- the USB paths (usbmux,
    # CDC-NCM, mobdev2) already hard-code TCP -- so pinning TCP aligns WiFi
    # with the transport USB has always used.  The PSK support the TCP
    # tunnel needs on Python < 3.13 comes from sslpsk-pmd3, which upstream
    # declares as a dependency exactly for that version range.
    TunneldRunner.create(TUNNELD_HOST, TUNNELD_PORT, protocol=TunnelProtocol.TCP)


async def set_location(latitude, longitude, serial=None):
    """Set simulated GPS location on connected iPhone.

    Returns a session dict that must be kept alive for the spoof to persist.
    For iOS 17+: {"provider", "sim", "ios_major"}
    For iOS <17:  {"lockdown", "sim", "ios_major"}
    """
    latitude = float(latitude)
    longitude = float(longitude)
    major, udid = await _get_device_session_context(serial)

    if major >= 17:
        if not ensure_tunneld():
            raise ConnectionError(_tunneld_error or "tunneld is not ready on 127.0.0.1:49151")
        # The RemoteXPC/DTX channel occasionally dies mid-flight (e.g. zlib
        # "incorrect header check" on a corrupted stream).  Such errors poison
        # the whole session, so retry by rebuilding the session from scratch.
        last_error = None
        for attempt in range(3):
            provider = loc_sim = rsd = None
            try:
                provider, loc_sim, rsd = await _open_dvt_session(udid)
                await loc_sim.set(latitude, longitude)
                return {"provider": provider, "sim": loc_sim, "rsd": rsd,
                        "ios_major": major}
            except Exception as exc:
                last_error = exc
                await _close_dvt_session(provider, loc_sim, rsd)
                if attempt < 2:
                    await asyncio.sleep(1.0 + attempt)
        raise ConnectionError(
            f"Location update failed after 3 attempts: {last_error}") from last_error
    else:
        lockdown = await _get_lockdown(serial=serial)
        from pymobiledevice3.services.simulate_location import DtSimulateLocation
        sim = DtSimulateLocation(lockdown)
        await sim.set(latitude, longitude)
        return {"lockdown": lockdown, "sim": sim, "ios_major": major}


async def clear_location(serial=None):
    """Clear simulated location (restore real GPS)."""
    major, udid = await _get_device_session_context(serial)

    if major >= 17:
        ensure_tunneld()
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        # Rebuild the whole session on failure — a corrupted channel cannot
        # be recovered from inside the same session.
        last_error = None
        for attempt in range(3):
            rsd = provider = loc_sim = None
            try:
                rsd = await _get_lockdown_via_tunneld(udid)
                provider = DvtProvider(rsd)
                await provider.__aenter__()
                loc_sim = LocationSimulation(provider)
                await loc_sim.__aenter__()
                await loc_sim.clear()
                return
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1.0 + attempt)
            finally:
                # The RSD is not closed by the provider — leaking it here keeps
                # the tunnel slot busy and breaks every later call.
                await _close_dvt_session(provider, loc_sim, rsd)
        raise ConnectionError(
            f"Clear failed after 3 attempts: {last_error}") from last_error
    else:
        lockdown = await _get_lockdown(serial=serial)
        from pymobiledevice3.services.simulate_location import DtSimulateLocation
        sim = DtSimulateLocation(lockdown)
        await sim.clear()
        await lockdown.close()


# A track can contain a pause (lunch, a tunnel, a flight) — honouring a
# 40-minute gap would look like a frozen UI, so a single gap is capped.
MAX_TIMESTAMP_GAP_S = 60.0


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def _playback_delays(points, speed_kmh):
    """Return (delays, has_time): seconds to wait *before* each point.

    Delays come from the file's ``<time>`` values when it has them, and from
    the distance between consecutive points at *speed_kmh* when it does not.
    """
    if any(len(point) > 3 and point[3] is not None for point in points):
        delays = [0.0] * len(points)
        previous = None
        for index, point in enumerate(points):
            when = point[3] if len(point) > 3 else None
            if previous is not None and when is not None:
                gap = (when - previous).total_seconds()
                if gap > 0:
                    delays[index] = min(gap, MAX_TIMESTAMP_GAP_S)
            if when is not None:
                previous = when
        return delays, True

    delays = [0.0] * len(points)
    if speed_kmh > 0:
        speed_ms = speed_kmh * 1000.0 / 3600.0
        for index in range(1, len(points)):
            previous, current = points[index - 1], points[index]
            delays[index] = _haversine_m(previous[0], previous[1],
                                         current[0], current[1]) / speed_ms
    return delays, False


async def _sleep_cancellable(delay, should_stop=None):
    """Sleep *delay* seconds, waking every 250 ms to honour a Stop request."""
    deadline = time.monotonic() + delay
    while True:
        if should_stop is not None and should_stop():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(remaining, 0.25))


async def play_gpx_file(filepath, serial=None, speed_kmh=5.0, session=None,
                        should_stop=None, on_progress=None):
    """Play a GPX trajectory on the device.

    Pacing comes from the file's ``<time>`` elements when present, otherwise
    from the haversine distance between points at *speed_kmh* (default 5 km/h,
    walking pace).  Playback is driven here rather than by
    ``LocationSimulation.play_gpx_file`` for three reasons: that helper reads
    the file with the locale codec (so GBK/UTF-8 files raised
    ``UnicodeDecodeError`` on a Chinese Windows), it gives no progress or
    cancellation, and it always opens a *second* DVT channel while the GUI's
    "Set Location" channel is still open.

    :param session: result of :func:`set_location`; reusing it keeps a single
        LocationSimulation client on the device.
    :param should_stop: zero-argument callable polled between points.
    :param on_progress: ``callable(sent, total)`` after each point.
    :returns: the session that now owns the simulated location.  The location
        disappears when that channel closes, so the caller should keep it.
    """
    from .gpx import parse_gpx

    points = parse_gpx(filepath)
    if not points:
        raise ValueError(f"GPX file contains no track points: {filepath}")

    speed_kmh = float(speed_kmh)
    delays, has_time = _playback_delays(points, speed_kmh)
    if not has_time and speed_kmh <= 0:
        raise ValueError("GPX playback speed must be greater than 0 km/h")

    major, udid = await _get_device_session_context(serial)
    total = len(points)
    sent = 0

    def _report():
        if on_progress is None:
            return
        try:
            on_progress(sent, total)
        except Exception:
            pass

    def _stopped():
        return should_stop is not None and should_stop()

    if major >= 17:
        provider = loc_sim = rsd = None
        if session and session.get("ios_major", 0) >= 17 and session.get("sim"):
            provider, loc_sim = session.get("provider"), session.get("sim")
            rsd = session.get("rsd")

        if loc_sim is None:
            # Open a fresh channel, retrying the transport errors that a
            # just-plugged device produces (zlib/RemoteXPC hiccups poison a
            # session, so the whole channel is rebuilt).
            last_error = None
            for attempt in range(3):
                try:
                    provider, loc_sim, rsd = await _open_dvt_session(udid)
                    break
                except Exception as exc:
                    last_error = exc
                    provider = loc_sim = rsd = None
                    if attempt < 2:
                        await asyncio.sleep(1.0 + attempt)
            if loc_sim is None:
                raise ConnectionError(
                    "Could not open the location simulation channel: "
                    f"{last_error!r}") from last_error

        try:
            for index, point in enumerate(points):
                if _stopped():
                    break
                await loc_sim.set(point[0], point[1])
                sent = index + 1
                _report()
                if index + 1 < total:
                    if not await _sleep_cancellable(delays[index + 1], should_stop):
                        break
        except Exception as exc:
            await _close_dvt_session(provider, loc_sim, rsd)
            raise ConnectionError(
                f"GPX playback failed after {sent}/{total} points: {exc!r}") from exc
        return {"provider": provider, "sim": loc_sim, "rsd": rsd, "ios_major": major}

    # iOS < 17: the lockdown based service, closed when playback ends.
    lockdown = await _get_lockdown(serial=serial)
    from pymobiledevice3.services.simulate_location import DtSimulateLocation
    sim = DtSimulateLocation(lockdown)
    try:
        for index, point in enumerate(points):
            if _stopped():
                break
            await sim.set(point[0], point[1])
            sent = index + 1
            _report()
            if index + 1 < total:
                if not await _sleep_cancellable(delays[index + 1], should_stop):
                    break
    finally:
        try:
            await lockdown.close()
        except Exception:
            pass
    return {"ios_major": major}


async def _get_serials(serials=None):
    """Resolve device serials: ``None`` = all connected devices, otherwise validate."""
    if serials is not None:
        return list(serials)
    devices = await list_connected_devices()
    if not devices:
        raise ConnectionError("No connected devices found")
    return [d.udid for d in devices]


async def set_location_multi(latitude, longitude, serials=None):
    """Set simulated location on all *serials* devices in parallel.

    :param serials: list of UDIDs, or ``None`` for all connected devices.
    :returns: list of session dicts (one per device).
    """
    udids = await _get_serials(serials)

    async def _one(udid):
        rsd = None
        try:
            result = await set_location(latitude, longitude, serial=udid)
            return result
        except Exception:
            raise

    tasks = [_one(udid) for udid in udids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    errors = []
    for udid, res in zip(udids, results):
        if isinstance(res, Exception):
            errors.append(f"{udid[:12]}...: {res!r}")
        else:
            res["udid"] = udid
            output.append(res)
    if errors:
        raise ConnectionError(
            f"Location set failed on {len(errors)}/{len(udids)} device(s): "
            "; ".join(errors))
    return output


async def play_gpx_multi(filepath, *, serials=None, speed_kmh=5.0,
                         sessions=None, should_stop=None,
                         on_device_progress=None):
    """Play a GPX trajectory simultaneously on multiple devices.

    :param serials: list of UDIDs, or ``None`` for all connected.
    :param sessions: optional ``{udid: session}`` mapping so an existing
        LocationSimulation channel (from Set Location) is reused per device.
    :param on_device_progress: ``callable(udid, sent, total)`` after each point.
    :returns: list of session dicts in the same order as *serials*.
    """
    udids = await _get_serials(serials)
    sessions = sessions or {}

    async def _one(udid):
        def _prog(s, t):
            if on_device_progress is not None:
                try:
                    on_device_progress(udid, s, t)
                except Exception:
                    pass
        result = await play_gpx_file(
            filepath, serial=udid, speed_kmh=speed_kmh,
            session=sessions.get(udid), should_stop=should_stop, on_progress=_prog)
        result["udid"] = udid
        return result

    tasks = [_one(udid) for udid in udids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    errors = []
    for udid, res in zip(udids, results):
        if isinstance(res, Exception):
            errors.append(f"{udid[:12]}...: {res!r}")
        else:
            output.append(res)
    if errors and not output:
        raise ConnectionError(
            f"GPX playback failed on all {len(errors)} device(s): "
            "; ".join(errors))
    return output


async def clear_location_multi(serials=None):
    """Clear simulated location on all *serials* devices in parallel."""
    udids = await _get_serials(serials)

    async def _one(udid):
        try:
            await clear_location(serial=udid)
            return udid, None
        except Exception as exc:
            return udid, exc

    results = await asyncio.gather(*[_one(u) for u in udids], return_exceptions=False)
    errors = [(udid, exc) for udid, exc in results if exc is not None]
    if errors:
        raise ConnectionError(
            f"Clear failed on {len(errors)}/{len(udids)} device(s): "
            "; ".join(f"{udid[:12]}...: {exc!r}" for udid, exc in errors))


def check_tunneld_running():
    """Check whether the tunneld server is up.

    The listening RSD port is the authoritative signal on every platform:
    process-name scans miss the frozen helper (CampusRunning.exe --tunneld) and
    can match stale processes.
    """
    return _tunnel_port_ready()


_loop = None
_loop_thread = None


def _get_persistent_loop():
    """Get (or create) a persistent background event loop."""
    global _loop, _loop_thread
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
        _loop_thread.start()
    return _loop


def run_async(coro):
    """Run an async function synchronously on a persistent event loop."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running and running.is_running():
        future = concurrent.futures.Future()

        async def _wrapper():
            try:
                result = await coro
                future.set_result(result)
            except Exception as e:
                future.set_exception(e)

        running.create_task(_wrapper())
        return future.result()
    else:
        loop = _get_persistent_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()


def run_async_cancellable(coro):
    """Schedule a coroutine and return a future that can cancel it."""
    loop = _get_persistent_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop)
