from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

logger = logging.getLogger("wallpaper")

class Thumbnailer:
    @staticmethod
    def generate(path: str, tw: int, th: int,
                 cache: Path) -> Optional[Path]:
        try:
            mtime = str(Path(path).stat().st_mtime)
            key = hashlib.sha1(
                f"{path}|{mtime}|{tw}x{th}".encode()
            ).hexdigest()
            cached = cache / f"{key}.jpg"

            if not cached.exists():
                with Image.open(path) as img:
                    if img.mode in ("RGBA", "LA", "PA"):
                        bg = Image.new("RGB", img.size, (40, 40, 40))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif img.mode != "RGB":
                        img = img.convert("RGB")

                    ratio = max(tw / img.width, th / img.height)
                    new_w = int(img.width * ratio)
                    new_h = int(img.height * ratio)
                    thumb = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                    left = (new_w - tw) // 2
                    top = (new_h - th) // 2
                    thumb = thumb.crop((left, top, left + tw, top + th))
                    thumb.save(str(cached), "JPEG", quality=85)

            return cached
        except Exception as e:
            logger.error("Thumb fail [%s]: %s", Path(path).name, e)
            return None


class ImageLoader:
    """Centralise les accès Pillow. Ne jamais appeler depuis le thread UI."""

    @staticmethod
    def get_dimensions(path: str) -> Optional[Tuple[int, int]]:
        """Retourne (width, height) en lisant seulement les headers."""
        try:
            with Image.open(path) as img:
                return (img.width, img.height)
        except Exception as e:
            logger.debug("get_dimensions failed [%s]: %s", Path(path).name, e)
            return None

    @staticmethod
    def load_for_composite(
        path: str, target_w: int, target_h: int, mode: str
    ) -> Optional[Image.Image]:
        """Charge et redimensionne une image pour le composite multi-monitor."""
        try:
            img = Image.open(path).convert("RGB")
            if mode in ("zoom", "scaled"):
                ratio = max(target_w / img.width, target_h / img.height)
                new_w = int(img.width * ratio)
                new_h = int(img.height * ratio)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                left = (new_w - target_w) // 2
                top = (new_h - target_h) // 2
                img = img.crop((left, top, left + target_w, top + target_h))
            elif mode == "stretched":
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            elif mode == "centered":
                bg = Image.new("RGB", (target_w, target_h), (30, 30, 30))
                off_x = (target_w - img.width) // 2
                off_y = (target_h - img.height) // 2
                bg.paste(img, (max(0, off_x), max(0, off_y)))
                img = bg
            else:
                ratio = max(target_w / img.width, target_h / img.height)
                img = img.resize(
                    (int(img.width * ratio), int(img.height * ratio)),
                    Image.Resampling.LANCZOS,
                )
                img = img.crop((0, 0, target_w, target_h))
            return img
        except Exception as e:
            logger.error("load_for_composite [%s]: %s", Path(path).name, e)
            return None
