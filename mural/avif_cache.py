from __future__ import annotations

"""
avif_cache.py — On-demand AVIF cache for Mural

Uses ImageMagick (convert/magick) for AVIF conversion.
No extra Python dependencies — ImageMagick is available
on all Linux distros via package manager.
  Debian/Ubuntu : sudo apt install imagemagick
  Fedora        : sudo dnf install ImageMagick
  Arch          : sudo pacman -S imagemagick

Architecture:
- Storage: .mural_cache/ hidden in each image folder
- Conversion: explicit on-demand only (button in UI)
- GNOME background: user choice via toggle in settings
- Originals are NEVER modified or deleted.
"""

import logging
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

logger = logging.getLogger("mural.avif_cache")

AVIF_QUALITY = 55       # 0-100, ~60-75% de gain selon la source
AVIF_WORKERS = 3
CACHE_DIRNAME = ".mural_cache"


def _find_imagemagick() -> Optional[str]:
    """Returns the path of the available ImageMagick command."""
    # ImageMagick 7 : commande 'magick'
    # ImageMagick 6 : commande 'convert'
    for cmd in ("magick", "convert"):
        path = shutil.which(cmd)
        if path:
            # Check that it is indeed ImageMagick and not another 'convert'
            try:
                out = subprocess.run(
                    [path, "--version"],
                    capture_output=True, text=True, timeout=5
                ).stdout
                if "ImageMagick" in out:
                    return path
            except Exception:
                pass
    return None


def avif_supported() -> bool:
    """Checks if ImageMagick is available and supports AVIF."""
    cmd = _find_imagemagick()
    if not cmd:
        return False
    try:
        # Check that the AVIF format is listed in supported formats
        out = subprocess.run(
            [cmd, "-list", "format"],
            capture_output=True, text=True, timeout=10
        ).stdout
        return "AVIF" in out.upper()
    except Exception:
        return False


# Evaluated once at module load
AVIF_SUPPORTED = avif_supported()
_IMAGEMAGICK_CMD = _find_imagemagick() if AVIF_SUPPORTED else None

logger.debug(
    "AVIF support: %s (cmd: %s)", AVIF_SUPPORTED, _IMAGEMAGICK_CMD
)


def cache_dir_for(folder: Path) -> Path:
    return folder / CACHE_DIRNAME


def avif_path_for(original: Path) -> Path:
    """ex: /img/foo.jpg → /img/.mural_cache/foo.avif"""
    return cache_dir_for(original.parent) / (original.stem + ".avif")


def get_cached_avif(original: str) -> Optional[Path]:
    """
    Returns the AVIF path if it exists and is newer than the original.
    Does NOT trigger implicit conversion.
    """
    orig = Path(original)
    avif = avif_path_for(orig)
    if not avif.exists():
        return None
    try:
        if avif.stat().st_mtime >= orig.stat().st_mtime:
            return avif
        avif.unlink(missing_ok=True)  # original newer → invalid
    except Exception:
        pass
    return None


