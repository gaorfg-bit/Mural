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
        self._sequential_index_per_monitor: dict[str, int] = {}

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
        target_connectors = (
            list(self._app.settings.slideshow_monitors)
            if self._app.settings.slideshow_monitors
            else [m.connector for m in self._app.monitors]
        )

        assignments: dict[str, str] = dict(self._app.settings.per_monitor)
        updated = False

        for connector in target_connectors:
            playlist = self._app.settings.resolve_slideshow_playlist(connector)
            if not playlist:
                # No dedicated favorites for this monitor: keep current image.
                continue
            if self._app.settings.slideshow_random:
                import random
                chosen = random.choice(playlist)
            else:
                idx = self._sequential_index_per_monitor.get(connector, 0) % len(playlist)
                chosen = playlist[idx]
                self._sequential_index_per_monitor[connector] = idx + 1
            assignments[connector] = chosen
            updated = True

        if not updated:
            return True

        self._app.settings.per_monitor = assignments

        # 2. SAVE to disk
        self._app.config.save(self._app.settings)

        # 3. Apply visually
        threading.Thread(
            target=self._apply_composite_async,
            args=(assignments, self._app.current_mode, self._app.apply_to_lockscreen),
            daemon=True
        ).start()

        return True

    def _apply_composite_async(
        self, assignments: dict[str, str], mode: str, lock: bool
    ) -> None:
        """Applies the multi-monitor composite in a separate thread (outside UI thread)."""
        final_assignments = {}
        for mon in self._app.monitors:
            current = assignments.get(mon.connector) or self._app.settings.per_monitor.get(mon.connector)
            if current:
                final_assignments[mon.connector] = current
        results = self._app.backend.apply_per_monitor(
            final_assignments, mode, lock,
            monitors=self._app.monitors
        )
        ok = any(results.values())
        if ok:
            sample = next(iter(final_assignments.values()), "")
            if sample:
                GLib.idle_add(self._app._status, f"Slideshow: {Path(sample).name}")
                GLib.idle_add(self._app._update_preview, sample)
            GLib.idle_add(self._app._set_active_wallpapers, list(set(final_assignments.values())))
        else:
            logger.error("Slideshow composite apply failed: %s", final_assignments)
