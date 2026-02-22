from __future__ import annotations

import logging
from typing import List

from gi.repository import Gdk

from .models import MonitorInfo

logger = logging.getLogger("wallpaper")

class MonitorDetector:
    @staticmethod
    def detect() -> List[MonitorInfo]:
        monitors = []

        # Méthode GDK
        try:
            display = Gdk.Display.get_default()
            if display:
                monitors_list = display.get_monitors()
                for i in range(monitors_list.get_n_items()):
                    mon = monitors_list.get_item(i)
                    geo = mon.get_geometry()
                    connector = ""
                    try:
                        connector = mon.get_connector() or ""
                    except AttributeError:
                        pass
                    name = mon.get_model() or f"Écran {i + 1}"
                    if connector:
                        name = f"{name} ({connector})"
                    monitors.append(MonitorInfo(
                        name=name,
                        connector=connector or f"output-{i}",
                        width=geo.width,
                        height=geo.height,
                        x=geo.x,
                        y=geo.y,
                        primary=(i == 0),
                        serial=f"{i}",
                    ))
        except Exception as e:
            logger.error("GDK detect error: %s", e)

        if not monitors:
            monitors.append(MonitorInfo(
                name="Écran principal",
                connector="default",
                width=1920,
                height=1080,
                x=0,
                y=0,
                primary=True,
                serial="0",
            ))

        return monitors
