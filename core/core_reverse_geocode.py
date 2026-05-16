# -*- coding: utf-8 -*-
"""Reverse geocode points into one row per nearby POI."""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import arcpy
import requests

from . import core_common as common


TEXT_LEN = 32767


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


def _txt(v, default=''):
    if v is None:
        return default
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return default


def _mask_key(key):
    return common.mask_key(key) if key else 'N/A'


def _json(v):
    try:
        return json.dumps(v, ensure_ascii=False, separators=(',', ':'))
    except Exception:
        return _txt(v, '')


def _textify(v):
    if v is None:
        return ''
    if isinstance(v, list):
        parts = []
        for item in v:
            if item in (None, ''):
                continue
            if isinstance(item, (dict, list)):
                parts.append(_json(item))
            else:
                parts.append(_txt(item, ''))
        return '; '.join([part for part in parts if part])
    if isinstance(v, dict):
        return _json(v)
    return _txt(v, '')


def _poi_from_baidu(item):
    point = item.get('point', {}) or {}
    x = _sfloat(point.get('x'))
    y = _sfloat(point.get('y'))
    if x is not None and y is not None:
        x, y = common.bd09_to_wgs84(x, y)
    return {
        'poi_id': item.get('uid', '') or item.get('id', ''),
        'poi_tel': item.get('tel', ''),
        'poi_business_area': item.get('businessArea', ''),
        'poi_name': item.get('name', ''),
        'poi_type': item.get('poiType', ''),
        'poi_distance': _sint(item.get('distance')),
        'poi_direction': item.get('direction', ''),
        'poi_address': item.get('addr', ''),
        'poi_lng': x,
        'poi_lat': y,
        'poi_raw': _json(item),
    }


def _poi_from_amap(item):
    loc = item.get('location', '')
    if loc and ',' in loc:
        x, y = map(float, loc.split(',', 1))
        x, y = common.gcj02_to_wgs84(x, y)
    else:
        x = y = None
    return {
        'poi_id': item.get('id', ''),
        'poi_tel': item.get('tel', ''),
        'poi_business_area': item.get('businessarea', ''),
        'poi_name': item.get('name', ''),
        'poi_type': item.get('type', '').split(';')[0] if item.get('type') else '',
        'poi_distance': _sint(item.get('distance')),
        'poi_direction': item.get('direction', ''),
        'poi_address': item.get('address', ''),
        'poi_lng': x,
        'poi_lat': y,
        'poi_raw': _json(item),
    }


