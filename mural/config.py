from __future__ import annotations

import json
import logging
from pathlib import Path

from gi.repository import GLib

from mural.models import WallpaperSettings
from mural.io_utils import (
    atomic_save_json,
    ensure_private_dir,
    load_json,
    locked_file,
)

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
    THUMBNAIL_SIZE = 180
    THUMB_W = THUMBNAIL_SIZE
    THUMB_H = max(1, int(round(THUMBNAIL_SIZE * THUMBNAIL_ASPECT)))
    MAX_IMAGES = 500
    PREVIEW_MAX_HEIGHT = 260
    PREVIEW_MIN_HEIGHT = 140

    def __init__(self):
        ensure_private_dir(self.THUMB_DIR)
        ensure_private_dir(self.AVIF_DIR)
        ensure_private_dir(self.CONFIG_FILE.parent)

    @staticmethod
    def default_folder() -> Path:
        # GLib.get_user_special_dir is Flatpak-safe, no subprocess
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
                with locked_file(self.CONFIG_FILE):
                    return WallpaperSettings.from_dict(load_json(self.CONFIG_FILE))
        except json.JSONDecodeError:
            logger.warning("Invalid JSON config detected, using defaults")
            try:
                corrupt = self.CONFIG_FILE.with_suffix(".json.corrupt")
                self.CONFIG_FILE.replace(corrupt)
                logger.warning("Corrupt config moved to: %s", corrupt)
            except Exception:
                pass
        except Exception:
            pass
        return WallpaperSettings(folder=str(self.default_folder()))

    def save(self, s: WallpaperSettings):
        try:
            ensure_private_dir(self.CONFIG_FILE.parent)
            with locked_file(self.CONFIG_FILE):
                atomic_save_json(self.CONFIG_FILE, s.to_dict(), mode=0o600)
        except Exception as e:
            logger.error("Save error: %s", e)
