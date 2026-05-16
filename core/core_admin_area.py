# -*- coding: utf-8 -*-
"""
Administrative area boundary export module.

Supports Baidu + Amap web service APIs and outputs polygon boundaries.
"""
import json
import re
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import arcpy
import requests

from . import core_common as common


BAIDU_ADMIN_URL = "https://api.map.baidu.com/api_region_search/v1/"
AMAP_ADMIN_URL = "https://restapi.amap.com/v3/config/district"

TEXT_LEN = 32767
TEXT_STORE_LIMIT = 30000
ADMIN_TREE_ROOT_KEYWORD = "\u4e2d\u56fd"
ADMIN_TREE_SUB_ADMIN = 2
ADMIN_TREE_EXTENSIONS_CODE = 1
ADMIN_DIVISION_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config",
    "admin_divisions_level.json",
)
AMAP_STREET_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "config",
    "admin_divisions_amap_street_cache.json",
)
ADMIN_ALL_CITY_VALUE = "__ALL_CITY__"
ADMIN_ALL_CITY_LABEL = "\u6240\u6709\u5e02\u7ea7"
ADMIN_ALL_COUNTY_VALUE = "__ALL_COUNTY__"
ADMIN_ALL_COUNTY_LABEL = "\u6240\u6709\u53bf\u7ea7"
ADMIN_ALL_STREET_VALUE = "__ALL_STREET__"
ADMIN_ALL_STREET_LABEL = "\u6240\u6709\u9547\u8857"
ADMIN_STREET_LEVEL = 4

_ADMIN_TREE_LOCK = threading.Lock()
_ADMIN_TREE_CACHE = {
    "loaded": False,
    "root_records": [],
    "nodes_by_code": {},
    "children_by_code": {},
    "levels": {},
}
_AMAP_STREET_CACHE_LOCK = threading.Lock()
_AMAP_STREET_CACHE = {
    "loaded": False,
    "version": 1,
    "parents": {},
}

_PROVINCE_FALLBACK = [
    ("110000", "\u5317\u4eac\u5e02"),
    ("120000", "\u5929\u6d25\u5e02"),
    ("130000", "\u6cb3\u5317\u7701"),
    ("140000", "\u5c71\u897f\u7701"),
    ("150000", "\u5185\u8499\u53e4\u81ea\u6cbb\u533a"),
    ("210000", "\u8fbd\u5b81\u7701"),
    ("220000", "\u5409\u6797\u7701"),
    ("230000", "\u9ed1\u9f99\u6c5f\u7701"),
    ("310000", "\u4e0a\u6d77\u5e02"),
    ("320000", "\u6c5f\u82cf\u7701"),
    ("330000", "\u6d59\u6c5f\u7701"),
    ("340000", "\u5b89\u5fbd\u7701"),
    ("350000", "\u798f\u5efa\u7701"),
    ("360000", "\u6c5f\u897f\u7701"),
    ("370000", "\u5c71\u4e1c\u7701"),
    ("410000", "\u6cb3\u5357\u7701"),
    ("420000", "\u6e56\u5317\u7701"),
    ("430000", "\u6e56\u5357\u7701"),
    ("440000", "\u5e7f\u4e1c\u7701"),
    ("450000", "\u5e7f\u897f\u58ee\u65cf\u81ea\u6cbb\u533a"),
    ("460000", "\u6d77\u5357\u7701"),
    ("500000", "\u91cd\u5e86\u5e02"),
    ("510000", "\u56db\u5ddd\u7701"),
    ("520000", "\u8d35\u5dde\u7701"),
    ("530000", "\u4e91\u5357\u7701"),
    ("540000", "\u897f\u85cf\u81ea\u6cbb\u533a"),
    ("610000", "\u9655\u897f\u7701"),
    ("620000", "\u7518\u8083\u7701"),
    ("630000", "\u9752\u6d77\u7701"),
    ("640000", "\u5b81\u590f\u56de\u65cf\u81ea\u6cbb\u533a"),
    ("650000", "\u65b0\u7586\u7ef4\u543e\u5c14\u81ea\u6cbb\u533a"),
    ("710000", "\u53f0\u6e7e\u7701"),
    ("810000", "\u9999\u6e2f\u7279\u522b\u884c\u653f\u533a"),
    ("820000", "\u6fb3\u95e8\u7279\u522b\u884c\u653f\u533a"),
]

def _txt(value, default=""):
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return default


def _choice_text(code, name):
    code = _txt(code, "").strip()
    name = _txt(name, "").strip()
    if code and name:
        return "{} ({})".format(name, code)
    return code or name


def _parse_choice_text(value_text):
    text = _txt(value_text, "").strip()
    if not text:
        return "", ""
    if text == ADMIN_ALL_CITY_LABEL:
        return ADMIN_ALL_CITY_VALUE, ADMIN_ALL_CITY_LABEL
    if text == ADMIN_ALL_COUNTY_LABEL:
        return ADMIN_ALL_COUNTY_VALUE, ADMIN_ALL_COUNTY_LABEL
    if text == ADMIN_ALL_STREET_LABEL:
        return ADMIN_ALL_STREET_VALUE, ADMIN_ALL_STREET_LABEL
    if "|" in text:
        left, right = text.split("|", 1)
        left = left.strip()
        right = right.strip()
        if re.fullmatch(r"\d{6}", left):
            return left, right
        if re.fullmatch(r"\d{6}", right):
            return right, left
        return left, right
    match = re.match(r"^(.*)\s*[\(（]\s*(\d{6})\s*[\)）]\s*$", text)
    if match:
        name = match.group(1).strip()
        code = match.group(2).strip()
        return code, name or code
    match = re.match(r"^(\d{6})\s*(.*)$", text)
    if match:
        code = match.group(1).strip()
        name = match.group(2).strip()
        return code, name or code
    return "", text


