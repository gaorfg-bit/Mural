#!/usr/bin/env python3
"""
Mural Wallpaper Daemon — D-Bus service
Runs in background, handles slideshow independently of the app.
"""
from __future__ import annotations

import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib, Gio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mural-daemon")

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".config" / "mural" / "settings.json"
CACHE_DIR = Path(GLib.get_user_cache_dir()) / "mural"

def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
    return {}

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        logger.error("Failed to save config: %s", e)

# ── Monitor detection (without GDK — uses Mutter D-Bus) ──────────────────────

def detect_monitors_fallback() -> list:
    """Returns a minimal monitor list using Mutter D-Bus, falls back to default."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        proxy = Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
            None,
        )
        result = proxy.call_sync(
            "GetCurrentState", None,
            Gio.DBusCallFlags.NONE, 3000, None,
        )
        # result: (serial, monitors, logical_monitors, properties)
        logical_monitors = result.unpack()[2]
        monitors = []
        for lm in logical_monitors:
            x, y, scale, transform, primary, assigned, props = lm
            for mon_path, modes, mon_props in assigned:
                for mode in modes:
                    if mode[6].get("is-current", False):
                        w = int(mode[1] / scale)
                        h = int(mode[2] / scale)
                        connector = mon_props.get("connector", f"output-{len(monitors)}")
                        monitors.append({
                            "connector": connector,
                            "x": x, "y": y,
                            "width": w, "height": h,
                        })
                        break
        if monitors:
            return monitors
    except Exception as e:
        logger.warning("Mutter D-Bus monitor detection failed: %s", e)

    return [{"connector": "default", "x": 0, "y": 0, "width": 1920, "height": 1080}]

# ── Wallpaper apply ───────────────────────────────────────────────────────────

def apply_wallpaper(path: str):
    """Applies wallpaper via gsettings (single image, works without composite)."""
    import subprocess
    uri = Path(path).as_uri()
    cmds = [
        ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri],
        ["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri],
        ["gsettings", "set", "org.gnome.desktop.background", "picture-options", "zoom"],
    ]
    for cmd in cmds:
        try:
            subprocess.run(cmd, check=False)
        except Exception as e:
            logger.warning("gsettings failed: %s", e)
    logger.info("Applied wallpaper: %s", path)

def apply_wallpaper_composite(path: str, cfg: dict):
    """Tries composite multi-monitor apply, falls back to single."""
    try:
        from PIL import Image
        import hashlib

        monitors_raw = detect_monitors_fallback()
        if len(monitors_raw) <= 1:
            apply_wallpaper(path)
            return

        per_monitor = cfg.get("per_monitor", {})
        min_x = min(m["x"] for m in monitors_raw)
        min_y = min(m["y"] for m in monitors_raw)
        max_x = max(m["x"] + m["width"] for m in monitors_raw)
        max_y = max(m["y"] + m["height"] for m in monitors_raw)

        canvas = Image.new("RGB", (max_x - min_x, max_y - min_y), (0, 0, 0))

        for mon in monitors_raw:
            img_path = per_monitor.get(mon["connector"], path)
            if not img_path or not os.path.exists(img_path):
                img_path = path
            if not os.path.exists(img_path):
                continue

            with Image.open(img_path) as img:
                img_ratio = img.width / img.height
                mon_ratio = mon["width"] / mon["height"]
                if img_ratio > mon_ratio:
                    new_h = mon["height"]
                    new_w = int(new_h * img_ratio)
                else:
                    new_w = mon["width"]
                    new_h = int(new_w / img_ratio)

                img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - mon["width"]) // 2
                top = (new_h - mon["height"]) // 2
                img_cropped = img_resized.crop((left, top, left + mon["width"], top + mon["height"]))
                canvas.paste(img_cropped, (mon["x"] - min_x, mon["y"] - min_y))

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        img_hash = hashlib.md5(canvas.tobytes()).hexdigest()[:10]
        final_path = CACHE_DIR / f"mural_universal_{img_hash}.png"

        for old in CACHE_DIR.glob("mural_universal_*.png"):
            try: old.unlink()
            except: pass

        canvas.save(final_path, "PNG")
        apply_wallpaper(str(final_path))

    except Exception as e:
        logger.warning("Composite failed, falling back to single: %s", e)
        apply_wallpaper(path)

# ── D-Bus service ─────────────────────────────────────────────────────────────

DBUS_XML = """
<node>
  <interface name="io.github.mural.Control">
    <method name="GetCurrentWallpaper">
      <arg type="s" direction="out"/>
    </method>
    <method name="NextWallpaper"/>
    <method name="SetWallpaper">
      <arg type="s" direction="in" name="path"/>
      <arg type="b" direction="out"/>
    </method>
    <method name="ToggleSlideshow">
      <arg type="b" direction="out"/>
    </method>
    <method name="SetSlideshowEnabled">
      <arg type="b" direction="in" name="enabled"/>
    </method>
    <method name="ReloadConfig"/>
  </interface>
