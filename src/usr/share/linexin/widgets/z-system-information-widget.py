#!/usr/bin/env python3
import gi
import subprocess
import threading
import gettext
import locale
import os
import distro
import psutil
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango
APP_NAME = "system-information"
LOCALE_DIR = os.path.abspath("/usr/share/locale")
locale.setlocale(locale.LC_ALL, '')
locale.bindtextdomain(APP_NAME, LOCALE_DIR)
gettext.bindtextdomain(APP_NAME, LOCALE_DIR)
gettext.textdomain(APP_NAME)
_ = gettext.gettext
class LinexinSysInfoWidget(Gtk.Box):
    def __init__(self, hide_sidebar=False, window=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.widgetname = "System Information"
        self.widgeticon = "/usr/share/icons/computer-linexin.svg"
        self.set_margin_top(12)
        self.set_margin_bottom(50)
        self.set_margin_start(50)
        self.set_margin_end(50)
        self.window = window
        self.hide_sidebar = hide_sidebar
        self.setup_ui()
        self.load_system_info()
        if self.hide_sidebar and self.window:
            GLib.idle_add(self.resize_window_deferred)
    def resize_window_deferred(self):
        """Resize window for single widget mode"""
        if self.window:
            try:
                self.window.set_default_size(1200, 900)
            except Exception as e:
                print(f"Failed to resize window: {e}")
        return False
    def setup_ui(self):
        """Setup the user interface"""
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_bottom(20)
        system_icon = Gtk.Image()
        if os.path.exists("/usr/share/icons/computer-linexin.svg"):
            system_icon.set_from_file("/usr/share/icons/computer-linexin.svg")
        else:
            system_icon.set_from_icon_name("computer")
        system_icon.set_pixel_size(48)
        header_box.append(system_icon)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_hexpand(True)  
        title_label = Gtk.Label(label=_("System Information"))
        title_label.add_css_class("title-2")
        title_label.set_halign(Gtk.Align.START)
        title_box.append(title_label)
        try:
            hostname = os.uname().nodename
            hostname_label = Gtk.Label(label=hostname)
            hostname_label.add_css_class("title-4")
            hostname_label.add_css_class("dim-label")
            hostname_label.set_halign(Gtk.Align.START)
            title_box.append(hostname_label)
        except:
            pass
        header_box.append(title_box)
        self.append(header_box)
        self.setup_row_view()
    def setup_row_view(self):
        """Setup the row-based system info view"""
        self.info_listbox = Gtk.ListBox()
        self.info_listbox.add_css_class("boxed-list")
        self.info_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.info_listbox)
        scrolled.set_vexpand(True)
        scrolled.set_vexpand(True)
        self.append(scrolled)
    def setup_fastfetch_view(self):
        """Setup the fastfetch output view using VTE terminal"""
        if not VTE_AVAILABLE:
            print("VTE not available, using text fallback")
            self.terminal_available = False
            self.setup_fastfetch_text_fallback()
            return
        try:
            self.terminal = Vte.Terminal()
            self.terminal.set_scrollback_lines(1000)
            font_desc = Pango.FontDescription.from_string("monospace 10")
            self.terminal.set_font(font_desc)
            print(f"VTE terminal created successfully (version {VTE_VERSION})")
            terminal_scrolled = Gtk.ScrolledWindow()
            terminal_scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            terminal_scrolled.set_child(self.terminal)
            terminal_scrolled.set_vexpand(True)
            self.content_stack.add_named(terminal_scrolled, "fastfetch")
            self.terminal_available = True
        except Exception as e:
            print(f"VTE terminal initialization failed: {e}")
            self.terminal_available = False
            self.setup_fastfetch_text_fallback()
    def create_info_row(self, label, value, icon_name=None):
        """Create a row with label and value"""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        if icon_name:
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(20)
            box.append(icon)
        label_widget = Gtk.Label(label=label)
        label_widget.set_halign(Gtk.Align.START)
        label_widget.set_hexpand(True)
        box.append(label_widget)
        value_widget = Gtk.Label(label=str(value))
        value_widget.add_css_class("dim-label")
        value_widget.set_halign(Gtk.Align.END)
        value_widget.set_selectable(True)  
        box.append(value_widget)
        row.set_child(box)
        return row
    def format_bytes(self, bytes_value):
        """Format bytes to human readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.1f} PB"
    def get_cpu_info(self):
        """Get CPU information"""
        try:
            with open('/proc/cpuinfo', 'r') as f:
                for line in f:
                    if line.startswith('model name'):
                        return line.split(':')[1].strip()
        except:
            pass
        return _("Unknown")
    def get_kernel_info(self):
        """Get kernel information"""
        try:
            return os.uname().release
        except:
            return _("Unknown")
    def get_uptime(self):
        """Get system uptime"""
        try:
            uptime_seconds = psutil.boot_time()
            import time
            uptime = time.time() - uptime_seconds
            days = int(uptime // 86400)
            hours = int((uptime % 86400) // 3600)
            minutes = int((uptime % 3600) // 60)
            if days > 0:
                return f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                return f"{hours}h {minutes}m"
            else:
                return f"{minutes}m"
        except:
            return _("Unknown")
    def get_version_date(self):
        """Get Version Date from /version file"""
        try:
            with open('/version', 'r') as f:
                content = f.read().strip()
                return content if content else None
        except:
            pass
        return None
    def get_version_id(self):
        """Get VERSION_ID from os-release"""
        try:
            with open('/etc/os-release', 'r') as f:
                for line in f:
                    if line.startswith('VERSION_ID='):
                        return line.split('=')[1].strip().strip('"')
        except:
            pass
        return None
    def get_session_type(self):
        """Get session type (X11/Wayland)"""
        session_type = os.environ.get('XDG_SESSION_TYPE', '').lower()
        if session_type:
            return session_type.capitalize()
        return _("Unknown")
    def get_desktop_environment(self):
        """Get desktop environment"""
        for env_var in ['XDG_CURRENT_DESKTOP', 'DESKTOP_SESSION', 'XDG_SESSION_DESKTOP']:
            de = os.environ.get(env_var, '').lower()
            if de:
                de_mapping = {
                    'gnome': 'GNOME',
                    'kde': 'KDE',
                    'xfce': 'Xfce',
                    'mate': 'MATE',
                    'cinnamon': 'Cinnamon',
                    'lxde': 'LXDE',
                    'lxqt': 'LXQt',
                    'pantheon': 'Pantheon',
                    'budgie': 'Budgie',
                    'deepin': 'Deepin',
                    'unity': 'Unity',
                    'i3': 'i3',
                    'sway': 'Sway',
                    'awesome': 'Awesome',
                    'openbox': 'Openbox',
                    'fluxbox': 'Fluxbox',
                    'bspwm': 'bspwm',
                    'dwm': 'dwm',
                    'qtile': 'Qtile',
                    'herbstluftwm': 'herbstluftwm'
                }
                return de_mapping.get(de, de.capitalize())
        return _("Unknown")
    def get_window_manager(self):
        """Get window manager"""
        wm = os.environ.get('WINDOW_MANAGER', '')
        if wm:
            return os.path.basename(wm)
        try:
            result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                wm_mapping = {
                    'mutter': 'Mutter',
                    'kwin_x11': 'KWin',
                    'kwin_wayland': 'KWin', 
                    'kwin': 'KWin',
                    'xfwm4': 'Xfwm4',
                    'openbox': 'Openbox',
                    'i3': 'i3',
                    'sway': 'Sway',
                    'awesome': 'Awesome',
                    'dwm': 'DWM',
                    'bspwm': 'bspwm',
                    'qtile': 'Qtile',
                    'herbstluftwm': 'herbstluftwm',
                    'fluxbox': 'Fluxbox',
                    'marco': 'Marco',
                    'metacity': 'Metacity',
                    'compiz': 'Compiz',
                    'enlightenment': 'Enlightenment',
                    'cwm': 'CWM',
                    'jwm': 'JWM'
                }
                for wm_proc, wm_name in wm_mapping.items():
                    if wm_proc in result.stdout:
                        return wm_name
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        try:
            if os.environ.get('DISPLAY'):
                result = subprocess.run(['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and 'window id' in result.stdout:
                    wm_id = result.stdout.split()[-1]
                    result2 = subprocess.run(['xprop', '-id', wm_id, '_NET_WM_NAME'], 
                                           capture_output=True, text=True, timeout=5)
                    if result2.returncode == 0 and '=' in result2.stdout:
                        wm_name = result2.stdout.split('=')[1].strip().strip('"')
                        if wm_name.lower() != 'gnome shell':
                            return wm_name
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        return _("Unknown")
    def get_gpu_info(self):
        """Get GPU card name and driver information"""
        gpu_name = _("Unknown")
        driver_version = None
        try:
            result = subprocess.run(['lspci'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                for line in result.stdout.split('\n'):
                    if 'VGA' in line or 'Display' in line or '3D' in line:
                        if ':' in line:
                            gpu_part = line.split(':', 2)[-1].strip()
                            gpu_part = gpu_part.replace('VGA compatible controller: ', '')
                            gpu_part = gpu_part.replace('Display controller: ', '')
                            gpu_part = gpu_part.replace('3D controller: ', '')
                            if '(rev' in gpu_part:
                                gpu_part = gpu_part.split('(rev')[0].strip()
                            if 'NVIDIA Corporation' in gpu_part:
                                gpu_part = gpu_part.replace('NVIDIA Corporation ', '')
                                if '[' in gpu_part and ']' in gpu_part:
                                    bracket_content = gpu_part[gpu_part.find('[')+1:gpu_part.find(']')]
                                    gpu_name = bracket_content
                                else:
                                    gpu_name = gpu_part
                            elif 'AMD' in gpu_part or 'Advanced Micro Devices' in gpu_part:
                                gpu_part = gpu_part.replace('Advanced Micro Devices, Inc. ', '')
                                gpu_part = gpu_part.replace('AMD ', '')
                                if '[' in gpu_part and ']' in gpu_part:
                                    bracket_content = gpu_part[gpu_part.find('[')+1:gpu_part.find(']')]
                                    gpu_name = bracket_content
                                else:
                                    gpu_name = gpu_part
                            elif 'Intel' in gpu_part:
                                gpu_part = gpu_part.replace('Intel Corporation ', '')
                                gpu_name = gpu_part
                            else:
                                gpu_name = gpu_part
                            break
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        try:
            result = subprocess.run(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader,nounits'], 
                                   capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip():
                driver_version = f"NVIDIA {result.stdout.strip()}"
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        if not driver_version:
            try:
                result = subprocess.run(['modinfo', 'amdgpu'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    driver_version = "AMDGPU"
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        if not driver_version:
            try:
                result = subprocess.run(['modinfo', 'i915'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    driver_version = "Intel i915"
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
        if driver_version:
            return f"{gpu_name} ({driver_version})"
        else:
            return gpu_name
    def load_system_info(self):
        """Load and display system information"""
        def load_info():
            info_data = []
            try:
                os_name = distro.name(pretty=True)
                if not os_name:
                    os_name = f"{distro.id()} {distro.version()}"
                info_data.append((_("Operating System"), os_name, "computer"))
                version_id = self.get_version_id()
                if version_id:
                    info_data.append((_("Version ID"), version_id, "application-certificate"))
                version_date = self.get_version_date()
                if version_date:
                    info_data.append((_("Version Date"), version_date, "preferences-system-time"))
                kernel = self.get_kernel_info()
                info_data.append((_("Kernel"), kernel, "application-x-firmware"))
                session_type = self.get_session_type()
                info_data.append((_("Session Type"), session_type, "preferences-desktop-display"))
                desktop_env = self.get_desktop_environment()
                info_data.append((_("Desktop Environment"), desktop_env, "preferences-desktop"))
                window_manager = self.get_window_manager()
                info_data.append((_("Window Manager"), window_manager, "preferences-desktop-wallpaper"))
                cpu_info = self.get_cpu_info()
                cpu_count = psutil.cpu_count()
                cpu_text = f"{cpu_info} ({cpu_count} cores)"
                info_data.append((_("Processor"), cpu_text, "applications-system"))
                gpu_info = self.get_gpu_info()
                info_data.append((_("Graphics"), gpu_info, "video-display"))
                memory = psutil.virtual_memory()
                memory_text = f"{self.format_bytes(memory.used)} / {self.format_bytes(memory.total)} ({memory.percent:.1f}%)"
                info_data.append((_("Memory"), memory_text, "drive-harddisk"))
                disk = psutil.disk_usage('/')
                disk_text = f"{self.format_bytes(disk.used)} / {self.format_bytes(disk.total)} ({disk.percent:.1f}%)"
                info_data.append((_("Disk Usage"), disk_text, "drive-harddisk"))
                uptime = self.get_uptime()
                info_data.append((_("Uptime"), uptime, "preferences-system-time"))
            except Exception as e:
                print(f"Error loading system info: {e}")
                info_data.append((_("Error"), _("Failed to load system information"), "dialog-error"))
            GLib.idle_add(self.update_ui, info_data)
        threading.Thread(target=load_info, daemon=True).start()
    def update_ui(self, info_data):
        """Update the UI with system information"""
        for label, value, icon in info_data:
            row = self.create_info_row(label, value, icon)
            self.info_listbox.append(row)
        return False
if __name__ == "__main__":
    class TestWindow(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app)
            self.set_title("System Information Widget")
            self.set_default_size(500, 400)
            widget = LinexinSysInfoWidget(hide_sidebar=True, window=self)
            self.set_child(widget)
    class TestApp(Gtk.Application):
        def do_activate(self):
            window = TestWindow(self)
            window.present()
    app = TestApp()
    app.run()
