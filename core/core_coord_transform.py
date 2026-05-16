# -*- coding: utf-8 -*-
"""
点要素坐标系批量转换模块。

支持 WGS84、GCJ-02（国测局/火星坐标）、BD09（百度）和 CGCS2000 之间的本地转换。
"""

import math

import arcpy

from . import core_common as common


_COORD_SYSTEM_CHOICES = [
    "WGS84",
    "GCJ02(国测局)",
    "BD09(百度)",
    "CGCS2000",
]


def get_coord_system_choices():
    """返回给界面使用的坐标系候选值。"""
    return list(_COORD_SYSTEM_CHOICES)


def _normalize_coord_system(system):
    """将界面值或别名统一成内部坐标系代码。"""
    text = str(system or "").strip().upper()
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("-", "").replace("_", "").replace(" ", "")

    if not text:
        raise ValueError("坐标系不能为空")

    if "WGS84" in text or text == "WGS" or text == "4326" or "EPSG4326" in text:
        return "WGS84"
    if "CGCS2000" in text or "CGCS" in text:
        return "CGCS2000"
    if "GCJ02" in text or "GCJ" in text or "国测局" in text:
        return "GCJ02"
    if "BD09" in text or "BAIDU" in text or "百度" in text:
        return "BD09"

    raise ValueError("不支持的坐标系：{}".format(system))


def _conversion_coord_system(system):
    """将 CGCS2000 近似折算为 WGS84，供本地转换公式使用。"""
    key = _normalize_coord_system(system)
    return "WGS84" if key == "CGCS2000" else key


def _output_spatial_reference(target_coord_system):
    """根据目标坐标系，选择输出要素类空间参考。"""
    key = _normalize_coord_system(target_coord_system)
    if key == "CGCS2000":
        return arcpy.SpatialReference(4490)
    if key in ("WGS84", "GCJ02", "BD09"):
        return arcpy.SpatialReference(4326)
    return None


def _build_copy_field_specs(in_fc):
    """
    生成输入字段复制方案。

    输出字段统一加 IN_ 前缀；不复制几何/OID/栅格类字段。
    """
    specs = []

    for field in arcpy.ListFields(in_fc):
        if field.type in ("OID", "Geometry", "Raster", "Blob"):
            continue

        output_name = "IN_{}".format(field.name)
        output_alias = "IN_{}".format(field.aliasName or field.name)
        field_type = None
        field_length = None
        field_precision = None
        field_scale = None
        coerce_to_text = False

        if field.type == "String":
            field_type = "TEXT"
            # 本地 GDB / memory 都支持较长文本，这里放宽到 32767，避免长 JSON / URL 字段写入失败。
            field_length = max(1, min(max(int(field.length or 255), 32767), 32767))
        elif field.type == "SmallInteger":
            field_type = "SHORT"
        elif field.type == "Integer":
            field_type = "LONG"
        elif field.type == "Single":
            field_type = "FLOAT"
            field_precision = field.precision or None
            field_scale = field.scale or None
        elif field.type == "Double":
            field_type = "DOUBLE"
            field_precision = field.precision or None
            field_scale = field.scale or None
        elif field.type == "Date":
            field_type = "DATE"
        elif field.type in ("GUID", "GlobalID"):
            # GUID/GlobalID 统一按文本保留，避免不同工作空间兼容性问题。
            field_type = "TEXT"
            field_length = 38
            coerce_to_text = True
        else:
            arcpy.AddWarning("跳过不支持复制的字段：{} ({})".format(field.name, field.type))
            continue

        specs.append({
            "source_name": field.name,
            "output_name": output_name,
            "field_type": field_type,
            "field_length": field_length,
            "field_precision": field_precision,
            "field_scale": field_scale,
            "field_alias": output_alias,
            "coerce_to_text": coerce_to_text,
        })

    return specs


def _create_output_feature_class(output_workspace, output_name, spatial_reference, field_specs):
    """创建输出点要素类并添加复制字段。"""
    if spatial_reference is None:
        out_fc = arcpy.management.CreateFeatureclass(
            output_workspace,
            output_name,
            "POINT"
        )[0]
    else:
        out_fc = arcpy.management.CreateFeatureclass(
            output_workspace,
            output_name,
            "POINT",
            spatial_reference=spatial_reference
        )[0]

    for spec in field_specs:
        kwargs = {
            "field_alias": spec["field_alias"],
        }
        if spec["field_length"] is not None:
            kwargs["field_length"] = spec["field_length"]
        if spec["field_precision"] is not None:
            kwargs["field_precision"] = spec["field_precision"]
        if spec["field_scale"] is not None:
            kwargs["field_scale"] = spec["field_scale"]

        arcpy.management.AddField(
            out_fc,
            spec["output_name"],
            spec["field_type"],
            **kwargs
        )

    arcpy.management.AddField(out_fc, "src_oid", "LONG", field_alias="源OID")
    arcpy.management.AddField(out_fc, "input_lng", "DOUBLE", field_alias="输入经度")
    arcpy.management.AddField(out_fc, "input_lat", "DOUBLE", field_alias="输入纬度")
    arcpy.management.AddField(out_fc, "output_lng", "DOUBLE", field_alias="输出经度")
    arcpy.management.AddField(out_fc, "output_lat", "DOUBLE", field_alias="输出纬度")
    arcpy.management.AddField(out_fc, "coord_sys", "TEXT", field_length=16, field_alias="输出坐标系")

    return out_fc


