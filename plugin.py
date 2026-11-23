import os

from qgis.PyQt import sip
from qgis.PyQt.QtCore import QEvent, QObject
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QApplication

from .dialog import AutoContourDialog

MENU = "&AutoContour"


class FollowMainWindow(QObject):
    """Minimise and restore the AutoContour windows together with the QGIS main window."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.hidden = []

    def _windows(self):
        dlg = self.plugin.dialog
        out = []
        if dlg is not None and not sip.isdeleted(dlg):
            out.append(dlg)
            viewer = getattr(dlg, "viewer", None)
            if viewer is not None and not sip.isdeleted(viewer):
                out.append(viewer)
        return out

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.WindowStateChange and obj is self.plugin.iface.mainWindow():
            if obj.isMinimized():
                self.hidden = [w for w in self._windows() if w.isVisible() and not w.isMinimized()]
                for w in self.hidden:
                    w.showMinimized()
            elif self.hidden:
                for w in self.hidden:
                    if not sip.isdeleted(w):
                        w.showNormal()
                        w.raise_()
                self.hidden = []
        return False


class AutoContourPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dialog = None
        self.follow = None

    def initGui(self):
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.png"))
        self.action = QAction(icon, "AutoContour: draw an area, get contours",
                              self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToRasterMenu(MENU, self.action)
        self.iface.addToolBarIcon(self.action)
        self.follow = FollowMainWindow(self)
        self.iface.mainWindow().installEventFilter(self.follow)

    def unload(self):
        if self.follow is not None:
            self.iface.mainWindow().removeEventFilter(self.follow)
            self.follow = None
        self.iface.removePluginRasterMenu(MENU, self.action)
        self.iface.removeToolBarIcon(self.action)
        if self.dialog:
            self.dialog.reset_state()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def run(self):
        if self.dialog is not None and sip.isdeleted(self.dialog):
            self.dialog = None
        if self.dialog is None:
            # No parent: own taskbar entry, so the window can be found after it is minimised.
            self.dialog = AutoContourDialog(self.iface, None)
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.dialog.close)
        else:
            pass
        if self.dialog.isMinimized():
            self.dialog.showNormal()
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