def _sfloat(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return ""


def _truncate_text(value, limit=TEXT_STORE_LIMIT):
    text = _txt(value, "")
    if not text or len(text) <= limit:
        return text
    suffix = "...[truncated]"
    head = max(limit - len(suffix), 0)
    return text[:head] + suffix


def _normalize_platform(platform):
    text = _txt(platform, "").strip().lower()
    if text in ("baidu", "百度"):
        return "baidu"
    if text in ("amap", "高德"):
        return "amap"
    if text in ("both", "都要"):
        return "both"
    raise ValueError("不支持的平台：{}".format(platform))




def _request_baidu_admin_tree(keyword=ADMIN_TREE_ROOT_KEYWORD, max_retries=None, sub_admin=ADMIN_TREE_SUB_ADMIN):
    if max_retries is None:
        max_retries = len(common.BAIDU_KEYS)

    tried = set()
    last_failure = None

    for retry in range(max_retries):
        key_index, key = common.get_next_baidu_key_info()
        if key is None:
            return {
                "ok": False,
                "status": "KEY_POOL_EMPTY",
                "message": "行政区划接口 Key 池已耗尽",
                "reason": "key_pool_exhausted",
                "key": "N/A",
                "retry_count": retry + 1,
                "raw": None,
                "records": [],
            }
        if key in tried:
            continue
        tried.add(key)

        params = {
            "keyword": keyword,
            "sub_admin": sub_admin,
            "extensions_code": ADMIN_TREE_EXTENSIONS_CODE,
            "output": "json",
            "ak": key,
        }
        try:
            resp = requests.get(BAIDU_ADMIN_URL, params=params, timeout=30)
            http_status = resp.status_code
            result = resp.json() if resp is not None else {}
        except Exception as exc:
            classification = common.classify_baidu_response_issue(message=str(exc), error=exc)
            if key_index is not None and classification.get("switch_key"):
                try:
                    common._set_baidu_key_delay(key_index, classification.get("delay_seconds", 1))
                except Exception:
                    pass
            last_failure = {
                "ok": False,
                "status": "ERROR",
                "message": str(exc),
                "reason": classification.get("kind", "request_error"),
                "key": common.mask_key(key),
                "retry_count": retry + 1,
                "raw": None,
                "records": [],
            }
            if classification.get("switch_key") and retry < max_retries - 1:
                continue
            return last_failure

        status = result.get("status")
        msg = result.get("message") or result.get("msg") or result.get("info") or ""
        if str(status) == "0":
            return {
                "ok": True,
                "status": str(status),
                "message": msg,
                "reason": "",
                "key": common.mask_key(key),
                "retry_count": retry + 1,
                "raw": result,
                "records": result.get("districts") or result.get("data") or result.get("result") or [],
            }

        classification = common.classify_baidu_response_issue(status=status, message=msg, http_status=http_status)
        if key_index is not None and classification.get("switch_key"):
            try:
                common._set_baidu_key_delay(key_index, classification.get("delay_seconds", 1))
            except Exception:
                pass
        last_failure = {
            "ok": False,
            "status": str(status),
            "message": msg,
            "reason": classification.get("kind", "api_error"),
            "key": common.mask_key(key),
            "retry_count": retry + 1,
            "raw": result,
            "records": [],
        }
        if classification.get("switch_key") and retry < max_retries - 1:
            continue
        return last_failure

    return last_failure or {
        "ok": False,
        "status": "RETRY_EXHAUSTED",
        "message": "行政区划查询重试耗尽",
        "reason": "retry_exhausted",
        "key": "N/A",
        "retry_count": max_retries,
        "raw": None,
        "records": [],
    }


def _normalize_admin_tree_node(item, parent_code="", level_hint=None):
    if not isinstance(item, dict):
        return None
    code = _txt(item.get("code") or item.get("adcode") or item.get("id") or item.get("c") or "", "").strip()
    name = _txt(item.get("name") or item.get("district_name") or item.get("n") or "", "").strip()
    if not code and not name:
        return None
    level_raw = item.get("level")
    try:
        level = int(level_raw)
    except Exception:
        level = level_hint
    children_raw = item.get("districts") or item.get("d") or []
    children = []
    if isinstance(children_raw, list):
        for child in children_raw:
            child_node = _normalize_admin_tree_node(
                child,
                parent_code=code,
                level_hint=(level + 1) if isinstance(level, int) else None,
            )
            if child_node:
                children.append(child_node)
    return {
        "code": code,
        "name": name or code,
        "level": level,
        "parent_code": parent_code,
        "children": children,
        "display": _choice_text(code, name or code),
    }


def _load_local_admin_divisions():
    if not os.path.exists(ADMIN_DIVISION_CACHE_FILE):
        return []
    try:
        with open(ADMIN_DIVISION_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("records") or data.get("districts") or []
    if not isinstance(data, list):
        return []
    return data


def _index_admin_tree_node(node):
    if not node:
        return
    code = node.get("code", "")
    parent_code = node.get("parent_code", "")
    level = node.get("level")

    if code:
        _ADMIN_TREE_CACHE["nodes_by_code"][code] = node
    _ADMIN_TREE_CACHE["children_by_code"].setdefault(parent_code, []).append(node)
    if level is not None:
        _ADMIN_TREE_CACHE["levels"].setdefault(level, []).append(node)
    for child in node.get("children") or []:
        _index_admin_tree_node(child)


def _load_admin_tree(force_reload=False):
    with _ADMIN_TREE_LOCK:
        if _ADMIN_TREE_CACHE["loaded"] and not force_reload:
            return _ADMIN_TREE_CACHE

        _ADMIN_TREE_CACHE["loaded"] = False
        _ADMIN_TREE_CACHE["root_records"] = []
        _ADMIN_TREE_CACHE["nodes_by_code"] = {}
        _ADMIN_TREE_CACHE["children_by_code"] = {}
        _ADMIN_TREE_CACHE["levels"] = {}

        root_records = []
        raw_records = _load_local_admin_divisions()
        if raw_records:
            for item in raw_records:
                node = _normalize_admin_tree_node(item, parent_code="", level_hint=1)
                if node:
                    root_records.append(node)

        if not root_records:
            result = _request_baidu_admin_tree()
            if result.get("ok"):
                raw_records = result.get("records") or []
                if isinstance(raw_records, dict):
                    raw_records = raw_records.get("districts") or raw_records.get("data") or []
                if isinstance(raw_records, list):
                    for item in raw_records:
                        node = _normalize_admin_tree_node(item, parent_code="", level_hint=1)
                        if node:
                            root_records.append(node)

        if not root_records:
            for code, name in _PROVINCE_FALLBACK:
                root_records.append({
                    "code": code,
                    "name": name,
                    "level": 1,
                    "parent_code": "",
                    "children": [],
                    "display": _choice_text(code, name),
                })

        for node in root_records:
            _index_admin_tree_node(node)

        _ADMIN_TREE_CACHE["root_records"] = root_records
        _ADMIN_TREE_CACHE["loaded"] = True
        return _ADMIN_TREE_CACHE


def _get_admin_choices(level, parent_code=""):
    tree = _load_admin_tree()
    if level == 1:
        nodes = tree["levels"].get(1) or tree["children_by_code"].get("", []) or tree["root_records"]
    else:
        nodes = tree["children_by_code"].get(parent_code or "", [])
    choices = []
    seen = set()
    for node in nodes:
        if not node:
            continue
        node_level = node.get("level")
        if node_level is not None and level is not None:
            if int(node_level) != int(level):
                continue
        code = _txt(node.get("code"), "").strip()
        name = _txt(node.get("name"), "").strip()
        if not code or not name:
            continue
        display = _choice_text(code, name)
        if display in seen:
            continue
        seen.add(display)
        choices.append(display)
    choices.sort()
    return choices


def _iter_admin_tree_nodes(node):
    if not node:
        return
    yield node
    for child in node.get("children") or []:
        for item in _iter_admin_tree_nodes(child):
            yield item


def _fallback_county_choices_by_city(city_choice):
    city_code, city_name = _parse_choice_text(city_choice)
    keyword = city_name or city_code
    if not keyword:
        return []

    result = _request_baidu_admin_tree(keyword=keyword, sub_admin=1)
    if not result.get("ok"):
        return []

    nodes = []
    for item in _normalize_baidu_records(result.get("records")):
        node = _normalize_admin_tree_node(item, parent_code="")
        if not node:
            continue
        for candidate in _iter_admin_tree_nodes(node):
            if int(candidate.get("level") or 0) == 3:
                nodes.append(candidate)

    choices = []
    seen = set()
    for node in nodes:
        code = _txt(node.get("code"), "").strip()
        name = _txt(node.get("name"), "").strip()
        if not code or not name:
            continue
        display = _choice_text(code, name)
        if display in seen:
            continue
        seen.add(display)
        choices.append(display)
    choices.sort()
    return choices


def _nodes_to_choices(nodes):
    choices = []
    seen = set()
    for node in nodes or []:
        if not node:
            continue
        code = _txt(node.get("code"), "").strip()
        name = _txt(node.get("name"), "").strip()
        if not code or not name:
            continue
        display = _choice_text(code, name)
        if display in seen:
            continue
        seen.add(display)
        choices.append(display)
    choices.sort()
    return choices


def _has_city_level_children(node):
    if not node:
        return False
    for child in node.get("children") or []:
        code = _txt(child.get("code"), "").strip()
        if len(code) == 6 and code.endswith("00"):
            return True
    return False


def _get_county_nodes_for_city_node(city_node):
    if not city_node:
        return []
    code = _txt(city_node.get("code"), "").strip()
    if not code:
        return []
    level = city_node.get("level")
    try:
        level = int(level)
    except Exception:
        level = 0
    if level == 1:
        nodes = _get_admin_child_nodes(code, level=2)
        if nodes:
            return nodes
        return _get_admin_child_nodes(code)
    nodes = _get_admin_child_nodes(code, level=3)
    if nodes:
        return nodes
    return _get_admin_child_nodes(code)


def get_admin_province_choices():
    return _get_admin_choices(1)


def get_admin_city_choices(province_choice):
    province_node = _find_admin_node_by_choice(province_choice)
    if not province_node:
        return []
    if _has_city_level_children(province_node):
        province_code = _txt(province_node.get("code"), "").strip()
        choices = _get_admin_choices(2, province_code)
        return [ADMIN_ALL_CITY_LABEL] + choices if choices else [ADMIN_ALL_CITY_LABEL]
    province_target = _make_admin_target(province_node)
    return [province_target["display"]] if province_target else []


def get_admin_county_choices(city_choice):
    city_node = _find_admin_node_by_choice(city_choice)
    if not city_node:
        return []
    city_code = _txt(city_node.get("code"), "").strip()
    if not city_code:
        return []
    if city_code == ADMIN_ALL_CITY_VALUE:
        return []

    choices = _nodes_to_choices(_get_county_nodes_for_city_node(city_node))
    if choices:
        return [ADMIN_ALL_COUNTY_LABEL] + choices
    fallback = _fallback_county_choices_by_city(city_choice)
    if fallback:
        return [ADMIN_ALL_COUNTY_LABEL] + fallback
    return []


def _find_admin_node_by_choice(choice):
    tree = _load_admin_tree()
    code, name = _parse_choice_text(choice)
    if code in (ADMIN_ALL_CITY_VALUE, ADMIN_ALL_COUNTY_VALUE):
        return None
    if code and code in tree["nodes_by_code"]:
        return tree["nodes_by_code"][code]
    if name:
        matches = [
            node for node in tree["nodes_by_code"].values()
            if _txt(node.get("name"), "") == name
        ]
        if matches:
            return matches[0]
    return None


def _resolve_admin_adcode(value_text):
    """尽量把下拉框文本或普通名称解析为 6 位行政编码。"""
    code, _ = _parse_choice_text(value_text)
    if re.fullmatch(r"\d{6}", code or ""):
        return code

    node = _find_admin_node_by_choice(value_text)
    if node:
        resolved = _txt(node.get("code"), "").strip()
        if re.fullmatch(r"\d{6}", resolved):
            return resolved

    return ""


def _normalize_amap_street_cache_record(record, parent_code, parent_name):
    if not isinstance(record, dict):
        return None

    name = _txt(record.get("name") or record.get("n") or "", "").strip()
    if not name:
        return None

    code = _txt(record.get("code") or record.get("adcode") or record.get("c") or parent_code, "").strip()
    parent_code = _txt(parent_code, "").strip() or code
    parent_name = _txt(parent_name, "").strip()
    display = _txt(record.get("display") or "", "").strip() or _choice_text(parent_code, name)
    choice_key = _txt(record.get("choice_key") or "", "").strip() or "{}|{}".format(parent_code, name)

    return {
        "choice_key": choice_key,
        "display": display,
        "name": name,
        "code": parent_code,
        "level": ADMIN_STREET_LEVEL,
        "parent_code": parent_code,
        "parent_name": parent_name,
        "citycode": _txt(record.get("citycode") or record.get("city_code") or "", "").strip(),
        "center": _txt(record.get("center") or record.get("location") or "", "").strip(),
    }


def _load_amap_street_cache(force_reload=False):
    with _AMAP_STREET_CACHE_LOCK:
        if _AMAP_STREET_CACHE["loaded"] and not force_reload:
            return _AMAP_STREET_CACHE

        cache = {
            "loaded": True,
            "version": 1,
            "parents": {},
        }

        if os.path.exists(AMAP_STREET_CACHE_FILE):
            try:
                with open(AMAP_STREET_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            if isinstance(data, dict):
                parents = data.get("parents") or data.get("data") or {}
                if isinstance(parents, dict):
                    for parent_code, parent_data in parents.items():
                        parent_code = _txt(parent_code, "").strip()
                        if not parent_code:
                            continue
                        parent_name = ""
                        records = []
                        if isinstance(parent_data, dict):
                            parent_name = _txt(parent_data.get("parent_name") or "", "").strip()
                            records = parent_data.get("records") or parent_data.get("children") or parent_data.get("items") or []
                        elif isinstance(parent_data, list):
                            records = parent_data
                        elif parent_data is None:
                            records = []
                        normalized = []
                        for record in records if isinstance(records, list) else []:
                            rec = _normalize_amap_street_cache_record(record, parent_code, parent_name)
                            if rec:
                                normalized.append(rec)
                        cache["parents"][parent_code] = normalized

        _AMAP_STREET_CACHE.update(cache)
        return _AMAP_STREET_CACHE


def _save_amap_street_cache():
    with _AMAP_STREET_CACHE_LOCK:
        cache = _AMAP_STREET_CACHE
        try:
            os.makedirs(os.path.dirname(AMAP_STREET_CACHE_FILE), exist_ok=True)
            data = {
                "version": 1,
                "parents": cache.get("parents") or {},
            }
            with open(AMAP_STREET_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            try:
                arcpy.AddWarning("高德镇街缓存写入失败：{}".format(str(exc)))
            except Exception:
                pass


def _resolve_amap_street_parent_node(province_choice=None, city_choice=None, county_choice=None):
    county_node = _find_admin_node_by_choice(county_choice)
    if county_node and _txt(county_choice, "").strip() not in (ADMIN_ALL_COUNTY_LABEL, ""):
        return county_node

    city_node = _find_admin_node_by_choice(city_choice)
    if not city_node or _txt(city_choice, "").strip() in (ADMIN_ALL_CITY_LABEL, ""):
        return None

    if _get_county_nodes_for_city_node(city_node):
        return None
    return city_node


def _load_amap_street_records(province_choice=None, city_choice=None, county_choice=None):
    parent_node = _resolve_amap_street_parent_node(province_choice, city_choice, county_choice)
    if not parent_node:
        return []

    parent_code = _txt(parent_node.get("code"), "").strip()
    parent_name = _txt(parent_node.get("name"), "").strip()
    if not parent_code:
        return []

    cache = _load_amap_street_cache()
    cached_records = cache.get("parents", {}).get(parent_code)
    if cached_records is not None:
        return cached_records

    result = _request_amap_admin_area(parent_code, None, subdistrict=1, extensions="base")
    records = []
    if result.get("ok"):
        raw_records = _normalize_amap_records(result.get("records"))
        seen = set()
        for item in raw_records:
            if not isinstance(item, dict):
                continue
            children = item.get("districts") or item.get("d") or []
            if not isinstance(children, list):
                children = []
            if not children:
                level = _txt(item.get("level") or "", "").strip().lower()
                if level not in ("street", "township"):
                    continue
                children = [item]
            for child in children:
                rec = _normalize_amap_street_cache_record(child, parent_code, parent_name)
                if not rec:
                    continue
                if rec["choice_key"] in seen:
                    continue
                seen.add(rec["choice_key"])
                records.append(rec)

        cache.setdefault("parents", {})[parent_code] = records
        _save_amap_street_cache()
    return records


def get_admin_street_choices(province_choice=None, city_choice=None, county_choice=None, platform=None):
    platform_code = _normalize_platform(platform or "amap") if platform else "amap"
    if platform_code != "amap":
        return []
    records = _load_amap_street_records(province_choice, city_choice, county_choice)
    choices = [record.get("display") for record in records if record.get("display")]
    if choices:
        return [ADMIN_ALL_STREET_LABEL] + choices
    return []


def _resolve_amap_street_choice(street_choice, province_choice=None, city_choice=None, county_choice=None):
    code, name = _parse_choice_text(street_choice)
    if code in (ADMIN_ALL_CITY_VALUE, ADMIN_ALL_COUNTY_VALUE, ADMIN_ALL_STREET_VALUE) or not name:
        return None

    parent_node = _find_admin_node_by_choice(code)
    if not parent_node:
        parent_node = _resolve_amap_street_parent_node(province_choice, city_choice, county_choice)
    if not parent_node:
        return None

    parent_code = _txt(parent_node.get("code"), "").strip()
    parent_name = _txt(parent_node.get("name"), "").strip()
    if not parent_code:
        return None

    records = _load_amap_street_records(province_choice, city_choice, county_choice)
    match = None
    for record in records:
        if _txt(record.get("name"), "").strip() == name and _txt(record.get("code"), "").strip() == parent_code:
            match = record
            break

    if match is None:
        match = {
            "choice_key": "{}|{}".format(parent_code, name),
            "display": _choice_text(parent_code, name),
            "name": name,
            "code": parent_code,
            "level": ADMIN_STREET_LEVEL,
            "parent_code": parent_code,
            "parent_name": parent_name,
            "citycode": "",
            "center": "",
        }

    return {
        "selected_name": name,
        "selected_level": "street",
        "selected_code": parent_code,
        "boundary_node": parent_node,
        "boundary_name": parent_name,
        "boundary_level": _txt(parent_node.get("level"), ""),
        "boundary_code": parent_code,
        "boundary_source": "street_fallback_county" if int(_txt(parent_node.get("level"), "0") or 0) >= 3 else "street_fallback_city",
        "selected_display": _choice_text(parent_code, name),
        "request_query_text": parent_name or parent_code,
        "request_parent_value": _txt(_find_admin_node_by_choice(parent_code).get("parent_name"), "") if _find_admin_node_by_choice(parent_code) else "",
        "amap_request_query_text": parent_code,
        "amap_request_parent_code": _txt(_find_admin_node_by_choice(parent_code).get("parent_code"), "") if _find_admin_node_by_choice(parent_code) else "",
        "street_record": match,
        "street_count": 1,
        "street_names": name,
    }


def _get_admin_child_nodes(parent_code, level=None):
    tree = _load_admin_tree()
    nodes = tree["children_by_code"].get(parent_code or "", [])
    result = []
    for node in nodes:
        if not node:
            continue
        if level is not None:
            node_level = node.get("level")
            if node_level is None or int(node_level) != int(level):
                continue
        result.append(node)
    return result


def _make_admin_target(node):
    if not node:
        return None
    tree = _load_admin_tree()
    code = _txt(node.get("code"), "").strip()
    name = _txt(node.get("name"), "").strip()
    if not code and not name:
        return None
    level = node.get("level")
    parent_code = _txt(node.get("parent_code"), "")
    parent_name = ""
    if parent_code and parent_code in tree["nodes_by_code"]:
        parent_name = _txt(tree["nodes_by_code"][parent_code].get("name"), "")
    return {
        "code": code,
        "name": name or code,
        "display": _choice_text(code, name or code),
        "level": level,
        "parent_code": parent_code,
        "parent_name": parent_name,
        "query_text": name or code,
        "parent_value": parent_name,
    }


def _finalize_admin_target(target, selected_name=None, selected_level=None, selected_code=None,
                           boundary_name=None, boundary_level=None, boundary_code=None,
                           boundary_source="selected"):
    if not target:
        return None

    selected_name = _txt(selected_name if selected_name is not None else target.get("name"), "").strip()
    selected_level = _txt(selected_level if selected_level is not None else target.get("level"), "").strip()
    selected_code = _txt(selected_code if selected_code is not None else target.get("code"), "").strip()
    boundary_name = _txt(boundary_name if boundary_name is not None else target.get("name"), "").strip()
    boundary_level = _txt(boundary_level if boundary_level is not None else target.get("level"), "").strip()
    boundary_code = _txt(boundary_code if boundary_code is not None else target.get("code"), "").strip()
    boundary_parent_name = _txt(target.get("parent_name"), "").strip()
    boundary_parent_code = _txt(target.get("parent_code"), "").strip()

    target.update({
        "selected_name": selected_name,
        "selected_level": selected_level,
        "selected_code": selected_code,
        "boundary_name": boundary_name,
        "boundary_level": boundary_level,
        "boundary_code": boundary_code,
        "boundary_source": boundary_source,
        "query_text": selected_name,
        "name": boundary_name,
        "level": boundary_level,
        "code": boundary_code,
        "display": _choice_text(selected_code, selected_name),
        "boundary_parent_name": boundary_parent_name,
        "boundary_parent_code": boundary_parent_code,
        "baidu_request_query_text": boundary_name,
        "baidu_request_parent_value": boundary_parent_name,
        "amap_request_query_text": boundary_name or selected_name or target.get("name"),
        "amap_request_parent_code": boundary_parent_code,
    })
    return target


def _normalize_admin_area_task(task):
    if isinstance(task, dict):
        normalized = dict(task)
        normalized["row_idx"] = int(normalized.get("row_idx", 0) or 0)
        normalized["src_oid"] = int(normalized.get("src_oid", normalized["row_idx"]) or 0)
        normalized["platform"] = _txt(normalized.get("platform"), "baidu").strip() or "baidu"
        normalized["query_text"] = _txt(
            normalized.get("query_text")
            or normalized.get("selected_name")
            or normalized.get("boundary_name")
            or normalized.get("request_query_text")
            or "",
            "",
        ).strip()
        normalized["request_query_text"] = _txt(
            normalized.get("request_query_text")
            or normalized.get("baidu_request_query_text")
            or normalized.get("amap_request_query_text")
            or normalized["query_text"],
            "",
        ).strip()
        normalized["request_parent_value"] = _txt(
            normalized.get("request_parent_value")
            or normalized.get("baidu_request_parent_value")
            or normalized.get("amap_request_parent_code")
            or normalized.get("boundary_parent_name")
            or "",
            "",
        ).strip()
        normalized["selected_name"] = _txt(normalized.get("selected_name") or normalized["query_text"], "").strip()
        normalized["selected_level"] = _txt(normalized.get("selected_level"), "").strip()
        normalized["selected_code"] = _txt(normalized.get("selected_code"), "").strip()
        normalized["boundary_name"] = _txt(normalized.get("boundary_name") or normalized["selected_name"], "").strip()
        normalized["boundary_level"] = _txt(normalized.get("boundary_level"), "").strip()
        normalized["boundary_code"] = _txt(normalized.get("boundary_code"), "").strip()
        normalized["boundary_source"] = _txt(normalized.get("boundary_source"), "selected").strip() or "selected"
        normalized["boundary_parent_name"] = _txt(normalized.get("boundary_parent_name"), "").strip()
        normalized["boundary_parent_code"] = _txt(normalized.get("boundary_parent_code"), "").strip()
        normalized["street_count"] = int(normalized.get("street_count", 0) or 0)
        normalized["street_names"] = _txt(normalized.get("street_names"), "").strip()
        normalized["output_wgs84"] = bool(normalized.get("output_wgs84"))
        return normalized

    if isinstance(task, (list, tuple)):
        row_idx = int(task[0]) if len(task) > 0 and task[0] is not None else 0
        src_oid = int(task[1]) if len(task) > 1 and task[1] is not None else row_idx
        query_text = _txt(task[2] if len(task) > 2 else "", "").strip()
        parent_value = _txt(task[3] if len(task) > 3 else "", "").strip()
        platform = _txt(task[4] if len(task) > 4 else "baidu", "baidu").strip() or "baidu"
        output_wgs84 = bool(task[5]) if len(task) > 5 else True
        return {
            "row_idx": row_idx,
            "src_oid": src_oid,
            "platform": platform,
            "query_text": query_text,
            "request_query_text": query_text,
            "request_parent_value": parent_value,
            "selected_name": query_text,
            "selected_level": "",
            "selected_code": "",
            "boundary_name": query_text,
            "boundary_level": "",
            "boundary_code": "",
            "boundary_source": "selected",
            "boundary_parent_name": parent_value,
            "boundary_parent_code": "",
            "street_count": 0,
            "street_names": "",
            "output_wgs84": output_wgs84,
        }

    return {
        "row_idx": 0,
        "src_oid": 0,
        "platform": "baidu",
        "query_text": "",
        "request_query_text": "",
        "request_parent_value": "",
        "selected_name": "",
        "selected_level": "",
        "selected_code": "",
        "boundary_name": "",
        "boundary_level": "",
        "boundary_code": "",
        "boundary_source": "selected",
        "boundary_parent_name": "",
        "boundary_parent_code": "",
        "street_count": 0,
        "street_names": "",
        "output_wgs84": True,
    }


def resolve_admin_targets(province_choice=None, city_choice=None, county_choice=None, street_choice=None):
    tree = _load_admin_tree()
    province_node = _find_admin_node_by_choice(province_choice)
    city_node = _find_admin_node_by_choice(city_choice)
    county_node = _find_admin_node_by_choice(county_choice)

    if not province_node:
        return None

    province_target = _finalize_admin_target(_make_admin_target(province_node))
    if not province_target:
        return None

    city_label = _txt(city_choice, "").strip()
    county_label = _txt(county_choice, "").strip()
    if city_label == ADMIN_ALL_CITY_LABEL:
        targets = []
        for node in _get_admin_child_nodes(province_node.get("code"), level=2):
            target = _finalize_admin_target(_make_admin_target(node))
            if target:
                targets.append(target)
        if not targets:
            return {
                "mode": "single",
                "display": province_target["display"],
                "targets": [province_target],
            }
        return {
            "mode": "all_city",
            "display": "{} / {}".format(province_target["display"], ADMIN_ALL_CITY_LABEL),
            "targets": targets,
        }

    if county_label == ADMIN_ALL_COUNTY_LABEL:
        if not city_node:
            return None
        city_target = _finalize_admin_target(_make_admin_target(city_node))
        if not city_target:
            return None
        targets = []
        for node in _get_county_nodes_for_city_node(city_node):
            target = _finalize_admin_target(_make_admin_target(node))
            if target:
                targets.append(target)
        if not targets:
            return None
        try:
            city_level = int(city_node.get("level") or 0)
        except Exception:
            city_level = 0
        if city_level == 1:
            display = "{} / {}".format(province_target["display"], ADMIN_ALL_COUNTY_LABEL)
        else:
            display = "{} / {} / {}".format(
                province_target["display"],
                city_target["display"],
                ADMIN_ALL_COUNTY_LABEL,
            )
        return {
            "mode": "all_county",
            "display": display,
            "targets": targets,
        }

    street_label = _txt(street_choice, "").strip()
    if street_label:
        if street_label == ADMIN_ALL_STREET_LABEL:
            parent_node = _resolve_amap_street_parent_node(province_choice, city_choice, county_choice)
            street_records = _load_amap_street_records(province_choice, city_choice, county_choice)
            if parent_node and street_records:
                parent_target = _finalize_admin_target(_make_admin_target(parent_node))
                if not parent_target:
                    return None
                try:
                    parent_level = int(parent_node.get("level") or 0)
                except Exception:
                    parent_level = 0
                city_display = province_target["display"]
                if city_node:
                    city_target = _finalize_admin_target(_make_admin_target(city_node))
                    if city_target:
                        city_display = city_target["display"]
                if parent_level >= 3:
                    display = "{} / {} / {} / {}".format(
                        province_target["display"],
                        city_display,
                        parent_target["display"],
                        ADMIN_ALL_STREET_LABEL,
                    )
                else:
                    display = "{} / {}".format(province_target["display"], ADMIN_ALL_STREET_LABEL)

                street_names = []
                seen_names = set()
                for record in street_records:
                    selected_name = _txt(record.get("name"), "").strip()
                    if not selected_name:
                        continue
                    if selected_name in seen_names:
                        continue
                    seen_names.add(selected_name)
                    street_names.append(selected_name)
                if street_names:
                    street_count = len(street_names)
                    target = _finalize_admin_target(
                        _make_admin_target(parent_node),
                        selected_name=ADMIN_ALL_STREET_LABEL,
                        selected_level="street",
                        selected_code=_txt(parent_node.get("code"), "").strip(),
                        boundary_name=_txt(parent_node.get("name"), "").strip(),
                        boundary_level=_txt(parent_node.get("level"), "").strip(),
                        boundary_code=_txt(parent_node.get("code"), "").strip(),
                        boundary_source="street_summary_county" if parent_level >= 3 else "street_summary_city",
                    )
                    if target:
                        target["street_count"] = street_count
                        target["street_names"] = _truncate_text(chr(0x3001).join(street_names), 2000)
                        summary_name = "{}（{}个）".format(ADMIN_ALL_STREET_LABEL, street_count)
                        target["selected_name"] = summary_name
                        target["query_text"] = summary_name
                        return {
                            "mode": "all_street",
                            "display": "{}（{}个）".format(display, street_count),
                            "targets": [target],
                        }
            return None
        street_info = _resolve_amap_street_choice(street_label, province_choice, city_choice, county_choice)
        if not street_info:
            return None
        boundary_node = street_info.get("boundary_node")
        target = _finalize_admin_target(
            _make_admin_target(boundary_node),
            selected_name=street_info.get("selected_name"),
            selected_level=street_info.get("selected_level"),
            selected_code=street_info.get("selected_code"),
            boundary_name=street_info.get("boundary_name"),
            boundary_level=street_info.get("boundary_level"),
            boundary_code=street_info.get("boundary_code"),
            boundary_source=street_info.get("boundary_source", "street_fallback_county"),
        )
        if target:
            return {
                "mode": "single",
                "display": target["display"],
                "targets": [target],
            }
        return None

    if county_node:
        target = _finalize_admin_target(_make_admin_target(county_node))
        if not target:
            return None
        return {
            "mode": "single",
            "display": target["display"],
            "targets": [target],
        }

    if city_node:
        target = _finalize_admin_target(_make_admin_target(city_node))
        if not target:
            return None
        return {
            "mode": "single",
            "display": target["display"],
            "targets": [target],
        }

    return {
        "mode": "single",
        "display": province_target["display"],
        "targets": [province_target],
    }


def resolve_admin_selection(province_choice=None, city_choice=None, county_choice=None, street_choice=None):
    resolved = resolve_admin_targets(province_choice, city_choice, county_choice, street_choice)
    if not resolved:
        return None
    targets = resolved.get("targets") or []
    if not targets:
        return None
    return targets[0]

def _parse_polyline_parts(polyline_text):
    if not polyline_text:
        return []
    parts = []
    for segment in str(polyline_text).split("|"):
        segment = segment.strip()
        if not segment:
            continue
        coords = []
        for pair in segment.split(";"):
            if "," not in pair:
                continue
            try:
                lng_text, lat_text = pair.split(",", 1)
                lng = _sfloat(lng_text)
                lat = _sfloat(lat_text)
                if lng is None or lat is None:
                    continue
                coords.append((lng, lat))
            except Exception:
                continue
        if len(coords) >= 3:
            parts.append(coords)
    return parts


def _parse_baidu_geo_parts(geo_text):
    if not geo_text:
        return []
    text = str(geo_text)
    parts = []
    for segment in text.split("|"):
        if not segment:
            continue
        segment = segment.strip()
        # common prefix like "1-1"
        segment = re.sub(r"^\d+\-\d+;?", "", segment)
        coords = []
        for match in re.finditer(r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", segment):
            lng = _sfloat(match.group(1))
            lat = _sfloat(match.group(2))
            if lng is None or lat is None:
                continue
            coords.append((lng, lat))
        if len(coords) >= 3:
            parts.append(coords)
    return parts


def _extract_boundary_parts(raw):
    if not raw:
        return []
    if isinstance(raw, dict):
        for key in ("geo", "boundary", "polyline", "points"):
            if key in raw:
                return _extract_boundary_parts(raw.get(key))
        return []
    if isinstance(raw, list):
        if not raw:
            return []
        # list of points
        if all(isinstance(item, dict) for item in raw):
            coords = []
            for item in raw:
                lng = _sfloat(item.get("lng") or item.get("x"))
                lat = _sfloat(item.get("lat") or item.get("y"))
                if lng is None or lat is None:
                    continue
                coords.append((lng, lat))
            return [coords] if len(coords) >= 3 else []
        # list of lists
        if all(isinstance(item, (list, tuple)) for item in raw):
            parts = []
            for part in raw:
                coords = []
                for pair in part:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                        lng = _sfloat(pair[0])
                        lat = _sfloat(pair[1])
                        if lng is None or lat is None:
                            continue
                        coords.append((lng, lat))
                if len(coords) >= 3:
                    parts.append(coords)
            return parts
        # list of strings
        if all(isinstance(item, str) for item in raw):
            return _parse_baidu_geo_parts("|".join(raw))
        return []
    if isinstance(raw, str):
        # try amap-like polyline first
        parts = _parse_polyline_parts(raw)
        if parts:
            return parts
        return _parse_baidu_geo_parts(raw)
    return []


def _close_ring(coords):
    if not coords:
        return coords
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def _parts_to_polygon(parts, spatial_reference):
    if not parts:
        return None
    rings = arcpy.Array()
    for part in parts:
        if not part or len(part) < 3:
            continue
        part = _close_ring(list(part))
        array = arcpy.Array([arcpy.Point(lng, lat) for lng, lat in part])
        rings.add(array)
    if rings.count == 0:
        return None
    return arcpy.Polygon(rings, spatial_reference)


def _convert_parts(platform, output_wgs84, parts):
    if not parts:
        return [], "WGS84" if output_wgs84 else "GCJ02"
    converted = []
    if platform == "amap":
        for part in parts:
            coords = []
            for lng, lat in part:
                if output_wgs84:
                    lng2, lat2 = common.gcj02_to_wgs84(lng, lat)
                else:
                    lng2, lat2 = lng, lat
                coords.append((lng2, lat2))
            if len(coords) >= 3:
                converted.append(coords)
        return converted, "WGS84" if output_wgs84 else "GCJ02"

    # Baidu boundary usually BD09
    for part in parts:
        coords = []
        for lng, lat in part:
            if output_wgs84:
                lng2, lat2 = common.bd09_to_wgs84(lng, lat)
            else:
                lng2, lat2 = common.bd09_to_gcj02(lng, lat)
            coords.append((lng2, lat2))
        if len(coords) >= 3:
            converted.append(coords)
    return converted, "WGS84" if output_wgs84 else "GCJ02"


def _request_baidu_admin_area(keyword, parent=None, max_retries=None):
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
            "records": [],
            "raw": None,
        }

    for retry in range(max_retries):
        key_index, key = common.get_next_baidu_key_info()
        if key is None:
            return _make_failure("KEY_POOL_EMPTY", "当前没有可用的百度Key", None, "key_pool_exhausted", retry)
        if key in tried:
            continue
        tried.add(key)

        params = {
            "keyword": keyword,
            "sub_admin": 0,
            "extensions": "all",
            "output": "json",
            "ak": key,
        }
        if parent:
            params["region"] = parent

        try:
            resp = requests.get(BAIDU_ADMIN_URL, params=params, timeout=30)
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
        if str(status) in ("0", "1") or result.get("data") or result.get("districts") or result.get("result"):
            return {
                "ok": True,
                "status": _txt(status, ""),
                "msg": _txt(msg, ""),
                "key": common.mask_key(key),
                "records": result.get("data")
                or result.get("districts")
                or result.get("result")
                or result.get("results")
                or [],
                "raw": result,
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

    return last_failure or _make_failure("RETRY_EXHAUSTED", "百度行政区划重试耗尽", None, "retry_exhausted", max_retries)


def _request_amap_admin_area(keyword, parent=None, max_retries=None, subdistrict=0, extensions="all"):
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
            "records": [],
            "raw": None,
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
            "subdistrict": subdistrict,
            "extensions": extensions,
            "output": "json",
            "key": key,
        }
        parent_code = _resolve_admin_adcode(parent)
        if parent_code:
            params["filter"] = parent_code

        try:
            resp = requests.get(AMAP_ADMIN_URL, params=params, timeout=30)
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
                "records": result.get("districts") or [],
                "raw": result,
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

    return last_failure or _make_failure("RETRY_EXHAUSTED", "高德行政区划重试耗尽", "", None, "retry_exhausted", max_retries)


def _normalize_baidu_records(records):
    if records is None:
        return []
    if isinstance(records, dict):
        records = records.get("districts") or records.get("data") or records.get("result") or []
    if not isinstance(records, list):
        return []
    return records


def _normalize_amap_records(records):
    if records is None:
        return []
    if isinstance(records, dict):
        records = records.get("districts") or []
    if not isinstance(records, list):
        return []
    return records


def _extract_baidu_center(item):
    center = item.get("center") or item.get("location") or item.get("geo_point") or item.get("geo")
    if isinstance(center, dict):
        lng = _sfloat(center.get("lng") or center.get("x"))
        lat = _sfloat(center.get("lat") or center.get("y"))
        return lng, lat
    if isinstance(center, str) and "," in center:
        try:
            lng_text, lat_text = center.split(",", 1)
            return _sfloat(lng_text), _sfloat(lat_text)
        except Exception:
            return None, None
    return None, None


def _extract_amap_center(item):
    center = item.get("center") or item.get("location")
    if isinstance(center, str) and "," in center:
        try:
            lng_text, lat_text = center.split(",", 1)
            return _sfloat(lng_text), _sfloat(lat_text)
        except Exception:
            return None, None
    return None, None


def _extract_boundary_raw(item):
    for key in ("polyline", "boundary", "geo"):
        if key in item:
            return item.get(key)
    return None


def _admin_area_one(args):
    task = _normalize_admin_area_task(args)
    row_idx = task.get("row_idx", 0)
    src_oid = task.get("src_oid", 0)
    query_text = _txt(task.get("query_text"), "").strip()
    request_query_text = _txt(task.get("request_query_text"), "").strip() or query_text
    request_parent_value = _txt(task.get("request_parent_value"), "").strip() or _txt(task.get("boundary_parent_name"), "").strip()
    platform = _txt(task.get("platform"), "baidu").strip()
    output_wgs84 = bool(task.get("output_wgs84"))
    selected_name = _txt(task.get("selected_name"), query_text).strip() or query_text
    selected_level = _txt(task.get("selected_level"), "").strip()
    selected_code = _txt(task.get("selected_code"), "").strip()
    boundary_name = _txt(task.get("boundary_name"), selected_name).strip() or selected_name
    boundary_level = _txt(task.get("boundary_level"), "").strip()
    boundary_code = _txt(task.get("boundary_code"), "").strip()
    boundary_source = _txt(task.get("boundary_source"), "selected").strip() or "selected"
    boundary_parent_name = _txt(task.get("boundary_parent_name"), "").strip()
    boundary_parent_code = _txt(task.get("boundary_parent_code"), "").strip()
    street_count = int(task.get("street_count", 0) or 0)
    street_names = _txt(task.get("street_names"), "").strip()
    street_note = ""
    if street_count > 0 and boundary_source.startswith("street_summary"):
        street_note = "；镇街汇总{}个，街道级无边界，已回退到所属区县边界".format(street_count)

    if not request_query_text:
        platform_label = "百度" if platform == "baidu" else "高德"
        return {
            "row_idx": row_idx,
            "success": False,
            "records": [],
            "error": "查询值为空",
            "error_detail": {
                "src_oid": src_oid,
                "row_idx": row_idx,
                "platform": platform_label,
                "query_text": query_text,
                "status": "INPUT_EMPTY",
                "message": "查询值为空",
                "reason": "查询值为空",
                "used_key": "",
                "retry_count": 0,
                "selected_name": selected_name,
                "selected_level": selected_level,
                "selected_code": selected_code,
                "boundary_name": boundary_name,
                "boundary_level": boundary_level,
                "boundary_code": boundary_code,
                "boundary_source": boundary_source,
                "street_count": street_count,
                "street_names": street_names,
            }
        }

    if platform == "baidu":
        result = _request_baidu_admin_area(request_query_text, request_parent_value)
        if not result.get("ok"):
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": result.get("msg") or "百度行政区划查询失败",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "百度",
                    "query_text": query_text,
                    "status": result.get("status", ""),
                    "message": result.get("msg", ""),
                    "reason": result.get("error_kind", "api_error"),
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                    "selected_name": selected_name,
                    "selected_level": selected_level,
                    "selected_code": selected_code,
                    "boundary_name": boundary_name,
                    "boundary_level": boundary_level,
                    "boundary_code": boundary_code,
                    "boundary_source": boundary_source,
                    "street_count": street_count,
                    "street_names": street_names,
                }
            }

        normalized = []
        has_boundary = False
        for item in _normalize_baidu_records(result.get("records")):
            name = _txt(item.get("name") or item.get("district_name") or item.get("city") or "")
            level = _txt(item.get("level") or item.get("level_name") or "")
            adcode = _txt(item.get("adcode") or item.get("code") or item.get("id") or "")
            citycode = _txt(item.get("city_code") or item.get("citycode") or "")
            parent_name = _txt(item.get("parent_name") or "")
            parent_code = _txt(item.get("parent_code") or "")
            center_lng, center_lat = _extract_baidu_center(item)
            raw_boundary = _extract_boundary_raw(item)
            parts = _extract_boundary_parts(raw_boundary)
            parts, coord_sys = _convert_parts("baidu", output_wgs84, parts)
            if parts:
                has_boundary = True
            normalized.append({
                "src_oid": src_oid,
                "row_idx": row_idx,
                "platform": "百度",
                "query_text": query_text,
                "selected_name": selected_name,
                "selected_level": selected_level,
                "selected_code": selected_code,
                "matched_name": name,
                "level": level,
                "adcode": adcode,
                "citycode": citycode,
                "parent_name": parent_name,
                "parent_code": parent_code,
                "boundary_name": boundary_name or name,
                "boundary_level": boundary_level or level,
                "boundary_code": boundary_code or adcode,
                "boundary_source": boundary_source,
                "street_count": street_count,
                "street_names": street_names,
                "center_lng": center_lng,
                "center_lat": center_lat,
                "boundary_parts": parts,
                "boundary_part_count": len(parts),
                "coord_sys": coord_sys,
                "status": result.get("status", ""),
                "message": _txt(result.get("msg"), "") + street_note,
                "reason": "",
                "used_key": result.get("key", ""),
                "raw_boundary": _truncate_text(_json(raw_boundary)),
                "raw_response": _truncate_text(_json(result.get("raw"))),
            })

        if not has_boundary:
            fallback = _request_amap_admin_area(request_query_text, None)
            if fallback.get("ok"):
                normalized = []
                for item in _normalize_amap_records(fallback.get("records")):
                    name = _txt(item.get("name") or "")
                    level = _txt(item.get("level") or "")
                    adcode = _txt(item.get("adcode") or "")
                    citycode = _txt(item.get("citycode") or "")
                    parent_name = ""
                    parent_code = ""
                    center_lng, center_lat = _extract_amap_center(item)
                    raw_boundary = _extract_boundary_raw(item)
                    parts = _extract_boundary_parts(raw_boundary)
                    parts, coord_sys = _convert_parts("amap", output_wgs84, parts)
                    normalized.append({
                        "src_oid": src_oid,
                        "row_idx": row_idx,
                        "platform": "百度",
                        "query_text": query_text,
                        "selected_name": selected_name,
                        "selected_level": selected_level,
                        "selected_code": selected_code,
                        "matched_name": name,
                        "level": level,
                        "adcode": adcode,
                        "citycode": citycode,
                        "parent_name": parent_name,
                        "parent_code": parent_code,
                        "boundary_name": boundary_name or name,
                        "boundary_level": boundary_level or level,
                        "boundary_code": boundary_code or adcode,
                        "boundary_source": "baidu_no_boundary_fallback_amap",
                        "street_count": street_count,
                        "street_names": street_names,
                        "center_lng": center_lng,
                        "center_lat": center_lat,
                        "boundary_parts": parts,
                        "boundary_part_count": len(parts),
                        "coord_sys": coord_sys,
                        "status": fallback.get("status", ""),
                        "message": _txt(fallback.get("info"), "") + street_note,
                        "reason": "baidu_no_boundary_fallback_amap",
                        "used_key": fallback.get("key", ""),
                        "raw_boundary": _truncate_text(_json(raw_boundary)),
                        "raw_response": _truncate_text(_json(fallback.get("raw"))),
                    })

        if not normalized or not any(record.get("boundary_parts") for record in normalized):
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": "未获取到行政区边界几何",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "百度",
                    "query_text": query_text,
                    "status": result.get("status", ""),
                    "message": result.get("msg", ""),
                    "reason": "boundary_not_found",
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                    "selected_name": selected_name,
                    "selected_level": selected_level,
                    "selected_code": selected_code,
                    "boundary_name": boundary_name,
                    "boundary_level": boundary_level,
                    "boundary_code": boundary_code,
                    "boundary_source": boundary_source,
                    "street_count": street_count,
                    "street_names": street_names,
                }
            }

        return {
            "row_idx": row_idx,
            "success": True,
            "records": normalized,
            "error": None,
        }

    if platform == "amap":
        result = _request_amap_admin_area(request_query_text, request_parent_value)
        if not result.get("ok"):
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": result.get("info") or "高德行政区划查询失败",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "高德",
                    "query_text": query_text,
                    "status": result.get("status", ""),
                    "message": result.get("info", ""),
                    "reason": result.get("error_kind", "api_error"),
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                    "selected_name": selected_name,
                    "selected_level": selected_level,
                    "selected_code": selected_code,
                    "boundary_name": boundary_name,
                    "boundary_level": boundary_level,
                    "boundary_code": boundary_code,
                    "boundary_source": boundary_source,
                    "street_count": street_count,
                    "street_names": street_names,
                }
            }

        normalized = []
        for item in _normalize_amap_records(result.get("records")):
            name = _txt(item.get("name") or "")
            level = _txt(item.get("level") or "")
            adcode = _txt(item.get("adcode") or "")
            citycode = _txt(item.get("citycode") or "")
            parent_name = ""
            parent_code = ""
            center_lng, center_lat = _extract_amap_center(item)
            raw_boundary = _extract_boundary_raw(item)
            parts = _extract_boundary_parts(raw_boundary)
            parts, coord_sys = _convert_parts("amap", output_wgs84, parts)
            normalized.append({
                "src_oid": src_oid,
                "row_idx": row_idx,
                "platform": "高德",
                "query_text": query_text,
                "selected_name": selected_name,
                "selected_level": selected_level,
                "selected_code": selected_code,
                "matched_name": name,
                "level": level,
                "adcode": adcode,
                "citycode": citycode,
                "parent_name": parent_name,
                "parent_code": parent_code,
                "boundary_name": boundary_name or name,
                "boundary_level": boundary_level or level,
                "boundary_code": boundary_code or adcode,
                "boundary_source": boundary_source,
                "street_count": street_count,
                "street_names": street_names,
                "center_lng": center_lng,
                "center_lat": center_lat,
                "boundary_parts": parts,
                "boundary_part_count": len(parts),
                "coord_sys": coord_sys,
                "status": result.get("status", ""),
                "message": _txt(result.get("info"), "") + street_note,
                "reason": "",
                "used_key": result.get("key", ""),
                "raw_boundary": _truncate_text(_json(raw_boundary)),
                "raw_response": _truncate_text(_json(result.get("raw"))),
            })

        if not normalized or not any(record.get("boundary_parts") for record in normalized):
            return {
                "row_idx": row_idx,
                "success": False,
                "records": [],
                "error": "未获取到行政区边界几何",
                "error_detail": {
                    "src_oid": src_oid,
                    "row_idx": row_idx,
                    "platform": "高德",
                    "query_text": query_text,
                    "status": result.get("status", ""),
                    "message": result.get("info", ""),
                    "reason": "boundary_not_found",
                    "used_key": result.get("key", ""),
                    "retry_count": result.get("retry_count", 0),
                    "selected_name": selected_name,
                    "selected_level": selected_level,
                    "selected_code": selected_code,
                    "boundary_name": boundary_name,
                    "boundary_level": boundary_level,
                    "boundary_code": boundary_code,
                    "boundary_source": boundary_source,
                    "street_count": street_count,
                    "street_names": street_names,
                }
            }

        return {
            "row_idx": row_idx,
            "success": True,
            "records": normalized,
            "error": None,
        }

    raise ValueError("不支持的平台：{}".format(platform))


