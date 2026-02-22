from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib

logger = logging.getLogger("wallpaper")

class SlideshowManager:
    """Rotation automatique du fond d'écran."""

    def __init__(self, app: "WallpaperApp"):
        self._app = app
        self._timeout_id: Optional[int] = None
        self._history: List[str] = []
        self._sequential_index: int = 0

    def start(self) -> None:
        self.stop()
        minutes = self._app.settings.slideshow_interval
        ms = max(60_000, minutes * 60 * 1000)
        self._timeout_id = GLib.timeout_add(ms, self._tick)
        logger.info("Slideshow started — %dmin", minutes)

    def stop(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
            logger.info("Slideshow stopped")

    def is_running(self) -> bool:
        return self._timeout_id is not None

    def next(self) -> None:
        self._tick()
        if self.is_running():
            self.start()

    def _tick(self) -> bool:
        playlist = self._app.settings.resolve_slideshow_playlist()
        if not playlist:
            self._app._status("⚠ Slideshow : aucune image sélectionnée")
            return True

        if self._app.settings.slideshow_random:
            import random
            half = max(1, len(playlist) // 2)
            choices = [p for p in playlist if p not in self._history[-half:]]
            if not choices:
                self._history.clear()
                choices = playlist
            path = random.choice(choices)
            self._history.append(path)
        else:
            self._sequential_index %= len(playlist)
            path = playlist[self._sequential_index]
            self._sequential_index += 1

        mode = self._app.mode_ids[self._app.mode_dropdown.get_selected()]
        lock = self._app.chk_lock.get_active()
        target_monitors = self._app.settings.slideshow_monitors
        if not target_monitors:
            # Tous les écrans — image unique globale
            if getattr(self._app, "_daemon", None) and self._app._daemon.available:
                ok = self._app._daemon.set_wallpaper(path)
                if not ok:
                    self._app._status("✗ Slideshow daemon — fallback local")
                    ok = self._app.backend.apply_single(
                        path, mode=mode, lock=lock
                    )
            else:
                ok = self._app.backend.apply_single(
                    path, mode=mode, lock=lock
                )
            if ok:
                self._app._status(f"⏱ Slideshow: {Path(path).name}")
                GLib.idle_add(self._app._set_active_wallpapers, [path])
                GLib.idle_add(self._app._update_preview, path)
            else:
                logger.error("Slideshow apply failed: %s", path)
        else:
            # Composite multi-monitor — traitement PIL lourd → thread séparé
            threading.Thread(
                target=self._apply_composite_async,
                args=(path, mode, lock, list(target_monitors)),
                daemon=True,
            ).start()

        return True

    def _apply_composite_async(
        self, path: str, mode: str, lock: bool, target_monitors: list
    ) -> None:
        """Applique le composite multi-monitor dans un thread séparé (hors thread UI)."""
        assignments = {}
        for mon in self._app.monitors:
            if mon.connector in target_monitors:
                assignments[mon.connector] = path
            else:
                # Conserver le fond actuel sur les autres écrans
                current = self._app.settings.per_monitor.get(mon.connector, path)
                assignments[mon.connector] = current
        results = self._app.backend.apply_per_monitor(
            assignments, mode, lock,
            monitors=self._app.monitors
        )
        ok = any(results.values())
        if ok:
            GLib.idle_add(self._app._status, f"⏱ Slideshow: {Path(path).name}")
            GLib.idle_add(self._app._set_active_wallpapers, [path])
            GLib.idle_add(self._app._update_preview, path)
        else:
            logger.error("Slideshow composite apply failed: %s", path)
