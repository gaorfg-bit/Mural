from __future__ import annotations
import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional
from PIL import Image
from gi.repository import GLib
from .models import MonitorInfo

logger = logging.getLogger("wallpaper")

class GnomeBackend:
    # On restreint les modes pour forcer la stabilité
    MODES = [("spanned", "Multi-Écrans (Spanned)"), ("zoom", "Écran Unique (Zoom)")]

    def __init__(self):
        pass

    def _force_gsettings(self, uri: str):
        """Force les clés GSettings via CLI pour bypasser l'UI de GNOME."""
        base = "org.gnome.desktop.background"
        # On applique à toutes les variantes (clair/sombre) pour éviter le 'saut'
        cmds = [
            f"gsettings set {base} picture-options 'spanned'",
            f"gsettings set {base} picture-uri '{uri}'",
            f"gsettings set {base} picture-uri-dark '{uri}'"
        ]
        for cmd in cmds:
            subprocess.run(cmd, shell=True, check=False)

    def _create_canvas(self, assignments, monitors):
        """Crée le canevas exact vu dans ta capture image_2341dd.jpg."""
        min_x = min(m.x for m in monitors)
        min_y = min(m.y for m in monitors)
        max_x = max(m.x + m.width for m in monitors)
        max_y = max(m.y + m.height for m in monitors)

        # Ton setup: 4512x2560
        canvas = Image.new("RGB", (max_x - min_x, max_y - min_y), (0, 0, 0))

        for mon in monitors:
            path = assignments.get(mon.connector)
            if not path or not os.path.exists(path): continue

            with Image.open(path) as img:
                # Zoom intelligent par écran
                img_ratio = img.width / img.height
                mon_ratio = mon.width / mon.height
                if img_ratio > mon_ratio:
                    new_h = mon.height
                    new_w = int(new_h * img_ratio)
                else:
                    new_w = mon.width
                    new_h = int(new_w / img_ratio)

                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                # Crop au centre de l'image pour chaque moniteur
                l = (new_w - mon.width) // 2
                t = (new_h - mon.height) // 2
                img = img.crop((l, t, l + mon.width, t + mon.height))

                # Collage aux positions logiques: (0,0) et (1440,0)
                canvas.paste(img, (mon.x - min_x, mon.y - min_y))
        return canvas

    def apply_per_monitor(self, assignments, mode="spanned", lock=True, monitors=None):
        if not monitors: return {}

        canvas = self._create_canvas(assignments, monitors)
        cache_dir = Path(GLib.get_user_cache_dir()) / "mural"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Nom unique pour forcer Mutter à vider son cache texture
        img_hash = hashlib.md5(canvas.tobytes()).hexdigest()[:10]
        final_path = cache_dir / f"mural_fix_{img_hash}.png"

        for old in cache_dir.glob("mural_fix_*.png"):
            try: old.unlink()
            except: pass

        canvas.save(final_path, "PNG")
        self._force_gsettings(final_path.as_uri())
        return {m.connector: True for m in monitors}

    def apply_single(self, path, mode="zoom", lock=True):
        self._force_gsettings(Path(path).as_uri())
        subprocess.run(f"gsettings set org.gnome.desktop.background picture-options '{mode}'", shell=True)
        return True

    def get_current(self): return ""
    def get_mode(self): return "spanned"
    def is_dark_mode(self): return True
