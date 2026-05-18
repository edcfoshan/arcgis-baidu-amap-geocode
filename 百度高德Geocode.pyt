# -*- coding: utf-8 -*-
import os
import datetime
import importlib
import sys
import webbrowser
import arcpy
import json
import re

# 导入核心模块
from core import core_common as common
from core import core_address_geocode as address_geocode
from core import core_reverse_geocode as reverse_geocode
from core import core_poi_search as poi_search
from core import core_coord_transform as coord_transform_tool
from core import core_admin_area as admin_area_tool


AMAP_POI_DOC_URL = "https://lbs.amap.com/api/webservice/guide/api-advanced/newpoisearch"
AMAP_POI_CATEGORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "config",
    "amap_poi_categories.json"
)

_AMAP_POI_CATEGORY_CACHE = None


def _reload_core_modules():
    """强制刷新 core 包里的模块，避免 ArcGIS 重复用旧缓存。"""
    global common, address_geocode, reverse_geocode, poi_search, coord_transform_tool
    global admin_area_tool
    importlib.invalidate_caches()

    def _safe_reload(module):
        module_name = getattr(module, "__name__", "")
        if not module_name:
            return module
        try:
            return importlib.reload(module)
        except Exception:
            try:
                sys.modules.pop(module_name, None)
                return importlib.import_module(module_name)
            except Exception as exc:
                try:
                    arcpy.AddWarning("模块重载失败，继续使用当前模块：{} ({})".format(module_name, exc))
                except Exception:
                    pass
                return module

    common = _safe_reload(common)
    address_geocode = _safe_reload(address_geocode)
    reverse_geocode = _safe_reload(reverse_geocode)
    poi_search = _safe_reload(poi_search)
    coord_transform_tool = _safe_reload(coord_transform_tool)
    admin_area_tool = _safe_reload(admin_area_tool)


_reload_core_modules()

def _get_project_default_geodatabase():
    """返回当前工程的默认地理数据库路径。"""
    try:
        aprx = arcpy.mp.ArcGISProject("CURRENT")
        default_gdb = getattr(aprx, "defaultGeodatabase", None)
        if default_gdb:
            return default_gdb
    except Exception:
        pass
    return ""


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


def _extent_to_wgs84(extent, source_sr=None):
    """将任意输入范围尽量统一成 WGS84。"""
    if extent is None:
        return extent

    if _extent_looks_geographic(extent):
        return extent

    sr = source_sr or getattr(extent, 'spatialReference', None)
    if not sr:
        return extent

    try:
        if getattr(sr, 'factoryCode', None) == 4326:
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
            pt_geom = arcpy.PointGeometry(arcpy.Point(x, y), sr)
            pt_wgs = pt_geom.projectAs(target_sr)
            lon_list.append(pt_wgs.firstPoint.X)
            lat_list.append(pt_wgs.firstPoint.Y)
        return arcpy.Extent(min(lon_list), min(lat_list), max(lon_list), max(lat_list))
    except Exception as e:
        arcpy.AddWarning("范围坐标系转换为 WGS84 失败，继续按原始范围处理：{}".format(str(e)))
        return extent


def _load_amap_poi_category_cache():
    """加载高德 POI 分类码表缓存。"""
    global _AMAP_POI_CATEGORY_CACHE

    if _AMAP_POI_CATEGORY_CACHE is not None:
        return _AMAP_POI_CATEGORY_CACHE

    cache = {
        "records": [],
        "big_choices": [],
        "big_to_records": {},
        "display_to_code": {},
    }

    try:
        with open(AMAP_POI_CATEGORY_FILE, "r", encoding="utf-8") as f:
            raw_records = json.load(f)

        big_seen = set()
        for item in raw_records:
            code = str(item.get("code", "")).strip()
            title = str(item.get("title", "")).strip()
            big = str(item.get("big", "")).strip()
            sub = str(item.get("sub", "")).strip()
            if not code:
                continue

            short_name = sub or (title.split(" / ")[-1].strip() if title else "") or big or code
            display = "{} {}".format(code, short_name).strip()
            full_display = item.get("display", "").strip() if isinstance(item.get("display", ""), str) else ""

            record = {
                "code": code,
                "display": display,
                "full_display": full_display,
                "title": title,
                "big": big,
                "sub": sub,
            }
            cache["records"].append(record)
            cache["display_to_code"][display] = code
            if full_display:
                cache["display_to_code"][full_display] = code
            cache["display_to_code"][code] = code

            if code.endswith("0000") and code not in big_seen:
                big_seen.add(code)
                big_display = "{} {}".format(code, big or title or code)
                cache["big_choices"].append((big_display, code))

            prefix = code[:2] if len(code) >= 2 else code
            cache["big_to_records"].setdefault(prefix, []).append(record)

        cache["big_choices"].sort(key=lambda item: item[1])
    except Exception as exc:
        try:
            arcpy.AddWarning("加载高德 POI 分类码表失败，将回退为自由输入：{}".format(str(exc)))
        except Exception:
            pass

    _AMAP_POI_CATEGORY_CACHE = cache
    return cache


def _split_arcgis_multi_value(value_text):
    """拆分 ArcPy 多值参数。"""
    if not value_text:
        return []
    if isinstance(value_text, (list, tuple)):
        return [str(item).strip() for item in value_text if str(item).strip()]
    return [item.strip() for item in str(value_text).split(";") if item.strip()]


def _resolve_amap_poi_typecode_list(value_text):
    """
    把界面上的“编码 + 中文名”或历史输入，解析为高德接口需要的 typecode 串。
    """
    values = _split_arcgis_multi_value(value_text)
    if not values:
        return []

    cache = _load_amap_poi_category_cache()
    display_to_code = cache.get("display_to_code", {})
    resolved = []
    seen = set()

    for item in values:
        code = display_to_code.get(item)
        if not code:
            match = re.match(r"^\d{6,}", item)
            if match:
                code = match.group(0)
            else:
                code = item

        code = str(code).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        resolved.append(code)

    return resolved


