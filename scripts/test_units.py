#!/usr/bin/env python3
"""
Tests unitaires des parties de Mural qui ne dépendent pas de GTK.

Lancés sans GNOME (macOS/Linux) :
    python3 scripts/test_units.py

Couvrent : WallpaperSettings.from_dict (coercion de vieilles configs),
playlists par moniteur, io_utils (écriture atomique + verrou), et le
module partagé wallpaper_ops (hash descriptif, composite EXIF, purge).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from mural import io_utils, wallpaper_ops  # noqa: E402
from mural.models import WallpaperSettings  # noqa: E402


def test_from_dict_repairs_bad_types():
    raw = {
        "slideshow_images": "one.jpg",          # string au lieu de liste
        "per_monitor": ["dp-1"],                # liste au lieu de dict
        "folder_bookmarks": 42,                 # type inconnu
        "slideshow_monitors": ["eDP-1"],        # valide, préservé
        "legacy_removed_field": {"old": True},  # clé inconnue, ignorée
        "dark_mode": False,                     # champ supprimé du modèle
    }
    s = WallpaperSettings.from_dict(raw)
    assert s.slideshow_images == ["one.jpg"], s.slideshow_images
    assert s.per_monitor == {}, s.per_monitor
    assert s.folder_bookmarks == [], s.folder_bookmarks
    assert s.slideshow_monitors == ["eDP-1"], s.slideshow_monitors
    assert not hasattr(s, "legacy_removed_field")
    assert not hasattr(s, "dark_mode")


def test_from_dict_accepts_valid_config():
    s = WallpaperSettings.from_dict(
        {"folder": "/tmp", "slideshow_images": ["a.jpg", "b.jpg"],
         "per_monitor": {"eDP-1": "/tmp/a.jpg"}}
    )
    assert s.folder == "/tmp"
    assert s.slideshow_images == ["a.jpg", "b.jpg"]
    assert s.per_monitor == {"eDP-1": "/tmp/a.jpg"}


def test_from_dict_non_dict():
    s = WallpaperSettings.from_dict(None)
    assert s.folder == ""


def test_playlist_per_monitor_is_manual_only():
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "a.jpg"), Path(tmp, "b.jpg")
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        s = WallpaperSettings()
        s.add_to_slideshow(str(a), "eDP-1")
        s.add_to_slideshow(str(a), "eDP-1")  # doublon ignoré
        s.add_to_slideshow(str(b), "HDMI-1")
        assert s.resolve_slideshow_playlist("eDP-1") == [str(a)]
        assert s.resolve_slideshow_playlist("HDMI-1") == [str(b)]
        # « 100 % manuel » : un moniteur sans favori ne joue rien
        assert s.resolve_slideshow_playlist("DP-9") == []
        assert s.is_in_slideshow(str(a), "eDP-1")
        assert not s.is_in_slideshow(str(b), "eDP-1")
        # Une image disparue du disque est filtrée de la playlist
        a.unlink()
        assert s.resolve_slideshow_playlist("eDP-1") == []
        # remove
        s.remove_from_slideshow(str(b), "HDMI-1")
        assert s.resolve_slideshow_playlist("HDMI-1") == []


def test_io_utils_roundtrip_and_lock():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "settings.json")
        payload = {"a": 1, "b": ["x"]}
        io_utils.atomic_save_json(p, payload, mode=0o600)
        assert io_utils.load_json(p) == payload
        assert (p.stat().st_mode & 0o777) == 0o600
        with io_utils.locked_file(p):
            assert io_utils.load_json(p) == payload
        assert Path(str(p) + ".lock").exists()


def test_composite_key_stable_and_content_aware():
    with tempfile.TemporaryDirectory() as tmp:
        img = Path(tmp, "a.png")
        Image.new("RGB", (10, 10), (255, 0, 0)).save(img)
        monitors = [("eDP-1", 0, 0, 10, 10)]
        assignments = {"eDP-1": str(img)}
        k1 = wallpaper_ops.composite_key(assignments, monitors)
        k2 = wallpaper_ops.composite_key(dict(assignments), list(monitors))
        assert k1 == k2, "même contenu -> même nom (pas de réencodage)"
        # même chemin, contenu modifié -> clé différente
        img.write_bytes(img.read_bytes() + b"")  # no-op, mtime peut bouger
        Image.new("RGB", (10, 10), (0, 255, 0)).save(img)
        k3 = wallpaper_ops.composite_key(assignments, monitors)
        assert k3 != k1, "contenu modifié -> nouveau nom (refresh GNOME)"
        # géométrie différente -> clé différente
        k4 = wallpaper_ops.composite_key(assignments, [("eDP-1", 0, 0, 20, 10)])
        assert k4 != k1


def test_build_canvas_respects_exif_orientation():
    """Une image paysage taguée EXIF orientation 6 doit être traitée en portrait."""
    with tempfile.TemporaryDirectory() as tmp:
        tagged = Path(tmp, "tagged.jpg")
        # 200x100 : moitié gauche rouge, moitié droite bleue (paysage)
        im = Image.new("RGB", (200, 100), (255, 0, 0))
        im.paste((0, 0, 255), (100, 0, 200, 100))
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° CW -> 100x200, rouge en haut
        im.save(tagged, "JPEG", quality=95, exif=exif)

        # écran portrait 100x200 : avec EXIF, cover-fit exact -> haut
        # entièrement rouge, bas entièrement bleu. Sans EXIF, le crop
        # centré laisserait du bleu en haut à droite.
        canvas = wallpaper_ops.build_canvas(
            {"eDP-1": str(tagged)}, [("eDP-1", 0, 0, 100, 200)]
        )
        assert canvas.size == (100, 200)
        for x in (0, 99):
            top = canvas.getpixel((x, 0))
            bottom = canvas.getpixel((x, 199))
            assert top[0] > 200 and top[2] < 60, f"haut doit être rouge: {top}"
            assert bottom[2] > 200 and bottom[0] < 60, f"bas doit être bleu: {bottom}"


def test_build_canvas_multi_monitor_layout():
    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp, "a.png")
        b = Path(tmp, "b.png")
        Image.new("RGB", (4, 4), (255, 0, 0)).save(a)
        Image.new("RGB", (4, 4), (0, 0, 255)).save(b)
        monitors = [("A", 0, 0, 4, 4), ("B", 4, 0, 4, 4)]
        canvas = wallpaper_ops.build_canvas({"A": str(a), "B": str(b)}, monitors)
        assert canvas.size == (8, 4)
        assert canvas.getpixel((0, 0)) == (255, 0, 0)
        assert canvas.getpixel((7, 0)) == (0, 0, 255)
        # moniteur sans image -> zone noire, pas de crash
        canvas2 = wallpaper_ops.build_canvas({"A": str(a)}, monitors)
        assert canvas2.getpixel((7, 0)) == (0, 0, 0)


def test_apply_composite_writes_applies_purges_safely():
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp, "cache")
        cache.mkdir()
        img = Path(tmp, "a.png")
        Image.new("RGB", (2, 2), (10, 120, 210)).save(img)

        # neutraliser gsettings (absent de macOS/CI)
        calls = []
        original = wallpaper_ops.set_gnome_background
        wallpaper_ops.set_gnome_background = lambda uri: calls.append(uri)
        try:
            # un ancien composite obsolète ne doit pas survivre
            stale = cache / (wallpaper_ops.CACHE_PREFIX + "deadbeef.png")
            stale.write_bytes(b"old")
            monitors = [("eDP-1", 0, 0, 2, 2)]
            out = wallpaper_ops.apply_composite({"eDP-1": str(img)}, monitors, cache=cache)
            assert out is not None and out.exists(), "composite écrit"
            assert out.parent == cache
            assert not stale.exists(), "ancien composite purgé"
            assert calls == [out.as_uri()], "gsettings appliqué après écriture"

            # réapplication du même contenu : pas de réencodage (mtime stable)
            mtime = out.stat().st_mtime_ns
            out2 = wallpaper_ops.apply_composite({"eDP-1": str(img)}, monitors, cache=cache)
            assert out2 == out
            assert out.stat().st_mtime_ns == mtime, "même contenu -> pas de réécriture"
        finally:
            wallpaper_ops.set_gnome_background = original


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passés")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
