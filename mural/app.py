from __future__ import annotations

import gc
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image
from PIL import ImageFile

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .backend import GnomeBackend
from .avif_cache import FolderConverter, get_cached_avif, AVIF_SUPPORTED
from .config import Config
from .daemon import MuralDaemonProxy
from .monitors import MonitorDetector
from .slideshow import SlideshowManager
from .thumbnails import ImageLoader, Thumbnailer

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

logger = logging.getLogger("wallpaper")
logger.setLevel(logging.DEBUG)


def _log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_uncaught_exception
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 268_435_456

SLIDESHOW_CSS = """
.slideshow-indicator {
    color: gold;
    -gtk-icon-size: 16px;
}
"""

class WallpaperApp(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application)

        # Init
        self.config = Config()
        self.settings = self.config.load()
        self.backend = GnomeBackend()
        self.slideshow = SlideshowManager(self)
        self.avif_converter = FolderConverter()
        if AVIF_SUPPORTED:
            logger.info("Cache AVIF disponible (pillow-avif-plugin)")
        else:
            logger.info("pillow-avif-plugin absent — conversion AVIF désactivée")
        # Proxy optionnel vers le daemon D-Bus (fallback local sinon)
        self._daemon = MuralDaemonProxy()
        self.monitors = MonitorDetector.detect()
        self.current_monitor = 0
        self.selected_image: Optional[str] = None
        self._selected_path: Optional[str] = None
        self._stop_event = threading.Event()
        self.gallery_generation = 0
        self._thumb_views: Dict[str, Tuple[Gtk.Widget, Gtk.Widget, Gtk.Widget]] = {}
        self._flowbox_columns = 0
        self._column_update_id: Optional[int] = None
        self._active_wallpapers: set[str] = set()
        self._child_to_path: Dict[Gtk.FlowBoxChild, str] = {}
        self._selected_child: Optional[Gtk.FlowBoxChild] = None
        self._search_text: str = ""
        self._context_path: Optional[str] = None
        self._preview_timeout_id: Optional[int] = None
        self._bookmark_action_names: list[str] = []  # D3 — tracking explicite des actions bookmarks
        self._save_timeout_id: Optional[int] = None  # D5 — debounce config save
        self._resize_reload_id: Optional[int] = None
        self._pending_batches: int = 0
        self._pending_batches_lock = threading.Lock()
        self.folder = (
            Path(self.settings.folder)
            if self.settings.folder
            else Config.default_folder()
        )
        self._slideshow_css_added = False
        # G5 — Écouter les changements de moniteurs (branchement/débranchement)
        _display = Gdk.Display.get_default()
        if _display:
            _display.get_monitors().connect("items-changed", self._on_monitors_changed)

        self.set_title("Mural")

        self._build_ui()
        self._ensure_slideshow_css()
        # Restaurer la taille sauvegardée mais plafonner à 1400×900
        # pour éviter de remplir un écran ultra-wide au premier lancement
        w = min(self.settings.window_width or 1100, 1400)
        h = min(self.settings.window_height or 700, 900)
        self.set_default_size(w, h)
        self.connect("close-request", self._on_close_request)
        self._schedule_flowbox_column_update()
        self._init_state()
        GLib.idle_add(self._load_gallery)
        GLib.timeout_add(200, self._refresh_flowbox_columns)

        self._init_shortcuts()
        if self._daemon.available:
            logger.info("Slideshow délégué au daemon")
            self._sync_ui_from_daemon()
        elif self.settings.slideshow_enabled:
            self.slideshow.start()

    # ────────────────────────────────────────────────────────────
    # BUILD UI
    # ────────────────────────────────────────────────────────────

    def _build_ui(self):
        # === HeaderBar ===
        hb = Gtk.HeaderBar()
        hb.set_show_title_buttons(True)
        self.set_decorated(True)
        n_mon = len(self.monitors)
        self._title_widget = Adw.WindowTitle(
            title="Mural",
            subtitle=(
                f"{n_mon} écran{'s' if n_mon > 1 else ''} "
                f"détecté{'s' if n_mon > 1 else ''}"
            ),
        )
        hb.set_title_widget(self._title_widget)
        self._ensure_menu_actions()
        self._ensure_thumb_menu()

        btn_folder = Gtk.Button()
        btn_folder.set_child(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        btn_folder.set_tooltip_text("Choisir un dossier")
        btn_folder.connect("clicked", self._on_choose_folder)

        self._search_toggle = Gtk.ToggleButton()
        self._search_toggle.set_icon_name("system-search-symbolic")
        self._search_toggle.set_tooltip_text("Rechercher (Ctrl+F)")

        hb.pack_start(btn_folder)
        hb.pack_start(self._search_toggle)

        self.btn_apply = Gtk.Button(label="Définir comme fond")
        self.btn_apply.add_css_class("suggested-action")
        self.btn_apply.set_sensitive(False)
        self.btn_apply.connect("clicked", self._on_apply)
        hb.pack_end(self.btn_apply)

        app_menu = Gio.Menu()
        app_menu.append("Rafraîchir la galerie", "win.refresh")
        app_menu.append("Vider le cache", "win.clear_cache")
        sep = Gio.Menu()
        sep.append("À propos de Mural", "win.about")
        app_menu.append_section(None, sep)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_menu_model(app_menu)
        menu_btn.set_tooltip_text("Menu principal")
        hb.pack_end(menu_btn)

        self._sidebar_toggle = Gtk.ToggleButton()
        self._sidebar_toggle.set_icon_name("sidebar-show-right-symbolic")
        self._sidebar_toggle.set_active(True)
        self._sidebar_toggle.set_tooltip_text("Afficher/masquer le panneau (Ctrl+B)")
        self._sidebar_toggle.connect("toggled", self._on_sidebar_toggle)
        hb.pack_end(self._sidebar_toggle)

        # === Layout racine ===
        root_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        root_box.set_hexpand(True)
        root_box.set_vexpand(True)
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(hb)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(root_box)
        toolbar_view.set_content(self._toast_overlay)
        self.set_content(toolbar_view)
        if getattr(self.settings, 'window_maximized', False):
            self.maximize()

        # === Zone gauche : galerie ===
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_vbox.set_hexpand(True)
        main_vbox.set_vexpand(True)
        main_vbox.set_size_request(200, -1)

        # SearchBar
        search_bar = Gtk.SearchBar()
        search_bar.set_search_mode(False)
        search_bar.set_show_close_button(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Rechercher…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_bar.set_child(self.search_entry)
        search_bar.connect_entry(self.search_entry)
        self._search_toggle.bind_property(
            "active", search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL
        )
        main_vbox.append(search_bar)

        # Paned vertical preview / galerie
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_wide_handle(False)

        # Preview
        preview_frame = Gtk.Frame()
        preview_frame.add_css_class("preview-area")
        preview_frame.set_hexpand(True)
        preview_frame.set_vexpand(True)
        self.preview = Gtk.Picture()
        self.preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview.set_can_shrink(True)
        self.preview.set_hexpand(True)
        self.preview.set_vexpand(True)
        self.preview.set_halign(Gtk.Align.FILL)
        self.preview.set_valign(Gtk.Align.FILL)
        self._preview_placeholder_label = Gtk.Label(label="Sélectionnez une image")
        self._preview_placeholder_label.add_css_class("dim-label")
        self._preview_placeholder_label.set_halign(Gtk.Align.CENTER)
        self._preview_placeholder_label.set_valign(Gtk.Align.CENTER)
        preview_overlay = Gtk.Overlay()
        preview_overlay.set_child(self.preview)
        preview_overlay.add_overlay(self._preview_placeholder_label)
        preview_overlay.set_hexpand(True)
        preview_overlay.set_vexpand(True)
        preview_top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        preview_top.set_margin_start(12)
        preview_top.set_margin_end(12)
        preview_top.set_margin_top(12)
        preview_top.set_margin_bottom(4)
        preview_frame.set_size_request(-1, Config.PREVIEW_MAX_HEIGHT)
        preview_frame.set_vexpand(True)
        preview_frame.set_hexpand(True)
        preview_frame.set_child(preview_overlay)
        preview_top.append(preview_frame)
        paned.set_start_child(preview_top)
        paned.set_resize_start_child(True)
        paned.set_shrink_start_child(False)
        paned.set_position(Config.PREVIEW_MAX_HEIGHT + 24)

        # Galerie + progressbar
        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom_box.set_hexpand(True)
        bottom_box.set_vexpand(True)
        self._gallery_progressbar = Gtk.ProgressBar()
        self._gallery_progressbar.set_visible(False)
        self._gallery_progressbar.set_hexpand(True)
        self._gallery_progressbar.set_margin_start(12)
        self._gallery_progressbar.set_margin_end(12)
        self._gallery_progressbar.set_margin_top(4)
        self._gallery_progressbar.set_margin_bottom(2)
        self._progress_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._progress_container.set_hexpand(True)
        self._progress_container.set_vexpand(False)
        self._progress_container.append(self._gallery_progressbar)
        self._progress_container.set_visible(False)
        bottom_box.append(self._progress_container)

        gallery_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        gallery_box.set_margin_start(12)
        gallery_box.set_margin_end(12)
        gallery_box.set_margin_bottom(0)
        gallery_box.set_hexpand(True)
        gallery_box.set_vexpand(True)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        self._scroll = scroll
        self.flowbox = Gtk.FlowBox()
        self.flowbox.set_valign(Gtk.Align.START)
        self.flowbox.set_hexpand(True)
        self.flowbox.set_vexpand(False)
        self.flowbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flowbox.set_homogeneous(False)
        self.flowbox.set_min_children_per_line(1)
        self.flowbox.set_max_children_per_line(20)
        self.flowbox.set_column_spacing(8)
        self.flowbox.set_row_spacing(8)
        self.flowbox.set_margin_start(12)
        self.flowbox.set_margin_end(12)
        self.flowbox.set_margin_top(12)
        self.flowbox.set_margin_bottom(12)
        self.flowbox.connect("child-activated", self._on_gallery_click)
        self.flowbox.connect("selected-children-changed", self._on_flowbox_selection_changed)
        self.flowbox.connect("notify::allocation", lambda *_: self._schedule_flowbox_column_update())
        self.flowbox.set_filter_func(self._flowbox_filter)
        scroll.set_child(self.flowbox)
        gallery_box.append(scroll)
        bottom_box.append(gallery_box)
        paned.set_end_child(bottom_box)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        main_vbox.append(paned)

        # Statusbar — info image toujours visible en bas
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        statusbar.set_margin_start(14)
        statusbar.set_margin_end(14)
        statusbar.set_margin_top(4)
        statusbar.set_margin_bottom(5)
        statusbar.add_css_class("dim-label")
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_hexpand(True)
        self.status_label.set_max_width_chars(80)
        statusbar.append(self.status_label)
        # Info image inline dans la statusbar
        self._sb_sep1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._sb_sep1.set_margin_top(4)
        self._sb_sep1.set_margin_bottom(4)
        self._sb_sep1.set_visible(False)
        statusbar.append(self._sb_sep1)
        self.lbl_name = Gtk.Label()
        self.lbl_name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.lbl_name.set_max_width_chars(30)
        self.lbl_name.set_selectable(True)
        self.lbl_name.set_visible(False)
        statusbar.append(self.lbl_name)
        self._sb_sep2 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._sb_sep2.set_margin_top(4)
        self._sb_sep2.set_margin_bottom(4)
        self._sb_sep2.set_visible(False)
        statusbar.append(self._sb_sep2)
        self.lbl_dims = Gtk.Label()
        self.lbl_dims.set_visible(False)
        statusbar.append(self.lbl_dims)
        self._sb_sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._sb_sep3.set_margin_top(4)
        self._sb_sep3.set_margin_bottom(4)
        self._sb_sep3.set_visible(False)
        statusbar.append(self._sb_sep3)
        self.lbl_size = Gtk.Label()
        self.lbl_size.set_visible(False)
        statusbar.append(self.lbl_size)
        # Séparateur visuel au-dessus de la statusbar
        sb_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        main_vbox.append(sb_sep)
        main_vbox.append(statusbar)

        root_box.append(main_vbox)

        # ═══════════════════════════════════════════════
        # SIDEBAR — 4 onglets
        # ═══════════════════════════════════════════════
        self._sidebar_sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        root_box.append(self._sidebar_sep)

        self._sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._sidebar.set_size_request(280, -1)
        self._sidebar.set_vexpand(True)

        sidebar = self._sidebar

        # Barre d'onglets
        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tab_bar.add_css_class("linked")
        tab_bar.set_margin_start(10)
        tab_bar.set_margin_end(10)
        tab_bar.set_margin_top(8)
        tab_bar.set_margin_bottom(8)

        self._tab_pages = {}
        self._tab_btns = {}
        tabs = [
            ("display",   "Affichage"),
            ("slideshow", "Slideshow"),
            ("folders",   "Dossiers"),
            ("avif",      "AVIF"),
        ]
        first_btn = None
        for tab_id, tab_label in tabs:
            btn = Gtk.ToggleButton(label=tab_label)
            btn.set_hexpand(True)
            if first_btn is None:
                first_btn = btn
                btn.set_active(True)
            else:
                btn.set_group(first_btn)
            btn.connect("toggled", self._on_tab_toggled, tab_id)
            tab_bar.append(btn)
            self._tab_btns[tab_id] = btn

        sidebar.append(tab_bar)
        tab_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar.append(tab_sep)

        # Stack pour les pages
        self._tab_stack = Gtk.Stack()
        self._tab_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._tab_stack.set_transition_duration(120)
        self._tab_stack.set_vexpand(True)
        sidebar.append(self._tab_stack)
        root_box.append(self._sidebar)

        # ── PAGE AFFICHAGE ──────────────────────────────
        page_display = Gtk.ScrolledWindow()
        page_display.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_display.set_vexpand(True)
        box_display = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box_display.set_margin_start(12)
        box_display.set_margin_end(12)
        box_display.set_margin_top(12)
        box_display.set_margin_bottom(16)

        # Section Écrans
        if len(self.monitors) > 1:
            group_screens = Adw.PreferencesGroup(title="Écrans")
            mon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            mon_box.set_margin_start(12)
            mon_box.set_margin_end(12)
            mon_box.set_margin_top(8)
            mon_box.set_margin_bottom(8)
            btns_box = Gtk.Box(spacing=4)
            btns_box.set_halign(Gtk.Align.CENTER)
            self.monitor_btns = []
            first_mon_btn = None
            for i, mon in enumerate(self.monitors):
                label = f"Écran {i + 1}"
                if mon.primary:
                    label += " ★"
                btn = Gtk.ToggleButton(label=label)
                btn.set_tooltip_text(f"{mon.name}\n{mon.width}×{mon.height}\nPosition: {mon.x},{mon.y}")
                btn.set_size_request(80, 36)
                if i == 0:
                    btn.set_active(True)
                    btn.add_css_class("suggested-action")
                if first_mon_btn is None:
                    first_mon_btn = btn
                else:
                    btn.set_group(first_mon_btn)
                btn.connect("toggled", self._on_monitor_toggle, i)
                btns_box.append(btn)
                self.monitor_btns.append(btn)
            mon_box.append(btns_box)
            self.lbl_monitor = Gtk.Label()
            self.lbl_monitor.set_markup(self._monitor_markup(0))
            self.lbl_monitor.set_wrap(True)
            mon_box.append(self.lbl_monitor)
            mode_label = Gtk.Label()
            mode_label.set_markup("<small>Application:</small>")
            mode_label.set_xalign(0)
            mon_box.append(mode_label)
            self.chk_same_all = Gtk.CheckButton(label="Même image sur tous")
            self.chk_same_all.set_active(True)
            self.chk_same_all.set_tooltip_text("Décocher pour une image différente par écran")
            mon_box.append(self.chk_same_all)
            mon_row = Adw.PreferencesRow()
            mon_row.set_child(mon_box)
            group_screens.add(mon_row)
            box_display.append(group_screens)
        else:
            self.monitor_btns = []
            self.chk_same_all = None

        # Section Options
        group_options = Adw.PreferencesGroup(title="Options")
        mode_row = Adw.PreferencesRow()
        mode_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mode_inner.set_margin_start(12)
        mode_inner.set_margin_end(12)
        mode_inner.set_margin_top(8)
        mode_inner.set_margin_bottom(8)
        mode_title = Gtk.Label(label="Mode d'affichage")
        mode_title.set_xalign(0)
        mode_title.set_hexpand(True)
        mode_title.set_ellipsize(Pango.EllipsizeMode.END)
        mode_inner.append(mode_title)
        self.mode_ids = [mid for mid, _ in GnomeBackend.MODES]
        self.mode_dropdown = Gtk.DropDown.new_from_strings([mlabel for _, mlabel in GnomeBackend.MODES])
        self.mode_dropdown.set_selected(
            self.mode_ids.index(self.settings.mode) if self.settings.mode in self.mode_ids else 0
        )
        self.mode_dropdown.set_valign(Gtk.Align.CENTER)
        self.mode_dropdown.set_hexpand(False)
        self.mode_dropdown.set_size_request(150, -1)
        mode_inner.append(self.mode_dropdown)
        mode_row.set_child(mode_inner)
        group_options.add(mode_row)

        lock_row = Adw.ActionRow(
            title="Écran de verrouillage",
            subtitle="Appliquer aussi à l'écran de verrouillage",
        )
        self.chk_lock = Gtk.Switch()
        self.chk_lock.set_active(self.settings.lock_screen)
        self.chk_lock.set_valign(Gtk.Align.CENTER)
        lock_row.add_suffix(self.chk_lock)
        lock_row.set_activatable_widget(self.chk_lock)
        group_options.add(lock_row)
        box_display.append(group_options)

        page_display.set_child(box_display)
        self._tab_stack.add_named(page_display, "display")

        # ── PAGE SLIDESHOW ──────────────────────────────
        page_slideshow = Gtk.ScrolledWindow()
        page_slideshow.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_slideshow.set_vexpand(True)
        box_slideshow = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box_slideshow.set_margin_start(12)
        box_slideshow.set_margin_end(12)
        box_slideshow.set_margin_top(12)
        box_slideshow.set_margin_bottom(16)

        group_slideshow = Adw.PreferencesGroup(title="Slideshow")
        slideshow_row = Adw.ActionRow(
            title="Changement automatique",
            subtitle="Change le fond toutes les X minutes",
        )
        self.switch_slideshow = Gtk.Switch()
        self.switch_slideshow.set_active(self.settings.slideshow_enabled)
        self.switch_slideshow.set_valign(Gtk.Align.CENTER)
        self.switch_slideshow.connect("notify::active", self._on_slideshow_toggle)
        slideshow_row.add_suffix(self.switch_slideshow)
        slideshow_row.set_activatable_widget(self.switch_slideshow)
        group_slideshow.add(slideshow_row)

        interval_row = Adw.PreferencesRow()
        interval_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        interval_inner.set_margin_start(12)
        interval_inner.set_margin_end(12)
        interval_inner.set_margin_top(8)
        interval_inner.set_margin_bottom(8)
        interval_lbl = Gtk.Label(label="Intervalle (min)")
        interval_lbl.set_hexpand(True)
        interval_lbl.set_xalign(0)
        interval_inner.append(interval_lbl)
        self.spin_interval = Gtk.SpinButton()
        self.spin_interval.set_adjustment(Gtk.Adjustment(
            value=self.settings.slideshow_interval,
            lower=1, upper=1440, step_increment=1, page_increment=10,
        ))
        self.spin_interval.set_numeric(True)
        self.spin_interval.set_valign(Gtk.Align.CENTER)
        self.spin_interval.set_size_request(80, -1)
        self.spin_interval.connect("value-changed", self._on_interval_changed)
        interval_inner.append(self.spin_interval)
        interval_row.set_child(interval_inner)
        group_slideshow.add(interval_row)

        random_row = Adw.ActionRow(title="Ordre aléatoire")
        self.switch_random = Gtk.Switch()
        self.switch_random.set_active(self.settings.slideshow_random)
        self.switch_random.set_valign(Gtk.Align.CENTER)
        self.switch_random.connect("notify::active", self._on_random_toggle)
        random_row.add_suffix(self.switch_random)
        random_row.set_activatable_widget(self.switch_random)
        group_slideshow.add(random_row)
        box_slideshow.append(group_slideshow)

        if len(self.monitors) > 1:
            group_ss_screens = Adw.PreferencesGroup(title="Appliquer sur")
            self._slideshow_monitor_checks = {}
            for mon in self.monitors:
                chk = Gtk.CheckButton(label=f"Écran {mon.name[:24]}")
                chk.set_active(
                    not self.settings.slideshow_monitors
                    or mon.connector in self.settings.slideshow_monitors
                )
                chk.connect("toggled", self._on_slideshow_monitor_toggled, mon.connector)
                self._slideshow_monitor_checks[mon.connector] = chk
                chk_row = Adw.PreferencesRow()
                chk_inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                chk_inner.set_margin_start(12)
                chk_inner.set_margin_end(12)
                chk_inner.set_margin_top(6)
                chk_inner.set_margin_bottom(6)
                chk_inner.append(chk)
                chk_row.set_child(chk_inner)
                group_ss_screens.add(chk_row)
            box_slideshow.append(group_ss_screens)

        self.lbl_slideshow_count = Gtk.Label()
        self.lbl_slideshow_count.add_css_class("dim-label")
        self.lbl_slideshow_count.set_xalign(0)
        self.lbl_slideshow_count.set_margin_start(4)
        self._update_slideshow_count_label()
        box_slideshow.append(self.lbl_slideshow_count)

        btn_next = Gtk.Button(label="⏭  Image suivante maintenant")
        btn_next.add_css_class("flat")
        btn_next.connect("clicked", lambda *_: self.slideshow.next())
        box_slideshow.append(btn_next)

        page_slideshow.set_child(box_slideshow)
        self._tab_stack.add_named(page_slideshow, "slideshow")

        # ── PAGE DOSSIERS ───────────────────────────────
        page_folders = Gtk.ScrolledWindow()
        page_folders.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_folders.set_vexpand(True)
        box_folders = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box_folders.set_margin_start(12)
        box_folders.set_margin_end(12)
        box_folders.set_margin_top(12)
        box_folders.set_margin_bottom(16)

        # Dossiers rapides par écran (si multi-monitor)
        if len(self.monitors) > 1:
            group_folders_mon = Adw.PreferencesGroup(title="Dossiers rapides")
            for i, mon in enumerate(self.monitors):
                folder_row = Adw.ActionRow(
                    title=f"Écran {i+1} — {mon.name[:20]}",
                    subtitle=self.settings.monitor_folders.get(mon.connector, "Non assigné")[-40:],
                )
                btn_assign = Gtk.Button()
                btn_assign.set_child(Gtk.Image.new_from_icon_name("folder-symbolic"))
                btn_assign.set_valign(Gtk.Align.CENTER)
                btn_assign.set_tooltip_text("Choisir le dossier de cet écran")
                btn_assign.connect("clicked", self._on_assign_monitor_folder, mon.connector, folder_row)
                folder_row.add_suffix(btn_assign)
                btn_load = Gtk.Button()
                btn_load.set_child(Gtk.Image.new_from_icon_name("go-jump-symbolic"))
                btn_load.set_valign(Gtk.Align.CENTER)
                btn_load.set_tooltip_text("Charger ce dossier dans la galerie")
                btn_load.connect("clicked", self._on_load_monitor_folder, mon.connector)
                folder_row.add_suffix(btn_load)
                group_folders_mon.add(folder_row)
            box_folders.append(group_folders_mon)

        # Dossier courant
        group_current = Adw.PreferencesGroup(title="Dossier courant")
        folder_info_row = Adw.PreferencesRow()
        folder_info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        folder_info_box.set_margin_start(12)
        folder_info_box.set_margin_end(12)
        folder_info_box.set_margin_top(8)
        folder_info_box.set_margin_bottom(8)
        self.lbl_folder = Gtk.Label()
        self.lbl_folder.set_wrap(True)
        self.lbl_folder.set_max_width_chars(28)
        self.lbl_folder.set_xalign(0)
        self.lbl_folder.set_selectable(True)
        folder_info_box.append(self.lbl_folder)
        self.lbl_count = Gtk.Label()
        self.lbl_count.set_xalign(0)
        self.lbl_count.add_css_class("dim-label")
        folder_info_box.append(self.lbl_count)
        folder_info_row.set_child(folder_info_box)
        group_current.add(folder_info_row)

        # Actions dossier courant
        folder_actions_row = Adw.PreferencesRow()
        gallery_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        gallery_toolbar.set_margin_start(12)
        gallery_toolbar.set_margin_end(12)
        gallery_toolbar.set_margin_top(6)
        gallery_toolbar.set_margin_bottom(6)
        btn_bookmark = Gtk.Button()
        btn_bookmark.set_child(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        btn_bookmark.set_tooltip_text("Ajouter le dossier actuel aux favoris")
        btn_bookmark.connect("clicked", self._on_add_bookmark)
        gallery_toolbar.append(btn_bookmark)
        self.btn_bookmarks = Gtk.MenuButton()
        self.btn_bookmarks.set_child(Gtk.Image.new_from_icon_name("user-bookmarks-symbolic"))
        self.btn_bookmarks.set_tooltip_text("Ouvrir un dossier favori")
        gallery_toolbar.append(self.btn_bookmarks)
        self._rebuild_bookmarks_menu()
        self.btn_folder_slideshow = Gtk.ToggleButton()
        self.btn_folder_slideshow.set_child(Gtk.Image.new_from_icon_name("starred-symbolic"))
        self.btn_folder_slideshow.set_tooltip_text("Inclure ce dossier dans le slideshow")
        self.btn_folder_slideshow.connect("toggled", self._on_folder_slideshow_toggled)
        gallery_toolbar.append(self.btn_folder_slideshow)
        folder_actions_row.set_child(gallery_toolbar)
        group_current.add(folder_actions_row)
        box_folders.append(group_current)

        page_folders.set_child(box_folders)
        self._tab_stack.add_named(page_folders, "folders")

        # ── PAGE AVIF ───────────────────────────────────
        page_avif = Gtk.ScrolledWindow()
        page_avif.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_avif.set_vexpand(True)
        box_avif = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box_avif.set_margin_start(12)
        box_avif.set_margin_end(12)
        box_avif.set_margin_top(12)
        box_avif.set_margin_bottom(16)

        group_avif = Adw.PreferencesGroup(title="Cache AVIF")
        self._avif_stats_label = Gtk.Label()
        self._avif_stats_label.set_xalign(0)
        self._avif_stats_label.add_css_class("dim-label")
        self._avif_stats_label.set_margin_start(12)
        self._avif_stats_label.set_margin_top(6)
        self._avif_stats_label.set_margin_bottom(2)
        self._avif_stats_label.set_wrap(True)
        self._avif_stats_label.set_text(
            "AVIF non disponible — installez imagemagick"
            if not AVIF_SUPPORTED else "Aucune info"
        )
        stats_row = Adw.PreferencesRow()
        stats_row.set_child(self._avif_stats_label)
        group_avif.add(stats_row)

        self._avif_progress_bar = Gtk.ProgressBar()
        self._avif_progress_bar.set_hexpand(True)
        self._avif_progress_bar.set_margin_start(12)
        self._avif_progress_bar.set_margin_end(12)
        self._avif_progress_bar.set_margin_top(4)
        self._avif_progress_bar.set_margin_bottom(4)
        self._avif_progress_bar.set_visible(False)
        self._avif_progress_label = Gtk.Label()
        self._avif_progress_label.add_css_class("dim-label")
        self._avif_progress_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._avif_progress_label.set_margin_start(12)
        self._avif_progress_label.set_visible(False)
        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        progress_box.set_margin_top(4)
        progress_box.set_margin_bottom(4)
        progress_box.append(self._avif_progress_bar)
        progress_box.append(self._avif_progress_label)
        progress_row = Adw.PreferencesRow()
        progress_row.set_child(progress_box)
        group_avif.add(progress_row)

        avif_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        avif_btn_box.set_margin_start(12)
        avif_btn_box.set_margin_end(12)
        avif_btn_box.set_margin_top(6)
        avif_btn_box.set_margin_bottom(6)
        self.btn_avif_convert = Gtk.Button(label="Convertir ce dossier")
        self.btn_avif_convert.add_css_class("suggested-action")
        self.btn_avif_convert.set_hexpand(True)
        self.btn_avif_convert.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_convert.set_tooltip_text("Convertit toutes les images en AVIF dans .mural_cache/")
        self.btn_avif_convert.connect("clicked", self._on_avif_convert)
        self.btn_avif_cancel = Gtk.Button(label="Annuler")
        self.btn_avif_cancel.set_visible(False)
        self.btn_avif_cancel.connect("clicked", lambda *_: self.avif_converter.cancel())
        self.btn_avif_purge = Gtk.Button(label="Purger")
        self.btn_avif_purge.add_css_class("destructive-action")
        self.btn_avif_purge.set_tooltip_text("Supprime le .mural_cache/ de ce dossier")
        self.btn_avif_purge.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_purge.connect("clicked", self._on_avif_purge)
        avif_btn_box.append(self.btn_avif_convert)
        avif_btn_box.append(self.btn_avif_cancel)
        avif_btn_box.append(self.btn_avif_purge)
        avif_btn_row = Adw.PreferencesRow()
        avif_btn_row.set_child(avif_btn_box)
        group_avif.add(avif_btn_row)

        avif_gnome_row = Adw.ActionRow(
            title="Utiliser l'AVIF pour le fond",
            subtitle="Applique l'AVIF à GNOME si disponible pour ce fichier",
        )
        self.switch_avif_gnome = Gtk.Switch()
        self.switch_avif_gnome.set_active(self.settings.avif_use_for_gnome)
        self.switch_avif_gnome.set_valign(Gtk.Align.CENTER)
        self.switch_avif_gnome.set_sensitive(AVIF_SUPPORTED)
        self.switch_avif_gnome.connect("notify::active", self._on_avif_gnome_toggle)
        avif_gnome_row.add_suffix(self.switch_avif_gnome)
        avif_gnome_row.set_activatable_widget(self.switch_avif_gnome)
        group_avif.add(avif_gnome_row)
        box_avif.append(group_avif)

        page_avif.set_child(box_avif)
        self._tab_stack.add_named(page_avif, "avif")

        # Afficher la première page
        self._tab_stack.set_visible_child_name("display")

    def _on_tab_toggled(self, btn: Gtk.ToggleButton, tab_id: str) -> None:
        if btn.get_active():
            self._tab_stack.set_visible_child_name(tab_id)

    def _on_sidebar_toggle(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._sidebar.set_visible(visible)
        self._sidebar_sep.set_visible(visible)

    def _ensure_slideshow_css(self) -> None:
        if self._slideshow_css_added:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(SLIDESHOW_CSS.encode())
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        self._slideshow_css_added = True

    def _sync_ui_from_daemon(self) -> None:
        """Synchronise l'UI avec l'état du daemon (fond courant)."""
        try:
            current = self._daemon.get_current_wallpaper() if getattr(self, "_daemon", None) else ""
            if current:
                self._set_active_wallpapers([current])
                self._update_preview(current)
                self._update_image_info(current)
        except Exception as e:
            logger.warning("Sync daemon failed: %s", e)

    # ────────────────────────────────────────────────────────────
    def _ensure_menu_actions(self):
        if getattr(self, "_primary_menu", None):
            return
        self._primary_menu = Gio.Menu()
        self._primary_menu.append("Importer…", "win.import")
        self._primary_menu.append("Supprimer", "win.remove")

        noop = Gio.SimpleAction.new("noop", None)
        self.get_application().add_action(noop)

        self._register_action("thumb_set", self._on_thumb_set, None)
        self._register_action("thumb_reveal", self._on_thumb_reveal, None)
        self._register_action("thumb_copy_path", self._on_thumb_copy_path, None)
        self._register_action(
            "thumb_slideshow_add", self._on_thumb_slideshow_add, None
        )
        self._register_action(
            "thumb_slideshow_remove", self._on_thumb_slideshow_remove, None
        )

        self._register_action("import", self._menu_import, "<Primary>O")
        self._register_action("remove", self._menu_remove, "Delete")
        # G1 — Actions du menu hamburger
        self._register_action("refresh", lambda *_: self._load_gallery(), "<Primary>R")
        self._register_action("clear_cache", self._on_clear_cache, None)
        self._register_action("about", self._on_about, None)
        # G3 — Action toggle SearchBar (Ctrl+F)
        action_search = Gio.SimpleAction.new("toggle_search", None)
        action_search.connect(
            "activate",
            lambda *_: self._search_toggle.set_active(
                not self._search_toggle.get_active()
            ) if hasattr(self, "_search_toggle") else None
        )
        self.add_action(action_search)
        app = self.get_application()
        if app:
            app.set_accels_for_action("win.toggle_search", ["<Primary>F"])

    def _register_action(self, name: str, handler, accel: str):
        action_name = f"win.{name}"
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", handler)
        self.add_action(action)
        app = self.get_application()
        if app and accel:
            app.set_accels_for_action(action_name, [accel])

    def _ensure_thumb_menu(self):
        if getattr(self, "thumb_menu", None):
            return
        menu = Gio.Menu()
        menu.append("Définir comme fond", "win.thumb_set")
        menu.append("Ouvrir dans Fichiers", "win.thumb_reveal")
        menu.append("Copier le chemin", "win.thumb_copy_path")
        menu.append("Ajouter au slideshow", "win.thumb_slideshow_add")
        menu.append("Retirer du slideshow", "win.thumb_slideshow_remove")
        self.thumb_menu = Gtk.PopoverMenu.new_from_model(menu)

    def _menu_import(self, action, param):
        self._on_choose_folder(None)

    def _menu_remove(self, action, param):
        self._status("Action " + action.get_name() + " non implémentée")

    # HELPERS
    # ────────────────────────────────────────────────────────────

    def _monitor_markup(self, idx: int) -> str:
        m = self.monitors[idx]
        primary = " <b>(principal)</b>" if m.primary else ""
        return (
            f"<small>{m.name}{primary}\n"
            f"{m.width} × {m.height} — pos({m.x}, {m.y})</small>"
        )

    def _status(self, msg: str):
        self.status_label.set_text(msg)
        # G2 — Toast pour les messages d'action (préfixés ✓ ✗ ⚠ ⏱)
        if msg and msg[0] in ("✓", "✗", "⚠", "⏱") and hasattr(self, "_toast_overlay"):
            toast = Adw.Toast.new(msg)
            toast.set_timeout(3)
            self._toast_overlay.add_toast(toast)

    def _on_about(self, *_) -> None:
        try:
            dialog = Adw.AboutDialog()
            dialog.set_application_name("Mural")
            dialog.set_version("0.1.1")
            dialog.set_developer_name("GaoR")
            dialog.set_developers(["GaoR https://github.com/gaorfg-bit"])
            dialog.set_application_icon("io.github.gaorfg-bit.Mural")
            dialog.set_website("https://github.com/gaorfg-bit/mural")
            dialog.set_issue_url(
                "https://github.com/gaorfg-bit/mural/issues"
            )
            dialog.set_copyright("© 2026 GaoR")
            dialog.set_license_type(Gtk.License.GPL_3_0)
            dialog.present(self)
        except AttributeError:
            win = Adw.AboutWindow(transient_for=self)
            win.set_application_name("Mural")
            win.set_version("0.1.1")
            win.set_developer_name("GaoR")
            win.set_developers(["GaoR"])
            win.set_application_icon("io.github.gaorfg-bit.Mural")
            win.set_website("https://github.com/gaorfg-bit/mural")
            win.set_issue_url(
                "https://github.com/gaorfg-bit/mural/issues"
            )
            win.set_copyright("© 2026 GaoR")
            win.set_license_type(Gtk.License.GPL_3_0)
            win.present()

    def _highlight_slideshow_image(self, path: str) -> bool:
        self._set_active_wallpapers([path])
        return False

    def _apply_wallpaper_global(self, path: str) -> bool:
        mode = self.mode_ids[self.mode_dropdown.get_selected()]
        lock = self.chk_lock.get_active()

        # Servir l'AVIF à GNOME si l'utilisateur l'a activé et qu'un cache existe
        apply_path = path
        if self.settings.avif_use_for_gnome and AVIF_SUPPORTED:
            cached = get_cached_avif(path)
            if cached:
                apply_path = str(cached)
                logger.debug("Serving AVIF to GNOME: %s", cached.name)

        if getattr(self, "_daemon", None) and self._daemon.available:
            ok = self._daemon.set_wallpaper(apply_path)
            if not ok:
                self._status("✗ Échec via daemon — fallback local")
                return self.backend.apply_single(apply_path, mode=mode, lock=lock)
            return True
        return self.backend.apply_single(apply_path, mode=mode, lock=lock)

    def _clear_selection(self):
        self.selected_image = None
        self._selected_path = None
        self._context_path = None
        self.btn_apply.set_sensitive(False)
        self.preview.set_paintable(None)
        # Cacher les infos image dans la statusbar
        self.lbl_name.set_visible(False)
        self.lbl_size.set_visible(False)
        self.lbl_dims.set_visible(False)
        self._sb_sep1.set_visible(False)
        self._sb_sep2.set_visible(False)
        self._sb_sep3.set_visible(False)
        if hasattr(self, "_preview_placeholder_label"):
            self._preview_placeholder_label.set_visible(True)
        self._set_selected_child(None)

    def _set_selected_image(self, path: str, child: Optional[Gtk.FlowBoxChild] = None) -> None:
        self.selected_image = path
        self._selected_path = path
        self.btn_apply.set_sensitive(True)
        self._update_preview(path)
        self._update_image_info(path)
        self._set_selected_child(child)
        self._preview_placeholder_label.set_visible(False)
        conn = self.monitors[self.current_monitor].connector
        self.settings.per_monitor[conn] = path
        # Ne pas sauvegarder ici — seulement à l'application
        self._status(f"Sélectionné: {Path(path).name}")

    def _set_selected_child(self, child: Optional[Gtk.FlowBoxChild]) -> None:
        if self._selected_child and self._selected_child is not child:
            self._selected_child.unset_state_flags(Gtk.StateFlags.SELECTED)
        self._selected_child = child
        if child:
            child.set_state_flags(Gtk.StateFlags.SELECTED, False)

    def _on_slideshow_toggle(self, switch, _param) -> None:
        enabled = switch.get_active()
        self.settings.slideshow_enabled = enabled
        self._schedule_save()
        if enabled:
            self.slideshow.start()
            self._status("⏱ Slideshow activé")
        else:
            self.slideshow.stop()
            self._status("Slideshow désactivé")

    def _on_interval_changed(self, spin) -> None:
        self.settings.slideshow_interval = int(spin.get_value())
        self._schedule_save()
        if self.slideshow.is_running():
            self.slideshow.start()

    def _on_random_toggle(self, switch, _param) -> None:
        self.settings.slideshow_random = switch.get_active()
        self._schedule_save()

    def _on_slideshow_monitor_toggled(
        self, chk, connector: str
    ) -> None:
        checked = [
            c for c, w in self._slideshow_monitor_checks.items()
            if w.get_active()
        ]
        # Si tous cochés → vide (= tous)
        if len(checked) == len(self.monitors):
            self.settings.slideshow_monitors = []
        else:
            self.settings.slideshow_monitors = checked
        self._schedule_save()

    def _on_search_changed(self, entry):
        self._search_text = (entry.get_text() or "").strip().lower()
        self.flowbox.invalidate_filter()

    def _flowbox_filter(self, child, user_data=None):
        q = self._search_text
        if not q:
            return True
        path = self._child_to_path.get(child, "")
        return q in Path(path).name.lower()

    def _on_thumb_right_click(self, gesture, n_press, x, y, path):
        self._context_path = path
        parent = gesture.get_widget()
        if self.thumb_menu.get_parent() is not parent:
            self.thumb_menu.set_parent(parent)
        self.thumb_menu.popup()

    def _on_thumb_set(self, action, param):
        path = self._context_path
        if not path or not Path(path).exists():
            return
        mode = self.mode_ids[self.mode_dropdown.get_selected()]
        lock = self.chk_lock.get_active()
        if getattr(self, "_daemon", None) and self._daemon.available:
            ok = self._daemon.set_wallpaper(path)
            if not ok:
                self._status("✗ Échec via daemon — fallback local")
                ok = self.backend.apply_single(path, mode, lock)
        else:
            ok = self.backend.apply_single(path, mode, lock)
        if ok:
            self._status(f"✓ Appliqué: {Path(path).name} ({mode})")
        else:
            self._status("✗ Erreur d'application")

    def _on_thumb_reveal(self, action, param):
        path = self._context_path
        if not path or not Path(path).exists():
            return
        uri = Path(path).parent.as_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            self._status("✗ Ouverture impossible")

    def _on_thumb_copy_path(self, action, param):
        path = self._context_path
        if not path:
            return
        try:
            display = Gdk.Display.get_default()
            if display:
                clipboard = display.get_clipboard()
                clipboard.set_text(path)
                self._status("Chemin copié")
        except Exception:
            self._status("✗ Copie impossible")

    def _on_thumb_slideshow_add(self, action, param) -> None:
        path = self._context_path
        if not path:
            return
        self.settings.add_to_slideshow(path)
        self._schedule_save()
        self._refresh_active_indicators()
        self._update_slideshow_count_label()
        self._status(f"✓ Ajouté au slideshow: {Path(path).name}")

    def _on_thumb_slideshow_remove(self, action, param) -> None:
        path = self._context_path
        if not path:
            return
        self.settings.remove_from_slideshow(path)
        self._schedule_save()
        self._refresh_active_indicators()
        self._update_slideshow_count_label()
        self._status(f"Retiré du slideshow: {Path(path).name}")

    def _on_folder_slideshow_toggled(self, btn: Gtk.ToggleButton) -> None:
        folder = str(self.folder)
        new_state = self.settings.toggle_folder_slideshow(folder)
        btn.handler_block_by_func(self._on_folder_slideshow_toggled)
        btn.set_active(new_state)
        btn.handler_unblock_by_func(self._on_folder_slideshow_toggled)
        self._schedule_save()
        self._refresh_active_indicators()
        self._update_slideshow_count_label()
        if new_state:
            self._status("✓ Dossier ajouté au slideshow")
        else:
            self._status("Dossier retiré du slideshow")

    # ── Handlers AVIF ────────────────────────────────────────────

    def _on_avif_gnome_toggle(self, switch, _param) -> None:
        self.settings.avif_use_for_gnome = switch.get_active()
        self._schedule_save()

    def _on_avif_convert(self, *_) -> None:
        if self.avif_converter.is_running():
            return
        self.btn_avif_convert.set_sensitive(False)
        self.btn_avif_cancel.set_visible(True)
        self._avif_progress_bar.set_visible(True)
        self._avif_progress_label.set_visible(True)
        self._avif_progress_bar.set_fraction(0)
        self._avif_progress_label.set_text("Démarrage…")
        self._status("⏱ Conversion AVIF en cours…")
        self.avif_converter.convert_folder(
            self.folder,
            Config.VALID_EXT,
            on_progress=self._on_avif_progress,
            on_done=self._on_avif_done,
        )

    def _on_avif_progress(self, converted: int, total: int, filename: str) -> None:
        if total > 0:
            self._avif_progress_bar.set_fraction(converted / total)
        self._avif_progress_label.set_text(f"{converted}/{total} — {filename}")

    def _on_avif_done(self, converted: int, total: int) -> None:
        self._avif_progress_bar.set_visible(False)
        self._avif_progress_label.set_visible(False)
        self.btn_avif_cancel.set_visible(False)
        self.btn_avif_convert.set_sensitive(True)
        self._update_avif_stats()
        if converted == 0 and total == 0:
            self._status("⚠ Aucune image à convertir dans ce dossier")
        else:
            self._status(f"✓ AVIF : {converted}/{total} images converties")

    def _on_avif_purge(self, *_) -> None:
        removed = self.avif_converter.purge_folder(self.folder)
        self._update_avif_stats()
        self._status(f"✓ Cache AVIF purgé ({removed} fichiers supprimés)")

    def _update_avif_stats(self) -> None:
        if not hasattr(self, "_avif_stats_label") or not AVIF_SUPPORTED:
            return
        stats = self.avif_converter.folder_stats(self.folder, Config.VALID_EXT)
        if stats["total"] == 0:
            self._avif_stats_label.set_text("Dossier vide")
            return
        cached = stats["cached"]
        total = stats["total"]
        orig_mb = stats["size_original_mb"]
        avif_mb = stats["size_avif_mb"]
        saving = stats["saving_pct"]
        if cached == 0:
            self._avif_stats_label.set_text(f"{total} images — aucun cache AVIF")
        else:
            self._avif_stats_label.set_text(
                f"{cached}/{total} converties — "
                f"{orig_mb:.1f} Mo → {avif_mb:.1f} Mo (−{saving:.0f}%)"
            )

    def _on_add_bookmark(self, *_) -> None:
        folder = str(self.folder)
        if folder not in self.settings.folder_bookmarks:
            self.settings.folder_bookmarks.append(folder)
            self._schedule_save()
            self._rebuild_bookmarks_menu()
            self._status(f"✓ Favori ajouté: {self.folder.name}")
        else:
            self._status("Dossier déjà dans les favoris")

    def _rebuild_bookmarks_menu(self) -> None:
        # D3 — Supprimer uniquement les actions trackées (plus de loop aveugle range(100))
        for name in self._bookmark_action_names:
            try:
                self.remove_action(name)
            except Exception:
                pass
        self._bookmark_action_names.clear()

        bookmarks = self.settings.folder_bookmarks
        menu = Gio.Menu()

        if not bookmarks:
            # Entrée désactivée — juste informatif
            section = Gio.Menu()
            section.append("Aucun favori", None)
            menu.append_section(None, section)
        else:
            for i, path in enumerate(bookmarks):
                name = Path(path).name
                action_id = f"bookmark_{i}"
                # Créer l'action avec le path capturé
                action = Gio.SimpleAction.new(action_id, None)
                action.connect(
                    "activate",
                    lambda _a, _v, p=path: self._jump_to_folder(p)
                )
                self.add_action(action)
                self._bookmark_action_names.append(action_id)  # D3 — tracking
                menu.append(name, f"win.{action_id}")

            # Séparateur + supprimer
            sep_section = Gio.Menu()
            sep_section.append(
                "Retirer le dossier actuel",
                "win.bookmark_remove_current"
            )
            menu.append_section(None, sep_section)

        # Action pour retirer le dossier courant des favoris
        try:
            self.remove_action("bookmark_remove_current")
        except Exception:
            pass
        remove_action = Gio.SimpleAction.new(
            "bookmark_remove_current", None
        )
        remove_action.connect("activate", self._on_remove_bookmark)
        self.add_action(remove_action)

        popover = Gtk.PopoverMenu.new_from_model(menu)
        self.btn_bookmarks.set_popover(popover)

    def _jump_to_folder(self, path: str) -> None:
        folder = Path(path)
        if not folder.exists():
            self._status(f"✗ Dossier introuvable: {folder.name}")
            return
        self._set_selected_folder(Gio.File.new_for_path(str(folder)))

    def _on_remove_bookmark(self, *_) -> None:
        folder = str(self.folder)
        if folder in self.settings.folder_bookmarks:
            self.settings.folder_bookmarks.remove(folder)
            self._schedule_save()
            self._rebuild_bookmarks_menu()
            self._status(f"Favori retiré: {self.folder.name}")
        else:
            self._status("Ce dossier n'est pas dans les favoris")

    def _on_assign_monitor_folder(
        self, btn, connector: str, row: Adw.ActionRow
    ) -> None:
        def _on_folder_chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder:
                path = folder.get_path()
                self.settings.monitor_folders[connector] = path
                self._schedule_save()
                row.set_subtitle(path[-40:])
                self._status(
                    f"✓ Dossier assigné à l'écran {connector}"
                )
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title("Choisir le dossier de cet écran")
            dialog.select_folder(self, None, _on_folder_chosen)
        else:
            dialog = Gtk.FileChooserNative(
                title="Choisir le dossier",
                transient_for=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            dialog.connect("response", lambda d, r: (
                self._on_assign_monitor_folder_response(
                    d, r, connector, row
                )
            ))
            dialog.show()
            self._file_dialog = dialog

    def _on_load_monitor_folder(
        self, btn, connector: str
    ) -> None:
        path = self.settings.monitor_folders.get(connector, "")
        if not path or not Path(path).exists():
            self._status(
                f"✗ Aucun dossier assigné à cet écran — "
                f"cliquez d'abord sur l'icône dossier"
            )
            return
        # Sélectionner l'écran correspondant
        for i, mon in enumerate(self.monitors):
            if mon.connector == connector:
                self.current_monitor = i
                if hasattr(self, "monitor_btns") and self.monitor_btns:
                    self.monitor_btns[i].set_active(True)
                break
        # Charger le dossier
        self._jump_to_folder(path)

    def _on_assign_monitor_folder_response(
        self,
        dialog,
        response,
        connector: str,
        row: Adw.ActionRow,
    ) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            folder = None
            if hasattr(dialog, "get_current_folder"):
                folder = dialog.get_current_folder()
            if not folder:
                folder = dialog.get_file()
            if folder:
                path = folder.get_path()
                self.settings.monitor_folders[connector] = path
                self._schedule_save()
                row.set_subtitle(path[-40:])
                self._status(
                    f"✓ Dossier assigné à l'écran {connector}"
                )
        dialog.destroy()
        self._file_dialog = None

    def _update_slideshow_count_label(self) -> None:
        if not hasattr(self, "lbl_slideshow_count"):
            return
        playlist = self.settings.resolve_slideshow_playlist()
        n = len(playlist)
        folders = len(self.settings.slideshow_folders)
        manual = len(self.settings.slideshow_images)
        parts = []
        if folders:
            parts.append(f"{folders} dossier{'s' if folders > 1 else ''}")
        if manual:
            suffix = "s" if manual > 1 else ""
            parts.append(f"{manual} image{suffix} manuelle{suffix}")
        if parts:
            self.lbl_slideshow_count.set_text(
                f"{n} image{'s' if n != 1 else ''} ({', '.join(parts)})"
            )
        else:
            self.lbl_slideshow_count.set_text("Aucune image sélectionnée")

    def _sync_flowbox(self):
        self.flowbox.invalidate_filter()
        self.flowbox.queue_resize()
        self._schedule_flowbox_column_update()

    def _sync_folder_slideshow_btn(self) -> None:
        if not hasattr(self, "btn_folder_slideshow"):
            return
        folder = str(self.folder)
        active = folder in self.settings.slideshow_folders
        self.btn_folder_slideshow.handler_block_by_func(
            self._on_folder_slideshow_toggled
        )
        self.btn_folder_slideshow.set_active(active)
        self.btn_folder_slideshow.handler_unblock_by_func(
            self._on_folder_slideshow_toggled
        )

    def _init_shortcuts(self):
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._on_key_shortcut)
        self.add_controller(controller)

    def _on_key_shortcut(self, controller, keyval, keycode, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and keyval == Gdk.KEY_b:
            self._sidebar_toggle.set_active(not self._sidebar_toggle.get_active())
            return True
        return False

    def _refresh_flowbox_columns(self) -> bool:
        self._column_update_id = None
        width = self.flowbox.get_width()
        if width <= 0:
            width = self._scroll.get_width()
        if width <= 0:
            self._schedule_flowbox_column_update()
            return False

        margin = (self.flowbox.get_margin_start()
                  + self.flowbox.get_margin_end())
        available = max(0, width - margin)
        spacing = self.flowbox.get_column_spacing()

        # Cible : entre 80px et 160px par vignette selon la place
        for target in [160, 140, 120, 100, 80]:
            cols = max(1, (available + spacing) // (target + spacing))
            if cols >= 3:
                break

        thumb_w = max(
            100,
            (available - spacing * (cols - 1)) // cols
        )
        thumb_h = max(
            1,
            int(round(thumb_w * Config.THUMBNAIL_ASPECT))
        )

        prev_thumb_w = Config.THUMB_W
        if (cols != self._flowbox_columns
                or thumb_w != Config.THUMB_W):
            self._flowbox_columns = cols
            Config.THUMB_W = thumb_w
            Config.THUMB_H = thumb_h
            Config.THUMBNAIL_SIZE = thumb_w
            self.flowbox.set_min_children_per_line(cols)
            self.flowbox.set_max_children_per_line(cols)
            # Recharger seulement si la différence est significative
            if abs(thumb_w - prev_thumb_w) > 20:
                self._schedule_gallery_reload()

        return False

    def _schedule_gallery_reload(self) -> None:
        if self._resize_reload_id is not None:
            GLib.source_remove(self._resize_reload_id)
        self._resize_reload_id = GLib.timeout_add(
            200, self._on_gallery_reload_timeout
        )

    def _on_gallery_reload_timeout(self) -> bool:
        self._resize_reload_id = None
        self._load_gallery()
        return False

    def _schedule_flowbox_column_update(self) -> None:
        if self._column_update_id is not None:
            return
        self._column_update_id = GLib.idle_add(self._refresh_flowbox_columns)

    def _update_preview(self, path: str):
        if self._preview_timeout_id:
            GLib.source_remove(self._preview_timeout_id)
            self._preview_timeout_id = None

        self._preview_timeout_id = GLib.timeout_add(
            75,
            lambda: (self._apply_preview(path), False)[1],
        )

    def _apply_preview(self, path: str) -> None:
        self._preview_timeout_id = None

        def _load_texture() -> None:
            # Utiliser l'AVIF en cache si disponible (plus léger), sinon l'original
            cached = get_cached_avif(path)
            display_path = str(cached) if cached else path
            try:
                texture: Optional[Gdk.Texture] = Gdk.Texture.new_from_file(
                    Gio.File.new_for_path(display_path)
                )
            except Exception as e:
                logger.error(
                    "Preview texture load error [%s]: %s",
                    Path(path).name,
                    e,
                )
                texture = None

            def _set_texture() -> bool:
                if self.selected_image == path:
                    if texture is None:
                        self.preview.set_paintable(None)
                    else:
                        self.preview.set_paintable(texture)
                return False

            GLib.idle_add(_set_texture)

        threading.Thread(target=_load_texture, daemon=True).start()

    def _update_image_info(self, path: str) -> None:
        p = Path(path)
        self.lbl_name.set_markup(f"<b>{p.name}</b>")
        self.lbl_name.set_visible(True)
        self._sb_sep1.set_visible(True)
        try:
            sz = p.stat().st_size
            if sz > 1_048_576:
                self.lbl_size.set_text(f"{sz / 1_048_576:.1f} Mo")
            else:
                self.lbl_size.set_text(f"{sz / 1024:.0f} Ko")
            self.lbl_size.set_visible(True)
            self._sb_sep2.set_visible(True)
        except Exception:
            self.lbl_size.set_visible(False)
            self._sb_sep2.set_visible(False)

        self.lbl_dims.set_text("…")
        self.lbl_dims.set_visible(True)
        self._sb_sep3.set_visible(True)

        def _load_dims() -> None:
            dims = ImageLoader.get_dimensions(path)

            def _update() -> bool:
                if self.selected_image == path:
                    if dims:
                        self.lbl_dims.set_text(f"{dims[0]} × {dims[1]} px")
                    else:
                        self.lbl_dims.set_visible(False)
                        self._sb_sep3.set_visible(False)
                return False

            GLib.idle_add(_update)

        threading.Thread(target=_load_dims, daemon=True).start()

    def _init_state(self):
        self.lbl_folder.set_text(str(self.folder))
        current = self.backend.get_current()
        if current and Path(current).exists():
            self.selected_image = current
            self.btn_apply.set_sensitive(True)
            self._update_preview(current)
            self._update_image_info(current)
            self._set_active_wallpapers([current])

        mode = self.backend.get_mode()
        if mode:
            if mode in self.mode_ids:
                self.mode_dropdown.set_selected(self.mode_ids.index(mode))
        self._sync_folder_slideshow_btn()
        GLib.idle_add(self._update_avif_stats)

    # ────────────────────────────────────────────────────────────
    # EVENTS
    # ────────────────────────────────────────────────────────────

    def _on_monitor_toggle(self, btn, index: int) -> None:
        if not btn.get_active():
            return
        self.current_monitor = index

        for j, b in enumerate(self.monitor_btns):
            if j == index:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")

        # Mettre à jour le label d'info moniteur
        if hasattr(self, "lbl_monitor"):
            self.lbl_monitor.set_markup(self._monitor_markup(index))

        # Charger le dossier assigné à cet écran si défini
        connector = self.monitors[index].connector
        assigned_folder = self.settings.monitor_folders.get(
            connector, ""
        )
        if assigned_folder and Path(assigned_folder).exists():
            if str(self.folder) != assigned_folder:
                self._jump_to_folder(assigned_folder)
                self._status(
                    f"Écran {index + 1} — "
                    f"dossier: {Path(assigned_folder).name}"
                )
        else:
            # Pas de dossier assigné — on garde la galerie actuelle
            self._status(
                f"Écran {index + 1} sélectionné "
                f"(aucun dossier assigné)"
            )

    def _on_choose_folder(self, widget):
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title("Choisir un dossier")
            dialog.select_folder(self, None, self._on_choose_folder_dialog)
            return

        dialog = Gtk.FileChooserNative(
            title="Choisir un dossier",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.connect("response", self._on_choose_folder_response)
        dialog.show()
        self._file_dialog = dialog

    def _set_selected_folder(self, folder: Gio.File):
        self.folder = Path(folder.get_path())
        self.settings.folder = str(self.folder)
        self._schedule_save()
        self.lbl_folder.set_text(str(self.folder))
        self._load_gallery()
        self._sync_folder_slideshow_btn()
        GLib.idle_add(self._update_avif_stats)

    def _on_choose_folder_dialog(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            folder = None
        if folder:
            self._set_selected_folder(folder)

    def _on_choose_folder_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            folder = None
            if hasattr(dialog, "get_current_folder"):
                folder = dialog.get_current_folder()
            if not folder:
                folder = dialog.get_file()
            if folder:
                self._set_selected_folder(folder)
        dialog.destroy()
        self._file_dialog = None

    def _on_clear_cache(self, widget):
        dialog = Gtk.AlertDialog()
        dialog.set_message("Vider le cache des miniatures ?")
        dialog.set_detail("Elles seront recréées au prochain chargement.")
        dialog.set_buttons(["Annuler", "Vider"])
        dialog.set_default_button(1)
        dialog.set_cancel_button(0)
        dialog.choose(self, None, self._on_clear_cache_response)

    def _on_clear_cache_response(self, dialog, result):
        try:
            response = dialog.choose_finish(result)
        except GLib.Error:
            return
        if response == 1:
            count = 0
            for entry in Config.THUMB_DIR.iterdir():
                if not entry.is_file():
                    continue
                try:
                    entry.unlink()
                    count += 1
                except Exception:
                    pass
            self._status(f"Cache vidé ({count} fichiers)")
            self._load_gallery()

    def _on_gallery_click(self, flowbox, child):
        self.flowbox.select_child(child)

    def _on_flowbox_selection_changed(self, flowbox, child=None):
        children = flowbox.get_selected_children()
        if not children:
            self._clear_selection()
            return
        thumb = children[-1]
        path = self._child_to_path.get(thumb, "")
        if not path or not Path(path).exists():
            self._clear_selection()
            return
        self._set_selected_image(path, thumb)

    def _on_apply(self, widget):
        if not self.selected_image or not Path(self.selected_image).exists():
            dialog = Gtk.AlertDialog()
            dialog.set_message("Aucune image sélectionnée")
            dialog.set_buttons(["OK"])
            dialog.choose(self, None, lambda *_: None)
            return

        mode = self.mode_ids[self.mode_dropdown.get_selected()]
        lock = self.chk_lock.get_active()
        same_all = self.chk_same_all.get_active() if self.chk_same_all else True

        self.btn_apply.set_sensitive(False)
        self._status("Application en cours…")

        if same_all or len(self.monitors) <= 1:
            # Mode global — une image sur tous les écrans
            if getattr(self, "_daemon", None) and self._daemon.available:
                ok = self._daemon.set_wallpaper(self.selected_image)
                if not ok:
                    self._status("✗ Échec via daemon — fallback local")
                    ok = self.backend.apply_single(
                        self.selected_image, mode=mode, lock=lock
                    )
            else:
                ok = self.backend.apply_single(
                    self.selected_image, mode=mode, lock=lock
                )
            if ok:
                for mon in self.monitors:
                    self.settings.per_monitor[mon.connector] = \
                        self.selected_image
                self._set_active_wallpapers([self.selected_image])
                self._status(
                    f"✓ Appliqué: {Path(self.selected_image).name}"
                )
            else:
                self._status("✗ Échec de l'application")
        else:
            # Mode per-monitor — composite OBLIGATOIRE
            # S'assurer que l'écran courant a bien l'image sélectionnée
            conn = self.monitors[self.current_monitor].connector
            self.settings.per_monitor[conn] = self.selected_image

            # Vérifier que TOUS les écrans ont une image
            missing = []
            for mon in self.monitors:
                img = self.settings.per_monitor.get(mon.connector, "")
                if not img or not Path(img).exists():
                    missing.append(mon.name)

            if missing:
                self._status(
                    f"⚠ Images manquantes pour : "
                    f"{', '.join(missing)} — "
                    f"sélectionnez une image pour chaque écran"
                )
                self.btn_apply.set_sensitive(True)
                return

            assignments = {
                mon.connector: self.settings.per_monitor[mon.connector]
                for mon in self.monitors
            }
            monitors_snapshot = list(self.monitors)

            def _apply_composite_thread():
                results = self.backend.apply_per_monitor(
                    assignments, mode, lock, monitors=monitors_snapshot
                )
                ok_count = sum(1 for v in results.values() if v)
                total = len(results)

                def _on_done():
                    if ok_count == total:
                        self._set_active_wallpapers(list(assignments.values()))
                        self._status(
                            f"✓ Composite appliqué: {ok_count}/{total} écrans"
                        )
                    else:
                        self._status(
                            f"⚠ Composite partiel: {ok_count}/{total} écrans"
                        )
                    self.btn_apply.set_sensitive(True)
                    self.btn_apply.set_label("✓ Appliqué !")
                    GLib.timeout_add(
                        2500,
                        lambda: (self.btn_apply.set_label("Définir comme fond"), False)[-1]
                    )
                GLib.idle_add(_on_done)

            threading.Thread(target=_apply_composite_thread, daemon=True).start()
            self.settings.mode = mode
            self.settings.lock_screen = lock
            self._schedule_save()
            return  # le reste est géré dans le thread

        self.settings.mode = mode
        self.settings.lock_screen = lock
        self._schedule_save()

        self.btn_apply.set_sensitive(True)
        self.btn_apply.set_label("✓ Appliqué !")
        GLib.timeout_add(
            2500,
            lambda: (self.btn_apply.set_label("Définir comme fond"), False)[-1]
        )

    # ────────────────────────────────────────────────────────────
    # GALLERY
    # ────────────────────────────────────────────────────────────

    def _load_gallery(self):
        self._stop_event.set()
        self._selected_child = None
        child = self.flowbox.get_first_child()
        self._thumb_views.clear()
        self._child_to_path.clear()
        while child:
            next_child = child.get_next_sibling()
            self.flowbox.remove(child)
            child = next_child
        self._stop_event = threading.Event()

        self._gallery_progressbar.set_fraction(0)
        self._set_progress_visibility(True)
        self._status("Chargement…")

        self.gallery_generation += 1
        generation = self.gallery_generation
        threading.Thread(
            target=self._gallery_worker,
            args=(Path(self.folder), generation, self._stop_event),
            daemon=True,
        ).start()
        GLib.timeout_add(100, self._refresh_flowbox_columns)

    def _gallery_worker(self, folder: Path, generation: int, stop_event: threading.Event):
        if not folder.exists():
            GLib.idle_add(self._status, "Dossier introuvable")
            GLib.idle_add(self._set_progress_visibility, False)
            return

        try:
            files = sorted(
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in Config.VALID_EXT
            )[:Config.MAX_IMAGES]
        except PermissionError:
            GLib.idle_add(self._status, "Permission refusée")
            GLib.idle_add(self._set_progress_visibility, False)
            return

        total = len(files)
        GLib.idle_add(self.lbl_count.set_text, f"{total} images")

        if total == 0:
            GLib.idle_add(self._status, "Aucune image trouvée")
            GLib.idle_add(self._set_progress_visibility, False)
            return

        BATCH_SIZE = 8
        batch: List[Tuple[Path, Optional[Path]]] = []

        for i, fpath in enumerate(files):
            if stop_event.is_set() or generation != self.gallery_generation:
                return

            # Utiliser l'AVIF comme source si disponible — plus léger à lire pour Pillow
            avif = get_cached_avif(str(fpath))
            thumb_source = str(avif) if avif else str(fpath)

            thumb_path = Thumbnailer.generate(
                thumb_source, Config.THUMB_W, Config.THUMB_H, Config.THUMB_DIR
            )
            if generation != self.gallery_generation:
                return

            batch.append((fpath, thumb_path))

            if len(batch) >= BATCH_SIZE:
                # Throttle : max 4 batchs en vol simultanément
                while True:
                    with self._pending_batches_lock:
                        if self._pending_batches < 4:
                            self._pending_batches += 1
                            break
                    if stop_event.is_set():
                        return
                    import time
                    time.sleep(0.05)
                GLib.idle_add(self._add_thumb_batch_counted, list(batch))
                batch.clear()

            if i % 16 == 0 and generation == self.gallery_generation:
                GLib.idle_add(
                    self._gallery_progressbar.set_fraction, (i + 1) / total
                )

        if batch and generation == self.gallery_generation:
            with self._pending_batches_lock:
                self._pending_batches += 1
            GLib.idle_add(self._add_thumb_batch_counted, list(batch))

        if generation == self.gallery_generation:
            GLib.idle_add(self._gallery_done, total)
            GLib.idle_add(self.flowbox.invalidate_filter)
        gc.collect()

    def _add_thumb_batch(
        self,
        items: List[Tuple[Path, Optional[Path]]],
        done_event: Optional[threading.Event],
    ) -> bool:
        for fpath, thumb_path in items:
            if self._stop_event.is_set():
                break
            self._add_thumb(fpath, thumb_path)
        if done_event is not None:
            done_event.set()
        return False

    def _add_thumb_batch_counted(self, items: list) -> bool:
        """Version fire-and-forget — décrémente le compteur après traitement."""
        for fpath, thumb_path in items:
            if self._stop_event.is_set():
                break
            self._add_thumb(fpath, thumb_path)
        with self._pending_batches_lock:
            self._pending_batches = max(0, self._pending_batches - 1)
        return False

    def _add_thumb(self, fpath: Path, thumb_path: Optional[Path]):
        if self._stop_event.is_set():
            return False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_name(str(fpath))
        box.set_size_request(Config.THUMB_W + 12, Config.THUMB_H + 8)

        frame = Gtk.Frame()
        frame.set_hexpand(False)
        frame.set_vexpand(False)
        frame.set_child(box)
        frame.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [Path(fpath).name]
        )

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("pressed", self._on_thumb_right_click, str(fpath))
        frame.add_controller(gesture)

        paintable = None
        source_path = thumb_path if thumb_path and thumb_path.exists() else fpath
        placeholder = Gtk.Image.new_from_icon_name("image-missing")
        placeholder.set_size_request(Config.THUMB_W, Config.THUMB_H)
        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(Config.THUMB_W, Config.THUMB_H)

        def _on_texture_loaded(texture: Optional[Gdk.Texture]) -> None:
            if overlay.get_parent() is None:
                return
            if texture is None:
                overlay.set_child(placeholder)
                return
            picture.set_paintable(texture)
            overlay.set_child(picture)

        overlay = Gtk.Overlay()
        overlay.set_child(placeholder)
        overlay.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [f"Aperçu de {Path(fpath).name}"]
        )
        indicator = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        indicator.add_css_class("thumb-indicator")
        indicator.set_valign(Gtk.Align.START)
        indicator.set_halign(Gtk.Align.END)
        indicator.set_margin_top(6)
        indicator.set_margin_end(6)
        indicator.set_visible(False)
        indicator.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Fond d'écran actif"]
        )
        overlay.add_overlay(indicator)

        slideshow_indicator = Gtk.Image.new_from_icon_name("starred-symbolic")
        slideshow_indicator.add_css_class("slideshow-indicator")
        slideshow_indicator.set_valign(Gtk.Align.END)
        slideshow_indicator.set_halign(Gtk.Align.START)
        slideshow_indicator.set_margin_bottom(6)
        slideshow_indicator.set_margin_start(6)
        slideshow_indicator.set_visible(
            self.settings.is_in_slideshow(str(fpath))
        )
        slideshow_indicator.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Dans le slideshow"]
        )
        overlay.add_overlay(slideshow_indicator)

        box.append(overlay)

        if source_path and Path(source_path).exists():
            self._load_texture_async(str(source_path), _on_texture_loaded)

        self._thumb_views[str(fpath)] = (box, indicator, slideshow_indicator)
        # Ne pas appeler _refresh_active_indicators ici — trop coûteux sur 500 images.
        # L'indicateur est initialisé directement selon l'état courant.
        active = str(fpath) in self._active_wallpapers
        if active:
            box.add_css_class("thumb-active")
        indicator.set_visible(active)

        self.flowbox.append(frame)
        fb_child = self.flowbox.get_last_child()
        if fb_child is not None:
            self._child_to_path[fb_child] = str(fpath)
        return False

    def _load_texture_async(self, path: str, on_done) -> None:
        gfile = Gio.File.new_for_path(path)

        def _finish(file_obj, result):
            try:
                data = file_obj.load_bytes_finish(result)
                gbytes = data[0] if isinstance(data, tuple) else data
                texture = Gdk.Texture.new_from_bytes(gbytes)
            except Exception:
                texture = None
            on_done(texture)

        try:
            gfile.load_bytes_async(None, _finish)
        except Exception:
            on_done(None)

    def _gallery_done(self, total):
        self._set_progress_visibility(False)
        self._status(f"✓ {total} images — {self.folder.name}")
        child = self.flowbox.get_first_child()
        if child:
            self.flowbox.select_child(child)
        return False

    def _set_progress_visibility(self, visible: bool):
        if self._progress_container is None:
            return
        self._progress_container.set_visible(visible)
        self._gallery_progressbar.set_visible(visible)

    def _set_active_wallpapers(self, paths: Iterable[str]) -> None:
        normalized = {
            str(Path(path))
            for path in paths
            if path and Path(path).exists()
        }
        self._active_wallpapers = normalized
        self._refresh_active_indicators()

    def _refresh_active_indicators(self) -> None:
        for path, (box, indicator, slideshow_indicator) in self._thumb_views.items():
            active = path in self._active_wallpapers
            if active:
                box.add_css_class("thumb-active")
            else:
                box.remove_css_class("thumb-active")
            indicator.set_visible(active)
            slideshow_indicator.set_visible(
                self.settings.is_in_slideshow(path)
            )

    def _on_monitors_changed(self, list_model, position, removed, added) -> None:
        """G5 — Mise à jour dynamique quand un écran est branché/débranché."""
        logger.info("Monitors changed: +%d -%d at pos %d", added, removed, position)
        self.monitors = MonitorDetector.detect()
        n = len(self.monitors)
        self._title_widget.set_subtitle(
            f"{n} écran{'s' if n > 1 else ''} détecté{'s' if n > 1 else ''}"
        )
        if self.current_monitor >= len(self.monitors):
            self.current_monitor = 0
        self._status(f"Écrans mis à jour: {n} détecté{'s' if n > 1 else ''}")

    # D2 — Propriétés découplées (SlideshowManager ne touche plus aux widgets)
    @property
    def current_mode(self) -> str:
        """Mode d'affichage sélectionné."""
        return self.mode_ids[self.mode_dropdown.get_selected()]

    @property
    def apply_to_lockscreen(self) -> bool:
        """True si le fond doit être appliqué à l'écran de verrouillage."""
        return self.chk_lock.get_active()

    @property
    def active_monitors(self) -> list:
        """Connecteurs moniteurs actifs pour le slideshow."""
        return list(self.settings.slideshow_monitors)

    # D5 — Debounce config save
    def _schedule_save(self) -> None:
        """Debounce config save — une seule écriture disque après 500ms de silence."""
        if self._save_timeout_id is not None:
            GLib.source_remove(self._save_timeout_id)
        self._save_timeout_id = GLib.timeout_add(500, self._do_save)

    def _do_save(self) -> bool:
        self._save_timeout_id = None
        self.config.save(self.settings)
        return False

    def _on_close_request(self, *_) -> bool:
        self.slideshow.stop()
        self.avif_converter.shutdown()
        self.settings.window_maximized = self.is_maximized()
        if not self.is_maximized():
            w, h = self.get_width(), self.get_height()
            if w > 100 and h > 100:
                self.settings.window_width = w
                self.settings.window_height = h
        self.config.save(self.settings)  # synchrone — app se ferme
        return False