def run_coord_transform(in_points, input_coord_type="WGS84", output_coord_type="WGS84",
                        use_in_memory=False, out_gdb=None, out_fc_name=None):
    """
    点要素批量坐标转换主函数。

    参数：
    - in_points: 输入点要素类/点图层
    - input_coord_type: 输入坐标系
    - output_coord_type: 目标坐标系
    - use_in_memory: 是否输出到内存工作空间
    - out_gdb: 输出 GDB
    - out_fc_name: 输出要素类名称
    """
    input_key = _normalize_coord_system(input_coord_type)
    output_key = _normalize_coord_system(output_coord_type)
    same_numeric_family = {input_key, output_key} <= {"WGS84", "CGCS2000"}

    if input_key == output_key:
        arcpy.AddMessage("输入坐标系与目标坐标系一致，将直接复制坐标值。")
    elif same_numeric_family:
        arcpy.AddMessage("WGS84 与 CGCS2000 在本工具中按近似等值处理，坐标值保持不变，仅按目标坐标系写入空间参考。")
    if output_key == "CGCS2000":
        arcpy.AddMessage("CGCS2000 输出将写入 EPSG:4490 空间参考，坐标值按 WGS84/CGCS2000 近似值写出。")
    elif output_key == "GCJ02":
        arcpy.AddMessage("GCJ02 输出将写入 WGS84 空间参考，但坐标值保持 GCJ02。")
    elif output_key == "BD09":
        arcpy.AddMessage("BD09 输出将写入 WGS84 空间参考，但坐标值保持 BD09。")

    output_workspace, output_name = common.get_output_path(use_in_memory, out_gdb, out_fc_name)
    if use_in_memory:
        # ArcGIS Pro 更适合用 memory 工作空间展示结果；in_memory 不稳定地出现在 Contents 中。
        output_workspace = "memory"
    spatial_reference = _output_spatial_reference(output_key)
    field_specs = _build_copy_field_specs(in_points)
    out_fc = _create_output_feature_class(output_workspace, output_name, spatial_reference, field_specs)

    if use_in_memory:
        arcpy.AddMessage("使用临时空间（内存工作空间）")

    arcpy.AddMessage("输入坐标系：{}".format(input_key))
    arcpy.AddMessage("目标坐标系：{}".format(output_key))
    arcpy.AddMessage("输出要素类：{}".format(out_fc))

    src_field_names = [spec["source_name"] for spec in field_specs]
    insert_fields = [
        "SHAPE@",
        "src_oid",
        "input_lng",
        "input_lat",
        "output_lng",
        "output_lat",
        "coord_sys",
    ] + [spec["output_name"] for spec in field_specs]

    total_count = 0
    success_count = 0
    fail_count = 0
    truncation_count = 0
    source_coord_key = _conversion_coord_system(input_key)
    target_coord_key = _conversion_coord_system(output_key)

    search_fields = ["OID@", "SHAPE@XY"] + src_field_names

    with arcpy.da.SearchCursor(in_points, search_fields) as search_cursor, \
            arcpy.da.InsertCursor(out_fc, insert_fields) as insert_cursor:
        for row in search_cursor:
            total_count += 1
            src_oid = row[0]
            shape_xy = row[1]

            if not shape_xy:
                fail_count += 1
                arcpy.AddWarning("记录 {} 缺少有效点几何，已跳过。".format(src_oid))
                continue

            try:
                input_lng = float(shape_xy[0])
                input_lat = float(shape_xy[1])
                if input_key == output_key or same_numeric_family:
                    output_lng, output_lat = input_lng, input_lat
                else:
                    output_lng, output_lat = common.convert_coord(
                        input_lng,
                        input_lat,
                        source_coord_key,
                        target_coord_key
                    )

                if not (math.isfinite(float(output_lng)) and math.isfinite(float(output_lat))):
                    raise ValueError("转换结果不是有效数值：{}, {}".format(output_lng, output_lat))

                point = arcpy.Point(float(output_lng), float(output_lat))
                geom = arcpy.PointGeometry(point, spatial_reference) if spatial_reference else arcpy.PointGeometry(point)
                copied_values = []

                for index, spec in enumerate(field_specs, start=2):
                    value = row[index]
                    if spec["coerce_to_text"] and value is not None:
                        value = str(value)
                    if spec["field_type"] == "TEXT" and value is not None:
                        value_text = value if isinstance(value, str) else str(value)
                        max_len = spec["field_length"] or 0
                        if max_len > 0 and len(value_text) > max_len:
                            value = value_text[:max_len]
                            truncation_count += 1
                        else:
                            value = value_text
                    copied_values.append(value)

                insert_cursor.insertRow([
                    geom,
                    src_oid,
                    input_lng,
                    input_lat,
                    float(output_lng),
                    float(output_lat),
                    output_key,
                ] + copied_values)
                success_count += 1
            except Exception as exc:
                fail_count += 1
                arcpy.AddWarning("记录 {} 坐标转换失败：{}".format(src_oid, str(exc)))

            if total_count % 1000 == 0:
                arcpy.AddMessage("已处理 {} 条记录（成功 {}，失败 {}）".format(
                    total_count, success_count, fail_count
                ))

    arcpy.AddMessage("坐标转换完成：成功 {} 条，失败 {} 条。".format(success_count, fail_count))
    if truncation_count > 0:
        arcpy.AddWarning("有 {} 个文本值因字段长度限制被截断。".format(truncation_count))
    arcpy.AddMessage("输出要素类：{}".format(out_fc))
    return out_fc


__all__ = ["run_coord_transform", "get_coord_system_choices"]




