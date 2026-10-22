from __future__ import annotations

"""
wallpaper_ops.py — opérations d'application du fond d'écran, partagées.

Avant, cette logique existait en deux copies (mural/backend.py côté app,
mural/mural-daemon-service.py côté daemon) qui avaient divergé. Elle vit
désormais ici ; les deux processus délèguent.

Moniteur = tuple (connector, x, y, width, height) en pixels logiques
(même convention que le portal GNOME : le scaling physique est appliqué
par GNOME lui-même).
"""

import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageOps

try:
    from mural.io_utils import locked_file
except ImportError:  # exécution directe depuis le dossier mural/
    from io_utils import locked_file

logger = logging.getLogger("mural.wallpaper_ops")

CACHE_PREFIX = "mural_universal_"

MonitorGeom = Tuple[str, int, int, int, int]


def default_cache_dir() -> Path:
    # Import tardif : le module reste testable sans GTK/GI.
    from gi.repository import GLib
    return Path(GLib.get_user_cache_dir()) / "mural"


def set_gnome_background(uri: str) -> None:
    """Applique l'image via gsettings (uri + uri-dark + spanned)."""
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


def composite_key(assignments: Dict[str, str], monitors: Sequence[MonitorGeom]) -> str:
    """Hash descriptif (assignments + mtimes + géométrie des moniteurs).

    Remplace le md5 du canvas brut (~45 Mo de pixels hashés à chaque
    application). Même contenu → même nom de fichier (pas de réencodage
    PNG inutile) ; contenu différent (image remplifiée, mtime changé) →
    nom différent, ce qui force GNOME à rafraîchir ses textures.
    """
    desc: Dict[str, object] = {
        "monitors": [list(m) for m in monitors],
        "images": {},
    }
    images = desc["images"]
    for conn, path in sorted(assignments.items()):
        p = Path(path)
        try:
            st = p.stat()
            images[conn] = [str(p), st.st_mtime_ns, st.st_size]
        except OSError:
            images[conn] = [str(p), -1, -1]
    return hashlib.md5(json.dumps(desc, sort_keys=True).encode()).hexdigest()[:10]


def build_canvas(
    assignments: Dict[str, str], monitors: Sequence[MonitorGeom]
) -> Image.Image:
    """Canvas couvrant la bbox des moniteurs, cover-crop centré par moniteur.

    Les moniteurs sans image valide restent noirs. L'orientation EXIF est
    respectée (JPEG d'appareil photo portrait), comme le fait GNOME quand
    il affiche l'original directement.
    """
    min_x = min(m[1] for m in monitors)
    min_y = min(m[2] for m in monitors)
    max_x = max(m[1] + m[3] for m in monitors)
    max_y = max(m[2] + m[4] for m in monitors)

    canvas = Image.new("RGB", (max_x - min_x, max_y - min_y), (0, 0, 0))

    for conn, x, y, w, h in monitors:
        img_path = assignments.get(conn)
        if not img_path or not Path(img_path).exists():
            continue
        try:
            with Image.open(img_path) as src:
                img = ImageOps.exif_transpose(src)
                img_ratio = img.width / img.height
                mon_ratio = w / h
                if img_ratio > mon_ratio:
                    # image plus large que l'écran -> caler sur la hauteur
                    new_h = h
                    new_w = int(new_h * img_ratio)
                else:
                    # image plus haute que l'écran -> caler sur la largeur
                    new_w = w
                    new_h = int(new_w / img_ratio)
                resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - w) // 2
                top = (new_h - h) // 2
                canvas.paste(
                    resized.crop((left, top, left + w, top + h)),
                    (x - min_x, y - min_y),
                )
        except Exception as e:
            logger.warning("Composite: skip %s (%s)", img_path, e)

    return canvas


def purge_old_composites(cache: Path, keep: Path) -> None:
    """Supprime les composites obsolètes, en gardant le fichier courant."""
    for old in cache.glob(CACHE_PREFIX + "*.png"):
        if old == keep:
            continue
        try:
            old.unlink()
        except OSError:
            pass


def apply_composite(
    assignments: Dict[str, str],
    monitors: Sequence[MonitorGeom],
    cache: Optional[Path] = None,
) -> Optional[Path]:
    """Génère et applique le composite multi-moniteurs. Retourne le chemin.

    Ordre volontairement sûr : écrire le nouveau fichier D'ABORD, appliquer
    via gsettings ENSUITE, purger les anciens ENFIN (hors le courant). Un
    échec d'écriture ne peut donc plus laisser gsettings pointer sur un
    fichier supprimé.

    La section est verrouillée : l'app, le daemon et le slideshow local
    peuvent appliquer simultanément sans se supprimer mutuellement leur
    fichier (regression connue de l'ancien « purge globale avant save »).
    """
    cache = cache or default_cache_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        with locked_file(cache / "composite"):
            final_path = cache / f"{CACHE_PREFIX}{composite_key(assignments, monitors)}.png"
            if not final_path.exists():
                canvas = build_canvas(assignments, monitors)
                canvas.save(final_path, "PNG")
            set_gnome_background(final_path.as_uri())
            purge_old_composites(cache, final_path)
        return final_path
    except Exception as e:
        logger.error("apply_composite failed: %s", e)
        return None
