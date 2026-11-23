def classFactory(iface):  # noqa: N802
    from .plugin import AutoContourPlugin
    return AutoContourPlugin(iface)
