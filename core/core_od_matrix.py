# -*- coding: utf-8 -*-
"""
Baidu OD matrix core module.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import arcpy
import requests

from . import core_common as common


TEXT_LEN = 32767
BAIDU_ROUTE_MATRIX_URL = "https://api.map.baidu.com/direction/v1/routematrix"


def _sfloat(v, default=None):
    try:
        return float(v)
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


def _coord_to_wgs84(lng, lat, input_coord_type):
    key = _normalize_coord_system(input_coord_type)
    if key in ("WGS84", "CGCS2000"):
        return lng, lat
    return common.convert_coord(lng, lat, key, "WGS84")


def _convert_output_coords(output_wgs84, lng_wgs, lat_wgs):
    if output_wgs84:
        return lng_wgs, lat_wgs, "WGS84"
    lng_bd, lat_bd = common.convert_coord(lng_wgs, lat_wgs, "WGS84", "BD09")
    return lng_bd, lat_bd, "BD09"


def _format_baidu_coord(lat, lng):
    return "{:.6f},{:.6f}".format(lat, lng)


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
        for idx, row in enumerate(cursor, 1):
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
                    "seq": idx,
                    "oid": oid,
                    "label": _txt(label, ""),
                    "lng_wgs": lng_wgs,
                    "lat_wgs": lat_wgs,
                }
            )
    return rows


def _chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _request_baidu_matrix(origins, destinations, mode="driving", tactics=None, coord_type="wgs84", max_retries=None):
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
            "origins": origins,
            "destinations": destinations,
            "mode": mode,
            "coord_type": coord_type,
            "output": "json",
            "ak": current_key,
        }
        if tactics is not None and mode == "driving":
            params["tactics"] = tactics
        try:
            resp = requests.get(BAIDU_ROUTE_MATRIX_URL, params=params, timeout=30)
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
        "message": "Baidu OD matrix retries exhausted",
        "reason": "retry_exhausted",
        "error_kind": "retry_exhausted",
        "used_key": "N/A",
        "retry_count": max_retries,
        "data": None,
    }


def _create_matrix_table(out_gdb, table_name):
    out_table = os.path.join(out_gdb, table_name)
    if arcpy.Exists(out_table):
        arcpy.management.Delete(out_table)
    arcpy.management.CreateTable(out_gdb, table_name)
    fields = [
        ("matrix_batch_id", "TEXT", 50, "BatchID"),
        ("matrix_row", "LONG", "RowIndex"),
        ("matrix_col", "LONG", "ColIndex"),
        ("origin_seq", "LONG", "OriginSeq"),
        ("dest_seq", "LONG", "DestSeq"),
        ("origin_oid", "LONG", "OriginOID"),
        ("dest_oid", "LONG", "DestOID"),
        ("origin_label", "TEXT", 255, "OriginLabel"),
        ("dest_label", "TEXT", 255, "DestLabel"),
        ("origin_x", "DOUBLE", "OriginX"),
        ("origin_y", "DOUBLE", "OriginY"),
        ("dest_x", "DOUBLE", "DestX"),
        ("dest_y", "DOUBLE", "DestY"),
        ("platform", "TEXT", 10, "Platform"),
        ("route_mode", "TEXT", 20, "RouteMode"),
        ("distance_m", "DOUBLE", "Distance_m"),
        ("duration_s", "DOUBLE", "Duration_s"),
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
        ("matrix_batch_id", "TEXT", 50, "BatchID"),
        ("origin_seq", "LONG", "OriginSeq"),
        ("dest_seq", "LONG", "DestSeq"),
        ("origin_oid", "LONG", "OriginOID"),
        ("dest_oid", "LONG", "DestOID"),
        ("origin_label", "TEXT", 255, "OriginLabel"),
        ("dest_label", "TEXT", 255, "DestLabel"),
        ("platform", "TEXT", 10, "Platform"),
        ("route_mode", "TEXT", 20, "RouteMode"),
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


def _normalize_matrix_mode(mode):
    text = _txt(mode, "").strip().lower()
    if text in ("driving", "驾车", "驾车矩阵", "car"):
        return "driving"
    if text in ("walking", "步行", "步行矩阵", "walk"):
        return "walking"
    return "driving"


def _parse_elements(data):
    if not data:
        return []
    if isinstance(data.get("result"), dict) and data.get("result", {}).get("elements") is not None:
        return data.get("result", {}).get("elements") or []
    if isinstance(data.get("result"), list):
        return data.get("result") or []
    if data.get("elements") is not None:
        return data.get("elements") or []
    return []


def _process_batch(task):
    batch_id, origins, destinations, mode, tactics, output_wgs84 = task
    origin_str = "|".join([_format_baidu_coord(o["lat_wgs"], o["lng_wgs"]) for o in origins])
    dest_str = "|".join([_format_baidu_coord(d["lat_wgs"], d["lng_wgs"]) for d in destinations])
    response = _request_baidu_matrix(origin_str, dest_str, mode=mode, tactics=tactics, coord_type="wgs84")
    if not response.get("ok"):
        failures = []
        for o in origins:
            for d in destinations:
                failures.append(
                    {
                        "matrix_batch_id": batch_id,
                        "origin_seq": o["seq"],
                        "dest_seq": d["seq"],
                        "origin_oid": o["oid"],
                        "dest_oid": d["oid"],
                        "origin_label": o["label"],
                        "dest_label": d["label"],
                        "platform": "Baidu",
                        "route_mode": mode,
                        "status": response.get("status", ""),
                        "message": response.get("message", ""),
                        "reason": response.get("reason", ""),
                        "used_key": response.get("used_key", ""),
                        "retry_count": response.get("retry_count", 0),
                        "raw_response": _json(response.get("data")),
                    }
                )
        return [], failures

    elements = _parse_elements(response.get("data"))
    records = []
    failures = []
    dest_count = len(destinations)
    coord_sys = "WGS84" if output_wgs84 else "BD09"
    for i, o in enumerate(origins):
        for j, d in enumerate(destinations):
            idx = i * dest_count + j
            element = elements[idx] if idx < len(elements) else None
            origin_x, origin_y, _ = _convert_output_coords(output_wgs84, o["lng_wgs"], o["lat_wgs"])
            dest_x, dest_y, _ = _convert_output_coords(output_wgs84, d["lng_wgs"], d["lat_wgs"])
            if not element:
                failures.append(
                    {
                        "matrix_batch_id": batch_id,
                        "origin_seq": o["seq"],
                        "dest_seq": d["seq"],
                        "origin_oid": o["oid"],
                        "dest_oid": d["oid"],
                        "origin_label": o["label"],
                        "dest_label": d["label"],
                        "platform": "Baidu",
                        "route_mode": mode,
                        "status": "EMPTY",
                        "message": "No element",
                        "reason": "empty_element",
                        "used_key": response.get("used_key", ""),
                        "retry_count": response.get("retry_count", 0),
                        "raw_response": _json(response.get("data")),
                    }
                )
                continue
            dist_val = None
            dur_val = None
            distance = element.get("distance")
            duration = element.get("duration")
            if isinstance(distance, dict):
                dist_val = _sfloat(distance.get("value"))
            else:
                dist_val = _sfloat(distance)
            if isinstance(duration, dict):
                dur_val = _sfloat(duration.get("value"))
            else:
                dur_val = _sfloat(duration)
            if dist_val is None or dur_val is None:
                failures.append(
                    {
                        "matrix_batch_id": batch_id,
                        "origin_seq": o["seq"],
                        "dest_seq": d["seq"],
                        "origin_oid": o["oid"],
                        "dest_oid": d["oid"],
                        "origin_label": o["label"],
                        "dest_label": d["label"],
                        "platform": "Baidu",
                        "route_mode": mode,
                        "status": "NO_METRIC",
                        "message": "Missing distance/duration",
                        "reason": "missing_metric",
                        "used_key": response.get("used_key", ""),
                        "retry_count": response.get("retry_count", 0),
                        "raw_response": _json(response.get("data")),
                    }
                )
                continue
            records.append(
                {
                    "matrix_batch_id": batch_id,
                    "matrix_row": i + 1,
                    "matrix_col": j + 1,
                    "origin_seq": o["seq"],
                    "dest_seq": d["seq"],
                    "origin_oid": o["oid"],
                    "dest_oid": d["oid"],
                    "origin_label": o["label"],
                    "dest_label": d["label"],
                    "origin_x": origin_x,
                    "origin_y": origin_y,
                    "dest_x": dest_x,
                    "dest_y": dest_y,
                    "platform": "Baidu",
                    "route_mode": mode,
                    "distance_m": dist_val,
                    "duration_s": dur_val,
                    "coord_sys": coord_sys,
                    "status": response.get("status", ""),
                    "message": response.get("message", ""),
                    "reason": response.get("reason", ""),
                    "used_key": response.get("used_key", ""),
                    "retry_count": response.get("retry_count", 0),
                    "raw_response": _json(response.get("data")),
                }
            )
    return records, failures


def run_baidu_od_matrix(
    origin_fc,
    dest_fc,
    origin_label_field=None,
    dest_label_field=None,
    input_coord_type="WGS84",
    matrix_mode="driving",
    tactics=None,
    output_wgs84=True,
    speed_level="medium",
    use_in_memory=False,
    out_gdb=None,
    out_table_name=None,
    out_failure_table_name=None,
    max_workers=None,
):
    arcpy.env.overwriteOutput = True
    if not out_table_name:
        raise ValueError("out_table_name is required.")
    if not out_gdb:
        raise ValueError("out_gdb is required.")

    origins = _read_points(origin_fc, origin_label_field, input_coord_type)
    destinations = _read_points(dest_fc, dest_label_field, input_coord_type)
    if not origins or not destinations:
        raise ValueError("Origins or destinations are empty.")

    mode = _normalize_matrix_mode(matrix_mode)

    if use_in_memory:
        out_gdb = "memory"
    elif out_gdb.lower() == "in_memory":
        out_gdb = "memory"

    if not out_failure_table_name:
        out_failure_table_name = "{}_failed".format(out_table_name)

    if max_workers is None:
        key_count = len(common.BAIDU_KEYS)
        max_workers = common.calculate_max_workers(speed_level, key_count, key_count)

    origin_chunks = list(_chunk_list(origins, 5))
    dest_chunks = list(_chunk_list(destinations, 5))
    tasks = []
    batch_id = 0
    for oi, o_chunk in enumerate(origin_chunks, 1):
        for di, d_chunk in enumerate(dest_chunks, 1):
            batch_id += 1
            tasks.append((str(batch_id), o_chunk, d_chunk, mode, tactics, output_wgs84))

    all_records = []
    failures = []
    max_workers = max(1, min(max_workers, len(tasks) or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_process_batch, task): task for task in tasks}
        for future in as_completed(futures):
            records, fails = future.result()
            all_records.extend(records)
            failures.extend(fails)

    out_table = _create_matrix_table(out_gdb, out_table_name)
    out_failure = _create_failure_table(out_gdb, out_failure_table_name)

    table_fields = [
        "matrix_batch_id",
        "matrix_row",
        "matrix_col",
        "origin_seq",
        "dest_seq",
        "origin_oid",
        "dest_oid",
        "origin_label",
        "dest_label",
        "origin_x",
        "origin_y",
        "dest_x",
        "dest_y",
        "platform",
        "route_mode",
        "distance_m",
        "duration_s",
        "coord_sys",
        "status",
        "message",
        "reason",
        "used_key",
        "retry_count",
        "raw_response",
    ]
    if all_records:
        with arcpy.da.InsertCursor(out_table, table_fields) as cursor:
            for record in all_records:
                cursor.insertRow([record.get(name) for name in table_fields])

    failure_fields = [
        "matrix_batch_id",
        "origin_seq",
        "dest_seq",
        "origin_oid",
        "dest_oid",
        "origin_label",
        "dest_label",
        "platform",
        "route_mode",
        "status",
        "message",
        "reason",
        "used_key",
        "retry_count",
        "raw_response",
    ]
    if failures:
        with arcpy.da.InsertCursor(out_failure, failure_fields) as cursor:
            for record in failures:
                cursor.insertRow([record.get(name) for name in failure_fields])

    return out_table, out_failure


class BaiduOdMatrixTool(object):
    """Module export for .pyt import."""

    @staticmethod
    def run(
        origin_fc,
        dest_fc,
        origin_label_field=None,
        dest_label_field=None,
        input_coord_type="WGS84",
        matrix_mode="driving",
        tactics=None,
        output_wgs84=True,
        speed_level="medium",
        use_in_memory=False,
        out_gdb=None,
        out_table_name=None,
        out_failure_table_name=None,
        max_workers=None,
    ):
        return run_baidu_od_matrix(
            origin_fc=origin_fc,
            dest_fc=dest_fc,
            origin_label_field=origin_label_field,
            dest_label_field=dest_label_field,
            input_coord_type=input_coord_type,
            matrix_mode=matrix_mode,
            tactics=tactics,
            output_wgs84=output_wgs84,
            speed_level=speed_level,
            use_in_memory=use_in_memory,
            out_gdb=out_gdb,
            out_table_name=out_table_name,
            out_failure_table_name=out_failure_table_name,
            max_workers=max_workers,
        )