def _create_output_feature_class(out_gdb, out_fc_name):
    sr = arcpy.SpatialReference(4326)
    out_fc = os.path.join(out_gdb, out_fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)
    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, "POLYGON", spatial_reference=sr)

    fields = [
        ("src_oid", "LONG", "源OID"),
        ("row_idx", "LONG", "输入序号"),
        ("query_text", "TEXT", 200, "查询值"),
        ("selected_name", "TEXT", 200, "选中名称"),
        ("selected_level", "TEXT", 20, "选中级别"),
        ("selected_code", "TEXT", 50, "选中编码"),
        ("platform", "TEXT", 10, "平台"),
        ("matched_name", "TEXT", 200, "匹配名称"),
        ("level", "TEXT", 20, "行政级别"),
        ("adcode", "TEXT", 50, "行政编码"),
        ("citycode", "TEXT", 50, "城市编码"),
        ("parent_name", "TEXT", 200, "上级名称"),
        ("parent_code", "TEXT", 50, "上级编码"),
        ("boundary_name", "TEXT", 200, "边界名称"),
        ("boundary_level", "TEXT", 20, "边界级别"),
        ("boundary_code", "TEXT", 50, "边界编码"),
        ("boundary_source", "TEXT", 50, "边界来源"),
        ("street_count", "LONG", "镇街数量"),
        ("street_names", "TEXT", 2000, "镇街名称列表"),
        ("center_lng", "DOUBLE", "中心经度"),
        ("center_lat", "DOUBLE", "中心纬度"),
        ("boundary_part_count", "SHORT", "边界分块数"),
        ("coord_sys", "TEXT", 10, "坐标系"),
        ("status", "TEXT", 50, "状态码"),
        ("message", "TEXT", 500, "返回信息"),
        ("reason", "TEXT", 500, "原因"),
        ("used_key", "TEXT", 32, "使用Key"),
        ("raw_boundary", "TEXT", TEXT_LEN, "原始边界"),
        ("raw_response", "TEXT", TEXT_LEN, "原始响应"),
    ]

    for spec in fields:
        if len(spec) == 4:
            name, field_type, length, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_length=length, field_alias=alias)
        else:
            name, field_type, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_alias=alias)

    return out_fc, sr


