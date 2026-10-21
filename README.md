# NDVI Extractor v2 for QGIS

An end-to-end Sentinel-2 Level-2A NDVI workflow for QGIS 3.34+.

## Workflow

1. Choose a polygon AOI layer in the current QGIS project.
2. Optionally restrict the AOI to selected features.
3. Choose a date range and maximum scene cloud cover.
4. Search the public Element84 Earth Search STAC catalog for Sentinel-2 L2A scenes.
5. Select one scene, or several (Ctrl/Shift-click) to build a mosaic. The table shows how much of the AOI each
   scene's *data footprint* covers; **Auto-select to cover AOI** picks the fewest, least cloudy scenes.
6. Download the AOI window of each scene's `red`, `nir` and (for cloud masking) `scl` Cloud-Optimized GeoTIFF assets.
7. Apply STAC raster scale/offset metadata where provided and calculate:

   `NDVI = (NIR - Red) / (NIR + Red)`

8. Mask clouds, cloud shadow and cirrus using the scene classification layer (SCL), then - when several scenes are
   selected - combine them into one mosaic and clip it to the AOI.
9. Run QGIS zonal statistics: count, mean, standard deviation, minimum, maximum.
10. Export the zonal attributes to CSV and optionally add the NDVI raster + zonal vector layer to the project.

## Output layout

```text
ndvi_output/
├── ndvi_<scene or mosaic_Nscenes_date_hash>.tif
├── ndvi_<...>_zonal_stats.csv
├── ndvi_<...>_scenes.csv          which scenes were used and how much of the AOI each supplied
└── _bands/
    └── <scene>/
        ├── red_aoi_<aoiHash>.tif
        ├── nir_aoi_<aoiHash>.tif
        └── scl_aoi_<aoiHash>.tif
```

The `_bands` directory is retained so downloaded inputs can be reused without downloading them again.

## Reflectance offset

Sentinel-2 L2A metadata (processing baseline 04.00 and newer) announces a reflectance offset of -0.1, i.e. DN 1000 means
zero reflectance. Some data sources have already removed it, so the metadata can disagree with the pixels; applying a
non-existent offset pushes red below zero and **saturates NDVI at 1.0** (bare soil included).

| Mode | Behaviour |
|---|---|
| Auto-detect (default) | Applies the metadata offset unless more than 15% of the valid red pixels are below 1000 DN, which would mean negative reflectance; then it is ignored |
| Use the scene metadata | Always applies the metadata offset |
| None | DN x scale only |

The offset used for each scene is written to the log and to the `reflectance_offset` column of `*_scenes.csv`.
Auto-detect needs some dark pixels (vegetation, water, shadow) to decide; on a uniformly bright scene it falls back to the
metadata value.

## Cloud masking

Uses the Sentinel-2 Scene Classification Layer (20 m, resampled to 10 m). Masked pixels are left empty (nodata),
so a mosaic can fill them from other scenes.

| Mode | SCL classes masked |
|---|---|
| Off | none |
| Standard (default) | 1 saturated/defective, 3 cloud shadow, 8 and 9 cloud, 10 thin cirrus |
| Strict | Standard + 2 dark area, 7 unclassified, 11 snow |

"Expand mask by" grows the mask by N pixels (10 m each) to catch hazy cloud edges. Pixels with no satellite data
(DN = 0, e.g. outside the satellite swath) are always left empty.

## Mosaicking

Select several scenes: different tiles of one date, different dates, or both. Scenes may be in different UTM zones;
they are put on one common 10 m grid in the CRS of the highest-priority scene.

| Method | Result per pixel |
|---|---|
| First valid pixel (default) | NDVI of the first scene, in priority order, that has a valid value there |
| Median of valid pixels | median over all scenes with a valid value |
| Maximum NDVI | maximum over all scenes with a valid value |

Priority (used by *First valid pixel* and in the report): *Least cloudy first* or *Newest first*. A selected scene
that does not overlap the AOI is skipped with a note in the log.

After each run the log and `*_scenes.csv` report the share of the AOI polygon that has a valid NDVI value and what
each scene contributed. (For *Median* and *Maximum*, `aoi_pixels_contributed` counts the valid pixels each scene
offered, so the column can add up to more than the AOI.)

## Satellite data source

Earth Search STAC API:
`https://earth-search.aws.element84.com/v1`

Collection:
`sentinel-2-l2a`

Earth Search exposes Sentinel-2 L2A spectral assets using common names such as `red` (B04) and `nir` (B08). The service provides Cloud-Optimized GeoTIFF assets and is publicly accessible without credentials.

## Requirements

- QGIS 3.34 or newer
- QGIS Processing provider enabled
- Network access to Earth Search and the AWS-hosted raster assets
- NumPy and GDAL as shipped with QGIS

## Important notes

- Search results are scenes intersecting the AOI bounding box. The *AOI cover %* column uses each scene's data footprint, so swath-edge scenes (mostly no-data) are easy to spot; select several scenes or use *Auto-select* to cover the whole AOI.
- Downloading full Red/NIR assets can consume substantial disk space. Existing files in `_bands/<scene>/` are reused.
- The search cloud filter uses the scene-level `eo:cloud_cover` value; pixel-level masking is done by the SCL option above. The SCL is a classification, not perfect: thin haze and small clouds can slip through, so inspect the result.
- Mosaics of different dates mix acquisition dates; use *Newest first* or a narrow date range when the date matters.
- A mosaic window is limited to about 50 megapixels at 10 m (about 22 x 22 km).
- Earth Search is a public best-effort catalog. For production systems, consider adding a provider fallback and retry logic.


## QGIS 4 compatibility

Version 2.1.1 uses Qt/PyQt enum compatibility helpers for QGIS 3.x and QGIS 4.x. QGIS 4 uses PyQt6, where several widget and shader enums are nested under their enum classes.
