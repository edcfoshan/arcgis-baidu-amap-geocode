# -*- coding: utf-8 -*-
"""
Input tips (address/POI suggestion) module.

Supports Baidu + Amap web service APIs and outputs a suggestion table.
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import arcpy
import requests

from . import core_common as common


BAIDU_INPUTTIPS_URL = "https://api.map.baidu.com/place/v2/suggestion"
AMAP_INPUTTIPS_URL = "https://restapi.amap.com/v3/assistant/inputtips"

TEXT_LEN = 32767


def _sfloat(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _txt(value, default=""):
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return default


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _normalize_platform(platform):
    text = _txt(platform, "").strip().lower()
    if text in ("baidu", "百度"):
        return "baidu"
    if text in ("amap", "高德"):
        return "amap"
    if text in ("both", "都要"):
        return "both"
    raise ValueError("不支持的平台：{}".format(platform))


def _coerce_region(region_value, default_region):
    region = _txt(region_value, "").strip()
    if not region:
        return _txt(default_region, "").strip()
    return region


def _convert_output_coord(platform, output_wgs84, lng, lat):
    if lng is None or lat is None:
        return None, None, "WGS84" if output_wgs84 else "GCJ02"

    if platform == "amap":
        if output_wgs84:
            lng2, lat2 = common.gcj02_to_wgs84(lng, lat)
            return lng2, lat2, "WGS84"
        return lng, lat, "GCJ02"

    # Baidu suggestion returns BD09 by default; convert to target system.
    if output_wgs84:
        lng2, lat2 = common.bd09_to_wgs84(lng, lat)
        return lng2, lat2, "WGS84"
    lng2, lat2 = common.bd09_to_gcj02(lng, lat)
    return lng2, lat2, "GCJ02"


def _request_baidu_inputtips(keyword, region=None, city_limit=False, max_retries=None):
    if max_retries is None:
        max_retries = len(common.BAIDU_KEYS)

    tried = set()
    last_failure = None

    def _make_failure(status, msg, key, kind, retry_count):
        return {
            "ok": False,
            "status": _txt(status, ""),
            "msg": _txt(msg, ""),
            "key": common.mask_key(key) if key else "N/A",
            "error_kind": kind or "api_error",
            "retry_count": retry_count,
            "suggestions": [],
        }

    for retry in range(max_retries):
        key_index, key = common.get_next_baidu_key_info()
        if key is None:
            return _make_failure("KEY_POOL_EMPTY", "当前没有可用的百度Key", None, "key_pool_exhausted", retry)
        if key in tried:
            continue
        tried.add(key)

        params = {
            "query": keyword,
            "output": "json",
            "ak": key,
            "ret_coordtype": "bd09ll",
        }
        if region:
            params["region"] = region
        if city_limit:
            params["city_limit"] = 1

        try:
            resp = requests.get(BAIDU_INPUTTIPS_URL, params=params, timeout=30)
            http_status = resp.status_code
            result = resp.json() if resp is not None else {}
        except Exception as exc:
            classification = common.classify_baidu_response_issue(message=str(exc), error=exc)
            if key_index is not None and classification.get("switch_key"):
                try:
                    common._set_baidu_key_delay(key_index, classification.get("delay_seconds", 1))
                except Exception:
                    pass
            last_failure = _make_failure("ERROR", str(exc), key, classification.get("kind", "request_error"), retry + 1)
            if classification.get("switch_key") and retry < max_retries - 1:
                continue
            return last_failure

        status = result.get("status")
        msg = result.get("message") or result.get("msg") or result.get("info") or ""
        if str(status) == "0":
            return {
                "ok": True,
                "status": _txt(status, ""),
                "msg": _txt(msg, ""),
                "key": common.mask_key(key),
                "suggestions": result.get("result") or [],
                "retry_count": retry + 1,
            }

        classification = common.classify_baidu_response_issue(status=status, message=msg, http_status=http_status)
        if classification.get("switch_key") and key_index is not None:
            try:
                common._set_baidu_key_delay(key_index, classification.get("delay_seconds", 1))
            except Exception:
                pass
        last_failure = _make_failure(status, msg, key, classification.get("kind", "api_error"), retry + 1)
        if classification.get("switch_key") and retry < max_retries - 1:
            continue
        return last_failure

    return last_failure or _make_failure("RETRY_EXHAUSTED", "百度建议检索重试耗尽", None, "retry_exhausted", max_retries)


def _request_amap_inputtips(keyword, region=None, city_limit=False, max_retries=None):
    if max_retries is None:
        max_retries = len(common.AMAP_KEYS)

    tried = set()
    last_failure = None

    def _make_failure(status, info, infocode, key, kind, retry_count):
        return {
            "ok": False,
            "status": _txt(status, ""),
            "info": _txt(info, ""),
            "infocode": _txt(infocode, ""),
            "key": common.mask_key(key) if key else "N/A",
            "error_kind": kind or "api_error",
            "retry_count": retry_count,
            "suggestions": [],
        }

    for retry in range(max_retries):
        key_index, key = common.get_next_amap_key_info()
        if key is None:
            return _make_failure("KEY_POOL_EMPTY", "当前没有可用的高德Key", "", None, "key_pool_exhausted", retry)
        if key in tried:
            continue
        tried.add(key)

        params = {
            "keywords": keyword,
            "key": key,
            "output": "json",
        }
        if region:
            params["city"] = region
        if city_limit:
            params["citylimit"] = "true"

        try:
            resp = requests.get(AMAP_INPUTTIPS_URL, params=params, timeout=30)
            result = resp.json() if resp is not None else {}
        except Exception as exc:
            classification = common.classify_amap_response_issue(info=str(exc))
            if key_index is not None and classification.get("switch_key"):
                try:
                    common._set_amap_key_delay(key_index, classification.get("delay_seconds", 1))
                except Exception:
                    pass
            last_failure = _make_failure("ERROR", str(exc), "ERROR", key, classification.get("kind", "request_error"), retry + 1)
            if classification.get("switch_key") and retry < max_retries - 1:
                continue
            return last_failure

        status = result.get("status", "")
        info = result.get("info", "")
        infocode = result.get("infocode", "")
        if str(status) == "1" and str(infocode) == "10000":
            return {
                "ok": True,
                "status": _txt(status, ""),
                "info": _txt(info, ""),
                "infocode": _txt(infocode, ""),
                "key": common.mask_key(key),
                "suggestions": result.get("tips") or [],
                "retry_count": retry + 1,
            }

        classification = common.classify_amap_response_issue(status=status, info=info, infocode=infocode)
        if classification.get("switch_key") and key_index is not None:
            try:
                common._set_amap_key_delay(key_index, classification.get("delay_seconds", 1))
            except Exception:
                pass
        last_failure = _make_failure(status, info, infocode, key, classification.get("kind", "api_error"), retry + 1)
        if classification.get("switch_key") and retry < max_retries - 1:
            continue
        return last_failure

    return last_failure or _make_failure("RETRY_EXHAUSTED", "高德输入提示重试耗尽", "", None, "retry_exhausted", max_retries)


def _build_tip_row(row_idx, src_oid, keyword, region_value, platform, tip_index, tip, output_wgs84):
    name = _txt(tip.get("name"), "")
    address = _txt(tip.get("address"), "")
    district = _txt(tip.get("district"), "")
    business = _txt(tip.get("business"), "")
    city = _txt(tip.get("city"), "")
    uid = _txt(tip.get("uid") or tip.get("id"), "")
    adcode = _txt(tip.get("adcode"), "")
    citycode = _txt(tip.get("citycode"), "")

    lng = lat = None
    location = tip.get("location")
    if isinstance(location, dict):
        lng = _sfloat(location.get("lng") or location.get("x"))
        lat = _sfloat(location.get("lat") or location.get("y"))
    elif isinstance(location, str) and "," in location:
        try:
            lng_text, lat_text = location.split(",", 1)
            lng, lat = _sfloat(lng_text), _sfloat(lat_text)
        except Exception:
            lng = lat = None

    lng, lat, coord_sys = _convert_output_coord(platform, output_wgs84, lng, lat)

    return {
        "src_oid": src_oid,
        "row_idx": row_idx,
        "platform": "百度" if platform == "baidu" else "高德",
        "keyword": keyword,
        "region_value": region_value,
        "suggest_rank": tip_index,
        "suggest_name": name,
        "suggest_city": city,
        "suggest_district": district,
        "suggest_business": business,
        "suggest_citycode": citycode,
        "suggest_adcode": adcode,
        "suggest_uid": uid,
        "suggest_address": address,
        "suggest_lng": lng,
        "suggest_lat": lat,
        "coord_sys": coord_sys,
        "raw_response": _json(tip),
    }


def _inputtips_one(args):
    row_idx, src_oid, keyword, region_value, platform, max_tip_count, output_wgs84 = args
    if not keyword:
        platform_label = "百度" if platform == "baidu" else "高德"
        return {
            "row_idx": row_idx,
            "success": False,
            "records": [],
            "error": "关键词为空",
            "error_detail": {
                "src_oid": src_oid,
                "row_idx": row_idx,
                "platform": platform_label,
                "keyword": keyword,
                "region_value": region_value,
                "status": "INPUT_EMPTY",
                "message": "关键词为空",
                "reason": "关键词为空",
                "suggest_count": 0,
                "used_key": "",
                "retry_count": 0,
            }
        }

    if platform == "baidu":
        result = _request_baidu_inputtips(keyword, region_value)
        ok = result.get("ok")
        suggestions = result.get("suggestions") or []
        if not ok:
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": result.get("msg") or "百度建议检索失败",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "百度",
                    "keyword": keyword,
                    "region_value": region_value,
                    "status": result.get("status", ""),
                    "message": result.get("msg", ""),
                    "reason": result.get("error_kind", "api_error"),
                    "suggest_count": 0,
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                }
            }
        records = []
        for idx, tip in enumerate(suggestions[:max_tip_count], 1):
            record = _build_tip_row(row_idx, src_oid, keyword, region_value, platform, idx, tip, output_wgs84)
            record.update({
                "status": result.get("status", ""),
                "message": result.get("msg", ""),
                "reason": "",
                "used_key": result.get("key", ""),
            })
            records.append(record)
        if not records:
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": "百度无建议结果",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "百度",
                    "keyword": keyword,
                    "region_value": region_value,
                    "status": result.get("status", ""),
                    "message": result.get("msg", ""),
                    "reason": "empty_result",
                    "suggest_count": 0,
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                }
            }
        return {
            "row_idx": row_idx,
            "success": True,
            "records": records,
            "error": None,
            "meta": result,
        }

    if platform == "amap":
        result = _request_amap_inputtips(keyword, region_value)
        ok = result.get("ok")
        suggestions = result.get("suggestions") or []
        if not ok:
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": result.get("info") or "高德输入提示失败",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "高德",
                    "keyword": keyword,
                    "region_value": region_value,
                    "status": result.get("status", ""),
                    "message": result.get("info", ""),
                    "reason": result.get("error_kind", "api_error"),
                    "suggest_count": 0,
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                }
            }
        records = []
        for idx, tip in enumerate(suggestions[:max_tip_count], 1):
            record = _build_tip_row(row_idx, src_oid, keyword, region_value, platform, idx, tip, output_wgs84)
            record.update({
                "status": result.get("status", ""),
                "message": result.get("info", ""),
                "reason": "",
                "used_key": result.get("key", ""),
            })
            records.append(record)
        if not records:
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": "高德无建议结果",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "高德",
                    "keyword": keyword,
                    "region_value": region_value,
                    "status": result.get("status", ""),
                    "message": result.get("info", ""),
                    "reason": "empty_result",
                    "suggest_count": 0,
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                }
            }
        return {
            "row_idx": row_idx,
            "success": True,
            "records": records,
            "error": None,
            "meta": result,
        }

    raise ValueError("不支持的平台：{}".format(platform))


def _create_output_table(out_gdb, out_table_name):
    out_table = arcpy.management.CreateTable(out_gdb, out_table_name)[0]
    arcpy.management.AddField(out_table, "src_oid", "LONG", field_alias="源OID")
    arcpy.management.AddField(out_table, "row_idx", "LONG", field_alias="输入序号")
    arcpy.management.AddField(out_table, "platform", "TEXT", field_length=10, field_alias="平台")
    arcpy.management.AddField(out_table, "keyword", "TEXT", field_length=500, field_alias="关键词")
    arcpy.management.AddField(out_table, "region_value", "TEXT", field_length=200, field_alias="区域")
    arcpy.management.AddField(out_table, "suggest_rank", "LONG", field_alias="建议序号")
    arcpy.management.AddField(out_table, "suggest_name", "TEXT", field_length=200, field_alias="建议名称")
    arcpy.management.AddField(out_table, "suggest_city", "TEXT", field_length=100, field_alias="城市")
    arcpy.management.AddField(out_table, "suggest_district", "TEXT", field_length=100, field_alias="区县")
    arcpy.management.AddField(out_table, "suggest_business", "TEXT", field_length=200, field_alias="商圈")
    arcpy.management.AddField(out_table, "suggest_citycode", "TEXT", field_length=50, field_alias="城市编码")
    arcpy.management.AddField(out_table, "suggest_adcode", "TEXT", field_length=50, field_alias="区域编码")
    arcpy.management.AddField(out_table, "suggest_uid", "TEXT", field_length=100, field_alias="建议ID")
    arcpy.management.AddField(out_table, "suggest_address", "TEXT", field_length=500, field_alias="地址")
    arcpy.management.AddField(out_table, "suggest_lng", "DOUBLE", field_alias="经度")
    arcpy.management.AddField(out_table, "suggest_lat", "DOUBLE", field_alias="纬度")
    arcpy.management.AddField(out_table, "coord_sys", "TEXT", field_length=10, field_alias="坐标系")
    arcpy.management.AddField(out_table, "status", "TEXT", field_length=50, field_alias="状态码")
    arcpy.management.AddField(out_table, "message", "TEXT", field_length=500, field_alias="返回信息")
    arcpy.management.AddField(out_table, "reason", "TEXT", field_length=500, field_alias="原因")
    arcpy.management.AddField(out_table, "used_key", "TEXT", field_length=32, field_alias="使用Key")
    arcpy.management.AddField(out_table, "raw_response", "TEXT", field_length=TEXT_LEN, field_alias="原始响应")
    return out_table


def _create_failure_table(out_gdb, out_table_name):
    out_table = arcpy.management.CreateTable(out_gdb, out_table_name)[0]
    arcpy.management.AddField(out_table, "src_oid", "LONG", field_alias="源OID")
    arcpy.management.AddField(out_table, "row_idx", "LONG", field_alias="输入序号")
    arcpy.management.AddField(out_table, "platform", "TEXT", field_length=10, field_alias="平台")
    arcpy.management.AddField(out_table, "keyword", "TEXT", field_length=500, field_alias="关键词")
    arcpy.management.AddField(out_table, "region_value", "TEXT", field_length=200, field_alias="区域")
    arcpy.management.AddField(out_table, "status", "TEXT", field_length=50, field_alias="状态码")
    arcpy.management.AddField(out_table, "message", "TEXT", field_length=500, field_alias="返回信息")
    arcpy.management.AddField(out_table, "reason", "TEXT", field_length=500, field_alias="原因")
    arcpy.management.AddField(out_table, "suggest_count", "LONG", field_alias="建议数量")
    arcpy.management.AddField(out_table, "used_key", "TEXT", field_length=32, field_alias="使用Key")
    arcpy.management.AddField(out_table, "retry_count", "LONG", field_alias="重试次数")
    return out_table


def run_inputtips_to_table(in_table, keyword_field, region_field=None, default_region=None,
                           platform="both", max_tip_count=10, speed_level="medium",
                           output_wgs84=True, out_gdb=None, out_table_name=None,
                           return_failure_table=True):
    arcpy.env.overwriteOutput = True

    if not out_table_name:
        raise ValueError("必须指定 out_table_name")
    if not out_gdb:
        raise ValueError("必须指定 out_gdb")
    if not arcpy.Exists(in_table):
        raise ValueError("输入表不存在：{}".format(in_table))

    platform = _normalize_platform(platform)

    try:
        max_tip_count = int(max_tip_count or 10)
    except Exception:
        max_tip_count = 10
    max_tip_count = max(1, min(max_tip_count, 20))

    if platform == "both":
        max_workers = common.calculate_max_workers(
            speed_level, len(common.BAIDU_KEYS), len(common.AMAP_KEYS)
        )
    elif platform == "baidu":
        max_workers = common.calculate_max_workers(speed_level, len(common.BAIDU_KEYS), len(common.BAIDU_KEYS))
    else:
        max_workers = common.calculate_max_workers(speed_level, len(common.AMAP_KEYS), len(common.AMAP_KEYS))

    arcpy.AddMessage("速度档位：{}，最大并发：{}".format(speed_level, max_workers))

    fields = ["OID@", keyword_field]
    if region_field:
        fields.append(region_field)

    rows = []
    with arcpy.da.SearchCursor(in_table, fields) as cursor:
        for idx, row in enumerate(cursor):
            src_oid = row[0]
            keyword = _txt(row[1], "").strip()
            region_value = _coerce_region(row[2], default_region) if region_field else _coerce_region(None, default_region)
            rows.append((idx, src_oid, keyword, region_value))

    if not rows:
        raise ValueError("输入表中没有有效记录")

    tasks = []
    for row_idx, src_oid, keyword, region_value in rows:
        if platform == "both":
            tasks.append((row_idx, src_oid, keyword, region_value, "baidu", max_tip_count, output_wgs84))
            tasks.append((row_idx, src_oid, keyword, region_value, "amap", max_tip_count, output_wgs84))
        else:
            tasks.append((row_idx, src_oid, keyword, region_value, platform, max_tip_count, output_wgs84))

    success_records = []
    failure_records = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_inputtips_one, task): task for task in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result.get("success"):
                success_records.extend(result.get("records", []))
            else:
                detail = result.get("error_detail")
                if detail:
                    failure_records.append(detail)
                err = result.get("error")
                if err:
                    arcpy.AddWarning("输入提示失败：{}".format(err))
            if i % 100 == 0:
                arcpy.AddMessage("已处理 {}/{} 条记录".format(i, len(tasks)))

    if not success_records and not failure_records:
        raise ValueError("没有生成任何输入提示结果")

    out_table = _create_output_table(out_gdb, out_table_name)
    insert_fields = [
        "src_oid", "row_idx", "platform", "keyword", "region_value", "suggest_rank",
        "suggest_name", "suggest_city", "suggest_district", "suggest_business",
        "suggest_citycode", "suggest_adcode", "suggest_uid", "suggest_address",
        "suggest_lng", "suggest_lat", "coord_sys", "status", "message", "reason",
        "used_key", "raw_response",
    ]
    with arcpy.da.InsertCursor(out_table, insert_fields) as cursor:
        for record in success_records:
            row = [record.get(name) for name in insert_fields]
            cursor.insertRow(row)

    out_failure_table = None
    if return_failure_table:
        failure_table_name = "{}_失败".format(out_table_name)
        out_failure_table = _create_failure_table(out_gdb, failure_table_name)
        failure_fields = [
            "src_oid", "row_idx", "platform", "keyword", "region_value", "status",
            "message", "reason", "suggest_count", "used_key", "retry_count",
        ]
        with arcpy.da.InsertCursor(out_failure_table, failure_fields) as cursor:
            for record in failure_records:
                row = [record.get(name) for name in failure_fields]
                cursor.insertRow(row)

    return out_table, out_failure_table


class InputTipsToTable(object):
    """Module export for .pyt import."""

    @staticmethod
    def run(in_table, keyword_field, region_field=None, default_region=None,
            platform="百度", max_tip_count=10, speed_level="medium",
            output_wgs84=True, out_gdb=None, out_table_name=None,
            return_failure_table=True):
        return run_inputtips_to_table(
            in_table=in_table,
            keyword_field=keyword_field,
            region_field=region_field,
            default_region=default_region,
            platform=platform,
            max_tip_count=max_tip_count,
            speed_level=speed_level,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_table_name=out_table_name,
            return_failure_table=return_failure_table,
        )