def _create_failure_table(out_gdb, out_table_name):
    out_table = arcpy.management.CreateTable(out_gdb, out_table_name)[0]
    arcpy.management.AddField(out_table, "src_oid", "LONG", field_alias="源OID")
    arcpy.management.AddField(out_table, "row_idx", "LONG", field_alias="输入序号")
    arcpy.management.AddField(out_table, "selected_name", "TEXT", field_length=200, field_alias="选中名称")
    arcpy.management.AddField(out_table, "selected_level", "TEXT", field_length=20, field_alias="选中级别")
    arcpy.management.AddField(out_table, "selected_code", "TEXT", field_length=50, field_alias="选中编码")
    arcpy.management.AddField(out_table, "platform", "TEXT", field_length=10, field_alias="平台")
    arcpy.management.AddField(out_table, "query_text", "TEXT", field_length=200, field_alias="查询值")
    arcpy.management.AddField(out_table, "status", "TEXT", field_length=50, field_alias="状态码")
    arcpy.management.AddField(out_table, "message", "TEXT", field_length=500, field_alias="返回信息")
    arcpy.management.AddField(out_table, "reason", "TEXT", field_length=500, field_alias="原因")
    arcpy.management.AddField(out_table, "matched_name", "TEXT", field_length=200, field_alias="匹配名称")
    arcpy.management.AddField(out_table, "adcode", "TEXT", field_length=50, field_alias="行政编码")
    arcpy.management.AddField(out_table, "boundary_name", "TEXT", field_length=200, field_alias="边界名称")
    arcpy.management.AddField(out_table, "boundary_level", "TEXT", field_length=20, field_alias="边界级别")
    arcpy.management.AddField(out_table, "boundary_code", "TEXT", field_length=50, field_alias="边界编码")
    arcpy.management.AddField(out_table, "boundary_source", "TEXT", field_length=50, field_alias="边界来源")
    arcpy.management.AddField(out_table, "street_count", "LONG", field_alias="镇街数量")
    arcpy.management.AddField(out_table, "street_names", "TEXT", field_length=2000, field_alias="镇街名称列表")
    arcpy.management.AddField(out_table, "used_key", "TEXT", field_length=32, field_alias="使用Key")
    arcpy.management.AddField(out_table, "retry_count", "LONG", field_alias="重试次数")
    return out_table




