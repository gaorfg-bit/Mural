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
    avif_use_for_gnome: bool = False  # Serve AVIF to GNOME if available
    spanned_banner_dismissed: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def resolve_slideshow_playlist(self) -> List[str]:
        """
        Returns the final list of images for the slideshow.
        100% Manual Mode: Only images added one by one are played.
        """
        result: set[str] = set()
        for img in self.slideshow_images:
            if Path(img).exists():
                result.add(img)
        return sorted(result)

    def is_in_slideshow(self, path: str) -> bool:
        """Returns True only if the image was added manually."""
        return path in self.slideshow_images

    def add_to_slideshow(self, path: str) -> None:
        """Adds an image to the playlist."""
        if path not in self.slideshow_images:
            self.slideshow_images.append(path)

    def remove_from_slideshow(self, path: str) -> None:
        """Removes an image from the playlist."""
        if path in self.slideshow_images:
            self.slideshow_images.remove(path)
