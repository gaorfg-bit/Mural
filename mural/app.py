from __future__ import annotations

import gc
import logging
import gettext
import locale
import os
import sys
import threading
import traceback
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
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango

from backend import GnomeBackend
from config import Config
from daemon import MuralDaemonProxy
from monitors import MonitorDetector
from slideshow import SlideshowManager
from thumbnails import ImageLoader, Thumbnailer
from avif_cache import get_cached_avif, AVIF_SUPPORTED

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

logger = logging.getLogger("wallpaper")
logger.setLevel(logging.DEBUG)

# --- Multilingual Configuration ---
APP_NAME = "mural"
LOCALE_DIR = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'locale')

try:
    locale.setlocale(locale.LC_ALL, '')
except locale.Error:
    pass

gettext.bindtextdomain(APP_NAME, LOCALE_DIR)
gettext.textdomain(APP_NAME)
_ = gettext.gettext

def _log_uncaught_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_uncaught_exception
ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 268_435_456

APP_VERSION = "1.1.1"

WHATS_NEW = {
    "1.1.1": [
        ("🖼️", "Better thumbnails", "Thumbnails are now sharper and larger. Clear cache once to regenerate them."),
        ("⌨️", "Keyboard shortcuts", "Press Enter to apply the selected wallpaper. Press Space for a fullscreen preview."),
        ("🖱️", "Sort from right-click", "Right-click any thumbnail to sort the gallery by name or by date."),
        ("📌", "Pinnable sidebar", "Click the sidebar button to pin it open permanently. Hover still works when unpinned."),
        ("🧹", "Memory improvements", "Texture cache is now cleared when switching folders, reducing RAM usage."),
    ],
    "1.1": [
        ("🖥️", "Independent monitors", "The \"Same image on all\" option is now unchecked by default. Each monitor keeps its own wallpaper independently."),
        ("🔒", "No more overwriting", "Changing the wallpaper on one monitor no longer overwrites the other monitors' existing wallpapers."),
        ("🔗", "Dock icon fixed", "Mural now shows its proper icon in the taskbar instead of the generic terminal script icon."),
    ]
}

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

class MuralWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application):
        super().__init__(application=application)

        self.config = Config()
        self.settings = self.config.load()
        self.backend = GnomeBackend()
        self.slideshow = SlideshowManager(self)
        self._daemon = MuralDaemonProxy()
        from avif_cache import FolderConverter
        self.avif_converter = FolderConverter()
        self.monitors = MonitorDetector.detect()
        self.current_monitor = 0
        self.selected_image: Optional[str] = None
        self._texture_cache: Dict[str, Gdk.Texture] = {}
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
        self._sort_mode: str = "name"  # "name" or "date"
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
        self._sidebar_pinned = False
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
        # Show "What's new" popup if version changed
        if self.settings.last_seen_version != APP_VERSION:
            GLib.timeout_add(800, self._show_whats_new)
        if self._daemon.available:
            logger.info("Slideshow delegated to daemon")
            # self._sync_ui_from_daemon()
        elif self.settings.slideshow_enabled:
            self.slideshow.start()

    def _build_ui(self):
        hb = Gtk.HeaderBar()
        hb.set_show_title_buttons(True)
        self.set_decorated(True)
        n_mon = len(self.monitors)
        
        # 1. SINGLE LINE TITLE
        self._title_widget = Adw.WindowTitle(
            title=f"Mural — {n_mon} " + (_("screens") if n_mon > 1 else _("screen"))
        )
        hb.set_title_widget(self._title_widget)
        self._ensure_menu_actions()
        self._ensure_thumb_menu()

        btn_folder = Gtk.Button()
        btn_folder.set_child(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
        btn_folder.set_tooltip_text(_("Choose a folder"))
        btn_folder.connect("clicked", self._on_choose_folder)
        self._search_toggle = Gtk.ToggleButton()
        self._search_toggle.set_icon_name("system-search-symbolic")
        self._search_toggle.set_tooltip_text(_("Search (Ctrl+F)"))
        hb.pack_start(btn_folder)
        hb.pack_start(self._search_toggle)

        # 2. FLAT BUTTON ON SINGLE LINE
        self.btn_apply = Gtk.Button(label=_("Set as background"))
        self.btn_apply.set_sensitive(False)
        self.btn_apply.set_valign(Gtk.Align.CENTER) # Force strict vertical alignment
        self.btn_apply.connect("clicked", self._on_apply)
        hb.pack_end(self.btn_apply)

        app_menu = Gio.Menu()
        app_menu.append(_("Refresh gallery"), "win.refresh")
        app_menu.append(_("Clear cache"), "win.clear_cache")
        _sep = Gio.Menu()
        _sep.append("🆕 What's new", "win.whats_new")
        _sep.append(_("About Mural"), "win.about")
        app_menu.append_section(None, _sep)
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_menu_model(app_menu)
        hb.pack_end(menu_btn)

        self._sidebar_toggle = Gtk.ToggleButton()
        self._sidebar_toggle.set_icon_name("sidebar-show-right-symbolic")
        self._sidebar_toggle.set_active(False)
        self._sidebar_toggle.set_tooltip_text(_("Sidebar (Ctrl+B)"))
        self._sidebar_toggle.connect("toggled", self._on_sidebar_toggle)
        hb.pack_end(self._sidebar_toggle)

        self._split_view = Adw.OverlaySplitView()
        self._split_view.set_sidebar_position(Gtk.PackType.END)
        self._split_view.set_sidebar_width_fraction(0.20)
        self._split_view.set_min_sidebar_width(140)
        self._split_view.set_max_sidebar_width(300)
        self._split_view.set_show_sidebar(False)
        self._split_view.set_enable_show_gesture(True)
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
        main_vbox.set_size_request(100, -1)

        search_bar = Gtk.SearchBar()
        search_bar.set_search_mode(False)
        search_bar.set_show_close_button(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search\u2026"))
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
        self._preview_placeholder_label = Gtk.Label(label=_("Select an image"))
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
        tab_bar.set_margin_start(4)
        tab_bar.set_margin_end(4)
        tab_bar.set_margin_top(8)
        tab_bar.set_margin_bottom(8)
        self._tab_btns = {}
        first_tab_btn = None
        for tab_id, tab_label in [("display", _("Display")), ("slideshow", _("Slideshow")), ("folders", _("Folders")), ("avif", "AVIF")]:
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
            g_sc = Adw.PreferencesGroup(title=_("Monitors"))
            mb_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            mb_box.set_margin_start(12); mb_box.set_margin_end(12)
            mb_box.set_margin_top(8); mb_box.set_margin_bottom(8)
            mb_btns = Gtk.Box(spacing=4)
            mb_btns.set_halign(Gtk.Align.CENTER)
            self.monitor_btns = []
            first_mb = None
            for i, mon in enumerate(self.monitors):
                mlbl = f"{_('Monitor')} {i+1}" + (" \u2605" if mon.primary else "")
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

            # SIGNAL ADDED HERE TO SYNCHRONIZE CHECKBOX
            self.chk_same_all = Gtk.CheckButton(label=_("Same image on all"))
            self.chk_same_all.set_active(self.settings.same_image_on_all)
            self.chk_same_all.connect("toggled", self._on_same_all_toggled)
            mb_box.append(self.chk_same_all)

            mr = Adw.PreferencesRow(); mr.set_child(mb_box); g_sc.add(mr)
            b_disp.append(g_sc)
        else:
            self.monitor_btns = []
            self.chk_same_all = None
        g_opt = Adw.PreferencesGroup(title=_("Options"))
        mi_row = Adw.PreferencesRow()
        mi_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mi_box.set_margin_start(12); mi_box.set_margin_end(12)
        mi_box.set_margin_top(8); mi_box.set_margin_bottom(8)
        mi_lbl = Gtk.Label(label=_("Display mode"))
        mi_lbl.set_xalign(0); mi_lbl.set_hexpand(True)
        mi_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        mi_box.append(mi_lbl)
        self.mode_ids = [mid for mid, _ in GnomeBackend.MODES]
        self.mode_dropdown = Gtk.DropDown.new_from_strings([_(ml) for mode_id, ml in GnomeBackend.MODES])
        self.mode_dropdown.set_selected(
            self.mode_ids.index(self.settings.mode) if self.settings.mode in self.mode_ids else 0
        )
        self.mode_dropdown.set_valign(Gtk.Align.CENTER)
        self.mode_dropdown.set_size_request(150, -1)
        mi_box.append(self.mode_dropdown)
        mi_row.set_child(mi_box); g_opt.add(mi_row)
        lk_row = Adw.ActionRow(title=_("Lock screen"), subtitle=_("Apply to lock screen as well"))
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
        g_ss = Adw.PreferencesGroup(title=_("Slideshow"))
        ss_row = Adw.ActionRow(title=_("Automatic change"), subtitle=_("Change background every X minutes"))
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
        iv_lbl = Gtk.Label(label=_("Interval (min)"))
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
        rnd_row = Adw.ActionRow(title=_("Random order"))
        self.switch_random = Gtk.Switch()
        self.switch_random.set_active(self.settings.slideshow_random)
        self.switch_random.set_valign(Gtk.Align.CENTER)
        self.switch_random.connect("notify::active", self._on_random_toggle)
        rnd_row.add_suffix(self.switch_random)
        rnd_row.set_activatable_widget(self.switch_random)
        g_ss.add(rnd_row)
        b_ss.append(g_ss)
        if len(self.monitors) > 1:
            g_ssm = Adw.PreferencesGroup(title=_("Apply on"))
            self._slideshow_monitor_checks = {}
            for mon in self.monitors:
                chk = Gtk.CheckButton(label=f"{_('Monitor')} {mon.name[:24]}")
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
        btn_next = Gtk.Button(label=_("\u23ed  Next image now"))
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

        # 1. Current Folder (Ultra-Clean Version)
        g_cur = Adw.PreferencesGroup(title=_("Navigation"))
        self.row_current_folder = Adw.ActionRow(title=_("Current folder"), subtitle=_("Loading..."))
        self.row_current_folder.set_title_lines(1)
        self.row_current_folder.set_subtitle_lines(3) # Allows path to be on multiple lines if needed

        btn_bm = Gtk.Button()
        btn_bm.set_icon_name("bookmark-new-symbolic")
        btn_bm.set_valign(Gtk.Align.CENTER)
        btn_bm.add_css_class("flat") # Removes the big gray background from the button
        btn_bm.set_tooltip_text(_("Add to bookmarks"))
        btn_bm.connect("clicked", self._on_add_bookmark)

        self.btn_bookmarks = Gtk.MenuButton()
        self.btn_bookmarks.set_icon_name("user-bookmarks-symbolic")
        self.btn_bookmarks.set_valign(Gtk.Align.CENTER)
        self.btn_bookmarks.add_css_class("flat")
        self.btn_bookmarks.set_tooltip_text(_("My bookmarks"))
        self._rebuild_bookmarks_menu()

        self.row_current_folder.add_suffix(btn_bm)
        self.row_current_folder.add_suffix(self.btn_bookmarks)
        g_cur.add(self.row_current_folder)
        b_fold.append(g_cur)

        # 2. Monitor Shortcuts (Lite Version)
        if len(self.monitors) > 1:
            g_fm = Adw.PreferencesGroup(title=_("Monitor Shortcuts"))
            for i, mon in enumerate(self.monitors):
                fr = Adw.ActionRow(
                    title=f"{_('Monitor')} {i+1}",
                    subtitle=self.settings.monitor_folders.get(mon.connector, _("Unassigned"))[-40:]
                )
                ba = Gtk.Button()
                ba.set_icon_name("folder-symbolic")
                ba.set_valign(Gtk.Align.CENTER)
                ba.add_css_class("flat")
                ba.set_tooltip_text(_("Assign default folder"))
                ba.connect("clicked", self._on_assign_monitor_folder, mon.connector, fr)

                bl = Gtk.Button()
                bl.set_icon_name("go-jump-symbolic")
                bl.set_valign(Gtk.Align.CENTER)
                bl.add_css_class("flat")
                bl.set_tooltip_text(_("Open this folder"))
                bl.connect("clicked", self._on_load_monitor_folder, mon.connector)

                fr.add_suffix(ba)
                fr.add_suffix(bl)
                g_fm.add(fr)
            b_fold.append(g_fm)
        p_fold.set_child(b_fold)
        self._tab_stack.add_named(p_fold, "folders")

        p_avif = Gtk.ScrolledWindow()
        p_avif.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        p_avif.set_vexpand(True)
        b_avif = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        b_avif.set_margin_start(12); b_avif.set_margin_end(12)
        b_avif.set_margin_top(12); b_avif.set_margin_bottom(16)
        g_avif = Adw.PreferencesGroup(title=_("AVIF Cache"))
        self._avif_stats_label = Gtk.Label()
        self._avif_stats_label.set_xalign(0)
        self._avif_stats_label.add_css_class("dim-label")
        self._avif_stats_label.set_margin_start(12); self._avif_stats_label.set_margin_top(6)
        self._avif_stats_label.set_margin_bottom(2); self._avif_stats_label.set_wrap(True)
        self._avif_stats_label.set_text(_("AVIF unavailable — install imagemagick") if not AVIF_SUPPORTED else _("No info"))
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
        self.btn_avif_convert = Gtk.Button(label=_("Convert this folder"))
        self.btn_avif_convert.add_css_class("suggested-action")
        self.btn_avif_convert.set_hexpand(True)
        self.btn_avif_convert.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_convert.connect("clicked", self._on_avif_convert)
        self.btn_avif_cancel = Gtk.Button(label=_("Cancel"))
        self.btn_avif_cancel.set_visible(False)
        self.btn_avif_cancel.connect("clicked", lambda *_: self.avif_converter.cancel())
        self.btn_avif_purge = Gtk.Button(label=_("Purge"))
        self.btn_avif_purge.add_css_class("destructive-action")
        self.btn_avif_purge.set_sensitive(AVIF_SUPPORTED)
        self.btn_avif_purge.connect("clicked", self._on_avif_purge)
        avif_bb.append(self.btn_avif_convert); avif_bb.append(self.btn_avif_cancel); avif_bb.append(self.btn_avif_purge)
        _abr = Adw.PreferencesRow(); _abr.set_child(avif_bb); g_avif.add(_abr)
        ag_row = Adw.ActionRow(title=_("Use AVIF for background"), subtitle=_("Applies AVIF to GNOME if available"))
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

        # --- HOVER + PIN ---
        motion_ctrl = Gtk.EventControllerMotion.new()
        motion_ctrl.connect("motion", self._on_pointer_motion)
        self.add_controller(motion_ctrl)

    def _on_same_all_toggled(self, btn):
        active = btn.get_active()
        self.settings.same_image_on_all = active
        self._schedule_save()
        self._update_apply_btn_subtitle()
        if active and self.selected_image:
            for mon in self.monitors:
                self.settings.per_monitor[mon.connector] = self.selected_image

    def _on_avif_convert(self, *_ignored) -> None:
        if self.avif_converter.is_running():
            return
        self.btn_avif_convert.set_sensitive(False)
        self.btn_avif_cancel.set_visible(True)
        self._avif_progress_bar.set_visible(True)
        self._avif_progress_label.set_visible(True)
        self._avif_progress_bar.set_fraction(0)
        self._avif_progress_label.set_text(_("Starting…"))
        from config import Config
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
        self._avif_progress_label.set_text(f"✓ {converted}/{total} " + _("images converted"))
        self._status(f"✓ AVIF: {converted}/{total} " + _("images converted"))

    def _on_avif_purge(self, *_ignored) -> None:
        removed = self.avif_converter.purge_folder(self.folder)
        self._avif_progress_label.set_text(f"{removed} " + _("files deleted"))
        self._status(f"AVIF: {removed} " + _("files deleted"))

    def _on_avif_gnome_toggle(self, switch, _param) -> None:
        self.settings.avif_use_for_gnome = switch.get_active()
        self._schedule_save()

    def _on_tab_toggled(self, btn, tab_id):
        if btn.get_active():
            self._tab_stack.set_visible_child_name(tab_id)

    def _on_sidebar_toggle(self, btn):
        """Pin button: locks sidebar open permanently. Hover still works when unpinned."""
        self._sidebar_pinned = btn.get_active()
        self._split_view.set_show_sidebar(self._sidebar_pinned)
        # Update icon to reflect pinned state
        btn.set_icon_name("view-pin-symbolic" if self._sidebar_pinned else "sidebar-show-right-symbolic")

    def _on_pointer_motion(self, controller, x, y) -> None:
        # If sidebar is pinned by user, hover does nothing
        if getattr(self, "_sidebar_pinned", False):
            return
        if self._split_view.get_collapsed():
            return

        win_width = self.get_width()
        is_open = self._split_view.get_show_sidebar()

        # Open on hover near right edge
        if not is_open and x >= win_width - 10:
            self._split_view.set_show_sidebar(True)

        # Close when mouse leaves sidebar area
        elif is_open:
            sidebar_w = self._sidebar.get_width()
            if x < win_width - sidebar_w - 20:
                self._split_view.set_show_sidebar(False)

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
        self._primary_menu.append(_("Import…"), "win.import")
        self._primary_menu.append(_("Remove"), "win.remove")

        noop = Gio.SimpleAction.new("noop", None)
        self.get_application().add_action(noop)

        self._register_action("thumb_set", self._on_thumb_set, None)
        self._register_action("thumb_reveal", self._on_thumb_reveal, None)
        self._register_action("thumb_copy_path", self._on_thumb_copy_path, None)
        self._register_action("thumb_slideshow_add", self._on_thumb_slideshow_add, None)
        self._register_action("thumb_slideshow_remove", self._on_thumb_slideshow_remove, None)
        self._register_action("thumb_delete_disk", self._on_thumb_delete_disk, None)
        self._register_action("import", self._menu_import, "<Primary>O")
        self._register_action("remove", self._menu_remove, "Delete")
        self._register_action("refresh", lambda *_: self._load_gallery(), "<Primary>R")
        self._register_action("clear_cache", self._on_clear_cache, None)
        self._register_action("about", self._on_about, None)
        self._register_action("whats_new", self._show_whats_new, None)
        self._register_action("sort_by_name", lambda *_: self._set_sort("name"), None)
        self._register_action("sort_by_date", lambda *_: self._set_sort("date"), None)

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
        menu.append(_("Set as background"), "win.thumb_set")
        menu.append(_("Open in Files"), "win.thumb_reveal")
        menu.append(_("Copy path"), "win.thumb_copy_path")

        # Sort section
        sec_sort = Gio.Menu()
        sec_sort.append("↑ " + _("Sort by name"), "win.sort_by_name")
        sec_sort.append("🕐 " + _("Sort by date"), "win.sort_by_date")
        menu.append_section(None, sec_sort)

        # Manager section (like "Remove from list")
        sec_slideshow = Gio.Menu()
        sec_slideshow.append("⭐ " + _("Add to slideshow"), "win.thumb_slideshow_add")
        sec_slideshow.append("❌ " + _("Remove from slideshow"), "win.thumb_slideshow_remove")
        menu.append_section(None, sec_slideshow)
        
        # Hard drive section (like "Delete from list AND disk")
        sec_danger = Gio.Menu()
        sec_danger.append("🗑️ " + _("Delete from disk"), "win.thumb_delete_disk")
        menu.append_section(None, sec_danger)
        
        self.thumb_menu = Gtk.PopoverMenu.new_from_model(menu)

    def _menu_import(self, action, param):
        self._on_choose_folder(None)

    def _menu_remove(self, action, param):
        self._status(_("Action {} not implemented").format(action.get_name()))

    def _monitor_markup(self, idx: int) -> str:
        m = self.monitors[idx]
        primary = f" <b>({_('primary')})</b>" if m.primary else ""
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

    def _show_whats_new(self, *args) -> None:
        dialog = Adw.MessageDialog(transient_for=self, heading=f"What's new in Mural {APP_VERSION}")
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(380, 320)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(4)
        box.set_margin_bottom(8)
        box.set_margin_start(4)
        box.set_margin_end(4)

        for version in sorted(WHATS_NEW.keys(), reverse=True):
            changes = WHATS_NEW[version]
            if not changes:
                continue

            # Version header
            ver_lbl = Gtk.Label()
            is_current = version == APP_VERSION
            ver_lbl.set_markup(f"<b>v{version}</b>" + (" — <small><i>current</i></small>" if is_current else ""))
            ver_lbl.set_xalign(0)
            ver_lbl.set_margin_top(4 if version == APP_VERSION else 8)
            ver_lbl.set_margin_bottom(4)
            box.append(ver_lbl)

            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.set_margin_bottom(6)
            box.append(sep)

            for icon, title, desc in changes:
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
                row.set_valign(Gtk.Align.START)
                row.set_margin_bottom(6)

                emoji_lbl = Gtk.Label(label=icon)
                emoji_lbl.set_valign(Gtk.Align.START)
                emoji_lbl.set_margin_top(2)
                row.append(emoji_lbl)

                text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                title_lbl = Gtk.Label()
                title_lbl.set_markup(f"<b>{title}</b>")
                title_lbl.set_xalign(0)
                desc_lbl = Gtk.Label(label=desc)
                desc_lbl.set_xalign(0)
                desc_lbl.set_wrap(True)
                desc_lbl.set_max_width_chars(42)
                desc_lbl.add_css_class("dim-label")
                text_box.append(title_lbl)
                text_box.append(desc_lbl)
                row.append(text_box)
                box.append(row)

        scroll.set_child(box)
        dialog.set_extra_child(scroll)
        dialog.connect("response", lambda d, r: None)
        dialog.present()

        # Mark as seen
        self.settings.last_seen_version = APP_VERSION
        self._schedule_save()

    def _on_about(self, *args) -> None:
        try:
            dialog = Adw.AboutDialog()
            dialog.set_application_name("Mural")
            dialog.set_version(APP_VERSION)
            dialog.set_developer_name("GaoR")
            dialog.set_developers(["GaoR https://github.com/gaorfg-bit"])
            dialog.set_application_icon("mural-app")
            dialog.set_website("https://github.com/gaorfg-bit/mural")
            dialog.set_issue_url("https://github.com/gaorfg-bit/mural/issues")
            dialog.set_copyright("© 2026 GaoR")
            dialog.set_license_type(Gtk.License.GPL_3_0)
            dialog.present(self)
        except AttributeError:
            win = Adw.AboutWindow(transient_for=self)
            win.set_application_name("Mural")
            win.set_version(APP_VERSION)
            win.set_developer_name("GaoR")
            win.set_developers(["GaoR"])
            win.set_application_icon("io.github.gaorfg_bit.Mural")
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
            self._btn_apply_title.set_text(_("Set as background"))
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
        """Fix 2: Dispatch image according to 'Same image' checkbox"""
        self.selected_image = path
        self._selected_path = path
        self.btn_apply.set_sensitive(True)
        self._update_preview(path)
        self._update_image_info(path)
        self._set_selected_child(child)
        self._preview_placeholder_label.set_visible(False)

        # Corrected logic to respect checkbox
        if self.chk_same_all and self.chk_same_all.get_active():
            for mon in self.monitors:
                self.settings.per_monitor[mon.connector] = path
        else:
            conn = self.monitors[self.current_monitor].connector
            self.settings.per_monitor[conn] = path

        self._status(f"{_('Selected')}: {Path(path).name}")
        self._update_apply_btn_subtitle()

    def _update_apply_btn_subtitle(self) -> None:
        if not self.monitors:
            self.btn_apply.set_label(_("Set as background"))
            return

        same_all = self.chk_same_all.get_active() if self.chk_same_all else True
        
        if same_all or len(self.monitors) <= 1:
            self.btn_apply.set_label(_("Apply (All)"))
        else:
            self.btn_apply.set_label(_("Apply (Monitor {})").format(self.current_monitor + 1))

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
            self._status("⏱ " + _("Slideshow enabled"))
        else:
            self.slideshow.stop()
            self._status(_("Slideshow disabled"))

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

    def _set_sort(self, mode: str) -> None:
        self._sort_mode = mode
        self._texture_cache.clear()
        self._load_gallery()
        label = _("Name") if mode == "name" else _("Date")
        self._status(f"↕ " + _("Sort by") + f": {label}")

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
                self._status("✗ " + _("Daemon failed — local fallback"))
                ok = self.backend.apply_single(path, mode, lock)
        else:
            ok = self.backend.apply_single(path, mode, lock)
        if ok:
            self._status(f"✓ {_('Applied')}: {Path(path).name} ({mode})")
        else:
            self._status("✗ " + _("Application error"))

    def _on_thumb_reveal(self, action, param):
        path = self._context_path
        if not path or not Path(path).exists():
            return
        uri = Path(path).parent.as_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception:
            self._status("✗ " + _("Cannot open"))

    def _on_thumb_copy_path(self, action, param):
        path = self._context_path
        if not path:
            return
        try:
            display = Gdk.Display.get_default()
            if display:
                clipboard = display.get_clipboard()
                clipboard.set_text(path)
                self._status(_("Path copied"))
        except Exception:
            self._status("✗ " + _("Copy failed"))

    def _on_thumb_slideshow_add(self, action, param) -> None:
        path = self._context_path
        if not path:
            return
        self.settings.add_to_slideshow(path)
        self._schedule_save()
        self._refresh_active_indicators()
        self._update_slideshow_count_label()
        self._status(f"✓ {_('Added to slideshow')}: {Path(path).name}")

    def _on_thumb_slideshow_remove(self, action, param) -> None:
        path = self._context_path
        if not path:
            return
        self.settings.remove_from_slideshow(path)
        self._schedule_save()
        self._refresh_active_indicators()
        self._update_slideshow_count_label()
        self._status(f"{_('Removed from slideshow')}: {Path(path).name}")

    def _on_thumb_delete_disk(self, action, param) -> None:
        path = self._context_path
        if not path or not Path(path).exists():
            return
            
        # Security confirmation window
        dialog = Gtk.AlertDialog()
        dialog.set_message(_("Permanent deletion"))
        dialog.set_detail(_("Do you really want to delete this image from your hard drive?") + f"\n\n{Path(path).name}")
        dialog.set_buttons([_("Cancel"), _("Delete")])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)

        def _on_response(dlg, res):
            try:
                response = dlg.choose_finish(res)
            except GLib.Error:
                return
            if response == 1: # If user clicks "Delete"
                try:
                    Path(path).unlink() # Physically delete the file
                    self._status(f"🗑️ {_('File deleted')}: {Path(path).name}")
                    
                    # Clean Mural internal database
                    self.settings.remove_from_slideshow(path)
                    for conn, p in list(self.settings.per_monitor.items()):
                        if p == path:
                            del self.settings.per_monitor[conn]
                    self._schedule_save()
                    
                    # Reload gallery to remove thumbnail
                    self._load_gallery()
                except Exception as e:
                    self._status(f"✗ {_('Deletion error')}: {e}")

        dialog.choose(self, None, _on_response)

    def _on_add_bookmark(self, *_ignored) -> None:
        folder = str(self.folder)
        if folder not in self.settings.folder_bookmarks:
            self.settings.folder_bookmarks.append(folder)
            self._schedule_save()
            self._rebuild_bookmarks_menu()
            self._status(f"✓ {_('Bookmark added')}: {self.folder.name}")
        else:
            self._status(_("Folder already in bookmarks"))

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
            section.append(_("No bookmarks"), None)
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
            sep_section.append(_("Remove current folder"), "win.bookmark_remove_current")
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
            self._status(f"✗ {_('Folder not found')}: {folder.name}")
            return
        self._set_selected_folder(Gio.File.new_for_path(str(folder)))

    def _on_remove_bookmark(self, *_ignored) -> None:
        folder = str(self.folder)
        if folder in self.settings.folder_bookmarks:
            self.settings.folder_bookmarks.remove(folder)
            self._schedule_save()
            self._rebuild_bookmarks_menu()
            self._status(f"{_('Bookmark removed')}: {self.folder.name}")
        else:
            self._status(_("This folder is not in bookmarks"))

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
                self._status(f"✓ {_('Folder assigned to monitor')} {connector}")
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title(_("Choose folder for this monitor"))
            dialog.select_folder(self, None, _on_folder_chosen)
        else:
            dialog = Gtk.FileChooserNative(
                title=_("Choose a folder"),
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
            self._status("✗ " + _("No folder assigned to this monitor — click the folder icon first"))
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
                self._status(f"✓ {_('Folder assigned to monitor')} {connector}")
        dialog.destroy()
        self._file_dialog = None

    def _update_slideshow_count_label(self) -> None:
        if not hasattr(self, "lbl_slideshow_count"):
            return
        playlist = self.settings.resolve_slideshow_playlist()
        n = len(playlist)
        if n > 0:
            self.lbl_slideshow_count.set_text(_("{} image{} in list").format(n, "s" if n > 1 else ""))
        else:
            self.lbl_slideshow_count.set_text(_("No image selected"))

    def _sync_flowbox(self):
        self.flowbox.invalidate_filter()
        self.flowbox.queue_resize()
        self._schedule_flowbox_column_update()

    def _init_shortcuts(self):
        controller = Gtk.EventControllerKey.new()
        controller.connect("key-pressed", self._on_key_shortcut)
        self.add_controller(controller)

    def _on_key_shortcut(self, controller, keyval, keycode, state):
        # Enter: apply selected wallpaper
        if keyval == Gdk.KEY_Return or keyval == Gdk.KEY_KP_Enter:
            if self.btn_apply.get_sensitive():
                self._on_apply(None)
            return True

        # Space: toggle fullscreen preview
        if keyval == Gdk.KEY_space:
            if self.selected_image and Path(self.selected_image).exists():
                self._show_fullscreen_preview(self.selected_image)
            return True

        return False

    def _show_fullscreen_preview(self, path: str) -> None:
        """Shows a fullscreen preview of the selected image."""
        win = Gtk.Window(transient_for=self, modal=True)
        win.set_title(Path(path).name)
        win.fullscreen()

        pic = Gtk.Picture()
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_shrink(True)
        pic.set_hexpand(True)
        pic.set_vexpand(True)

        overlay = Gtk.Overlay()
        overlay.set_child(pic)

        # Close hint label
        hint = Gtk.Label(label=_("Press Escape or Space to close"))
        hint.add_css_class("dim-label")
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_valign(Gtk.Align.END)
        hint.set_margin_bottom(16)
        overlay.add_overlay(hint)

        win.set_child(overlay)

        # Load image in thread
        def _load():
            result = ImageLoader.load_for_preview(path, 3840, 2160)
            if result:
                raw, w, h, has_alpha = result
                def _set():
                    from gi.repository import GLib as _GL
                    gbytes = _GL.Bytes.new(raw)
                    fmt = Gdk.MemoryFormat.R8G8B8A8 if has_alpha else Gdk.MemoryFormat.R8G8B8
                    stride = w * (4 if has_alpha else 3)
                    texture = Gdk.MemoryTexture.new(w, h, fmt, gbytes, stride)
                    pic.set_paintable(texture)
                    return False
                GLib.idle_add(_set)
        threading.Thread(target=_load, daemon=True).start()

        # Close on Escape or Space
        key_ctrl = Gtk.EventControllerKey.new()
        key_ctrl.connect("key-pressed", lambda c, kv, kc, s: win.close() or True
                         if kv in (Gdk.KEY_Escape, Gdk.KEY_space) else False)
        win.add_controller(key_ctrl)

        win.present()

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

        for target in [180, 150, 120, 100, 80]:
            cols = max(1, (available + spacing) // (target + spacing))
            if cols >= 2:
                break

        thumb_w = max(60, (available - spacing * (cols - 1)) // cols)
        thumb_h = max(1, int(round(thumb_w * Config.THUMBNAIL_ASPECT)))

        if (cols != self._flowbox_columns or thumb_w != Config.THUMB_W):
            self._flowbox_columns = cols
            Config.THUMB_W = thumb_w
            Config.THUMB_H = thumb_h
            Config.THUMBNAIL_SIZE = thumb_w
            self.flowbox.set_min_children_per_line(4)
            self.flowbox.set_max_children_per_line(cols)

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
                self.lbl_size.set_text(f"{sz / 1_048_576:.1f} MB")
            else:
                self.lbl_size.set_text(f"{sz / 1024:.0f} KB")
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

    def _update_folder_count(self, total: int):
        if hasattr(self, "row_current_folder"):
            self.row_current_folder.set_subtitle(f"{self.folder}\n{total} " + (_("images") if total > 1 else _("image")))

    def _init_state(self):
        self.row_current_folder.set_subtitle(str(self.folder))
        current = self.backend.get_current() if self.backend else None
        if current and Path(current).exists():
            self.selected_image = current
            self.btn_apply.set_sensitive(True)
            self._update_preview(current)
            self._update_image_info(current)
            self._set_active_wallpapers([current])

        mode = self.backend.get_mode() if self.backend else None
        if mode:
            if mode in self.mode_ids:
                self.mode_dropdown.set_selected(self.mode_ids.index(mode))

        # Load monitor 0 folder on startup if assigned
        if self.monitors:
            conn0 = self.monitors[0].connector
            folder0 = self.settings.monitor_folders.get(conn0, "")
            if folder0 and Path(folder0).exists() and str(self.folder) != folder0:
                self.folder = Path(folder0)
                self.settings.folder = folder0
                self.row_current_folder.set_subtitle(folder0)


    def _on_monitor_toggle(self, btn, index: int) -> None:
        """Fix 3: Visually restore image when clicking a monitor tab"""
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

        # Restore image assigned to this specific monitor
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

            # Disconnect signal to avoid re-triggering events
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

        # Load folder if specially assigned for this monitor
        assigned_folder = self.settings.monitor_folders.get(conn)
        if assigned_folder and Path(assigned_folder).exists():
            if str(self.folder) != str(assigned_folder):
                self._jump_to_folder(assigned_folder)
                self._status(f"{_('Monitor')} {index + 1} — {_('folder')}: {Path(assigned_folder).name}")
        else:
            self._status(f"{_('Monitor')} {index + 1} {_('selected')}")


    def _on_choose_folder(self, widget):
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title(_("Choose a folder"))
            dialog.select_folder(self, None, self._on_choose_folder_dialog)
            return

        dialog = Gtk.FileChooserNative(
            title=_("Choose a folder"),
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
        self.row_current_folder.set_subtitle(str(self.folder))
        self._texture_cache.clear()
        self._load_gallery()

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
        dialog.set_message(_("Clear thumbnail cache?"))
        dialog.set_detail(_("They will be recreated on next load."))
        dialog.set_buttons([_("Cancel"), _("Clear")])
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
            self._status(f"{_('Cache cleared')} ({count} {_('files')})")
            self._texture_cache.clear()
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
        """Fix 4: Always force Universal Canvas if more than one screen."""
        if not self.selected_image or not Path(self.selected_image).exists():
            dialog = Gtk.AlertDialog()
            dialog.set_message(_("No image selected"))
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
                self._status(f"⚠ {_('Missing images:')} {', '.join(missing)}")
                return

        self.btn_apply.set_sensitive(False)
        self._status(_("Applying…"))
        image = self.selected_image
        monitors_snapshot = list(self.monitors)
        per_monitor_snapshot = dict(self.settings.per_monitor)

        def _apply_worker():
            ok = False
            active_paths = []
            status_msg = ""

            # NEW LOGIC: Go through multi-monitor backend no matter what (even for "Same image")
            if len(monitors_snapshot) > 1:
                assignments = {}
                if same_all:
                    assignments = {mon.connector: image for mon in monitors_snapshot}
                else:
                    # Bug fix: don't overwrite monitors that already have an assigned wallpaper
                    # Only update the currently selected monitor
                    conn = monitors_snapshot[self.current_monitor].connector
                    assignments = {}
                    for mon in monitors_snapshot:
                        if mon.connector == conn:
                            assignments[mon.connector] = image
                        else:
                            # Keep existing assignment, do NOT fall back to current image
                            existing = per_monitor_snapshot.get(mon.connector, "")
                            if existing and Path(existing).exists():
                                assignments[mon.connector] = existing
                            else:
                                assignments[mon.connector] = image

                results = self.backend.apply_per_monitor(
                    assignments, "spanned", lock, monitors=monitors_snapshot
                )
                ok_count = sum(1 for v in results.values() if v)
                total = len(results)
                ok = ok_count == total
                active_paths = list(assignments.values())
                status_msg = (
                    f"✓ {_('Applied on')} {ok_count}/{total} {_('monitors')}"
                    if ok else f"⚠ {_('Partial canvas:')} {ok_count}/{total} {_('monitors')}"
                )
            else:
                # If user physically has only one screen
                if getattr(self, "_daemon", None) and self._daemon.available:
                    ok = self._daemon.set_wallpaper(image)
                    if not ok:
                        GLib.idle_add(self._status, "✗ " + _("daemon — local fallback"))
                        ok = self.backend.apply_single(image, mode=mode, lock=lock)
                else:
                    ok = self.backend.apply_single(image, mode=mode, lock=lock)
                if ok:
                    active_paths = [image]
                    for mon in monitors_snapshot:
                        self.settings.per_monitor[mon.connector] = image
                    status_msg = f"✓ {_('Applied')}: {Path(image).name}"
                else:
                    status_msg = "✗ " + _("Application failed")

            GLib.idle_add(self._on_apply_done, ok, image, active_paths, mode, lock, status_msg)

        threading.Thread(target=_apply_worker, daemon=True).start()

    def _on_apply_done(self, ok: bool, image: str, active_paths: list,
                       mode: str, lock: bool, status_msg: str) -> bool:
        self.btn_apply.set_sensitive(True)
        self._status(status_msg)
        if ok:
            self._set_active_wallpapers(active_paths)
            self.btn_apply.set_label(_("✓ Applied!"))
            GLib.timeout_add(
                2500,
                lambda: (self._update_apply_btn_subtitle(), False)[-1]
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
        self._status(_("Loading..."))

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
            GLib.idle_add(self._status, _("Folder not found"))
            GLib.idle_add(self._set_progress_visibility, False)
            return
        try:
            raw_files = [
                f for f in folder.iterdir()
                if f.is_file() and f.suffix.lower() in Config.VALID_EXT
            ]
            if getattr(self, "_sort_mode", "name") == "date":
                files = sorted(raw_files, key=lambda f: f.stat().st_mtime, reverse=True)[:Config.MAX_IMAGES]
            else:
                files = sorted(raw_files)[:Config.MAX_IMAGES]
        except PermissionError:
            GLib.idle_add(self._status, _("Permission denied"))
            GLib.idle_add(self._set_progress_visibility, False)
            return

        total = len(files)
        GLib.idle_add(self._update_folder_count, total)
        if total == 0:
            GLib.idle_add(self._status, _("No images found"))
            GLib.idle_add(self._set_progress_visibility, False)
            return

        import time
        BATCH_SIZE = 8
        batch = []

        for i, fpath in enumerate(files):
            if stop_event.is_set() or generation != self.gallery_generation:
                return
            
            # 1. RAM Cache: If already loaded, use texture directly
            if str(fpath) in self._texture_cache:
                batch.append((fpath, self._texture_cache[str(fpath)]))
            else:
                # 2. AVIF priority for source
                avif = get_cached_avif(str(fpath))
                src = str(avif) if avif else str(fpath)
                
                # Disk generation (if necessary)
                thumb_path = Thumbnailer.generate(src, Config.THUMB_W, Config.THUMB_H, Config.THUMB_DIR)
                
                # 3. Async Loading: Read Pixbuf here (Thread) to avoid blocking UI
                load_path = str(thumb_path) if (thumb_path and thumb_path.exists()) else src
                try:
                    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                        load_path, Config.THUMB_W, Config.THUMB_H, preserve_aspect_ratio=False
                    )
                    batch.append((fpath, pixbuf))
                except Exception:
                    batch.append((fpath, None))

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

    def _add_thumb_batch_counted(self, items: list) -> bool:
        for fpath, obj in items:
            if self._stop_event.is_set():
                break
            self._add_thumb(fpath, obj)
        self._refresh_active_indicators()
        with self._pending_batches_lock:
            self._pending_batches = max(0, self._pending_batches - 1)
        return False

    def _add_thumb(self, fpath: Path, image_obj: object):
        if self._stop_event.is_set():
            return False

        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(Config.THUMB_W, Config.THUMB_H)
        try:
            texture = None
            if isinstance(image_obj, Gdk.Texture):
                texture = image_obj
            elif isinstance(image_obj, GdkPixbuf.Pixbuf):
                texture = Gdk.Texture.new_for_pixbuf(image_obj)
                # RAM caching for next time
                self._texture_cache[str(fpath)] = texture
            
            if texture:
                picture.set_paintable(texture)
            else:
                raise ValueError("No texture")
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
        self._status(f"✓ {total} " + _("images") + f" — {self.folder.name}")
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
            f"{n} " + (_("screens") if n > 1 else _("screen")) + " " + _("detected")
        )
        if self.current_monitor >= len(self.monitors):
            self.current_monitor = 0
        self._status(_("Monitors updated: {} detected").format(n))

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
        logger.debug("Config saved")
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

class MuralApplication(Adw.Application):
    def __init__(self, **kwargs):
        super().__init__(application_id="io.github.gaorfg_bit.Mural", **kwargs)

    def do_activate(self):
        """This method is called when launching 'mural'"""
        try:
            logger.debug("Entering do_activate")
            
            # Check if a window already exists
            win = self.get_active_window()
            
            if not win:
                logger.debug("Creating new MuralWindow")
                # Ensure window class is named MuralWindow
                win = MuralWindow(application=self)
            
            logger.debug("Showing window")
            win.present() # This is THE line that prevents program from quitting immediately
        except Exception as e:
            # This will give us the real reason for the crash
            print("\n" + "!"*50)
            print("💥 FATAL CRASH IN INTERFACE:")
            traceback.print_exc()
            print("!"*50 + "\n")
            self.quit()

if __name__ == "__main__":
    app = MuralApplication()
    app.run(sys.argv)
