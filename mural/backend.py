from __future__ import annotations
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional
from PIL import Image
from gi.repository import Gio, GLib

# Import de tes modèles (assure-toi que MonitorInfo a bien x, y, width, height, connector)
from models import MonitorInfo

logger = logging.getLogger("wallpaper")

class GnomeBackend:
    # Les modes que ton interface Mural proposera
    MODES = [
        ("spanned", "Multi-Écrans Indépendants (Recommandé)"),
        ("zoom", "Même image partout (Zoom)")
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
        La formule géométrique universelle. S'adapte à toute topologie d'écrans.
        """
        # 1. Détection du Bounding Box global (Gère les coordonnées négatives)
        min_x = min(m.x for m in monitors)
        min_y = min(m.y for m in monitors)
        max_x = max(m.x + m.width for m in monitors)
        max_y = max(m.y + m.height for m in monitors)

        canvas_width = max_x - min_x
        canvas_height = max_y - min_y

        # Création de la toile vide (Noir par défaut pour les zones mortes)
        canvas = Image.new("RGB", (canvas_width, canvas_height), (0, 0, 0))

        # 2. Traitement indépendant de chaque moniteur
        for mon in monitors:
            img_path = assignments.get(mon.connector)
            if not img_path or not os.path.exists(img_path):
                continue

            with Image.open(img_path) as img:
                # 3. Calcul du ratio parfait (Objectif-Fit sans déformation)
                img_ratio = img.width / img.height
                mon_ratio = mon.width / mon.height

                if img_ratio > mon_ratio:
                    # L'image est plus panoramique que l'écran -> on cale sur la hauteur
                    new_h = mon.height
                    new_w = int(new_h * img_ratio)
                else:
                    # L'image est plus carrée que l'écran -> on cale sur la largeur
                    new_w = mon.width
                    new_h = int(new_w / img_ratio)

                # Redimensionnement haute qualité
                img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                # Recadrage par le centre
                left = (new_w - mon.width) // 2
                top = (new_h - mon.height) // 2
                right = left + mon.width
                bottom = top + mon.height
                img_cropped = img_resized.crop((left, top, right, bottom))

                # 4. Collage sur la toile universelle (Translation relative au point zéro)
                paste_x = mon.x - min_x
                paste_y = mon.y - min_y
                canvas.paste(img_cropped, (paste_x, paste_y))

        return canvas

    def apply_per_monitor(self, assignments: Dict[str, str], mode: str = "spanned", lock: bool = True, monitors: List[MonitorInfo] = None) -> Dict[str, bool]:
        """
        Génère et applique le montage pour chaque utilisateur.
        """
        if not monitors:
            return {}

        canvas = self._generate_universal_canvas(assignments, monitors)

        # Gestion du cache (Création du dossier si inexistant)
        cache_dir = Path(GLib.get_user_cache_dir()) / "mural"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # Empreinte MD5 unique pour forcer le rafraîchissement des textures du bureau
        img_hash = hashlib.md5(canvas.tobytes()).hexdigest()[:10]
        final_path = cache_dir / f"mural_universal_{img_hash}.png"

        # Nettoyage de l'ancien cache pour ne pas polluer le disque des utilisateurs
        for old_file in cache_dir.glob("mural_universal_*.png"):
            try: old_file.unlink()
            except Exception: pass

        # Sauvegarde et application forcée en mode "spanned"
        canvas.save(final_path, "PNG")
        self._force_gnome_settings(final_path.as_uri(), "spanned")

        return {m.connector: True for m in monitors}

    def apply_single(self, path: str, mode: str = "zoom", lock: bool = True) -> bool:
        """Applique une image standard pour ceux qui veulent la même partout."""
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
            # Vérifie le thème du système hôte
            settings = Gio.Settings.new("org.gnome.desktop.interface")
            return "dark" in settings.get_string("color-scheme").lower()
        except:
            return True
