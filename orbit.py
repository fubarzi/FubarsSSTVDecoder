"""Satellite positions and look angles from TLE or OMM GP elements."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'vendor'))
from sgp4.api import Satrec, jday
from sgp4 import omm

EARTH_RADIUS = 6378.137  # WGS84 equatorial radius, km
FLATTENING = 1 / 298.257223563
ECCENTRICITY_SQUARED = FLATTENING * (2 - FLATTENING)


@lru_cache(maxsize=32)
def _satrec(line1: str, line2: str):
    return Satrec.twoline2rv(line1, line2)


@lru_cache(maxsize=32)
def _omm_satrec(encoded: str):
    fields = json.loads(encoded)
    if '.' not in fields['EPOCH']:
        fields['EPOCH'] += '.000000'
    sat = Satrec()
    omm.initialize(sat, fields)
    return sat


def _elements_satrec(elements):
    return (_omm_satrec(json.dumps(elements, sort_keys=True)) if isinstance(elements, dict)
            else _satrec(*elements))


def _gmst(when: datetime) -> float:
    jd = when.timestamp() / 86400 + 2440587.5
    t = (jd - 2451545.0) / 36525.0
    degrees = 280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t - t * t * t / 38710000
    return math.radians(degrees % 360)


def _ecef(lat_deg: float, lon_deg: float, alt_km: float) -> tuple[float, float, float]:
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = EARTH_RADIUS / math.sqrt(1 - ECCENTRICITY_SQUARED * math.sin(lat) ** 2)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon),
            (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1 - ECCENTRICITY_SQUARED) + alt_km) * math.sin(lat))


def position(lines, when: datetime | None = None) -> dict:
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        raise ValueError('Orbit time must have a timezone')
    when = when.astimezone(timezone.utc)
    jd, fraction = jday(when.year, when.month, when.day, when.hour, when.minute,
                        when.second + when.microsecond / 1e6)
    error, coordinates, _velocity = _elements_satrec(lines).sgp4(jd, fraction)
    if error:
        raise ValueError(f'SGP4 propagation error {error}')
    x, y, z = coordinates  # TEME coordinates in km
    theta = _gmst(when)
    ex = x * math.cos(theta) + y * math.sin(theta)
    ey = -x * math.sin(theta) + y * math.cos(theta)
    lon = math.atan2(ey, ex)
    p = math.hypot(ex, ey)
    b = EARTH_RADIUS * (1 - FLATTENING)
    ep2 = (EARTH_RADIUS ** 2 - b ** 2) / b ** 2
    beta = math.atan2(z * EARTH_RADIUS, p * b)
    lat = math.atan2(z + ep2 * b * math.sin(beta) ** 3,
                     p - ECCENTRICITY_SQUARED * EARTH_RADIUS * math.cos(beta) ** 3)
    n = EARTH_RADIUS / math.sqrt(1 - ECCENTRICITY_SQUARED * math.sin(lat) ** 2)
    altitude = p / math.cos(lat) - n
    # Geometric radio horizon. Terrain and refraction are intentionally excluded.
    horizon_angle = math.degrees(math.acos(EARTH_RADIUS / (EARTH_RADIUS + altitude)))
    return {'lat': math.degrees(lat), 'lon': math.degrees(lon), 'alt_km': altitude,
            'footprint_deg': horizon_angle, 'time': when.isoformat().replace('+00:00', 'Z')}


def look_angles(sat: dict, home_lat: float, home_lon: float) -> dict:
    la, lo = math.radians(home_lat), math.radians(home_lon)
    hx, hy, hz = _ecef(home_lat, home_lon, 0.0)
    sx, sy, sz = _ecef(sat['lat'], sat['lon'], sat['alt_km'])
    dx, dy, dz = sx - hx, sy - hy, sz - hz
    east = -math.sin(lo) * dx + math.cos(lo) * dy
    north = -math.sin(la) * math.cos(lo) * dx - math.sin(la) * math.sin(lo) * dy + math.cos(la) * dz
    up = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    return {'range_km': math.sqrt(dx * dx + dy * dy + dz * dz),
            'azimuth_deg': math.degrees(math.atan2(east, north)) % 360,
            'elevation_deg': math.degrees(math.atan2(up, math.hypot(east, north)))}


def footprint_contains(sat: dict, home_lat: float, home_lon: float) -> bool:
    """Match the spherical coverage ring drawn around the satellite on the map."""
    sat_lat, home_lat = math.radians(sat['lat']), math.radians(home_lat)
    longitude_gap = math.radians(sat['lon'] - home_lon)
    cosine = (math.sin(sat_lat) * math.sin(home_lat) +
              math.cos(sat_lat) * math.cos(home_lat) * math.cos(longitude_gap))
    return cosine >= math.cos(math.radians(sat['footprint_deg']))


def next_home_passes(lines, home_lat: float, home_lon: float,
                     now: datetime | None = None, count: int = 4) -> list[dict]:
    """Return upcoming horizon-to-horizon passes at the observer's location."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if count <= 0:
        return []

    def elevation(when):
        return look_angles(position(lines, when), home_lat, home_lon)['elevation_deg']

    def horizon_crossing(earlier, later, rising):
        for _ in range(15):
            middle = earlier + (later - earlier) / 2
            if (elevation(middle) >= 0) == rising:
                later = middle
            else:
                earlier = middle
        return earlier + (later - earlier) / 2

    passes = []
    step = timedelta(seconds=45)
    limit = now + timedelta(days=4)
    previous_time, previous_el = now, elevation(now)
    aos = None
    peak_el = -90.0
    peak_time = now
    if previous_el >= 0:
        # Include a pass already in progress when the list is refreshed.
        earlier = now
        for _ in range(40):
            before = earlier - step
            if elevation(before) < 0:
                aos = horizon_crossing(before, earlier, True)
                peak_el, peak_time = previous_el, now
                sample = aos
                while sample < now:
                    candidate = elevation(sample)
                    if candidate > peak_el:
                        peak_el, peak_time = candidate, sample
                    sample += step
                break
            earlier = before
    while previous_time < limit and len(passes) < count:
        next_time = min(previous_time + step, limit)
        next_el = elevation(next_time)
        if previous_el < 0 <= next_el:
            aos = horizon_crossing(previous_time, next_time, True)
            peak_el, peak_time = next_el, next_time
        elif previous_el >= 0 > next_el:
            if aos is not None:
                los = horizon_crossing(previous_time, next_time, False)
                passes.append({'aos': aos, 'los': los, 'peak': peak_time,
                               'max_elevation_deg': peak_el})
            aos, peak_el = None, -90.0
        elif aos is not None and next_el > peak_el:
            peak_el, peak_time = next_el, next_time
        previous_time, previous_el = next_time, next_el
    return passes


def next_mapped_home_passes(satellites, home_lat: float, home_lon: float,
                            now: datetime | None = None, count: int = 4) -> list[dict]:
    """Merge upcoming Home passes for selected mapped satellites in time order."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    merged = []
    for item in satellites:
        try:
            for prediction in next_home_passes(item['lines'], home_lat, home_lon, now, count):
                if prediction['los'] > now:
                    merged.append({**prediction, 'satellite_id': str(item['id']),
                                   'satellite_name': item['name']})
        except Exception:
            continue
    merged.sort(key=lambda item: (item['aos'], item['satellite_id']))
    return merged[:count]
