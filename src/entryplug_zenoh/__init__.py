"""Optional native-Zenoh compute integration; not imported by Entryplug core."""

from entryplug_zenoh.detector import NativeDetectorServer, ZenohDetectorWorker

__all__ = ["NativeDetectorServer", "ZenohDetectorWorker"]
