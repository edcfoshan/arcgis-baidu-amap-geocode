# -*- coding: utf-8 -*-
"""
地理编码功能模块 - 提供地址转经纬度功能

此模块实现了双平台（百度/高德）地理编码，支持自动切换和并发处理
"""
import requests
import arcpy
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import core_common as common


_ADDRESS_MULTI_SPLIT_RE = re.compile(r'[；;\r\n]+')


def _normalize_geocode_address(address):
    """
    将地理编码输入地址尽量收敛为单条可请求地址。

    规则很保守：
    - 只切分明显的多地址分隔符（分号/换行）
    - 默认取首段，避免把一条记录里的多条地址直接拼到接口里
    """
    original = '' if address is None else str(address).strip()
    if not original:
        return '', {
            'changed': False,
            'segment_count': 0,
            'note': '',
            'original_address': '',
            'normalized_address': '',
        }

    compact = re.sub(r'[\u3000\t]+', ' ', original).strip()
    segments = [part.strip(' ，,|/') for part in _ADDRESS_MULTI_SPLIT_RE.split(compact)]
    segments = [part for part in segments if part]

    if not segments:
        return '', {
            'changed': True,
            'segment_count': 0,
            'note': '地址仅包含分隔符或空白',
            'original_address': original,
            'normalized_address': '',
        }

    normalized = segments[0]
    note = ''
    if len(segments) > 1:
        note = '检测到{}段地址，已按首段请求'.format(len(segments))

    return normalized, {
        'changed': normalized != original or len(segments) > 1,
        'segment_count': len(segments),
        'note': note,
        'original_address': original,
        'normalized_address': normalized,
    }


def _baidu_issue_label(kind):
    labels = {
        'concurrency': '并发限制',
        'quota': '配额或限流',
        'auth': '鉴权或权限',
        'service': '服务异常或超时',
        'not_found': '未找到匹配结果',
        'key_pool_exhausted': '可用 Key 耗尽',
        'api_error': 'API 返回错误',
        'request_error': '请求异常',
        'unknown': '未知错误',
    }
    return labels.get(kind, '未知错误')


def geocode_baidu_full(address, city=None, key=None, max_retries=None):
    """
    百度地理编码函数，支持自动换 Key 重试。

    参数：
    - address: 地址
    - city: 城市名（可选）
    - key: 百度key（可选，如果不提供则自动获取）
    - max_retries: 最大重试次数（默认为key数量）

    返回：
    - 包含地理编码结果的字典
    """
    if max_retries is None:
        max_retries = len(common.BAIDU_KEYS)
    use_key_pool = key is None
    if not use_key_pool:
        max_retries = max(1, max_retries)

    tried_keys = set()
    last_failure = None

    def _make_failure(status, msg, current_key, kind, reason, retry_count):
        return {
            'ok': False,
            'status': str(status) if status is not None else '',
            'msg': msg or '',
            'key': common.mask_key(current_key) if current_key else 'N/A',
            'lng_gcj': None,
            'lat_gcj': None,
            'precise': None,
            'conf': None,
            'level': None,
            'need_check': '是',
            'reason': reason or msg or '百度地理编码失败',
            'error_kind': kind or 'unknown',
            'retry_count': retry_count
        }

    for retry in range(max_retries):
        if use_key_pool:
            key_index, current_key = common.get_next_baidu_key_info()
            if current_key is None:
                return _make_failure(
                    'KEY_POOL_EMPTY',
                    '当前没有可用的百度 Key',
                    None,
                    'key_pool_exhausted',
                    '当前没有可用的百度 Key',
                    retry
                )
            if current_key in tried_keys:
                continue
        else:
            current_key = key
            key_index = common.BAIDU_KEYS.index(current_key) if current_key in common.BAIDU_KEYS else None

        tried_keys.add(current_key)

        params = {
            'address': address,
            'ak': current_key,
            'output': 'json',
            'out_coord_type': 'gcj02'
        }
        if city:
            params['city'] = city

        try:
            resp = requests.get(common.BAIDU_GEO_URL, params=params, timeout=30)
        except Exception as exc:
            classification = common.classify_baidu_response_issue(message=str(exc), error=exc)
            if key_index is not None and classification.get('switch_key'):
                try:
                    common._set_baidu_key_delay(key_index, classification.get('delay_seconds', 1))
                except Exception:
                    pass
                arcpy.AddWarning(
                    "百度 Key {} 触发{}，已切换下一个 Key".format(
                        common.mask_key(current_key), _baidu_issue_label(classification.get('kind'))
                    )
                )

            failure = _make_failure(
                'ERROR',
                str(exc),
                current_key,
                classification.get('kind', 'request_error'),
                classification.get('message') or '百度地理编码请求异常：{}'.format(str(exc)),
                retry + 1
            )
            last_failure = failure
            if classification.get('switch_key') and use_key_pool and retry < max_retries - 1:
                continue
            return failure

        http_status = resp.status_code
        try:
            result = resp.json()
        except Exception:
            result = {}

        status = result.get('status', None)
        msg = (
            result.get('message')
            or result.get('msg')
            or result.get('info')
            or resp.reason
            or resp.text
            or 'unknown'
        )

        if str(status) == '0':
            location = result.get('result', {}).get('location', {})
            lng_gcj, lat_gcj = location.get('lng'), location.get('lat')
            if lng_gcj is None or lat_gcj is None:
                return _make_failure(
                    status,
                    msg,
                    current_key,
                    'not_found',
                    '百度未找到匹配结果',
                    retry + 1
                )

            precise = result.get('result', {}).get('precise', '')
            conf = result.get('result', {}).get('confidence', '')
            level = result.get('result', {}).get('level', '')
            need_check, reason = '否', ''
            try:
                if conf not in (None, '') and float(conf) < 50:
                    need_check, reason = '是', '置信度较低 ({}<50)'.format(conf)
            except Exception:
                pass
            if level in ['城市', '省', '国家']:
                need_check, reason = '是', '匹配等级过低 ({})'.format(level)

            return {
                'ok': True,
                'status': str(status),
                'msg': msg,
                'key': common.mask_key(current_key),
                'lng_gcj': lng_gcj,
                'lat_gcj': lat_gcj,
                'precise': str(precise),
                'conf': str(conf),
                'level': level,
                'need_check': need_check,
                'reason': reason,
                'error_kind': 'ok',
                'retry_count': retry + 1
            }

        classification = common.classify_baidu_response_issue(
            status=status,
            message=msg,
            http_status=http_status
        )
        failure = _make_failure(
            status,
            msg,
            current_key,
            classification.get('kind', 'api_error'),
            classification.get('message') or '百度 API 返回错误：{}'.format(msg),
            retry + 1
        )
        last_failure = failure

        if classification.get('switch_key'):
            if key_index is not None:
                try:
                    common._set_baidu_key_delay(key_index, classification.get('delay_seconds', 1))
                except Exception:
                    pass
                arcpy.AddWarning(
                    "百度 Key {} 触发{}，已切换下一个 Key".format(
                        common.mask_key(current_key), _baidu_issue_label(classification.get('kind'))
                    )
                )

            if use_key_pool and retry < max_retries - 1:
                continue

        return failure

    return last_failure or _make_failure(
        'RETRY_EXHAUSTED',
        '所有 Key 重试失败',
        None,
        'retry_exhausted',
        '百度 API 重试次数耗尽',
        max_retries
    )


