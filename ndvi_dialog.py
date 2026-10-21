import csv
import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.request
import warnings
from datetime import datetime
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QDate, QCoreApplication, QEvent, QItemSelectionModel, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
    QDateEdit, QDoubleSpinBox, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QLineEdit, QFileDialog, QMessageBox, QProgressBar,
    QTextEdit, QGroupBox, QAbstractItemView, QApplication, QSpinBox
)
from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsDistanceArea, QgsFeatureRequest, QgsField, QgsFields, QgsRasterLayer,
    QgsVectorLayer, QgsVectorFileWriter, QgsWkbTypes, QgsProject,
    QgsProcessingFeedback, QgsProcessingAlgRunnerTask, QgsProcessingContext,
    QgsRectangle, QgsTask, QgsMessageLog, Qgis, QgsGeometry
)
from qgis.PyQt.QtCore import QVariant

from osgeo import gdal, ogr, osr
import numpy as np
import processing


STAC_URL = 'https://earth-search.aws.element84.com/v1/search'
# Below this reflectance sum (red + NIR) a pixel carries no usable signal (deep shadow / black water).
MIN_SIGNAL = 0.01
COLLECTION = 'sentinel-2-l2a'
MAX_WINDOW_PIXELS = 50_000_000
DOWNLOAD_ATTEMPTS = 3       # per band: transient network failures are retried

# Sentinel-2 L2A (baseline >= 04.00) metadata announces a reflectance offset of -0.1 (DN = reflectance * 10000 + 1000).
# Some data sources already removed it, so the metadata can disagree with the pixels. With the offset really present,
# almost no valid red pixel is below 1000 DN; if more than this share is, the offset cannot be in the data.
OFFSET_ABSENT_SHARE = 0.15
OFFSET_MODES = [
    ('auto', 'Auto-detect (recommended)'),
    ('metadata', 'Use the scene metadata'),
    ('none', 'None (DN \u00d7 scale only)'),
]

# Cloud masking uses the Sentinel-2 Scene Classification Layer (SCL).
# 1 saturated/defective, 2 dark area, 3 cloud shadow, 7 unclassified, 8/9 cloud (medium/high), 10 thin cirrus, 11 snow
CLOUD_MODES = [
    ('off', 'Off'),
    ('standard', 'Standard (cloud, shadow, cirrus)'),
    ('strict', 'Strict (also dark, unclassified, snow)'),
]
SCL_CLASSES = {'standard': (1, 3, 8, 9, 10), 'strict': (1, 2, 3, 7, 8, 9, 10, 11)}
MOSAIC_METHODS = [
    ('first', 'First valid pixel (by priority)'),
    ('median', 'Median of valid pixels'),
    ('max', 'Maximum NDVI'),
]


class SilentFeedback(QgsProcessingFeedback):
    def __init__(self, log_fn):
        super().__init__()
        self.log_fn = log_fn

    def pushInfo(self, info):
        self.log_fn(str(info))

    def pushCommandInfo(self, info):
        self.log_fn(str(info))

    def reportError(self, error, fatalError=False):
        self.log_fn('ERROR: ' + str(error))

    def setProgressText(self, text):
        self.log_fn(str(text))


class WorkflowCancelled(Exception):
    """Raised inside a background worker when the user pressed Cancel."""


class NonRetryableError(RuntimeError):
    """An error that retrying cannot fix (for example an AOI that is too large)."""


class SceneSkipped(Exception):
    """A scene that cannot contribute (e.g. it does not overlap the AOI). Skipped in multi-scene runs."""


