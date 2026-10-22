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
import time
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib, Gio

try:
    from mural.io_utils import atomic_save_json, ensure_private_dir, load_json, locked_file
    from mural.wallpaper_ops import apply_composite, set_gnome_background
except Exception:
    from io_utils import atomic_save_json, ensure_private_dir, load_json, locked_file
    from wallpaper_ops import apply_composite, set_gnome_background

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mural-daemon")

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".config" / "mural" / "settings.json"

def load_config() -> dict:
    ensure_private_dir(CONFIG_FILE.parent)
    try:
        if CONFIG_FILE.exists():
            with locked_file(CONFIG_FILE):
                return load_json(CONFIG_FILE)
    except json.JSONDecodeError:
        logger.warning("Invalid daemon JSON config detected, using defaults")
        try:
            corrupt = CONFIG_FILE.with_suffix(".json.corrupt")
            CONFIG_FILE.replace(corrupt)
            logger.warning("Corrupt daemon config moved to: %s", corrupt)
        except Exception:
            pass
    except Exception as e:
        logger.error("Failed to load config: %s", e)
    return {}

def save_config(cfg: dict):
    ensure_private_dir(CONFIG_FILE.parent)
    try:
        with locked_file(CONFIG_FILE):
            atomic_save_json(CONFIG_FILE, cfg, mode=0o600)
    except Exception as e:
        logger.error("Failed to save config: %s", e)


def update_config(updates: dict) -> None:
    """Read-modify-write ciblé de la config.

    Recharge le fichier frais sous le verrou, applique uniquement `updates`
    puis sauve. Évite le « lost update » historique : le daemon écrasait
    tout settings.json avec son snapshot mémoire, effaçant les changements
    que l'app venait d'écrire (bookmarks, dossier courant, etc.).
    """
    ensure_private_dir(CONFIG_FILE.parent)
    try:
        with locked_file(CONFIG_FILE):
            fresh: dict = {}
            if CONFIG_FILE.exists():
                try:
                    fresh = load_json(CONFIG_FILE)
                except Exception:
                    fresh = {}
            if not isinstance(fresh, dict):
                fresh = {}
            fresh.update(updates)
            atomic_save_json(CONFIG_FILE, fresh, mode=0o600)
    except Exception as e:
        logger.error("Failed to update config: %s", e)

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
    """Applique une image unique via gsettings (même fond partout)."""
    set_gnome_background(Path(path).as_uri())
    logger.info("Applied wallpaper: %s", path)

