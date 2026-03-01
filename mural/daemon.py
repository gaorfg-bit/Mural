from __future__ import annotations

import logging
from typing import Optional

from gi.repository import Gio, GLib

logger = logging.getLogger("wallpaper")

class MuralDaemonProxy:
    """D-Bus proxy to the mural daemon. Used by the app if the daemon is running."""

    DBUS_NAME = "io.github.mural"
    DBUS_PATH = "/io/github/mural"
    DBUS_IFACE = "io.github.mural.Control"

    def __init__(self):
        self._proxy: Optional[Gio.DBusProxy] = None
        self._available = False
        self._try_connect()

    def _try_connect(self) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                bus,
                Gio.DBusProxyFlags.NONE,
                None,
                self.DBUS_NAME,
                self.DBUS_PATH,
                self.DBUS_IFACE,
                None,
            )
            # Check that the service really responds
            self._proxy.call_sync(
                "GetCurrentWallpaper",
                None,
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            self._available = True
            logger.info("D-Bus Daemon available")
            return True
        except Exception as e:
            logger.info("Daemon not available (local fallback): %s", e)
            self._available = False
            return False

    @property
    def available(self) -> bool:
        return self._available

    def _call(self, method: str, params=None):
        if not self._available or not self._proxy:
            return None
        try:
            return self._proxy.call_sync(
                method,
                params,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as e:
            logger.warning("D-Bus call %s failed: %s", method, e)
            self._available = False
            return None

    def next_wallpaper(self):
        self._call("NextWallpaper", None)

    def set_wallpaper(self, path: str) -> bool:
        res = self._call("SetWallpaper", GLib.Variant("(s)", (path,)))
        if res is None:
            return False
        try:
            return bool(res.unpack()[0])
        except Exception:
            return False

    def toggle_slideshow(self) -> Optional[bool]:
        res = self._call("ToggleSlideshow", None)
        if res is None:
            return None
        try:
            return bool(res.unpack()[0])
        except Exception:
            return None

    def set_slideshow_enabled(self, enabled: bool):
        self._call("SetSlideshowEnabled", GLib.Variant("(b)", (bool(enabled),)))

    def reload_config(self):
        self._call("ReloadConfig", None)

    def get_current_wallpaper(self) -> str:
        res = self._call("GetCurrentWallpaper", None)
        if res is None:
            return ""
        try:
            return str(res.unpack()[0])
        except Exception:
            return ""