def reverse_geocode_baidu(lng, lat, key=None, max_retries=None, radius=1000):
    if max_retries is None:
        max_retries = len(common.BAIDU_KEYS)
    tried = set()
    for retry in range(max_retries):
        if key is None:
            key_index, current_key = common.get_next_baidu_key_info()
            if current_key is None:
                return {'ok': False, 'source': 'Baidu', 'status': 'KEY_POOL_EMPTY', 'key': 'N/A', 'error': '当前没有可用的百度 Key', 'raw_payload': None, 'formatted_address': '', 'province': '', 'city': '', 'district': '', 'street': '', 'street_number': '', 'poi': {}}
            if current_key in tried:
                continue
        else:
            current_key = key
            key_index = common.BAIDU_KEYS.index(current_key) if current_key in common.BAIDU_KEYS else None
        tried.add(current_key)

        bd_lng, bd_lat = common.wgs84_to_bd09(lng, lat)
        params = {
            'ak': current_key,
            'location': '{},{}'.format(bd_lat, bd_lng),
            'coordtype': 'bd09ll',
            'extensions_poi': '1',
            'radius': str(radius),
            'sort_strategy': 'distance',
            'output': 'json',
        }

        try:
            resp = requests.get(common.BAIDU_REVERSE_GEO_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            status = data.get('status', -1)
            msg = data.get('message', '') or data.get('msg', '')
            cls = common.classify_baidu_response_issue(status=status, message=msg, http_status=resp.status_code)
            if cls.get('switch_key') and key_index is not None:
                try:
                    common._set_baidu_key_delay(key_index, cls.get('delay_seconds', 1.0))
                except Exception:
                    pass
                if retry < max_retries - 1:
                    continue

            if str(status) == '0':
                result = data.get('result', {}) or {}
                ac = result.get('addressComponent', {}) or {}
                pois = result.get('pois', []) or []
                poi_rows = [_poi_from_baidu(item) for item in pois]
                return {
                    'ok': True,
                    'source': 'Baidu',
                    'status': str(status),
                    'key': _mask_key(current_key),
                    'formatted_address': result.get('formatted_address', ''),
                    'province': ac.get('province', ''),
                    'city': ac.get('city', ''),
                    'district': ac.get('district', ''),
                    'street': ac.get('street', ''),
                    'street_number': ac.get('street_number', ''),
                    'baidu_country': ac.get('country', ''),
                    'baidu_adcode': ac.get('adcode', ''),
                    'baidu_town': ac.get('town', ''),
                    'baidu_town_code': ac.get('town_code', ''),
                    'baidu_city_code': result.get('cityCode', ''),
                    'baidu_business': result.get('business', ''),
                    'baidu_sematic_description': result.get('sematic_description', ''),
                    'baidu_location_lng': _sfloat(result.get('location', {}).get('lng')) if isinstance(result.get('location', {}), dict) else None,
                    'baidu_location_lat': _sfloat(result.get('location', {}).get('lat')) if isinstance(result.get('location', {}), dict) else None,
                    'pois': poi_rows,
                    'raw_payload': data,
                    'error': '',
                }

            error = data.get('msg') or data.get('message') or '百度 API 返回错误'
            arcpy.AddWarning('百度逆地理编码失败：status={}, msg={}'.format(status, error))
            return {
                'ok': False,
                'source': 'Baidu',
                'status': str(status),
                'key': _mask_key(current_key),
                'error': error,
                'raw_payload': data,
                'formatted_address': '',
                'province': '',
                'city': '',
                'district': '',
                'street': '',
                'street_number': '',
                'pois': [],
            }
        except Exception as e:
            cls = common.classify_baidu_response_issue(message=str(e), error=e)
            if key_index is not None and cls.get('switch_key'):
                try:
                    common._set_baidu_key_delay(key_index, cls.get('delay_seconds', 1.0))
                except Exception:
                    pass
            arcpy.AddWarning('百度逆地理编码请求异常：{}'.format(str(e)))
            if retry < max_retries - 1:
                continue
            return {
                'ok': False,
                'source': 'Baidu',
                'status': 'ERROR',
                'key': _mask_key(current_key),
                'error': str(e),
                'raw_payload': None,
                'formatted_address': '',
                'province': '',
                'city': '',
                'district': '',
                'street': '',
                'street_number': '',
                'pois': [],
            }

    return {
        'ok': False,
        'source': 'Baidu',
        'status': 'RETRY_EXHAUSTED',
        'key': 'N/A',
        'error': '百度 Key 重试次数耗尽',
        'raw_payload': None,
        'formatted_address': '',
        'province': '',
        'city': '',
        'district': '',
        'street': '',
        'street_number': '',
        'pois': [],
    }


def reverse_geocode_amap(lng, lat, key=None, radius=1000):
    if key is None:
        key = common._get_next_amap_key()

    gcj_lng, gcj_lat = common.wgs84_to_gcj02(lng, lat)
    params = {
        'key': key,
        'location': '{},{}'.format(gcj_lng, gcj_lat),
        'extensions': 'all',
        'radius': str(radius),
        'output': 'json',
    }

    try:
        arcpy.AddMessage('高德逆地理编码请求：坐标={},{}'.format(gcj_lng, gcj_lat))
        resp = requests.get(common.AMAP_REVERSE_GEO_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data.get('status', '0')
        infocode = data.get('infocode', '')
        if status == '1' and infocode == '10000':
            regeo = data.get('regeocode', {}) or {}
            ac = regeo.get('addressComponent', {}) or {}
            pois = regeo.get('pois', []) or []
            poi_rows = [_poi_from_amap(item) for item in pois]
            street_number = ac.get('streetNumber', {}) or {}
            neighborhood = ac.get('neighborhood', {}) or {}
            building = ac.get('building', {}) or {}
            return {
                'ok': True,
                'source': 'Amap',
                'status': status,
                'key': _mask_key(key),
                'formatted_address': regeo.get('formatted_address', ''),
                'province': ac.get('province', ''),
                'city': ac.get('city', ''),
                'district': ac.get('district', ''),
                'street': street_number.get('street', '') or ac.get('street', ''),
                'street_number': street_number.get('number', '') or ac.get('number', ''),
                'amap_country': ac.get('country', ''),
                'amap_citycode': ac.get('citycode', ''),
                'amap_adcode': ac.get('adcode', ''),
                'amap_township': ac.get('township', ''),
                'amap_towncode': ac.get('towncode', ''),
                'amap_sea_area': ac.get('seaArea', ''),
                'amap_neighborhood_name': neighborhood.get('name', ''),
                'amap_neighborhood_type': neighborhood.get('type', ''),
                'amap_building_name': building.get('name', ''),
                'amap_building_type': building.get('type', ''),
                'amap_street_number_street': street_number.get('street', ''),
                'amap_street_number_number': street_number.get('number', ''),
                'amap_street_number_direction': street_number.get('direction', ''),
                'amap_street_number_distance': street_number.get('distance', ''),
                'pois': poi_rows,
                'raw_payload': data,
                'error': '',
            }

        error = data.get('info', '高德 API 返回错误')
        arcpy.AddWarning('高德逆地理编码失败：status={}, infocode={}, info={}'.format(status, infocode, error))
        return {
            'ok': False,
            'source': 'Amap',
            'status': status,
            'key': _mask_key(key),
            'error': error,
            'raw_payload': data,
            'formatted_address': '',
            'province': '',
            'city': '',
            'district': '',
            'street': '',
            'street_number': '',
            'pois': [],
        }
    except Exception as e:
        arcpy.AddWarning('高德逆地理编码请求异常：{}'.format(str(e)))
        return {
            'ok': False,
            'source': 'Amap',
            'status': 'ERROR',
            'key': _mask_key(key),
            'error': str(e),
            'raw_payload': None,
            'formatted_address': '',
            'province': '',
            'city': '',
            'district': '',
            'street': '',
            'street_number': '',
            'pois': [],
        }


def _normalize_platform(platform):
    text = _txt(platform, '').strip().lower()
    if text in ('baidu', '百度'):
        return 'baidu'
    if text in ('amap', '高德'):
        return 'amap'
    raise ValueError('不支持的平台：{}'.format(platform))


def _build_row(row_idx, lng, lat, input_coord_type, result, poi, poi_index):
    poi_lng = _sfloat(poi.get('poi_lng'))
    poi_lat = _sfloat(poi.get('poi_lat'))
    if poi_lng is None or poi_lat is None:
        poi_lng, poi_lat = lng, lat

    row = {
        'src_oid': row_idx,
        'input_lng': lng,
        'input_lat': lat,
        'input_coord_type': input_coord_type,
        'platform': result.get('source', ''),
        'status': result.get('status', ''),
        'key': result.get('key', ''),
        'formatted_address': result.get('formatted_address', ''),
        'province': result.get('province', ''),
        'city': result.get('city', ''),
        'district': result.get('district', ''),
        'street': result.get('street', ''),
        'street_number': result.get('street_number', ''),
        'poi_index': poi_index,
        'poi_id': poi.get('poi_id', ''),
        'poi_tel': poi.get('poi_tel', ''),
        'poi_business_area': poi.get('poi_business_area', ''),
        'poi_name': poi.get('poi_name', ''),
        'poi_type': poi.get('poi_type', ''),
        'poi_distance': poi.get('poi_distance'),
        'poi_direction': poi.get('poi_direction', ''),
        'poi_address': poi.get('poi_address', ''),
        'poi_lng': poi_lng,
        'poi_lat': poi_lat,
        'raw_response': _json(result.get('raw_payload')),
    }

    if result.get('source') == 'Baidu':
        row.update({
            'baidu_ok': 1 if result.get('ok') else 0,
            'baidu_message': result.get('error', ''),
            'baidu_country': result.get('baidu_country', ''),
            'baidu_adcode': result.get('baidu_adcode', ''),
            'baidu_town': result.get('baidu_town', ''),
            'baidu_town_code': result.get('baidu_town_code', ''),
            'baidu_city_code': result.get('baidu_city_code', ''),
            'baidu_business': result.get('baidu_business', ''),
            'baidu_sematic_description': result.get('baidu_sematic_description', ''),
            'baidu_location_lng': result.get('baidu_location_lng'),
            'baidu_location_lat': result.get('baidu_location_lat'),
        })
    elif result.get('source') == 'Amap':
        row.update({
            'amap_ok': 1 if result.get('ok') else 0,
            'amap_info': result.get('error', ''),
            'amap_infocode': result.get('status', ''),
            'amap_country': result.get('amap_country', ''),
            'amap_citycode': result.get('amap_citycode', ''),
            'amap_adcode': result.get('amap_adcode', ''),
            'amap_township': result.get('amap_township', ''),
            'amap_towncode': result.get('amap_towncode', ''),
            'amap_sea_area': result.get('amap_sea_area', ''),
            'amap_neighborhood_name': result.get('amap_neighborhood_name', ''),
            'amap_neighborhood_type': result.get('amap_neighborhood_type', ''),
            'amap_building_name': result.get('amap_building_name', ''),
            'amap_building_type': result.get('amap_building_type', ''),
            'amap_street_number_street': result.get('amap_street_number_street', ''),
            'amap_street_number_number': result.get('amap_street_number_number', ''),
            'amap_street_number_direction': result.get('amap_street_number_direction', ''),
            'amap_street_number_distance': result.get('amap_street_number_distance', ''),
        })

    return row


def _reverse_geocode_one(args):
    row_idx, lng, lat, input_coord_type, platform, poi_count, radius = args
    try:
        if platform == 'baidu':
            result = reverse_geocode_baidu(lng, lat, radius=radius)
        elif platform == 'amap':
            result = reverse_geocode_amap(lng, lat, radius=radius)
        else:
            raise ValueError('不支持的平台：{}'.format(platform))

        if not result or not result.get('ok'):
            err = ''
            if result:
                err = result.get('error') or result.get('msg') or result.get('status') or ''
            return {'row_idx': row_idx, 'success': False, 'records': [], 'error': err or '逆地理编码失败'}

        records = []
        for poi_index, poi in enumerate((result.get('pois', []) or [])[:poi_count], 1):
            records.append(_build_row(row_idx, lng, lat, input_coord_type, result, poi, poi_index))
        return {'row_idx': row_idx, 'success': True, 'records': records, 'error': None}
    except Exception as e:
        arcpy.AddWarning('记录 {} 处理异常：{}'.format(row_idx, str(e)))
        return {'row_idx': row_idx, 'success': False, 'records': [], 'error': str(e)}


def _create_output_feature_class(out_gdb, out_fc_name, platform):
    sr = arcpy.SpatialReference(4326)
    out_fc = os.path.join(out_gdb, out_fc_name)
    if arcpy.Exists(out_fc):
        arcpy.management.Delete(out_fc)

    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, 'POINT', spatial_reference=sr)
    fields = [
        ('src_oid', 'LONG',  '源OID'),
        ('input_lng', 'DOUBLE', '输入经度'),
        ('input_lat', 'DOUBLE', '输入纬度'),
        ('input_coord_type', 'TEXT', 32, '输入坐标系'),
        ('platform', 'TEXT', 10, '数据来源'),
        ('status', 'TEXT', 50, '状态码'),
        ('key', 'TEXT', 32, '使用Key'),
        ('formatted_address', 'TEXT', 500, '格式化地址'),
        ('province', 'TEXT', 100, '省份'),
        ('city', 'TEXT', 100, '城市'),
        ('district', 'TEXT', 100, '区县'),
        ('street', 'TEXT', 200, '街道'),
        ('street_number', 'TEXT', 100, '门牌号'),
        ('poi_index', 'SHORT', 'POI序号'),
        ('poi_id', 'TEXT', 100, 'POI ID'),
        ('poi_tel', 'TEXT', 100, 'POI电话'),
        ('poi_business_area', 'TEXT', 200, 'POI商圈'),
        ('poi_name', 'TEXT', 200, 'POI名称'),
        ('poi_type', 'TEXT', 200, 'POI类型'),
        ('poi_distance', 'LONG', 'POI距离_米'),
        ('poi_direction', 'TEXT', 50, 'POI方向'),
        ('poi_address', 'TEXT', 500, 'POI地址'),
        ('poi_lng', 'DOUBLE', 'POI经度'),
        ('poi_lat', 'DOUBLE', 'POI纬度'),
        ('raw_response', 'TEXT', TEXT_LEN, '原始响应'),
    ]

    if platform == 'baidu':
        fields.extend([
            ('baidu_ok', 'SHORT', '百度_是否成功'),
            ('baidu_message', 'TEXT', 500, '百度_错误信息'),
            ('baidu_country', 'TEXT', 100, '百度_国家'),
            ('baidu_adcode', 'TEXT', 50, '百度_行政区编码'),
            ('baidu_town', 'TEXT', 100, '百度_乡镇'),
            ('baidu_town_code', 'TEXT', 50, '百度_乡镇编码'),
            ('baidu_city_code', 'TEXT', 50, '百度_城市编码'),
            ('baidu_business', 'TEXT', 500, '百度_商圈'),
            ('baidu_sematic_description', 'TEXT', 1000, '百度_语义描述'),
            ('baidu_location_lng', 'DOUBLE', '百度_坐标经度'),
            ('baidu_location_lat', 'DOUBLE', '百度_坐标纬度'),
        ])
    elif platform == 'amap':
        fields.extend([
            ('amap_ok', 'SHORT', '高德_是否成功'),
            ('amap_info', 'TEXT', 500, '高德_错误信息'),
            ('amap_infocode', 'TEXT', 50, '高德_信息码'),
            ('amap_country', 'TEXT', 100, '高德_国家'),
            ('amap_citycode', 'TEXT', 50, '高德_城市编码'),
            ('amap_adcode', 'TEXT', 50, '高德_行政区编码'),
            ('amap_township', 'TEXT', 100, '高德_乡镇'),
            ('amap_towncode', 'TEXT', 50, '高德_乡镇编码'),
            ('amap_sea_area', 'TEXT', 200, '高德_海域'),
            ('amap_neighborhood_name', 'TEXT', 200, '高德_社区名称'),
            ('amap_neighborhood_type', 'TEXT', 200, '高德_社区类型'),
            ('amap_building_name', 'TEXT', 200, '高德_楼宇名称'),
            ('amap_building_type', 'TEXT', 200, '高德_楼宇类型'),
            ('amap_street_number_street', 'TEXT', 200, '高德_门牌街道'),
            ('amap_street_number_number', 'TEXT', 100, '高德_门牌号'),
            ('amap_street_number_direction', 'TEXT', 50, '高德_门牌方向'),
            ('amap_street_number_distance', 'TEXT', 50, '高德_门牌距离'),
        ])

    for spec in fields:
        if len(spec) == 4:
            name, field_type, length, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_length=length, field_alias=alias)
        else:
            name, field_type, alias = spec
            arcpy.management.AddField(out_fc, name, field_type, field_alias=alias)

    return out_fc, sr


