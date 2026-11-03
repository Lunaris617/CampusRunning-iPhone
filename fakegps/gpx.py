"""GPX file parsing for trajectory playback.

Why this module reads the bytes itself
-------------------------------------
GPX files in the wild come as UTF-8 (sometimes with a BOM), as GBK/GB18030
written by Chinese exporters that also declare ``encoding="GBK"``, and
occasionally as UTF-16.  Two things used to break playback on Windows:

* ``open(path)`` reads with the *locale* codec (cp936 on a Chinese system), so
  any UTF-8 file containing non-ASCII text (a Chinese ``<name>``, a BOM) raised
  ``UnicodeDecodeError: 'gbk' codec can't decode byte ...``.
* Python's expat refuses a document whose XML declaration names a multi-byte
  codec, raising ``ValueError: multi-byte encodings are not supported``.

Both aborted the whole playback before a single point reached the phone.  So
the file is read as bytes here, decoded explicitly, and the XML declaration is
dropped before parsing: expat then only ever sees a plain ``str``.

Namespace handling is by local tag name too, so GPX 1.0, GPX 1.1, files with no
namespace and files with a different one all parse.
"""

import codecs
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_DECL_ENCODING = re.compile(rb"""encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")

# Tried in order after the declared encoding.  gb18030 is a superset of GBK and
# GB2312, so it covers every Chinese export without needing to guess which one.
_FALLBACK_ENCODINGS = ("utf-8", "gb18030", "big5", "cp1252")

# ``2026-10-01T10:00:00Z``, ``2026-10-01 10:00:00``, ``...+08:00``, fractions.
_TIME_RE = re.compile(
    r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{1,2}):(\d{1,2})"
    r"(?:\.(\d+))?\s*(Z|[+-]\d{1,2}:?\d{0,2})?\s*$",
    re.IGNORECASE,
)


def _decode(raw):
    """Decode GPX bytes, honouring a BOM and the XML declaration."""
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", "replace")

    candidates = []
    match = _DECL_ENCODING.search(raw[:512])
    if match:
        declared = match.group(1).decode("ascii", "ignore")
        # cp936/gbk/gb2312 are all covered by gb18030 and some of those names are
        # not valid Python codecs, so normalise the Chinese family.
        if declared and declared.lower().replace("_", "-") in (
            "gbk", "gb2312", "gb-2312", "cp936", "x-gbk", "gb18030",
        ):
            declared = "gb18030"
        if declared:
            candidates.append(declared)
    candidates.extend(enc for enc in _FALLBACK_ENCODINGS if enc not in candidates)

    for encoding in candidates:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1")


def _local_name(tag):
    """Tag name without its ``{namespace}`` prefix."""
    return tag.rpartition("}")[2]


def _children(element, name):
    return [child for child in element if _local_name(child.tag) == name]


def _child_text(element, name):
    child = next((c for c in element if _local_name(c.tag) == name), None)
    return child.text if child is not None else None


def parse_time(text):
    """Parse a GPX ``<time>`` into an aware UTC datetime, or None.

    A missing or unreadable timestamp is not an error: the point still gets
    played, it just carries no timing information.
    """
    match = _TIME_RE.match(text or "")
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    fraction = (match.group(7) or "")[:6].ljust(6, "0")
    zone = match.group(8)
    try:
        tzinfo = timezone.utc
        if zone and zone.upper() != "Z":
            sign = -1 if zone[0] == "-" else 1
            digits = zone[1:].replace(":", "")
            hours = int(digits[:2] or 0)
            minutes = int(digits[2:4] or 0)
            tzinfo = timezone(sign * timedelta(hours=hours, minutes=minutes))
        return datetime(year, month, day, hour, minute, second,
                        int(fraction), tzinfo).astimezone(timezone.utc)
    except ValueError:
        return None


def _points_from(element):
    """Yield (lat, lon, ele, time) for every point-ish child of *element*."""
    for point in element:
        name = _local_name(point.tag)
        if name not in ("trkpt", "rtept", "wpt"):
            continue
        try:
            lat = float(point.get("lat"))
            lon = float(point.get("lon"))
        except (TypeError, ValueError):
            continue
        try:
            ele = float(_child_text(point, "ele"))
        except (TypeError, ValueError):
            ele = 0.0
        yield (lat, lon, ele, parse_time(_child_text(point, "time")))


def parse_gpx(filepath):
    """Parse a .gpx file and return a list of (lat, lng, elevation, time).

    ``time`` is an aware UTC ``datetime`` or ``None``.  Tracks, segments,
    routes and bare waypoints are all accepted, in document order.
    """
    raw = Path(filepath).expanduser().read_bytes()
    if not raw.strip():
        raise ValueError(f"GPX file is empty: {filepath}")

    text = _XML_DECL.sub("", _decode(raw), count=1)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"{filepath} is not a valid GPX/XML file: {exc}") from exc

    points = []
    for track in _children(root, "trk"):
        for segment in _children(track, "trkseg"):
            points.extend(_points_from(segment))
    if not points:
        for route in _children(root, "rte"):
            points.extend(_points_from(route))
    if not points:
        points.extend(_points_from(root))

    if not points:
        raise ValueError(f"GPX file contains no track points: {filepath}")
    return points


def track_name(filepath):
    """Return the first ``<trk><name>`` (best effort, for the log pane)."""
    try:
        raw = Path(filepath).expanduser().read_bytes()
        text = _XML_DECL.sub("", _decode(raw), count=1)
        root = ET.fromstring(text)
        for track in _children(root, "trk"):
            name = _child_text(track, "name")
            if name:
                return name.strip()
    except Exception:
        pass
    return ""