def _load_amap_poi_subcategory_choices(big_value_text):
    """按所选大类加载子类列表。"""
    cache = _load_amap_poi_category_cache()
    big_codes = _resolve_amap_poi_typecode_list(big_value_text)
    if not big_codes:
        return []

    big_code = big_codes[0]
    prefix = big_code[:2] if len(big_code) >= 2 else big_code
    records = cache.get("big_to_records", {}).get(prefix, [])
    return [record["display"] for record in records]


class GeocodeToWgs84Points(object):
    """D_地址地理编码_坐标输出"""
    def __init__(self):
        self.label = "D_地理编码_地名转点坐标"
        self.description = "输入地址表/图层，按地址字段进行百度/高德地理编码；可输出 WGS84 或 GCJ02，GCJ02 模式下坐标值保持 GCJ02，但空间参考仍写为 WGS84。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="输入表/图层（地址记录）",
            name="in_table",
            datatype=["GPFeatureLayer", "GPTableView"],
            parameterType="Required",
            direction="Input"
        )
        p0.value = None

        p1 = arcpy.Parameter(
            displayName="地址字段（文本地址）",
            name="address_field",
            datatype="Field",
            parameterType="Required",
            direction="Input"
        )
        p1.parameterDependencies = [p0.name]

        p_platform = arcpy.Parameter(
            displayName="地理编码平台（高德 / 百度 / 都要）",
            name="platform",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p_platform.filter.type = "ValueList"
        p_platform.filter.list = ["高德", "百度", "都要"]
        p_platform.value = "高德"

        p2 = arcpy.Parameter(
            displayName="市级参考字段（可选，建议填城市名）",
            name="city_field",
            datatype="Field",
            parameterType="Optional",
            direction="Input"
        )
        p2.parameterDependencies = [p0.name]

        p3 = arcpy.Parameter(
            displayName="处理速度（越快并发越高）",
            name="speed_level",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p3.filter.type = "ValueList"
        p3.filter.list = ["低（稳定）", "中（平衡）"]
        p3.value = "低（稳定）"

        p4 = arcpy.Parameter(
            displayName="输出 WGS84 坐标（不勾选则输出 GCJ-02）",
            name="output_wgs84",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p4.value = True

        p5 = arcpy.Parameter(
            displayName="使用临时空间（in_memory）",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p5.value = False

        p6 = arcpy.Parameter(
            displayName="输出 GDB（.gdb）",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p6.value = _get_project_default_geodatabase()

        p7 = arcpy.Parameter(
            displayName="输出要素类名称（点）",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p7.value = "点要素_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p8 = arcpy.Parameter(
            displayName="输出要素类",
            name="out_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        p9 = arcpy.Parameter(
            displayName="输出失败记录表",
            name="out_failure_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p_platform, p2, p3, p4, p5, p6, p7, p8, p9]

    def isLicensed(self):
        return True

    def updateMessages(self, parameters):
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText

        if not use_in_memory:
            if not out_gdb:
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录），或勾选'使用临时空间'。")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录）。")

        in_table = parameters[0].valueAsText
        addr_field = parameters[1].valueAsText
        if in_table and addr_field:
            try:
                fns = [f.name for f in arcpy.ListFields(in_table)]
                if addr_field not in fns:
                    parameters[1].setErrorMessage("地址字段不存在：{0}".format(addr_field))
            except Exception:
                pass

    def execute(self, parameters, messages):
        _reload_core_modules()
        # 检查 Key 是否已配置
        platform_text = parameters[2].valueAsText or "高德"
        platform_map = {"百度": ["baidu"], "高德": ["amap"], "都要": ["baidu", "amap"]}
        common.require_keys(platform_map.get(platform_text, ["amap"]))

        in_table = parameters[0].valueAsText or ""
        address_field = parameters[1].valueAsText or ""
        city_field = parameters[3].valueAsText or ""
        speed_level_text = parameters[4].valueAsText or "低（稳定）"
        output_wgs84 = bool(parameters[5].value)
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText or ""
        out_fc_name = parameters[8].valueAsText or ""

        # 转换平台选择为内部代码
        platform_map = {"百度": "baidu", "高德": "amap", "都要": "both"}
        platform = platform_map.get(platform_text, "amap")

        speed_level = common.speed_level_to_code(speed_level_text)
        out_gdb, out_fc_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)

        if use_in_memory:
            arcpy.AddMessage("使用临时空间（内存工作空间）")

        arcpy.AddMessage("地理编码平台：{}".format(platform_text))
        arcpy.AddMessage("处理速度：{}".format(speed_level_text))
        if output_wgs84:
            arcpy.AddMessage("输出坐标系：WGS84")
        else:
            arcpy.AddMessage("输出坐标系：GCJ02（坐标值保持GCJ02，空间参考写为WGS84）")

        out_fc, out_failure_table = address_geocode.run_address_geocode(
            in_table=in_table,
            address_field=address_field,
            city_field=city_field,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            speed_level=speed_level,
            platform=platform,
            return_failure_table=True
        )

        if out_fc:
            parameters[9].value = out_fc
            try:
                arcpy.SetParameterAsText(9, out_fc)
            except Exception:
                pass
            arcpy.AddMessage("地理编码完成！")
        else:
            arcpy.AddWarning("未找到任何地理编码结果。")

        if out_failure_table:
            parameters[10].value = out_failure_table
            try:
                arcpy.SetParameterAsText(10, out_failure_table)
            except Exception:
                pass
            arcpy.AddMessage("失败记录表：{}".format(out_failure_table))



def _resolve_poi_extent(extent_type, in_polygon, manual_extent):
    """D_地址地理编码_坐标输出"""
    source_sr = None

    if extent_type == "面图层范围":
        if not in_polygon:
            raise ValueError("选择“面图层范围”时必须指定输入面图层。")
        desc = arcpy.Describe(in_polygon)
        source_sr = getattr(desc, 'spatialReference', None)

        resolved_extent = None
        feature_count = 0
        try:
            with arcpy.da.SearchCursor(in_polygon, ['SHAPE@']) as cursor:
                for (geom,) in cursor:
                    if geom is None:
                        continue
                    geom_extent = getattr(geom, 'extent', None)
                    if geom_extent is None:
                        continue

                    feature_count += 1
                    if resolved_extent is None:
                        resolved_extent = arcpy.Extent(
                            geom_extent.XMin, geom_extent.YMin,
                            geom_extent.XMax, geom_extent.YMax
                        )
                    else:
                        resolved_extent = arcpy.Extent(
                            min(resolved_extent.XMin, geom_extent.XMin),
                            min(resolved_extent.YMin, geom_extent.YMin),
                            max(resolved_extent.XMax, geom_extent.XMax),
                            max(resolved_extent.YMax, geom_extent.YMax)
                        )
        except Exception as e:
            arcpy.AddWarning("读取面图层几何范围失败，改用图层范围：{}".format(str(e)))
            resolved_extent = getattr(desc, 'extent', None)

        if resolved_extent is None or resolved_extent.XMin is None:
            raise ValueError("面图层中没有可用于计算范围的有效要素。")

        manual_extent = resolved_extent
        arcpy.AddMessage("使用面图层范围（{} 个要素）：XMin={}, YMin={}, XMax={}, YMax={}".format(
            feature_count, manual_extent.XMin, manual_extent.YMin, manual_extent.XMax, manual_extent.YMax))
        if feature_count > 1:
            arcpy.AddWarning("面图层包含 {} 个要素，当前按全部要素的外包络搜索；如果只想采集一个村庄，请先选择目标要素或添加定义查询。".format(
                feature_count
            ))
    elif extent_type == "屏幕范围（当前视图）":
        try:
            aprx = arcpy.mp.ArcGISProject("CURRENT")
            map_view = aprx.activeMap
            if not map_view:
                raise ValueError("无法获取当前地图视图范围。")
            manual_extent = map_view.defaultCamera.getExtent()
            source_sr = getattr(map_view, 'spatialReference', None)
            arcpy.AddMessage("使用屏幕范围：XMin={}, YMin={}, XMax={}, YMax={}".format(
                manual_extent.XMin, manual_extent.YMin, manual_extent.XMax, manual_extent.YMax))
        except Exception as e:
            arcpy.AddWarning("获取屏幕范围失败：{}".format(str(e)))
            raise
    else:
        if not manual_extent:
            raise ValueError("选择“手动输入范围”时必须指定处理范围。")
        source_sr = getattr(manual_extent, 'spatialReference', None)
        arcpy.AddMessage("使用手动输入范围：XMin={}, YMin={}, XMax={}, YMax={}".format(
            manual_extent.XMin, manual_extent.YMin, manual_extent.XMax, manual_extent.YMax))

    if not manual_extent or manual_extent.XMin is None:
        raise ValueError("无效的范围，请检查输入。")

    return manual_extent, source_sr


class ReverseGeocodeToPoi(object):
    """E_逆地理编码_坐标转POI"""

    def __init__(self):
        self.label = "E_逆地理编码_坐标信息转最近的点信息"
        self.description = "输入点为主体，输出其附近 POI 列表，每个 POI 一行；仅保留百度/高德单平台模式。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="输入点图层/要素类",
            name="in_points",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )
        p0.filter.list = ["Point"]

        p1 = arcpy.Parameter(
            displayName="输入坐标系",
            name="input_coord_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p1.filter.type = "ValueList"
        p1.filter.list = coord_transform_tool.get_coord_system_choices()
        p1.value = "WGS84"

        p2 = arcpy.Parameter(
            displayName="逆地理编码平台",
            name="platform",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p2.filter.type = "ValueList"
        p2.filter.list = ["高德", "百度"]
        p2.value = "高德"

        p3 = arcpy.Parameter(
            displayName="搜索半径（米）",
            name="radius",
            datatype="GPLong",
            parameterType="Required",
            direction="Input"
        )
        p3.value = 1000

        p4 = arcpy.Parameter(
            displayName="POI 数量（每点最多 10 个）",
            name="poi_count",
            datatype="GPLong",
            parameterType="Required",
            direction="Input"
        )
        p4.value = 1

        p5 = arcpy.Parameter(
            displayName="处理速度",
            name="speed_level",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p5.filter.type = "ValueList"
        p5.filter.list = ["低（稳定）", "中（平衡）"]
        p5.value = "低（稳定）"

        p6 = arcpy.Parameter(
            displayName="使用临时空间（in_memory）",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p6.value = False

        p7 = arcpy.Parameter(
            displayName="输出 GDB（.gdb）",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p7.value = _get_project_default_geodatabase()

        p8 = arcpy.Parameter(
            displayName="输出要素类名称（点）",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p8.value = "逆地理编码_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p9 = arcpy.Parameter(
            displayName="输出要素类",
            name="out_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        use_in_memory = bool(parameters[6].value)
        parameters[7].enabled = not use_in_memory
        if use_in_memory:
            parameters[7].value = None
        return parameters

    def updateMessages(self, parameters):
        in_points = parameters[0].valueAsText
        if in_points:
            try:
                desc = arcpy.Describe(in_points)
                if getattr(desc, "shapeType", "").lower() != "point":
                    parameters[0].setErrorMessage("输入图层必须是点要素。")
            except Exception:
                pass

        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText

        if not use_in_memory:
            if not out_gdb:
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录），或勾选'使用临时空间'。")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录）。")

        radius = parameters[3].value
        poi_count = parameters[4].value
        if radius is not None and float(radius) <= 0:
            parameters[3].setErrorMessage("搜索半径必须大于 0。")
        if poi_count is not None:
            poi_count_int = int(poi_count)
            if poi_count_int <= 0:
                parameters[4].setErrorMessage("POI 数量必须大于 0。")
            elif poi_count_int > 10:
                parameters[4].setErrorMessage("POI 数量最多只能设置为 10。")

    def execute(self, parameters, messages):
        _reload_core_modules()
        # 检查 Key 是否已配置
        platform_text = parameters[2].valueAsText or "高德"
        common.require_keys(["baidu" if platform_text == "百度" else "amap"])

        in_points = parameters[0].valueAsText or ""
        input_coord_type = parameters[1].valueAsText or "WGS84"
        radius = int(parameters[3].value) if parameters[3].value else 1000
        poi_count = int(parameters[4].value) if parameters[4].value else 1
        poi_count = max(1, min(poi_count, 10))
        speed_level_text = parameters[5].valueAsText or "低（稳定）"
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText or ""
        out_fc_name = parameters[8].valueAsText or ""

        platform_map = {"高德": "amap", "百度": "baidu"}
        platform = platform_map.get(platform_text, "amap")
        speed_level = common.speed_level_to_code(speed_level_text)
        out_gdb, out_fc_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)

        if use_in_memory:
            arcpy.AddMessage("使用临时空间（内存工作空间）")

        arcpy.AddMessage("逆地理编码平台：{}".format(platform_text))
        arcpy.AddMessage("输出模式：长表（每个 POI 一行）")
        arcpy.AddMessage("输入坐标系：{}".format(input_coord_type))
        arcpy.AddMessage("搜索半径：{} 米".format(radius))
        arcpy.AddMessage("POI 数量：{}".format(poi_count))
        arcpy.AddMessage("处理速度：{}".format(speed_level_text))

        out_fc = reverse_geocode.run_reverse_geocode(
            in_table=in_points,
            input_coord_type=input_coord_type,
            platform=platform,
            radius=radius,
            poi_count=poi_count,
            speed_level=speed_level,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name
        )

        if out_fc:
            parameters[9].value = out_fc
            try:
                arcpy.SetParameterAsText(9, out_fc)
            except Exception:
                pass
            arcpy.AddMessage("逆地理编码完成！")
        else:
            arcpy.AddWarning("未生成逆地理编码结果。")


class BaiduPoiInExtent(object):
    """C_百度地点检索3.0_POI按区域批量采集"""

    def __init__(self):
        self.label = "C_百度地点检索3.0_POI按区域批量采集（测试中）"
        self.description = "输入范围后调用百度 POI 检索 3.0；WGS84 模式下坐标值保持 WGS84，GCJ02 模式下坐标值保持 GCJ02，但输出要素类空间参考仍写为 WGS84。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="范围输入方式",
            name="extent_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p0.filter.type = "ValueList"
        p0.filter.list = ["面图层范围", "屏幕范围（当前视图）", "手动输入范围"]
        p0.value = "面图层范围"

        p1 = arcpy.Parameter(
            displayName="范围面图层（用于取范围）",
            name="in_polygon",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p1.filter.list = ["Polygon"]
        p1.enabled = False

        p2 = arcpy.Parameter(
            displayName="手动输入范围（ArcGIS Extent）",
            name="extent",
            datatype="GPExtent",
            parameterType="Optional",
            direction="Input"
        )

        p3 = arcpy.Parameter(
            displayName="POI 关键词（query，支持 $ 多词）",
            name="keywords",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p3.value = ""

        p4 = arcpy.Parameter(
            displayName="输出 WGS84 坐标（不勾选则输出 GCJ02）",
            name="output_wgs84",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p4.value = True

        p5 = arcpy.Parameter(
            displayName="最大获取数量（条）",
            name="max_poi_count",
            datatype="GPLong",
            parameterType="Required",
            direction="Input"
        )
        p5.value = 1000

        p6 = arcpy.Parameter(
            displayName="使用临时空间（in_memory）",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p6.value = False

        p7 = arcpy.Parameter(
            displayName="输出 GDB（.gdb）",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p7.value = _get_project_default_geodatabase()

        p8 = arcpy.Parameter(
            displayName="输出要素类名称（点）",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p8.value = "百度_POI_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p9 = arcpy.Parameter(
            displayName="输出 POI 要素类",
            name="out_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        extent_type = parameters[0].value

        if extent_type == "面图层范围":
            parameters[1].enabled = True
            parameters[2].enabled = False
        elif extent_type == "屏幕范围（当前视图）":
            parameters[1].enabled = False
            parameters[2].enabled = False
        else:
            parameters[1].enabled = False
            parameters[2].enabled = True

        use_in_memory = bool(parameters[6].value)
        parameters[7].enabled = not use_in_memory
        if use_in_memory:
            parameters[7].value = None
        return parameters

    def updateMessages(self, parameters):
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText

        if not use_in_memory:
            if not out_gdb:
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录），或勾选“使用临时空间”。")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[7].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录）。")

        extent_type = parameters[0].value
        in_polygon = parameters[1].value
        manual_extent = parameters[2].value
        keywords = (parameters[3].valueAsText or "").strip()

        if extent_type == "面图层范围" and not in_polygon:
            parameters[1].setErrorMessage("选择“面图层范围”时必须指定输入面图层。")
        elif extent_type == "手动输入范围" and not manual_extent:
            parameters[2].setErrorMessage("选择“手动输入范围”时必须指定处理范围。")

        if not keywords:
            parameters[3].setErrorMessage("百度 3.0 POI 检索需要填写关键词（query）。")

    def execute(self, parameters, messages):
        _reload_core_modules()
        # 百度地点检索需要百度 Key
        common.require_keys(["baidu"])

        extent_type = parameters[0].valueAsText or "面图层范围"
        in_polygon = parameters[1].valueAsText
        extent = parameters[2].value
        keywords = (parameters[3].valueAsText or "").strip()
        output_wgs84 = bool(parameters[4].value)
        max_poi_count = int(parameters[5].value) if parameters[5].value else 1000
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText or ""
        out_fc_name = parameters[8].valueAsText or ""

        if not keywords:
            arcpy.AddError("百度 3.0 POI 检索需要填写关键词（query）。")
            raise arcpy.ExecuteError

        try:
            extent, source_sr = _resolve_poi_extent(extent_type, in_polygon, extent)
        except ValueError as exc:
            arcpy.AddError(str(exc))
            raise arcpy.ExecuteError
        extent = _extent_to_wgs84(extent, source_sr)
        arcpy.AddMessage("WGS84 范围：XMin={}, YMin={}, XMax={}, YMax={}".format(
            extent.XMin, extent.YMin, extent.XMax, extent.YMax))

        out_gdb, out_fc_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)

        if use_in_memory:
            arcpy.AddMessage("使用临时空间（内存工作空间）")

        arcpy.AddMessage("百度 3.0 POI 检索")
        if output_wgs84:
            arcpy.AddMessage("输出坐标系：WGS84")
        else:
            arcpy.AddMessage("输出坐标系：GCJ02（坐标值保持GCJ02，空间参考写为WGS84）")
        arcpy.AddMessage("关键词：{}".format(keywords if keywords else "无"))
        arcpy.AddMessage("POI 类型：无")
        arcpy.AddMessage("最大获取数量：{}".format(max_poi_count))

        out_fc, unfinished_fc = poi_search.run_poi_search_in_extent(
            extent=extent,
            keywords=keywords,
            poi_types=None,
            platform='baidu',
            max_poi_count=max_poi_count,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            out_unfinished_fc_name='{}_未完成矩形范围'.format(out_fc_name)
        )

        if out_fc:
            parameters[9].value = out_fc
            try:
                arcpy.SetParameterAsText(9, out_fc)
            except Exception:
                pass
            arcpy.AddMessage("POI 检索完成！")
        else:
            arcpy.AddWarning("未找到任何 POI 结果。")

class AmapPoiInExtent(object):
    """B_高德POI按区域批量采集"""

    def __init__(self):
        self.label = "B_高德 POI 批量采集"
        self.description = "输入范围后调用高德 POI 检索；GCJ02 模式下坐标值保持 GCJ02，但输出要素类空间参考仍写为 WGS84。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="范围输入方式",
            name="extent_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p0.filter.type = "ValueList"
        p0.filter.list = ["面图层范围", "屏幕范围（当前视图）", "手动输入范围"]
        p0.value = "面图层范围"

        p1 = arcpy.Parameter(
            displayName="范围面图层（用于取范围）",
            name="in_polygon",
            datatype="GPFeatureLayer",
            parameterType="Optional",
            direction="Input"
        )
        p1.filter.list = ["Polygon"]
        p1.enabled = False

        p2 = arcpy.Parameter(
            displayName="手动输入范围（ArcGIS Extent）",
            name="extent",
            datatype="GPExtent",
            parameterType="Optional",
            direction="Input"
        )

        p3 = arcpy.Parameter(
            displayName="POI 关键词（可选）",
            name="keywords",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p3.value = ""

        p4 = arcpy.Parameter(
            displayName="需要细分 POI 类别",
            name="need_detail",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p4.value = False

        category_cache = _load_amap_poi_category_cache()
        big_choice_list = [display for display, _code in category_cache.get("big_choices", [])]
        first_big_choice = big_choice_list[0] if big_choice_list else ""

        p5 = arcpy.Parameter(
            displayName="POI 大类（编码+中文名）",
            name="poi_type_big",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p5.filter.type = "ValueList"
        p5.filter.list = big_choice_list
        p5.value = first_big_choice
        p5.enabled = False

        p6 = arcpy.Parameter(
            displayName="POI 子类（编码+中文名，可多选）",
            name="poi_types",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p6.multiValue = True
        p6.filter.type = "ValueList"
        p6.filter.list = _load_amap_poi_subcategory_choices(p5.valueAsText)
        p6.value = ""
        p6.enabled = False

        p7 = arcpy.Parameter(
            displayName="输出 WGS84 坐标（不勾选则输出 GCJ02）",
            name="output_wgs84",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p7.value = True

        p8 = arcpy.Parameter(
            displayName="最大获取数量（条）",
            name="max_poi_count",
            datatype="GPLong",
            parameterType="Required",
            direction="Input"
        )
        p8.value = 1000

        p9 = arcpy.Parameter(
            displayName="使用临时空间（in_memory）",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p9.value = False

        p10 = arcpy.Parameter(
            displayName="输出 GDB（.gdb）",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p10.value = _get_project_default_geodatabase()

        p11 = arcpy.Parameter(
            displayName="输出要素类名称（点）",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p11.value = "高德_POI_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p12 = arcpy.Parameter(
            displayName="输出 POI 要素类",
            name="out_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        p13 = arcpy.Parameter(
            displayName="运行后打开高德接口网页",
            name="open_amap_doc",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input"
        )
        p13.value = False

        p14 = arcpy.Parameter(
            displayName="输出未完成矩形范围",
            name="out_unfinished_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11, p12, p13, p14]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        extent_type = parameters[0].value

        if extent_type == "面图层范围":
            parameters[1].enabled = True
            parameters[2].enabled = False
        elif extent_type == "屏幕范围（当前视图）":
            parameters[1].enabled = False
            parameters[2].enabled = False
        else:
            parameters[1].enabled = False
            parameters[2].enabled = True

        need_detail = bool(parameters[4].value)
        parameters[5].enabled = need_detail
        parameters[6].enabled = need_detail

        if need_detail:
            big_choice = parameters[5].valueAsText or ""
            if not big_choice:
                category_cache = _load_amap_poi_category_cache()
                big_choice_list = [display for display, _code in category_cache.get("big_choices", [])]
                if big_choice_list:
                    parameters[5].value = big_choice_list[0]
                    big_choice = parameters[5].valueAsText or ""

            sub_choices = _load_amap_poi_subcategory_choices(big_choice)
            parameters[6].filter.type = "ValueList"
            parameters[6].filter.list = sub_choices

            current_sub_values = _split_arcgis_multi_value(parameters[6].valueAsText)
            if current_sub_values:
                valid_codes = set(_resolve_amap_poi_typecode_list(";".join(sub_choices)))
                current_codes = _resolve_amap_poi_typecode_list(current_sub_values)
                if any(code not in valid_codes for code in current_codes):
                    parameters[6].value = ""
        else:
            parameters[5].value = ""
            parameters[6].value = ""

        use_in_memory = bool(parameters[9].value)
        parameters[10].enabled = not use_in_memory
        if use_in_memory:
            parameters[10].value = None
        return parameters

    def updateMessages(self, parameters):
        use_in_memory = bool(parameters[9].value)
        out_gdb = parameters[10].valueAsText

        if not use_in_memory:
            if not out_gdb:
                parameters[10].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录），或勾选“使用临时空间”。")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[10].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录）。")

        extent_type = parameters[0].value
        in_polygon = parameters[1].value
        manual_extent = parameters[2].value

        if extent_type == "面图层范围" and not in_polygon:
            parameters[1].setErrorMessage("选择“面图层范围”时必须指定输入面图层。")
        elif extent_type == "手动输入范围" and not manual_extent:
            parameters[2].setErrorMessage("选择“手动输入范围”时必须指定处理范围。")

    def execute(self, parameters, messages):
        _reload_core_modules()
        # 高德 POI 检索需要高德 Key
        common.require_keys(["amap"])

        extent_type = parameters[0].valueAsText or "面图层范围"
        in_polygon = parameters[1].valueAsText
        extent = parameters[2].value
        keywords = (parameters[3].valueAsText or "").strip() or None
        need_detail = bool(parameters[4].value)
        poi_type_big = (parameters[5].valueAsText or "").strip() or None
        poi_types_values = parameters[6].valueAsText
        output_wgs84 = bool(parameters[7].value)
        max_poi_count = int(parameters[8].value) if parameters[8].value else 1000
        use_in_memory = bool(parameters[9].value)
        out_gdb = parameters[10].valueAsText or ""
        out_fc_name = parameters[11].valueAsText or ""
        open_amap_doc = bool(parameters[13].value)
        unfinished_param_index = 14

        poi_types = None
        if need_detail:
            poi_type_codes = _resolve_amap_poi_typecode_list(poi_types_values)
            if not poi_type_codes:
                poi_type_codes = _resolve_amap_poi_typecode_list(poi_type_big)
            poi_types = "|".join(poi_type_codes) if poi_type_codes else None

        if open_amap_doc:
            try:
                if webbrowser.open_new_tab(AMAP_POI_DOC_URL):
                    arcpy.AddMessage("已打开高德接口网页：{}".format(AMAP_POI_DOC_URL))
                else:
                    arcpy.AddWarning("已勾选打开高德接口网页，但系统未能自动打开浏览器：{}".format(AMAP_POI_DOC_URL))
            except Exception as exc:
                arcpy.AddWarning("打开高德接口网页失败：{} - {}".format(AMAP_POI_DOC_URL, str(exc)))

        try:
            extent, source_sr = _resolve_poi_extent(extent_type, in_polygon, extent)
        except ValueError as exc:
            arcpy.AddError(str(exc))
            raise arcpy.ExecuteError
        extent = _extent_to_wgs84(extent, source_sr)
        arcpy.AddMessage("WGS84 范围：XMin={}, YMin={}, XMax={}, YMax={}".format(
            extent.XMin, extent.YMin, extent.XMax, extent.YMax))

        out_gdb, out_fc_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)

        if use_in_memory:
            arcpy.AddMessage("使用临时空间（内存工作空间）")

        arcpy.AddMessage("高德 POI 检索")
        if output_wgs84:
            arcpy.AddMessage("输出坐标系：WGS84")
        else:
            arcpy.AddMessage("输出坐标系：GCJ02（坐标值保持GCJ02，空间参考写为WGS84）")
        arcpy.AddMessage("关键词：{}".format(keywords if keywords else "无"))
        arcpy.AddMessage("需要细分：{}".format("是" if need_detail else "否"))
        if need_detail:
            arcpy.AddMessage("POI 大类：{}".format(poi_type_big if poi_type_big else "无"))
            arcpy.AddMessage("POI 类型：{}".format(poi_types if poi_types else "无"))
        else:
            arcpy.AddMessage("POI 类型：全部爬取")
        arcpy.AddMessage("最大获取数量：{}".format(max_poi_count))

        out_fc, unfinished_fc = poi_search.run_poi_search_in_extent(
            extent=extent,
            keywords=keywords,
            poi_types=poi_types,
            platform='amap',
            max_poi_count=max_poi_count,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            out_unfinished_fc_name='{}_未完成矩形范围'.format(out_fc_name)
        )

        if out_fc:
            parameters[12].value = out_fc
            try:
                arcpy.SetParameterAsText(12, out_fc)
            except Exception:
                pass
            arcpy.AddMessage("POI 检索完成！")
        else:
            arcpy.AddWarning("未找到任何 POI 结果。")

        if unfinished_fc and unfinished_param_index is not None:
            parameters[unfinished_param_index].value = unfinished_fc
            try:
                arcpy.SetParameterAsText(unfinished_param_index, unfinished_fc)
            except Exception:
                pass

class CoordTransformPoints(object):
    """F_坐标系转换_点要素批量转换"""

    def __init__(self):
        self.label = "F_坐标系转换_WGS84\\CGC2000\\GCJ02\\BD09互转"
        self.description = "输入点图层/要素类，按指定源/目标坐标系批量转换；WGS84 与 CGCS2000 之间按近似等值处理，输出空间参考按目标坐标系写入（CGCS2000 使用 EPSG:4490）；GCJ02 和 BD09 坐标值保持原值，但输出要素类空间参考仍写为 WGS84。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="输入点图层/要素类",
            name="in_points",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input"
        )
        p0.filter.list = ["Point"]

        p1 = arcpy.Parameter(
            displayName="输入坐标系",
            name="input_coord_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p1.filter.type = "ValueList"
        p1.filter.list = coord_transform_tool.get_coord_system_choices()
        p1.value = "WGS84"

        p2 = arcpy.Parameter(
            displayName="目标坐标系",
            name="output_coord_type",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p2.filter.type = "ValueList"
        p2.filter.list = coord_transform_tool.get_coord_system_choices()
        p2.value = "WGS84"

        p3 = arcpy.Parameter(
            displayName="使用临时空间（内存工作空间）",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p3.value = False

        p4 = arcpy.Parameter(
            displayName="输出GDB（.gdb目录）",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p4.value = _get_project_default_geodatabase()

        p5 = arcpy.Parameter(
            displayName="输出要素类名称",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p5.value = "坐标转换_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p6 = arcpy.Parameter(
            displayName="输出要素类（点）",
            name="out_feature_class",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p2, p3, p4, p5, p6]
    def updateMessages(self, parameters):
        in_points = parameters[0].valueAsText
        if in_points:
            try:
                desc = arcpy.Describe(in_points)
                if getattr(desc, "shapeType", "").lower() != "point":
                    parameters[0].setErrorMessage("输入图层必须是点要素。")
            except Exception:
                pass

        use_in_memory = bool(parameters[3].value)
        out_gdb = parameters[4].valueAsText

        if not use_in_memory:
            if not out_gdb:
                parameters[4].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录），或勾选“使用临时空间”。")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[4].setErrorMessage("请选择 FileGDB（以 .gdb 结尾的目录）。")

    def execute(self, parameters, messages):
        _reload_core_modules()
        in_points = parameters[0].valueAsText or ""
        input_coord_type = parameters[1].valueAsText or "WGS84"
        output_coord_type = parameters[2].valueAsText or "WGS84"
        use_in_memory = bool(parameters[3].value)
        out_gdb = parameters[4].valueAsText or ""
        out_fc_name = parameters[5].valueAsText or ""

        arcpy.AddMessage("输入点图层/要素类：{}".format(in_points))
        arcpy.AddMessage("输入坐标系：{}".format(input_coord_type))
        output_coord_text = (output_coord_type or "").upper()
        if "CGCS2000" in output_coord_text or "CGCS" in output_coord_text:
            arcpy.AddMessage("目标坐标系：CGCS2000（输出几何写入 EPSG:4490 空间参考；坐标值按 WGS84/CGCS2000 近似值写出）")
        elif "GCJ02" in output_coord_text or "GCJ" in output_coord_text:
            arcpy.AddMessage("目标坐标系：{}（输出几何按 GCJ02 坐标值写出，空间参考仍写为 WGS84）".format(output_coord_type))
        elif "BD09" in output_coord_text or "BAIDU" in output_coord_text:
            arcpy.AddMessage("目标坐标系：{}（输出几何按 BD09 坐标值写出，空间参考仍写为 WGS84）".format(output_coord_type))
        else:
            arcpy.AddMessage("目标坐标系：{}".format(output_coord_type))

        out_fc = coord_transform_tool.run_coord_transform(
            in_points=in_points,
            input_coord_type=input_coord_type,
            output_coord_type=output_coord_type,
            use_in_memory=use_in_memory,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name
        )

        if out_fc:
            parameters[6].value = out_fc
            try:
                arcpy.SetParameterAsText(6, out_fc)
            except Exception:
                pass
            arcpy.AddMessage("坐标转换完成！")
        else:
            arcpy.AddWarning("未生成坐标转换结果。")


def _platform_to_code(platform_text):
    return {
        "高德": "amap",
        "百度": "baidu",
        "都要": "both",
        "baidu": "baidu",
        "amap": "amap",
        "both": "both",
    }.get(platform_text, "amap")


def _route_mode_choices(platform_text):
    if platform_text == "高德" or platform_text == "amap":
        return ["驾车", "步行", "公交", "骑行", "电动车"]
    return ["驾车", "步行", "公交"]


class AdminAreaBoundaryExport(object):
    """A_行政区划查询_边界导出"""

    def __init__(self):
        self.label = "A_全国行政区划_边界导出"
        self.description = "按省/市/区县级联下拉选择导出行政区边界面，支持所有市级/所有县级。"
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="\u7701\u4efd",
            name="province_choice",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p0.filter.type = "ValueList"
        p0.filter.list = admin_area_tool.get_admin_province_choices()

        p1 = arcpy.Parameter(
            displayName="\u57ce\u5e02",
            name="city_choice",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p1.filter.type = "ValueList"
        p1.filter.list = []

        p2 = arcpy.Parameter(
            displayName="\u533a\u53bf",
            name="county_choice",
            datatype="GPString",
            parameterType="Optional",
            direction="Input"
        )
        p2.filter.type = "ValueList"
        p2.filter.list = []

        p3 = arcpy.Parameter(
            displayName="\u5e73\u53f0",
            name="platform",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p3.filter.type = "ValueList"
        p3.filter.list = ["\u9ad8\u5fb7", "\u767e\u5ea6"]
        p3.value = "\u9ad8\u5fb7"

        p4 = arcpy.Parameter(
            displayName="\u5904\u7406\u901f\u5ea6",
            name="speed_level",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p4.filter.type = "ValueList"
        p4.filter.list = ["\u4f4e\uff08\u7a33\u5b9a\uff09", "\u4e2d\uff08\u5e73\u8861\uff09"]
        p4.value = "\u4f4e\uff08\u7a33\u5b9a\uff09"

        p5 = arcpy.Parameter(
            displayName="\u8f93\u51fa WGS84 \u5750\u6807",
            name="output_wgs84",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p5.value = True

        p6 = arcpy.Parameter(
            displayName="\u4f7f\u7528\u4e34\u65f6\u7a7a\u95f4\uff08in_memory\uff09",
            name="use_in_memory",
            datatype="GPBoolean",
            parameterType="Required",
            direction="Input"
        )
        p6.value = False

        p7 = arcpy.Parameter(
            displayName="\u8f93\u51fa GDB\uff08.gdb\uff09",
            name="out_gdb",
            datatype="DEWorkspace",
            parameterType="Optional",
            direction="Input"
        )
        p7.value = _get_project_default_geodatabase()

        p8 = arcpy.Parameter(
            displayName="\u8f93\u51fa\u8981\u7d20\u7c7b\u540d\u79f0",
            name="out_fc_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input"
        )
        p8.value = "\u884c\u653f\u533a\u8fb9\u754c_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        p9 = arcpy.Parameter(
            displayName="\u8f93\u51fa\u8fb9\u754c\u8981\u7d20\u7c7b",
            name="out_boundary_fc",
            datatype="DEFeatureClass",
            parameterType="Derived",
            direction="Output"
        )

        p10 = arcpy.Parameter(
            displayName="\u8f93\u51fa\u5931\u8d25\u8868",
            name="out_failure_table",
            datatype="DETable",
            parameterType="Derived",
            direction="Output"
        )

        return [p0, p1, p2, p3, p4, p5, p6, p7, p8, p9, p10]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        province_choices = admin_area_tool.get_admin_province_choices()
        parameters[0].filter.list = province_choices
        province_value = parameters[0].valueAsText or ""
        if province_value and province_value not in province_choices:
            parameters[0].value = None
            province_value = ""
        parameters[1].enabled = bool(province_value)

        city_choices = admin_area_tool.get_admin_city_choices(province_value) if province_value else []
        parameters[1].filter.list = city_choices
        city_value = parameters[1].valueAsText or ""
        if city_value and city_value not in city_choices:
            parameters[1].value = None
            city_value = ""
        elif province_value and len(city_choices) == 1 and city_choices[0] and city_value != city_choices[0]:
            parameters[1].value = city_choices[0]
            city_value = city_choices[0]

        county_enabled = bool(city_value)
        if city_value == admin_area_tool.ADMIN_ALL_CITY_LABEL:
            county_choices = []
        else:
            county_choices = admin_area_tool.get_admin_county_choices(city_value) if county_enabled else []
            county_enabled = county_enabled and bool(county_choices)
        parameters[2].enabled = county_enabled
        parameters[2].filter.list = county_choices
        county_value = parameters[2].valueAsText or ""
        if not county_enabled or city_value == admin_area_tool.ADMIN_ALL_CITY_LABEL:
            parameters[2].value = None
            county_value = ""
        elif county_value and county_value not in county_choices:
            parameters[2].value = None
            county_value = ""

        platform_text = parameters[3].valueAsText or "\u9ad8\u5fb7"
        if platform_text not in ("\u767e\u5ea6", "\u9ad8\u5fb7"):
            platform_text = "\u767e\u5ea6"
            parameters[3].value = platform_text

        use_in_memory = bool(parameters[6].value)
        parameters[7].enabled = not use_in_memory
        if use_in_memory:
            parameters[7].value = None
        return parameters

    def updateMessages(self, parameters):
        province_value = parameters[0].valueAsText
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText

        if not province_value:
            parameters[0].setErrorMessage("\u8bf7\u9009\u62e9\u7701\u4efd")

        if not use_in_memory:
            if not out_gdb:
                parameters[7].setErrorMessage("\u8bf7\u6307\u5b9a FileGDB\uff08.gdb\uff09\u8f93\u51fa\u8def\u5f84")
            elif not out_gdb.lower().endswith(".gdb"):
                parameters[7].setErrorMessage("\u8f93\u51fa\u76ee\u5f55\u5fc5\u987b\u662f FileGDB\uff08.gdb\uff09")

    def execute(self, parameters, messages):
        _reload_core_modules()
        # 行政区划导出需要对应平台 Key
        platform_text = parameters[3].valueAsText or "高德"
        common.require_keys(["baidu" if platform_text == "百度" else "amap"])

        province_choice = parameters[0].valueAsText or ""
        city_choice = parameters[1].valueAsText or ""
        county_choice = parameters[2].valueAsText or ""
        if platform_text not in ("\u767e\u5ea6", "\u9ad8\u5fb7"):
            platform_text = "\u9ad8\u5fb7"
        speed_level_text = parameters[4].valueAsText or "\u4f4e\uff08\u7a33\u5b9a\uff09"
        output_wgs84 = bool(parameters[5].value)
        use_in_memory = bool(parameters[6].value)
        out_gdb = parameters[7].valueAsText or ""
        out_fc_name = parameters[8].valueAsText or ""

        platform = "baidu" if platform_text == "\u767e\u5ea6" else "amap"
        speed_level = common.speed_level_to_code(speed_level_text)
        out_gdb, out_fc_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)

        if use_in_memory:
            arcpy.AddMessage("\u4f7f\u7528\u4e34\u65f6\u7a7a\u95f4\uff08\u5185\u5b58\u5de5\u4f5c\u7a7a\u95f4\uff09")
        arcpy.AddMessage("\u884c\u653f\u533a\u8fb9\u754c\u5e73\u53f0\uff1a{}".format(platform_text))
        arcpy.AddMessage("\u5f53\u524d\u9009\u62e9\uff1a{} / {} / {}".format(
            province_choice,
            city_choice,
            county_choice,
        ))
        arcpy.AddMessage("\u5904\u7406\u901f\u5ea6\uff1a{}".format(speed_level_text))

        out_boundary_fc, out_failure_table = admin_area_tool.run_admin_area_boundary_export_selected(
            province_choice=province_choice,
            city_choice=city_choice,
            county_choice=county_choice,
            platform=platform,
            speed_level=speed_level,
            output_wgs84=output_wgs84,
            out_gdb=out_gdb,
            out_fc_name=out_fc_name,
            return_failure_table=True,
        )

        parameters[9].value = out_boundary_fc
        parameters[10].value = out_failure_table
        try:
            arcpy.SetParameterAsText(9, out_boundary_fc)
            arcpy.SetParameterAsText(10, out_failure_table)
        except Exception:
            pass

class Toolbox(object):
    def __init__(self):
        _reload_core_modules()
        self.label = "百度高德Geocode"
        self.alias = "baidu_amap_geocode"
        self.tools = [
            AdminAreaBoundaryExport,
            AmapPoiInExtent,
            GeocodeToWgs84Points,
            ReverseGeocodeToPoi,
            CoordTransformPoints,
            BaiduPoiInExtent,
        ]

