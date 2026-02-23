from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

@dataclass
class MonitorInfo:
    name: str = ""
    connector: str = ""
    width: int = 1920
    height: int = 1080
    x: int = 0
    y: int = 0
    primary: bool = False
    serial: str = ""


@dataclass
class WallpaperSettings:
    folder: str = ""
    mode: str = "zoom"
    lock_screen: bool = True
    dark_mode: bool = True
    per_monitor: Dict[str, str] = field(default_factory=dict)
    paned_position: Optional[int] = None
    window_width: int = 1280
    window_height: int = 800
    window_maximized: bool = False
    slideshow_enabled: bool = False
    slideshow_interval: int = 10
    slideshow_random: bool = True
    slideshow_folders: List[str] = field(default_factory=list)
    slideshow_images: List[str] = field(default_factory=list)
    slideshow_excluded: List[str] = field(default_factory=list)
    slideshow_monitors: List[str] = field(default_factory=list)
    folder_bookmarks: List[str] = field(default_factory=list)
    monitor_folders: Dict[str, str] = field(default_factory=dict)
    avif_use_for_gnome: bool = False  # Servir l'AVIF à GNOME si disponible

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def resolve_slideshow_playlist(self) -> List[str]:
        """
        Retourne la liste finale des images pour le slideshow.
        Ordre de priorité :
        1. Toutes les images des dossiers inclus (slideshow_folders)
           sauf celles dans slideshow_excluded
        2. + Les images individuelles (slideshow_images)
        Dédupliqué, fichiers existants uniquement.
        """
        result: set[str] = set()

        for folder_str in self.slideshow_folders:
            folder = Path(folder_str)
            if not folder.exists():
                continue
            try:
                from .config import Config
                for f in folder.iterdir():
                    if f.is_file() and f.suffix.lower() in Config.VALID_EXT:
                        p = str(f)
                        if p not in self.slideshow_excluded:
                            result.add(p)
            except Exception:
                pass

        for img in self.slideshow_images:
            if Path(img).exists():
                result.add(img)

        return sorted(result)

    def is_in_slideshow(self, path: str) -> bool:
        """Retourne True si cette image fait partie du slideshow."""
        if path in self.slideshow_images:
            return True
        if path in self.slideshow_excluded:
            return False
        parent = str(Path(path).parent)
        return parent in self.slideshow_folders

    def add_to_slideshow(self, path: str) -> None:
        """Ajoute une image au slideshow."""
        parent = str(Path(path).parent)
        if parent in self.slideshow_folders:
            if path in self.slideshow_excluded:
                self.slideshow_excluded.remove(path)
        else:
            if path not in self.slideshow_images:
                self.slideshow_images.append(path)

    def remove_from_slideshow(self, path: str) -> None:
        """Retire une image du slideshow."""
        parent = str(Path(path).parent)
        if path in self.slideshow_images:
            self.slideshow_images.remove(path)
        if parent in self.slideshow_folders:
            if path not in self.slideshow_excluded:
                self.slideshow_excluded.append(path)

    def toggle_folder_slideshow(self, folder: str) -> bool:
        """Bascule un dossier dans/hors du slideshow. Retourne le nouvel état."""
        if folder in self.slideshow_folders:
            self.slideshow_folders.remove(folder)
            self.slideshow_excluded = [
                p for p in self.slideshow_excluded
                if str(Path(p).parent) != folder
            ]
            return False
        else:
            self.slideshow_folders.append(folder)
            return True
