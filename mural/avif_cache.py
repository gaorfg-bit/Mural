from __future__ import annotations

"""
avif_cache.py — Cache AVIF à la demande pour Mural

Utilise ImageMagick (convert/magick) pour la conversion AVIF.
Aucune dépendance Python supplémentaire — ImageMagick est disponible
sur toutes les distributions Linux via le gestionnaire de paquets.
  Debian/Ubuntu : sudo apt install imagemagick
  Fedora        : sudo dnf install ImageMagick
  Arch          : sudo pacman -S imagemagick

Architecture :
- Stockage  : .mural_cache/ caché dans chaque dossier d'images
- Conversion : à la demande explicite uniquement (bouton dans l'UI)
- Fond GNOME : choix utilisateur via toggle dans les settings
- Les originaux ne sont JAMAIS modifiés ni supprimés.
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
    """Retourne le chemin de la commande ImageMagick disponible."""
    # ImageMagick 7 : commande 'magick'
    # ImageMagick 6 : commande 'convert'
    for cmd in ("magick", "convert"):
        path = shutil.which(cmd)
        if path:
            # Vérifier que c'est bien ImageMagick et pas un autre 'convert'
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
    """Vérifie qu'ImageMagick est disponible et supporte AVIF."""
    cmd = _find_imagemagick()
    if not cmd:
        return False
    try:
        # Vérifier que le format AVIF est listé dans les formats supportés
        out = subprocess.run(
            [cmd, "-list", "format"],
            capture_output=True, text=True, timeout=10
        ).stdout
        return "AVIF" in out.upper()
    except Exception:
        return False


# Évalué une fois au chargement du module
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
    Retourne le chemin AVIF s'il existe et est plus récent que l'original.
    Ne lance AUCUNE conversion implicite.
    """
    orig = Path(original)
    avif = avif_path_for(orig)
    if not avif.exists():
        return None
    try:
        if avif.stat().st_mtime >= orig.stat().st_mtime:
            return avif
        avif.unlink(missing_ok=True)  # original plus récent → invalide
    except Exception:
        pass
    return None


class FolderConverter:
    """
    Convertit toutes les images d'un dossier en AVIF dans .mural_cache/
    via ImageMagick. Conversion à la demande uniquement.
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
        Lance la conversion de tout un dossier en arrière-plan.

        Callbacks (appelés sur le thread GLib) :
          on_progress(converted, total, current_filename)
          on_done(converted, total)
        """
        if not AVIF_SUPPORTED:
            logger.warning("ImageMagick non disponible — conversion impossible")
            if on_done:
                GLib.idle_add(on_done, 0, 0)
            return

        with self._lock:
            if self._active_folder is not None:
                logger.warning("Conversion déjà en cours: %s", self._active_folder)
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
        """Convertit un fichier via ImageMagick. Tourne dans le ThreadPoolExecutor."""
        if not _IMAGEMAGICK_CMD:
            return False

        dest = avif_path_for(original)

        # Déjà à jour ?
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
                    "-define", "heic:speed=6",  # encode rapide
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
        """Stats sur le cache AVIF d'un dossier."""
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
        """Supprime tous les AVIF de .mural_cache/ pour un dossier."""
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
