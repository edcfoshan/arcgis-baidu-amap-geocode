# -*- coding: utf-8 -*-
"""
POI 搜索功能模块 - 提供范围内 POI 搜索功能

支持高德和百度双平台 POI 搜索
"""
import hashlib
import json
import re
import time
import requests
import arcpy
import os

from . import core_common as common


def _coerce_text(value):
    """将 API 返回值统一成可写入字段的文本。"""
    if value is None:
        return ''
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if item not in (None, '')]
        return '; '.join(parts)
    return str(value).strip()


def _parse_location(location):
    """将经纬度字段统一解析成 (lng, lat)。"""
    if not location:
        return None, None

    if isinstance(location, dict):
        try:
            if 'lng' in location and 'lat' in location:
                return float(location['lng']), float(location['lat'])
            if 'x' in location and 'y' in location:
                return float(location['x']), float(location['y'])
        except Exception:
            return None, None

    if isinstance(location, (tuple, list)) and len(location) >= 2:
        try:
            return float(location[0]), float(location[1])
        except Exception:
            return None, None

    if isinstance(location, str) and ',' in location:
        try:
            lng_text, lat_text = location.split(',', 1)
            return float(lng_text), float(lat_text)
        except Exception:
            return None, None

    return None, None


def _round_coord(value, digits=6):
    try:
        return round(float(value), digits)
    except Exception:
        return None


def _poi_dedupe_key(poi):
    """
    为单条 POI 生成稳定的去重键。

    优先使用 poi_id，其次使用名称、地址、类型和坐标的组合。
    """
    poi_id = _coerce_text(poi.get('poi_id'))
    if poi_id:
        return ('poi_id', poi_id)

    return (
        'fallback',
        _coerce_text(poi.get('source')).lower(),
        _coerce_text(poi.get('name')).lower(),
        _coerce_text(poi.get('address')).lower(),
        _coerce_text(poi.get('typecode')).lower(),
        _round_coord(poi.get('longitude')),
        _round_coord(poi.get('latitude'))
    )


def _dedupe_poi_list(poi_list):
    """去掉重复或无坐标的 POI。"""
    deduped = []
    seen = set()

    for poi in poi_list:
        if poi.get('longitude') is None or poi.get('latitude') is None:
            continue

        key = _poi_dedupe_key(poi)
        if key in seen:
            continue

        seen.add(key)
        deduped.append(poi)

    return deduped


def _convert_output_coord(output_wgs84, platform, lng_a, lat_a):
    """
    根据输出坐标系，返回最终用于输出的坐标。

    参数：
    - output_wgs84: True 输出 WGS84；False 输出 GCJ02
    - platform: 'amap' 或 'baidu'
    - lng_a/lat_a: Amap 的 GCJ02 坐标，或 Baidu 地点检索 3.0 返回的 GCJ02 坐标
    """
    target_system = 'WGS84' if output_wgs84 else 'GCJ02'
    if platform == 'amap':
        return common.convert_coord(lng_a, lat_a, 'GCJ02', target_system)
    if platform == 'baidu':
        return common.convert_coord(lng_a, lat_a, 'GCJ02', target_system)
    return lng_a, lat_a


def _baidu_issue_label(kind):
    """把百度响应问题类型转换成更直观的中文日志标签。"""
    mapping = {
        'concurrency': '并发限制',
        'quota': '配额/限流',
        'auth': '授权失败',
        'service': '服务异常',
        'request_error': '请求参数错误',
        'not_found': '未找到结果',
        'api_error': '接口异常',
        'key_pool_exhausted': 'Key池耗尽',
        'retry_exhausted': '重试耗尽',
        'ok': '正常',
        'unknown': '未知问题',
    }
    kind_text = _coerce_text(kind)
    return mapping.get(kind_text, kind_text or '未知问题')


def _flatten_amap_poi_extra(poi):
    """
    将高德 POI 的额外返回字段尽量摊平成字段字典。

    现有基础字段（name/address/type/code/坐标等）由主流程单独输出；
    这里仅保留其余返回信息，避免丢失。
    """
    base_keys = {
        'show_fields'
    }

    def _stringify(value):
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, list):
            if not value:
                return ''
            if all(not isinstance(item, (dict, list)) for item in value):
                return '; '.join(_coerce_text(item) for item in value if item not in (None, ''))
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return _coerce_text(value)

    def _flatten(value, prefix, out):
        if value is None:
            return
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_key == 'show_fields':
                    continue
                next_prefix = '{}_{}'.format(prefix, sub_key) if prefix else sub_key
                _flatten(sub_value, next_prefix, out)
            return

        out[prefix] = _stringify(value)

    extra = {}
    for key, value in (poi or {}).items():
        if key in base_keys:
            continue
        _flatten(value, 'amap_{}'.format(key), extra)

    return extra


AMAP_EXTRA_TEXT_FIELD_LENGTH = 4000

AMAP_POI_REQUEST_TIMEOUT = (5, 30)
AMAP_POI_REQUEST_RETRY_COUNT = 3
AMAP_POI_REQUEST_RETRY_DELAY = 1.5

AMAP_ALIAS_PART_MAP = {
    'name': 'POI名称',
    'address': '地址',
    'tel': '电话',
    'type': '类型',
    'typecode': '类型编码',
    'location': '坐标',
    'pcode': '省份编码',
    'citycode': '城市编码',
    'cityname': '城市名称',
    'adcode': '区域编码',
    'adname': '区域名称',
    'pname': '省份名称',
    'id': 'POI ID',
    'alias': '别名',
    'tag': '标签',
    'keytag': '关键词标签',
    'business_area': '商圈',
    'distance': '距离',
    'building': '建筑',
    'children': '子POI',
    'childtype': '子类型',
    'photos': '照片',
    'website': '官网',
    'email': '邮箱',
    'parking_type': '停车类型',
    'recommend': '推荐',
    'shopid': '商铺ID',
    'shopinfo': '商铺信息',
    'importance': '重要性',
    'postcode': '邮政编码',
    'poiweight': 'POI权重',
    'timestamp': '时间戳',
    'match': '匹配',
    'event': '事件',
    'gridcode': '网格编码',
    'discount_num': '优惠数量',
    'favorite_num': '收藏数量',
    'featured_reviews': '精选评论',
    'featured_reviews_remake': '精选评论改写',
    'navi_poiid': '导航POI ID',
    'space_num': '场所数量',
    'biz_ext': '商户扩展',
    'cost': '人均消费',
    'opentime2': '营业时间2',
    'open_time': '营业时间',
    'rating': '评分',
    'indoor_data': '室内信息',
    'indoor_map': '室内地图',
    'cmsid': 'CMS标识',
    'truefloor': '实际楼层',
    'floor': '楼层',
    'cpid': '室内POI编号',
}


def _amap_extra_alias(raw_key, ordinal):
    """把高德返回字段名尽量转成中文别名。"""
    raw_name = _coerce_text(raw_key)
    if not raw_name:
        return '高德返回字段{}'.format(ordinal)

    path = raw_name[5:] if raw_name.startswith('amap_') else raw_name
    if not path:
        return '高德返回字段{}'.format(ordinal)

    tokens = path.split('_')
    translated = []
    index = 0
    while index < len(tokens):
        matched_text = None
        matched_len = 0
        for end in range(len(tokens), index, -1):
            candidate = '_'.join(tokens[index:end])
            if candidate in AMAP_ALIAS_PART_MAP:
                matched_text = AMAP_ALIAS_PART_MAP[candidate]
                matched_len = end - index
                break
        if matched_text is None:
            matched_text = AMAP_ALIAS_PART_MAP.get(tokens[index], '字段')
            matched_len = 1
        translated.append(matched_text)
        index += matched_len

    if not translated or all(part == '字段' for part in translated):
        return '高德返回字段{}'.format(ordinal)

    alias = '_'.join(part for part in translated if part)
    if not alias.startswith('高德'):
        alias = '高德' + alias
    return alias


