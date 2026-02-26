# 🖼️ Mural

**Gestionnaire de fonds d'écran moderne pour GNOME 49+ Wayland**

> Parcourez, prévisualisez et appliquez vos wallpapers en un clic — avec support multi-moniteurs natif, slideshow automatique et conversion AVIF.

![GNOME 49](https://img.shields.io/badge/GNOME-49%2B-blue?logo=gnome)
![Wayland](https://img.shields.io/badge/Wayland-Native-green)
![Python](https://img.shields.io/badge/Python-3.11%2B-yellow?logo=python)
![GTK4](https://img.shields.io/badge/GTK-4%20%2F%20Libadwaita-orange)
![License](https://img.shields.io/badge/License-GPL--3.0-red)

---

## ✨ Fonctionnalités

### 🖥️ Multi-moniteurs natif
- Image différente sur chaque écran, appliquée en un clic
- Génération automatique d'un composite pixel-perfect sur le bureau virtuel
- Détection dynamique des moniteurs (résolution, position, scaling)
- Compatible fractional scaling Wayland

### 🎨 Galerie intuitive
- Vignettes générées à la volée avec cache intelligent
- Filtrage par nom en temps réel
- Prévisualisation instantanée
- Marquage des images actives

### ⏱️ Slideshow automatique
- Rotation par intervalle configurable (minutes)
- Mode aléatoire ou séquentiel
- Sélection par dossier ou image individuelle
- Ciblage par écran

### 🗂️ Gestion des dossiers
- Dossiers favoris avec assignation par écran
- Navigation rapide entre collections

### 🔷 AVIF (optionnel)
- Conversion à la demande via ImageMagick
- Gain de 60–75% sur la taille des fichiers
- Les originaux ne sont jamais modifiés

---

## 📸 Captures d'écran

| Vue principale | Multi-moniteurs |
|---|---|
| *(galerie + prévisualisation)* | *(composite 2 écrans)* |

---

## 🚀 Installation

### Prérequis

```bash
# Ubuntu / Debian
sudo apt install python3-gi python3-pil gir1.2-gtk-4.0 gir1.2-adw-1

# Fedora
sudo dnf install python3-gobject python3-pillow gtk4 libadwaita

# Arch
sudo pacman -S python-gobject python-pillow gtk4 libadwaita
```

### AVIF (optionnel)

```bash
# Ubuntu / Debian
sudo apt install imagemagick

# Fedora
sudo dnf install ImageMagick

# Arch
sudo pacman -S imagemagick
```

### Lancement

```bash
git clone https://github.com/gaorfg-bit/mural
cd mural
python3 main.py
```

---

## 🏗️ Architecture

```
mural/
├── app.py          # Interface GTK4 / Libadwaita (WallpaperApp)
├── backend.py      # Application du fond via XDG Portal
├── monitors.py     # Détection des moniteurs (GDK pixels logiques)
├── thumbnails.py   # Génération de vignettes (Pillow)
├── slideshow.py    # Rotation automatique
├── avif_cache.py   # Conversion AVIF (ImageMagick)
├── config.py       # Persistance JSON
├── models.py       # Types de données
└── daemon.py       # Proxy D-Bus optionnel
```

### Détail technique — GNOME 49 Wayland

GNOME 49 a rompu la compatibilité avec `gsettings set picture-uri` pour le rendu visuel en temps réel. Mural utilise exclusivement **`org.freedesktop.portal.Wallpaper.SetWallpaperFile`** comme canal d'application, avec `picture-options=spanned` écrit dans GSettings pour le mode de rendu du composite multi-moniteurs.

Le composite est généré en **pixels logiques GDK** (pas physiques) — GNOME gère lui-même le scaling fractionnaire. La bounding box du bureau virtuel est calculée dynamiquement depuis les positions et dimensions rapportées par GDK.

---

## ⚙️ Compatibilité

| Environnement | Support |
|---|---|
| GNOME 49+ / Wayland | ✅ Complet |
| GNOME 46–48 / Wayland | ✅ Compatible |
| GNOME + X11 | ✅ Compatible |
| KDE / XFCE | ⚠️ Non testé |
| Flatpak / Sandbox | ⚠️ Portal uniquement |

---

## 🤝 Contribution

Les issues et PR sont les bienvenues. Domaines prioritaires :

- Support KDE Plasma
- Packaging Flatpak
- Tests automatisés
- Thèmes d'icônes

---

## 📄 Licence

GPL-3.0 — © 2026 GaoR

---

## 🙏 Remerciements

Merci aux projets [GTK4](https://gtk.org), [Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/), [Pillow](https://python-pillow.org) et à la communauté GNOME pour la documentation sur le portal XDG Wallpaper.
