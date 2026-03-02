from __future__ import annotations
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from PIL import Image
from gi.repository import Gio, GLib

from models import MonitorInfo

logger = logging.getLogger("wallpaper")

class GnomeBackend:
    # Modes available in the Mural interface
    MODES = [
        ("spanned", "Independent Multi-Monitor (Recommended)"),
        ("zoom", "Same image everywhere (Zoom)")
    ]

    def __init__(self):
        self.settings_schema = "org.gnome.desktop.background"

    def _force_gnome_settings(self, uri: str, mode: str):
        import subprocess
        cmds = [
            ["gsettings", "set", "org.gnome.desktop.background", "picture-options", "spanned"],
            ["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri],
            ["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri],
        ]
        for cmd in cmds:
            try:
                subprocess.run(cmd, check=False)
            except Exception as e:
                logger.warning("gsettings failed: %s", e)

    def _generate_universal_canvas(self, assignments: Dict[str, str], monitors: List[MonitorInfo]) -> Image.Image:
        """
        Universal geometry formula. Adapts to any monitor topology.
        """
        # 1. Compute global bounding box (handles negative coordinates)
        min_x = min(m.x for m in monitors)
        min_y = min(m.y for m in monitors)
        max_x = max(m.x + m.width for m in monitors)
        max_y = max(m.y + m.height for m in monitors)

        canvas_width = max_x - min_x
        canvas_height = max_y - min_y

        # Empty canvas (black for dead zones between monitors)
        canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))

        # 2. Process each monitor independently
        for mon in monitors:
            img_path = assignments.get(mon.connector)
            if not img_path or not os.path.exists(img_path):
                continue

            with Image.open(img_path) as img:
                # 3. Compute perfect ratio (cover-fit without distortion)
                img_ratio = img.width / img.height
                mon_ratio = mon.width / mon.height

                if img_ratio > mon_ratio:
                    # Image is wider than screen -> fit to height
                    new_h = mon.height
                    new_w = int(new_h * img_ratio)
                else:
                    # Image is taller than screen -> fit to width
                    new_w = mon.width
                    new_h = int(new_w / img_ratio)

                # High quality resize
                img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # Center crop
                left = (new_w - mon.width) // 2
                top = (new_h - mon.height) // 2
                right = left + mon.width
                bottom = top + mon.height
                img_cropped = img_resized.crop((left, top, right, bottom))

                # 4. Paste onto canvas (translate relative to origin)
                paste_x = mon.x - min_x
                paste_y = mon.y - min_y
                canvas.paste(img_cropped, (paste_x, paste_y))

        return canvas

    def apply_per_monitor(self, assignments: Dict[str, str], mode: str = "spanned", lock: bool = True, monitors: List[MonitorInfo] = None) -> Dict[str, bool]:
        """
        Generates and applies the composite wallpaper for all monitors.
        """
        if not monitors:
            return {}

        canvas = self._generate_universal_canvas(assignments, monitors)

        # Cache directory
        cache_dir = Path(GLib.get_user_cache_dir()) / "mural"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Unique MD5 hash to force GNOME to refresh desktop textures
        img_hash = hashlib.md5(canvas.tobytes()).hexdigest()[:10]
        final_path = cache_dir / f"mural_universal_{img_hash}.png"

        # Clean up old cached files to avoid disk pollution
        for old_file in cache_dir.glob("mural_universal_*.png"):
            try: old_file.unlink()
            except Exception: pass

        # Save and force apply in spanned mode
        canvas.save(final_path, "PNG")
        self._force_gnome_settings(final_path.as_uri(), "spanned")

        return {m.connector: True for m in monitors}

    def apply_single(self, path: str, mode: str = "zoom", lock: bool = True) -> bool:
        """Applies a single image for users who want the same wallpaper everywhere."""
        if not os.path.exists(path):
            return False
        self._force_gnome_settings(Path(path).as_uri(), mode)
        return True

    def get_current(self) -> str:
        return ""

    def get_mode(self) -> str:
        return "spanned"

    def is_dark_mode(self) -> bool:
        try:
            # Check host system theme
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            return "dark" in settings.get_string("color-scheme").lower()
        except:
            return True
