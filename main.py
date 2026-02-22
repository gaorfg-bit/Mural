#!/usr/bin/env python3
import sys
import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw
from mural.app import WallpaperApp


class MuralApp(Adw.Application):
    def __init__(self):
# NOTE: Must NOT be "io.github.mural" because that DBus name is used by the daemon.
        super().__init__(application_id="io.github.gaorfg-bit.Mural")

    def do_activate(self):
        win = WallpaperApp(self)
        win.present()


def main():
    app = MuralApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())

