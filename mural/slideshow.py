from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib

logger = logging.getLogger("wallpaper")

class SlideshowManager:
    """Automatic wallpaper rotation."""

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
            return True

        # Selection logic
        if self._app.settings.slideshow_random:
            import random
            path = random.choice(playlist)
        else:
            self._sequential_index %= len(playlist)
            path = playlist[self._sequential_index]
            self._sequential_index += 1

        # 1. Update local state
        for mon in self._app.monitors:
            self._app.settings.per_monitor[mon.connector] = path
            
        # 2. SAVE to disk
        self._app.config.save(self._app.settings) 

        # 3. WAKE UP THE DAEMON
        if self._app._daemon.available:
            self._app._daemon.reload_config()
            self._app._daemon.set_wallpaper(path) # Push the order directly

        # 4. Apply visually
        threading.Thread(
            target=self._apply_composite_async,
            args=(path, self._app.current_mode, self._app.apply_to_lockscreen, [m.connector for m in self._app.monitors]),
            daemon=True
        ).start()

        return True

    def _apply_composite_async(
        self, path: str, mode: str, lock: bool, target_monitors: list
    ) -> None:
        """Applies the multi-monitor composite in a separate thread (outside UI thread)."""
        assignments = {}
        for mon in self._app.monitors:
            if mon.connector in target_monitors:
                assignments[mon.connector] = path
            else:
                # Keep current background on other screens
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
