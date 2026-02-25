from __future__ import annotations

import json
import logging
from pathlib import Path

from gi.repository import GLib

from .models import WallpaperSettings

logger = logging.getLogger("wallpaper")

import importlib.util

class Config:
    _BASE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".svg"}
    VALID_EXT = (
        _BASE_EXT | {".avif"}
        if importlib.util.find_spec("pillow_avif") is not None
        else _BASE_EXT
    )
    CONFIG_FILE = Path.home() / ".config" / "mural" / "settings.json"
    THUMB_DIR = Path(GLib.get_user_cache_dir()) / "mural" / "thumbnails"
    AVIF_DIR = Path(GLib.get_user_cache_dir()) / "mural" / "avif"  # XDG_CACHE_HOME — compatible Flatpak sandbox
    THUMBNAIL_ASPECT = 220 / 360
    THUMBNAIL_SIZE = 120
    THUMB_W = THUMBNAIL_SIZE
    THUMB_H = max(1, int(round(THUMBNAIL_SIZE * THUMBNAIL_ASPECT)))
    MAX_IMAGES = 500
    PREVIEW_MAX_HEIGHT = 200
    PREVIEW_MIN_HEIGHT = 140

    def __init__(self):
        self.THUMB_DIR.mkdir(parents=True, exist_ok=True)
        self.AVIF_DIR.mkdir(parents=True, exist_ok=True)
        self.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def default_folder() -> Path:
        # GLib.get_user_special_dir est Flatpak-safe, pas de subprocess
        try:
            pictures = GLib.get_user_special_dir(
                GLib.UserDirectory.DIRECTORY_PICTURES
            )
            if pictures:
                p = Path(pictures)
                if p.exists():
                    return p
        except Exception as e:
            logger.warning("GLib.get_user_special_dir failed: %s", e)
        for name in ("Images", "Pictures"):
            p = Path.home() / name
            if p.exists():
                return p
        return Path.home()

    def load(self) -> WallpaperSettings:
        try:
            if self.CONFIG_FILE.exists():
                with open(self.CONFIG_FILE) as f:
                    return WallpaperSettings.from_dict(json.load(f))
        except Exception:
            pass
        return WallpaperSettings(folder=str(self.default_folder()))

    def save(self, s: WallpaperSettings):
        try:
            with open(self.CONFIG_FILE, "w") as f:
                json.dump(s.to_dict(), f, indent=2)
        except Exception as e:
            logger.error("Save error: %s", e)