def _row_value(record, name):
    short_fields = {'baidu_ok', 'amap_ok'}
    numeric_fields = {
        'src_oid', 'input_lng', 'input_lat', 'poi_index', 'poi_distance',
        'poi_lng', 'poi_lat', 'baidu_ok', 'baidu_location_lng',
        'baidu_location_lat', 'amap_ok',
    }
    value = record.get(name, None)
    if value is None:
        if name in short_fields:
            return 0
        return None if name in numeric_fields else ''
    if name in numeric_fields:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str) and value.strip() == '':
            return None
        if isinstance(value, (list, dict)):
            return None
        try:
            return float(value) if name not in ('src_oid', 'poi_index', 'baidu_ok', 'amap_ok', 'poi_distance') else int(float(value))
        except Exception:
            return None
    return _textify(value)


def run_reverse_geocode(in_table, input_coord_type='WGS84', platform='baidu',
                        radius=1000, poi_count=1, speed_level='medium',
                        out_gdb=None, out_fc_name=None, max_workers=None):
    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError('必须指定 out_fc_name')
    if not out_gdb:
        raise ValueError('必须指定 out_gdb')
    if not arcpy.Exists(in_table):
        raise ValueError('输入要素类不存在：{}'.format(in_table))

    platform = _normalize_platform(platform)

    try:
        poi_count = int(poi_count or 1)
    except Exception:
        poi_count = 1
    poi_count = max(1, min(poi_count, 10))

    if max_workers is None:
        active_key_count = len(common.BAIDU_KEYS) if platform == 'baidu' else len(common.AMAP_KEYS)
        max_workers = common.calculate_max_workers(speed_level, active_key_count, active_key_count)

    arcpy.AddMessage('速度档位：{}，最大并发线程数：{}'.format(speed_level, max_workers))
    arcpy.AddMessage('平台选择：{}'.format(platform))

    desc = arcpy.Describe(in_table)
    arcpy.AddMessage('输入坐标系：{}'.format(getattr(desc.spatialReference, 'name', 'Unknown')))
    arcpy.AddMessage('输入坐标参数：{}（将统一转换为 WGS84 后再调用逆地理编码 API）'.format(input_coord_type))

    sr4326 = arcpy.SpatialReference(4326)
    input_key = str(input_coord_type or 'WGS84').strip().upper()
    use_wgs84_cursor = ('WGS84' in input_key) or ('CGCS2000' in input_key) or ('CGCS' in input_key)
    cursor_kwargs = {'spatial_reference': sr4326} if use_wgs84_cursor else {}

    rows = []
    with arcpy.da.SearchCursor(in_table, ['OID@', 'SHAPE@'], **cursor_kwargs) as cursor:
        for oid, geom in cursor:
            if geom is None:
                continue
            pt = geom.firstPoint
            if use_wgs84_cursor:
                lng_wgs, lat_wgs = pt.X, pt.Y
            else:
                lng_wgs, lat_wgs = common.convert_coord(pt.X, pt.Y, input_coord_type, 'WGS84')
            rows.append({'oid': oid, 'lng': lng_wgs, 'lat': lat_wgs, 'input_coord_type': _txt(input_coord_type, '')})

    if not rows:
        raise ValueError('输入要素类中没有有效点')

    tasks = [(r['oid'], r['lng'], r['lat'], r['input_coord_type'], platform, poi_count, radius) for r in rows]
    all_records, ok_count, fail_count = [], 0, 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_reverse_geocode_one, task): task for task in tasks}
        for i, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result['success']:
                ok_count += 1
                all_records.extend(result.get('records', []))
            else:
                fail_count += 1
                if result.get('error'):
                    arcpy.AddWarning('记录 {} 处理失败：{}'.format(result.get('row_idx', 'N/A'), result['error']))
            if i % 100 == 0:
                arcpy.AddMessage('已处理 {}/{} 条输入记录（成功：{}，失败：{}）'.format(i, len(tasks), ok_count, fail_count))

    if not all_records:
        raise ValueError('没有生成任何逆地理编码输出记录')

    all_records.sort(key=lambda x: (x.get('src_oid', 0), x.get('poi_index', 0)))
    out_fc, out_sr = _create_output_feature_class(out_gdb, out_fc_name, platform)
    arcpy.AddMessage('已创建输出要素类和字段')
    arcpy.AddMessage('开始写入要素类...')

    insert_fields = [
        'SHAPE@', 'src_oid', 'input_lng', 'input_lat', 'input_coord_type', 'platform',
        'status', 'key', 'formatted_address', 'province', 'city', 'district',
        'street', 'street_number', 'poi_index', 'poi_id', 'poi_tel', 'poi_business_area',
        'poi_name', 'poi_type', 'poi_distance', 'poi_direction', 'poi_address',
        'poi_lng', 'poi_lat', 'raw_response',
    ]

    if platform == 'baidu':
        insert_fields.extend([
            'baidu_ok', 'baidu_message', 'baidu_country', 'baidu_adcode',
            'baidu_town', 'baidu_town_code', 'baidu_city_code', 'baidu_business',
            'baidu_sematic_description', 'baidu_location_lng', 'baidu_location_lat',
        ])
    elif platform == 'amap':
        insert_fields.extend([
            'amap_ok', 'amap_info', 'amap_infocode', 'amap_country',
            'amap_citycode', 'amap_adcode', 'amap_township', 'amap_towncode',
            'amap_sea_area', 'amap_neighborhood_name', 'amap_neighborhood_type',
            'amap_building_name', 'amap_building_type',
            'amap_street_number_street', 'amap_street_number_number',
            'amap_street_number_direction', 'amap_street_number_distance',
        ])

    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        for record in all_records:
            try:
                poi_lng = record.get('poi_lng')
                poi_lat = record.get('poi_lat')
                if poi_lng is None or poi_lat is None:
                    poi_lng = record.get('input_lng')
                    poi_lat = record.get('input_lat')
                geom = arcpy.PointGeometry(arcpy.Point(poi_lng, poi_lat), out_sr)
                row = [geom] + [_row_value(record, name) for name in insert_fields[1:]]
                cursor.insertRow(row)
            except Exception as e:
                arcpy.AddWarning('写入记录失败：{} - {}'.format(record.get('src_oid', 'N/A'), str(e)))

    arcpy.AddMessage('要素类写入完成')
    arcpy.AddMessage('输出要素类：{}'.format(out_fc))
    return out_fc
