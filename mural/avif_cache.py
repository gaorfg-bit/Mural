from __future__ import annotations

"""
avif_cache.py — Cache AVIF à la demande pour Mural

Architecture :
- Stockage  : .mural_cache/ caché dans chaque dossier d'images
              ex: /home/user/Images/.mural_cache/photo.avif
- Conversion : uniquement à la demande explicite (bouton dans l'UI)
- Fond GNOME : toggle utilisateur — servir l'AVIF ou toujours l'original
- Les originaux ne sont JAMAIS modifiés ni supprimés.
- quality=55 : ~60-75% de gain selon la source.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

logger = logging.getLogger("mural.avif_cache")

AVIF_QUALITY = 55
AVIF_WORKERS = 3
CACHE_DIRNAME = ".mural_cache"


def avif_supported() -> bool:
    """Vérifie que Pillow peut encoder en AVIF (pillow-avif-plugin installé)."""
    try:
        from PIL import features
        return features.check("avif")
    except Exception:
        return False


AVIF_SUPPORTED = avif_supported()


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
        avif.unlink(missing_ok=True)  # invalide — original plus récent
    except Exception:
        pass
    return None


class FolderConverter:
    """
    Convertit toutes les images d'un dossier en AVIF dans .mural_cache/.
    Conversion uniquement à la demande explicite, hors thread UI.
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
            logger.warning("pillow-avif-plugin non disponible — conversion impossible")
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
        """Annule la conversion en cours."""
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

            # Soumettre toutes les conversions au pool
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
        """Convertit un fichier. Tourne dans le ThreadPoolExecutor."""
        dest = avif_path_for(original)

        # Déjà à jour ?
        if dest.exists():
            try:
                if dest.stat().st_mtime >= original.stat().st_mtime:
                    return True
            except Exception:
                pass

        try:
            from PIL import Image
            with Image.open(str(original)) as img:
                if img.mode in ("RGBA", "LA", "PA"):
                    img = img.convert("RGBA")
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(str(dest), "AVIF", quality=AVIF_QUALITY)

            orig_size = original.stat().st_size
            avif_size = dest.stat().st_size
            ratio = (1 - avif_size / orig_size) * 100 if orig_size else 0
            logger.debug("✓ %s → .avif (−%.0f%%)", original.name, ratio)
            return True
        except Exception as e:
            logger.warning("AVIF encode error [%s]: %s", original.name, e)
            dest.unlink(missing_ok=True)
            return False

    def folder_stats(self, folder: Path, valid_extensions: set[str]) -> dict:
        """
        Stats rapides sur le cache AVIF d'un dossier.
        Retourne: {total, cached, size_original_mb, size_avif_mb, saving_pct}
        """
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