def _request_json_with_retry(url, params, timeout, label='请求', retry_count=3, retry_delay=1.5):
    """请求 JSON 接口，失败后自动重试。"""
    last_exc = None
    for attempt in range(1, retry_count + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retry_count:
                arcpy.AddWarning('{}失败，准备重试（{}/{}）：{}'.format(label, attempt, retry_count, str(exc)))
                time.sleep(retry_delay * attempt)
                continue
            raise

    if last_exc:
        raise last_exc


def _make_valid_field_name(raw_name, existing_names, max_length=64):
    """把任意字段名整理成 ArcGIS 可用的要素字段名。"""
    field_name = re.sub(r'[^0-9A-Za-z_]', '_', raw_name or '')
    field_name = re.sub(r'_+', '_', field_name).strip('_')

    if not field_name:
        field_name = 'f'
    if field_name[0].isdigit():
        field_name = 'f_' + field_name

    if len(field_name) > max_length:
        digest = hashlib.md5((raw_name or '').encode('utf-8')).hexdigest()[:6]
        keep_len = max(1, max_length - 7)
        field_name = '{}_{}'.format(field_name[:keep_len], digest)

    candidate = field_name
    index = 1
    while candidate in existing_names:
        suffix = '_{}'.format(index)
        base_len = max(1, max_length - len(suffix))
        candidate = '{}{}'.format(field_name[:base_len], suffix)
        index += 1

    existing_names.add(candidate)
    return candidate


def _collect_amap_extra_field_specs(poi_list):
    """从 Amap 返回结果中收集需要动态建字段的字段定义。"""
    specs = []
    seen_raw_keys = set()
    existing_names = set()

    for poi in poi_list or []:
        extra = poi.get('amap_extra') or {}
        if not isinstance(extra, dict):
            continue

        for raw_key in extra.keys():
            if raw_key == 'show_fields' or raw_key in seen_raw_keys:
                continue
            seen_raw_keys.add(raw_key)
            field_name = _make_valid_field_name(raw_key, existing_names)
            field_alias = _amap_extra_alias(raw_key, len(specs) + 1)
            specs.append({
                'field_name': field_name,
                'field_alias': field_alias,
                'source_key': raw_key,
            })

    return specs


def _extent_looks_geographic(extent):
    """判断范围值是否已经像经纬度坐标。"""
    try:
        return (
            -180.0 <= float(extent.XMin) <= 180.0 and
            -180.0 <= float(extent.XMax) <= 180.0 and
            -90.0 <= float(extent.YMin) <= 90.0 and
            -90.0 <= float(extent.YMax) <= 90.0
        )
    except Exception:
        return False


def _extent_to_wgs84(extent):
    """将任意输入范围尽量统一成 WGS84。"""
    if extent is None:
        return extent

    if _extent_looks_geographic(extent):
        return extent

    source_sr = getattr(extent, 'spatialReference', None)
    if not source_sr:
        return extent

    try:
        if getattr(source_sr, 'factoryCode', None) == 4326:
            return extent

        target_sr = arcpy.SpatialReference(4326)
        corners = [
            (extent.XMin, extent.YMin),
            (extent.XMax, extent.YMin),
            (extent.XMax, extent.YMax),
            (extent.XMin, extent.YMax),
        ]
        lon_list = []
        lat_list = []
        for x, y in corners:
            pt_geom = arcpy.PointGeometry(arcpy.Point(x, y), source_sr)
            pt_wgs = pt_geom.projectAs(target_sr)
            lon_list.append(pt_wgs.firstPoint.X)
            lat_list.append(pt_wgs.firstPoint.Y)

        return arcpy.Extent(min(lon_list), min(lat_list), max(lon_list), max(lat_list))
    except Exception as e:
        arcpy.AddWarning('范围坐标系转换为 WGS84 失败，继续按原始范围处理：{}'.format(str(e)))
        return extent


def extent_to_polygon_coords(extent, coord_system='gcj02'):
    """
    将 ArcGIS Extent 对象转换为高德 polygon 坐标字符串

    参数：
    - extent: arcpy.Extent 对象
    - coord_system: 'gcj02' 或 'wgs84'，高德需要 gcj02

    返回：
    - polygon_coords: "经度,纬度|经度,纬度|..." 格式
    """
    extent = _extent_to_wgs84(extent)

    # 获取范围四个角点（顺时针）
    if coord_system == 'gcj02':
        # WGS84 -> GCJ02
        lng1, lat1 = common.wgs84_to_gcj02(extent.XMin, extent.YMin)  # 左下
        lng2, lat2 = common.wgs84_to_gcj02(extent.XMax, extent.YMin)  # 右下
        lng3, lat3 = common.wgs84_to_gcj02(extent.XMax, extent.YMax)  # 右上
        lng4, lat4 = common.wgs84_to_gcj02(extent.XMin, extent.YMax)  # 左上
    else:
        lng1, lat1 = extent.XMin, extent.YMin
        lng2, lat2 = extent.XMax, extent.YMin
        lng3, lat3 = extent.XMax, extent.YMax
        lng4, lat4 = extent.XMin, extent.YMax

    # 格式：左下|右下|右上|左上|左下（闭合）
    polygon_coords = "{:.6f},{:.6f}|{:.6f},{:.6f}|{:.6f},{:.6f}|{:.6f},{:.6f}|{:.6f},{:.6f}".format(
        lng1, lat1, lng2, lat2, lng3, lat3, lng4, lat4, lng1, lat1
    )
    return polygon_coords


def extent_to_bounds(extent):
    """
    将 ArcGIS Extent 对象转换为百度地点检索 3.0 的 bounds 格式。

    百度 polygon 接口使用 WGS84 输入，矩形范围可直接传两个顶点：
    左下角纬度,左下角经度,右上角纬度,右上角经度
    """
    extent = _extent_to_wgs84(extent)

    lat_min = min(extent.YMin, extent.YMax)
    lat_max = max(extent.YMin, extent.YMax)
    lng_min = min(extent.XMin, extent.XMax)
    lng_max = max(extent.XMin, extent.XMax)
    return "{:.6f},{:.6f},{:.6f},{:.6f}".format(lat_min, lng_min, lat_max, lng_max)


AMAP_POI_PAGE_SIZE_LIMIT = 25
AMAP_POI_PAGE_NUM_LIMIT = 100
AMAP_POI_SPLIT_TRIGGER_PAGES = 4
AMAP_POI_SPLIT_MIN_RESULTS = 50
AMAP_POI_SPLIT_MAX_DEPTH = 4
AMAP_POI_RECOVERY_ROUNDS = 2
AMAP_POI_RECOVERY_DEPTH_BOOSTS = (1, 2)
AMAP_POI_RECOVERY_TRIGGER_PAGES = AMAP_POI_SPLIT_TRIGGER_PAGES + 1
AMAP_POI_RECOVERY_MIN_RESULTS = AMAP_POI_SPLIT_MIN_RESULTS + 10
AMAP_POI_MIN_SPLIT_SPAN = 0.001


AMAP_UNFINISHED_REASON_LABELS = {
    'key_pool_exhausted': '高德 Key 已全部用完',
    'request_failed': '高德请求失败',
    'api_error': '高德 API 返回错误',
    'split_limit': '已达到最大拆分深度',
}


def _extent_path_to_text(path):
    """把递归路径转成可读的范围编号。"""
    if not path:
        return 'R'
    return 'R.' + '.'.join(str(index) for index in path)


def _extent_output_bounds(extent, output_wgs84=True):
    """返回用于输出面图层的矩形边界。"""
    extent = _extent_to_wgs84(extent)
    if output_wgs84:
        return float(extent.XMin), float(extent.YMin), float(extent.XMax), float(extent.YMax)

    corners = [
        common.wgs84_to_gcj02(extent.XMin, extent.YMin),
        common.wgs84_to_gcj02(extent.XMax, extent.YMin),
        common.wgs84_to_gcj02(extent.XMax, extent.YMax),
        common.wgs84_to_gcj02(extent.XMin, extent.YMax),
    ]
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _extent_to_output_polygon(extent, output_wgs84=True):
    """把矩形范围转成输出面几何。"""
    xmin, ymin, xmax, ymax = _extent_output_bounds(extent, output_wgs84=output_wgs84)
    sr = arcpy.SpatialReference(4326)
    points = [
        arcpy.Point(xmin, ymin),
        arcpy.Point(xmax, ymin),
        arcpy.Point(xmax, ymax),
        arcpy.Point(xmin, ymax),
        arcpy.Point(xmin, ymin),
    ]
    return arcpy.Polygon(arcpy.Array(points), sr)


def _build_unfinished_rect_record(extent, path, reason, output_wgs84=True):
    """生成未完成矩形的输出记录。"""
    xmin, ymin, xmax, ymax = _extent_output_bounds(extent, output_wgs84=output_wgs84)
    coord_sys = 'WGS84' if output_wgs84 else 'GCJ02'
    reason_text = AMAP_UNFINISHED_REASON_LABELS.get(reason, reason or '高德采集未完成')
    return {
        'extent': extent,
        'rect_id': _extent_path_to_text(path),
        'parent_id': _extent_path_to_text(path[:-1]) if path else '',
        'path': tuple(path),
        'split_level': len(path),
        'reason': reason_text,
        'xmin': xmin,
        'ymin': ymin,
        'xmax': xmax,
        'ymax': ymax,
        'coord_sys': coord_sys,
    }


def _create_amap_unfinished_rect_feature_class(out_gdb, out_fc_name, unfinished_rectangles, output_wgs84=True):
    """把未完成矩形范围写成单独面要素类。"""
    if not unfinished_rectangles:
        return None

    out_fc = os.path.join(out_gdb, out_fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    sr = arcpy.SpatialReference(4326)
    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, 'POLYGON', spatial_reference=sr)

    arcpy.management.AddField(out_fc, 'rect_id', 'TEXT', field_length=50, field_alias='范围编号')
    arcpy.management.AddField(out_fc, 'parent_id', 'TEXT', field_length=50, field_alias='父范围编号')
    arcpy.management.AddField(out_fc, 'split_level', 'SHORT', field_alias='拆分层级')
    arcpy.management.AddField(out_fc, 'reason', 'TEXT', field_length=200, field_alias='终止原因')
    arcpy.management.AddField(out_fc, 'xmin', 'DOUBLE', field_alias='最小经度')
    arcpy.management.AddField(out_fc, 'ymin', 'DOUBLE', field_alias='最小纬度')
    arcpy.management.AddField(out_fc, 'xmax', 'DOUBLE', field_alias='最大经度')
    arcpy.management.AddField(out_fc, 'ymax', 'DOUBLE', field_alias='最大纬度')
    arcpy.management.AddField(out_fc, 'coord_sys', 'TEXT', field_length=10, field_alias='坐标系')

    insert_fields = ['SHAPE@', 'rect_id', 'parent_id', 'split_level', 'reason', 'xmin', 'ymin', 'xmax', 'ymax', 'coord_sys']
    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        for rect in unfinished_rectangles:
            geom = _extent_to_output_polygon(rect['extent'], output_wgs84=output_wgs84)
            cursor.insertRow([
                geom,
                _coerce_text(rect.get('rect_id')),
                _coerce_text(rect.get('parent_id')),
                int(rect.get('split_level', 0) or 0),
                _coerce_text(rect.get('reason')),
                rect.get('xmin'),
                rect.get('ymin'),
                rect.get('xmax'),
                rect.get('ymax'),
                _coerce_text(rect.get('coord_sys')),
            ])

    return out_fc


class _SimpleExtent(object):
    __slots__ = ("XMin", "YMin", "XMax", "YMax", "spatialReference")

    def __init__(self, xmin, ymin, xmax, ymax, spatial_reference=None):
        self.XMin = float(xmin)
        self.YMin = float(ymin)
        self.XMax = float(xmax)
        self.YMax = float(ymax)
        self.spatialReference = spatial_reference


def _extent_span(extent):
    try:
        width = abs(float(extent.XMax) - float(extent.XMin))
        height = abs(float(extent.YMax) - float(extent.YMin))
        return width, height
    except Exception:
        return 0.0, 0.0


def _extent_can_split(extent):
    width, height = _extent_span(extent)
    return width > AMAP_POI_MIN_SPLIT_SPAN and height > AMAP_POI_MIN_SPLIT_SPAN


def _split_extent_quadrants(extent):
    """把一个范围切成四块，用于高密度区域的递归采集。"""
    mid_x = (float(extent.XMin) + float(extent.XMax)) / 2.0
    mid_y = (float(extent.YMin) + float(extent.YMax)) / 2.0
    spatial_reference = getattr(extent, 'spatialReference', None)

    return [
        _SimpleExtent(extent.XMin, extent.YMin, mid_x, mid_y, spatial_reference),
        _SimpleExtent(mid_x, extent.YMin, extent.XMax, mid_y, spatial_reference),
        _SimpleExtent(mid_x, mid_y, extent.XMax, extent.YMax, spatial_reference),
        _SimpleExtent(extent.XMin, mid_y, mid_x, extent.YMax, spatial_reference),
    ]


def _search_amap_poi_polygon_single(extent, keywords=None, types=None, max_poi_count=1000,
                                     output_wgs84=True, all_pois=None, seen_keys=None):
    """在单个范围内执行一次高德 POI 分页检索。"""
    if all_pois is None:
        all_pois = []
    if seen_keys is None:
        seen_keys = set()

    polygon_coords = extent_to_polygon_coords(extent, 'gcj02')
    arcpy.AddMessage('高德搜索范围（GCJ02）：{}'.format(polygon_coords))

    page_num = 1
    pages_fetched = 0
    reported_total = 0
    query_failed = False

    while len(all_pois) < max_poi_count and page_num <= AMAP_POI_PAGE_NUM_LIMIT:
        key = common._get_next_amap_key()
        page_size = min(AMAP_POI_PAGE_SIZE_LIMIT, max_poi_count - len(all_pois))
        params = {
            'key': key,
            'polygon': polygon_coords,
            'output': 'json',
            'page_size': page_size,
            'page_num': page_num,
            'extensions': 'all'
        }
        if keywords:
            params['keywords'] = keywords
        if types:
            params['types'] = types

        try:
            result = _request_json_with_retry(
                common.AMAP_POI_POLYGON_URL,
                params,
                AMAP_POI_REQUEST_TIMEOUT,
                label='高德 POI 请求',
                retry_count=AMAP_POI_REQUEST_RETRY_COUNT,
                retry_delay=AMAP_POI_REQUEST_RETRY_DELAY
            )

            if result.get('status') != '1':
                arcpy.AddWarning('高德 API 返回错误：{}'.format(result.get('info', '未知错误')))
                break

            try:
                reported_total = int(result.get('count', 0) or 0)
            except Exception:
                reported_total = 0

            pois = result.get('pois', []) or []
            if not pois:
                break

            pages_fetched += 1

            for poi in pois:
                lng_gcj, lat_gcj = _parse_location(poi.get('location', ''))
                if lng_gcj is None or lat_gcj is None:
                    continue

                lng_out, lat_out = _convert_output_coord(True if output_wgs84 else False, 'amap', lng_gcj, lat_gcj)
                if lng_out is None or lat_out is None:
                    continue

                poi_record = {
                    'name': _coerce_text(poi.get('name', '')),
                    'address': _coerce_text(poi.get('address', '')),
                    'phone': _coerce_text(poi.get('tel', '')),
                    'type': _coerce_text(poi.get('type', '')),
                    'typecode': _coerce_text(poi.get('typecode', '')),
                    'longitude': lng_out,
                    'latitude': lat_out,
                    'pcode': _coerce_text(poi.get('pcode', '')),
                    'citycode': _coerce_text(poi.get('citycode', '')),
                    'adcode': _coerce_text(poi.get('adcode', '')),
                    'poi_id': _coerce_text(poi.get('id', '')),
                    'source': 'Amap',
                    'coord_sys': 'WGS84' if output_wgs84 else 'GCJ02',
                    'amap_extra': _flatten_amap_poi_extra(poi)
                }

                dedupe_key = _poi_dedupe_key(poi_record)
                if dedupe_key in seen_keys:
                    continue

                seen_keys.add(dedupe_key)
                all_pois.append(poi_record)

            arcpy.AddMessage('高德 POI 第 {} 页，获取 {} 条，累计 {} 条'.format(
                page_num, len(pois), len(all_pois)))

            if len(pois) < page_size:
                break

            page_num += 1

            if len(all_pois) >= max_poi_count:
                all_pois[:] = all_pois[:max_poi_count]
                arcpy.AddMessage('已达到最大获取数量限制 {}'.format(max_poi_count))
                break

        except Exception as e:
            query_failed = True
            arcpy.AddWarning('高德 POI 搜索请求失败：{}'.format(str(e)))
            break

    arcpy.AddMessage('高德 POI 搜索完成，共获取 {} 条'.format(len(all_pois)))
    return reported_total, pages_fetched, query_failed


def _search_amap_poi_polygon_recursive(extent, keywords=None, types=None, max_poi_count=1000,
                                       output_wgs84=True, all_pois=None, seen_keys=None,
                                       depth=0):
    if all_pois is None:
        all_pois = []
    if seen_keys is None:
        seen_keys = set()

    before_count = len(all_pois)
    reported_total, pages_fetched, query_failed = _search_amap_poi_polygon_single(
        extent=extent,
        keywords=keywords,
        types=types,
        max_poi_count=max_poi_count,
        output_wgs84=output_wgs84,
        all_pois=all_pois,
        seen_keys=seen_keys
    )
    collected_count = len(all_pois) - before_count

    need_split = (
        depth < AMAP_POI_SPLIT_MAX_DEPTH
        and collected_count >= AMAP_POI_SPLIT_MIN_RESULTS
        and _extent_can_split(extent)
        and (
            query_failed
            or pages_fetched >= AMAP_POI_SPLIT_TRIGGER_PAGES
            or collected_count >= AMAP_POI_PAGE_SIZE_LIMIT * AMAP_POI_SPLIT_TRIGGER_PAGES
        )
    )

    if need_split:
        arcpy.AddMessage('高德单次多边形检索结果可能已触顶，自动拆分子范围继续采集（第 {} 层）'.format(depth + 1))
        for sub_extent in _split_extent_quadrants(extent):
            if len(all_pois) >= max_poi_count:
                break
            _search_amap_poi_polygon_recursive(
                extent=sub_extent,
                keywords=keywords,
                types=types,
                max_poi_count=max_poi_count,
                output_wgs84=output_wgs84,
                all_pois=all_pois,
                seen_keys=seen_keys,
                depth=depth + 1
            )

    return all_pois


def search_amap_poi_polygon(extent, keywords=None, types=None, max_poi_count=1000,
                            output_wgs84=True):
    """
    高德多边形 POI 搜索

    参数：
    - extent: arcpy.Extent 对象（WGS84）
    - keywords: 搜索关键词（可选）
    - types: POI 类型编码，多个用 | 分隔（可选）
    - max_poi_count: 最大获取数量

    返回：
    - poi_list: POI 列表，每个元素是字典
    """
    if max_poi_count <= 0:
        return []

    extent = _extent_to_wgs84(extent)
    all_pois = []
    seen_keys = set()
    return _search_amap_poi_polygon_recursive(
        extent=extent,
        keywords=keywords,
        types=types,
        max_poi_count=max_poi_count,
        output_wgs84=output_wgs84,
        all_pois=all_pois,
        seen_keys=seen_keys,
        depth=0
    )

    polygon_coords = extent_to_polygon_coords(extent, 'gcj02')
    arcpy.AddMessage('高德搜索范围（GCJ02）：{}'.format(polygon_coords))

    all_pois = []
    seen_keys = set()
    page_num = 1
    page_size_limit = 25  # 高德最大每页 25 条

    while len(all_pois) < max_poi_count:
        key = common._get_next_amap_key()
        page_size = min(page_size_limit, max_poi_count - len(all_pois))
        params = {
            'key': key,
            'polygon': polygon_coords,
            'output': 'json',
            'page_size': page_size,
            'page_num': page_num,
            'extensions': 'all'
        }
        if keywords:
            params['keywords'] = keywords
        if types:
            params['types'] = types

        try:
            resp = requests.get(common.AMAP_POI_POLYGON_URL, params=params, timeout=30)
            resp.raise_for_status()
            result = resp.json()

            if result.get('status') != '1':
                arcpy.AddWarning('高德 API 返回错误：{}'.format(result.get('info', '未知错误')))
                break

            pois = result.get('pois', [])
            if not pois:
                break

            for poi in pois:
                lng_gcj, lat_gcj = _parse_location(poi.get('location', ''))
                if lng_gcj is None or lat_gcj is None:
                    continue

                lng_out, lat_out = _convert_output_coord(True if output_wgs84 else False, 'amap', lng_gcj, lat_gcj)
                if lng_out is None or lat_out is None:
                    continue

                poi_record = {
                    'name': _coerce_text(poi.get('name', '')),
                    'address': _coerce_text(poi.get('address', '')),
                    'phone': _coerce_text(poi.get('tel', '')),
                    'type': _coerce_text(poi.get('type', '')),
                    'typecode': _coerce_text(poi.get('typecode', '')),
                    'longitude': lng_out,
                    'latitude': lat_out,
                    'pcode': _coerce_text(poi.get('pcode', '')),
                    'citycode': _coerce_text(poi.get('citycode', '')),
                    'adcode': _coerce_text(poi.get('adcode', '')),
                    'poi_id': _coerce_text(poi.get('id', '')),
                    'source': 'Amap',
                    'coord_sys': 'WGS84' if output_wgs84 else 'GCJ02'
                }

                dedupe_key = _poi_dedupe_key(poi_record)
                if dedupe_key in seen_keys:
                    continue

                seen_keys.add(dedupe_key)
                all_pois.append(poi_record)

            arcpy.AddMessage('高德 POI 第 {} 页，获取 {} 条，累计 {} 条'.format(
                page_num, len(pois), len(all_pois)))

            # 如果本页返回数量不足 page_size，说明已经到最后一页
            if len(pois) < page_size:
                break

            page_num += 1

            # 检查是否达到最大数量限制
            if len(all_pois) >= max_poi_count:
                all_pois = all_pois[:max_poi_count]
                arcpy.AddMessage('已达到最大获取数量限制 {}'.format(max_poi_count))
                break

        except Exception as e:
            arcpy.AddWarning('高德 POI 搜索请求失败：{}'.format(str(e)))
            break

    arcpy.AddMessage('高德 POI 搜索完成，共获取 {} 条'.format(len(all_pois)))
    return all_pois


def search_baidu_poi_polygon(extent, query=None, max_poi_count=1000,
                             output_wgs84=True):
    """
    百度地点检索 3.0 多边形/矩形区域 POI 搜索

    参数：
    - extent: arcpy.Extent 对象（WGS84）
    - query: 搜索关键词（必填）
    - max_poi_count: 最大获取数量

    返回：
    - poi_list: POI 列表
    """
    if max_poi_count <= 0:
        return []

    bounds = extent_to_bounds(extent)
    arcpy.AddMessage('百度地点检索 3.0 搜索范围（WGS84）：{}'.format(bounds))
    arcpy.AddMessage('百度请求参数：coord_type=1, ret_coordtype=gcj02ll')

    all_pois = []
    seen_keys = set()
    page_num = 0  # 百度页码从 0 开始
    page_size_limit = 20  # 百度最大每页 20 条

    # 如果没有指定 query，使用空字符串
    if not query or not str(query).strip():
        raise ValueError('百度地点检索 3.0 必须填写 query 关键词；该接口不支持空关键词全量搜索。请改用高德，或填写如“酒店”“餐饮”等关键词。')
    query = str(query).strip()

    while len(all_pois) < max_poi_count:
        page_size = min(page_size_limit, max_poi_count - len(all_pois))
        params = {
            'query': query,
            'bounds': bounds,
            'output': 'json',
            'coord_type': 1,
            'ret_coordtype': 'gcj02ll',
            'page_size': page_size,
            'page_num': page_num,
            'scope': '2',  # 返回详细信息
            'extensions_adcode': 'true'
        }

        request_result = _request_baidu_poi_json_with_rotation(
            params,
            timeout=30,
            label='百度 POI 请求'
        )
        if not request_result.get('ok'):
            reason = request_result.get('reason') or '百度 POI 请求失败'
            arcpy.AddWarning('百度地点检索 3.0 请求失败：{}'.format(reason))
            break

        result = request_result['result']
        results = result.get('results', []) or []
        if not results:
            break

        for poi in results:
            location = poi.get('location', {})
            gcj_lng, gcj_lat = _parse_location(location)

            if gcj_lng is not None and gcj_lat is not None:
                lng_out, lat_out = _convert_output_coord(True if output_wgs84 else False, 'baidu', gcj_lng, gcj_lat)
            else:
                lng_out, lat_out = None, None

            if lng_out is None or lat_out is None:
                continue

            detail_info = poi.get('detail_info', {}) or {}
            if not isinstance(detail_info, dict):
                detail_info = {}
            poi_type_text = (
                _coerce_text(poi.get('tag'))
                or _coerce_text(detail_info.get('tag'))
                or _coerce_text(detail_info.get('classified_poi_tag'))
                or _coerce_text(detail_info.get('type'))
                or _coerce_text(poi.get('type'))
            )

            poi_record = {
                'name': _coerce_text(poi.get('name', '')),
                'address': _coerce_text(poi.get('address', '') or detail_info.get('address', '')),
                'phone': _coerce_text(poi.get('telephone', '') or poi.get('phone', '') or detail_info.get('telephone', '') or detail_info.get('tel', '')),
                'type': poi_type_text,
                'typecode': '',  # 百度没有统一的 typecode
                'longitude': lng_out,
                'latitude': lat_out,
                'pcode': '',
                'citycode': '',
                'adcode': _coerce_text(poi.get('adcode', '') or (poi.get('ad_info') or {}).get('adcode', '')),
                'poi_id': _coerce_text(poi.get('uid', '') or poi.get('id', '')),
                'source': 'Baidu',
                'coord_sys': 'WGS84' if output_wgs84 else 'GCJ02'
            }

            dedupe_key = _poi_dedupe_key(poi_record)
            if dedupe_key in seen_keys:
                continue

            seen_keys.add(dedupe_key)
            all_pois.append(poi_record)

        arcpy.AddMessage('百度地点检索 3.0 第 {} 页，获取 {} 条，累计 {} 条'.format(
            page_num + 1, len(results), len(all_pois)))

        # 检查是否到最后一页
        try:
            total = int(result.get('total', 0) or 0)
        except Exception:
            total = 0
        if len(all_pois) >= total or len(results) < page_size:
            break

        page_num += 1

        # 检查是否达到最大数量限制
        if len(all_pois) >= max_poi_count:
            all_pois = all_pois[:max_poi_count]
            arcpy.AddMessage('已达到最大获取数量限制 {}'.format(max_poi_count))
            break

    arcpy.AddMessage('百度地点检索 3.0 完成，共获取 {} 条'.format(len(all_pois)))
    return all_pois


search_baidu_poi_bounds = search_baidu_poi_polygon


def _request_baidu_poi_json_with_rotation(params, timeout=30, label='百度 POI 请求'):
    """按 Key 轮询请求百度 POI 接口，遇到失效 Key 时继续尝试下一个。"""
    pool_size = max(1, len(common.BAIDU_KEYS))
    tried_count = 0
    switch_failure_count = 0
    last_result = None
    last_reason = ''
    last_reason_code = ''

    for _ in range(pool_size):
        key_index, key = common.get_next_baidu_key_info()
        if key is None:
            break

        tried_count += 1
        request_params = dict(params)
        request_params['ak'] = key

        try:
            resp = requests.get(common.BAIDU_POI_URL, params=request_params, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
        except Exception as exc:
            classification = common.classify_baidu_response_issue(message=str(exc), error=exc)
            last_result = None
            last_reason = classification.get('message') or str(exc)
            last_reason_code = classification.get('kind') or 'request_error'

            if key_index is not None and classification.get('switch_key'):
                try:
                    common._set_baidu_key_delay(key_index, classification.get('delay_seconds', 3600))
                except Exception:
                    pass
                arcpy.AddWarning(
                    '百度 Key {} 触发{}，已切换下一个 Key'.format(
                        common.mask_key(key), _baidu_issue_label(classification.get('kind'))
                    )
                )
                switch_failure_count += 1
                continue

            return {
                'ok': False,
                'stop_all': False,
                'result': None,
                'reason_code': last_reason_code,
                'reason': last_reason or '百度 POI 请求失败',
                'key_index': key_index,
                'key': key,
            }

        status = _coerce_text(result.get('status'))
        if status == '0':
            return {
                'ok': True,
                'stop_all': False,
                'result': result,
                'reason_code': '',
                'reason': '',
                'key_index': key_index,
                'key': key,
            }

        error_msg = (
            result.get('message')
            or result.get('msg')
            or result.get('info')
            or '未知错误'
        )
        classification = common.classify_baidu_response_issue(
            status=status,
            message=error_msg,
            http_status=resp.status_code
        )
        last_result = result
        last_reason = classification.get('message') or _coerce_text(error_msg) or '百度 API 返回错误'
        last_reason_code = classification.get('kind') or 'api_error'

        if classification.get('switch_key'):
            switch_failure_count += 1
            if key_index is not None:
                try:
                    common._set_baidu_key_delay(key_index, classification.get('delay_seconds', 3600))
                except Exception:
                    pass
                arcpy.AddWarning(
                    '百度 Key {} 触发{}，已切换下一个 Key'.format(
                        common.mask_key(key), _baidu_issue_label(classification.get('kind'))
                    )
                )
            continue

        return {
            'ok': False,
            'stop_all': False,
            'result': result,
            'reason_code': last_reason_code,
            'reason': last_reason or '百度 API 返回错误',
            'key_index': key_index,
            'key': key,
        }

    if tried_count == 0:
        return {
            'ok': False,
            'stop_all': True,
            'result': None,
            'reason_code': 'key_pool_exhausted',
            'reason': '当前没有可用的百度 Key',
            'key_index': None,
            'key': None,
        }

    if switch_failure_count > 0 and switch_failure_count == tried_count:
        return {
            'ok': False,
            'stop_all': True,
            'result': last_result,
            'reason_code': 'key_pool_exhausted',
            'reason': last_reason or '百度 Key 已全部用完',
            'key_index': None,
            'key': None,
        }

    return {
        'ok': False,
        'stop_all': False,
        'result': last_result,
        'reason_code': last_reason_code or 'request_failed',
        'reason': last_reason or '百度 POI 请求失败',
        'key_index': None,
        'key': None,
    }


def _request_amap_poi_json_with_rotation(params, timeout=AMAP_POI_REQUEST_TIMEOUT,
                                        label='楂樺痉 POI 璇锋眰',
                                        retry_count=AMAP_POI_REQUEST_RETRY_COUNT,
                                        retry_delay=AMAP_POI_REQUEST_RETRY_DELAY):
    """按 Key 轮询请求高德 POI 接口。"""
    pool_size = max(1, len(common.AMAP_KEYS))
    tried_count = 0
    switch_failure_count = 0
    last_result = None
    last_reason = ''
    last_reason_code = ''

    for _ in range(pool_size):
        key_index, key = common.get_next_amap_key_info()
        if key is None:
            break

        tried_count += 1
        request_params = dict(params)
        request_params['key'] = key

        try:
            result = _request_json_with_retry(
                common.AMAP_POI_POLYGON_URL,
                request_params,
                timeout,
                label=label,
                retry_count=retry_count,
                retry_delay=retry_delay
            )
        except Exception as exc:
            last_result = None
            last_reason = str(exc)
            last_reason_code = 'request_failed'
            continue

        status = _coerce_text(result.get('status'))
        if status == '1':
            return {
                'ok': True,
                'stop_all': False,
                'result': result,
                'reason_code': '',
                'reason': '',
                'key_index': key_index,
                'key': key
            }

        classification = common.classify_amap_response_issue(status, result.get('info'), result.get('infocode'))
        last_result = result
        last_reason = classification.get('message') or _coerce_text(result.get('info')) or _coerce_text(result.get('infocode')) or '高德 API 返回错误'
        last_reason_code = classification.get('kind') or 'api_error'

        if classification.get('switch_key'):
            switch_failure_count += 1
            common.set_key_delay('amap', key_index, classification.get('delay_seconds', 3600))
        else:
            continue

    if tried_count == 0:
        return {
            'ok': False,
            'stop_all': True,
            'result': None,
            'reason_code': 'key_pool_exhausted',
            'reason': '当前没有可用的高德 Key',
            'key_index': None,
            'key': None
        }

    if switch_failure_count > 0 and switch_failure_count == tried_count:
        return {
            'ok': False,
            'stop_all': True,
            'result': last_result,
            'reason_code': 'key_pool_exhausted',
            'reason': last_reason or '高德 Key 已全部用完',
            'key_index': None,
            'key': None
        }

    return {
        'ok': False,
        'stop_all': False,
        'result': last_result,
        'reason_code': last_reason_code or 'request_failed',
        'reason': last_reason or '高德 POI 请求失败',
        'key_index': None,
        'key': None
    }


def _search_amap_poi_polygon_single_detailed(extent, keywords=None, types=None, max_poi_count=1000,
                                             output_wgs84=True, all_pois=None, seen_keys=None):
    """在单个范围内执行一轮高德 POI 分页采集，并返回状态。"""
    if all_pois is None:
        all_pois = []
    if seen_keys is None:
        seen_keys = set()

    polygon_coords = extent_to_polygon_coords(extent, 'gcj02')
    arcpy.AddMessage('高德搜索范围（GCJ02）：{}'.format(polygon_coords))

    page_num = 1
    pages_fetched = 0
    reported_total = 0
    query_failed = False
    stop_all = False
    failure_reason = ''
    failure_reason_code = ''

    while len(all_pois) < max_poi_count and page_num <= AMAP_POI_PAGE_NUM_LIMIT:
        page_size = min(AMAP_POI_PAGE_SIZE_LIMIT, max_poi_count - len(all_pois))
        params = {
            'polygon': polygon_coords,
            'output': 'json',
            'page_size': page_size,
            'page_num': page_num,
            'extensions': 'all'
        }
        if keywords:
            params['keywords'] = keywords
        if types:
            params['types'] = types

        request_result = _request_amap_poi_json_with_rotation(
            params,
            timeout=AMAP_POI_REQUEST_TIMEOUT,
            label='高德 POI 请求',
            retry_count=AMAP_POI_REQUEST_RETRY_COUNT,
            retry_delay=AMAP_POI_REQUEST_RETRY_DELAY
        )

        if not request_result.get('ok'):
            query_failed = True
            stop_all = bool(request_result.get('stop_all'))
            failure_reason_code = request_result.get('reason_code') or ('key_pool_exhausted' if stop_all else 'request_failed')
            failure_reason = request_result.get('reason') or AMAP_UNFINISHED_REASON_LABELS.get(failure_reason_code, '高德 POI 请求失败')
            arcpy.AddWarning('高德 POI 搜索请求失败：{}'.format(failure_reason))
            break

        result = request_result['result']
        if result.get('status') != '1':
            query_failed = True
            failure_reason_code = 'api_error'
            failure_reason = _coerce_text(result.get('info')) or '高德 API 返回错误'
            arcpy.AddWarning('高德 API 返回错误：{}'.format(failure_reason))
            break

        try:
            reported_total = int(result.get('count', 0) or 0)
        except Exception:
            reported_total = 0

        pois = result.get('pois', []) or []
        if not pois:
            break

        pages_fetched += 1

        for poi in pois:
            lng_gcj, lat_gcj = _parse_location(poi.get('location', ''))
            if lng_gcj is None or lat_gcj is None:
                continue

            lng_out, lat_out = _convert_output_coord(True if output_wgs84 else False, 'amap', lng_gcj, lat_gcj)
            if lng_out is None or lat_out is None:
                continue

            poi_record = {
                'name': _coerce_text(poi.get('name', '')),
                'address': _coerce_text(poi.get('address', '')),
                'phone': _coerce_text(poi.get('tel', '')),
                'type': _coerce_text(poi.get('type', '')),
                'typecode': _coerce_text(poi.get('typecode', '')),
                'longitude': lng_out,
                'latitude': lat_out,
                'pcode': _coerce_text(poi.get('pcode', '')),
                'citycode': _coerce_text(poi.get('citycode', '')),
                'adcode': _coerce_text(poi.get('adcode', '')),
                'poi_id': _coerce_text(poi.get('id', '')),
                'source': 'Amap',
                'coord_sys': 'WGS84' if output_wgs84 else 'GCJ02',
                'amap_extra': _flatten_amap_poi_extra(poi)
            }

            dedupe_key = _poi_dedupe_key(poi_record)
            if dedupe_key in seen_keys:
                continue

            seen_keys.add(dedupe_key)
            all_pois.append(poi_record)

        arcpy.AddMessage('高德 POI 第 {} 页，获取 {} 条，累计 {} 条'.format(
            page_num, len(pois), len(all_pois)))

        if len(pois) < page_size:
            break

        page_num += 1

        if len(all_pois) >= max_poi_count:
            all_pois[:] = all_pois[:max_poi_count]
            arcpy.AddMessage('已达到最大获取数量限制 {}'.format(max_poi_count))
            break

    arcpy.AddMessage('高德 POI 搜索完成，共获取 {} 条'.format(len(all_pois)))
    return {
        'pois': all_pois,
        'reported_total': reported_total,
        'pages_fetched': pages_fetched,
        'query_failed': query_failed,
        'stop_all': stop_all,
        'failure_reason': failure_reason,
        'failure_reason_code': failure_reason_code,
    }



def search_amap_poi_polygon_detailed(extent, keywords=None, types=None, max_poi_count=1000,
                                     output_wgs84=True):
    """高德多边形 POI 递归采集，首轮后仅对未完成矩形做有限轮回灌。"""
    if max_poi_count <= 0:
        return {
            'pois': [],
            'unfinished_rectangles': [],
            'terminated_early': False,
            'stop_all': False,
            'termination_reason': '',
            'reported_total': 0,
            'pages_fetched': 0,
            'query_failed': False,
        }

    extent = _extent_to_wgs84(extent)
    all_pois = []
    seen_keys = set()
    primary_unfinished_rectangles = []
    final_unfinished_rectangles = []
    overall_stop_all = False
    overall_reason = ''
    primary_query_failed = False

    def _append_unfinished(target_bucket, current_extent, path, reason_code):
        target_bucket.append(
            _build_unfinished_rect_record(current_extent, path, reason_code, output_wgs84)
        )

    def _recurse(current_extent, depth=0, path=(), unfinished_bucket=None,
                 max_depth=AMAP_POI_SPLIT_MAX_DEPTH, stage_name='首轮',
                 split_trigger_pages=AMAP_POI_SPLIT_TRIGGER_PAGES,
                 split_min_results=AMAP_POI_SPLIT_MIN_RESULTS,
                 use_raw_density=False):
        if unfinished_bucket is None:
            unfinished_bucket = []

        before_count = len(all_pois)
        single_result = _search_amap_poi_polygon_single_detailed(
            extent=current_extent,
            keywords=keywords,
            types=types,
            max_poi_count=max_poi_count,
            output_wgs84=output_wgs84,
            all_pois=all_pois,
            seen_keys=seen_keys
        )

        raw_total = int(single_result.get('reported_total') or 0)
        pages_fetched = int(single_result.get('pages_fetched') or 0)
        query_failed = bool(single_result.get('query_failed'))
        stop_all = bool(single_result.get('stop_all'))
        reason_code = single_result.get('failure_reason_code') or ''
        reason_text = single_result.get('failure_reason') or ''
        added_count = len(all_pois) - before_count
        density_total = raw_total if use_raw_density and raw_total > 0 else added_count

        if len(all_pois) >= max_poi_count:
            all_pois[:] = all_pois[:max_poi_count]
            return {
                'terminated_early': False,
                'stop_all': False,
                'reason_code': '',
                'reason': '',
                'query_failed': False
            }

        if stop_all:
            _append_unfinished(unfinished_bucket, current_extent, path, 'key_pool_exhausted')
            return {
                'terminated_early': True,
                'stop_all': True,
                'reason_code': 'key_pool_exhausted',
                'reason': reason_text or AMAP_UNFINISHED_REASON_LABELS['key_pool_exhausted'],
                'query_failed': True
            }

        if query_failed:
            _append_unfinished(
                unfinished_bucket,
                current_extent,
                path,
                reason_code or 'request_failed'
            )
            return {
                'terminated_early': True,
                'stop_all': False,
                'reason_code': reason_code or 'request_failed',
                'reason': reason_text or AMAP_UNFINISHED_REASON_LABELS.get(
                    reason_code or 'request_failed',
                    '高德采集未完成'
                ),
                'query_failed': True
            }

        can_split = depth < max_depth and _extent_can_split(current_extent)
        pages_hit = pages_fetched >= split_trigger_pages
        raw_capacity_hit = raw_total >= AMAP_POI_PAGE_SIZE_LIMIT * split_trigger_pages
        density_capacity_hit = density_total >= AMAP_POI_PAGE_SIZE_LIMIT * split_trigger_pages
        top_out_signal = pages_hit or raw_capacity_hit or density_capacity_hit
        density_enough = density_total >= split_min_results or raw_total >= split_min_results
        should_split = can_split and top_out_signal and density_enough

        if should_split:
            if stage_name == '首轮':
                split_message = '高德单次多边形检索结果可能已触顶，自动拆分子范围继续采集'
            else:
                split_message = '高德回灌矩形结果可能已触顶，继续拆分子范围采集'
            arcpy.AddMessage('{}（第 {} 层）'.format(split_message, depth + 1))

            sub_extents = _split_extent_quadrants(current_extent)
            child_reason_code = reason_code or 'split_limit'
            child_reason_text = reason_text
            child_stop_all = False
            child_query_failed = False

            for idx, sub_extent in enumerate(sub_extents, start=1):
                if len(all_pois) >= max_poi_count:
                    break

                child_result = _recurse(
                    sub_extent,
                    depth=depth + 1,
                    path=path + (idx,),
                    unfinished_bucket=unfinished_bucket,
                    max_depth=max_depth,
                    stage_name=stage_name,
                    split_trigger_pages=split_trigger_pages,
                    split_min_results=split_min_results,
                    use_raw_density=use_raw_density
                )

                if child_result.get('reason_code'):
                    child_reason_code = child_result.get('reason_code') or child_reason_code
                if child_result.get('reason'):
                    child_reason_text = child_result.get('reason') or child_reason_text

                if child_result.get('stop_all'):
                    child_stop_all = True
                    child_reason_code = child_result.get('reason_code') or 'key_pool_exhausted'
                    child_reason_text = child_result.get('reason') or AMAP_UNFINISHED_REASON_LABELS.get(
                        child_reason_code,
                        '高德采集未完成'
                    )
                    for rem_idx, rem_extent in enumerate(sub_extents[idx:], start=idx + 1):
                        _append_unfinished(
                            unfinished_bucket,
                            rem_extent,
                            path + (rem_idx,),
                            child_reason_code
                        )
                    break

                if child_result.get('query_failed'):
                    child_query_failed = True
                    child_reason_code = child_result.get('reason_code') or 'request_failed'
                    child_reason_text = child_result.get('reason') or AMAP_UNFINISHED_REASON_LABELS.get(
                        child_reason_code,
                        '高德采集未完成'
                    )
                    for rem_idx, rem_extent in enumerate(sub_extents[idx:], start=idx + 1):
                        _append_unfinished(
                            unfinished_bucket,
                            rem_extent,
                            path + (rem_idx,),
                            child_reason_code
                        )
                    break

            if child_stop_all:
                return {
                    'terminated_early': True,
                    'stop_all': True,
                    'reason_code': child_reason_code,
                    'reason': child_reason_text,
                    'query_failed': True
                }

            if child_query_failed:
                return {
                    'terminated_early': True,
                    'stop_all': False,
                    'reason_code': child_reason_code,
                    'reason': child_reason_text,
                    'query_failed': True
                }

            return {
                'terminated_early': bool(unfinished_bucket),
                'stop_all': False,
                'reason_code': child_reason_code if unfinished_bucket else '',
                'reason': child_reason_text if unfinished_bucket else '',
                'query_failed': False
            }

        if top_out_signal:
            reason_code = reason_code or 'split_limit'
            _append_unfinished(unfinished_bucket, current_extent, path, reason_code)
            return {
                'terminated_early': True,
                'stop_all': False,
                'reason_code': reason_code,
                'reason': reason_text or AMAP_UNFINISHED_REASON_LABELS.get(
                    reason_code,
                    '高德采集未完成'
                ),
                'query_failed': False
            }

        return {
            'terminated_early': False,
            'stop_all': False,
            'reason_code': '',
            'reason': '',
            'query_failed': False
        }

    primary_result = _recurse(
        extent,
        depth=0,
        path=(),
        unfinished_bucket=primary_unfinished_rectangles,
        max_depth=AMAP_POI_SPLIT_MAX_DEPTH,
        stage_name='首轮',
        split_trigger_pages=AMAP_POI_SPLIT_TRIGGER_PAGES,
        split_min_results=AMAP_POI_SPLIT_MIN_RESULTS,
        use_raw_density=False
    )

    overall_stop_all = bool(primary_result.get('stop_all'))
    overall_reason = primary_result.get('reason') or ''
    primary_query_failed = bool(primary_result.get('query_failed'))
    final_unfinished_rectangles = list(primary_unfinished_rectangles)

    if (
        primary_unfinished_rectangles
        and not overall_stop_all
        and not primary_query_failed
        and len(all_pois) < max_poi_count
    ):
        current_round_rectangles = list(primary_unfinished_rectangles)
        recovery_depth_boosts = list(AMAP_POI_RECOVERY_DEPTH_BOOSTS[:AMAP_POI_RECOVERY_ROUNDS])
        if not recovery_depth_boosts and AMAP_POI_RECOVERY_ROUNDS > 0:
            recovery_depth_boosts = [1] * AMAP_POI_RECOVERY_ROUNDS

        for round_index, depth_boost in enumerate(recovery_depth_boosts, start=1):
            if not current_round_rectangles or len(all_pois) >= max_poi_count:
                break

            round_start_count = len(all_pois)
            round_unfinished_rectangles = []
            round_stop_all = False
            round_query_failed = False
            round_reason = ''
            recovery_max_depth = AMAP_POI_SPLIT_MAX_DEPTH + max(1, int(depth_boost or 1))

            arcpy.AddMessage(
                '高德未完成矩形回灌第 {} 轮：对 {} 个矩形提高拆分深度到第 {} 层后重新采集'.format(
                    round_index,
                    len(current_round_rectangles),
                    recovery_max_depth
                )
            )

            for rect_index, rect in enumerate(current_round_rectangles):
                if len(all_pois) >= max_poi_count:
                    break

                rect_result = _recurse(
                    rect.get('extent'),
                    depth=0,
                    path=rect.get('path', ()),
                    unfinished_bucket=round_unfinished_rectangles,
                    max_depth=recovery_max_depth,
                    stage_name='回灌',
                    split_trigger_pages=AMAP_POI_RECOVERY_TRIGGER_PAGES,
                    split_min_results=AMAP_POI_RECOVERY_MIN_RESULTS,
                    use_raw_density=True
                )

                if rect_result.get('reason'):
                    round_reason = rect_result.get('reason') or round_reason

                if rect_result.get('stop_all'):
                    round_stop_all = True
                if rect_result.get('query_failed'):
                    round_query_failed = True

                if round_stop_all or round_query_failed:
                    for remaining_rect in current_round_rectangles[rect_index + 1:]:
                        round_unfinished_rectangles.append(remaining_rect)
                    break

            round_new_poi = len(all_pois) - round_start_count
            final_unfinished_rectangles = list(round_unfinished_rectangles)
            overall_reason = round_reason or overall_reason

            if round_stop_all:
                overall_stop_all = True
                break

            if round_query_failed:
                break

            if not final_unfinished_rectangles:
                break

            if round_new_poi <= 0 or len(final_unfinished_rectangles) >= len(current_round_rectangles):
                break

            current_round_rectangles = final_unfinished_rectangles

    return {
        'pois': all_pois,
        'unfinished_rectangles': final_unfinished_rectangles,
        'terminated_early': bool(final_unfinished_rectangles) or overall_stop_all,
        'stop_all': overall_stop_all,
        'termination_reason': overall_reason,
        'reported_total': 0,
        'pages_fetched': 0,
        'query_failed': bool(final_unfinished_rectangles) or overall_stop_all or primary_query_failed,
    }

def run_poi_search_in_extent(extent, keywords=None, poi_types=None, platform='amap',
                               max_poi_count=1000, output_wgs84=True,
                               out_gdb=None, out_fc_name=None,
                               out_unfinished_fc_name=None):
    """
    POI 搜索主处理函数

    参数：
    - extent: arcpy.Extent 对象（WGS84坐标系）
    - keywords: 搜索关键词（可选）
    - poi_types: POI 类型编码（仅高德支持，可选）
    - platform: 搜索平台 'amap' 或 'baidu'
    - max_poi_count: 最大获取数量
    - output_wgs84: True 输出 WGS84；False 输出 GCJ02
    - out_gdb: 输出 GDB 路径或 'in_memory'
    - out_fc_name: 输出要素类名称

    返回：
    - out_fc: 输出要素类路径
    """
    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError('必须指定 out_fc_name')
    if not out_gdb:
        raise ValueError('必须指定 out_gdb')
    if max_poi_count <= 0:
        raise ValueError('max_poi_count 必须大于 0')

    # 执行 POI 搜索
    unfinished_rectangles = []
    if platform == 'amap':
        poi_result = search_amap_poi_polygon_detailed(extent, keywords, poi_types, max_poi_count, output_wgs84)
        poi_list = poi_result.get('pois', [])
        unfinished_rectangles = poi_result.get('unfinished_rectangles', []) or []
        if unfinished_rectangles and not out_unfinished_fc_name:
            out_unfinished_fc_name = '{}_未完成矩形范围'.format(out_fc_name)
    elif platform == 'baidu':
        poi_list = search_baidu_poi_polygon(extent, keywords, max_poi_count, output_wgs84)
    else:
        raise ValueError('不支持的平台：{}'.format(platform))

    if not poi_list and not unfinished_rectangles:
        arcpy.AddWarning('未找到任何 POI')
        return None, None

    poi_list = _dedupe_poi_list(poi_list)
    if not poi_list and not unfinished_rectangles:
        arcpy.AddWarning('未找到任何有效的 POI 坐标')
        return None, None

    # 检查是否使用内存工作空间
    if out_gdb.lower() == 'in_memory':
        arcpy.AddMessage('使用临时空间（内存工作空间）创建要素类')
    else:
        arcpy.AddMessage('输出到文件GDB：{}'.format(out_gdb))

    # 创建输出要素类
    out_fc = os.path.join(out_gdb, out_fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    if output_wgs84:
        sr = arcpy.SpatialReference(4326)  # WGS84
        arcpy.AddMessage('输出坐标系：WGS84')
    else:
        sr = arcpy.SpatialReference(4326)
        arcpy.AddMessage('输出坐标系：GCJ02（坐标值按GCJ02写出，空间参考写为WGS84）')
    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, 'POINT', spatial_reference=sr)

    # 添加字段
    arcpy.management.AddField(out_fc, 'name', 'TEXT', field_length=100, field_alias='POI名称')
    arcpy.management.AddField(out_fc, 'address', 'TEXT', field_length=255, field_alias='地址')
    arcpy.management.AddField(out_fc, 'phone', 'TEXT', field_length=50, field_alias='电话')
    arcpy.management.AddField(out_fc, 'type', 'TEXT', field_length=100, field_alias='类型')
    arcpy.management.AddField(out_fc, 'typecode', 'TEXT', field_length=20, field_alias='类型编码')
    arcpy.management.AddField(out_fc, 'longitude', 'DOUBLE', field_alias='经度')
    arcpy.management.AddField(out_fc, 'latitude', 'DOUBLE', field_alias='纬度')
    arcpy.management.AddField(out_fc, 'pcode', 'TEXT', field_length=20, field_alias='省份编码')
    arcpy.management.AddField(out_fc, 'citycode', 'TEXT', field_length=20, field_alias='城市编码')
    arcpy.management.AddField(out_fc, 'adcode', 'TEXT', field_length=20, field_alias='区域编码')
    arcpy.management.AddField(out_fc, 'poi_id', 'TEXT', field_length=50, field_alias='POI ID')
    arcpy.management.AddField(out_fc, 'source', 'TEXT', field_length=10, field_alias='数据来源')
    arcpy.management.AddField(out_fc, 'coord_sys', 'TEXT', field_length=10, field_alias='输出坐标系')

    amap_extra_specs = []
    if platform == 'amap':
        amap_extra_specs = _collect_amap_extra_field_specs(poi_list)
        if amap_extra_specs:
            arcpy.AddMessage('高德返回额外字段 {} 个，将写入输出要素类'.format(len(amap_extra_specs)))
            for spec in amap_extra_specs:
                arcpy.management.AddField(
                    out_fc,
                    spec['field_name'],
                    'TEXT',
                    field_length=AMAP_EXTRA_TEXT_FIELD_LENGTH,
                    field_alias=spec['field_alias']
                )

    arcpy.AddMessage('已创建要素类和字段，开始写入 {} 条 POI...'.format(len(poi_list)))

    # 插入数据
    base_fields = ['name', 'address', 'phone', 'type', 'typecode',
                   'longitude', 'latitude', 'pcode', 'citycode', 'adcode', 'poi_id', 'source', 'coord_sys']
    fields = ['SHAPE@'] + base_fields + [spec['field_name'] for spec in amap_extra_specs]

    with arcpy.da.InsertCursor(out_fc, fields) as cursor:
        for poi in poi_list:
            try:
                lng = poi['longitude']
                lat = poi['latitude']
                if lng is None or lat is None:
                    continue
                pt = arcpy.Point(lng, lat)
                geom = arcpy.PointGeometry(pt, sr)
                row = [
                    geom,
                    _coerce_text(poi.get('name')),
                    _coerce_text(poi.get('address')),
                    _coerce_text(poi.get('phone')),
                    _coerce_text(poi.get('type')),
                    _coerce_text(poi.get('typecode')),
                    poi['longitude'],
                    poi['latitude'],
                    _coerce_text(poi.get('pcode')),
                    _coerce_text(poi.get('citycode')),
                    _coerce_text(poi.get('adcode')),
                    _coerce_text(poi.get('poi_id')),
                    _coerce_text(poi.get('source')),
                    _coerce_text(poi.get('coord_sys'))
                ]
                if amap_extra_specs:
                    amap_extra = poi.get('amap_extra') or {}
                    row.extend(_coerce_text(amap_extra.get(spec['source_key'])) for spec in amap_extra_specs)
                cursor.insertRow(row)
            except Exception as e:
                arcpy.AddWarning('写入 POI 失败：{} - {}'.format(poi.get('name', ''), str(e)))

    arcpy.AddMessage('POI 数据写入完成')
    arcpy.AddMessage('输出要素类：{}'.format(out_fc))

    unfinished_fc = None
    if platform == 'amap' and unfinished_rectangles:
        if not out_unfinished_fc_name:
            out_unfinished_fc_name = '{}_未完成矩形范围'.format(out_fc_name)
        arcpy.AddMessage('未完成矩形范围数量：{}'.format(len(unfinished_rectangles)))
        unfinished_fc = _create_amap_unfinished_rect_feature_class(
            out_gdb,
            out_unfinished_fc_name,
            unfinished_rectangles,
            output_wgs84=output_wgs84
        )
        if unfinished_fc:
            arcpy.AddMessage('未完成矩形范围要素类：{}'.format(unfinished_fc))

    return out_fc, unfinished_fc
