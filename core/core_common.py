# -*- coding: utf-8 -*-
"""
核心公共模块 - 提供Key管理、坐标转换、参数处理等共享功能

此模块被 core_address_geocode.py 和 core_poi_search.py 共享使用
"""
__version__ = '2026-05-25'
import math
import os
import threading
import time
import arcpy

BAIDU_GEO_URL = 'https://api.map.baidu.com/geocoding/v3/'
AMAP_GEO_URL = 'https://restapi.amap.com/v3/geocode/geo'
AMAP_POI_POLYGON_URL = 'https://restapi.amap.com/v3/place/polygon'
# 百度地点检索 3.0 多边形区域检索
BAIDU_POI_URL = 'https://api.map.baidu.com/place/v3/polygon'

# 逆地理编码API
BAIDU_REVERSE_GEO_URL = 'https://api.map.baidu.com/reverse_geocoding/v3/'
AMAP_REVERSE_GEO_URL = 'https://restapi.amap.com/v3/geocode/regeo'

# ==================== Key 管理 ====================
# 获取工具箱根目录（core_common.py 在 core/ 子目录中）
_toolbox_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAIDU_KEYS_FILE = os.path.join(_toolbox_dir, 'config', 'baidu_keys.txt')
AMAP_KEYS_FILE = os.path.join(_toolbox_dir, 'config', 'amap_keys.txt')

# 自动创建 Key 文件时使用的模板内容
_KEY_FILE_TEMPLATES = {
    '百度': (
        '# 百度地图 API Key 配置文件\n'
        '#\n'
        '# 获取 Key：https://lbsyun.baidu.com/apiconsole/key\n'
        '# 建议申请「服务端」类型 Key，并启用以下服务：\n'
        '#   地理编码、地点检索、行政区划、逆地理编码、路线规划\n'
        '#\n'
        '# 使用方法：\n'
        '#   删除下面行首的 # 号，把 your_baidu_key 替换成你的真实 Key\n'
        '#   每行一个 Key，可配多个提高速度\n'
        '#\n'
        '#your_baidu_key_here\n'
    ),
    '高德': (
        '# 高德地图 API Key 配置文件\n'
        '#\n'
        '# 获取 Key：https://console.amap.com/dev/key/app\n'
        '# 建议申请「Web服务」类型 Key，并启用以下服务：\n'
        '#   地理编码、逆地理编码、POI搜索、行政区划、路径规划\n'
        '#\n'
        '# 使用方法：\n'
        '#   删除下面行首的 # 号，把 your_amap_key 替换成你的真实 Key\n'
        '#   每行一个 Key，可配多个提高速度\n'
        '#\n'
        '#your_amap_key_here\n'
    ),
}