def run_admin_area_boundary_export_selected(province_choice=None, city_choice=None, county_choice=None,
                                           platform="both", speed_level="medium", output_wgs84=True,
                                           out_gdb=None, out_fc_name=None, return_failure_table=True):
    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError("必须指定 out_fc_name")
    if not out_gdb:
        raise ValueError("必须指定 out_gdb")

    resolved = resolve_admin_targets(province_choice, city_choice, county_choice)
    if not resolved:
        raise ValueError("未能解析行政区划选择，请检查省份/城市/区县是否有效")
    targets = resolved.get("targets") or []
    if not targets:
        raise ValueError("未能解析到有效的行政区边界目标")

    platform = _normalize_platform(platform)
    if platform == "both":
        max_workers = common.calculate_max_workers(
            speed_level, len(common.BAIDU_KEYS), len(common.AMAP_KEYS)
        )
    elif platform == "baidu":
        max_workers = common.calculate_max_workers(speed_level, len(common.BAIDU_KEYS), len(common.BAIDU_KEYS))
    else:
        max_workers = common.calculate_max_workers(speed_level, len(common.AMAP_KEYS), len(common.AMAP_KEYS))

    arcpy.AddMessage("行政区边界查询速度档位：{}，最大并发：{}".format(speed_level, max_workers))
    arcpy.AddMessage("当前选择的行政区：{}".format(resolved["display"]))
    if resolved.get("mode") == "all_street" and targets:
        street_count = int(targets[0].get("street_count", 0) or 0)
        if street_count > 0:
            arcpy.AddMessage("镇街汇总：{}个；高德街道级没有边界 polyline，已回退到所属区县边界".format(street_count))

    tasks = []
    for idx, target in enumerate(targets, 1):
        query_text = target.get("query_text") or target.get("name") or target.get("code")
        parent_value = target.get("parent_value") or target.get("parent_name") or ""
        if platform == "both":
            tasks.append((idx, idx, query_text, parent_value, "baidu", output_wgs84))
            tasks.append((idx, idx, query_text, parent_value, "amap", output_wgs84))
        else:
            tasks.append((idx, idx, query_text, parent_value, platform, output_wgs84))

    success_records = []
    failure_records = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_admin_area_one, task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result.get("success"):
                for record in result.get("records", []):
                    success_records.append(record)
            else:
                detail = result.get("error_detail") or {}
                failure_records.append({
                    "src_oid": detail.get("src_oid", 1),
                    "row_idx": detail.get("row_idx", 0),
                    "platform": detail.get("platform", ""),
                    "query_text": detail.get("query_text", resolved["display"]),
                    "status": detail.get("status", ""),
                    "message": detail.get("message", ""),
                    "reason": detail.get("reason", ""),
                    "matched_name": detail.get("matched_name", ""),
                    "adcode": detail.get("adcode", ""),
                    "street_count": detail.get("street_count", 0),
                    "street_names": detail.get("street_names", ""),
                    "used_key": detail.get("used_key", ""),
                    "retry_count": detail.get("retry_count", 0),
                })

    platform_order = {"\u767e\u5ea6": 0, "\u9ad8\u5fb7": 1}
    success_records.sort(
        key=lambda rec: (
            int(rec.get("row_idx", 0) or 0),
            platform_order.get(_txt(rec.get("platform"), ""), 99),
            _txt(rec.get("query_text"), ""),
        )
    )
    failure_records.sort(
        key=lambda rec: (
            int(rec.get("row_idx", 0) or 0),
            platform_order.get(_txt(rec.get("platform"), ""), 99),
            _txt(rec.get("query_text"), ""),
        )
    )

    if not success_records and not failure_records:
        raise ValueError("未获取到任何边界结果")

    out_fc, out_sr = _create_output_feature_class(out_gdb, out_fc_name)
    insert_fields = [
        "SHAPE@", "src_oid", "row_idx", "query_text", "platform",
        "matched_name", "level", "adcode", "citycode", "parent_name", "parent_code",
        "street_count", "street_names",
        "center_lng", "center_lat", "boundary_part_count", "coord_sys",
        "status", "message", "reason", "used_key", "raw_boundary", "raw_response",
    ]

    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        inserted_count = 0
        for record in success_records:
            geom = _parts_to_polygon(record.get("boundary_parts"), out_sr)
            if geom is None:
                continue
            row = [geom] + [record.get(name) for name in insert_fields[1:]]
            cursor.insertRow(row)
            inserted_count += 1

    if success_records and inserted_count == 0:
        raise ValueError("已查询到行政区结果，但未成功写入任何边界几何，请检查返回的 polyline 或坐标转换逻辑")

    out_failure_table = None
    if return_failure_table:
        failure_table_name = "{}_失败".format(out_fc_name)
        out_failure_table = _create_failure_table(out_gdb, failure_table_name)
        failure_fields = [
            "src_oid", "row_idx", "platform", "query_text", "status",
            "message", "reason", "matched_name", "adcode", "street_count", "street_names",
            "used_key", "retry_count",
        ]
        with arcpy.da.InsertCursor(out_failure_table, failure_fields) as cursor:
            for record in failure_records:
                row = [record.get(name) for name in failure_fields]
                cursor.insertRow(row)

    return out_fc, out_failure_table


