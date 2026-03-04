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
    slideshow_images_per_monitor: Dict[str, List[str]] = field(default_factory=dict)
    slideshow_excluded: List[str] = field(default_factory=list)
    slideshow_monitors: List[str] = field(default_factory=list)
    folder_bookmarks: List[str] = field(default_factory=list)
    monitor_folders: Dict[str, str] = field(default_factory=dict)
    avif_use_for_gnome: bool = False  # Serve AVIF to GNOME if available
    spanned_banner_dismissed: bool = False
    same_image_on_all: bool = False  # Default: independent per monitor
    last_seen_version: str = ""  # Last version for which "What's new" was shown

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def _clean_existing_paths(self, paths: List[str]) -> List[str]:
        cleaned: List[str] = []
        seen: set[str] = set()
        for img in paths:
            if img in seen:
                continue
            if Path(img).exists():
                seen.add(img)
                cleaned.append(img)
        return cleaned

    def resolve_slideshow_playlist(self, connector: Optional[str] = None) -> List[str]:
        """
        Returns the final list of images for the slideshow.
        100% Manual Mode: only images added manually are played.
        If connector is provided, only that monitor's dedicated list is used.
        """
        if connector:
            return self._clean_existing_paths(self.slideshow_images_per_monitor.get(connector, []))
        return self._clean_existing_paths(self.slideshow_images)

    def is_in_slideshow(self, path: str, connector: Optional[str] = None) -> bool:
        """Returns True only if the image was added manually."""
        if connector and connector in self.slideshow_images_per_monitor:
            return path in self.slideshow_images_per_monitor.get(connector, [])
        return path in self.slideshow_images

    def add_to_slideshow(self, path: str, connector: Optional[str] = None) -> None:
        """Adds an image to the playlist."""
        if connector:
            bucket = self.slideshow_images_per_monitor.setdefault(connector, [])
            if path not in bucket:
                bucket.append(path)
            return
        if path not in self.slideshow_images:
            self.slideshow_images.append(path)

    def remove_from_slideshow(self, path: str, connector: Optional[str] = None) -> None:
        """Removes an image from the playlist."""
        if connector and connector in self.slideshow_images_per_monitor:
            bucket = self.slideshow_images_per_monitor.get(connector, [])
            if path in bucket:
                bucket.remove(path)
            return
        if path in self.slideshow_images:
            self.slideshow_images.remove(path)
