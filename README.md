<p align="center">
  <img src="mural/data/icons/io.github.gaorfg-bit.Mural.png" alt="Mural Icon" width="64">
</p>

<h1 align="center">Mural</h1>

<p align="center">
  <strong>The wallpaper manager GNOME was missing.</strong>
</p>

<p align="center">
   <a href="https://ko-fi.com/M4M51RKN7V"><img src="https://img.shields.io/badge/Support%20me-on%20Ko--fi-orange?logo=ko-fi" alt="Ko-fi"></a>
</p>

---

<p align="center">
**The wallpaper manager GNOME was missing.**

Browse your image collection, preview in full size, and set your wallpaper in one click — with a different image on 
</p>

<p align="center">
  <a href="https://gnome.org"><img src="https://img.shields.io/badge/GNOME-49%2B-blue?logo=gnome" alt="GNOME 49"></a>
  <a href="https://wayland.freedesktop.org"><img src="https://img.shields.io/badge/Wayland-✓-green" alt="Wayland"></a>
  <a href="https://python.org"><img src="https://img.shields.io/badge/Python-3.11%2B-yellow?logo=python" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-red" alt="License"></a>
  <a href="https://ko-fi.com/M4M51RKN7V"><img src="https://img.shields.io/badge/Support%20me-on%20Ko--fi-orange?logo=ko-fi" alt="Ko-fi"></a>
</p>

---

## What you can do with Mural

**Browse and apply**
Open a folder, scroll through thumbnails, click an image to preview it full size, then apply it. That's it.

**A different wallpaper on each screen**
Got two monitors? Pick one image for your main display, another for the second. Mural handles everything — no manual setup needed.

**Automatic slideshow**
Turn on auto-rotation and Mural will change your wallpaper every X minutes. Random or sequential, on all your screens or just some of them.

**AVIF compression** *(optional)*
Have a large collection? Mural can convert your images to AVIF and cut their file size in half — without ever touching the originals.
</p>

---


  <img src="assets/Mural.png" alt="Mural Logo" width="900">
</p>

### Installation

### Dependencies

```bash
# Ubuntu / Debian
sudo apt install python3-gi python3-pil gir1.2-gtk-4.0 gir1.2-adw-1

# Fedora
sudo dnf install python3-gobject python3-pillow gtk4 libadwaita

# Arch
sudo pacman -S python-gobject python-pillow gtk4 libadwaita
```

### Install

```bash
git clone https://github.com/gaorfg-bit/mural
cd mural
pip install -r requirements.txt
./install.sh
```

That's it. Mural will appear in your GNOME Activities as **Mural**. No system-wide changes — everything installs in your home folder.

### Uninstall

```bash
./uninstall.sh
```

Removes everything cleanly.

---

## Compatibility

Mural works on **GNOME 46 and above**, on both Wayland and X11, with or without multiple monitors.

---

## Contributing

Got an idea, found a bug, or want to add KDE support or a Flatpak package? Issues and PRs are welcome.

---

## License

GPL-3.0 — © 2026 GaoR
