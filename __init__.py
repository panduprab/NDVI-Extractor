"""NDVI Extractor v2 QGIS plugin."""


def classFactory(iface):
    from .ndvi_extractor_v2 import NDVIExtractorV2
    return NDVIExtractorV2(iface)
