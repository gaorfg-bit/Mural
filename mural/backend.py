from __future__ import annotations

import hashlib
import io
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image
from gi.repository import Gio, GLib

from .models import MonitorInfo
from .thumbnails import ImageLoader

logger = logging.getLogger("wallpaper")

class GnomeBackend:
    MODES = [
        ("zoom", "Zoom (recommandé"),
        ("scaled", "Ajusté"),
        ("stretched", "Étiré"),
        ("centered", "Centré"),
        ("wallpaper", "Mosaïque"),
        ("spanned", "Étendu multi-écrans"),
    ]

    def __init__(self):
        self._gsettings_bg: Optional[Gio.Settings] = None
        self._gsettings_lock: Optional[Gio.Settings] = None
        self.available = True

        try:
            self._gsettings_bg = Gio.Settings.new("org.gnome.desktop.background")
        except Exception as e:
            logger.warning("GSettings background schema unavailable: %s", e)

        try:
            self._gsettings_lock = Gio.Settings.new("org.gnome.desktop.screensaver")
        except Exception as e:
            logger.warning("GSettings screensaver schema unavailable: %s", e)

        session_env = os.environ.get("XDG_SESSION_TYPE", "").lower()
        self.session = "wayland" if "wayland" in session_env else "x11"

    def _on_wallpaper_set(self, proxy, result, user_data=None):
        try:
            proxy.call_with_unix_fd_list_finish(result)
        except Exception as e:
            logger.error("Wallpaper portal async callback error: %s", e)

    def _apply_gsettings_options(self, path: str, mode: str, lock: bool) -> None:
        """Applique picture-options et picture-uri-dark via GSettings (non-fatal)."""
        if not self._gsettings_bg:
            return
        try:
            uri = Gio.File.new_for_path(path).get_uri()
            self._gsettings_bg.set_string("picture-uri", uri)
            self._gsettings_bg.set_string("picture-uri-dark", uri)
            self._gsettings_bg.set_string("picture-options", mode)
        except Exception as e:
            logger.warning("GSettings options apply failed (non-fatal): %s", e)

        if lock and self._gsettings_lock:
            try:
                uri = Gio.File.new_for_path(path).get_uri()
                self._gsettings_lock.set_string("picture-uri", uri)
            except Exception as e:
                logger.warning("GSettings lock screen apply failed: %s", e)

    def set_wallpaper(self, file_path: str, lock: bool = True) -> bool:
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            try:
                proxy = Gio.DBusProxy.new_sync(
                    bus,
                    Gio.DBusProxyFlags.NONE,
                    None,
                    "org.freedesktop.portal.Desktop",
                    "/org/freedesktop/portal/desktop",
                    "org.freedesktop.portal.Wallpaper",
                    None,
                )
            except Exception as e:
                logger.error("Portal not available: %s", e)
                return False

            try:
                # Le portal reçoit un file descriptor, pas un path.
                # os.open() est intentionnel : on ouvre le FD côté app (dans le sandbox),
                # le portal lit le fichier via ce FD sans nécessiter filesystem=host.
                # C'est le pattern correct pour org.freedesktop.portal.Wallpaper.
                fd = os.open(file_path, os.O_RDONLY)
            except Exception as e:
                logger.error("Open error: %s", e)
                return False

            try:
                fd_list = Gio.UnixFDList.new()
                # Gio.UnixFDList.append() duplique le FD (dup2 interne).
                # Il est donc sûr de fermer le FD original dans le finally ci-dessous.
                fd_index = fd_list.append(fd)
            except Exception as e:
                os.close(fd)
                logger.error("FD append error: %s", e)
                return False
            finally:
                try:
                    # Fermeture du FD original (le portal utilise sa propre copie dupliquée).
                    os.close(fd)
                except Exception:
                    pass

            options = {
                "set-on": GLib.Variant("s", "both" if lock else "background"),
            }

            proxy.call_with_unix_fd_list(
                "SetWallpaperFile",
                GLib.Variant("(sha{sv})", ("", fd_index, options)),
                Gio.DBusCallFlags.NONE,
                -1,
                fd_list,
                None,
                self._on_wallpaper_set,
                None,
            )
            return True
        except Exception as e:
            logger.error("Wallpaper portal error: %s", e)
            return False

    def apply_single(self, path: str, mode: str = "zoom",
                     lock: bool = True,
                     target_connector: Optional[str] = None) -> bool:
        """Applique le fond d'écran sur GNOME 49+ Wayland sans nécessiter de logout.

        Stratégie double-canal :
        1. GSettings avec reset→set atomique : force gnome-shell à invalider
           son cache texture interne (Mutter recharge le fichier immédiatement).
           Le reset() sur picture-uri déclenche un signal "changed" que
           gnome-shell écoute — le set() qui suit charge la nouvelle image.
        2. Portal XDG en fallback non-bloquant : utile en sandbox Flatpak
           ou si GSettings n'est pas disponible.

        NE PAS utiliser portal seul hors sandbox : sur GNOME 49 non-sandboxé,
        le portal affiche une dialog de confirmation qui peut être ignorée.
        """
        if not self.available:
            return False
        if target_connector is not None:
            logger.warning("apply_single() ne peut pas cibler un écran spécifique.")
            return False
        if not Path(path).exists():
            logger.error("apply_single: fichier introuvable: %s", path)
            return False

        uri = Gio.File.new_for_path(path).get_uri()
        ok = False

        # ── Canal 1 : GSettings reset→set (GNOME 49 Wayland, hors sandbox) ──
        if self._gsettings_bg:
            try:
                # Le reset() génère un signal "changed" sur picture-uri,
                # ce qui force gnome-shell à libérer le cache texture de Mutter.
                # Le set() immédiatement après charge la nouvelle image.
                # Sans le reset préalable, gnome-shell ignore parfois le changement
                # si l'URI était déjà identique (même fichier, nouveau contenu).
                self._gsettings_bg.reset("picture-uri")
                self._gsettings_bg.reset("picture-uri-dark")
                self._gsettings_bg.set_string("picture-options", mode)
                self._gsettings_bg.set_string("picture-uri", uri)
                self._gsettings_bg.set_string("picture-uri-dark", uri)
                ok = True
                logger.info("Wallpaper applied via GSettings (GNOME 49): %s", path)
            except Exception as e:
                logger.warning("GSettings apply failed, trying portal: %s", e)

        if lock and self._gsettings_lock:
            try:
                self._gsettings_lock.set_string("picture-uri", uri)
                self._gsettings_lock.set_string("picture-options", mode)
            except Exception as e:
                logger.warning("GSettings lock screen failed (non-fatal): %s", e)

        # ── Canal 2 : Portal XDG (fallback Flatpak ou si GSettings indisponible) ──
        if not ok:
            ok = self.set_wallpaper(path, lock)
            if ok:
                logger.info("Wallpaper applied via portal (fallback): %s", path)

        return ok

    def apply_per_monitor(
        self,
        assignments: Dict[str, str],
        mode: str = "zoom",
        lock: bool = True,
        monitors: Optional[List[MonitorInfo]] = None,
    ) -> Dict[str, bool]:
        """
        Applique un fond différent par moniteur.
        assignments: {connector: image_path}

        Stratégie:
        Image composée (canvas) appliquée en spanned
        """
        results = {}
        results = self._apply_composite(assignments, mode, lock, monitors or [])

        return results

    def _apply_composite(
        self,
        assignments: Dict[str, str],
        mode: str,
        lock: bool,
        monitors: List[MonitorInfo],
    ) -> Dict[str, bool]:
        """
        Créer une image composée pour simuler le multi-monitor.
        Compose les images sur un canvas virtuel puis applique en 'spanned'.
        """
        if not monitors:
            return {}
        # Calculer les bounds du desktop virtuel
        min_x = min(m.x for m in monitors)
        min_y = min(m.y for m in monitors)
        max_x = max(m.x + m.width for m in monitors)
        max_y = max(m.y + m.height for m in monitors)

        canvas_w = max_x - min_x
        canvas_h = max_y - min_y

        canvas = Image.new("RGB", (canvas_w, canvas_h), (30, 30, 30))

        results = {}
        for mon in monitors:
            path = assignments.get(mon.connector, "")
            if not path or not Path(path).exists():
                results[mon.connector] = False
                continue

            img = ImageLoader.load_for_composite(path, mon.width, mon.height, mode)
            if img is None:
                results[mon.connector] = False
                continue
            paste_x = mon.x - min_x
            paste_y = mon.y - min_y
            canvas.paste(img, (paste_x, paste_y))
            results[mon.connector] = True

        # Sauvegarder le composite avec un nom unique à chaque génération.
        # GNOME Shell peut garder un FD ouvert sur "composite.png" et ne pas
        # détecter le changement si le nom de fichier reste identique.
        # Un hash du contenu garantit un nouveau path = nouveau FD = reload forcé.
        cache_dir = Path(GLib.get_user_cache_dir()) / "mural"
        cache_dir.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        img_bytes = buf.getvalue()
        img_hash = hashlib.md5(img_bytes).hexdigest()[:12]
        composite_path = cache_dir / f"composite_{img_hash}.png"
        # Supprimer les anciens composites pour ne pas remplir le cache
        for old_file in cache_dir.glob("composite_*.png"):
            if old_file != composite_path:
                try:
                    old_file.unlink()
                except Exception:
                    pass
        composite_path.write_bytes(img_bytes)

        # Appliquer en mode spanned
        self.apply_single(str(composite_path), "spanned", lock=lock)

        return results

    def get_current(self) -> str:
        if not self._gsettings_bg:
            return ""
        try:
            uri = self._gsettings_bg.get_string("picture-uri")
            if not uri:
                return ""
            gfile = Gio.File.new_for_uri(uri)
            return gfile.get_path() or ""
        except Exception as e:
            logger.error("get_current failed: %s", e)
            return ""

    def get_mode(self) -> str:
        if not self._gsettings_bg:
            return "zoom"
        try:
            mode = self._gsettings_bg.get_string("picture-options")
            return mode if mode else "zoom"
        except Exception:
            return "zoom"

    def set_dark_mode(self, dark: bool):
        if not self._gsettings_bg:
            return
        logger.debug("Dark mode requested (%s) but operation is no-op.", dark)

    def is_dark_mode(self) -> bool:
        if not self._gsettings_bg:
            return True
        try:
            val = self._gsettings_bg.get_string("color-scheme")
            return "dark" in val
        except Exception:
            return True