def geocode_amap_full(address, city=None, key=None):
    """
    高德地理编码函数

    参数：
    - address: 地址
    - city: 城市名（可选）
    - key: 高德key（可选，如果不提供则自动获取）

    返回：
    - 包含地理编码结果的字典
    """
    if key is None:
        key = common._get_next_amap_key()
    params = {'address': address, 'key': key, 'output': 'json'}
    if city:
        params['city'] = city
    try:
        resp = requests.get(common.AMAP_GEO_URL, params=params, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        status = result.get('status', '0')
        info = result.get('info', 'unknown')
        infocode = result.get('infocode', '')

        if status == '1' and infocode == '10000':
            geocodes = result.get('geocodes', [])
            count = len(geocodes)
            if count > 0:
                g = geocodes[0]
                location = g.get('location', '')
                lng_gcj, lat_gcj = (map(float, location.split(',')) if location else (None, None))
                address_matched = g.get('formatted_address', '')
                level = g.get('level', '')
                need_check, reason = '否', ''
                if level in ['province', 'city']:
                    need_check, reason = '是', '匹配等级过低 ({})'.format(level)
                return {
                    'ok': True,
                    'status': status,
                    'info': info,
                    'infocode': infocode,
                    'key': common.mask_key(key),
                    'count': str(count),
                    'address': address_matched,
                    'lng_gcj': lng_gcj,
                    'lat_gcj': lat_gcj,
                    'level': level,
                    'need_check': need_check,
                    'reason': reason
                }
            else:
                return {
                    'ok': False,
                    'status': status,
                    'info': info,
                    'infocode': infocode,
                    'key': common.mask_key(key),
                    'count': '0',
                    'address': None,
                    'lng_gcj': None,
                    'lat_gcj': None,
                    'level': None,
                    'need_check': '是',
                    'reason': '高德未找到匹配结果'
                }
        else:
            return {
                'ok': False,
                'status': status,
                'info': info,
                'infocode': infocode,
                'key': common.mask_key(key),
                'count': '0',
                'address': None,
                'lng_gcj': None,
                'lat_gcj': None,
                'level': None,
                'need_check': '是',
                'reason': '高德 API 返回错误：{} ({})'.format(info, infocode)
            }
    except Exception as e:
        return {
            'ok': False,
            'status': 'ERROR',
            'info': str(e),
            'infocode': 'ERROR',
            'key': common.mask_key(key),
            'count': '0',
            'address': None,
            'lng_gcj': None,
            'lat_gcj': None,
            'level': None,
            'need_check': '是',
            'reason': '高德地理编码异常：{}'.format(str(e))
        }


def geocode_address_both_platforms(address, city=None):
    """
    双平台地理编码，优先使用百度，失败时尝试高德

    参数：
    - address: 地址
    - city: 城市名（可选）

    返回：
    - 包含地理编码结果和平台信息的字典
    """
    baidu_result = geocode_baidu_full(address, city)
    amap_result = geocode_amap_full(address, city)

    if baidu_result['ok'] and baidu_result['lng_gcj'] is not None:
        return {
            'source': 'Baidu',
            'city': city,
            'lng_gcj': baidu_result['lng_gcj'],
            'lat_gcj': baidu_result['lat_gcj'],
            'baidu_result': baidu_result,
            'amap_result': amap_result
        }
    elif amap_result['ok'] and amap_result['lng_gcj'] is not None:
        return {
            'source': 'Amap',
            'city': city,
            'lng_gcj': amap_result['lng_gcj'],
            'lat_gcj': amap_result['lat_gcj'],
            'baidu_result': baidu_result,
            'amap_result': amap_result
        }
    else:
        return {
            'source': None,
            'city': city,
            'lng_gcj': None,
            'lat_gcj': None,
            'baidu_result': baidu_result,
            'amap_result': amap_result
        }


def _format_geocode_failure_warning(detail):
    platform_labels = {
        'baidu': '百度',
        'amap': '高德',
        'both': '都要'
    }
    parts = []
    if detail.get('src_oid') is not None:
        parts.append('OID {}'.format(detail.get('src_oid')))
    if detail.get('address_original'):
        parts.append('原始地址：{}'.format(detail.get('address_original')))
    elif detail.get('address'):
        parts.append('地址：{}'.format(detail.get('address')))
    if detail.get('address_request') and detail.get('address_request') != detail.get('address_original'):
        parts.append('请求地址：{}'.format(detail.get('address_request')))
    platform = detail.get('platform')
    if platform:
        parts.append('平台：{}'.format(platform_labels.get(platform, platform)))
    if detail.get('return_message'):
        parts.append('返回信息：{}'.format(detail.get('return_message')))
    if detail.get('error_kind'):
        parts.append('类型：{}'.format(detail.get('error_kind')))
    if detail.get('reason'):
        parts.append('原因：{}'.format(detail.get('reason')))
    if detail.get('address_note'):
        parts.append('地址处理：{}'.format(detail.get('address_note')))
    if detail.get('used_key'):
        parts.append('Key：{}'.format(detail.get('used_key')))
    if detail.get('retry_count') is not None:
        parts.append('重试：{}'.format(detail.get('retry_count')))
    return '；'.join(parts)


def _format_address_normalization_warning(detail):
    parts = []
    if detail.get('src_oid') is not None:
        parts.append('OID {}'.format(detail.get('src_oid')))
    if detail.get('address_original'):
        parts.append('原始地址：{}'.format(detail.get('address_original')))
    if detail.get('address_request'):
        parts.append('请求地址：{}'.format(detail.get('address_request')))
    if detail.get('address_segment_count') is not None:
        parts.append('分段数：{}'.format(detail.get('address_segment_count')))
    if detail.get('address_note'):
        parts.append('处理：{}'.format(detail.get('address_note')))
    return '；'.join(parts)


def _build_failure_detail(row_idx, src_oid, address, city, platform, reason=None, error_kind=None,
                          used_key='', retry_count=0, baidu_result=None, amap_result=None, address_note=None,
                          request_address=None, address_segment_count=0, return_message=None):
    detail = {
        'row_idx': row_idx,
        'src_oid': src_oid,
        'address': address or '',
        'address_original': address or '',
        'address_request': request_address or address or '',
        'city': city or '',
        'platform': platform,
        'error_kind': error_kind or 'unknown',
        'return_message': return_message or '',
        'reason': reason or '地理编码失败',
        'used_key': used_key or '',
        'retry_count': retry_count or 0,
        'address_note': address_note or '',
        'address_segment_count': address_segment_count or 0
    }

    if platform == 'baidu' and isinstance(baidu_result, dict):
        detail['error_kind'] = baidu_result.get('error_kind') or detail['error_kind']
        detail['reason'] = baidu_result.get('reason') or baidu_result.get('msg') or detail['reason']
        detail['return_message'] = baidu_result.get('msg') or detail['return_message']
        detail['used_key'] = baidu_result.get('key') or detail['used_key']
        detail['retry_count'] = baidu_result.get('retry_count', detail['retry_count']) or detail['retry_count']
    elif platform == 'amap' and isinstance(amap_result, dict):
        detail['error_kind'] = amap_result.get('error_kind') or detail['error_kind']
        detail['reason'] = amap_result.get('reason') or amap_result.get('info') or detail['reason']
        detail['return_message'] = amap_result.get('info') or detail['return_message']
        detail['used_key'] = amap_result.get('key') or detail['used_key']
        detail['retry_count'] = amap_result.get('retry_count', detail['retry_count']) or detail['retry_count']
    elif platform == 'both':
        baidu_reason = ''
        amap_reason = ''
        baidu_msg = ''
        amap_info = ''
        baidu_key = ''
        amap_key = ''
        baidu_retry = 0
        amap_retry = 0
        if isinstance(baidu_result, dict):
            baidu_reason = baidu_result.get('reason') or baidu_result.get('msg') or ''
            baidu_msg = baidu_result.get('msg') or ''
            baidu_key = baidu_result.get('key') or ''
            baidu_retry = baidu_result.get('retry_count') or 0
        if isinstance(amap_result, dict):
            amap_reason = amap_result.get('reason') or amap_result.get('info') or ''
            amap_info = amap_result.get('info') or ''
            amap_key = amap_result.get('key') or ''
            amap_retry = amap_result.get('retry_count') or 0

        reason_parts = []
        if baidu_reason:
            reason_parts.append('百度：{}'.format(baidu_reason))
        if amap_reason:
            reason_parts.append('高德：{}'.format(amap_reason))
        detail['error_kind'] = 'both_failed'
        detail['reason'] = '；'.join(reason_parts) if reason_parts else detail['reason']
        msg_parts = []
        if baidu_msg:
            msg_parts.append('百度：{}'.format(baidu_msg))
        if amap_info:
            msg_parts.append('高德：{}'.format(amap_info))
        detail['return_message'] = '；'.join(msg_parts) if msg_parts else detail['return_message']
        detail['used_key'] = baidu_key or amap_key or detail['used_key']
        detail['retry_count'] = max(baidu_retry, amap_retry, detail['retry_count'])

    return detail


def _geocode_one(args):
    """
    单条记录地理编码处理函数（用于多线程并发）

    参数：
    - args: 包含 (row_idx, src_oid, row_data, address_field, city_field, output_wgs84, copy_fields, platform) 的元组
           platform: 'baidu', 'amap', 或 'both'

    返回：
    - 包含处理结果的字典
    """
    row_idx, src_oid, row_data, address_field, city_field, output_wgs84, copy_fields, platform = args
    try:
        raw_address = row_data.get(address_field, '')
        address, address_meta = _normalize_geocode_address(raw_address)
        city = row_data.get(city_field, '') if city_field else None
        address_original = raw_address or ''
        address_request = address or ''
        address_note = address_meta.get('note', '')
        address_segment_count = address_meta.get('segment_count', 0)

        def _failure_payload(detail):
            for field in copy_fields:
                detail['IN_{}'.format(field)] = row_data.get(field, '')
            return {
                'row_idx': row_idx,
                'src_oid': src_oid,
                'success': False,
                'result': None,
                'error': detail['reason'],
                'error_detail': detail,
                'address_original': address_original,
                'address_request': address_request,
                'address_note': address_note,
                'address_segment_count': address_segment_count,
            }

        if not address or str(address).strip() == '':
            detail = _build_failure_detail(
                row_idx=row_idx,
                src_oid=src_oid,
                address=raw_address,
                city=city,
                platform=platform,
                reason='地址为空',
                error_kind='input_empty',
                address_note=address_note,
                request_address=address_request,
                address_segment_count=address_segment_count,
            )
            return _failure_payload(detail)

        # 根据平台选择调用不同的地理编码函数
        if platform == 'baidu':
            baidu_result = geocode_baidu_full(str(address), city)
            if not baidu_result['ok'] or baidu_result['lng_gcj'] is None:
                detail = _build_failure_detail(
                    row_idx=row_idx,
                    src_oid=src_oid,
                    address=raw_address,
                    city=city,
                    platform='baidu',
                    baidu_result=baidu_result,
                    address_note=address_note,
                    request_address=address_request,
                    address_segment_count=address_segment_count,
                    return_message=baidu_result.get('msg', ''),
                )
                return _failure_payload(detail)
            # 计算WGS84坐标用于落点
            lng_wgs, lat_wgs = common.gcj02_to_wgs84(baidu_result['lng_gcj'], baidu_result['lat_gcj'])
            # 百度模式：直接使用百度字段，不创建final_字段
            result = {
                'src_oid': src_oid,
                'address_original': address_original,
                'address_request': address_request,
                'address_note': address_note,
                'address_segment_count': address_segment_count,
                'baidu_ok': '是' if baidu_result['ok'] else '否',
                'baidu_status': baidu_result.get('status', ''),
                'baidu_msg': baidu_result.get('msg', ''),
                'baidu_key': baidu_result.get('key', ''),
                'baidu_lng_gcj': baidu_result['lng_gcj'],
                'baidu_lat_gcj': baidu_result['lat_gcj'],
                'baidu_lng_wgs84': lng_wgs,
                'baidu_lat_wgs84': lat_wgs,
                'baidu_precise': baidu_result.get('precise', ''),
                'baidu_conf': baidu_result.get('conf', ''),
                'baidu_level': baidu_result.get('level', ''),
                'baidu_need_check': baidu_result.get('need_check', ''),
                'baidu_check_reason': baidu_result.get('reason', '')
            }
        elif platform == 'amap':
            amap_result = geocode_amap_full(str(address), city)
            if not amap_result['ok'] or amap_result['lng_gcj'] is None:
                detail = _build_failure_detail(
                    row_idx=row_idx,
                    src_oid=src_oid,
                    address=raw_address,
                    city=city,
                    platform='amap',
                    amap_result=amap_result,
                    address_note=address_note,
                    request_address=address_request,
                    address_segment_count=address_segment_count,
                    return_message=amap_result.get('info', ''),
                )
                return _failure_payload(detail)
            # 计算WGS84坐标用于落点
            lng_wgs, lat_wgs = common.gcj02_to_wgs84(amap_result['lng_gcj'], amap_result['lat_gcj'])
            # 高德模式：直接使用高德字段，不创建final_字段
            result = {
                'src_oid': src_oid,
                'address_original': address_original,
                'address_request': address_request,
                'address_note': address_note,
                'address_segment_count': address_segment_count,
                'amap_ok': '是' if amap_result['ok'] else '否',
                'amap_status': amap_result.get('status', ''),
                'amap_info': amap_result.get('info', ''),
                'amap_infocode': amap_result.get('infocode', ''),
                'amap_key': amap_result.get('key', ''),
                'amap_count': amap_result.get('count', ''),
                'amap_address': amap_result.get('address', ''),
                'amap_lng_gcj': amap_result['lng_gcj'],
                'amap_lat_gcj': amap_result['lat_gcj'],
                'amap_lng_wgs84': lng_wgs,
                'amap_lat_wgs84': lat_wgs,
                'amap_level': amap_result.get('level', ''),
                'amap_need_check': amap_result.get('need_check', ''),
                'amap_check_reason': amap_result.get('reason', '')
            }
        else:  # both
            geo_result = geocode_address_both_platforms(str(address), city)
            if geo_result['lng_gcj'] is None:
                detail = _build_failure_detail(
                    row_idx=row_idx,
                    src_oid=src_oid,
                    address=raw_address,
                    city=city,
                    platform='both',
                    baidu_result=geo_result.get('baidu_result'),
                    amap_result=geo_result.get('amap_result'),
                    address_note=address_note,
                    request_address=address_request,
                    address_segment_count=address_segment_count,
                    return_message='；'.join(
                        part for part in [
                            '百度：{}'.format(geo_result.get('baidu_result', {}).get('msg', '')) if isinstance(geo_result.get('baidu_result'), dict) else '',
                            '高德：{}'.format(geo_result.get('amap_result', {}).get('info', '')) if isinstance(geo_result.get('amap_result'), dict) else '',
                        ] if part
                    ),
                )
                return _failure_payload(detail)
            lng_wgs, lat_wgs = common.gcj02_to_wgs84(geo_result['lng_gcj'], geo_result['lat_gcj'])
            # 都要模式：需要final_字段作为最终结果
            result = {
                'src_oid': src_oid,
                'address_original': address_original,
                'address_request': address_request,
                'address_note': address_note,
                'address_segment_count': address_segment_count,
                'final_source': geo_result['source'],
                'final_city': city or '',
                'final_output_wgs84': '是' if output_wgs84 else '否',
                'final_lng_gcj': geo_result['lng_gcj'],
                'final_lat_gcj': geo_result['lat_gcj'],
                'final_lng_wgs84': lng_wgs,
                'final_lat_wgs84': lat_wgs,
                'final_need_check': '否',
                'final_check_reason': ''
            }

        # 都要模式：需要额外填充百度和高德字段
        if platform == 'both':
            baidu = geo_result.get('baidu_result')
            if baidu:
                result.update({
                    'baidu_ok': '是' if baidu['ok'] else '否',
                    'baidu_status': baidu.get('status', ''),
                    'baidu_msg': baidu.get('msg', ''),
                    'baidu_key': baidu.get('key', ''),
                    'baidu_lng_gcj': baidu.get('lng_gcj'),
                    'baidu_lat_gcj': baidu.get('lat_gcj'),
                    'baidu_precise': baidu.get('precise', ''),
                    'baidu_conf': baidu.get('conf', ''),
                    'baidu_level': baidu.get('level', ''),
                    'baidu_need_check': baidu.get('need_check', ''),
                    'baidu_check_reason': baidu.get('reason', '')
                })

            amap = geo_result.get('amap_result')
            if amap:
                result.update({
                    'amap_ok': '是' if amap['ok'] else '否',
                    'amap_status': amap.get('status', ''),
                    'amap_info': amap.get('info', ''),
                    'amap_infocode': amap.get('infocode', ''),
                    'amap_key': amap.get('key', ''),
                    'amap_count': amap.get('count', ''),
                    'amap_address': amap.get('address', ''),
                    'amap_lng_gcj': amap.get('lng_gcj'),
                    'amap_lat_gcj': amap.get('lat_gcj'),
                    'amap_level': amap.get('level', ''),
                    'amap_need_check': amap.get('need_check', ''),
                    'amap_check_reason': amap.get('reason', '')
                })

        # 复制原始字段
        for field in copy_fields:
            result['IN_{}'.format(field)] = row_data.get(field, '')

        return {
            'row_idx': row_idx,
            'src_oid': src_oid,
            'success': True,
            'result': result,
            'error': None,
            'error_detail': None,
            'address_original': address_original,
            'address_request': address_request,
            'address_note': address_note,
            'address_segment_count': address_segment_count,
        }

    except Exception as e:
        detail = _build_failure_detail(
            row_idx=row_idx,
            src_oid=src_oid,
            address=row_data.get(address_field, ''),
            city=row_data.get(city_field, '') if city_field else None,
            platform=platform,
            reason='地理编码异常：{}'.format(str(e)),
            error_kind='exception',
            address_note='',
            request_address=row_data.get(address_field, '') or '',
            address_segment_count=0,
            return_message=str(e),
        )
        for field in copy_fields:
            detail['IN_{}'.format(field)] = row_data.get(field, '')
        return {
            'row_idx': row_idx,
            'src_oid': src_oid,
            'success': False,
            'result': None,
            'error': detail['reason'],
            'error_detail': detail,
            'address_original': row_data.get(address_field, '') or '',
            'address_request': row_data.get(address_field, '') or '',
            'address_note': '',
            'address_segment_count': 0,
        }


def run_address_geocode(in_table, address_field, city_field=None, output_wgs84=True,
                         out_gdb=None, out_fc_name=None, speed_level='medium', max_workers=None,
                         platform='both', return_failure_table=False):
    """
    地理编码主处理函数 - 将地址表转换为点要素类

    参数：
    - in_table: 输入表或要素类路径
    - address_field: 地址字段名
    - city_field: 城市字段名（可选）
    - output_wgs84: 是否输出WGS84坐标
    - out_gdb: 输出GDB路径或'in_memory'
    - out_fc_name: 输出要素类名称
    - speed_level: 速度档位 ('low', 'medium', 'fast', 'fastest')
    - max_workers: 最大并发线程数（可选，默认自动计算）
    - platform: 地理编码平台 ('baidu', 'amap', 'both')
    - return_failure_table: 是否同时返回失败记录表路径

    返回：
    - out_fc: 输出要素类路径
    - out_failure_table: 失败记录表路径（仅在 return_failure_table=True 时返回）
    """
    import os

    arcpy.env.overwriteOutput = True

    if not out_fc_name:
        raise ValueError('必须指定 out_fc_name')
    if not out_gdb:
        raise ValueError('必须指定 out_gdb（可以是文件GDB路径或"in_memory"）')
    if not arcpy.Exists(in_table):
        raise ValueError('输入表/要素类不存在：{}'.format(in_table))

    # 动态计算最大并发线程数
    if max_workers is None:
        max_workers = common.calculate_max_workers(
            speed_level, len(common.BAIDU_KEYS), len(common.AMAP_KEYS)
        )

    arcpy.AddMessage('速度档位：{}，最大并发线程数：{}'.format(speed_level, max_workers))

    # 读取输入数据
    all_fields = []
    for f in arcpy.ListFields(in_table):
        if f.name in ['OID', 'Shape', 'FID', 'OBJECTID']:
            continue
        all_fields.append(f.name)

    fields_to_read = ['OID@', address_field]
    if city_field:
        fields_to_read.append(city_field)
    fields_to_read.extend(all_fields)

    rows = []
    with arcpy.da.SearchCursor(in_table, fields_to_read) as cursor:
        for idx, row in enumerate(cursor):
            row_data = {'row_idx': idx, 'src_oid': row[0]}
            row_data[address_field] = row[1]
            if city_field:
                row_data[city_field] = row[2] if len(row) > 2 else None
            offset = 3 if city_field else 2
            for i, field in enumerate(all_fields):
                if offset + i < len(row):
                    row_data[field] = row[offset + i]
                else:
                    row_data[field] = ''
            rows.append(row_data)

    total = len(rows)
    arcpy.AddMessage('读取输入记录：{} 条'.format(total))

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
        sr = arcpy.SpatialReference(4326)
        arcpy.AddMessage('输出坐标系：WGS84')
    else:
        sr = arcpy.SpatialReference(4326)
        arcpy.AddMessage('输出坐标系：GCJ02（坐标值按GCJ02写出，空间参考写为WGS84）')
    arcpy.management.CreateFeatureclass(out_gdb, out_fc_name, 'POINT', spatial_reference=sr)

    # 添加字段
    arcpy.management.AddField(out_fc, 'src_oid', 'LONG', field_alias='源 OID')
    arcpy.management.AddField(out_fc, 'coord_sys', 'TEXT', field_length=10, field_alias='输出坐标系')
    arcpy.management.AddField(out_fc, 'address_original', 'TEXT', field_length=500, field_alias='原始地址')
    arcpy.management.AddField(out_fc, 'address_request', 'TEXT', field_length=500, field_alias='请求地址')
    arcpy.management.AddField(out_fc, 'address_note', 'TEXT', field_length=500, field_alias='地址处理')
    arcpy.management.AddField(out_fc, 'address_segment_count', 'LONG', field_alias='地址段数')

    copy_fields = []
    for f in arcpy.ListFields(in_table):
        if f.name in ['OID', 'Shape', 'FID', 'OBJECTID']:
            continue
        if f.type not in ['String', 'Single', 'Double', 'Integer', 'SmallInteger']:
            continue
        new_field_name = 'IN_{}'.format(f.name)
        if f.type == 'String':
            arcpy.management.AddField(out_fc, new_field_name, 'TEXT', field_length=f.length or 255)
        elif f.type == 'Double':
            arcpy.management.AddField(out_fc, new_field_name, 'DOUBLE')
        elif f.type == 'Single':
            arcpy.management.AddField(out_fc, new_field_name, 'FLOAT')
        elif f.type == 'Integer':
            arcpy.management.AddField(out_fc, new_field_name, 'LONG')
        elif f.type == 'SmallInteger':
            arcpy.management.AddField(out_fc, new_field_name, 'SHORT')
        copy_fields.append(f.name)

    # 根据平台选择添加对应的字段
    # 只有"都要"模式才需要"最终"字段，单平台模式直接使用平台字段作为结果
    if platform == 'both':
        arcpy.management.AddField(out_fc, 'final_source', 'TEXT', field_length=10, field_alias='最终_采用平台')
        arcpy.management.AddField(out_fc, 'final_city', 'TEXT', field_length=50, field_alias='最终_市级参考')
        arcpy.management.AddField(out_fc, 'final_output_wgs84', 'TEXT', field_length=1, field_alias='最终_输出 WGS84')
        arcpy.management.AddField(out_fc, 'final_lng_gcj', 'DOUBLE', field_alias='最终_经度 GCJ02')
        arcpy.management.AddField(out_fc, 'final_lat_gcj', 'DOUBLE', field_alias='最终_纬度 GCJ02')
        arcpy.management.AddField(out_fc, 'final_lng_wgs84', 'DOUBLE', field_alias='最终_经度 WGS84')
        arcpy.management.AddField(out_fc, 'final_lat_wgs84', 'DOUBLE', field_alias='最终_纬度 WGS84')
        arcpy.management.AddField(out_fc, 'final_need_check', 'TEXT', field_length=1, field_alias='最终_是否需核验')
        arcpy.management.AddField(out_fc, 'final_check_reason', 'TEXT', field_length=500, field_alias='最终_核验原因')

    # 根据平台选择添加对应的字段
    if platform in ('baidu', 'both'):
        # 百度结果字段
        arcpy.management.AddField(out_fc, 'baidu_ok', 'TEXT', field_length=1, field_alias='百度_是否成功')
        arcpy.management.AddField(out_fc, 'baidu_status', 'TEXT', field_length=50, field_alias='百度_状态码')
        arcpy.management.AddField(out_fc, 'baidu_msg', 'TEXT', field_length=500, field_alias='百度_返回信息')
        arcpy.management.AddField(out_fc, 'baidu_key', 'TEXT', field_length=32, field_alias='百度_使用 Key')
        arcpy.management.AddField(out_fc, 'baidu_lng_gcj', 'DOUBLE', field_alias='百度_经度 GCJ02')
        arcpy.management.AddField(out_fc, 'baidu_lat_gcj', 'DOUBLE', field_alias='百度_纬度 GCJ02')
        arcpy.management.AddField(out_fc, 'baidu_precise', 'TEXT', field_length=20, field_alias='百度_是否精确匹配')
        arcpy.management.AddField(out_fc, 'baidu_conf', 'TEXT', field_length=20, field_alias='百度_置信度')
        arcpy.management.AddField(out_fc, 'baidu_level', 'TEXT', field_length=50, field_alias='百度_匹配等级')
        arcpy.management.AddField(out_fc, 'baidu_need_check', 'TEXT', field_length=1, field_alias='百度_是否需核验')
        arcpy.management.AddField(out_fc, 'baidu_check_reason', 'TEXT', field_length=500, field_alias='百度_核验原因')

    if platform in ('amap', 'both'):
        # 高德结果字段
        arcpy.management.AddField(out_fc, 'amap_ok', 'TEXT', field_length=1, field_alias='高德_是否成功')
        arcpy.management.AddField(out_fc, 'amap_status', 'TEXT', field_length=50, field_alias='高德_状态码')
        arcpy.management.AddField(out_fc, 'amap_info', 'TEXT', field_length=500, field_alias='高德_返回说明')
        arcpy.management.AddField(out_fc, 'amap_infocode', 'TEXT', field_length=50, field_alias='高德_返回码')
        arcpy.management.AddField(out_fc, 'amap_key', 'TEXT', field_length=32, field_alias='高德_使用 Key')
        arcpy.management.AddField(out_fc, 'amap_count', 'TEXT', field_length=20, field_alias='高德_候选数量')
        arcpy.management.AddField(out_fc, 'amap_address', 'TEXT', field_length=500, field_alias='高德_匹配地址')
        arcpy.management.AddField(out_fc, 'amap_lng_gcj', 'DOUBLE', field_alias='高德_经度 GCJ02')
        arcpy.management.AddField(out_fc, 'amap_lat_gcj', 'DOUBLE', field_alias='高德_纬度 GCJ02')
        arcpy.management.AddField(out_fc, 'amap_level', 'TEXT', field_length=50, field_alias='高德_匹配等级')
        arcpy.management.AddField(out_fc, 'amap_need_check', 'TEXT', field_length=1, field_alias='高德_是否需核验')
        arcpy.management.AddField(out_fc, 'amap_check_reason', 'TEXT', field_length=500, field_alias='高德_核验原因')

    arcpy.AddMessage('已创建要素类和字段')

    # 并发处理地理编码
    results, failure_records = [], []
    success_count, fail_count = 0, 0
    normalized_count = 0
    normalized_records = []
    tasks = [(row['row_idx'], row['src_oid'], row, address_field, city_field, output_wgs84, copy_fields, platform) for row in rows]

    arcpy.AddMessage('开始地理编码处理...')
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_geocode_one, task): task for task in tasks}
        completed = 0
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            if result.get('address_note') and int(result.get('address_segment_count', 0) or 0) > 1:
                normalized_count += 1
                normalized_records.append({
                    'src_oid': result.get('src_oid'),
                    'address_original': result.get('address_original', ''),
                    'address_request': result.get('address_request', ''),
                    'address_note': result.get('address_note', ''),
                    'address_segment_count': result.get('address_segment_count', 0),
                })
            if result['success'] and result['result']:
                results.append(result['result'])
                success_count += 1
            else:
                fail_count += 1
                failure_detail = result.get('error_detail') or {
                    'row_idx': result.get('row_idx'),
                    'src_oid': result.get('src_oid'),
                    'address': '',
                    'platform': platform,
                    'error_kind': 'unknown',
                    'reason': result.get('error', '处理失败'),
                    'used_key': '',
                    'retry_count': 0,
                    'address_original': result.get('address_original', ''),
                    'address_request': result.get('address_request', ''),
                    'address_note': result.get('address_note', ''),
                    'address_segment_count': result.get('address_segment_count', 0),
                }
                for field in copy_fields:
                    failure_detail['IN_{}'.format(field)] = (result.get('error_detail') or {}).get('IN_{}'.format(field), '')
                failure_records.append(failure_detail)
                arcpy.AddWarning('记录失败：{}'.format(_format_geocode_failure_warning(failure_detail)))
            if completed % 100 == 0:
                arcpy.AddMessage('已处理 {}/{} 条记录（成功：{}，失败：{}）'.format(
                    completed, total, success_count, fail_count))

    arcpy.AddMessage('地理编码完成：成功 {} 条，失败 {} 条'.format(success_count, fail_count))
    if normalized_count > 0:
        arcpy.AddWarning('检测到 {} 条记录包含多段地址分隔符，已自动按首段请求接口。'.format(normalized_count))
        for detail in normalized_records:
            arcpy.AddWarning('多段地址处理：{}'.format(_format_address_normalization_warning(detail)))
    if fail_count > 0:
        arcpy.AddWarning('最终失败 {} 条，详情已在上方警告中给出（含 OID 和原因）'.format(fail_count))

    # 写入结果
    arcpy.AddMessage('开始写入要素类...')
    # 构建插入字段列表（根据平台选择）
    insert_fields = ['SHAPE@', 'src_oid', 'coord_sys', 'address_original', 'address_request', 'address_note', 'address_segment_count'] + ['IN_{}'.format(f) for f in copy_fields]

    # 只有"都要"模式才需要"最终"字段
    if platform == 'both':
        insert_fields.extend([
            'final_source', 'final_city', 'final_output_wgs84',
            'final_lng_gcj', 'final_lat_gcj', 'final_lng_wgs84', 'final_lat_wgs84',
            'final_need_check', 'final_check_reason'
        ])

    # 根据平台添加对应的字段
    if platform in ('baidu', 'both'):
        insert_fields.extend([
            'baidu_ok', 'baidu_status', 'baidu_msg', 'baidu_key',
            'baidu_lng_gcj', 'baidu_lat_gcj', 'baidu_precise', 'baidu_conf', 'baidu_level',
            'baidu_need_check', 'baidu_check_reason'
        ])

    if platform in ('amap', 'both'):
        insert_fields.extend([
            'amap_ok', 'amap_status', 'amap_info', 'amap_infocode', 'amap_key',
            'amap_count', 'amap_address', 'amap_lng_gcj', 'amap_lat_gcj', 'amap_level',
            'amap_need_check', 'amap_check_reason'
        ])

    with arcpy.da.InsertCursor(out_fc, insert_fields) as cursor:
        for result in results:
            try:
                # 根据平台和输出坐标系确定几何坐标
                if platform == 'baidu':
                    if output_wgs84:
                        lng_out = result['baidu_lng_wgs84']
                        lat_out = result['baidu_lat_wgs84']
                    else:
                        lng_out = result['baidu_lng_gcj']
                        lat_out = result['baidu_lat_gcj']
                elif platform == 'amap':
                    if output_wgs84:
                        lng_out = result['amap_lng_wgs84']
                        lat_out = result['amap_lat_wgs84']
                    else:
                        lng_out = result['amap_lng_gcj']
                        lat_out = result['amap_lat_gcj']
                else:  # both
                    if output_wgs84:
                        lng_out = result['final_lng_wgs84']
                        lat_out = result['final_lat_wgs84']
                    else:
                        lng_out = result['final_lng_gcj']
                        lat_out = result['final_lat_gcj']

                pt = arcpy.Point(lng_out, lat_out)
                geom = arcpy.PointGeometry(pt, sr)
                coord_sys = 'WGS84' if output_wgs84 else 'GCJ02'
                # 构建基础行值
                row_values = [
                    geom,
                    result['src_oid'],
                    coord_sys,
                    result.get('address_original', ''),
                    result.get('address_request', ''),
                    result.get('address_note', ''),
                    result.get('address_segment_count', 0),
                ] + [
                    result.get('IN_{}'.format(f), '') for f in copy_fields
                ]

                # 只有"都要"模式才需要"最终"字段
                if platform == 'both':
                    row_values.extend([
                        result['final_source'], result['final_city'], result['final_output_wgs84'],
                        result['final_lng_gcj'], result['final_lat_gcj'],
                        result['final_lng_wgs84'], result['final_lat_wgs84'],
                        result['final_need_check'], result['final_check_reason']
                    ])

                # 根据平台添加百度字段值
                if platform in ('baidu', 'both'):
                    row_values.extend([
                        result.get('baidu_ok', ''), result.get('baidu_status', ''),
                        result.get('baidu_msg', ''), result.get('baidu_key', ''),
                        result.get('baidu_lng_gcj'), result.get('baidu_lat_gcj'),
                        result.get('baidu_precise', ''), result.get('baidu_conf', ''),
                        result.get('baidu_level', ''), result.get('baidu_need_check', ''),
                        result.get('baidu_check_reason', '')
                    ])
                # 根据平台添加高德字段值
                if platform in ('amap', 'both'):
                    row_values.extend([
                        result.get('amap_ok', ''), result.get('amap_status', ''),
                        result.get('amap_info', ''), result.get('amap_infocode', ''),
                        result.get('amap_key', ''), result.get('amap_count', ''),
                        result.get('amap_address', ''), result.get('amap_lng_gcj'),
                        result.get('amap_lat_gcj'), result.get('amap_level', ''),
                        result.get('amap_need_check', ''), result.get('amap_check_reason', '')
                    ])
                cursor.insertRow(row_values)
            except Exception as e:
                arcpy.AddWarning('写入记录失败：{}'.format(str(e)))

    arcpy.AddMessage('要素类写入完成')
    arcpy.AddMessage('输出要素类：{}'.format(out_fc))

    out_failure_table = None
    if failure_records:
        failure_table_name = '{}_失败记录'.format(out_fc_name)
        out_failure_table = os.path.join(out_gdb, failure_table_name)
        if arcpy.Exists(out_failure_table):
            arcpy.management.Delete(out_failure_table)

        arcpy.management.CreateTable(out_gdb, failure_table_name)
        arcpy.management.AddField(out_failure_table, 'src_oid', 'LONG', field_alias='源 OID')
        arcpy.management.AddField(out_failure_table, 'row_idx', 'LONG', field_alias='输入序号')
        arcpy.management.AddField(out_failure_table, 'coord_sys', 'TEXT', field_length=10, field_alias='输出坐标系')
        arcpy.management.AddField(out_failure_table, 'address_original', 'TEXT', field_length=500, field_alias='原始地址')
        arcpy.management.AddField(out_failure_table, 'address_request', 'TEXT', field_length=500, field_alias='请求地址')
        arcpy.management.AddField(out_failure_table, 'address_note', 'TEXT', field_length=500, field_alias='地址处理')
        arcpy.management.AddField(out_failure_table, 'address_segment_count', 'LONG', field_alias='地址段数')
        arcpy.management.AddField(out_failure_table, 'city_value', 'TEXT', field_length=200, field_alias='城市')
        arcpy.management.AddField(out_failure_table, 'platform', 'TEXT', field_length=10, field_alias='平台')
        arcpy.management.AddField(out_failure_table, 'is_success', 'TEXT', field_length=1, field_alias='是否成功')
        arcpy.management.AddField(out_failure_table, 'return_msg', 'TEXT', field_length=500, field_alias='返回信息')
        arcpy.management.AddField(out_failure_table, 'error_kind', 'TEXT', field_length=50, field_alias='失败类型')
        arcpy.management.AddField(out_failure_table, 'reason', 'TEXT', field_length=500, field_alias='原因')
        arcpy.management.AddField(out_failure_table, 'used_key', 'TEXT', field_length=32, field_alias='使用 Key')
        arcpy.management.AddField(out_failure_table, 'retry_count', 'LONG', field_alias='重试次数')

        for f in arcpy.ListFields(in_table):
            if f.name in ['OID', 'Shape', 'FID', 'OBJECTID']:
                continue
            if f.type not in ['String', 'Single', 'Double', 'Integer', 'SmallInteger']:
                continue
            new_field_name = 'IN_{}'.format(f.name)
            if f.type == 'String':
                arcpy.management.AddField(out_failure_table, new_field_name, 'TEXT', field_length=f.length or 255)
            elif f.type == 'Double':
                arcpy.management.AddField(out_failure_table, new_field_name, 'DOUBLE')
            elif f.type == 'Single':
                arcpy.management.AddField(out_failure_table, new_field_name, 'FLOAT')
            elif f.type == 'Integer':
                arcpy.management.AddField(out_failure_table, new_field_name, 'LONG')
            elif f.type == 'SmallInteger':
                arcpy.management.AddField(out_failure_table, new_field_name, 'SHORT')

        arcpy.AddMessage('开始写入失败记录表...')
        failure_insert_fields = [
            'src_oid', 'row_idx', 'coord_sys', 'address_original', 'address_request',
            'address_note', 'address_segment_count', 'city_value', 'platform',
            'is_success', 'return_msg', 'error_kind', 'reason', 'used_key', 'retry_count'
        ] + ['IN_{}'.format(f) for f in copy_fields]

        coord_sys_value = 'WGS84' if output_wgs84 else 'GCJ02'
        with arcpy.da.InsertCursor(out_failure_table, failure_insert_fields) as failure_cursor:
            for failure in failure_records:
                row_values = [
                    failure.get('src_oid'),
                    failure.get('row_idx'),
                    coord_sys_value,
                    failure.get('address_original', ''),
                    failure.get('address_request', ''),
                    failure.get('address_note', ''),
                    failure.get('address_segment_count', 0),
                    failure.get('city', ''),
                    failure.get('platform', ''),
                    '否',
                    failure.get('return_message', '') or failure.get('reason', ''),
                    failure.get('error_kind', ''),
                    failure.get('reason', ''),
                    failure.get('used_key', ''),
                    failure.get('retry_count', 0),
                ] + [failure.get('IN_{}'.format(f), '') for f in copy_fields]
                failure_cursor.insertRow(row_values)

        arcpy.AddMessage('失败记录表写入完成')
        arcpy.AddMessage('输出失败记录表：{}'.format(out_failure_table))

    if return_failure_table:
        return out_fc, out_failure_table
    return out_fc