def _create_key_file_from_template(file_path, platform_name):
    """Key 文件不存在时自动创建带注释的模板文件，方便用户填写"""
    template_content = _KEY_FILE_TEMPLATES.get(platform_name, "")
    try:
        config_dir = os.path.dirname(file_path)
        if not os.path.exists(config_dir):
            os.makedirs(config_dir, exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(template_content)
    except Exception:
        pass  # 创建失败不影响后续流程


def _load_keys_from_file(file_path, platform_name):
    """从文件加载 API Keys；文件不存在时自动创建模板"""
    if not os.path.exists(file_path):
        _create_key_file_from_template(file_path, platform_name)
        return [], (
            '{0} Key 未配置！\n'
            '请按以下步骤操作：\n'
            '1. 用记事本打开：{1}\n'
            '2. 删除 # 号，把示例 Key 替换成你的真实 Key\n'
            '3. 保存文件后重新运行工具\n'
            '获取{0} Key：{_url}'
        ).format(platform_name, file_path,
                 _url='https://lbsyun.baidu.com/apiconsole/key' if platform_name == '百度'
                 else 'https://console.amap.com/dev/key/app')
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        keys = [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]
        if not keys:
            return [], (
                '{0} Key 未配置！\n'
                '请按以下步骤操作：\n'
                '1. 用记事本打开：{1}\n'
                '2. 删除 # 号，把示例 Key 替换成你的真实 Key\n'
                '3. 保存文件后重新运行工具\n'
                '获取{0} Key：{_url}'
            ).format(platform_name, file_path,
                     _url='https://lbsyun.baidu.com/apiconsole/key' if platform_name == '百度'
                     else 'https://console.amap.com/dev/key/app')
        return keys, None
    except Exception as e:
        return [], 'Key 文件读取失败：{0}\n 详细错误：{1}'.format(file_path, str(e))

# 加载 Keys（不再抛出 RuntimeError，改为存储错误信息，让工具加载时不崩溃）
baidu_keys, baidu_error = _load_keys_from_file(BAIDU_KEYS_FILE, '百度')
amap_keys, amap_error = _load_keys_from_file(AMAP_KEYS_FILE, '高德')

BAIDU_KEYS = baidu_keys
AMAP_KEYS = amap_keys
BAIDU_KEYS_LOAD_ERROR = baidu_error
AMAP_KEYS_LOAD_ERROR = amap_error


def require_keys(platforms):
    """
    检查指定平台的 Key 是否已配置，未配置时通过 arcpy 报错并中止执行。

    参数：
    - platforms: 需要检查的平台列表，如 ['baidu']、['amap']、['baidu', 'amap']
    """
    errors = []
    if 'baidu' in platforms and BAIDU_KEYS_LOAD_ERROR:
        errors.append(BAIDU_KEYS_LOAD_ERROR)
    if 'amap' in platforms and AMAP_KEYS_LOAD_ERROR:
        errors.append(AMAP_KEYS_LOAD_ERROR)
    if errors:
        for err in errors:
            arcpy.AddError(err)
        raise arcpy.ExecuteError('API Key 未配置，请按提示填写后重试。')

# Key 轮换和延迟控制
_baidu_index = 0
_amap_index = 0
_key_lock = threading.Lock()

# 记录每个key的最后使用时间和延迟标记
_baidu_key_delay = {}  # {key_index: delay_until_timestamp}
_amap_key_delay = {}


def mask_key(key):
    """Key脱敏：只显示前4位和后4位，中间用两个星号填充"""
    if not key or len(key) <= 8:
        return key
    return key[:4] + '**' + key[-4:]


def _get_next_baidu_key():
    """获取下一个百度 Key（带延迟控制）"""
    global _baidu_index
    with _key_lock:
        start_index = _baidu_index
        current_time = time.time()

        for _ in range(len(BAIDU_KEYS)):
            key_index = _baidu_index % len(BAIDU_KEYS)
            _baidu_index += 1

            # 检查该key是否在延迟中
            if key_index in _baidu_key_delay:
                if current_time < _baidu_key_delay[key_index]:
                    continue
                else:
                    del _baidu_key_delay[key_index]

            return BAIDU_KEYS[key_index]

        return BAIDU_KEYS[start_index % len(BAIDU_KEYS)]


def _set_baidu_key_delay(key_index, delay_seconds=1.0):
    """设置百度key延迟使用"""
    with _key_lock:
        _baidu_key_delay[key_index] = time.time() + delay_seconds


def get_next_baidu_key_info():
    """返回下一个可用的百度 Key 信息，或 (None, None)"""
    global _baidu_index
    with _key_lock:
        if not BAIDU_KEYS:
            return None, None

        current_time = time.time()
        for _ in range(len(BAIDU_KEYS)):
            key_index = _baidu_index % len(BAIDU_KEYS)
            _baidu_index += 1

            delay_until = _baidu_key_delay.get(key_index)
            if delay_until is not None:
                if current_time < delay_until:
                    continue
                del _baidu_key_delay[key_index]

            return key_index, BAIDU_KEYS[key_index]

        return None, None


def get_baidu_key_wait_seconds():
    """返回百度 Key 还要等待的最短秒数；若已有可用 Key 则返回 0。"""
    with _key_lock:
        if not BAIDU_KEYS:
            return None

        current_time = time.time()
        wait_seconds = None
        for key_index in range(len(BAIDU_KEYS)):
            delay_until = _baidu_key_delay.get(key_index)
            if delay_until is None:
                return 0
            remaining = delay_until - current_time
            if remaining <= 0:
                return 0
            if wait_seconds is None or remaining < wait_seconds:
                wait_seconds = remaining

        return wait_seconds


def _get_next_amap_key():
    """获取下一个高德 Key（带延迟控制）"""
    global _amap_index
    with _key_lock:
        start_index = _amap_index
        current_time = time.time()

        for _ in range(len(AMAP_KEYS)):
            key_index = _amap_index % len(AMAP_KEYS)
            _amap_index += 1

            # 检查该key是否在延迟中
            if key_index in _amap_key_delay:
                if current_time < _amap_key_delay[key_index]:
                    continue
                else:
                    del _amap_key_delay[key_index]

            return AMAP_KEYS[key_index]

        return AMAP_KEYS[start_index % len(AMAP_KEYS)]


def _set_amap_key_delay(key_index, delay_seconds=1.0):
    """设置高德key延迟使用"""
    with _key_lock:
        _amap_key_delay[key_index] = time.time() + delay_seconds


def set_key_delay(platform, key_index, delay_seconds=1.0):
    """
    设置指定平台key的延迟

    参数：
    - platform: 'baidu' 或 'amap'
    - key_index: key的索引
    - delay_seconds: 延迟秒数
    """
    if platform == 'baidu':
        _set_baidu_key_delay(key_index, delay_seconds)
    elif platform == 'amap':
        _set_amap_key_delay(key_index, delay_seconds)


# ==================== 参数处理工具 ====================
def _format_baidu_error_message(status=None, message=None, http_status=None):
    parts = []
    if message not in (None, ''):
        parts.append(str(message))
    if status not in (None, ''):
        parts.append('status={}'.format(status))
    if http_status not in (None, ''):
        parts.append('http={}'.format(http_status))
    return '；'.join(parts) if parts else '百度 API 返回错误'


def classify_baidu_response_issue(status=None, message=None, http_status=None, error=None):
    """识别百度响应是否应切换 Key。"""
    text = '{} {} {} {}'.format(status or '', http_status or '', message or '', error or '').lower()
    try:
        http_code = int(http_status) if http_status not in (None, '') else None
    except Exception:
        http_code = None

    if str(status) == '0':
        return {
            'kind': 'ok',
            'switch_key': False,
            'delay_seconds': 0,
            'message': ''
        }

    concurrency_hints = (
        '并发量已经超过约定并发配额',
        '并发配额',
        '并发限制',
        'concurrent',
        'too many concurrent',
    )
    quota_hints = (
        '配额',
        '额度',
        '超限',
        '超出',
        '超过',
        '限流',
        '频繁',
        '过量',
        'daily',
        'quota',
        'over limit',
        'too frequent',
        'too many requests',
        'rate limit',
        'access too frequent',
        'day limit',
        'request limit',
    )
    auth_hints = (
        '无效',
        'invalid',
        '签名',
        'scode',
        'domain',
        'ip',
        '权限',
        '授权',
        '未开通',
        '鉴权',
        '认证',
        'app服务被禁用',
        'app 服务被禁用',
        '服务被禁用',
        'app disabled',
        'app service disabled',
        'service disabled',
        '未开通app服务',
        'app未开通',
        'forbidden',
        'unauthorized',
        'denied',
    )
    service_hints = (
        '服务不可用',
        '服务被禁用',
        'app服务被禁用',
        'app 服务被禁用',
        'service unavailable',
        'service',
        '系统繁忙',
        'busy',
        'maintenance',
        'timeout',
        '超时',
        '连接失败',
        'connection',
        'network',
        'socket',
        '502',
        '503',
        '504',
    )
    not_found_hints = (
        '未找到',
        '没有找到',
        '无结果',
        '无匹配',
        '找不到',
        'not found',
        'no result',
        'empty result',
        '无法解析',
        '地址不存在',
    )

    if any(hint in text for hint in concurrency_hints):
        return {
            'kind': 'concurrency',
            'switch_key': True,
            'delay_seconds': 5,
            'message': '百度并发限制'
        }

    if any(hint in text for hint in quota_hints) or http_code == 429:
        return {
            'kind': 'quota',
            'switch_key': True,
            'delay_seconds': 3600,
            'message': '百度配额或限流限制'
        }

    if any(hint in text for hint in auth_hints) or http_code in (401, 403):
        return {
            'kind': 'auth',
            'switch_key': True,
            'delay_seconds': 3600,
            'message': '百度 Key 鉴权失败或权限不足'
        }

    if any(hint in text for hint in service_hints) or http_code in (500, 502, 503, 504):
        return {
            'kind': 'service',
            'switch_key': True,
            'delay_seconds': 30,
            'message': '百度服务异常或请求超时'
        }

    request_param_hints = (
        'request parameter error',
        'parameter error',
        'param error',
        'invalid parameter',
        'address length too long',
        '地址长度过长',
        '地址过长',
        '请求参数错误',
        '参数错误',
    )
    if any(hint in text for hint in request_param_hints) or http_code in (400, 414):
        return {
            'kind': 'request_error',
            'switch_key': False,
            'delay_seconds': 0,
            'message': '百度请求参数错误（地址过长或参数格式不正确）'
        }

    if any(hint in text for hint in not_found_hints):
        return {
            'kind': 'not_found',
            'switch_key': False,
            'delay_seconds': 0,
            'message': '百度未找到匹配结果'
        }

    return {
        'kind': 'api_error',
        'switch_key': True,
        'delay_seconds': 30,
        'message': '百度 API 返回异常，已切换到下一个 Key'
    }


def get_next_amap_key_info():
    """鑾峰彇涓嬩竴涓彲鐢ㄧ殑楂樺痉 Key 鍙婂叾绱㈠紩銆?

    杩斿洖锛?
    - (key_index, key): 鍙敤鏃惰繑鍥炵储寮曞拰 Key
    - (None, None): 褰撳墠娌℃湁鍙敤 Key锛堝凡鍏ㄩ儴澶勪簬寤惰繜鎴栬鏆傚仠锛?
    """
    global _amap_index
    with _key_lock:
        if not AMAP_KEYS:
            return None, None

        current_time = time.time()
        for _ in range(len(AMAP_KEYS)):
            key_index = _amap_index % len(AMAP_KEYS)
            _amap_index += 1

            delay_until = _amap_key_delay.get(key_index)
            if delay_until is not None:
                if current_time < delay_until:
                    continue
                del _amap_key_delay[key_index]

            return key_index, AMAP_KEYS[key_index]

        return None, None


def classify_amap_response_issue(status=None, info=None, infocode=None):
    """鎸夐珮寰?API 杩斿洖鍐呭锛屽垽鏂槸鍚﹂渶瑕佹崲 Key 鎴栧凡瑙﹂《銆?

    杩斿洖瀛楀吀锛?
    - kind: ok / quota / auth / service / api_error / unknown
    - switch_key: 褰撳墠 Key鏄惁搴旇鍒囨崲鎴栧欢杩熷悗閲嶈瘯
    - delay_seconds: 濡傛灉闇€瑕佸欢杩燂紝寤惰繜鏃堕暱
    - message: 澶勭悊鐢ㄧ殑绠€瑕佽鏄?
    """
    text = '{} {} {}'.format(status or '', infocode or '', info or '').lower()

    if str(status) == '1' or str(infocode) == '10000':
        return {
            'kind': 'ok',
            'switch_key': False,
            'delay_seconds': 0,
            'message': ''
        }

    quota_hints = (
        '配额', '额度', '超限', '超出', '超过', '限流', '频繁', '过量',
        'daily', 'quota', 'over limit', 'too frequent', 'access too frequent',
        'day limit', 'request limit'
    )
    auth_hints = (
        '无效', 'invalid', '签名', 'scode', 'domain', 'ip', '权限', '未开通',
        '鉴权', '认证', 'ak'
    )
    service_hints = (
        '服务不可用', 'service unavailable', 'service', '系统繁忙', 'busy',
        'maintenance', 'timeout', '超时', '请求过于频繁'
    )

    if any(hint in text for hint in quota_hints):
        return {
            'kind': 'quota',
            'switch_key': True,
            'delay_seconds': 3600,
            'message': '高德配额已用完或访问过于频繁'
        }

    if any(hint in text for hint in auth_hints):
        return {
            'kind': 'auth',
            'switch_key': True,
            'delay_seconds': 3600,
            'message': '高德 Key 无效或权限不足'
        }

    if any(hint in text for hint in service_hints):
        return {
            'kind': 'service',
            'switch_key': False,
            'delay_seconds': 60,
            'message': '高德服务不可用或请求超时'
        }

    return {
        'kind': 'api_error',
        'switch_key': False,
        'delay_seconds': 0,
        'message': _format_amap_error_message(status, info, infocode)
    }


def _format_amap_error_message(status=None, info=None, infocode=None):
    parts = []
    if info not in (None, ''):
        parts.append(str(info))
    if infocode not in (None, ''):
        parts.append('infocode={}'.format(infocode))
    if status not in (None, ''):
        parts.append('status={}'.format(status))
    return '；'.join(parts) if parts else '高德 API 返回错误'


def speed_level_to_code(level):
    """
    将显示名称转换为内部代码

    参数：
    - level: 速度档位显示名称，如 "中（平衡）"

    返回：
    - 内部代码，如 "medium"
    """
    mapping = {
        "低（稳定）": "low",
        "中（平衡）": "medium",
        "快（高效）": "fast",
        "最快（极限）": "fastest"
    }
    return mapping.get(level, "low")


def calculate_max_workers(speed_level, num_baidu_keys, num_amap_keys, max_qps_per_key=30):
    """
    根据速度档位和key数量动态计算最大并发线程数

    参数：
    - speed_level: 速度档位（'low', 'medium', 'fast', 'fastest'）
    - num_baidu_keys: 百度key数量
    - num_amap_keys: 高德key数量
    - max_qps_per_key: 单个key最大并发量（默认30 QPS）

    返回：
    - max_workers: 最大并发线程数
    """
    # 计算理论最大并发量（取百度和高德的最小值）
    max_baidu_qps = num_baidu_keys * max_qps_per_key
    max_amap_qps = num_amap_keys * max_qps_per_key
    max_total_qps = min(max_baidu_qps, max_amap_qps)

    # 根据速度档位计算目标并发量
    speed_multipliers = {
        'low': 0.25,      # 25%
        'medium': 0.50,   # 50%
        'fast': 0.75,     # 75%
        'fastest': 1.00   # 100%
    }

    if speed_level not in speed_multipliers:
        speed_level = 'low'

    target_qps = max_total_qps * speed_multipliers[speed_level]

    # 计算最大线程数（向上取整）
    max_workers = math.ceil(target_qps / max_qps_per_key)

    # 确保至少有1个线程
    max_workers = max(1, max_workers)

    return max_workers


def get_output_path(use_in_memory, out_gdb, out_fc_name):
    """
    统一处理输出路径逻辑

    参数：
    - use_in_memory: 是否使用内存工作空间
    - out_gdb: 输出 GDB 路径
    - out_fc_name: 输出要素类名称

    返回：
    - (out_gdb, out_fc_name) 元组

    异常：
    - 如果非内存输出且 GDB 路径无效，抛出 ValueError
    """
    if use_in_memory:
        return "in_memory", out_fc_name
    if not out_gdb or not out_gdb.lower().endswith(".gdb"):
        raise ValueError("请选择有效的 FileGDB 路径（以 .gdb 结尾的目录），或勾选'使用临时空间'")
    return out_gdb, out_fc_name


# ==================== 坐标转换统一入口 ====================
# 实际实现统一放到 coord_transform.py，避免三套工具分别维护转换公式。
from .coord_transform import (
    convert_coord,
    gcj02_to_wgs84,
    wgs84_to_gcj02,
    gcj02_to_bd09,
    bd09_to_gcj02,
    wgs84_to_bd09,
    bd09_to_wgs84,
)
