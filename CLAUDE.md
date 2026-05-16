# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

ArcGIS Python Toolbox (`.pyt`) for geocoding with Baidu (百度) and Amap (高德) Web service APIs. Runs inside ArcGIS Pro. The `.pyt` file is the ArcGIS toolbox definition; `core/` contains the actual logic. There is no build step, test suite, or package manager — this is a self-contained toolbox deployed by copying files.

## Architecture

```
百度高德Geocode.pyt          # ArcGIS toolbox entry point (6 tools, labeled A–F)
├── config/
│   ├── baidu_keys.example.txt    # Template → user copies to baidu_keys.txt
│   ├── amap_keys.example.txt     # Template → user copies to amap_keys.txt
│   ├── amap_poi_categories.json  # Amap POI category code table
│   └── admin_divisions_level.json  # Cached admin tree (generated at runtime, .gitignored)
└── core/
    ├── coord_transform.py   # Pure math: WGS84 ↔ GCJ02 ↔ BD09 (no arcpy deps)
    ├── core_common.py       # Key pool (round-robin + delay), speed→worker calc, shared helpers
    ├── core_address_geocode.py  # Forward geocode: address → lat/lng, concurrent
    ├── core_reverse_geocode.py  # Reverse geocode: point → nearby POIs (long table)
    ├── core_poi_search.py       # POI search within an extent (Amap recursive split + Baidu)
    ├── core_coord_transform.py  # Batch point coordinate system conversion
    ├── core_admin_area.py       # Admin boundary export (province→city→county cascade)
    ├── core_inputtips.py        # Address/POI input suggestion
    ├── core_route_planning.py   # Point-to-point route planning
    └── core_od_matrix.py        # Baidu OD (origin-destination) matrix
```

**Key management**: `core_common.py` maintains a thread-safe key pool per platform. Each key rotates round-robin. When a key hits rate/concurrency/auth errors, it gets a delay penalty (1s–3600s) and the pool skips to the next key. Keys are loaded at module-import time; if zero valid keys exist, import raises `RuntimeError`.

**Coordinate pipeline**: Baidu APIs return in BD09/GCJ02, Amap in GCJ02. Output can be WGS84 or GCJ02. Conversions are in `coord_transform.py` (pure math). `core_common.py` re-exports them and adds `convert_coord()` as a unified entrypoint. CGCS2000 is treated as numerically equivalent to WGS84 (approximated).

**Concurrency**: `core_common.calculate_max_workers(speed_level, num_keys, ...)` maps a UI speed choice to `ThreadPoolExecutor` worker count via key-count × 30 QPS × multiplier. The UI currently exposes two levels ("低/中"), but the function supports four (low/medium/fast/fastest with 0.25/0.50/0.75/1.0 multipliers).

**Module hot-reload**: The `.pyt` file calls `_reload_core_modules()` at the top and before each tool's `execute()` — it uses `importlib.reload` so ArcGIS picks up changes without restarting.

## The 6 tools (in the .pyt)

| Label | Class | What it does |
|-------|-------|-------------|
| A | `AdminAreaBoundaryExport` | Province/city/county cascading dropdown → boundary polygons |
| B | `AmapPoiInExtent` | Amap POI search within polygon/screen/manual extent, recursive split |
| C | `BaiduPoiInExtent` | Baidu POI 3.0 search within extent (requires keyword query) |
| D | `GeocodeToWgs84Points` | Address table → point features (Baidu/Amap/both) |
| E | `ReverseGeocodeToPoi` | Point input → nearby POI long table (1 row per POI per point) |
| F | `CoordTransformPoints` | Batch WGS84/GCJ02/BD09/CGCS2000 coordinate conversion |

## Key conventions

- Output is always to FileGDB (`.gdb`), `in_memory`, or `memory` workspace. The `use_in_memory` boolean parameter toggles this. Note: `core_coord_transform.py` uses `"memory"` instead of `"in_memory"` as the memory workspace identifier.
- Output spatial reference is always written as WGS84 (EPSG:4326), even when coordinate values are GCJ02. Exception: CGCS2000 output writes EPSG:4490.
- Field names copied from input are prefixed with `IN_`.
- Address normalization: multi-address strings separated by `；`, `;`, or newlines are split, and only the first segment is used.
- Amap POI search implements recursive quadrant split when a single request hits the 25/page × 100 page limit, with a "recovery round" that re-attempts unfinished rectangles at greater split depth.

## Adding a new tool

1. Write the core logic in `core/core_*.py` following existing patterns (import `core_common`, use `ThreadPoolExecutor`, call `common.get_output_path()`).
2. Add a class in `百度高德Geocode.pyt` with `getParameterInfo()`, `updateParameters()`, `updateMessages()`, `execute()`.
3. Add it to the `Toolbox.tools` list.
4. Add the import at the top of the `.pyt` and to `_reload_core_modules()`.
