from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

logger = logging.getLogger("mural.thumbnails")


class Thumbnailer:

    @staticmethod
    def generate(path: str, tw: int, th: int, cache: Path) -> Optional[Path]:
        """
        Generates a JPEG thumbnail in the cache.
        - JPEG draft mode: Pillow decodes at 1/8 native resolution (8x faster)
        - BILINEAR instead of LANCZOS: imperceptible at 120px, 3x faster
        - with block: releases image memory immediately after save
        """
        try:
            p = Path(path)
            mtime = str(p.stat().st_mtime)
            key = hashlib.sha1(f"{path}|{mtime}|{tw}x{th}".encode()).hexdigest()
            cached = cache / f"{key}.jpg"
            if cached.exists():
                return cached

            with Image.open(path) as img:
                # Draft mode: ask Pillow for minimal decode for target size
                if hasattr(img, "draft"):
                    img.draft("RGB", (tw * 2, th * 2))

                if img.mode in ("RGBA", "LA", "PA"):
                    bg = Image.new("RGB", img.size, (40, 40, 40))
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                ratio = max(tw / img.width, th / img.height)
                new_w = int(img.width * ratio)
                new_h = int(img.height * ratio)
                # BILINEAR: 3x faster, invisible at this size
                thumb = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
                left = (new_w - tw) // 2
                top  = (new_h - th) // 2
                thumb = thumb.crop((left, top, left + tw, top + th))
                thumb.save(str(cached), "JPEG", quality=82, optimize=False)
                # with block releases img and thumb here
            return cached

        except Exception as e:
            logger.error("Thumb fail [%s]: %s", Path(path).name, e)
            return None


class ImageLoader:
    """Centralizes Pillow access. Never call from UI thread."""

    @staticmethod
    def get_dimensions(path: str) -> Optional[Tuple[int, int]]:
        try:
            with Image.open(path) as img:
                return (img.width, img.height)
        except Exception as e:
            logger.debug("get_dimensions [%s]: %s", Path(path).name, e)
            return None

    @staticmethod
    def load_for_preview(
        path: str, max_w: int, max_h: int
    ) -> Optional[Tuple[bytes, int, int, bool]]:
        """
        Loads and resizes for preview.
        Returns (raw_pixels, w, h, has_alpha).
        Raw pixels allow Gdk.MemoryTexture without any extra decoding
        in UI thread — zero GTK blocking.
        """
        try:
            with Image.open(path) as img:
                # Draft mode: decode only minimum necessary
                if hasattr(img, "draft"):
                    img.draft("RGB", (max_w, max_h))

                has_alpha = img.mode in ("RGBA", "LA", "PA")
                if has_alpha:
                    img = img.convert("RGBA")
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                ratio = min(max_w / img.width, max_h / img.height)
                if ratio < 1.0:
                    new_w = max(1, int(img.width * ratio))
                    new_h = max(1, int(img.height * ratio))
                    img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)

                w, h = img.size
                raw = img.tobytes()   # raw pixels, no PNG encoding
            return (raw, w, h, has_alpha)

        except Exception as e:
            logger.error("load_for_preview [%s]: %s", Path(path).name, e)
            return None

    @staticmethod
    def load_for_composite(
        path: str, target_w: int, target_h: int, mode: str
    ) -> Optional[Image.Image]:
        try:
            img = Image.open(path).convert("RGB")
            if mode in ("zoom", "scaled"):
                ratio = max(target_w / img.width, target_h / img.height)
                nw = int(img.width * ratio)
                nh = int(img.height * ratio)
                img = img.resize((nw, nh), Image.Resampling.LANCZOS)
                left = (nw - target_w) // 2
                top  = (nh - target_h) // 2
                img = img.crop((left, top, left + target_w, top + target_h))
            elif mode == "stretched":
                img = img.resize((target_w, target_h), Image.Resampling.LANCZOS)
            elif mode == "centered":
                bg = Image.new("RGB", (target_w, target_h), (30, 30, 30))
                ox = (target_w - img.width) // 2
                oy = (target_h - img.height) // 2
                bg.paste(img, (max(0, ox), max(0, oy)))
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
