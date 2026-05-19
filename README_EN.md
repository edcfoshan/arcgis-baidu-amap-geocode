# arcgis-baidu-amap-geocode

English | [中文](README.md)

An **ArcGIS Pro** Python toolbox (`.pyt`) integrating Baidu Maps and Amap (Gaode) Web Service APIs, providing geocoding, POI search, reverse geocoding, administrative boundary export, and coordinate system conversion for Chinese coordinate systems.

## Requirements

| Item | Requirement |
|------|-------------|
| **Software** | ArcGIS Pro **3.0 or later** (does NOT support ArcMap / ArcGIS 10.x) |
| **Python** | Bundled with ArcGIS Pro (3.x), no separate installation needed |
| **API Key** | Baidu Maps Key and/or Amap Key (**must apply and fill in yourself**, no built-in keys) |

> This toolbox is built on ArcGIS Pro 3.x with Python 3. It cannot run under ArcMap or ArcGIS Desktop 10.x.

## Features

| Tool | Description |
|------|-------------|
| **A_Admin Boundary Export** | Cascading province/city/district selection, export administrative boundary polygons (Baidu/Amap dual-platform) |
| **B_Amap POI Batch Collection** | Collect Amap POI data by keyword or category within a specified extent, auto-recursive splitting for high-density areas |
| **C_Baidu Place Search 3.0** | Baidu POI batch collection with rectangular area search |
| **D_Geocoding_Address to Points** | Input address table, batch geocode via Baidu/Amap, output point feature class (dual-platform mode supported) |
| **E_Reverse Geocoding_Point to POI** | Input point coordinates, output nearby POI list (long table format, one row per POI) |
| **F_Coordinate Transform** | Batch conversion between WGS84 / GCJ02 / BD09 / CGCS2000 |

## Installation

1. Clone or download this repository to a local directory
2. In ArcGIS Pro, open the **Catalog** pane → **Toolboxes** → right-click → **Add Toolbox**, select `百度高德Geocode.pyt`
   ![Add Toolbox](截图/1添加工具箱.png)
   *Right-click "Toolboxes" in the Catalog pane, select "Add Toolbox"*
3. In the file browser dialog, select `百度高德Geocode.pyt` and click OK
   ![Select File](截图/2点击工具箱.png)
   *Browse to the repository directory, select the .pyt file and click OK*
4. A third-party code confirmation dialog will appear, click **Yes** to allow
   ![Confirm](截图/3确定运行.png)
   *Click "Yes" to allow the toolbox code to run*
5. The toolbox is now added. Expand it to see all 6 tools (A–F)
   ![Tool List](截图/4工具列表.png)
   *The toolbox contains 6 geospatial tools: admin boundary export, POI collection, geocoding, reverse geocoding, coordinate transform*
6. **Configure API Keys before first use** (see below)

## API Key Configuration (Required for First Use)

The toolbox does not include any API keys. **You must apply for and fill in your own keys before using any tool.** For detailed configuration, see [CONFIG.md](CONFIG.md).

### Setup Steps

1. **Apply for API Keys**
   - Baidu: https://lbsyun.baidu.com/apiconsole/key → Create a "Server-side" application
   - Amap: https://console.amap.com/dev/key/app → Create a "Web Service" application

2. **Locate the config files** — In the `config/` folder of the toolbox directory:
   ```
   arcgis-baidu-amap-geocode/
   └── config/
       ├── baidu_keys.txt    ← Fill Baidu key here
       └── amap_keys.txt     ← Fill Amap key here
   ```

3. **Open with Notepad** `baidu_keys.txt` or `amap_keys.txt`, the content looks like:
   ```
   # Baidu Maps API Key Configuration
   # ...
   # How to use:
   #   Remove the # at the beginning of the line, replace your_baidu_key with your real Key
   #   One key per line, multiple keys improve speed
   #
   #your_baidu_key_here
   ```

4. **Remove the `#` prefix and replace with your real key**, for example:
   ```
   AbCdEf1234567890GhIjKlMnOpQrStUv
   ```
   - One key per line; lines starting with `#` are comments
   - Multiple keys can be configured to improve concurrency and avoid single-key quota limits

5. Save the file and re-run the tool

> **Configure as needed**: If you only use Amap tools, just fill in `amap_keys.txt`; for Baidu only, just fill in `baidu_keys.txt`.

## Coordinate Systems

Common coordinate systems in China and their relationships:

| Coordinate System | Alias | Description |
|-------------------|-------|-------------|
| **WGS84** | EPSG:4326 | International standard, used by GPS |
| **GCJ-02** | Mars Coordinate | China National Administration standard, used by Amap, Tencent |
| **BD-09** | Baidu Coordinate | Baidu Maps proprietary, double-encrypted on top of GCJ-02 |
| **CGCS2000** | China Geodetic 2000 | China's official geodetic datum, approximately equal to WGS84 (treated as equivalent in this toolbox) |

Conversion chain: BD-09 → GCJ-02 → WGS84 ↔ CGCS2000

## Concurrency & Speed Levels

The toolbox provides two processing speed levels:

- **Low (Stable)**: Uses 25% of theoretical key concurrency, suitable for free-tier personal developer quotas
- **Medium (Balanced)**: Uses 50% of theoretical key concurrency

The number of concurrent threads is automatically calculated based on the number of keys and the speed level (estimated at 30 QPS per key).

## Notes

- Output spatial reference is uniformly set to WGS84 (EPSG:4326). When GCJ-02 output is selected, the coordinate values remain GCJ-02 but the spatial reference field is still WGS84
- Amap POI batch collection automatically recursively splits into 4 sub-quadrants when a single area exceeds 25×page trigger count
- Baidu POI Search 3.0 API **requires** a keyword (query); empty-keyword full search is not supported
- For output GDB, use File Geodatabase (directory ending with .gdb) or check "Use temporary workspace"

## Support the Author

If you find this toolbox helpful, your support is appreciated!

![Support](关注、赞赏码.png)

## License

[MIT](LICENSE)
