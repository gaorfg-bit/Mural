from __future__ import annotations

import gc
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import pillow_avif
except ImportError:
    pass

from PIL import Image
from PIL import ImageFile

import gi

gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .backend import GnomeBackend
from .config import Config
from .daemon import MuralDaemonProxy
from .monitors import MonitorDetector
from .slideshow import SlideshowManager
from .thumbnails import ImageLoader, Thumbnailer
from .avif_cache import get_cached_avif, AVIF_SUPPORTED

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
    -gtk-icon-size: 14px;
    background: alpha(black, 0.55);
    border-radius: 10px;
    padding: 2px;
}
paned > separator {
    min-width: 0; min-height: 0;
    background: transparent; border: none; opacity: 0;
}
flowboxchild {
    padding: 0; border: none; outline: none; border-radius: 6px;
}
flowboxchild:selected {
    background: transparent;
    outline: 3px solid @accent_color;
    outline-offset: -3px;
}
flowboxchild picture, flowboxchild overlay { border-radius: 6px; }
.thumb-indicator-active {
    color: white; background: @accent_color;
    border-radius: 10px; padding: 1px; -gtk-icon-size: 14px;
}
"""

class WallpaperApp(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application)

        self.config = Config()
        self.settings = self.config.load()
        self.backend = GnomeBackend()
        self.slideshow = SlideshowManager(self)
        self._daemon = MuralDaemonProxy()
        from .avif_cache import FolderConverter
        self.avif_converter = FolderConverter()
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
        self._bookmark_action_names: list[str] = []
        self._save_timeout_id: Optional[int] = None
        self._pending_batches: int = 0
        self._pending_batches_lock = threading.Lock()
        self.folder = (
            Path(self.settings.folder)
            if self.settings.folder
            else Config.default_folder()
        )
        self._slideshow_css_added = False
        _display = Gdk.Display.get_default()
        if _display:
            _display.get_monitors().connect("items-changed", self._on_monitors_changed)

        self.set_title("Mural")

        self._build_ui()
        self._ensure_slideshow_css()
        w = self.settings.window_width or 1280
        h = self.settings.window_height or 800
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

    def _build_ui(self):
        hb = Gtk.HeaderBar()
        hb.set_show_title_buttons(True)
        self.set_decorated(True)
        n_mon = len(self.monitors)
        self._title_widget = Adw.WindowTitle(
            title="Mural",
            subtitle=f"{n_mon} écran{'s' if n_mon > 1 else ''} détecté{'s' if n_mon > 1 else ''}",
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

        self.btn_apply = Gtk.Button()
        self.btn_apply.add_css_class("suggested-action")
        self.btn_apply.set_sensitive(False)
        self.btn_apply.connect("clicked", self._on_apply)
        _apply_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _apply_box.set_halign(Gtk.Align.CENTER)
        self._btn_apply_title = Gtk.Label(label="Définir comme fond")
        self._btn_apply_title.add_css_class("heading")
        self._btn_apply_subtitle = Gtk.Label(label="")
        self._btn_apply_subtitle.add_css_class("caption")
        self._btn_apply_subtitle.set_opacity(0.75)
        self._btn_apply_subtitle.set_visible(False)
        _apply_box.append(self._btn_apply_title)
        _apply_box.append(self._btn_apply_subtitle)
        self.btn_apply.set_child(_apply_box)
        hb.pack_end(self.btn_apply)

        app_menu = Gio.Menu()
        app_menu.append("Rafraîchir la galerie", "win.refresh")
        app_menu.append("Vider le cache", "win.clear_cache")
        _sep = Gio.Menu()
        _sep.append("À propos de Mural", "win.about")
        app_menu.append_section(None, _sep)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_menu_model(app_menu)
        hb.pack_end(menu_btn)

        self._sidebar_toggle = Gtk.ToggleButton()
        self._sidebar_toggle.set_icon_name("sidebar-show-right-symbolic")
        self._sidebar_toggle.set_active(True)
        self._sidebar_toggle.set_tooltip_text("Panneau (Ctrl+B)")
        self._sidebar_toggle.connect("toggled", self._on_sidebar_toggle)
        hb.pack_end(self._sidebar_toggle)

        self._split_view = Adw.OverlaySplitView()
        self._split_view.set_sidebar_position(Gtk.PackType.END)
        self._split_view.set_sidebar_width_fraction(0.28)
        self._split_view.set_min_sidebar_width(260)
        self._split_view.set_max_sidebar_width(380)
        self._split_view.set_show_sidebar(True)
        self._split_view.set_collapsed(False)
        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(hb)
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._split_view)
        toolbar_view.set_content(self._toast_overlay)
        self.set_content(toolbar_view)
        if getattr(self.settings, "window_maximized", False):
            self.maximize()

        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_vbox.set_hexpand(True)
        main_vbox.set_vexpand(True)
        main_vbox.set_size_request(400, -1)

        search_bar = Gtk.SearchBar()
        search_bar.set_search_mode(False)
        search_bar.set_show_close_button(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Rechercher\u2026")
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_bar.set_child(self.search_entry)
        search_bar.connect_entry(self.search_entry)
        self._search_toggle.bind_property(
            "active", search_bar, "search-mode-enabled",
            GObject.BindingFlags.BIDIRECTIONAL,
        )
        main_vbox.append(search_bar)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_wide_handle(False)

        self.preview = Gtk.Picture()
        self.preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.preview.set_can_shrink(True)
        self.preview.set_hexpand(True)
        self.preview.set_vexpand(False)
        self._preview_placeholder_label = Gtk.Label(label="S\u00e9lectionnez une image")
        self._preview_placeholder_label.add_css_class("dim-label")
        self._preview_placeholder_label.set_halign(Gtk.Align.CENTER)
        self._preview_placeholder_label.set_valign(Gtk.Align.CENTER)
        preview_overlay = Gtk.Overlay()
        preview_overlay.set_child(self.preview)
        preview_overlay.add_overlay(self._preview_placeholder_label)
        preview_overlay.set_hexpand(True)
        preview_overlay.set_vexpand(False)
        preview_overlay.set_size_request(-1, Config.PREVIEW_MAX_HEIGHT)
        paned.set_start_child(preview_overlay)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)
        paned.set_position(Config.PREVIEW_MAX_HEIGHT + 4)

        bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom_box.set_hexpand(True)
        bottom_box.set_vexpand(True)
        self._gallery_progressbar = Gtk.ProgressBar()
        self._gallery_progressbar.set_visible(False)
        self._gallery_progressbar.set_hexpand(True)
        self._gallery_progressbar.set_margin_top(3)
        self._gallery_progressbar.set_margin_bottom(1)
        self._progress_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._progress_container.set_hexpand(True)
        self._progress_container.set_vexpand(False)
        self._progress_container.append(self._gallery_progressbar)
        self._progress_container.set_visible(False)
        bottom_box.append(self._progress_container)

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
        self.flowbox.set_column_spacing(4)
        self.flowbox.set_row_spacing(4)
        self.flowbox.set_margin_start(2)
        self.flowbox.set_margin_end(2)
        self.flowbox.set_margin_top(4)
        self.flowbox.set_margin_bottom(4)
        self.flowbox.connect("child-activated", self._on_gallery_click)
        self.flowbox.connect("selected-children-changed", self._on_flowbox_selection_changed)
        self.flowbox.connect("notify::allocation", lambda *_: self._schedule_flowbox_column_update())
        self.flowbox.set_filter_func(self._flowbox_filter)
        scroll.set_child(self.flowbox)
        bottom_box.append(scroll)
        paned.set_end_child(bottom_box)
        paned.set_resize_end_child(True)
        paned.set_shrink_end_child(False)
        main_vbox.append(paned)

        sb_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        main_vbox.append(sb_sep)
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        statusbar.set_margin_start(10)
        statusbar.set_margin_end(10)
        statusbar.set_margin_top(4)
        statusbar.set_margin_bottom(4)
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_hexpand(True)
        self.status_label.add_css_class("dim-label")
        statusbar.append(self.status_label)
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
        self.lbl_dims.add_css_class("dim-label")
        self.lbl_dims.set_visible(False)
        statusbar.append(self.lbl_dims)
        self._sb_sep3 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self._sb_sep3.set_margin_top(4)
        self._sb_sep3.set_margin_bottom(4)
        self._sb_sep3.set_visible(False)
        statusbar.append(self._sb_sep3)
        self.lbl_size = Gtk.Label()
        self.lbl_size.add_css_class("dim-label")
        self.lbl_size.set_visible(False)
        statusbar.append(self.lbl_size)
        main_vbox.append(statusbar)
        self._split_view.set_content(main_vbox)

        self._sidebar_sep = None
        self._sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._sidebar.set_vexpand(True)
        sidebar = self._sidebar

        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        tab_bar.add_css_class("linked")
        tab_bar.set_margin_start(10)
        tab_bar.set_margin_end(10)
        tab_bar.set_margin_top(8)
        tab_bar.set_margin_bottom(8)
        self._tab_btns = {}
        first_tab_btn = None
        for tab_id, tab_label in [("display","Affichage"),("slideshow","Slideshow"),("folders","Dossiers"),("avif","AVIF")]:
            tb = Gtk.ToggleButton(label=tab_label)
            tb.set_hexpand(True)
            if first_tab_btn is None:
                first_tab_btn = tb
                tb.set_active(True)
            else:
                tb.set_group(first_tab_btn)
            tb.connect("toggled", self._on_tab_toggled, tab_id)
            tab_bar.append(tb)
            self._tab_btns[tab_id] = tb
        sidebar.append(tab_bar)
        sidebar.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        self._tab_stack = Gtk.Stack()
        self._tab_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._tab_stack.set_transition_duration(120)
        self._tab_stack.set_vexpand(True)
        sidebar.append(self._tab_stack)
        self._split_view.set_sidebar(self._sidebar)

        p_disp = Gtk.ScrolledWindow()
        p_disp.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        p_disp.set_vexpand(True)
        b_disp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        b_disp.set_margin_start(12); b_disp.set_margin_end(12)
        b_disp.set_margin_top(12); b_disp.set_margin_bottom(16)
        if len(self.monitors) > 1:
            g_sc = Adw.PreferencesGroup(title="\u00c9crans")
            mb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            mb_box.set_margin_start(12); mb_box.set_margin_end(12)
            mb_box.set_margin_top(8); mb_box.set_margin_bottom(8)
            mb_btns = Gtk.Box(spacing=4)
            mb_btns.set_halign(Gtk.Align.CENTER)
            self.monitor_btns = []
            first_mb = None
            for i, mon in enumerate(self.monitors):
                mlbl = f"\u00c9cran {i+1}" + (" \u2605" if mon.primary else "")
                mb = Gtk.ToggleButton(label=mlbl)
                mb.set_size_request(80, 36)
                if i == 0:
                    mb.set_active(True)
                    mb.add_css_class("suggested-action")
                if first_mb is None:
                    first_mb = mb
                else:
                    mb.set_group(first_mb)
                mb.connect("toggled", self._on_monitor_toggle, i)
                mb_btns.append(mb)
                self.monitor_btns.append(mb)
            mb_box.append(mb_btns)
            self.lbl_monitor = Gtk.Label()
            self.lbl_monitor.set_markup(self._monitor_markup(0))
            self.lbl_monitor.set_wrap(True)
            mb_box.append(self.lbl_monitor)

            # SIGNAL AJOUTE ICI POUR SYNCHRONISER LA CASE
            self.chk_same_all = Gtk.CheckButton(label="M\u00eame image sur tous")
            self.chk_same_all.set_active(True)
            self.chk_same_all.connect("toggled", self._on_same_all_toggled)
            mb_box.append(self.chk_same_all)

            mr = Adw.PreferencesRow(); mr.set_child(mb_box); g_sc.add(mr)
            b_disp.append(g_sc)
        else:
            self.monitor_btns = []
            self.chk_same_all = None
        g_opt = Adw.PreferencesGroup(title="Options")
        mi_row = Adw.PreferencesRow()
        mi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mi_box.set_margin_start(12); mi_box.set_margin_end(12)
        mi_box.set_margin_top(8); mi_box.set_margin_bottom(8)
        mi_lbl = Gtk.Label(label="Mode d'affichage")
        mi_lbl.set_xalign(0); mi_lbl.set_hexpand(True)
        mi_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        mi_box.append(mi_lbl)
        self.mode_ids = [mid for mid, _ in GnomeBackend.MODES]
        self.mode_dropdown = Gtk.DropDown.new_from_strings([ml for _, ml in GnomeBackend.MODES])
        self.mode_dropdown.set_selected(
            self.mode_ids.index(self.settings.mode) if self.settings.mode in self.mode_ids else 0
        )
        self.mode_dropdown.set_valign(Gtk.Align.CENTER)
        self.mode_dropdown.set_size_request(150, -1)
        mi_box.append(self.mode_dropdown)
        mi_row.set_child(mi_box); g_opt.add(mi_row)
        lk_row = Adw.ActionRow(title="\u00c9cran de verrouillage", subtitle="Appliquer aussi \u00e0 l'\u00e9cran de verrouillage")
        self.chk_lock = Gtk.Switch()
        self.chk_lock.set_active(self.settings.lock_screen)
        self.chk_lock.set_valign(Gtk.Align.CENTER)
        lk_row.add_suffix(self.chk_lock)
        lk_row.set_activatable_widget(self.chk_lock)
        g_opt.add(lk_row)
        b_disp.append(g_opt)
        p_disp.set_child(b_disp)
        self._tab_stack.add_named(p_disp, "display")

        p_ss = Gtk.ScrolledWindow()
        p_ss.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        p_ss.set_vexpand(True)
        b_ss = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        b_ss.set_margin_start(12); b_ss.set_margin_end(12)
        b_ss.set_margin_top(12); b_ss.set_margin_bottom(16)
        g_ss = Adw.PreferencesGroup(title="Slideshow")
        ss_row = Adw.ActionRow(title="Changement automatique", subtitle="Change le fond toutes les X minutes")
        self.switch_slideshow = Gtk.Switch()
        self.switch_slideshow.set_active(self.settings.slideshow_enabled)
        self.switch_slideshow.set_valign(Gtk.Align.CENTER)
        self.switch_slideshow.connect("notify::active", self._on_slideshow_toggle)
        ss_row.add_suffix(self.switch_slideshow)
        ss_row.set_activatable_widget(self.switch_slideshow)
        g_ss.add(ss_row)
        iv_row = Adw.PreferencesRow()
        iv_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        iv_box.set_margin_start(12); iv_box.set_margin_end(12)
        iv_box.set_margin_top(8); iv_box.set_margin_bottom(8)
        iv_lbl = Gtk.Label(label="Intervalle (min)")
        iv_lbl.set_hexpand(True); iv_lbl.set_xalign(0)
        iv_box.append(iv_lbl)
        self.spin_interval = Gtk.SpinButton()
        self.spin_interval.set_adjustment(Gtk.Adjustment(
            value=self.settings.slideshow_interval, lower=1, upper=1440,
            step_increment=1, page_increment=10,
        ))
        self.spin_interval.set_numeric(True)
        self.spin_interval.set_valign(Gtk.Align.CENTER)
        self.spin_interval.set_size_request(80, -1)
        self.spin_interval.connect("value-changed", self._on_interval_changed)
        iv_box.append(self.spin_interval)
        iv_row.set_child(iv_box); g_ss.add(iv_row)
        rnd_row = Adw.ActionRow(title="Ordre al\u00e9atoire")
        self.switch_random = Gtk.Switch()
        self.switch_random.set_active(self.settings.slideshow_random)
        self.switch_random.set_valign(Gtk.Align.CENTER)
        self.switch_random.connect("notify::active", self._on_random_toggle)
        rnd_row.add_suffix(self.switch_random)
        rnd_row.set_activatable_widget(self.switch_random)
        g_ss.add(rnd_row)
        b_ss.append(g_ss)
        if len(self.monitors) > 1:
            g_ssm = Adw.PreferencesGroup(title="Appliquer sur")
            self._slideshow_monitor_checks = {}
            for mon in self.monitors:
                chk = Gtk.CheckButton(label=f"\u00c9cran {mon.name[:24]}")
                chk.set_active(not self.settings.slideshow_monitors or mon.connector in self.settings.slideshow_monitors)
                chk.connect("toggled", self._on_slideshow_monitor_toggled, mon.connector)
                self._slideshow_monitor_checks[mon.connector] = chk
                cr = Adw.PreferencesRow()
                ci = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                ci.set_margin_start(12); ci.set_margin_end(12)
                ci.set_margin_top(6); ci.set_margin_bottom(6)
                ci.append(chk); cr.set_child(ci); g_ssm.add(cr)
            b_ss.append(g_ssm)
        self.lbl_slideshow_count = Gtk.Label()
        self.lbl_slideshow_count.add_css_class("dim-label")
        self.lbl_slideshow_count.set_xalign(0)
        self.lbl_slideshow_count.set_margin_start(4)
        self._update_slideshow_count_label()
        b_ss.append(self.lbl_slideshow_count)
        btn_next = Gtk.Button(label="\u23ed  Image suivante maintenant")
        btn_next.add_css_class("flat")
        btn_next.connect("clicked", lambda *_: self.slideshow.next())
        b_ss.append(btn_next)
        p_ss.set_child(b_ss)
        self._tab_stack.add_named(p_ss, "slideshow")

        p_fold = Gtk.ScrolledWindow()
        p_fold.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        p_fold.set_vexpand(True)
        b_fold = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        b_fold.set_margin_start(12); b_fold.set_margin_end(12)
        b_fold.set_margin_top(12); b_fold.set_margin_bottom(16)
        if len(self.monitors) > 1:
            g_fm = Adw.PreferencesGroup(title="Dossiers rapides")
            for i, mon in enumerate(self.monitors):
                fr = Adw.ActionRow(
                    title=f"\u00c9cran {i+1} \u2014 {mon.name[:20]}",
                    subtitle=self.settings.monitor_folders.get(mon.connector, "Non assign\u00e9")[-40:],
                )
                ba = Gtk.Button()
                ba.set_child(Gtk.Image.new_from_icon_name("folder-symbolic"))
                ba.set_valign(Gtk.Align.CENTER)
                ba.connect("clicked", self._on_assign_monitor_folder, mon.connector, fr)
                fr.add_suffix(ba)
                bl = Gtk.Button()
                bl.set_child(Gtk.Image.new_from_icon_name("go-jump-symbolic"))
                bl.set_valign(Gtk.Align.CENTER)
                bl.connect("clicked", self._on_load_monitor_folder, mon.connector)
                fr.add_suffix(bl)
                g_fm.add(fr)
            b_fold.append(g_fm)
        g_cur = Adw.PreferencesGroup(title="Dossier courant")
        fi_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        fi_box.set_margin_start(12); fi_box.set_margin_end(12)
        fi_box.set_margin_top(8); fi_box.set_margin_bottom(8)
        self.lbl_folder = Gtk.Label()
        self.lbl_folder.set_wrap(True); self.lbl_folder.set_xalign(0)
        self.lbl_folder.set_selectable(True); self.lbl_folder.set_max_width_chars(28)
        fi_box.append(self.lbl_folder)
        self.lbl_count = Gtk.Label()
        self.lbl_count.set_xalign(0); self.lbl_count.add_css_class("dim-label")
        fi_box.append(self.lbl_count)
        fir = Adw.PreferencesRow(); fir.set_child(fi_box); g_cur.add(fir)
        act_row = Adw.PreferencesRow()
        act_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        act_box.set_margin_start(12); act_box.set_margin_end(12)
        act_box.set_margin_top(6); act_box.set_margin_bottom(6)
        btn_bm = Gtk.Button()
        btn_bm.set_child(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        btn_bm.set_tooltip_text("Ajouter aux favoris")
        btn_bm.connect("clicked", self._on_add_bookmark)
        act_box.append(btn_bm)
        self.btn_bookmarks = Gtk.MenuButton()
        self.btn_bookmarks.set_child(Gtk.Image.new_from_icon_name("user-bookmarks-symbolic"))
        self.btn_bookmarks.set_tooltip_text("Ouvrir un favori")
        act_box.append(self.btn_bookmarks)
        self._rebuild_bookmarks_menu()
        self.btn_folder_slideshow = Gtk.ToggleButton()
        self.btn_folder_slideshow.set_child(Gtk.Image.new_from_icon_name("starred-symbolic"))
        self.btn_folder_slideshow.set_tooltip_text("Inclure dans le slideshow")
        self.btn_folder_slideshow.connect("toggled", self._on_folder_slideshow_toggled)
        act_box.append(self.btn_folder_slideshow)
        act_row.set_child(act_box); g_cur.add(act_row)
        b_fold.append(g_cur)
        p_fold.set_child(b_fold)
        self._tab_stack.add_named(p_fold, "folders")

        p_avif = Gtk.ScrolledWindow()
        p_avif.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        p_avif.set_vexpand(True)
        b_avif = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        b_avif.set_margin_start(12); b_avif.set_margin_end(12)
        b_avif.set_margin_top(12); b_avif.set_margin_bottom(16)
        g_avif = Adw.PreferencesGroup(title="Cache AVIF")
        self._avif_stats_label = Gtk.Label()
        self._avif_stats_label.set_xalign(0)
        self._avif_stats_label.add_css_class("dim-label")
        self._avif_stats_label.set_margin_start(12); self._avif_stats_label.set_margin_top(6)
        self._avif_stats_label.set_margin_bottom(2); self._avif_stats_label.set_wrap(True)
        self._avif_stats_label.set_text("AVIF non disponible \u2014 installez imagemagick" if not AVIF_SUPPORTED else "Aucune info")
        _sr = Adw.PreferencesRow(); _sr.set_child(self._avif_stats_label); g_avif.add(_sr)
        self._avif_progress_bar = Gtk.ProgressBar()
        self._avif_progress_bar.set_hexpand(True)
        self._avif_progress_bar.set_margin_start(12); self._avif_progress_bar.set_margin_end(12)
        self._avif_progress_bar.set_margin_top(4); self._avif_progress_bar.set_margin_bottom(4)
        self._avif_progress_bar.set_visible(False)
        self._avif_progress_label = Gtk.Label()
        self._avif_progress_label.add_css_class("dim-label")
        self._avif_progress_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._avif_progress_label.set_margin_start(12); self._avif_progress_label.set_visible(False)
        _pb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        _pb.set_margin_top(4); _pb.set_margin_bottom(4)
        _pb.append(self._avif_progress_bar); _pb.append(self._avif_progress_label)
        _pr = Adw.PreferencesRow(); _pr.set_child(_pb); g_avif.add(_pr)
        avif_bb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        avif_bb.set_margin_start(12); avif_bb.set_margin_end(12)
        avif_bb.set_margin_top(6); avif_bb.set_margin_bottom(6)
        self.btn_avif_convert = Gtk.Button(label="Convertir ce dossier")
        self.btn_avif_convert.add_css_class("suggested-action")
        self.btn_avif_convert.set_hexpand(True)
        self.btn_avif_convert.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_convert.connect("clicked", self._on_avif_convert)
        self.btn_avif_cancel = Gtk.Button(label="Annuler")
        self.btn_avif_cancel.set_visible(False)
        self.btn_avif_cancel.connect("clicked", lambda *_: self.avif_converter.cancel())
        self.btn_avif_purge = Gtk.Button(label="Purger")
        self.btn_avif_purge.add_css_class("destructive-action")
        self.btn_avif_purge.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_purge.connect("clicked", self._on_avif_purge)
        avif_bb.append(self.btn_avif_convert); avif_bb.append(self.btn_avif_cancel); avif_bb.append(self.btn_avif_purge)
        _abr = Adw.PreferencesRow(); _abr.set_child(avif_bb); g_avif.add(_abr)
        ag_row = Adw.ActionRow(title="Utiliser l'AVIF pour le fond", subtitle="Applique l'AVIF \u00e0 GNOME si disponible")
        self.switch_avif_gnome = Gtk.Switch()
        self.switch_avif_gnome.set_active(self.settings.avif_use_for_gnome)
        self.switch_avif_gnome.set_valign(Gtk.Align.CENTER)
        self.switch_avif_gnome.set_sensitive(AVIF_SUPPORTED)
        self.switch_avif_gnome.connect("notify::active", self._on_avif_gnome_toggle)
        ag_row.add_suffix(self.switch_avif_gnome)
        ag_row.set_activatable_widget(self.switch_avif_gnome)
        g_avif.add(ag_row)
        b_avif.append(g_avif)
        p_avif.set_child(b_avif)
        self._tab_stack.add_named(p_avif, "avif")

        self._tab_stack.set_visible_child_name("display")

    def _on_same_all_toggled(self, btn):
        """Fix 1 : Si on recoche la case, on sauvegarde virtuellement la sélection pour tous"""
        self._update_apply_btn_subtitle()
        if btn.get_active() and self.selected_image:
            for mon in self.monitors:
                self.settings.per_monitor[mon.connector] = self.selected_image

    def _on_avif_convert(self, *_) -> None:
        if self.avif_converter.is_running():
            return
        self.btn_avif_convert.set_sensitive(False)
        self.btn_avif_cancel.set_visible(True)
        self._avif_progress_bar.set_visible(True)
        self._avif_progress_label.set_visible(True)
        self._avif_progress_bar.set_fraction(0)
        self._avif_progress_label.set_text("Démarrage…")
        from .config import Config
        self.avif_converter.convert_folder(
            folder=self.folder,
            valid_extensions=Config.VALID_EXT,
            on_progress=self._on_avif_progress,
            on_done=self._on_avif_done,
        )

    def _on_avif_progress(self, converted: int, total: int, filename: str) -> None:
        frac = converted / total if total else 0
        self._avif_progress_bar.set_fraction(frac)
        self._avif_progress_label.set_text(f"{converted}/{total} — {filename}")

    def _on_avif_done(self, converted: int, total: int) -> None:
        self.btn_avif_convert.set_sensitive(True)
        self.btn_avif_cancel.set_visible(False)
        self._avif_progress_bar.set_fraction(1.0)
        self._avif_progress_label.set_text(f"✓ {converted}/{total} images converties")
        self._status(f"✓ AVIF: {converted}/{total} converties")

    def _on_avif_purge(self, *_) -> None:
        removed = self.avif_converter.purge_folder(self.folder)
        self._avif_progress_label.set_text(f"{removed} fichiers supprimés")
        self._status(f"AVIF purgés: {removed} fichiers supprimés")

    def _on_avif_gnome_toggle(self, switch, _param) -> None:
        self.settings.avif_use_for_gnome = switch.get_active()
        self._schedule_save()

    def _on_tab_toggled(self, btn, tab_id):
        if btn.get_active():
            self._tab_stack.set_visible_child_name(tab_id)

    def _on_sidebar_toggle(self, btn):
        self._split_view.set_show_sidebar(btn.get_active())

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
        try:
            current = self._daemon.get_current_wallpaper() if getattr(self, "_daemon", None) else ""
            if current:
                self._set_active_wallpapers([current])
                self._update_preview(current)
                self._update_image_info(current)
        except Exception as e:
            logger.warning("Sync daemon failed: %s", e)

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
        self._register_action("thumb_slideshow_add", self._on_thumb_slideshow_add, None)
        self._register_action("thumb_slideshow_remove", self._on_thumb_slideshow_remove, None)
        self._register_action("import", self._menu_import, "<Primary>O")
        self._register_action("remove", self._menu_remove, "Delete")
        self._register_action("refresh", lambda *_: self._load_gallery(), "<Primary>R")
        self._register_action("clear_cache", self._on_clear_cache, None)
        self._register_action("about", self._on_about, None)

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

    def _monitor_markup(self, idx: int) -> str:
        m = self.monitors[idx]
        primary = " <b>(principal)</b>" if m.primary else ""
        return (
            f"<small>{m.name}{primary}\n"
            f"{m.width} × {m.height} — pos({m.x}, {m.y})</small>"
        )

    def _status(self, msg: str):
        self.status_label.set_text(msg)
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
            dialog.set_issue_url("https://github.com/gaorfg-bit/mural/issues")
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
            win.set_issue_url("https://github.com/gaorfg-bit/mural/issues")
            win.set_copyright("© 2026 GaoR")
            win.set_license_type(Gtk.License.GPL_3_0)
            win.present()

    def _highlight_slideshow_image(self, path: str) -> bool:
        self._set_active_wallpapers([path])
        return False

    def _clear_selection(self):
        self.selected_image = None
        self._selected_path = None
        self._context_path = None
        self.btn_apply.set_sensitive(False)
        if hasattr(self, "_btn_apply_subtitle"):
            self._btn_apply_subtitle.set_visible(False)
        if hasattr(self, "_btn_apply_title"):
            self._btn_apply_title.set_text("Définir comme fond")
        self.preview.set_paintable(None)
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
        """Fix 2 : On dispatche l'image selon la case 'Même image'"""
        self.selected_image = path
        self._selected_path = path
        self.btn_apply.set_sensitive(True)
        self._update_preview(path)
        self._update_image_info(path)
        self._set_selected_child(child)
        self._preview_placeholder_label.set_visible(False)

        # Logique corrigée pour respecter la case
        if self.chk_same_all and self.chk_same_all.get_active():
            for mon in self.monitors:
                self.settings.per_monitor[mon.connector] = path
        else:
            conn = self.monitors[self.current_monitor].connector
            self.settings.per_monitor[conn] = path

        self._status(f"Sélectionné: {Path(path).name}")
        self._update_apply_btn_subtitle()

    def _update_apply_btn_subtitle(self) -> None:
        if not hasattr(self, "_btn_apply_subtitle"):
            return
        mon = self.monitors[self.current_monitor] if self.monitors else None
        mode_label = ""
        if hasattr(self, "mode_dropdown") and hasattr(self, "mode_ids"):
            try:
                mode_id = self.mode_ids[self.mode_dropdown.get_selected()]
                mode_label = dict(self.backend.MODES).get(mode_id, mode_id)
            except Exception:
                pass
        if mon:
            same_all = self.chk_same_all.get_active() if self.chk_same_all else True
            screen_label = "Tous les écrans" if (same_all or len(self.monitors) <= 1) else f"Écran {self.current_monitor + 1}"
            subtitle = f"{screen_label} · {mode_label}" if mode_label else screen_label
            self._btn_apply_subtitle.set_text(subtitle)
            self._btn_apply_subtitle.set_visible(True)
        else:
            self._btn_apply_subtitle.set_visible(False)

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

    def _on_slideshow_monitor_toggled(self, chk, connector: str) -> None:
        checked = [c for c, w in self._slideshow_monitor_checks.items() if w.get_active()]
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
        for name in self._bookmark_action_names:
            try:
                self.remove_action(name)
            except Exception:
                pass
        self._bookmark_action_names.clear()

        bookmarks = self.settings.folder_bookmarks
        menu = Gio.Menu()

        if not bookmarks:
            section = Gio.Menu()
            section.append("Aucun favori", None)
            menu.append_section(None, section)
        else:
            for i, path in enumerate(bookmarks):
                name = Path(path).name
                action_id = f"bookmark_{i}"
                action = Gio.SimpleAction.new(action_id, None)
                action.connect("activate", lambda _a, _v, p=path: self._jump_to_folder(p))
                self.add_action(action)
                self._bookmark_action_names.append(action_id)
                menu.append(name, f"win.{action_id}")

            sep_section = Gio.Menu()
            sep_section.append("Retirer le dossier actuel", "win.bookmark_remove_current")
            menu.append_section(None, sep_section)

        try:
            self.remove_action("bookmark_remove_current")
        except Exception:
            pass
        remove_action = Gio.SimpleAction.new("bookmark_remove_current", None)
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

    def _on_assign_monitor_folder(self, btn, connector: str, row: Adw.ActionRow) -> None:
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
                self._status(f"✓ Dossier assigné à l'écran {connector}")
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
                self._on_assign_monitor_folder_response(d, r, connector, row)
            ))
            dialog.show()
            self._file_dialog = dialog

    def _on_load_monitor_folder(self, btn, connector: str) -> None:
        path = self.settings.monitor_folders.get(connector, "")
        if not path or not Path(path).exists():
            self._status(f"✗ Aucun dossier assigné à cet écran — cliquez d'abord sur l'icône dossier")
            return
        for i, mon in enumerate(self.monitors):
            if mon.connector == connector:
                self.current_monitor = i
                if hasattr(self, "monitor_btns") and self.monitor_btns:
                    self.monitor_btns[i].set_active(True)
                break
        self._jump_to_folder(path)

    def _on_assign_monitor_folder_response(self, dialog, response, connector: str, row: Adw.ActionRow) -> None:
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
                self._status(f"✓ Dossier assigné à l'écran {connector}")
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
            self.lbl_slideshow_count.set_text(f"{n} image{'s' if n != 1 else ''} ({', '.join(parts)})")
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
        self.btn_folder_slideshow.handler_block_by_func(self._on_folder_slideshow_toggled)
        self.btn_folder_slideshow.set_active(active)
        self.btn_folder_slideshow.handler_unblock_by_func(self._on_folder_slideshow_toggled)

    def _init_shortcuts(self):
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._on_key_shortcut)
        self.add_controller(controller)

    def _on_key_shortcut(self, controller, keyval, keycode, state):
        return False

    def _refresh_flowbox_columns(self) -> bool:
        self._column_update_id = None
        width = self.flowbox.get_width()
        if width <= 0:
            width = self._scroll.get_width()
        if width <= 0:
            self._schedule_flowbox_column_update()
            return False

        margin = (self.flowbox.get_margin_start() + self.flowbox.get_margin_end())
        available = max(0, width - margin)
        spacing = self.flowbox.get_column_spacing()

        for target in [220, 200, 180, 160, 140, 120]:
            cols = max(1, (available + spacing) // (target + spacing))
            if cols >= 2:
                break

        thumb_w = max(100, (available - spacing * (cols - 1)) // cols)
        thumb_h = max(1, int(round(thumb_w * Config.THUMBNAIL_ASPECT)))

        if (cols != self._flowbox_columns or thumb_w != Config.THUMB_W):
            self._flowbox_columns = cols
            Config.THUMB_W = thumb_w
            Config.THUMB_H = thumb_h
            Config.THUMBNAIL_SIZE = thumb_w
            self.flowbox.set_min_children_per_line(cols)
            self.flowbox.set_max_children_per_line(cols)
            if abs(thumb_w - Config.THUMBNAIL_SIZE) > 20:
                GLib.idle_add(self._load_gallery)

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
            result = ImageLoader.load_for_preview(path, 1600, Config.PREVIEW_MAX_HEIGHT * 3)
            if result is None:
                GLib.idle_add(lambda: self.preview.set_paintable(None) or False)
                return
            raw, w, h, has_alpha = result

            def _set_texture() -> bool:
                if self.selected_image != path:
                    return False
                try:
                    from gi.repository import GLib as _GL
                    gbytes = _GL.Bytes.new(raw)
                    fmt    = Gdk.MemoryFormat.R8G8B8A8 if has_alpha else Gdk.MemoryFormat.R8G8B8
                    stride = w * (4 if has_alpha else 3)
                    texture = Gdk.MemoryTexture.new(w, h, fmt, gbytes, stride)
                    self.preview.set_paintable(texture)
                except Exception as e:
                    logger.error("set_texture [%s]: %s", Path(path).name, e)
                return False

            GLib.idle_add(_set_texture)

        threading.Thread(target=_load_texture, daemon=True).start()

    def _update_image_info(self, path: str) -> None:
        p = Path(path)
        self.lbl_name.set_markup(f"<b>{p.name}</b>")
        try:
            sz = p.stat().st_size
            if sz > 1_048_576:
                self.lbl_size.set_text(f"{sz / 1_048_576:.1f} Mo")
            else:
                self.lbl_size.set_text(f"{sz / 1024:.0f} Ko")
        except Exception:
            self.lbl_size.set_text("")

        self.lbl_dims.set_text("…")

        def _load_dims() -> None:
            dims = ImageLoader.get_dimensions(path)

            def _update() -> bool:
                if self.selected_image == path:
                    self.lbl_dims.set_text(f"{dims[0]} × {dims[1]} px" if dims else "")
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


    def _on_monitor_toggle(self, btn, index: int) -> None:
        """Fix 3 : Restaurer visuellement l'image quand on clique sur l'onglet d'un écran"""
        if not btn.get_active():
            return
        self.current_monitor = index
        self._update_apply_btn_subtitle()

        for j, b in enumerate(self.monitor_btns):
            if j == index:
                b.add_css_class("suggested-action")
            else:
                b.remove_css_class("suggested-action")

        if hasattr(self, "lbl_monitor"):
            self.lbl_monitor.set_markup(self._monitor_markup(index))

        # Restaure l'image assignée à cet écran spécifique
        conn = self.monitors[index].connector
        assigned_path = self.settings.per_monitor.get(conn)

        if assigned_path and Path(assigned_path).exists():
            self.selected_image = assigned_path
            self._selected_path = assigned_path
            self.btn_apply.set_sensitive(True)
            self._update_preview(assigned_path)
            self._update_image_info(assigned_path)
            self._preview_placeholder_label.set_visible(False)

            target_child = None
            for child, path in self._child_to_path.items():
                if path == assigned_path:
                    target_child = child
                    break

            # Déconnecte le signal pour ne pas redéclencher d'événements
            self.flowbox.handler_block_by_func(self._on_flowbox_selection_changed)
            if target_child:
                self.flowbox.select_child(target_child)
                self._set_selected_child(target_child)
            else:
                self.flowbox.unselect_all()
                self._set_selected_child(None)
            self.flowbox.handler_unblock_by_func(self._on_flowbox_selection_changed)
        else:
            self.flowbox.handler_block_by_func(self._on_flowbox_selection_changed)
            self._clear_selection()
            self.flowbox.handler_unblock_by_func(self._on_flowbox_selection_changed)

        # Charger le dossier s'il a été assigné spécialement pour cet écran
        assigned_folder = self.settings.monitor_folders.get(conn, "")
        if assigned_folder and Path(assigned_folder).exists():
            if str(self.folder) != assigned_folder:
                self._jump_to_folder(assigned_folder)
                self._status(f"Écran {index + 1} — dossier: {Path(assigned_folder).name}")
        else:
            self._status(f"Écran {index + 1} sélectionné")


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
        """Fix 4 : Toujours forcer le Universal Canvas si on a plus d'un écran."""
        if not self.selected_image or not Path(self.selected_image).exists():
            dialog = Gtk.AlertDialog()
            dialog.set_message("Aucune image sélectionnée")
            dialog.set_buttons(["OK"])
            dialog.choose(self, None, lambda *_: None)
            return

        mode = self.mode_ids[self.mode_dropdown.get_selected()]
        lock = self.chk_lock.get_active()
        same_all = self.chk_same_all.get_active() if self.chk_same_all else True

        if not same_all and len(self.monitors) > 1:
            conn = self.monitors[self.current_monitor].connector
            self.settings.per_monitor[conn] = self.selected_image
            missing = [
                mon.name for mon in self.monitors
                if not self.settings.per_monitor.get(mon.connector)
                or not Path(self.settings.per_monitor[mon.connector]).exists()
            ]
            if missing:
                self._status(f"⚠ Images manquantes : {', '.join(missing)}")
                return

        self.btn_apply.set_sensitive(False)
        self._status("Application en cours…")
        image = self.selected_image
        monitors_snapshot = list(self.monitors)
        per_monitor_snapshot = dict(self.settings.per_monitor)

        def _apply_worker():
            ok = False
            active_paths = []
            status_msg = ""

            # NOUVELLE LOGIQUE: On passe par le backend multi-écrans quoi qu'il arrive (même pour "Même image")
            if len(monitors_snapshot) > 1:
                assignments = {}
                if same_all:
                    assignments = {mon.connector: image for mon in monitors_snapshot}
                else:
                    assignments = {mon.connector: per_monitor_snapshot.get(mon.connector, image) for mon in monitors_snapshot}

                results = self.backend.apply_per_monitor(
                    assignments, "spanned", lock, monitors=monitors_snapshot
                )
                ok_count = sum(1 for v in results.values() if v)
                total = len(results)
                ok = ok_count == total
                active_paths = list(assignments.values())
                status_msg = (
                    f"✓ Appliqué sur {ok_count}/{total} écrans"
                    if ok else f"⚠ Canvas partiel: {ok_count}/{total} écrans"
                )
            else:
                # Si l'utilisateur n'a physiquement qu'un seul écran
                if getattr(self, "_daemon", None) and self._daemon.available:
                    ok = self._daemon.set_wallpaper(image)
                    if not ok:
                        GLib.idle_add(self._status, "✗ daemon — fallback local")
                        ok = self.backend.apply_single(image, mode=mode, lock=lock)
                else:
                    ok = self.backend.apply_single(image, mode=mode, lock=lock)
                if ok:
                    active_paths = [image]
                    for mon in monitors_snapshot:
                        self.settings.per_monitor[mon.connector] = image
                    status_msg = f"✓ Appliqué: {Path(image).name}"
                else:
                    status_msg = "✗ Échec de l'application"

            GLib.idle_add(self._on_apply_done, ok, image, active_paths, mode, lock, status_msg)

        threading.Thread(target=_apply_worker, daemon=True).start()

    def _on_apply_done(self, ok: bool, image: str, active_paths: list,
                       mode: str, lock: bool, status_msg: str) -> bool:
        self.btn_apply.set_sensitive(True)
        self._status(status_msg)
        if ok:
            self._set_active_wallpapers(active_paths)
            self._btn_apply_title.set_text("✓ Appliqué !")
            GLib.timeout_add(
                2500,
                lambda: (self._btn_apply_title.set_text("Définir comme fond"), False)[-1]
            )
        self.settings.mode = mode
        self.settings.lock_screen = lock
        self._schedule_save()
        return False

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
            GLib.idle_add(self._status, "Permission refusee")
            GLib.idle_add(self._set_progress_visibility, False)
            return

        total = len(files)
        GLib.idle_add(self.lbl_count.set_text, f"{total} images")
        if total == 0:
            GLib.idle_add(self._status, "Aucune image trouvee")
            GLib.idle_add(self._set_progress_visibility, False)
            return

        import time
        BATCH_SIZE = 8
        batch = []

        for i, fpath in enumerate(files):
            if stop_event.is_set() or generation != self.gallery_generation:
                return
            avif = get_cached_avif(str(fpath))
            src = str(avif) if avif else str(fpath)
            thumb_path = Thumbnailer.generate(src, Config.THUMB_W, Config.THUMB_H, Config.THUMB_DIR)
            batch.append((fpath, thumb_path))

            if len(batch) >= BATCH_SIZE or i == total - 1:
                if stop_event.is_set() or generation != self.gallery_generation:
                    return
                while True:
                    with self._pending_batches_lock:
                        if self._pending_batches < 3:
                            self._pending_batches += 1
                            break
                    if stop_event.is_set():
                        return
                    time.sleep(0.02)
                GLib.idle_add(self._add_thumb_batch_counted, list(batch))
                batch = []
                GLib.idle_add(self._gallery_progressbar.set_fraction, (i + 1) / total)

        if not (stop_event.is_set() or generation != self.gallery_generation):
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
        for fpath, thumb_path in items:
            if self._stop_event.is_set():
                break
            self._add_thumb(fpath, thumb_path)
        self._refresh_active_indicators()
        with self._pending_batches_lock:
            self._pending_batches = max(0, self._pending_batches - 1)
        return False

    def _add_thumb(self, fpath: Path, thumb_path: Optional[Path]):
        if self._stop_event.is_set():
            return False

        source_path = str(thumb_path) if (thumb_path and thumb_path.exists()) else str(fpath)

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(Config.THUMB_W, Config.THUMB_H)
        try:
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                source_path, Config.THUMB_W, Config.THUMB_H,
                preserve_aspect_ratio=False
            )
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            pixbuf = None
            picture.set_paintable(texture)
        except Exception:
            picture = Gtk.Image.new_from_icon_name("image-missing")
            picture.set_size_request(Config.THUMB_W, Config.THUMB_H)

        indicator = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        indicator.add_css_class("thumb-indicator")
        indicator.add_css_class("thumb-indicator-active")
        indicator.set_valign(Gtk.Align.START)
        indicator.set_halign(Gtk.Align.END)
        indicator.set_margin_top(4)
        indicator.set_margin_end(4)
        indicator.set_visible(False)

        slideshow_indicator = Gtk.Image.new_from_icon_name("starred-symbolic")
        slideshow_indicator.add_css_class("slideshow-indicator")
        slideshow_indicator.set_valign(Gtk.Align.END)
        slideshow_indicator.set_halign(Gtk.Align.START)
        slideshow_indicator.set_margin_bottom(4)
        slideshow_indicator.set_margin_start(4)
        slideshow_indicator.set_visible(self.settings.is_in_slideshow(str(fpath)))

        overlay = Gtk.Overlay()
        overlay.set_child(picture)
        overlay.add_overlay(indicator)
        overlay.add_overlay(slideshow_indicator)
        overlay.set_size_request(Config.THUMB_W, Config.THUMB_H)
        overlay.update_property([Gtk.AccessibleProperty.LABEL], [Path(fpath).name])

        gesture = Gtk.GestureClick()
        gesture.set_button(3)
        gesture.connect("pressed", self._on_thumb_right_click, str(fpath))
        overlay.add_controller(gesture)

        self._thumb_views[str(fpath)] = (overlay, indicator, slideshow_indicator)
        self.flowbox.append(overlay)
        fb_child = self.flowbox.get_last_child()
        if fb_child is not None:
            self._child_to_path[fb_child] = str(fpath)
        return False

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
        logger.info("Monitors changed: +%d -%d at pos %d", added, removed, position)
        self.monitors = MonitorDetector.detect()
        n = len(self.monitors)
        self._title_widget.set_subtitle(
            f"{n} écran{'s' if n > 1 else ''} détecté{'s' if n > 1 else ''}"
        )
        if self.current_monitor >= len(self.monitors):
            self.current_monitor = 0
        self._status(f"Écrans mis à jour: {n} détecté{'s' if n > 1 else ''}")

    @property
    def current_mode(self) -> str:
        return self.mode_ids[self.mode_dropdown.get_selected()]

    @property
    def apply_to_lockscreen(self) -> bool:
        return self.chk_lock.get_active()

    @property
    def active_monitors(self) -> list:
        return list(self.settings.slideshow_monitors)

    def _schedule_save(self) -> None:
        if self._save_timeout_id is not None:
            GLib.source_remove(self._save_timeout_id)
        self._save_timeout_id = GLib.timeout_add(500, self._do_save)

    def _do_save(self) -> bool:
        self._save_timeout_id = None
        self.config.save(self.settings)
        logger.debug("Config sauvegardée")
        return False

    def _on_close_request(self, *_) -> bool:
        self.slideshow.stop()
        self.settings.window_maximized = self.is_maximized()
        if not self.is_maximized():
            w, h = self.get_width(), self.get_height()
            if w > 100 and h > 100:
                self.settings.window_width = w
                self.settings.window_height = h
        self.config.save(self.settings)
        return False