def run_admin_area_boundary_export_selected_v2(province_choice=None, city_choice=None, county_choice=None,
                                               street_choice=None, platform="both", speed_level="medium",
                                               output_wgs84=True, out_gdb=None, out_fc_name=None,
                                               return_failure_table=True):
    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError("必须指定 out_fc_name")
    if not out_gdb:
        raise ValueError("必须指定 out_gdb")

    resolved = resolve_admin_targets(province_choice, city_choice, county_choice, street_choice)
    if not resolved:
        raise ValueError("未能解析行政区选择，请检查省份/城市/区县/镇街是否有效")
    targets = resolved.get("targets") or []
    if not targets:
        raise ValueError("未能解析到有效的行政区边界目标")

    platform = _normalize_platform(platform)
    if platform not in ("baidu", "amap", "both"):
        platform = "baidu"
    if platform == "baidu":
        street_choice = ""

    if platform == "both":
        max_workers = common.calculate_max_workers(
            speed_level, len(common.BAIDU_KEYS), len(common.AMAP_KEYS)
        )
    elif platform == "baidu":
        max_workers = common.calculate_max_workers(speed_level, len(common.BAIDU_KEYS), len(common.BAIDU_KEYS))
    else:
        max_workers = common.calculate_max_workers(speed_level, len(common.AMAP_KEYS), len(common.AMAP_KEYS))

    arcpy.AddMessage("行政区边界查询速度档位：{}，最大并发：{}".format(speed_level, max_workers))
    arcpy.AddMessage("当前选择的行政区：{}".format(resolved["display"]))

    tasks = []
    for idx, target in enumerate(targets, 1):
        base_task = {
            "row_idx": idx,
            "src_oid": idx,
            "query_text": _txt(
                target.get("selected_name")
                or target.get("query_text")
                or target.get("boundary_name")
                or target.get("name")
                or target.get("code"),
                "",
            ).strip(),
            "selected_name": _txt(target.get("selected_name") or target.get("query_text") or target.get("name"), "").strip(),
            "selected_level": _txt(target.get("selected_level") or target.get("level"), "").strip(),
            "selected_code": _txt(target.get("selected_code") or target.get("code"), "").strip(),
            "boundary_name": _txt(target.get("boundary_name") or target.get("name"), "").strip(),
            "boundary_level": _txt(target.get("boundary_level") or target.get("level"), "").strip(),
            "boundary_code": _txt(target.get("boundary_code") or target.get("code"), "").strip(),
            "boundary_source": _txt(target.get("boundary_source"), "selected").strip() or "selected",
            "street_count": int(target.get("street_count", 0) or 0),
            "street_names": _txt(target.get("street_names"), "").strip(),
            "boundary_parent_name": _txt(target.get("boundary_parent_name") or target.get("parent_name"), "").strip(),
            "boundary_parent_code": _txt(target.get("boundary_parent_code") or target.get("parent_code"), "").strip(),
            "output_wgs84": output_wgs84,
        }

        if platform in ("both", "baidu"):
            task = dict(base_task)
            task["platform"] = "baidu"
            task["request_query_text"] = _txt(
                target.get("baidu_request_query_text") or base_task["boundary_name"] or base_task["query_text"],
                "",
            ).strip()
            task["request_parent_value"] = _txt(
                target.get("baidu_request_parent_value") or base_task["boundary_parent_name"],
                "",
            ).strip()
            tasks.append(task)

        if platform in ("both", "amap"):
            task = dict(base_task)
            task["platform"] = "amap"
            task["request_query_text"] = _txt(
                target.get("amap_request_query_text") or base_task["boundary_name"] or base_task["query_text"],
                "",
            ).strip()
            task["request_parent_value"] = _txt(
                target.get("amap_request_parent_code") or base_task["boundary_parent_code"],
                "",
            ).strip()
            tasks.append(task)

    success_records = []
    failure_records = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_admin_area_one, task): task for task in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result.get("success"):
                success_records.extend(result.get("records", []))
            else:
                detail = result.get("error_detail") or {}
                failure_records.append({
                    "src_oid": detail.get("src_oid", 1),
                    "row_idx": detail.get("row_idx", 0),
                    "selected_name": detail.get("selected_name", ""),
                    "selected_level": detail.get("selected_level", ""),
                    "selected_code": detail.get("selected_code", ""),
                    "platform": detail.get("platform", ""),
                    "query_text": detail.get("query_text", resolved["display"]),
                    "status": detail.get("status", ""),
                    "message": detail.get("message", ""),
                    "reason": detail.get("reason", ""),
                    "matched_name": detail.get("matched_name", ""),
                    "adcode": detail.get("adcode", ""),
                    "boundary_name": detail.get("boundary_name", ""),
                    "boundary_level": detail.get("boundary_level", ""),
                    "boundary_code": detail.get("boundary_code", ""),
                    "boundary_source": detail.get("boundary_source", ""),
                    "street_count": detail.get("street_count", 0),
                    "street_names": detail.get("street_names", ""),
                    "used_key": detail.get("used_key", ""),
                    "retry_count": detail.get("retry_count", 0),
                })

    platform_order = {"百度": 0, "高德": 1}
    success_records.sort(
        key=lambda rec: (
            int(rec.get("row_idx", 0) or 0),
            platform_order.get(_txt(rec.get("platform"), ""), 99),
            _txt(rec.get("query_text"), ""),
        )
    )
    failure_records.sort(
        key=lambda rec: (
            int(rec.get("row_idx", 0) or 0),
            platform_order.get(_txt(rec.get("platform"), ""), 99),
            _txt(rec.get("query_text"), ""),
        )
    )

    if not success_records and not failure_records:
        raise ValueError("未获取到任何边界结果")

    out_fc, out_sr = _create_output_feature_class(out_gdb, out_fc_name)
    insert_fields = [
        "SHAPE@", "src_oid", "row_idx", "query_text", "selected_name", "selected_level", "selected_code",
        "platform", "matched_name", "level", "adcode", "citycode", "parent_name", "parent_code",
        "boundary_name", "boundary_level", "boundary_code", "boundary_source",
        "street_count", "street_names",
        "center_lng", "center_lat", "boundary_part_count", "coord_sys",
        "status", "message", "reason", "used_key", "raw_boundary", "raw_response",
    ]

    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        inserted_count = 0
        for record in success_records:
            geom = _parts_to_polygon(record.get("boundary_parts"), out_sr)
            if geom is None:
                continue
            row = [geom] + [record.get(name) for name in insert_fields[1:]]
            cursor.insertRow(row)
            inserted_count += 1

    if success_records and inserted_count == 0:
        raise ValueError("已有边界结果返回，但没有成功写入任何几何，请检查返回的 polyline 或坐标转换逻辑")

    out_failure_table = None
    if return_failure_table:
        failure_table_name = "{}_失败".format(out_fc_name)
        out_failure_table = _create_failure_table(out_gdb, failure_table_name)
        failure_fields = [
            "src_oid", "row_idx", "selected_name", "selected_level", "selected_code", "platform", "query_text",
            "status", "message", "reason", "matched_name", "adcode", "boundary_name", "boundary_level",
            "boundary_code", "boundary_source", "street_count", "street_names", "used_key", "retry_count",
        ]
        with arcpy.da.InsertCursor(out_failure_table, failure_fields) as cursor:
            for record in failure_records:
                row = [record.get(name) for name in failure_fields]
                cursor.insertRow(row)

    return out_fc, out_failure_table