class NDVIDialog(QDialog):
    # Signals let background threads update the GUI safely (they are delivered on the GUI thread).
    log_message = pyqtSignal(str)
    progress_changed = pyqtSignal(int)

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.selected_item = None
        self.scene_rows = []
        self.selected_items = []     # all selected scenes (several = mosaic)
        self._scene_geoms = []       # scene footprint intersected with the AOI (EPSG:4326), parallel to scene_rows
        self._aoi_geom = None
        self._busy = False
        self._operation = None       # 'search' or 'workflow' while busy
        self._search_token = 0       # lets a cancelled search's late result be ignored
        self._shutting_down = False
        self._cancel_event = threading.Event()
        self._task = None            # QgsTask / QgsProcessingAlgRunnerTask currently running
        self._worker_task = None     # the QgsTask seen by the worker thread (for progress/cancel)
        self._last_progress = -1
        self._pending = {}
        self._zonal_context = None
        self._zonal_feedback = None
        self._zonal_aoi = None
        self.setWindowTitle('NDVI Extractor v2 \u2014 Sentinel-2')
        self.setMinimumSize(980, 840)
        self._build_ui()
        self.log_message.connect(self._append_log)
        self.progress_changed.connect(self._set_progress)
        self._refresh_aoi_layers()

    def _build_ui(self):
        root = QVBoxLayout(self)

        search_box = QGroupBox('1. AOI and image search')
        grid = QGridLayout(search_box)
        grid.addWidget(QLabel('AOI polygon layer:'), 0, 0)
        self.aoi_combo = QComboBox()
        grid.addWidget(self.aoi_combo, 0, 1, 1, 3)
        self.selected_only = QCheckBox('Use selected features only')
        self.selected_only.setChecked(False)
        grid.addWidget(self.selected_only, 0, 4)

        grid.addWidget(QLabel('Start date:'), 1, 0)
        self.start_date = QDateEdit(QDate.currentDate().addMonths(-1))
        self.start_date.setCalendarPopup(True)
        grid.addWidget(self.start_date, 1, 1)
        grid.addWidget(QLabel('End date:'), 1, 2)
        self.end_date = QDateEdit(QDate.currentDate())
        self.end_date.setCalendarPopup(True)
        grid.addWidget(self.end_date, 1, 3)
        grid.addWidget(QLabel('Cloud cover ≤'), 1, 4)
        self.cloud_spin = QDoubleSpinBox()
        self.cloud_spin.setRange(0, 100)
        self.cloud_spin.setDecimals(1)
        self.cloud_spin.setValue(20)
        self.cloud_spin.setSuffix(' %')
        grid.addWidget(self.cloud_spin, 1, 5)

        self.search_btn = QPushButton('Search Sentinel-2')
        self.search_btn.clicked.connect(self.search_scenes)
        grid.addWidget(self.search_btn, 2, 0, 1, 2)
        self.refresh_aoi_btn = QPushButton('Refresh AOI list')
        self.refresh_aoi_btn.clicked.connect(self._refresh_aoi_layers)
        grid.addWidget(self.refresh_aoi_btn, 2, 2)
        root.addWidget(search_box)

        table_box = QGroupBox('2. Available scenes — select one, or several (Ctrl/Shift-click) to mosaic')
        table_layout = QVBoxLayout(table_box)
        self.results = QTableWidget(0, 9)
        self.results.setHorizontalHeaderLabels([
            'Date', 'Cloud %', 'Satellite', 'Tile', 'AOI cover %', 'Scene ID', 'Red', 'NIR', 'SCL'
        ])
        # PyQt6/QGIS 4 moved Qt widget enums under the base enum classes.
        # Keep a fallback for QGIS 3.x/PyQt5 installations.
        selection_rows = getattr(getattr(QAbstractItemView, 'SelectionBehavior', QAbstractItemView), 'SelectRows')
        no_edit_triggers = getattr(getattr(QAbstractItemView, 'EditTrigger', QAbstractItemView), 'NoEditTriggers')
        self.results.setSelectionBehavior(selection_rows)
        self.results.setEditTriggers(no_edit_triggers)
        self.results.setAlternatingRowColors(True)
        extended = getattr(getattr(QAbstractItemView, 'SelectionMode', QAbstractItemView), 'ExtendedSelection')
        self.results.setSelectionMode(extended)
        header = self.results.horizontalHeader()
        resize_to_contents = getattr(getattr(QHeaderView, 'ResizeMode', QHeaderView), 'ResizeToContents')
        header.setSectionResizeMode(resize_to_contents)
        stretch = getattr(getattr(QHeaderView, 'ResizeMode', QHeaderView), 'Stretch')
        header.setSectionResizeMode(5, stretch)
        self.results.itemSelectionChanged.connect(self._scene_selected)
        table_layout.addWidget(self.results)
        root.addWidget(table_box, 1)

        process_box = QGroupBox('3. Download, calculate, zonal statistics, export')
        pg = QGridLayout(process_box)
        pg.addWidget(QLabel('Output folder:'), 0, 0)
        self.output_edit = QLineEdit(self._default_output_folder())
        pg.addWidget(self.output_edit, 0, 1, 1, 4)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self.choose_output)
        pg.addWidget(browse, 0, 5)

        pg.addWidget(QLabel('Output prefix:'), 1, 0)
        self.prefix_edit = QLineEdit('ndvi')
        pg.addWidget(self.prefix_edit, 1, 1)
        self.add_to_project = QCheckBox('Add NDVI raster and zonal layer to QGIS')
        self.add_to_project.setChecked(True)
        pg.addWidget(self.add_to_project, 1, 2, 1, 3)

        pg.addWidget(QLabel('Cloud masking:'), 2, 0)
        self.cloud_combo = QComboBox()
        for key, label in CLOUD_MODES:
            self.cloud_combo.addItem(label, key)
        self.cloud_combo.setCurrentIndex(1)   # standard
        self.cloud_combo.setToolTip('Uses the scene classification layer (SCL). Masked pixels are left empty, '
                                    'so a mosaic can fill them from other scenes.')
        pg.addWidget(self.cloud_combo, 2, 1, 1, 2)
        pg.addWidget(QLabel('Expand mask by:'), 2, 3)
        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(0, 10)
        self.buffer_spin.setValue(2)
        self.buffer_spin.setSuffix(' px (10 m)')
        self.buffer_spin.setToolTip('Grows the cloud/shadow mask to catch hazy cloud edges.')
        pg.addWidget(self.buffer_spin, 2, 4, 1, 2)

        pg.addWidget(QLabel('Several scenes:'), 3, 0)
        self.method_combo = QComboBox()
        for key, label in MOSAIC_METHODS:
            self.method_combo.addItem(label, key)
        pg.addWidget(self.method_combo, 3, 1, 1, 2)
        pg.addWidget(QLabel('Priority:'), 3, 3)
        self.priority_combo = QComboBox()
        self.priority_combo.addItem('Least cloudy first', 'cloud')
        self.priority_combo.addItem('Newest first', 'newest')
        pg.addWidget(self.priority_combo, 3, 4)
        self.auto_btn = QPushButton('Auto-select to cover AOI')
        self.auto_btn.setToolTip('Picks the fewest, least cloudy scenes whose footprints cover the AOI.')
        self.auto_btn.clicked.connect(self.auto_select_scenes)
        pg.addWidget(self.auto_btn, 3, 5)

        pg.addWidget(QLabel('Reflectance offset:'), 4, 0)
        self.offset_combo = QComboBox()
        for key, label in OFFSET_MODES:
            self.offset_combo.addItem(label, key)
        self.offset_combo.setToolTip('Sentinel-2 metadata may announce a -0.1 reflectance offset that the pixels do not contain. '
                                     'Auto-detect checks the data and ignores the offset when it is clearly absent '
                                     '(otherwise NDVI saturates at 1.0). Each run logs and reports the offset it used.')
        pg.addWidget(self.offset_combo, 4, 1, 1, 5)

        pg.addWidget(QLabel('Workflow:'), 5, 0)
        self.workflow_label = QLabel('Search for scenes and select one (or several to mosaic), then run the workflow.')
        self.workflow_label.setWordWrap(True)
        pg.addWidget(self.workflow_label, 5, 1, 1, 5)
        root.addWidget(process_box)

        button_row = QHBoxLayout()
        self.run_btn = QPushButton('Run complete NDVI workflow')
        self.run_btn.clicked.connect(self.run_workflow)
        self.run_btn.setMinimumHeight(36)
        button_row.addWidget(self.run_btn)
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_running)
        button_row.addWidget(self.cancel_btn)
        self.clear_btn = QPushButton('Clear log')
        self.clear_btn.clicked.connect(lambda: self.log_edit.clear())
        button_row.addWidget(self.clear_btn)
        root.addLayout(button_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        root.addWidget(self.progress)
        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(150)
        root.addWidget(self.log_edit)

    def _default_output_folder(self):
        home = QgsProject.instance().homePath()
        if home and os.path.isdir(home):
            return os.path.join(home, 'ndvi_output')
        return os.path.join(os.path.expanduser('~'), 'ndvi_output')

    def _refresh_aoi_layers(self):
        current = self.aoi_combo.currentData() if self.aoi_combo.count() else None
        self.aoi_combo.clear()
        polygon_geometry = getattr(QgsWkbTypes, 'PolygonGeometry', None)
        if polygon_geometry is None:
            from qgis.core import Qgis
            polygon_geometry = Qgis.GeometryType.Polygon
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer) and QgsWkbTypes.geometryType(layer.wkbType()) == polygon_geometry:
                self.aoi_combo.addItem(layer.name(), layer.id())
        if current:
            idx = self.aoi_combo.findData(current)
            if idx >= 0:
                self.aoi_combo.setCurrentIndex(idx)

    def _aoi_layer(self):
        layer_id = self.aoi_combo.currentData()
        if not layer_id:
            raise RuntimeError('Select a polygon AOI layer.')
        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer or not layer.isValid():
            raise RuntimeError('The selected AOI layer is invalid.')
        if self.selected_only.isChecked() and layer.selectedFeatureCount() == 0:
            raise RuntimeError('Selected-features-only is enabled, but no AOI features are selected.')
        return layer

    def _aoi_bbox_wgs84(self, layer):
        extent = None
        if self.selected_only.isChecked():
            for feat in layer.selectedFeatures():
                box = feat.geometry().boundingBox()
                if extent is None:
                    extent = box
                else:
                    extent.combineExtentWith(box)
            if extent is None:
                raise RuntimeError('No selected AOI features are available.')
        else:
            extent = layer.extent()
        if layer.crs().authid() != 'EPSG:4326':
            transform = QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
            extent = transform.transformBoundingBox(extent)
        import math
        bbox = [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()]
        if extent.isNull() or extent.isEmpty() or any(not math.isfinite(v) for v in bbox):
            raise RuntimeError('AOI extent is empty or invalid (layer has no features or bad CRS).')
        # clamp to valid WGS84 range and pad degenerate boxes (e.g. a single point)
        bbox = [max(-180.0, bbox[0]), max(-90.0, bbox[1]), min(180.0, bbox[2]), min(90.0, bbox[3])]
        if bbox[0] >= bbox[2]:
            bbox[0] -= 1e-4; bbox[2] += 1e-4
        if bbox[1] >= bbox[3]:
            bbox[1] -= 1e-4; bbox[3] += 1e-4
        return [round(v, 6) for v in bbox]

    def _working_aoi_layer(self, layer):
        if not self.selected_only.isChecked():
            return layer
        crs_authid = layer.crs().authid() or layer.crs().toWkt()
        mem = QgsVectorLayer(f'Polygon?crs={crs_authid}', 'NDVI_AOI_selected', 'memory')
        provider = mem.dataProvider()
        provider.addAttributes(list(layer.fields()))
        mem.updateFields()
        feats = [feat for feat in layer.selectedFeatures()]
        provider.addFeatures(feats)
        mem.updateExtents()
        if not mem.featureCount():
            raise RuntimeError('Selected-features-only is enabled, but no selected AOI features were copied.')
        return mem

    def search_scenes(self):
        if self._busy:
            return
        try:
            layer = self._aoi_layer()
            if self.start_date.date() > self.end_date.date():
                raise RuntimeError('Start date must be on or before end date.')
            bbox = self._aoi_bbox_wgs84(layer)
            aoi_geom = self._aoi_geometry_wgs84(layer)
            start = self.start_date.date().toString('yyyy-MM-dd') + 'T00:00:00Z'
            end = self.end_date.date().toString('yyyy-MM-dd') + 'T23:59:59Z'
            max_cloud = float(self.cloud_spin.value())
            body = {
                'collections': [COLLECTION],
                'bbox': bbox,
                'datetime': f'{start}/{end}',
                'limit': 100,
                'query': {'eo:cloud_cover': {'lte': max_cloud}}
            }
        except Exception as exc:
            self._error(str(exc))
            return
        self._log('Searching Earth Search\u2026')
        self._log('Request body: ' + json.dumps(body))
        self.results.setRowCount(0)
        self.scene_rows = []
        self.selected_item = None
        self.selected_items = []
        self._scene_geoms = []
        self._aoi_geom = aoi_geom
        self._search_token += 1
        token = self._search_token
        self._start_task('NDVI Extractor: searching Sentinel-2 scenes', self._search_worker,
                         (body, max_cloud, 10),
                         lambda exc, value=None, t=token: self._search_finished(t, exc, value),
                         indeterminate=True, operation='search')

    def _search_worker(self, task, body, max_cloud, max_pages):
        """Background thread: only talks to the network, never touches widgets or the project."""
        self._worker_task = task
        try:
            raw_features = []
            notes = []
            page_body = body
            for _page in range(max_pages):
                self._check_cancel()
                data = self._stac_post(page_body)
                raw_features.extend(data.get('features', []))
                next_link = next((l for l in data.get('links', []) if l.get('rel') == 'next'), None)
                if not next_link or not next_link.get('body'):
                    break
                page_body = {**body, **next_link['body']} if next_link.get('merge') else next_link['body']
            else:
                notes.append(f'Stopped after {max_pages} result pages ({len(raw_features)} scenes). '
                             'Narrow the dates or AOI to see more.')
            # Safety net: the server already filters cloud cover, re-check locally.
            features = [
                f for f in raw_features
                if float(f.get('properties', {}).get('eo:cloud_cover', 0) or 0) <= max_cloud
            ]
            # sort newest first on the client (server-side sortby on 'datetime' fails on Earth Search)
            features.sort(key=lambda f: f.get('properties', {}).get('datetime') or '', reverse=True)
            return {'features': features, 'notes': notes}
        except WorkflowCancelled:
            return {'cancelled': True}
        except Exception as exc:
            if self._is_cancelled():
                return {'cancelled': True}
            return {'error': str(exc)}

    def _search_finished(self, token, exception, value=None):
        """GUI thread: called when the background search ends."""
        if self._shutting_down or token != self._search_token:
            return   # dialog is closing, or this search was cancelled and its late result is stale
        self._task = None
        self._worker_task = None
        self._set_busy(False)
        result = value if isinstance(value, dict) else {}
        try:
            if result.get('cancelled') or (not result and exception is not None and self._cancel_event.is_set()):
                self._log('Search cancelled.')
                return
            if not result:
                raise RuntimeError(str(exception) if exception is not None else 'The search ended without a result.')
            if result.get('error'):
                raise RuntimeError(result['error'])
            for note in result.get('notes', []):
                self._log(note)
            self._populate_scenes(result.get('features', []))
        except Exception as exc:
            self._error(str(exc))

    def _populate_scenes(self, features):
        self.scene_rows = []
        self._scene_geoms = []
        for feature in features:
            assets = feature.get('assets', {})
            if 'red' not in assets or 'nir' not in assets:
                continue
            props = feature.get('properties', {})
            dt = props.get('datetime') or props.get('start_datetime') or ''
            cloud = props.get('eo:cloud_cover', '')
            platform = props.get('platform', props.get('constellation', ''))
            tile = self._tile_label(props)
            geom, cover = self._footprint_cover(feature)
            self.scene_rows.append(feature)
            self._scene_geoms.append(geom)
            row = self.results.rowCount()
            self.results.insertRow(row)
            values = [
                str(dt).replace('T', ' ')[:19],
                '' if cloud == '' else f'{float(cloud):.1f}',
                str(platform),
                str(tile),
                '' if cover is None else f'{cover:.0f}',
                str(feature.get('id', '')),
                'OK', 'OK', 'OK' if 'scl' in assets else '\u2013'
            ]
            for col, value in enumerate(values):
                self.results.setItem(row, col, QTableWidgetItem(value))
        self._log(f'Found {len(self.scene_rows)} Sentinel-2 scenes with Red/NIR assets.')
        if self.scene_rows:
            self.results.selectRow(0)
        else:
            self._log('No scenes matched the AOI, date range, cloud filter, and required assets.')

    def _aoi_geometry_wgs84(self, layer):
        """AOI as one geometry in EPSG:4326, used only for footprint-coverage estimates in the table."""
        if self.selected_only.isChecked():
            count, feats = layer.selectedFeatureCount(), layer.selectedFeatures()
        else:
            count, feats = layer.featureCount(), layer.getFeatures()
        if count > 2000:
            geom = QgsGeometry.fromRect(layer.extent())      # too many polygons to union quickly
        else:
            geoms = [QgsGeometry(f.geometry()) for f in feats
                     if f.geometry() is not None and not f.geometry().isNull() and not f.geometry().isEmpty()]
            if not geoms:
                return None
            geom = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
        if layer.crs().authid() != 'EPSG:4326':
            geom.transform(QgsCoordinateTransform(layer.crs(), QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance()))
        if not geom.isGeosValid():
            geom = geom.makeValid()
        return geom

    def _footprint_cover(self, feature):
        """(footprint intersected with the AOI, % of the AOI covered by the scene's DATA footprint).
        Earth Search item geometries follow the real data footprint, so this reveals swath-edge scenes
        before anything is downloaded."""
        if self._aoi_geom is None or not feature.get('geometry'):
            return None, None
        try:
            ogr_geom = ogr.CreateGeometryFromJson(json.dumps(feature['geometry']))
            footprint = QgsGeometry.fromWkt(ogr_geom.ExportToWkt())
            inter = self._aoi_geom.intersection(footprint)
            area = self._aoi_geom.area()
            return inter, (inter.area() / area * 100.0 if area > 0 else None)
        except Exception:
            return None, None

    def _union_cover(self, rows):
        geoms = [self._scene_geoms[r] for r in rows if r < len(self._scene_geoms) and self._scene_geoms[r] is not None]
        if not geoms or self._aoi_geom is None or self._aoi_geom.area() <= 0:
            return None
        union = QgsGeometry.unaryUnion(geoms) if len(geoms) > 1 else geoms[0]
        return union.area() / self._aoi_geom.area() * 100.0

    def _select_rows(self, rows):
        flag = getattr(QItemSelectionModel, 'SelectionFlag', QItemSelectionModel)
        flags = getattr(flag, 'Select') | getattr(flag, 'Rows')
        self.results.clearSelection()
        model = self.results.model()
        selection_model = self.results.selectionModel()
        for r in rows:
            selection_model.select(model.index(r, 0), flags)

    def auto_select_scenes(self):
        """Greedy cover: repeatedly add the scene giving the most NEW AOI coverage, weighted by (1 - cloud cover)."""
        if not self.scene_rows or self._aoi_geom is None or self._aoi_geom.area() <= 0:
            self._error('Search for scenes first (the AOI footprint is needed to plan the coverage).')
            return
        area = self._aoi_geom.area()
        pool = [i for i, g in enumerate(self._scene_geoms) if g is not None and not g.isEmpty()]
        covered, chosen = None, []
        while pool and len(chosen) < 8:
            best = None
            for i in pool:
                new = self._scene_geoms[i] if covered is None else self._scene_geoms[i].difference(covered)
                gain = new.area() / area
                if gain < 0.01:
                    continue
                cloud = float(self.scene_rows[i].get('properties', {}).get('eo:cloud_cover', 0) or 0)
                score = gain * (1.0 - cloud / 100.0)
                if best is None or score > best[0]:
                    best = (score, i)
            if best is None:
                break
            i = best[1]
            chosen.append(i)
            pool.remove(i)
            covered = self._scene_geoms[i] if covered is None else covered.combine(self._scene_geoms[i])
            if covered.area() / area >= 0.995:
                break
        if not chosen:
            self._log('No scene footprint overlaps the AOI.')
            return
        self._select_rows(sorted(chosen))
        self._log(f'Auto-selected {len(chosen)} scene(s); their footprints cover about '
                  f'{covered.area() / area * 100:.0f}% of the AOI.')

    def _stac_post(self, body):
        req = urllib.request.Request(
            STAC_URL,
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'Accept': 'application/geo+json'},
            method='POST')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as http_err:
            detail = http_err.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'HTTP {http_err.code} from Earth Search: {detail}')

    @staticmethod
    def _tile_label(props):
        """Full MGRS tile (e.g. 48MXU). Earth Search exposes it as three separate properties."""
        tile = props.get('s2:mgrs_tile')
        if tile:
            return str(tile)
        zone = props.get('mgrs:utm_zone')
        band = props.get('mgrs:latitude_band', '') or ''
        square = props.get('mgrs:grid_square', '') or ''
        if zone is not None and (band or square):
            return f'{int(zone):02d}{band}{square}'
        return '' if zone is None else str(zone)

    @staticmethod
    def _aoi_key(layer):
        """Short hash of the AOI extent + CRS, used so cached band windows are never reused for a different AOI."""
        ext = layer.extent()
        text = '{}|{:.6f}|{:.6f}|{:.6f}|{:.6f}'.format(
            layer.crs().authid() or layer.crs().toWkt(),
            ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum())
        return hashlib.md5(text.encode('utf-8')).hexdigest()[:8]

    def _scene_selected(self):
        rows = sorted({i.row() for i in self.results.selectionModel().selectedRows()})
        items = [self.scene_rows[r] for r in rows if 0 <= r < len(self.scene_rows)]
        self.selected_items = items
        self.selected_item = items[0] if items else None
        self._update_selection_info(rows)

    def _update_selection_info(self, rows):
        if not rows:
            self.workflow_label.setText('Search for scenes and select one (or several to mosaic), then run the workflow.')
            return
        if len(rows) == 1:
            text = f"Selected: {self.scene_rows[rows[0]].get('id', '')}."
        else:
            text = f'{len(rows)} scenes selected \u2014 they will be mosaicked.'
        cover = self._union_cover(rows)
        if cover is not None:
            text += f' Their data footprints cover about {cover:.0f}% of the AOI.'
            if cover < 99:
                text += ' Add more scenes (or use Auto-select) to cover the rest.'
        self.workflow_label.setText(text)

    def choose_output(self):
        directory = QFileDialog.getExistingDirectory(self, 'Choose output folder', self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)

    def run_workflow(self):
        if self._busy:
            return
        try:
            scenes = list(self.selected_items) if self.selected_items else ([self.selected_item] if self.selected_item else [])
            if not scenes:
                raise RuntimeError('Search for imagery and select a scene first.')
            aoi = self._aoi_layer()
            processing_aoi = self._working_aoi_layer(aoi)
            out_dir = os.path.abspath(os.path.expanduser(self.output_edit.text().strip()))
            prefix = re.sub(r'[^A-Za-z0-9_-]+', '_', self.prefix_edit.text().strip() or 'ndvi').strip('_')
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(os.path.join(out_dir, '_bands'), exist_ok=True)

            if len(scenes) > 1:
                # Priority matters for "first valid pixel" and for the report: newest first, then (stable sort)
                # least cloudy first when that priority is chosen.
                scenes.sort(key=lambda sc: sc.get('properties', {}).get('datetime') or '', reverse=True)
                if self.priority_combo.currentData() == 'cloud':
                    scenes.sort(key=lambda sc: float(sc.get('properties', {}).get('eo:cloud_cover', 0) or 0))

            if len(scenes) == 1:
                label = scenes[0].get('id', 'scene')
            else:
                dates = sorted(((sc.get('properties', {}).get('datetime') or '')[:10]).replace('-', '') for sc in scenes)
                digest = hashlib.md5('|'.join(sorted(sc.get('id', '') for sc in scenes)).encode('utf-8')).hexdigest()[:6]
                label = f"mosaic_{len(scenes)}scenes_{dates[-1] or 'nodate'}_{digest}"
            label_safe = re.sub(r'[^A-Za-z0-9_-]+', '_', label)

            aoi_key = self._aoi_key(processing_aoi)
            scene_jobs = []
            for scene in scenes:
                scene_id = scene.get('id', 'scene')
                band_dir = os.path.join(out_dir, '_bands', re.sub(r'[^A-Za-z0-9_-]+', '_', scene_id))
                os.makedirs(band_dir, exist_ok=True)
                scene_jobs.append({
                    'scene': scene,
                    'id': scene_id,
                    'red_path': os.path.join(band_dir, f'red_aoi_{aoi_key}.tif'),
                    'nir_path': os.path.join(band_dir, f'nir_aoi_{aoi_key}.tif'),
                    'scl_path': os.path.join(band_dir, f'scl_aoi_{aoi_key}.tif'),
                    'native': os.path.join(band_dir, f'ndvi_native_{aoi_key}.tmp.tif'),
                })

            ndvi_clip = os.path.join(out_dir, f'{prefix}_{label_safe}.tif')
            job = {
                'scenes': scene_jobs,
                'options': {
                    'cloud_mode': self.cloud_combo.currentData(),
                    'buffer_px': int(self.buffer_spin.value()),
                    'offset_mode': self.offset_combo.currentData(),
                    'method': self.method_combo.currentData(),
                },
                'label': label,
                'ndvi_full': os.path.join(out_dir, f'{prefix}_{label_safe}_full.tif'),
                'clip_part': ndvi_clip + '.part',
                'scenes_csv': os.path.join(out_dir, f'{prefix}_{label_safe}_scenes.csv'),
                # Plain copies of everything the worker needs from QGIS objects, taken here on the GUI thread.
                'aoi_info': {
                    'crs': QgsCoordinateReferenceSystem(processing_aoi.crs()),
                    'extent': QgsRectangle(processing_aoi.extent()),
                    'tctx': QgsProject.instance().transformContext(),
                },
                'cutline': self._collect_cutline(processing_aoi),
            }
            self._pending = {
                'aoi': processing_aoi,
                'scene_id': label,
                'prefix': prefix,
                'clip_part': job['clip_part'],
                'clip_final': ndvi_clip,
                'csv_path': os.path.join(out_dir, f'{prefix}_{label_safe}_zonal_stats.csv'),
                'add_to_project': self.add_to_project.isChecked(),
                'coverage': None,
                'scenes_csv': None,
            }
        except Exception as exc:
            self._error(str(exc))
            return
        self.progress.setValue(0)
        self.log_edit.clear()
        self._log('Starting NDVI workflow\u2026')
        self._start_task('NDVI Extractor: download and calculate NDVI', self._workflow_worker,
                         (job,), self._workflow_stage1_finished, operation='workflow')

    @staticmethod
    def _collect_cutline(layer):
        """AOI geometries as plain WKB bytes + CRS, so the worker thread never touches the QGIS layer."""
        wkbs = []
        for feat in layer.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                continue
            try:
                if QgsWkbTypes.isCurvedType(geom.wkbType()):
                    geom.convertToStraightSegment()
            except Exception:
                pass
            wkbs.append(bytes(geom.asWkb()))
        if not wkbs:
            raise RuntimeError('The AOI layer has no valid polygon geometries.')
        crs = layer.crs()
        return {'wkbs': wkbs, 'authid': crs.authid(), 'wkt': crs.toWkt()}

    def _workflow_worker(self, task, job):
        """Background thread: network and raster work only (no widgets, no QGIS project access)."""
        self._worker_task = task
        scenes, opts = job['scenes'], job['options']
        n = len(scenes)
        try:
            report, native_paths = [], []
            share = 60.0 / n
            for i, sj in enumerate(scenes):
                self._check_cancel()
                if n > 1:
                    self._log(f"--- Scene {i + 1}/{n}: {sj['id']} ---")
                try:
                    stats = self._process_scene(sj, job, share * i, share, single=(n == 1))
                except SceneSkipped as skip:
                    if n == 1:
                        raise RuntimeError(str(skip))
                    self._log(f"Skipping {sj['id']}: {skip}")
                    continue
                report.append(stats)
                native_paths.append(stats['native'])
            if not native_paths:
                raise RuntimeError('None of the selected scenes overlaps the AOI.')

            if n > 1:
                self._log(f"Mosaicking {len(native_paths)} scene(s), method: {opts['method']}\u2026")
                counts = self._mosaic_ndvi(native_paths, opts['method'], job['aoi_info'], job['cutline'],
                                           job['ndvi_full'], 60, 70)
                for stats, count in zip(report, counts):
                    stats['contributed'] = count
            self._progress(70)

            self._log('Clipping NDVI to AOI\u2026')
            self._clip_raster(job['ndvi_full'], job['cutline'], job['clip_part'], progress=(70, 80))
            coverage = self._aoi_coverage(job['clip_part'], job['cutline'])
            if n == 1:
                report[0]['contributed'] = coverage['valid']
            self._progress(80)
            self._log(f"AOI coverage: {coverage['pct']:.1f}% of the AOI polygon has valid NDVI "
                      f"({coverage['valid']} of {coverage['inside']} pixels).")
            if coverage['pct'] < 99.0:
                reason = 'no satellite data' + (' or cloud/shadow masking' if opts['cloud_mode'] != 'off' else '')
                self._log(f'  The rest is empty because of {reason}. Select more scenes to mosaic to fill it.')
            self._write_scenes_csv(job['scenes_csv'], report, coverage)

            # Remove the large intermediates after a successful clip.
            self._remove_files([job['ndvi_full']] + [sj['native'] for sj in scenes])
            return {'ok': True, 'coverage': coverage, 'scenes_csv': job['scenes_csv']}
        except WorkflowCancelled:
            self._cleanup_partials(job)
            return {'cancelled': True}
        except Exception as exc:
            self._cleanup_partials(job)
            if self._is_cancelled():
                return {'cancelled': True}
            return {'error': str(exc), 'trace': traceback.format_exc()}

    def _process_scene(self, sj, job, base, share, single):
        """Download the AOI windows of one scene and compute its (cloud-masked) NDVI on the scene's own grid."""
        scene, opts, aoi_info = sj['scene'], job['options'], job['aoi_info']
        use_cloud = opts['cloud_mode'] != 'off' and 'scl' in scene.get('assets', {})
        if opts['cloud_mode'] != 'off' and not use_cloud:
            self._log('  This scene has no SCL asset: cloud masking skipped for it.')
        w = (0.30, 0.30, 0.15, 0.25) if use_cloud else (0.40, 0.40, 0.0, 0.20)

        def at(fraction):
            return base + share * fraction

        self._download_asset(scene, 'red', sj['red_path'], at(0), at(w[0]), aoi_info)
        self._check_cancel()
        self._download_asset(scene, 'nir', sj['nir_path'], at(w[0]), at(w[0] + w[1]), aoi_info)
        self._check_cancel()
        if use_cloud:
            self._download_asset(scene, 'scl', sj['scl_path'], at(w[0] + w[1]), at(w[0] + w[1] + w[2]), aoi_info, margin_px=2)
            self._check_cancel()
        out_path = job['ndvi_full'] if single else sj['native']
        self._log('Calculating NDVI\u2026')
        stats = self._calculate_ndvi(
            sj['red_path'], sj['nir_path'], scene, out_path,
            scl_path=sj['scl_path'] if use_cloud else None,
            cloud_classes=SCL_CLASSES.get(opts['cloud_mode']), buffer_px=opts['buffer_px'],
            offset_mode=opts.get('offset_mode', 'auto'),
            progress=(at(w[0] + w[1] + w[2]), at(1.0)))
        stats.update({'id': sj['id'], 'native': out_path, 'props': scene.get('properties', {})})
        return stats

    def _mosaic_ndvi(self, native_paths, method, aoi_info, cutline, out_path, p0, p1):
        """Combine per-scene NDVI rasters (any CRS) on ONE common 10 m grid and write `out_path`.
        Returns, per scene, the number of pixels inside the AOI polygon that it contributes."""
        first = gdal.Open(native_paths[0], gdal.GA_ReadOnly)
        target_wkt = first.GetProjection()
        res = abs(first.GetGeoTransform()[1])
        first = None
        target_crs = QgsCoordinateReferenceSystem.fromWkt(target_wkt)
        ext = QgsCoordinateTransform(aoi_info['crs'], target_crs, aoi_info['tctx']).transformBoundingBox(
            QgsRectangle(aoi_info['extent']))
        x0 = math.floor((ext.xMinimum() - res) / res) * res
        x1 = math.ceil((ext.xMaximum() + res) / res) * res
        y0 = math.floor((ext.yMinimum() - res) / res) * res
        y1 = math.ceil((ext.yMaximum() + res) / res) * res
        width, height = int(round((x1 - x0) / res)), int(round((y1 - y0) / res))
        if width * height > MAX_WINDOW_PIXELS:
            raise RuntimeError('The AOI is too large to mosaic at 10 m (>50 megapixels). Use a smaller AOI.')
        geotransform = (x0, res, 0.0, y1, 0.0, -res)

        n = len(native_paths)
        warped, sources = [], []
        out_ds = band_out = None
        counts = [0] * n
        try:
            # 1) put every scene on the common grid (a plain copy when it is already on it; reprojection otherwise)
            for i, path in enumerate(native_paths):
                self._check_cancel()
                wp = path + '.warp.tmp.tif'
                if os.path.exists(wp):
                    os.remove(wp)
                ds = gdal.Warp(wp, path, format='GTiff', dstSRS=target_wkt, outputBounds=[x0, y0, x1, y1],
                               xRes=res, yRes=res, resampleAlg='near', srcNodata=-9999.0, dstNodata=-9999.0,
                               outputType=gdal.GDT_Float32, creationOptions=['TILED=YES', 'COMPRESS=DEFLATE'])
                ds = None
                warped.append(wp)
                self._progress(p0 + (p1 - p0) * 0.5 * (i + 1) / n)

            inside = self._polygon_mask(cutline, geotransform, target_wkt, width, height)

            # 2) combine in row blocks (bounded memory)
            sources = [gdal.Open(wp, gdal.GA_ReadOnly) for wp in warped]
            bands = [src.GetRasterBand(1) for src in sources]
            out_ds = gdal.GetDriverByName('GTiff').Create(
                out_path, width, height, 1, gdal.GDT_Float32,
                options=['TILED=YES', 'COMPRESS=DEFLATE', 'PREDICTOR=2', 'BIGTIFF=IF_SAFER'])
            out_ds.SetGeoTransform(geotransform)
            out_ds.SetProjection(target_wkt)
            band_out = out_ds.GetRasterBand(1)
            band_out.SetNoDataValue(-9999.0)
            block = 512
            for y in range(0, height, block):
                self._check_cancel()
                h = min(block, height - y)
                stack = np.stack([b.ReadAsArray(0, y, width, h) for b in bands])      # (scenes, h, width)
                valid = stack != -9999.0
                any_valid = valid.any(axis=0)
                if method == 'median':
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore', RuntimeWarning)
                        merged = np.nanmedian(np.where(valid, stack, np.nan), axis=0)
                    contributed = valid
                elif method == 'max':
                    merged = np.where(valid, stack, -np.inf).max(axis=0)
                    contributed = valid
                else:   # first valid pixel in priority order
                    idx = valid.argmax(axis=0)
                    merged = np.take_along_axis(stack, idx[None], 0)[0]
                    contributed = np.zeros_like(valid)
                    np.put_along_axis(contributed, idx[None], True, 0)
                    contributed &= valid
                band_out.WriteArray(np.where(any_valid, merged, -9999.0).astype(np.float32), 0, y)
                rows_inside = inside[y:y + h]
                for k in range(n):
                    counts[k] += int((contributed[k] & rows_inside).sum())
                self._progress(p0 + (p1 - p0) * (0.5 + 0.5 * min(y + h, height) / height))
            band_out.FlushCache()
            out_ds.FlushCache()
        finally:
            band_out = None
            out_ds = None
            bands = None
            sources = None
            self._remove_files(warped)
        return counts

    @staticmethod
    def _cutline_srs(cutline):
        srs = osr.SpatialReference()
        loaded = False
        authid = cutline.get('authid')
        if authid:
            try:
                loaded = (srs.SetFromUserInput(authid) == 0)
            except Exception:
                loaded = False
        if not loaded:
            srs = osr.SpatialReference()
            srs.ImportFromWkt(cutline['wkt'])
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs

    def _polygon_mask(self, cutline, geotransform, wkt, width, height):
        """Boolean array on the given grid: True where the pixel centre is inside the AOI polygon(s)."""
        dst = osr.SpatialReference()
        dst.ImportFromWkt(wkt)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        transform = osr.CoordinateTransformation(self._cutline_srs(cutline), dst)
        mem = ogr.GetDriverByName('Memory').CreateDataSource('aoi_mask')
        layer = mem.CreateLayer('mask', dst, ogr.wkbUnknown)
        for wkb in cutline['wkbs']:
            try:
                geom = ogr.CreateGeometryFromWkb(wkb)
                geom.Transform(transform)
            except Exception:
                continue
            feat = ogr.Feature(layer.GetLayerDefn())
            feat.SetGeometry(geom)
            layer.CreateFeature(feat)
            feat = None
        ds = gdal.GetDriverByName('MEM').Create('', width, height, 1, gdal.GDT_Byte)
        ds.SetGeoTransform(geotransform)
        ds.SetProjection(wkt)
        gdal.RasterizeLayer(ds, [1], layer, burn_values=[1])
        mask = ds.GetRasterBand(1).ReadAsArray() == 1
        ds = None
        layer = None
        mem = None
        return mask

    def _aoi_coverage(self, clip_path, cutline):
        """How much of the AOI polygon ended up with a valid NDVI value."""
        ds = gdal.Open(clip_path, gdal.GA_ReadOnly)
        width, height = ds.RasterXSize, ds.RasterYSize
        geotransform, wkt = ds.GetGeoTransform(), ds.GetProjection()
        data = ds.GetRasterBand(1).ReadAsArray()
        ds = None
        inside = self._polygon_mask(cutline, geotransform, wkt, width, height)
        n_inside = int(inside.sum())
        n_valid = int(((data != -9999.0) & inside).sum())
        return {'inside': n_inside, 'valid': n_valid, 'pct': (n_valid * 100.0 / n_inside) if n_inside else 0.0}

    def _write_scenes_csv(self, path, report, coverage):
        """Provenance table: which scenes were used and how much of the AOI each one supplied."""
        inside = max(coverage['inside'], 1)
        with open(path, 'w', newline='', encoding='utf-8-sig') as fh:
            writer = csv.writer(fh)
            writer.writerow(['priority', 'scene_id', 'date', 'tile', 'satellite', 'scene_cloud_pct',
                             'data_pixels_in_window', 'cloud_masked_pixels', 'cloud_masked_pct_of_data',
                             'aoi_pixels_contributed', 'aoi_pct_contributed', 'reflectance_offset'])
            for k, st in enumerate(report, 1):
                props = st.get('props', {})
                data, masked, used = st.get('data', 0), st.get('cloud_masked', 0), st.get('contributed', 0)
                writer.writerow([k, st['id'], (props.get('datetime') or '')[:10], self._tile_label(props),
                                 props.get('platform', ''), props.get('eo:cloud_cover', ''), data, masked,
                                 f'{masked * 100.0 / data:.1f}' if data else '', used, f'{used * 100.0 / inside:.1f}',
                                 st.get('offset', '')])

    @staticmethod
    def _remove_files(paths):
        for path in paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    @staticmethod
    def _cleanup_partials(job):
        paths = [job['ndvi_full'], job['clip_part']]
        for sj in job['scenes']:
            paths += [sj['native'], sj['native'] + '.warp.tmp.tif',
                      sj['red_path'] + '.part', sj['nir_path'] + '.part', sj['scl_path'] + '.part']
        NDVIDialog._remove_files(paths)

    def _workflow_stage1_finished(self, exception, value=None):
        """GUI thread: the download / NDVI / clip worker has ended."""
        if self._shutting_down:
            return
        self._task = None
        self._worker_task = None
        pend = self._pending
        result = value if isinstance(value, dict) else {}
        try:
            if result.get('cancelled') or (not result and exception is not None and self._cancel_event.is_set()):
                self._finish_workflow('Cancelled.')
                return
            if not result:
                raise RuntimeError(str(exception) if exception is not None else 'The workflow ended without a result.')
            if result.get('error'):
                if result.get('trace'):
                    QgsMessageLog.logMessage(result['trace'], 'NDVI Extractor', Qgis.MessageLevel.Warning)
                raise RuntimeError(result['error'])
            pend['coverage'] = result.get('coverage')
            pend['scenes_csv'] = result.get('scenes_csv')
            # Replace the previous result only now that the new one is complete.
            try:
                self._release_output(pend['clip_final'])
                os.replace(pend['clip_part'], pend['clip_final'])
            except Exception:
                try:
                    os.remove(pend['clip_part'])
                except OSError:
                    pass
                raise
            self._progress(82)
            self._log('Running zonal statistics\u2026')
            self._start_zonal(pend['clip_final'], pend['aoi'], pend['scene_id'])
        except Exception as exc:
            self._finish_workflow()
            self._error(str(exc))

    def _start_zonal(self, raster_path, aoi_layer, scene_id):
        """Zonal statistics run as a QGIS Processing background task (output: in-memory layer)."""
        alg = QgsApplication.processingRegistry().createAlgorithmById('native:zonalstatisticsfb')
        if alg is None:
            raise RuntimeError('Zonal statistics algorithm is unavailable.')
        params = {
            'INPUT': aoi_layer,
            'INPUT_RASTER': raster_path,
            'RASTER_BAND': 1,
            'COLUMN_PREFIX': 'ndvi_',
            'STATISTICS': [0, 2, 4, 5, 6],  # count, mean, std.dev, min, max
            'OUTPUT': 'memory:'
        }
        context = QgsProcessingContext()
        context.setProject(QgsProject.instance())
        feedback = SilentFeedback(self._log)
        task = QgsProcessingAlgRunnerTask(alg, params, context, feedback)
        task.executed.connect(self._zonal_finished)
        # Keep everything alive until the task reports back.
        self._zonal_context = context
        self._zonal_feedback = feedback
        self._zonal_aoi = aoi_layer
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _zonal_finished(self, successful, results=None):
        """GUI thread: zonal statistics finished; export the CSV and add layers to the project."""
        if self._shutting_down:
            return
        pend = self._pending
        context = self._zonal_context
        try:
            if self._cancel_event.is_set():
                self._finish_workflow('Cancelled.')
                return
            if not successful:
                raise RuntimeError('Zonal statistics failed. See the log for details.')
            out = (results or {}).get('OUTPUT')
            layer = out if isinstance(out, QgsVectorLayer) else None
            if layer is None and isinstance(out, str) and context is not None:
                layer = context.takeResultLayer(out)
            if layer is None and isinstance(out, str):
                layer = QgsVectorLayer(out, 'NDVI zones', 'ogr')
            if layer is None or not layer.isValid():
                raise RuntimeError('Zonal statistics did not return a valid vector layer.')
            layer.setName(f"NDVI zones - {pend['scene_id']}")
            self._export_zonal_csv(layer, pend['csv_path'])
            self._progress(94)

            if pend['add_to_project']:
                self._add_ndvi_layer(pend['clip_final'], pend['prefix'], pend['scene_id'])
                active = self.iface.activeLayer()
                if active:
                    self.iface.layerTreeView().refreshLayerSymbology(active.id())
                QgsProject.instance().addMapLayer(layer)

            self._progress(100)
            self._log(f"DONE \u2014 NDVI: {pend['clip_final']}")
            self._log(f"DONE \u2014 Zonal CSV: {pend['csv_path']}")
            clip_final, csv_path = pend['clip_final'], pend['csv_path']
            scenes_csv, coverage = pend.get('scenes_csv'), pend.get('coverage')
            self._finish_workflow()
            extra = f"\n\nScenes used (CSV):\n{scenes_csv}" if scenes_csv else ''
            cover = f"\nAOI coverage: {coverage['pct']:.1f}% of the polygon has valid NDVI." if coverage else ''
            QMessageBox.information(self, 'NDVI Extractor v2',
                                    f'Workflow complete.{cover}\n\nNDVI GeoTIFF:\n{clip_final}\n\nZonal CSV:\n{csv_path}{extra}')
        except Exception as exc:
            self._finish_workflow()
            self._error(str(exc))

    def _finish_workflow(self, message=None):
        self._task = None
        self._worker_task = None
        self._zonal_context = None
        self._zonal_feedback = None
        self._zonal_aoi = None
        self._pending = {}
        self._set_busy(False)
        if message:
            self._log(message)

    def _download_asset(self, feature, asset_key, path, progress_start, progress_end, aoi_info, margin_px=1):
        """Read ONLY the AOI window from the remote Cloud-Optimized GeoTIFF (HTTP range requests) instead of
        downloading the whole ~100+ MB, 10980x10980 px band. Transient network failures are retried.
        Runs in a worker thread: no widgets, no QgsProject access."""
        asset = feature.get('assets', {}).get(asset_key)
        if not asset or not asset.get('href'):
            raise SceneSkipped(f'the scene has no {asset_key} asset')
        self._log(f'Reading {asset_key.upper()} band (AOI window only)\u2026')
        if os.path.exists(path) and os.path.getsize(path) > 0:
            self._log(f'Using existing file: {path}')
            self._progress(progress_end)
            return

        tmp_path = path + '.part'
        last_error = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                self._read_window(asset, asset_key, tmp_path, progress_start, progress_end, aoi_info, margin_px)
                last_error = None
                break
            except (SceneSkipped, WorkflowCancelled, NonRetryableError):
                raise
            except Exception as exc:
                self._check_cancel()
                last_error = exc
                if attempt < DOWNLOAD_ATTEMPTS:
                    self._log(f'  {asset_key.upper()} read failed (attempt {attempt} of {DOWNLOAD_ATTEMPTS}): '
                              f'{self._short_error(exc)} Retrying\u2026')
                    gdal.VSICurlClearCache()      # GDAL caches failed chunks; a retry must not reuse them
                    self._sleep_cancellable(2.0 * attempt)
        if last_error is not None:
            raise RuntimeError(
                f'Could not read the {asset_key.upper()} band from the Sentinel-2 file server after '
                f'{DOWNLOAD_ATTEMPTS} attempts. This usually means a network problem (connection reset or '
                f'timeout); please try again later.\n\nDetails: {self._short_error(last_error)}')
        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise RuntimeError(f'Read failed for {asset_key}.')

    @staticmethod
    def _short_error(exc):
        text = ' '.join(str(exc).split())
        return text.split(' May be caused by')[0][:200]

    def _sleep_cancellable(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_cancel()
            time.sleep(0.1)

    def _read_window(self, asset, asset_key, tmp_path, progress_start, progress_end, aoi_info, margin_px):
        """One attempt: open the remote COG and copy the AOI window to `tmp_path`."""
        # Thread-local GDAL options: they apply to this worker only and never leak into the rest of QGIS.
        gdal_options = {
            'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
            'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif,.TIF',
            'GDAL_HTTP_MAX_RETRY': '3',
            'GDAL_HTTP_RETRY_DELAY': '2',
            'GDAL_HTTP_TIMEOUT': '60',
        }
        for key, value in gdal_options.items():
            gdal.SetThreadLocalConfigOption(key, value)
        src = None
        out = None
        try:
            src = gdal.Open('/vsicurl/' + asset['href'], gdal.GA_ReadOnly)
            if src is None:
                raise RuntimeError(f'GDAL could not open the {asset_key} band remotely: {asset["href"]}')
            self._check_cancel()

            # AOI extent -> raster CRS
            ras_crs = QgsCoordinateReferenceSystem.fromWkt(src.GetProjection())
            tr = QgsCoordinateTransform(aoi_info['crs'], ras_crs, aoi_info['tctx'])
            ext = tr.transformBoundingBox(QgsRectangle(aoi_info['extent']))
            gt = src.GetGeoTransform()
            px = abs(gt[1])
            minx, maxx = ext.xMinimum() - margin_px * px, ext.xMaximum() + margin_px * px
            miny, maxy = ext.yMinimum() - margin_px * px, ext.yMaximum() + margin_px * px
            # clamp to raster footprint
            r_minx, r_maxy = gt[0], gt[3]
            r_maxx = gt[0] + gt[1] * src.RasterXSize
            r_miny = gt[3] + gt[5] * src.RasterYSize
            minx, maxx = max(minx, r_minx), min(maxx, r_maxx)
            miny, maxy = max(miny, r_miny), min(maxy, r_maxy)
            if minx >= maxx or miny >= maxy:
                raise SceneSkipped('the AOI does not overlap this scene')
            n_px = ((maxx - minx) / px) * ((maxy - miny) / px)
            self._log(f'  window \u2248 {int((maxx - minx) / px)} x {int((maxy - miny) / px)} px')
            if n_px > MAX_WINDOW_PIXELS:
                raise NonRetryableError('AOI is very large (>50 megapixels at this resolution). '
                                        'Use a smaller AOI to avoid running out of memory.')

            def cb(frac, msg, data):
                self._progress(progress_start + frac * (progress_end - progress_start))
                return 0 if self._is_cancelled() else 1

            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            out = gdal.Translate(
                tmp_path, src, format='GTiff', projWin=[minx, maxy, maxx, miny],
                creationOptions=['TILED=YES', 'COMPRESS=DEFLATE', 'BIGTIFF=IF_SAFER'],
                callback=cb)
            if out is None:
                self._check_cancel()
                raise RuntimeError(f'Could not read the {asset_key} window from Earth Search.')
        finally:
            out = None   # closing the datasets flushes the file and releases the handle
            src = None
            for key in gdal_options:
                gdal.SetThreadLocalConfigOption(key, None)

    def _asset_scale_offset(self, feature, asset_key):
        asset = feature.get('assets', {}).get(asset_key, {})
        bands = asset.get('raster:bands') or []
        if bands:
            rb = bands[0]
            scale = rb.get('scale', 1.0)
            offset = rb.get('offset', 0.0)
            return float(scale if scale is not None else 1.0), float(offset if offset is not None else 0.0)
        return 1.0, 0.0

    def _calculate_ndvi(self, red_path, nir_path, feature, output_path, scl_path=None, cloud_classes=None,
                        buffer_px=0, progress=(60, 68), offset_mode='metadata'):
        """NDVI on the scene's own grid. Pixels flagged as cloud/shadow/cirrus by the SCL (plus an optional
        buffer) are left empty, so a mosaic can fill them from other scenes. Returns pixel statistics."""
        red_ds = gdal.Open(red_path, gdal.GA_ReadOnly)
        nir_ds = gdal.Open(nir_path, gdal.GA_ReadOnly)
        out_ds = None
        band_out = red_band = nir_band = None
        n_data = n_cloud = n_valid = 0
        try:
            if red_ds is None or nir_ds is None:
                raise RuntimeError('GDAL could not open the downloaded Red/NIR rasters.')
            if red_ds.RasterXSize != nir_ds.RasterXSize or red_ds.RasterYSize != nir_ds.RasterYSize:
                raise RuntimeError('Red and NIR rasters have different dimensions. The selected scene is not directly co-registered.')
            if not self._same_geotransform(red_ds.GetGeoTransform(), nir_ds.GetGeoTransform()):
                raise RuntimeError('Red and NIR rasters have different grids/geotransforms.')
            projection = red_ds.GetProjection()
            cloud = None
            if scl_path and cloud_classes:
                cloud = self._cloud_mask(scl_path, red_ds, cloud_classes, buffer_px)
            driver = gdal.GetDriverByName('GTiff')
            out_ds = driver.Create(
                output_path, red_ds.RasterXSize, red_ds.RasterYSize, 1, gdal.GDT_Float32,
                options=['TILED=YES', 'COMPRESS=DEFLATE', 'PREDICTOR=2', 'BIGTIFF=IF_SAFER'])
            out_ds.SetGeoTransform(red_ds.GetGeoTransform())
            out_ds.SetProjection(projection)
            band_out = out_ds.GetRasterBand(1)
            band_out.SetNoDataValue(-9999.0)
            red_band = red_ds.GetRasterBand(1)
            nir_band = nir_ds.GetRasterBand(1)
            red_nodata = red_band.GetNoDataValue()
            nir_nodata = nir_band.GetNoDataValue()
            red_scale, red_offset = self._asset_scale_offset(feature, 'red')
            nir_scale, nir_offset = self._asset_scale_offset(feature, 'nir')
            red_offset, nir_offset, offset_note = self._resolve_offsets(
                offset_mode, red_band, red_nodata, red_scale, red_offset, nir_offset)
            self._log('  ' + offset_note)

            block_x, block_y = red_band.GetBlockSize()
            if block_x <= 0:
                block_x, block_y = 512, 512
            total_blocks = math.ceil(red_ds.RasterXSize / block_x) * math.ceil(red_ds.RasterYSize / block_y)
            done = 0
            for y in range(0, red_ds.RasterYSize, block_y):
                h = min(block_y, red_ds.RasterYSize - y)
                for x in range(0, red_ds.RasterXSize, block_x):
                    w = min(block_x, red_ds.RasterXSize - x)
                    raw_red = red_band.ReadAsArray(x, y, w, h)
                    raw_nir = nir_band.ReadAsArray(x, y, w, h)
                    red = raw_red.astype(np.float32)
                    nir = raw_nir.astype(np.float32)
                    red = red * red_scale + red_offset
                    nir = nir * nir_scale + nir_offset
                    # Slightly negative reflectance is normal after the BOA offset on very dark surfaces
                    # (dense vegetation reflects almost no red). Clamp it to 0 instead of discarding the
                    # pixel, otherwise the healthiest vegetation would be thrown away.
                    red = np.maximum(red, 0.0)
                    nir = np.maximum(nir, 0.0)
                    denom = nir + red
                    nodata_px = np.zeros(raw_red.shape, dtype=bool)
                    if red_nodata is not None:
                        nodata_px |= (raw_red == red_nodata)
                    if nir_nodata is not None:
                        nodata_px |= (raw_nir == nir_nodata)
                    # Only mask pixels where BOTH bands carry (almost) no signal: the ratio is noise there.
                    invalid = ~np.isfinite(red) | ~np.isfinite(nir) | (denom < MIN_SIGNAL) | nodata_px
                    n_data += int((~nodata_px).sum())
                    if cloud is not None:
                        cloud_block = cloud[y:y + h, x:x + w]
                        n_cloud += int((cloud_block & ~nodata_px).sum())
                        invalid |= cloud_block
                    ndvi = np.full((h, w), -9999.0, dtype=np.float32)
                    valid = ~invalid
                    n_valid += int(valid.sum())
                    ndvi[valid] = (nir[valid] - red[valid]) / denom[valid]
                    band_out.WriteArray(ndvi, x, y)
                    done += 1
                    self._progress(progress[0] + (progress[1] - progress[0]) * done / max(total_blocks, 1))
                    self._check_cancel()
            band_out.FlushCache()
            out_ds.FlushCache()
            if cloud is not None:
                pct = n_cloud * 100.0 / n_data if n_data else 0.0
                self._log(f'  cloud/shadow mask removed {n_cloud} of {n_data} pixels with data ({pct:.1f}%)')
            return {'data': n_data, 'cloud_masked': n_cloud, 'valid': n_valid, 'offset': red_offset}
        finally:
            # Always release the datasets so a failed/cancelled run leaves no locked files behind.
            band_out = None
            red_band = None
            nir_band = None
            out_ds = None
            red_ds = None
            nir_ds = None

    def _resolve_offsets(self, mode, red_band, red_nodata, red_scale, red_offset, nir_offset):
        """Decide which reflectance offset to apply. Returns (red_offset, nir_offset, log message).
        Auto: if the metadata offset is -0.1 (DN 1000 = zero reflectance) but a large share of valid red pixels
        lies BELOW 1000 DN, the offset cannot be in the pixels (that would be negative reflectance), so it is ignored."""
        if mode == 'none':
            return 0.0, 0.0, 'Reflectance offset: none (DN x scale only), as selected.'
        if mode == 'metadata' or not red_offset:
            return red_offset, nir_offset, f'Reflectance offset: {red_offset:g} from the scene metadata.'
        width, height = red_band.XSize, red_band.YSize
        step = max(1, math.ceil(math.sqrt(width * height / 4_000_000.0)))      # test on a sub-sample: fast on big windows
        sample = red_band.ReadAsArray(buf_xsize=max(1, width // step), buf_ysize=max(1, height // step)).astype('float64')
        valid = sample > 0
        if red_nodata is not None:
            valid &= sample != red_nodata
        count = int(valid.sum())
        if count < 100:
            return red_offset, nir_offset, 'Reflectance offset: too few pixels to test it, using the scene metadata value.'
        threshold = -red_offset / red_scale                       # DN that equals zero reflectance after the offset
        share = float((sample[valid] < threshold).sum()) / count
        if share > OFFSET_ABSENT_SHARE:
            return 0.0, 0.0, (f'Reflectance offset: the metadata announces {red_offset:g}, but {share * 100:.0f}% of the valid '
                              f'red pixels are below {threshold:g} DN (that would be negative reflectance), so the offset is '
                              f'not present in this data: ignored.')
        return red_offset, nir_offset, (f'Reflectance offset: {red_offset:g} from the scene metadata ({share * 100:.1f}% of '
                                        f'red pixels below {threshold:g} DN: consistent with the offset being present).')

    def _cloud_mask(self, scl_path, ref_ds, classes, buffer_px):
        """Boolean array on the reference raster's grid: True where the SCL flags the pixel (grown by buffer_px)."""
        scl_ds = gdal.Open(scl_path, gdal.GA_ReadOnly)
        if scl_ds is None:
            raise RuntimeError('GDAL could not open the downloaded SCL raster.')
        gt = ref_ds.GetGeoTransform()
        w, h = ref_ds.RasterXSize, ref_ds.RasterYSize
        warped = None
        try:
            # SCL is 20 m: nearest-neighbour onto the 10 m grid of the NDVI window.
            warped = gdal.Warp('', scl_ds, format='MEM',
                               outputBounds=[gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3]],
                               width=w, height=h, dstSRS=ref_ds.GetProjection(),
                               resampleAlg='near', outputType=gdal.GDT_Byte)
            scl = warped.GetRasterBand(1).ReadAsArray()
        finally:
            warped = None
            scl_ds = None
        return self._dilate(np.isin(scl, classes), buffer_px)

    @staticmethod
    def _dilate(mask, pixels):
        """Grow a boolean mask by `pixels` pixels in every direction (square structuring element)."""
        out = mask
        for _ in range(max(int(pixels), 0)):
            p = np.pad(out, 1, mode='constant', constant_values=False)
            out = (p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
                   | p[:-2, :-2] | p[:-2, 2:] | p[2:, :-2] | p[2:, 2:])
        return out

    @staticmethod
    def _same_geotransform(a, b, tol=1e-9):
        return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))

    @staticmethod
    def _release_output(path):
        """Make room for a new result. On Windows a GeoTIFF that is loaded as a QGIS layer is locked and
        cannot be overwritten, which previously left the old file in place without any visible error."""
        norm = os.path.normcase(os.path.abspath(path))
        project = QgsProject.instance()
        for layer in list(project.mapLayers().values()):
            src = layer.source().split('|')[0]
            if os.path.normcase(os.path.abspath(src)) == norm:
                project.removeMapLayer(layer.id())
        # Removed layers are destroyed via deferred delete; flush it so the file handle is released.
        deferred = getattr(getattr(QEvent, 'Type', QEvent), 'DeferredDelete')
        QCoreApplication.sendPostedEvents(None, deferred)
        QApplication.processEvents()
        if os.path.exists(path):
            last_exc = None
            for _attempt in range(5):
                try:
                    os.remove(path)
                    last_exc = None
                    break
                except OSError as exc:
                    last_exc = exc
                    QCoreApplication.sendPostedEvents(None, deferred)
                    QApplication.processEvents()
                    time.sleep(0.3)
            if last_exc is not None:
                raise RuntimeError(f'Cannot overwrite the existing output file (is it open in another program?):\n{path}\n{last_exc}')
        # GDAL's cached statistics of the OLD raster would otherwise be picked up by the new file of the same name.
        try:
            if os.path.exists(path + '.aux.xml'):
                os.remove(path + '.aux.xml')
        except OSError:
            pass

    def _write_cutline(self, cutline, path):
        """Write the AOI polygons to an in-memory GeoPackage that gdalwarp uses as the cutline."""
        srs = self._cutline_srs(cutline)
        ds = ogr.GetDriverByName('GPKG').CreateDataSource(path)
        if ds is None:
            raise RuntimeError('Could not create the AOI cutline for clipping.')
        layer = ds.CreateLayer('MASK', srs, ogr.wkbUnknown)
        defn = layer.GetLayerDefn()
        count = 0
        for wkb in cutline['wkbs']:
            try:
                geom = ogr.CreateGeometryFromWkb(wkb)
            except Exception:
                geom = None
            if geom is None:
                continue
            feat = ogr.Feature(defn)
            feat.SetGeometry(geom)
            layer.CreateFeature(feat)
            feat = None
            count += 1
        layer = None
        defn = None
        ds = None
        if count == 0:
            raise RuntimeError('The AOI has no usable polygon geometries to clip with.')

    def _clip_raster(self, raster_path, cutline, output_path, progress=(68, 80)):
        """Clip the NDVI raster to the AOI polygons (worker thread). Equivalent to the previous
        'gdal:cliprasterbymasklayer' call: keep resolution, -tap, crop to cutline, nodata -9999."""
        src = gdal.Open(raster_path, gdal.GA_ReadOnly)
        if src is None:
            raise RuntimeError('GDAL could not open the NDVI raster for clipping.')
        gt = src.GetGeoTransform()
        xres, yres = abs(gt[1]), abs(gt[5])
        src = None
        cut_path = f'/vsimem/ndvi_cutline_{threading.get_ident()}_{int(time.time() * 1000)}.gpkg'

        def cb(frac, msg, data):
            self._progress(progress[0] + int((progress[1] - progress[0]) * frac))
            return 0 if self._is_cancelled() else 1

        out = None
        try:
            self._write_cutline(cutline, cut_path)
            if os.path.exists(output_path):
                os.remove(output_path)
            options = gdal.WarpOptions(
                format='GTiff', outputType=gdal.GDT_Float32, xRes=xres, yRes=yres,
                targetAlignedPixels=True, cutlineDSName=cut_path, cutlineLayer='MASK',
                cropToCutline=True, dstNodata=-9999.0, multithread=True,
                creationOptions=['COMPRESS=DEFLATE', 'PREDICTOR=2', 'TILED=YES'],
                callback=cb)
            out = gdal.Warp(output_path, raster_path, options=options)
            if out is None:
                self._check_cancel()
                raise RuntimeError('GDAL could not clip the NDVI raster to the AOI.')
        finally:
            out = None
            try:
                gdal.Unlink(cut_path)
            except Exception:
                pass
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError('NDVI clip did not create the requested GeoTIFF.')

    def _export_zonal_csv(self, layer, csv_path):
        fields = layer.fields()
        field_names = [f.name() for f in fields]
        # Keep geometry out of the CSV: this is intended as a statistics table.
        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(field_names)
            for feat in layer.getFeatures():
                writer.writerow([feat[name] for name in field_names])

    def _add_ndvi_layer(self, path, prefix, scene_id):
        layer = QgsRasterLayer(path, f'NDVI - {scene_id}', 'gdal')
        if not layer.isValid():
            self._log('Warning: NDVI raster could not be added to the project.')
            return
        # Minimal, portable renderer: five classes centered around zero/vegetation range.
        from qgis.core import QgsColorRampShader, QgsRasterShader, QgsSingleBandPseudoColorRenderer
        shader = QgsRasterShader()
        ramp = QgsColorRampShader()
        interpolation = getattr(QgsColorRampShader, 'Interpolated', None)
        if interpolation is None:
            from qgis.core import Qgis
            interpolation = Qgis.ShaderInterpolationMethod.Linear
        ramp.setColorRampType(interpolation)
        ramp.setColorRampItemList([
            QgsColorRampShader.ColorRampItem(-1.0, QColor('#8c510a'), '< -0.2'),
            QgsColorRampShader.ColorRampItem(-0.2, QColor('#d8b365'), '-0.2'),
            QgsColorRampShader.ColorRampItem(0.0, QColor('#f6e8c3'), '0.0'),
            QgsColorRampShader.ColorRampItem(0.2, QColor('#c7eae5'), '0.2'),
            QgsColorRampShader.ColorRampItem(0.4, QColor('#5ab4ac'), '0.4'),
            QgsColorRampShader.ColorRampItem(0.6, QColor('#01665e'), '0.6'),
            QgsColorRampShader.ColorRampItem(1.0, QColor('#003c30'), '1.0'),
        ])
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        QgsProject.instance().addMapLayer(layer)

    def _log(self, message):
        """Thread-safe: also called from background workers (delivered on the GUI thread)."""
        self.log_message.emit(str(message))

    def _append_log(self, message):
        self.log_edit.append(message)
        self.log_edit.ensureCursorVisible()

    def _progress(self, value):
        """Thread-safe progress update (de-duplicated so block loops do not flood the GUI)."""
        value = int(value)
        if value == self._last_progress:
            return
        self._last_progress = value
        self.progress_changed.emit(value)
        task = self._worker_task
        if task is not None:
            try:
                task.setProgress(float(value))
            except RuntimeError:
                pass

    def _set_progress(self, value):
        self.progress.setValue(value)

    def _is_cancelled(self):
        if self._cancel_event.is_set():
            return True
        task = self._worker_task
        if task is not None:
            try:
                return bool(task.isCanceled())
            except RuntimeError:
                return False
        return False

    def _check_cancel(self):
        if self._is_cancelled():
            raise WorkflowCancelled()

    def _start_task(self, description, worker, args, on_finished, indeterminate=False, operation=None):
        """Run `worker(task, *args)` in a QGIS background task; `on_finished` runs on the GUI thread."""
        self._cancel_event.clear()
        self._last_progress = -1
        self._worker_task = None
        self._set_busy(True, indeterminate)
        self._operation = operation
        task = QgsTask.fromFunction(description, worker, *args, on_finished=on_finished)
        self._task = task
        QgsApplication.taskManager().addTask(task)

    def _set_busy(self, busy, indeterminate=False):
        self._busy = busy
        if not busy:
            self._operation = None
        self.search_btn.setEnabled(not busy)
        self.run_btn.setEnabled(not busy)
        self.refresh_aoi_btn.setEnabled(not busy)
        self.auto_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        if busy and indeterminate:
            self.progress.setRange(0, 0)     # busy indicator
        else:
            self.progress.setRange(0, 100)

    def cancel_running(self):
        """Cancel button: stop the running background job (the GUI stays responsive meanwhile)."""
        if not self._busy:
            return
        self._cancel_event.set()
        if self._operation == 'search':
            # A search has no files to clean up, so release the UI at once. The worker may still be blocked
            # in an HTTP read (up to its timeout); its late result is discarded via the token.
            self._search_token += 1
            self._task = None
            self._worker_task = None
            self._set_busy(False)
            self._log('Search cancelled.')
            return
        self._log('Cancelling\u2026')
        if self._zonal_feedback is not None:
            self._zonal_feedback.cancel()
            task = self._task
            if task is not None:
                try:
                    task.cancel()
                except RuntimeError:
                    pass

    def shutdown(self):
        """Called when the plugin unloads: stop background work before the dialog goes away."""
        self._shutting_down = True
        self._cancel_event.set()
        if self._zonal_feedback is not None:
            self._zonal_feedback.cancel()

    def _error(self, message):
        self._log('ERROR: ' + str(message))
        QMessageBox.critical(self, 'NDVI Extractor v2', str(message))