class FolderConverter:
    """
    Converts all images in a folder to AVIF in .mural_cache/
    via ImageMagick. On-demand conversion only.
    """

    def __init__(self):
        self._executor = ThreadPoolExecutor(
            max_workers=AVIF_WORKERS,
            thread_name_prefix="mural-avif",
        )
        self._active_folder: Optional[str] = None
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()

    def convert_folder(
        self,
        folder: Path,
        valid_extensions: set[str],
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        on_done: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """
        Starts conversion of a whole folder in the background.

        Callbacks (called on GLib thread):
          on_progress(converted, total, current_filename)
          on_done(converted, total)
        """
        if not AVIF_SUPPORTED:
            logger.warning("ImageMagick not available — conversion impossible")
            if on_done:
                GLib.idle_add(on_done, 0, 0)
            return

        with self._lock:
            if self._active_folder is not None:
                logger.warning("Conversion already in progress: %s", self._active_folder)
                return
            self._active_folder = str(folder)
            self._cancel_event.clear()

        threading.Thread(
            target=self._folder_worker,
            args=(folder, valid_extensions, on_progress, on_done),
            daemon=True,
        ).start()

    def cancel(self) -> None:
        self._cancel_event.set()

    def is_running(self) -> bool:
        with self._lock:
            return self._active_folder is not None

    def _folder_worker(
        self,
        folder: Path,
        valid_extensions: set[str],
        on_progress: Optional[Callable],
        on_done: Optional[Callable],
    ) -> None:
        converted = 0
        total = 0
        try:
            files = [
                f for f in sorted(folder.iterdir())
                if f.is_file()
                and f.suffix.lower() in valid_extensions
                and f.suffix.lower() != ".avif"
            ]
            total = len(files)
            if total == 0:
                return

            cache_dir_for(folder).mkdir(exist_ok=True)

            futures: list[tuple[Path, Future]] = [
                (fpath, self._executor.submit(self._convert_one, fpath))
                for fpath in files
                if not self._cancel_event.is_set()
            ]

            for fpath, future in futures:
                if self._cancel_event.is_set():
                    future.cancel()
                    continue
                ok = future.result()
                if ok:
                    converted += 1
                if on_progress:
                    GLib.idle_add(on_progress, converted, total, fpath.name)

        except Exception as e:
            logger.error("Folder conversion error [%s]: %s", folder, e)
        finally:
            with self._lock:
                self._active_folder = None
            if on_done:
                GLib.idle_add(on_done, converted, total)

    def _convert_one(self, original: Path) -> bool:
        """Converts a file via ImageMagick. Runs in ThreadPoolExecutor."""
        if not _IMAGEMAGICK_CMD:
            return False

        dest = avif_path_for(original)

        # Already up to date?
        if dest.exists():
            try:
                if dest.stat().st_mtime >= original.stat().st_mtime:
                    return True
            except Exception:
                pass

        try:
            result = subprocess.run(
                [
                    _IMAGEMAGICK_CMD,
                    str(original),
                    "-quality", str(AVIF_QUALITY),
                    "-define", "heic:speed=6",  # fast encode
                    str(dest),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(
                    "ImageMagick error [%s]: %s",
                    original.name, result.stderr.strip()
                )
                dest.unlink(missing_ok=True)
                return False

            orig_size = original.stat().st_size
            avif_size = dest.stat().st_size
            ratio = (1 - avif_size / orig_size) * 100 if orig_size else 0
            logger.debug("✓ %s → .avif (−%.0f%%)", original.name, ratio)
            return True

        except subprocess.TimeoutExpired:
            logger.warning("Timeout converting %s", original.name)
            dest.unlink(missing_ok=True)
            return False
        except Exception as e:
            logger.warning("Convert error [%s]: %s", original.name, e)
            dest.unlink(missing_ok=True)
            return False

    def folder_stats(self, folder: Path, valid_extensions: set[str]) -> dict:
        """Stats on the AVIF cache of a folder."""
        files = []
        if folder.exists():
            try:
                files = [
                    f for f in folder.iterdir()
                    if f.is_file()
                    and f.suffix.lower() in valid_extensions
                    and f.suffix.lower() != ".avif"
                ]
            except Exception:
                pass

        total = len(files)
        cached = 0
        size_orig = 0
        size_avif = 0

        for f in files:
            avif = avif_path_for(f)
            try:
                size_orig += f.stat().st_size
            except Exception:
                pass
            if avif.exists():
                cached += 1
                try:
                    size_avif += avif.stat().st_size
                except Exception:
                    pass

        saving = (1 - size_avif / size_orig) * 100 if size_orig and size_avif else 0
        return {
            "total": total,
            "cached": cached,
            "size_original_mb": size_orig / 1_048_576,
            "size_avif_mb": size_avif / 1_048_576,
            "saving_pct": saving,
        }

    def purge_folder(self, folder: Path) -> int:
        """Deletes all AVIFs from .mural_cache/ for a folder."""
        cache_dir = cache_dir_for(folder)
        removed = 0
        if cache_dir.exists():
            for f in cache_dir.glob("*.avif"):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
        logger.info("Purged %d AVIF from %s", removed, folder)
        return removed

    def shutdown(self) -> None:
        self._cancel_event.set()
        self._executor.shutdown(wait=False, cancel_futures=True)