def apply_wallpaper_composite(path: str, cfg: dict):
    """Applique le composite multi-moniteurs, avec repli sur une image unique."""
    try:
        monitors_raw = detect_monitors_fallback()
        if len(monitors_raw) <= 1:
            apply_wallpaper(path)
            return

        per_monitor = cfg.get("per_monitor", {}) or {}
        assignments = {}
        for mon in monitors_raw:
            img_path = per_monitor.get(mon["connector"]) or path
            if not os.path.exists(img_path):
                img_path = path
            if img_path and os.path.exists(img_path):
                assignments[mon["connector"]] = img_path

        geoms = [
            (m["connector"], m["x"], m["y"], m["width"], m["height"])
            for m in monitors_raw
        ]
        if apply_composite(assignments, geoms) is None:
            apply_wallpaper(path)
        else:
            logger.info("Applied composite: %s", path)

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
    <method name="SetConfigJson">
      <arg type="s" direction="in" name="json"/>
      <arg type="b" direction="out"/>
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
        self._last_wallpaper_change: float = 0.0
        self._wallpaper_rate_limit: float = 0.5

        self._reload_config()
        # Restaure le wallpaper courant depuis la config : sans cela,
        # GetCurrentWallpaper renvoie "" jusqu'au premier changement et
        # l'UI ne peut pas se resynchroniser au démarrage.
        self._current_wallpaper = next(
            iter((self._cfg.get("per_monitor") or {}).values()), ""
        )
        self._start_slideshow_if_needed()

    def _get_sender_uid(self, connection: Gio.DBusConnection, sender: str) -> Optional[int]:
        if not sender:
            return None
        try:
            result = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixUser",
                GLib.Variant("(s)", (sender,)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            return int(result.unpack()[0])
        except Exception as e:
            logger.warning("Cannot resolve sender uid for %s: %s", sender, e)
            return None

    def _is_authorized_sender(self, connection: Gio.DBusConnection, sender: str) -> bool:
        uid = self._get_sender_uid(connection, sender)
        if uid is None:
            return False
        return uid == os.getuid()

    def _reload_config(self):
        self._cfg = load_config()
        logger.info("Config loaded. Slideshow: %s, interval: %dmin",
                    self._cfg.get("slideshow_enabled"), self._cfg.get("slideshow_interval", 10))

    def _get_playlist(self, connector: Optional[str] = None) -> List[str]:
        """Playlist du moniteur, sans repli implicite sur la liste globale.

        Aligné sur WallpaperSettings.resolve_slideshow_playlist (mode
        « 100 % manuel ») : un moniteur sans favoris dédiés ne joue rien,
        au lieu de jouer la liste globale — l'ancien désaccord faisait
        diverger le slideshow selon que le daemon tournait ou non.
        """
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

    def _restart_slideshow_timer(self) -> None:
        """Reprogramme le timer avec l'intervalle courant (stop + start)."""
        self._stop_slideshow()
        self._start_slideshow_if_needed()

    def _sync_slideshow_runtime(self, old_enabled: bool, old_interval: int) -> None:
        """Applique un éventuel changement d'état/intervalle après rechargement."""
        new_enabled = bool(self._cfg.get("slideshow_enabled", False))
        new_interval = int(self._cfg.get("slideshow_interval", 10) or 10)
        if not new_enabled:
            self._stop_slideshow()
        elif (
            (not old_enabled)
            or (new_interval != old_interval)
            or (self._slideshow_timeout_id is None)
        ):
            self._restart_slideshow_timer()

    def _tick(self) -> bool:
        # Toujours repartir d'une config fraîche : l'app peut avoir écrit
        # entre deux ticks (playlist, favoris par moniteur, etc.).
        self._reload_config()
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
        # Écriture ciblée : ne jamais réécrire tout le fichier depuis un
        # snapshot mémoire (perte des changements faits par l'app).
        update_config({"per_monitor": per_monitor})
        self._current_wallpaper = next(iter(per_monitor.values()), "")
        apply_wallpaper_composite(next(iter(per_monitor.values()), ""), self._cfg)
        return True

    def _apply(self, path: str):
        """SetWallpaper : cette image devient le fond de tous les écrans.

        On assigne les moniteurs DÉTECTÉS sans effacer d'éventuelles clés
        existantes : si Mutter ne répond pas (détection dégradée sur
        « default »), on ne détruit pas un mapping multi-écrans valide.
        """
        self._current_wallpaper = path
        self._reload_config()
        monitors = detect_monitors_fallback()
        per_monitor = dict(self._cfg.get("per_monitor") or {})
        for mon in monitors:
            per_monitor[mon["connector"]] = path
        self._cfg["per_monitor"] = per_monitor
        update_config({"per_monitor": per_monitor})
        apply_wallpaper_composite(path, self._cfg)

    def _set_config_json(self, json_str: str) -> bool:
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict):
                return False
        except Exception:
            return False

        old_enabled = bool(self._cfg.get("slideshow_enabled", False))
        old_interval = int(self._cfg.get("slideshow_interval", 10) or 10)

        save_config(data)
        self._reload_config()
        self._sync_slideshow_runtime(old_enabled, old_interval)

        return True

    # ── D-Bus method handlers ────────────────────────────────────────────────

    def handle_method_call(self, connection, sender, path, iface, method, params, invocation):
        try:
            if not self._is_authorized_sender(connection, sender):
                logger.warning("Unauthorized D-Bus caller: sender=%s", sender)
                invocation.return_dbus_error(
                    "io.github.mural.Error.Unauthorized",
                    "Unauthorized caller",
                )
                return

            if method == "GetCurrentWallpaper":
                invocation.return_value(GLib.Variant("(s)", (self._current_wallpaper,)))

            elif method == "NextWallpaper":
                self._tick()
                if self._cfg.get("slideshow_enabled"):
                    # Repartir d'un compte à rebours complet après un
                    # déclenchement manuel (l'ancien code appelait un
                    # _start_slideshow_if_needed() qui était un no-op).
                    self._restart_slideshow_timer()
                invocation.return_value(None)

            elif method == "SetWallpaper":
                path_arg = params.unpack()[0]
                now = time.monotonic()
                if now - self._last_wallpaper_change < self._wallpaper_rate_limit:
                    invocation.return_value(GLib.Variant("(b)", (False,)))
                    return

                image_path = Path(path_arg)
                if image_path.exists() and image_path.is_file():
                    self._last_wallpaper_change = now
                    self._apply(path_arg)
                    invocation.return_value(GLib.Variant("(b)", (True,)))
                else:
                    invocation.return_value(GLib.Variant("(b)", (False,)))

            elif method == "ToggleSlideshow":
                enabled = not self._cfg.get("slideshow_enabled", False)
                self._cfg["slideshow_enabled"] = enabled
                update_config({"slideshow_enabled": enabled})
                if enabled:
                    self._start_slideshow_if_needed()
                else:
                    self._stop_slideshow()
                invocation.return_value(GLib.Variant("(b)", (enabled,)))

            elif method == "SetSlideshowEnabled":
                enabled = params.unpack()[0]
                self._cfg["slideshow_enabled"] = enabled
                update_config({"slideshow_enabled": enabled})
                if enabled:
                    self._start_slideshow_if_needed()
                else:
                    self._stop_slideshow()
                invocation.return_value(None)

            elif method == "SetConfigJson":
                json_arg = params.unpack()[0]
                ok = self._set_config_json(json_arg)
                invocation.return_value(GLib.Variant("(b)", (ok,)))

            elif method == "ReloadConfig":
                old_enabled = bool(self._cfg.get("slideshow_enabled", False))
                old_interval = int(self._cfg.get("slideshow_interval", 10) or 10)
                self._reload_config()
                # Un changement d'intervalle doit réellement reprogrammer
                # le timer : c'était l'angle mort (ReloadConfig ne faisait
                # que recharger, le timer gardait l'ancien rythme).
                self._sync_slideshow_runtime(old_enabled, old_interval)
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
