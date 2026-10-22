from __future__ import annotations
import logging
from pathlib import Path
from typing import Dict, List

from gi.repository import Gio

from mural.models import MonitorInfo
from mural.wallpaper_ops import apply_composite, set_gnome_background

logger = logging.getLogger("wallpaper")

class GnomeBackend:
    # Modes disponibles dans l'interface Mural
    MODES = [
        ("spanned", "Independent Multi-Monitor (Recommended)"),
        ("zoom", "Same image everywhere (Zoom)")
    ]

    def __init__(self):
        self.settings_schema = "org.gnome.desktop.background"

    def apply_per_monitor(
        self,
        assignments: Dict[str, str],
        mode: str = "spanned",
        lock: bool = True,
        monitors: List[MonitorInfo] = None,
    ) -> Dict[str, bool]:
        """Génère et applique le composite pour tous les moniteurs.

        La logique vit dans mural.wallpaper_ops (partagée avec le daemon).
        """
        if not monitors:
            return {}
        geoms = [(m.connector, m.x, m.y, m.width, m.height) for m in monitors]
        applied = apply_composite(assignments, geoms)
        ok = applied is not None and applied.exists()
        return {m.connector: ok for m in monitors}

    def apply_single(self, path: str, mode: str = "zoom", lock: bool = True) -> bool:
        """Applique une image unique (même fond partout).

        Note : `mode` est historique — GNOME reçoit toujours « spanned »,
        qui sur un espace virtuel mono-écran est équivalent au zoom.
        """
        if not Path(path).exists():
            return False
        set_gnome_background(Path(path).as_uri())
        return True

    def get_current(self) -> str:
        return ""

    def get_mode(self) -> str:
        return "spanned"

    def is_dark_mode(self) -> bool:
        try:
            # Thème du système hôte
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            return "dark" in settings.get_string("color-scheme").lower()
        except Exception:
            return True
