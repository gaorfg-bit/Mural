#!/usr/bin/env python3
import logging
import sys
from pathlib import Path

import gi
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gtk
from mural.app import WallpaperApp

logger = logging.getLogger("mural.main")

class MuralApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.gaorfg-bit.Mural")

    def do_startup(self):
        Adw.Application.do_startup(self)
        icon_path = Path(__file__).parent / "data" / "icons" / "mural-icon.png"
        if icon_path.exists():
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(icon_path))
                Gtk.Window.set_default_icon(pixbuf)
                logger.info("Icône chargée: %s", icon_path)
            except Exception as e:
                logger.warning("Icône non chargée: %s", e)

    def do_activate(self):
        win = WallpaperApp(self)
        win.present()

def main():
    app = MuralApp()
    return app.run(sys.argv)

if __name__ == "__main__":
    raise SystemExit(main())