</node>
"""

class MuralDaemon:
    DBUS_NAME = "io.github.mural"
    DBUS_PATH = "/io/github/mural"

    def __init__(self, loop: GLib.MainLoop):
        self._loop = loop
        self._cfg: dict = {}
        self._current_wallpaper: str = ""
        self._slideshow_timeout_id: Optional[int] = None
        self._sequential_index: int = 0
        self._sequential_index_per_monitor: dict[str, int] = {}

        self._reload_config()
        self._start_slideshow_if_needed()

    def _reload_config(self):
        self._cfg = load_config()
        logger.info("Config loaded. Slideshow: %s, interval: %dmin",
                    self._cfg.get("slideshow_enabled"), self._cfg.get("slideshow_interval", 10))

    def _get_playlist(self, connector: Optional[str] = None) -> List[str]:
        if connector:
            per_monitor = self._cfg.get("slideshow_images_per_monitor", {}) or {}
            images = per_monitor.get(connector, [])
        else:
            images = self._cfg.get("slideshow_images", [])
        return [p for p in images if Path(p).exists()]

    def _start_slideshow_if_needed(self):
        if self._slideshow_timeout_id is not None:
            return
        if self._cfg.get("slideshow_enabled"):
            minutes = max(1, self._cfg.get("slideshow_interval", 10))
            ms = minutes * 60 * 1000
            self._slideshow_timeout_id = GLib.timeout_add(ms, self._tick)
            logger.info("Slideshow started — every %dmin", minutes)

    def _stop_slideshow(self):
        if self._slideshow_timeout_id is not None:
            GLib.source_remove(self._slideshow_timeout_id)
            self._slideshow_timeout_id = None
            logger.info("Slideshow stopped")

    def _tick(self) -> bool:
        monitors = detect_monitors_fallback()
        target_connectors = self._cfg.get("slideshow_monitors") or [m["connector"] for m in monitors]
        per_monitor = dict(self._cfg.get("per_monitor", {}))
        updated = False

        for connector in target_connectors:
            playlist = self._get_playlist(connector)
            if not playlist:
                continue
            if self._cfg.get("slideshow_random", True):
                chosen = random.choice(playlist)
            else:
                idx = self._sequential_index_per_monitor.get(connector, 0) % len(playlist)
                chosen = playlist[idx]
                self._sequential_index_per_monitor[connector] = idx + 1
            per_monitor[connector] = chosen
            updated = True

        if not updated:
            logger.warning("Slideshow tick: no per-monitor playlist configured")
            return True

        self._cfg["per_monitor"] = per_monitor
        save_config(self._cfg)
        apply_wallpaper_composite(next(iter(per_monitor.values()), ""), self._cfg)
        return True

    def _apply(self, path: str):
        self._current_wallpaper = path
        # Keep existing per-monitor assignments when available.
        per_monitor = dict(self._cfg.get("per_monitor", {}))
        if not per_monitor:
            monitors = detect_monitors_fallback()
            for mon in monitors:
                per_monitor[mon["connector"]] = path
            self._cfg["per_monitor"] = per_monitor
        save_config(self._cfg)
        apply_wallpaper_composite(path, self._cfg)

    # ── D-Bus method handlers ────────────────────────────────────────────────

    def handle_method_call(self, connection, sender, path, iface, method, params, invocation):
        try:
            if method == "GetCurrentWallpaper":
                invocation.return_value(GLib.Variant("(s)", (self._current_wallpaper,)))

            elif method == "NextWallpaper":
                self._tick()
                if self._slideshow_timeout_id is not None:
                    self._start_slideshow_if_needed()
                invocation.return_value(None)

            elif method == "SetWallpaper":
                path_arg = params.unpack()[0]
                if os.path.exists(path_arg):
                    self._apply(path_arg)
                    invocation.return_value(GLib.Variant("(b)", (True,)))
                else:
                    invocation.return_value(GLib.Variant("(b)", (False,)))

            elif method == "ToggleSlideshow":
                enabled = not self._cfg.get("slideshow_enabled", False)
                self._cfg["slideshow_enabled"] = enabled
                save_config(self._cfg)
                if enabled:
                    self._start_slideshow_if_needed()
                else:
                    self._stop_slideshow()
                invocation.return_value(GLib.Variant("(b)", (enabled,)))

            elif method == "SetSlideshowEnabled":
                enabled = params.unpack()[0]
                self._cfg["slideshow_enabled"] = enabled
                save_config(self._cfg)
                if enabled:
                    self._start_slideshow_if_needed()
                else:
                    self._stop_slideshow()
                invocation.return_value(None)

            elif method == "ReloadConfig":
                self._reload_config()
                self._start_slideshow_if_needed()
                invocation.return_value(None)

            else:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD,
                    f"Unknown method: {method}"
                )
        except Exception as e:
            logger.error("D-Bus method %s error: %s", method, e)
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.FAILED, str(e)
            )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    loop = GLib.MainLoop()
    daemon = MuralDaemon(loop)

    node_info = Gio.DBusNodeInfo.new_for_xml(DBUS_XML)

    def on_bus_acquired(connection, name):
        connection.register_object(
            MuralDaemon.DBUS_PATH,
            node_info.interfaces[0],
            daemon.handle_method_call,
            None, None,
        )
        logger.info("D-Bus object registered at %s", MuralDaemon.DBUS_PATH)

    def on_name_lost(connection, name):
        logger.error("D-Bus name lost: %s — another instance running?", name)
        loop.quit()

    Gio.bus_own_name(
        Gio.BusType.SESSION,
        MuralDaemon.DBUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        None,
        on_name_lost,
    )

    logger.info("Mural daemon starting — D-Bus name: %s", MuralDaemon.DBUS_NAME)
    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("Daemon stopped by user")

if __name__ == "__main__":
    main()
