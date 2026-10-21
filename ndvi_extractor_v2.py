import os

from qgis.PyQt.QtCore import Qt, QDate, QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
    QDateEdit, QDoubleSpinBox, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QCheckBox, QLineEdit, QFileDialog, QMessageBox, QProgressBar,
    QTextEdit, QGroupBox
)
from qgis.core import QgsProject

from . import resources
from .ndvi_dialog import NDVIDialog


class NDVIExtractorV2:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dialog = None

    def initGui(self):
        self.action = QAction(QIcon(os.path.join(self.plugin_dir, 'icon.svg')),
                              'NDVI Extractor v2', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu('&NDVI Extractor', self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu('&NDVI Extractor', self.action)
            self.iface.removeToolBarIcon(self.action)
        if self.dialog:
            self.dialog.shutdown()
            self.dialog.close()

    def run(self):
        if self.dialog is None:
            self.dialog = NDVIDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