def run_admin_area_boundary_export(in_table, query_field, parent_field=None,
                                   platform="both", speed_level="medium", output_wgs84=True,
                                   out_gdb=None, out_fc_name=None, return_failure_table=True):
    import os

    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError("必须指定 out_fc_name")
    if not out_gdb:
        raise ValueError("必须指定 out_gdb")
    if not arcpy.Exists(in_table):
        raise ValueError("输入表不存在：{}".format(in_table))

    platform = _normalize_platform(platform)

    if platform == "both":
        max_workers = common.calculate_max_workers(
            speed_level, len(common.BAIDU_KEYS), len(common.AMAP_KEYS)
        )
    elif platform == "baidu":
        max_workers = common.calculate_max_workers(speed_level, len(common.BAIDU_KEYS), len(common.BAIDU_KEYS))
    else:
        max_workers = common.calculate_max_workers(speed_level, len(common.AMAP_KEYS), len(common.AMAP_KEYS))

    arcpy.AddMessage("速度档位：{}，最大并发：{}".format(speed_level, max_workers))

    fields = ["OID@", query_field]
    if parent_field:
        fields.append(parent_field)

    rows = []
    with arcpy.da.SearchCursor(in_table, fields) as cursor:
        for idx, row in enumerate(cursor):
            src_oid = row[0]
            query_text = _txt(row[1], "").strip()
            parent_value = _txt(row[2], "").strip() if parent_field else ""
            rows.append((idx, src_oid, query_text, parent_value))

    if not rows:
        raise ValueError("输入表中没有有效记录")

    tasks = []
    for row_idx, src_oid, query_text, parent_value in rows:
        if platform == "both":
            tasks.append((row_idx, src_oid, query_text, parent_value, "baidu", output_wgs84))
            tasks.append((row_idx, src_oid, query_text, parent_value, "amap", output_wgs84))
        else:
            tasks.append((row_idx, src_oid, query_text, parent_value, platform, output_wgs84))

    success_records = []
    failure_records = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_admin_area_one, task): task for task in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result.get("success"):
                for record in result.get("records", []):
                    if record.get("boundary_parts"):
                        success_records.append(record)
                    else:
                        failure_records.append({
                            "src_oid": record.get("src_oid"),
                            "row_idx": record.get("row_idx"),
                            "platform": record.get("platform"),
                            "query_text": record.get("query_text"),
                            "status": record.get("status"),
                            "message": record.get("message"),
                            "reason": "boundary_not_found",
                            "matched_name": record.get("matched_name"),
                            "adcode": record.get("adcode"),
                            "used_key": record.get("used_key"),
                            "retry_count": 0,
                        })
            else:
                detail = result.get("error_detail")
                if detail:
                    failure_records.append({
                        "src_oid": detail.get("src_oid"),
                        "row_idx": detail.get("row_idx"),
                        "platform": detail.get("platform"),
                        "query_text": detail.get("query_text"),
                        "status": detail.get("status"),
                        "message": detail.get("message"),
                        "reason": detail.get("reason"),
                        "matched_name": "",
                        "adcode": "",
                        "used_key": detail.get("used_key"),
                        "retry_count": detail.get("retry_count", 0),
                    })
                err = result.get("error")
                if err:
                    arcpy.AddWarning("行政区划查询失败：{}".format(err))
            if i % 100 == 0:
                arcpy.AddMessage("已处理 {}/{} 条记录".format(i, len(tasks)))

    if not success_records and not failure_records:
        raise ValueError("没有生成任何行政区划结果")

    out_fc, out_sr = _create_output_feature_class(out_gdb, out_fc_name)
    insert_fields = [
        "SHAPE@", "src_oid", "row_idx", "query_text", "platform",
        "matched_name", "level", "adcode", "citycode", "parent_name", "parent_code",
        "center_lng", "center_lat", "boundary_part_count", "coord_sys",
        "status", "message", "reason", "used_key", "raw_boundary", "raw_response",
    ]

    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        inserted_count = 0
        for record in success_records:
            geom = _parts_to_polygon(record.get("boundary_parts"), out_sr)
            if geom is None:
                continue
            row = [geom] + [record.get(name) for name in insert_fields[1:]]
            cursor.insertRow(row)
            inserted_count += 1

    if success_records and inserted_count == 0:
        raise ValueError("已查询到行政区结果，但未成功写入任何边界几何，请检查返回的 polyline 或坐标转换逻辑")

    out_failure_table = None
    if return_failure_table:
        failure_table_name = "{}_失败".format(out_fc_name)
        out_failure_table = _create_failure_table(out_gdb, failure_table_name)
        failure_fields = [
            "src_oid", "row_idx", "platform", "query_text", "status",
            "message", "reason", "matched_name", "adcode", "used_key", "retry_count",
        ]
        with arcpy.da.InsertCursor(out_failure_table, failure_fields) as cursor:
            for record in failure_records:
                row = [record.get(name) for name in failure_fields]
                cursor.insertRow(row)

    return out_fc, out_failure_table


class AdminAreaBoundaryExport(object):
    """Module export for .pyt import."""

    @staticmethod
    def run(in_table, query_field, parent_field=None,
            platform="百度", speed_level="medium", output_wgs84=True,
            out_gdb=None, out_fc_name=None, return_failure_table=True):
        return run_admin_area_boundary_export(
            in_table=in_table,
            query_field=query_field,
            parent_field=parent_field,
            platform=platform,
            speed_level=speed_level,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            return_failure_table=return_failure_table,
        )
