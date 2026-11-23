"""Qt5 / Qt6 and QGIS-version helpers (field types, enums)."""
from qgis.core import QgsField
from qgis.PyQt.QtCore import QVariant

try:                                    # QGIS >= 3.38 / Qt6
    from qgis.PyQt.QtCore import QMetaType
    _TYPES = {
        "str": QMetaType.Type.QString,
        "dbl": QMetaType.Type.Double,
        "int": QMetaType.Type.Int,
    }
    QgsField("probe", _TYPES["str"])
except (ImportError, AttributeError, TypeError):   # older QGIS
    _TYPES = {
        "str": QVariant.Type.String,
        "dbl": QVariant.Type.Double,
        "int": QVariant.Type.Int,
    }


def make_field(name, kind):
    """kind: 'str' | 'dbl' | 'int'."""
    return QgsField(name, _TYPES[kind])


def _enum(path_new, path_old):
    from qgis import core
    for path in (path_new, path_old):
        obj = core
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            return obj
        except AttributeError:
            continue
    raise AttributeError(path_new)


MSG_INFO = _enum("Qgis.MessageLevel.Info", "Qgis.Info")
GEOM_POLYGON = _enum("Qgis.GeometryType.Polygon", "QgsWkbTypes.PolygonGeometry")
NET_OK = _enum("QgsBlockingNetworkRequest.ErrorCode.NoError", "QgsBlockingNetworkRequest.NoError")
