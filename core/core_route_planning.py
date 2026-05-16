# -*- coding: utf-8 -*-
"""
Route planning core module.

Provides point-to-point route planning for Baidu and Amap Web service APIs.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import arcpy
import requests

from . import core_common as common


TEXT_LEN = 32767

BAIDU_DIRECTION_URL = "https://api.map.baidu.com/direction/v1"
AMAP_ROUTE_URLS = {
    "driving": "https://restapi.amap.com/v5/direction/driving",
    "walking": "https://restapi.amap.com/v5/direction/walking",
    "bicycling": "https://restapi.amap.com/v5/direction/bicycling",
    "electrobike": "https://restapi.amap.com/v5/direction/electrobike",
    "transit": "https://restapi.amap.com/v5/direction/transit/integrated",
}


def _sfloat(v, default=None):
    try:
        return float(v)
    except Exception:
        return default


def _sint(v, default=None):
    try:
        return int(float(v))
    except Exception:
        return default


def _txt(v, default=""):
    if v is None:
        return default
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return default


def _json(v):
    try:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return _txt(v, "")


def _normalize_coord_system(system):
    text = str(system or "").strip().upper()
    text = text.replace("-", "").replace("_", "").replace(" ", "")
    if not text:
        return "WGS84"
    if "WGS84" in text or text in ("WGS", "4326") or "EPSG4326" in text:
        return "WGS84"
    if "CGCS2000" in text or "CGCS" in text:
        return "CGCS2000"
    if "GCJ02" in text or "GCJ" in text or "国测局" in text:
        return "GCJ02"
    if "BD09" in text or "BAIDU" in text or "百度" in text:
        return "BD09"
    return text


def _normalize_platform(platform):
    text = _txt(platform, "").strip().lower()
    if text in ("baidu", "百度"):
        return "baidu"
    if text in ("amap", "高德"):
        return "amap"
    raise ValueError("Unsupported platform: {}".format(platform))


def _normalize_route_mode(mode):
    text = _txt(mode, "").strip().lower()
    mapping = {
        "driving": "driving",
        "驾车": "driving",
        "car": "driving",
        "walking": "walking",
        "步行": "walking",
        "walk": "walking",
        "transit": "transit",
        "公交": "transit",
        "public": "transit",
        "bicycling": "bicycling",
        "cycling": "bicycling",
        "骑行": "bicycling",
        "electrobike": "electrobike",
        "电动车": "electrobike",
    }
    if text in mapping:
        return mapping[text]
    return text or "driving"


def _coord_to_wgs84(lng, lat, input_coord_type):
    key = _normalize_coord_system(input_coord_type)
    if key in ("WGS84", "CGCS2000"):
        return lng, lat
    return common.convert_coord(lng, lat, key, "WGS84")


def _convert_origin_dest_coords(platform, output_wgs84, lng_wgs, lat_wgs):
    if output_wgs84:
        return lng_wgs, lat_wgs, "WGS84"
    if platform == "amap":
        lng_gcj, lat_gcj = common.convert_coord(lng_wgs, lat_wgs, "WGS84", "GCJ02")
        return lng_gcj, lat_gcj, "GCJ02"
    lng_bd, lat_bd = common.convert_coord(lng_wgs, lat_wgs, "WGS84", "BD09")
    return lng_bd, lat_bd, "BD09"


def _convert_output_point(platform, output_wgs84, lng, lat):
    if output_wgs84:
        if platform == "amap":
            return common.convert_coord(lng, lat, "GCJ02", "WGS84")
        return common.convert_coord(lng, lat, "BD09", "WGS84")
    return lng, lat


def _format_baidu_coord(lat, lng):
    return "{:.6f},{:.6f}".format(lat, lng)


def _format_amap_coord(lng, lat):
    return "{:.6f},{:.6f}".format(lng, lat)


def _parse_polyline_text(text):
    points = []
    if not text:
        return points
    for seg in str(text).split(";"):
        seg = seg.strip()
        if not seg:
            continue
        parts = seg.split(",")
        if len(parts) < 2:
            continue
        try:
            lng = float(parts[0])
            lat = float(parts[1])
        except Exception:
            continue
        points.append((lng, lat))
    return points


def _append_points(target, new_points):
    for pt in new_points:
        if not target or target[-1] != pt:
            target.append(pt)


def _extract_polyline_from_steps(steps):
    points = []
    for step in steps or []:
        for key in ("polyline", "path"):
            val = step.get(key)
            if not val:
                continue
            _append_points(points, _parse_polyline_text(val))
    return points


def _extract_polyline_from_route(route):
    if not route:
        return []
    points = []
    if isinstance(route, dict):
        if route.get("polyline"):
            _append_points(points, _parse_polyline_text(route.get("polyline")))
        steps = route.get("steps") or []
        _append_points(points, _extract_polyline_from_steps(steps))
    return points


def _extract_polyline_from_transit(transit):
    points = []
    if not transit:
        return points
    segments = transit.get("segments") or []
    for seg in segments:
        walking = seg.get("walking") or {}
        steps = walking.get("steps") or []
        _append_points(points, _extract_polyline_from_steps(steps))
        bus = seg.get("bus") or {}
        for busline in bus.get("buslines", []) or []:
            polyline = busline.get("polyline")
            if polyline:
                _append_points(points, _parse_polyline_text(polyline))
        railway = seg.get("railway") or {}
        railway_polyline = railway.get("polyline")
        if railway_polyline:
            _append_points(points, _parse_polyline_text(railway_polyline))
    return points


def _read_points(in_fc, label_field, input_coord_type):
    if not arcpy.Exists(in_fc):
        raise ValueError("Input feature class does not exist: {}".format(in_fc))
    desc = arcpy.Describe(in_fc)
    if getattr(desc, "shapeType", "").lower() != "point":
        raise ValueError("Input must be point features: {}".format(in_fc))
    fields = ["OID@", "SHAPE@"]
    if label_field:
        fields.append(label_field)
    rows = []
    with arcpy.da.SearchCursor(in_fc, fields) as cursor:
        for row in cursor:
            oid = row[0]
            geom = row[1]
            if geom is None:
                continue
            pt = geom.firstPoint
            if pt is None:
                continue
            lng, lat = pt.X, pt.Y
            lng_wgs, lat_wgs = _coord_to_wgs84(lng, lat, input_coord_type)
            label = row[2] if label_field and len(row) > 2 else None
            rows.append(
                {
                    "oid": oid,
                    "label": _txt(label, ""),
                    "lng_wgs": lng_wgs,
                    "lat_wgs": lat_wgs,
                }
            )
    return rows


def _build_pairs(origins, destinations):
    if not origins or not destinations:
        raise ValueError("Origins or destinations are empty.")
    if len(origins) == len(destinations):
        return list(zip(origins, destinations))
    if len(origins) == 1:
        return [(origins[0], dest) for dest in destinations]
    if len(destinations) == 1:
        return [(orig, destinations[0]) for orig in origins]
    raise ValueError("Origins/Destinations counts mismatch and cannot be broadcast.")


def _request_baidu_route(origin, destination, mode, tactics=None, coord_type="wgs84", max_retries=None):
    if max_retries is None:
        max_retries = max(1, len(common.BAIDU_KEYS))
    tried = set()
    for retry in range(max_retries):
        key_index, current_key = common.get_next_baidu_key_info()
        if current_key is None:
            return {
                "ok": False,
                "status": "KEY_POOL_EMPTY",
                "message": "No Baidu key available",
                "reason": "key_pool_empty",
                "error_kind": "key_pool_exhausted",
                "used_key": "N/A",
                "retry_count": retry + 1,
                "data": None,
            }
        if current_key in tried:
            continue
        tried.add(current_key)
        params = {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "coord_type": coord_type,
            "output": "json",
            "ak": current_key,
        }
        if tactics is not None and mode == "driving":
            params["tactics"] = tactics
        try:
            resp = requests.get(BAIDU_DIRECTION_URL, params=params, timeout=30)
        except Exception as exc:
            cls = common.classify_baidu_response_issue(message=str(exc), error=exc)
            if cls.get("switch_key") and key_index is not None:
                common.set_key_delay("baidu", key_index, cls.get("delay_seconds", 1))
            if cls.get("switch_key") and retry < max_retries - 1:
                continue
            return {
                "ok": False,
                "status": "ERROR",
                "message": str(exc),
                "reason": cls.get("message") or str(exc),
                "error_kind": cls.get("kind", "request_error"),
                "used_key": common.mask_key(current_key),
                "retry_count": retry + 1,
                "data": None,
            }
        http_status = resp.status_code
        try:
            data = resp.json()
        except Exception:
            data = {}
        status = data.get("status")
        msg = data.get("message") or data.get("msg") or resp.reason or ""
        if str(status) == "0":
            return {
                "ok": True,
                "status": str(status),
                "message": msg,
                "reason": "",
                "error_kind": "ok",
                "used_key": common.mask_key(current_key),
                "retry_count": retry + 1,
                "data": data,
            }
        cls = common.classify_baidu_response_issue(status=status, message=msg, http_status=http_status)
        if cls.get("switch_key") and key_index is not None:
            common.set_key_delay("baidu", key_index, cls.get("delay_seconds", 1))
            if retry < max_retries - 1:
                continue
        return {
            "ok": False,
            "status": str(status),
            "message": msg,
            "reason": cls.get("message") or msg or "Baidu API error",
            "error_kind": cls.get("kind", "api_error"),
            "used_key": common.mask_key(current_key),
            "retry_count": retry + 1,
            "data": data,
        }
    return {
        "ok": False,
        "status": "RETRY_EXHAUSTED",
        "message": "Baidu route retries exhausted",
        "reason": "retry_exhausted",
        "error_kind": "retry_exhausted",
        "used_key": "N/A",
        "retry_count": max_retries,
        "data": None,
    }


def _request_amap_route(url, origin, destination, mode, strategy=None, max_retries=None):
    if max_retries is None:
        max_retries = max(1, len(common.AMAP_KEYS))
    tried = set()
    for retry in range(max_retries):
        key_index, current_key = common.get_next_amap_key_info()
        if current_key is None:
            return {
                "ok": False,
                "status": "KEY_POOL_EMPTY",
                "message": "No Amap key available",
                "reason": "key_pool_empty",
                "error_kind": "key_pool_exhausted",
                "used_key": "N/A",
                "retry_count": retry + 1,
                "data": None,
            }
        if current_key in tried:
            continue
        tried.add(current_key)
        params = {
            "key": current_key,
            "origin": origin,
            "destination": destination,
            "output": "json",
        }
        if mode == "driving" and strategy is not None:
            params["strategy"] = strategy
        if mode in ("driving", "walking", "bicycling", "electrobike", "transit"):
            params["show_fields"] = "polyline,cost,duration"
        try:
            resp = requests.get(url, params=params, timeout=30)
        except Exception as exc:
            cls = common.classify_amap_response_issue(status="0", info=str(exc), infocode="ERROR")
            if cls.get("switch_key") and key_index is not None:
                common.set_key_delay("amap", key_index, cls.get("delay_seconds", 1))
            if cls.get("switch_key") and retry < max_retries - 1:
                continue
            return {
                "ok": False,
                "status": "ERROR",
                "message": str(exc),
                "reason": cls.get("message") or str(exc),
                "error_kind": cls.get("kind", "request_error"),
                "used_key": common.mask_key(current_key),
                "retry_count": retry + 1,
                "data": None,
            }
        try:
            data = resp.json()
        except Exception:
            data = {}
        status = data.get("status", "0")
        info = data.get("info", "")
        infocode = data.get("infocode", "")
        if str(status) == "1" and str(infocode) == "10000":
            return {
                "ok": True,
                "status": str(status),
                "message": info,
                "reason": "",
                "error_kind": "ok",
                "used_key": common.mask_key(current_key),
                "retry_count": retry + 1,
                "data": data,
            }
        cls = common.classify_amap_response_issue(status=status, info=info, infocode=infocode)
        if cls.get("switch_key") and key_index is not None:
            common.set_key_delay("amap", key_index, cls.get("delay_seconds", 1))
            if retry < max_retries - 1:
                continue
        return {
            "ok": False,
            "status": str(status),
            "message": info,
            "reason": cls.get("message") or info or "Amap API error",
            "error_kind": cls.get("kind", "api_error"),
            "used_key": common.mask_key(current_key),
            "retry_count": retry + 1,
            "data": data,
        }
    return {
        "ok": False,
        "status": "RETRY_EXHAUSTED",
        "message": "Amap route retries exhausted",
        "reason": "retry_exhausted",
        "error_kind": "retry_exhausted",
        "used_key": "N/A",
        "retry_count": max_retries,
        "data": None,
    }


def _parse_baidu_route_result(data):
    result = (data or {}).get("result") or {}
    routes = result.get("routes") or []
    if not routes:
        return None, None, []
    route = routes[0]
    distance = _sfloat(route.get("distance"))
    duration = _sfloat(route.get("duration"))
    points = _extract_polyline_from_route(route)
    return distance, duration, points


def _parse_amap_route_result(data, mode):
    route = (data or {}).get("route") or {}
    if mode == "transit":
        transits = route.get("transits") or []
        if not transits:
            return None, None, []
        transit = transits[0]
        distance = _sfloat(transit.get("distance"))
        duration = _sfloat(transit.get("duration"))
        points = _extract_polyline_from_transit(transit)
        return distance, duration, points
    paths = route.get("paths") or []
    if not paths:
        return None, None, []
    path = paths[0]
    distance = _sfloat(path.get("distance"))
    duration = _sfloat(path.get("duration"))
    points = _extract_polyline_from_route(path)
    return distance, duration, points


def _build_polyline_geom(points, spatial_ref):
    if not points:
        return None
    arr = arcpy.Array([arcpy.Point(pt[0], pt[1]) for pt in points])
    return arcpy.Polyline(arr, spatial_ref)


def _create_route_feature_class(out_gdb, out_fc_name):
    sr = arcpy.SpatialReference(4326)
    out_fc = os.path.join(out_gdb, out_fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, "POLYLINE", spatial_reference=sr)
    fields = [
        ("pair_id", "TEXT", 64, "PairID"),
        ("origin_oid", "LONG", "OriginOID"),
        ("dest_oid", "LONG", "DestOID"),
        ("origin_label", "TEXT", 255, "OriginLabel"),
        ("dest_label", "TEXT", 255, "DestLabel"),
        ("platform", "TEXT", 10, "Platform"),
        ("route_mode", "TEXT", 20, "RouteMode"),
        ("route_rank", "SHORT", "RouteRank"),
        ("origin_x", "DOUBLE", "OriginX"),
        ("origin_y", "DOUBLE", "OriginY"),
        ("dest_x", "DOUBLE", "DestX"),
        ("dest_y", "DOUBLE", "DestY"),
        ("distance_m", "DOUBLE", "Distance_m"),
        ("duration_s", "DOUBLE", "Duration_s"),
        ("strategy", "TEXT", 50, "Strategy"),
        ("coord_sys", "TEXT", 16, "CoordSys"),
        ("status", "TEXT", 50, "Status"),
        ("message", "TEXT", 500, "Message"),
        ("reason", "TEXT", 500, "Reason"),
        ("used_key", "TEXT", 32, "UsedKey"),
        ("retry_count", "LONG", "RetryCount"),
        ("raw_response", "TEXT", TEXT_LEN, "RawResponse"),
    ]
    for spec in fields:
        if len(spec) == 4:
            name, field_type, length, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_length=length, field_alias=alias)
        else:
            name, field_type, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_alias=alias)
    return out_fc, sr


def _create_summary_table(out_gdb, table_name):
    out_table = os.path.join(out_gdb, table_name)
    if arcpy.Exists(out_table):
        arcpy.management.Delete(out_table)
    arcpy.management.CreateTable(out_gdb, table_name)
    fields = [
        ("pair_id", "TEXT", 64, "PairID"),
        ("origin_oid", "LONG", "OriginOID"),
        ("dest_oid", "LONG", "DestOID"),
        ("origin_label", "TEXT", 255, "OriginLabel"),
        ("dest_label", "TEXT", 255, "DestLabel"),
        ("platform", "TEXT", 10, "Platform"),
        ("route_mode", "TEXT", 20, "RouteMode"),
        ("route_rank", "SHORT", "RouteRank"),
        ("origin_x", "DOUBLE", "OriginX"),
        ("origin_y", "DOUBLE", "OriginY"),
        ("dest_x", "DOUBLE", "DestX"),
        ("dest_y", "DOUBLE", "DestY"),
        ("distance_m", "DOUBLE", "Distance_m"),
        ("duration_s", "DOUBLE", "Duration_s"),
        ("strategy", "TEXT", 50, "Strategy"),
        ("coord_sys", "TEXT", 16, "CoordSys"),
        ("status", "TEXT", 50, "Status"),
        ("message", "TEXT", 500, "Message"),
        ("reason", "TEXT", 500, "Reason"),
        ("used_key", "TEXT", 32, "UsedKey"),
        ("retry_count", "LONG", "RetryCount"),
        ("raw_response", "TEXT", TEXT_LEN, "RawResponse"),
    ]
    for spec in fields:
        if len(spec) == 4:
            name, field_type, length, alias = spec
            arcpy.management.AddField(out_table, name, field_type, field_length=length, field_alias=alias)
        else:
            name, field_type, alias = spec
            arcpy.management.AddField(out_table, name, field_type, field_alias=alias)
    return out_table


def _create_failure_table(out_gdb, table_name):
    out_table = os.path.join(out_gdb, table_name)
    if arcpy.Exists(out_table):
        arcpy.management.Delete(out_table)
    arcpy.management.CreateTable(out_gdb, table_name)
    fields = [
        ("pair_id", "TEXT", 64, "PairID"),
        ("origin_oid", "LONG", "OriginOID"),
        ("dest_oid", "LONG", "DestOID"),
        ("origin_label", "TEXT", 255, "OriginLabel"),
        ("dest_label", "TEXT", 255, "DestLabel"),
        ("platform", "TEXT", 10, "Platform"),
        ("route_mode", "TEXT", 20, "RouteMode"),
        ("origin_x", "DOUBLE", "OriginX"),
        ("origin_y", "DOUBLE", "OriginY"),
        ("dest_x", "DOUBLE", "DestX"),
        ("dest_y", "DOUBLE", "DestY"),
        ("status", "TEXT", 50, "Status"),
        ("message", "TEXT", 500, "Message"),
        ("reason", "TEXT", 500, "Reason"),
        ("used_key", "TEXT", 32, "UsedKey"),
        ("retry_count", "LONG", "RetryCount"),
        ("raw_response", "TEXT", TEXT_LEN, "RawResponse"),
    ]
    for spec in fields:
        if len(spec) == 4:
            name, field_type, length, alias = spec
            arcpy.management.AddField(out_table, name, field_type, field_length=length, field_alias=alias)
        else:
            name, field_type, alias = spec
            arcpy.management.AddField(out_table, name, field_type, field_alias=alias)
    return out_table


def _route_one(task):
    pair_id, origin, dest, platform, mode, strategy, output_wgs84 = task
    platform_key = _normalize_platform(platform)
    mode_key = _normalize_route_mode(mode)
    origin_lng_wgs = origin["lng_wgs"]
    origin_lat_wgs = origin["lat_wgs"]
    dest_lng_wgs = dest["lng_wgs"]
    dest_lat_wgs = dest["lat_wgs"]
    if platform_key == "amap":
        origin_lng_gcj, origin_lat_gcj = common.convert_coord(
            origin_lng_wgs, origin_lat_wgs, "WGS84", "GCJ02"
        )
        dest_lng_gcj, dest_lat_gcj = common.convert_coord(
            dest_lng_wgs, dest_lat_wgs, "WGS84", "GCJ02"
        )
        req_origin = _format_amap_coord(origin_lng_gcj, origin_lat_gcj)
        req_dest = _format_amap_coord(dest_lng_gcj, dest_lat_gcj)
        url = AMAP_ROUTE_URLS.get(mode_key)
        if not url:
            return {
                "pair_id": pair_id,
                "success": False,
                "error": "Unsupported mode for Amap: {}".format(mode),
                "detail": {
                    "status": "MODE_UNSUPPORTED",
                    "message": "Unsupported mode",
                    "reason": "mode_not_supported",
                    "used_key": "",
                    "retry_count": 0,
                    "raw_response": "",
                },
            }
        response = _request_amap_route(url, req_origin, req_dest, mode_key, strategy=strategy)
        if not response.get("ok"):
            return {
                "pair_id": pair_id,
                "success": False,
                "error": response.get("reason") or "Amap route failed",
                "detail": response,
            }
        distance, duration, points_raw = _parse_amap_route_result(response.get("data"), mode_key)
        raw_points = points_raw or []
        if not raw_points:
            raw_points = [(origin_lng_gcj, origin_lat_gcj), (dest_lng_gcj, dest_lat_gcj)]
        points = [_convert_output_point("amap", output_wgs84, p[0], p[1]) for p in raw_points]
        origin_x, origin_y, coord_sys = _convert_origin_dest_coords(
            "amap", output_wgs84, origin_lng_wgs, origin_lat_wgs
        )
        dest_x, dest_y, _ = _convert_origin_dest_coords(
            "amap", output_wgs84, dest_lng_wgs, dest_lat_wgs
        )
        return {
            "pair_id": pair_id,
            "success": True,
            "record": {
                "origin_oid": origin["oid"],
                "dest_oid": dest["oid"],
                "origin_label": origin["label"],
                "dest_label": dest["label"],
                "platform": "Amap",
                "route_mode": mode_key,
                "route_rank": 1,
                "origin_x": origin_x,
                "origin_y": origin_y,
                "dest_x": dest_x,
                "dest_y": dest_y,
                "distance_m": distance,
                "duration_s": duration,
                "strategy": _txt(strategy, ""),
                "coord_sys": coord_sys,
                "status": response.get("status", ""),
                "message": response.get("message", ""),
                "reason": response.get("reason", ""),
                "used_key": response.get("used_key", ""),
                "retry_count": response.get("retry_count", 0),
                "raw_response": _json(response.get("data")),
                "points": points,
            },
        }
    origin_str = _format_baidu_coord(origin_lat_wgs, origin_lng_wgs)
    dest_str = _format_baidu_coord(dest_lat_wgs, dest_lng_wgs)
    response = _request_baidu_route(origin_str, dest_str, mode_key, tactics=strategy, coord_type="wgs84")
    if not response.get("ok"):
        return {
            "pair_id": pair_id,
            "success": False,
            "error": response.get("reason") or "Baidu route failed",
            "detail": response,
        }
    distance, duration, points_raw = _parse_baidu_route_result(response.get("data"))
    raw_points = points_raw or []
    if not raw_points:
        raw_points = [(origin_lng_wgs, origin_lat_wgs), (dest_lng_wgs, dest_lat_wgs)]
    points = [_convert_output_point("baidu", output_wgs84, p[0], p[1]) for p in raw_points]
    origin_x, origin_y, coord_sys = _convert_origin_dest_coords(
        "baidu", output_wgs84, origin_lng_wgs, origin_lat_wgs
    )
    dest_x, dest_y, _ = _convert_origin_dest_coords(
        "baidu", output_wgs84, dest_lng_wgs, dest_lat_wgs
    )
    return {
        "pair_id": pair_id,
        "success": True,
        "record": {
            "origin_oid": origin["oid"],
            "dest_oid": dest["oid"],
            "origin_label": origin["label"],
            "dest_label": dest["label"],
            "platform": "Baidu",
            "route_mode": mode_key,
            "route_rank": 1,
            "origin_x": origin_x,
            "origin_y": origin_y,
            "dest_x": dest_x,
            "dest_y": dest_y,
            "distance_m": distance,
            "duration_s": duration,
            "strategy": _txt(strategy, ""),
            "coord_sys": coord_sys,
            "status": response.get("status", ""),
            "message": response.get("message", ""),
            "reason": response.get("reason", ""),
            "used_key": response.get("used_key", ""),
            "retry_count": response.get("retry_count", 0),
            "raw_response": _json(response.get("data")),
            "points": points,
        },
    }


def run_route_planning(
    origin_fc,
    dest_fc,
    origin_label_field=None,
    dest_label_field=None,
    input_coord_type="WGS84",
    platform="baidu",
    route_mode="driving",
    strategy=None,
    output_wgs84=True,
    speed_level="medium",
    use_in_memory=False,
    out_gdb=None,
    out_fc_name=None,
    out_summary_table_name=None,
    out_failure_table_name=None,
    max_workers=None,
):
    arcpy.env.overwriteOutput = True
    if not out_fc_name:
        raise ValueError("out_fc_name is required.")
    if not out_gdb:
        raise ValueError("out_gdb is required.")

    origins = _read_points(origin_fc, origin_label_field, input_coord_type)
    destinations = _read_points(dest_fc, dest_label_field, input_coord_type)
    pairs = _build_pairs(origins, destinations)

    platform_key = _normalize_platform(platform)
    mode_key = _normalize_route_mode(route_mode)
    if platform_key == "baidu" and mode_key not in ("driving", "walking", "transit"):
        raise ValueError("Baidu only supports driving/walking/transit.")
    if platform_key == "amap" and mode_key not in AMAP_ROUTE_URLS:
        raise ValueError("Amap route mode not supported: {}".format(route_mode))

    if max_workers is None:
        key_count = len(common.BAIDU_KEYS) if platform_key == "baidu" else len(common.AMAP_KEYS)
        max_workers = common.calculate_max_workers(speed_level, key_count, key_count)

    if use_in_memory:
        out_gdb = "memory"
    elif out_gdb.lower() == "in_memory":
        out_gdb = "memory"

    if not out_summary_table_name:
        out_summary_table_name = "{}_summary".format(out_fc_name)
    if not out_failure_table_name:
        out_failure_table_name = "{}_failed".format(out_fc_name)

    tasks = []
    for origin, dest in pairs:
        pair_id = "{}_{}".format(origin["oid"], dest["oid"])
        tasks.append((pair_id, origin, dest, platform_key, mode_key, strategy, output_wgs84))

    all_records = []
    failures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_route_one, task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result.get("success"):
                all_records.append((result["pair_id"], result["record"]))
            else:
                detail = result.get("detail") or {}
                failures.append(
                    {
                        "pair_id": result.get("pair_id"),
                        "platform": "Amap" if platform_key == "amap" else "Baidu",
                        "route_mode": mode_key,
                        "status": detail.get("status", ""),
                        "message": detail.get("message", ""),
                        "reason": detail.get("reason", result.get("error", "")),
                        "used_key": detail.get("used_key", ""),
                        "retry_count": detail.get("retry_count", 0),
                        "raw_response": _json(detail.get("data")) if detail else "",
                        "origin_oid": None,
                        "dest_oid": None,
                        "origin_label": "",
                        "dest_label": "",
                        "origin_x": None,
                        "origin_y": None,
                        "dest_x": None,
                        "dest_y": None,
                    }
                )

    out_fc, sr = _create_route_feature_class(out_gdb, out_fc_name)
    out_summary = _create_summary_table(out_gdb, out_summary_table_name)
    out_failure = _create_failure_table(out_gdb, out_failure_table_name)

    insert_fields = [
        "SHAPE@",
        "pair_id",
        "origin_oid",
        "dest_oid",
        "origin_label",
        "dest_label",
        "platform",
        "route_mode",
        "route_rank",
        "origin_x",
        "origin_y",
        "dest_x",
        "dest_y",
        "distance_m",
        "duration_s",
        "strategy",
        "coord_sys",
        "status",
        "message",
        "reason",
        "used_key",
        "retry_count",
        "raw_response",
    ]
    summary_fields = insert_fields[1:]
    with arcpy.da.InsertCursor(out_fc, insert_fields) as fc_cursor, arcpy.da.InsertCursor(
        out_summary, summary_fields
    ) as tbl_cursor:
        for pair_id, record in all_records:
            pts = record.get("points") or []
            geom = _build_polyline_geom(pts, sr)
            row = [
                geom,
                pair_id,
                record.get("origin_oid"),
                record.get("dest_oid"),
                record.get("origin_label"),
                record.get("dest_label"),
                record.get("platform"),
                record.get("route_mode"),
                record.get("route_rank"),
                record.get("origin_x"),
                record.get("origin_y"),
                record.get("dest_x"),
                record.get("dest_y"),
                record.get("distance_m"),
                record.get("duration_s"),
                record.get("strategy"),
                record.get("coord_sys"),
                record.get("status"),
                record.get("message"),
                record.get("reason"),
                record.get("used_key"),
                record.get("retry_count"),
                record.get("raw_response"),
            ]
            fc_cursor.insertRow(row)
            tbl_cursor.insertRow(row[1:])

    failure_fields = [
        "pair_id",
        "origin_oid",
        "dest_oid",
        "origin_label",
        "dest_label",
        "platform",
        "route_mode",
        "origin_x",
        "origin_y",
        "dest_x",
        "dest_y",
        "status",
        "message",
        "reason",
        "used_key",
        "retry_count",
        "raw_response",
    ]
    if failures:
        with arcpy.da.InsertCursor(out_failure, failure_fields) as fail_cursor:
            for record in failures:
                fail_cursor.insertRow([record.get(name) for name in failure_fields])

    return out_fc, out_summary, out_failure


class RoutePlanningLineTool(object):
    """Module export for .pyt import."""

    @staticmethod
    def run(
        origin_fc,
        dest_fc,
        origin_label_field=None,
        dest_label_field=None,
        input_coord_type="WGS84",
        platform="baidu",
        route_mode="driving",
        strategy=None,
        output_wgs84=True,
        speed_level="medium",
        use_in_memory=False,
        out_gdb=None,
        out_fc_name=None,
        out_summary_table_name=None,
        out_failure_table_name=None,
        max_workers=None,
    ):
        return run_route_planning(
            origin_fc=origin_fc,
            dest_fc=dest_fc,
            origin_label_field=origin_label_field,
            dest_label_field=dest_label_field,
            input_coord_type=input_coord_type,
            platform=platform,
            route_mode=route_mode,
            strategy=strategy,
            output_wgs84=output_wgs84,
            speed_level=speed_level,
            use_in_memory=use_in_memory,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            out_summary_table_name=out_summary_table_name,
            out_failure_table_name=out_failure_table_name,
            max_workers=max_workers,
        )
