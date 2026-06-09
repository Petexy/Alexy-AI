#!/usr/bin/env python3
import gi # type: ignore # pylint: disable=import-error
import os
import json
import re as _re
import shutil
import urllib.request
import urllib.error
import threading
import subprocess
import gettext
import locale
import uuid
import tempfile
import atexit
import base64
from typing import Optional, Any, List, Dict

gi.require_version("Gtk", "4.0") # type: ignore
gi.require_version("Adw", "1") # type: ignore
from gi.repository import Gtk, Adw, GLib # type: ignore # pylint: disable=import-error

APP_NAME = "ai-sysadmin"
LOCALE_DIR = os.path.abspath("/usr/share/locale")
try:
    locale.setlocale(locale.LC_ALL, '')
    locale.bindtextdomain(APP_NAME, LOCALE_DIR)
    gettext.bindtextdomain(APP_NAME, LOCALE_DIR)
    gettext.textdomain(APP_NAME)
    _ = gettext.gettext
except Exception:
    def _(message: str) -> str: return message

CONFIG_DIR = os.path.expanduser("~/.config/linexin")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ai-sysadmin.json")
CONVERSATIONS_DIR = os.path.join(CONFIG_DIR, "conversations")
BUNDLED_THEMES_DIR = "/usr/share/linexin/widgets/themes/"
USER_THEMES_DIR = os.path.join(CONFIG_DIR, "themes")

# Previous location of Alexy's settings (shared with the linexin-center dir).
# On first launch after the move, the Alexy-specific files below are migrated
# from here into CONFIG_DIR.
_OLD_CONFIG_DIR = os.path.expanduser("~/.config/linexin-center")

# Name of the built-in, non-editable agent (proper noun, kept as a stable key).
DEFAULT_AGENT_NAME = "Alexy"

# Additional built-in, non-editable agent that answers in rhyming verse.
RHYMEXY_AGENT_NAME = "Rhymexy"
RHYMEXY_PROMPT = """You are Rhymexy.

Your purpose is to answer every user request in rhyming verse.

Rules:

1. Every response must rhyme.
2. Always rhyme in the same language the user is writing in. Detect the user's language from their message and compose your rhymes in that language, never defaulting to English unless the user writes in English.
3. Preserve factual accuracy.
4. Never sacrifice correctness solely for rhyme.
5. Use couplets, quatrains, or longer rhyme schemes as needed.
6. Maintain a natural flow and readability.
7. Never explain your rhyming behavior unless explicitly asked.
8. For technical topics:
 - Explain concepts using rhyme.
 - Use precise terminology even if some lines do not rhyme perfectly.
9. For code requests:
 - Introduce the solution with rhyming text.
 - Output code exactly as needed in code blocks.
 - Resume rhyming after the code block if additional explanation is required.
10. For refusals:
 - Refuse while maintaining rhyme.
 - Never abandon the rhyme scheme.

Example:

To sort this list with speed and grace,
Use quicksort in the proper place.
Its average runtime shines quite bright,
Though worst-case paths may lose the fight.

You are not occasionally poetic.
You are permanently rhyming."""

# Ordered list of built-in agent names (non-editable, baked into the app).
BUILTIN_AGENT_NAMES = [DEFAULT_AGENT_NAME, RHYMEXY_AGENT_NAME]


def _migrate_legacy_config() -> None:
    """One-time migration of Alexy's settings from ~/.config/linexin-center to
    ~/.config/linexin. Only the Alexy-specific items are moved; linexin-center's
    own files are left untouched. Runs only if the new config does not yet exist."""
    try:
        if os.path.exists(CONFIG_FILE):
            return  # Already migrated (or fresh install with new layout).
        if not os.path.isdir(_OLD_CONFIG_DIR):
            return  # Nothing to migrate.
        items = ["ai-sysadmin.json", "conversations", "themes"]
        moved_any = False
        for name in items:
            src = os.path.join(_OLD_CONFIG_DIR, name)
            if not os.path.exists(src):
                continue
            os.makedirs(CONFIG_DIR, exist_ok=True)
            dst = os.path.join(CONFIG_DIR, name)
            if os.path.exists(dst):
                continue  # Don't clobber anything already at the destination.
            try:
                shutil.move(src, dst)
                moved_any = True
            except Exception as e:
                print(f"Config migration: failed to move {name}: {e}")
        if moved_any:
            print(f"Migrated Alexy settings to {CONFIG_DIR}")
    except Exception as e:
        print(f"Config migration error: {e}")


_migrate_legacy_config()


def _resolve_icon(*candidates: str) -> Optional[str]:
    """Return the first icon name that actually exists in the current icon
    theme. Returns None if none of the candidates are available.

    Some desktops (notably KDE Plasma with the Breeze icon theme) do not ship
    every GNOME/freedesktop symbolic icon name, e.g. ``list-add-symbolic``.
    Probing the theme lets us fall back to a name that does exist instead of
    rendering a blank/broken button.
    """
    try:
        from gi.repository import Gdk  # type: ignore # pylint: disable=import-error
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            for name in candidates:
                if name and theme.has_icon(name):
                    return name
    except Exception:
        pass
    return None


def _icon(primary: str, *fallbacks: str) -> str:
    """Like :func:`_resolve_icon` but always returns a usable string, defaulting
    to ``primary`` when nothing in the theme matched."""
    return _resolve_icon(primary, *fallbacks) or primary


def _set_button_icon(button, *candidates: str, text_fallback: Optional[str] = None) -> None:
    """Set a button's icon to the first available candidate. If none of the
    icon names exist in the theme and ``text_fallback`` is given, show that text
    instead so the control is never invisible."""
    name = _resolve_icon(*candidates)
    if name:
        button.set_icon_name(name)
    elif text_fallback is not None:
        button.set_label(text_fallback)
    else:
        button.set_icon_name(candidates[0])


class SudoManager:
    _instance: Optional['SudoManager'] = None
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    def __init__(self):
        self.user_password = None
        self._running = True
        self._askpass_tf = tempfile.NamedTemporaryFile(delete=False, prefix="linexin-askpass-")
        self.askpass_script = self._askpass_tf.name
        self._askpass_tf.close()
        self._sudo_tf = tempfile.NamedTemporaryFile(delete=False, prefix="linexin-sudo-")
        self.wrapper_path = self._sudo_tf.name
        self._sudo_tf.close()
        self.fifo_dir = tempfile.mkdtemp(prefix="linexin-pipe-")
        self.fifo_path = os.path.join(self.fifo_dir, "password_pipe")
        os.mkfifo(self.fifo_path, 0o600)
        self._setup_scripts()
        self._feed_condition = threading.Condition()
        self._feeds_allowed = 0
        self.feeder_thread = threading.Thread(target=self._feed_pipe_loop, daemon=True)
        self.feeder_thread.start()
        atexit.register(self.cleanup) # type: ignore
    def _feed_pipe_loop(self):
        """Thread that writes password to pipe only when authorized"""
        while self._running:
            with self._feed_condition:
                self._feed_condition.wait_for(lambda: self._feeds_allowed > 0 or not self._running)
            
            if not self._running:
                break
                
            if self.user_password:
                try:
                    # Open will block until a reader connects (sudo -A)
                    fd = os.open(self.fifo_path, os.O_WRONLY)
                    with os.fdopen(fd, 'w') as f:
                        f.write(str(self.user_password) + '\n')
                    
                    # Decrement allowed feeds after successful write
                    with self._feed_condition:
                        if self._feeds_allowed > 0:
                            self._feeds_allowed -= 1
                except OSError:
                    pass
                except Exception as e:
                    print(f"Pipe error: {e}")
            else:
                # Consume token but write nothing/newline if no password (shouldn't happen in valid flow)
                with self._feed_condition:
                     if self._feeds_allowed > 0:
                         self._feeds_allowed -= 1
    
    def run_privileged(self, cmd, **kwargs):
        """Run a command using the sudo wrapper with secure gating"""
        if not self.user_password:
            raise ValueError("No password set")
            
        with self._feed_condition:
            self._feeds_allowed += 1
            self._feed_condition.notify_all()
            
        try:
            full_cmd = [self.wrapper_path] + cmd
            return subprocess.run(full_cmd, **kwargs)
        finally:
            self._drain_pipe()

    def start_privileged_session(self):
        """Open the password gate for a long-running session"""
        if not self.user_password:
             return
        with self._feed_condition:
            self._feeds_allowed = 1000 # Allow many reads for complex operations
            self._feed_condition.notify_all()
            
    def stop_privileged_session(self):
        """Close the password gate"""
        with self._feed_condition:
            self._feeds_allowed = 0
        self._drain_pipe()

    def _drain_pipe(self):
        """Helper to drain pipe if feed wasn't consumed"""
        remaining = 0
        with self._feed_condition:
            remaining = self._feeds_allowed
            
        if remaining > 0:
            try:
                fd = os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
                os.read(fd, 1024)
                os.close(fd)
            except Exception:
                pass

    def _setup_scripts(self):
        with open(self.askpass_script, "w") as f:
            f.write(f"#!/bin/sh\ncat \"{self.fifo_path}\"\n")
        os.chmod(self.askpass_script, 0o700)
        with open(self.wrapper_path, "w") as f:
            f.write(f"#!/bin/sh\nexport SUDO_ASKPASS='{self.askpass_script}'\nexec sudo -A \"$@\"\n")
        os.chmod(self.wrapper_path, 0o700)
    def validate_password(self, password):
        """Validate password using sudo -S -v"""
        if not password:
            return False
        try:
            subprocess.run(['sudo', '-k'], check=False)
            result = subprocess.run(
                ['sudo', '-S', '-v'],
                input=(password + '\n'),
                capture_output=True,
                text=True,
                env={**os.environ, 'LC_ALL': 'C'}
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Sudo validation error: {e}")
            return False
    def set_password(self, password):
        """Store the validated password"""
        self.user_password = password
    def clear_cache(self):
        """Invalidate sudo credentials cache"""
        try:
            subprocess.run(['sudo', '-k'], check=False)
        except Exception:
            pass
    def forget_password(self):
        """Clear stored password and invalidate sudo cache"""
        self.user_password = None
        self.clear_cache()
    def get_env(self):
        """Return environment variables needed for the wrapper (none for password now)"""
        env = os.environ.copy()
        return env
    def cleanup(self):
        """Remove temporary files and clear credentials"""
        self._running = False
        self.forget_password()
        try:
            os.open(self.fifo_path, os.O_RDONLY | os.O_NONBLOCK)
        except:
            pass
        try:
            if os.path.exists(self.askpass_script):
                os.remove(self.askpass_script)
            if os.path.exists(self.wrapper_path):
                os.remove(self.wrapper_path)
            if os.path.exists(self.fifo_path):
                os.remove(self.fifo_path)
            if os.path.exists(self.fifo_dir):
                os.rmdir(self.fifo_dir)
        except:
            pass

class _ActionProgressWindow(Adw.Window):
    def __init__(self, parent=None, title="", cmd_string="", is_ollama=False, initial_status=None, on_close_callback=None, sudo_manager=None, model_name=None, **kwargs):
        if not cmd_string:
            super().__init__()
            return
        super().__init__(title=title, transient_for=parent, modal=True) # type: ignore
        self.set_default_size(500, 200)
        self.cmd_string = cmd_string
        self.is_ollama = is_ollama
        self.model_name = model_name or ""
        self.on_close_callback = on_close_callback
        self.success = False
        self.process_finished = False
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.sudo_manager = sudo_manager
        self._has_real_progress = False
        self.set_deletable(False)
        self.connect("close-request", self.handle_close)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)
        
        # HeaderBar
        header = Adw.HeaderBar()
        box.append(header)
        
        # Main content area
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)
        box.append(content)
        
        # Status Label
        if not initial_status:
            initial_status = _("Starting operation...")
        self.status_label = Gtk.Label(label=initial_status)
        self.status_label.add_css_class("title-4")
        self.status_label.set_wrap(True)
        content.append(self.status_label)
        
        # Progress Bar
        self.progress = Gtk.ProgressBar()
        self.progress.set_margin_top(12)
        if not self.is_ollama:
            self.progress.set_pulse_step(0.1)
            GLib.timeout_add(100, self.pulse_progress)
        content.append(self.progress)
        
        # Start the subprocess in a background thread
        threading.Thread(target=self.run_process, daemon=True).start()

    def pulse_progress(self):
        if not self.process_finished and not self.is_ollama and not self._has_real_progress:
            self.progress.pulse()
            return True
        return False
        
    def run_process(self):
        try:
            cmd_args = ["bash", "-c", self.cmd_string]
            if self.sudo_manager:
                self.sudo_manager.start_privileged_session()
                cmd_args = [self.sudo_manager.wrapper_path] + cmd_args
            
            self.process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            
            process = self.process
            if process:
                stdout = process.stdout
                if stdout:
                    buf = b""
                    while True:
                        chunk = stdout.read(1)
                        if not chunk:
                            if buf:
                                GLib.idle_add(self.parse_and_append, buf.decode("utf-8", errors="replace"))
                            break
                        if chunk in (b"\n", b"\r"):
                            if buf:
                                GLib.idle_add(self.parse_and_append, buf.decode("utf-8", errors="replace"))
                                buf = b""
                        else:
                            buf += chunk
                
            if process:
                process.wait()
            self.process_finished = True
            
            if self.sudo_manager:
                self.sudo_manager.stop_privileged_session()
                
            GLib.idle_add(self.on_finish, process.returncode if process else 1)
        except Exception as e:
            self.process_finished = True
            if self.sudo_manager:
                self.sudo_manager.stop_privileged_session()
            print(f"Error launching process: {str(e)}")
            GLib.idle_add(self.status_label.set_label, _("Process failed to start."))

    def parse_and_append(self, line):
        # Print raw output to the shell for debugging
        print(line)
        
        import re
        
        # Strip ANSI escape sequences (colors, formatting)
        clean_line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
        clean_line = clean_line.strip()
        
        # Filter out ASCII box borders (e.g. +-----------------+, |                 |)
        filtered_line = re.sub(r'^[+\-|*=\s]+$', '', clean_line)
        filtered_line = filtered_line.strip('| \t')
        
        if not filtered_line:
            return False
        
        # Filter out progress bar lines (curl ##, npm progress, spinners)
        if re.match(r'^[#\s.]+$', filtered_line):
            return False
        # Detect curl-style progress bars with percentage (e.g. "######### 45.2%")
        curl_pct_match = re.match(r'^[#\s]+(\d+(?:\.\d+)?)%\s*$', filtered_line)
        if curl_pct_match:
            pct = float(curl_pct_match.group(1))
            self._has_real_progress = True
            self.progress.set_fraction(pct / 100.0)
            self.status_label.set_label(f"{pct:.1f}%")
            return False
        if re.match(r'^[\\|/\-⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏\s]+$', filtered_line):
            return False
        # Filter out npm progress lines like '⸩ ⠏' or bare percentage lines
        if re.match(r'^[⸩⸨()\[\]#=>.\-\s⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+$', filtered_line):
            return False
        
        if self.is_ollama:
            import re
            display_name = self.model_name or _("model")
            lower = filtered_line.lower()
            # Map ollama pull phases to user-friendly labels
            if "pulling manifest" in lower:
                self.status_label.set_label(_("Fetching model info for {}...").format(display_name))
            elif "pulling" in lower:
                # "pulling <hash>" lines — show friendly download label
                self.status_label.set_label(_("Downloading {}...").format(display_name))
            elif "verifying" in lower:
                self.status_label.set_label(_("Verifying download integrity..."))
            elif "writing" in lower:
                self.status_label.set_label(_("Finalizing {}...").format(display_name))
            elif "success" in lower:
                self.status_label.set_label(_("Successfully downloaded {}!").format(display_name))
                
            match = re.search(r'(\d+)%', clean_line)
            if match:
                val = int(match.group(1))
                self.progress.set_fraction(val / 100.0)
        else:
            # Extract percentage if present and update progress bar
            match = re.search(r'(\d+(?:\.\d+)?)%', clean_line)
            if match:
                self._has_real_progress = True
                val = float(match.group(1))
                self.progress.set_fraction(val / 100.0)
            # Just take the last 60 chars of whatever it is doing to look busy
            truncated = (filtered_line[:60] + '...') if len(filtered_line) > 60 else filtered_line # type: ignore
            self.status_label.set_label(truncated)
            
        return False
        
    def on_finish(self, rc):
        if self.success:
            return

        if rc == 0:
            self.status_label.set_label(_("Operation completed successfully."))
            self.progress.set_fraction(1.0)
            self.success = True
            self.set_deletable(True)
            GLib.timeout_add(1500, self.close)
        else:
            self.set_deletable(True)
            self.status_label.set_label(_("Operation failed with exit code {}. Check console output.").format(rc))
            self.success = False

    def handle_close(self, win):
        if not self.process_finished:
            return True
        if hasattr(self, 'on_close_callback') and self.on_close_callback:
            self.on_close_callback(self.success)
        return False

class MultilineEntry(Gtk.ScrolledWindow):
    _css_loaded = False

    def __init__(self):
        super().__init__()
        
        if not MultilineEntry._css_loaded:
            from gi.repository import Gdk # type: ignore
            provider = Gtk.CssProvider()
            provider.load_from_data(b"""
                scrolledwindow.multiline-entry {
                    min-height: 0px;
                    min-width: 0px;
                    background-color: @view_bg_color;
                    border: 1px solid @borders;
                    border-radius: 6px;
                    transition: outline 200ms cubic-bezier(0.25, 0.46, 0.45, 0.94);
                }
                scrolledwindow.multiline-entry scrollbar,
                scrolledwindow.multiline-entry scrollbar slider {
                    min-height: 0px;
                    min-width: 0px;
                }
                scrolledwindow.multiline-entry:focus-within {
                    outline: 2px solid @accent_bg_color;
                    outline-offset: -2px;
                }
                scrolledwindow.multiline-entry textview {
                    background-color: transparent;
                }
            """)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider, # type: ignore
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            MultilineEntry._css_loaded = True

        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_propagate_natural_height(False)
        self.set_has_frame(False)
        self.add_css_class("multiline-entry")
        
        self.textview = Gtk.TextView()
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_left_margin(12)
        self.textview.set_right_margin(12)
        self.textview.set_top_margin(8)
        self.textview.set_bottom_margin(8)
        self.textview.set_valign(Gtk.Align.FILL)
        
        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.textview)
        
        self.placeholder_label = Gtk.Label()
        self.placeholder_label.add_css_class("dim-label")
        self.placeholder_label.set_halign(Gtk.Align.START)
        self.placeholder_label.set_valign(Gtk.Align.START)
        self.placeholder_label.set_margin_start(12)
        self.placeholder_label.set_margin_top(8)
        self.placeholder_label.set_can_target(False)
        self.overlay.add_overlay(self.placeholder_label)
        
        self.set_child(self.overlay)
        self.set_valign(Gtk.Align.END)
        self.set_size_request(-1, 40)
        
        self.buf = self.textview.get_buffer()
        self.buf.connect("changed", self._on_buf_changed)
        
    def _on_buf_changed(self, buf):
        has_text = buf.get_char_count() > 0
        self.placeholder_label.set_visible(not has_text)
        
        def update_height():
            from gi.repository import Pango # type: ignore
            layout = self.textview.create_pango_layout(self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), True))
            width = self.textview.get_allocated_width() - 24
            if width > 0:
                layout.set_width(width * Pango.SCALE)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            _, logical_rect = layout.get_pixel_extents()
            text_height = logical_rect.height
            total_height = text_height + 18
            
            new_height = max(40, min(total_height, 140))
            self.set_size_request(-1, new_height)
            return False
            
        from gi.repository import GLib # type: ignore
        GLib.idle_add(update_height)

    def set_placeholder_text(self, text):
        self.placeholder_label.set_label(text)
        
    def get_text(self):
        return self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), True)
        
    def set_text(self, text):
        self.buf.set_text(text)
        
    def set_sensitive(self, sensitive):
        self.textview.set_sensitive(sensitive)
        
    def grab_focus(self):
        return self.textview.grab_focus()

    def connect_activate(self, callback):
        key_ctrl = Gtk.EventControllerKey.new()
        def on_key(ctrl, keyval, keycode, state):
            from gi.repository import Gdk # type: ignore
            if keyval in [Gdk.KEY_Return, Gdk.KEY_KP_Enter] and not (state & Gdk.ModifierType.SHIFT_MASK):
                callback(self)
                return True
            return False
        key_ctrl.connect("key-pressed", on_key)
        self.textview.add_controller(key_ctrl)

class LinexinAISysadminWidget(Gtk.Box):
    # Class-level (display-shared) theme CSS provider. All widget instances
    # share ONE provider so a stale provider from a previously-created instance
    # can never linger on the display and keep applying an old theme's rules
    # (e.g. switching Matrix -> Default left Matrix's border/background because a
    # second instance still had a Matrix provider attached).
    _shared_theme_provider: Optional[Gtk.CssProvider] = None

    def __init__(self, hide_sidebar=False, window=None, sudo_manager=None, voice_autostart=False, conversation_id=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12) # type: ignore
        self.widgetname = "Alexy AI"
        # Scope tag so theme CSS only affects this widget, not the whole app.
        self.add_css_class("alexy-ai-root")
        self.alexy_icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "icons", "github.petexy.alexy.svg")
        if not os.path.isfile(self.alexy_icon_path):
            self.alexy_icon_path = "/usr/share/icons/github.petexy.alexy.svg"
        if os.path.isfile(self.alexy_icon_path):
            self.widgeticon = self.alexy_icon_path
        else:
            self.widgeticon = "utilities-terminal-symbolic"
        self.set_margin_top(4)
        self.set_margin_bottom(50)
        self.set_margin_start(50)
        self.set_margin_end(50)
        self.window = window
        self.hide_sidebar = hide_sidebar
        self.sudo_manager = sudo_manager or globals().get('sudo_manager')
        if not self.sudo_manager:
            self.sudo_manager = SudoManager.get_instance()
        
        self.conv_filter_box = None
        self.arecord_proc: Optional[subprocess.Popen[bytes]] = None
        
        # Default config
        self.backend = "local" # "direct", "local" or "endpoint"
        
        # Direct API Config
        self.api_key = ""
        self.api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        self.model = "qwen-plus"
        
        # Local AI Config
        self.local_model = "qwen3.5"
        self.local_url = "http://localhost:11434/api/chat"

        # Local AI (custom OpenAI-compatible endpoint) Config
        self.endpoint_url = "http://localhost:6767/v1"
        self.endpoint_model = "local-model"
        
        # Voice-to-Text Config
        self.stt_backend = "whisper"  # "whisper" or "vosk"
        self.whisper_model = "small"  # tiny, base, small, medium
        self.vosk_lang = "small-en-us-0.15"
        self.hey_linux_enabled = False
        
        # Per-backend voice correction toggles
        self.voice_correction_direct = False
        
        # Screen Awareness
        self.screen_awareness_active = False
        self.compact_screen_awareness = True  # default: screen awareness ON in compact mode
        self._voice_autostart = voice_autostart  # remember for forced screen awareness
        
        # Security / Safety
        self.auto_execute_commands = True

        # Theme
        self.theme = "default"
        self.theme_data: Dict[str, Any] = {}
        self.theme_dir: Optional[str] = None
        self._theme_css_provider: Optional[Gtk.CssProvider] = None
        
        self.system_prompt = _(
            "You are Alexy, an expert AI Sysadmin running under Linexin - An Arch Linux based operating system. "
            "You have the ability to execute bash commands autonomously. If you need to gather system information or execute a task, "
            "output a codeblock with ```bash containing the exact script. Do NOT output any other text if you output a bash block. "
            "The system will invisibly execute it and return the STDOUT to you. Do NOT run interactive commands like top, htop, or nano. "
            "When installing software, you should prioritize Flatpaks over the system package manager to avoid breaking the base system. Assume the flatpak package is already installed on the system."
            "If there is no flatpak version of what the user is asking for, you should then ONLY use the system package manager to fulfil the request. "
            "If the user wants you to run any program, you should first check if it is installed by searching both installed system packages and installed flatpaks. If it is not installed, you should tell the user that it is not installed and ask them if they want you to install it. "
            "If you need to launch a GUI application, you MUST run it in the background disconnected from stdout like this: `nohup app_name >/dev/null 2>&1 & disown` so it does not block the terminal. "
            "If the user wants you to `Shutdown` / `Turn off` / `Power down`, you MUST run ```bash\nshutdown now\n``` (no sudo needed). If the user wants to `Reboot` / `Restart`, run ```bash\nreboot\n``` (no sudo needed)."
            "You may run multiple queries in sequence. Once you have all the information necessary, provide a final conversational response WITHOUT any bash blocks. "
            "CRITICAL LANGUAGE RULE: You MUST always reply in the same language the user is writing or speaking to you in — determine this ONLY from the user's text messages, NEVER from screenshots, images, screen content, terminal output, or any other visual context. "
            "If the user writes in English, reply in English even if a screenshot shows Polish, German, or any other language. "
            "If the user writes in Polish, reply in Polish. If they write in German, reply in German, etc. "
            "The language of attached images or screen content is completely irrelevant to your reply language — always match the user's text language."
        )
        # Keep an immutable copy of the built-in master prompt so the user can
        # revert their customized prompt back to the default at any time.
        self._default_system_prompt = self.system_prompt
        # Agents: named master-prompt profiles. The built-in "Alexy" agent uses
        # the default prompt and is non-editable; the user can create custom
        # agents with their own prompts. Only custom agents are persisted here.
        self.user_agents: List[Dict[str, str]] = []
        self.active_agent = DEFAULT_AGENT_NAME
        self.chat_history = []
        # Streaming response context (set while an assistant reply streams in).
        self._stream_ctx = None
        self._stream_tick_id = None
        self.current_conversation_id = str(uuid.uuid4())
        self._reset_history()
        
        self.load_config()
        # load_config may have changed the active agent's prompt; rebuild the
        # initial (empty) history so a fresh conversation uses it.
        self._reset_history()
        self._load_theme()

        # Flush pending GTK events so the loading spinner keeps animating
        # and the window stays responsive (closeable) during setup_ui.
        ctx = GLib.MainContext.default()
        while ctx.pending():
            ctx.iteration(False)

        self.setup_ui()

        # Load a specific conversation if requested (e.g. expanding from compact mode)
        if conversation_id:
            GLib.idle_add(self._load_conversation, conversation_id)

        # Auto-activate voice input if launched with --voice flag
        if voice_autostart:
            print("[Screen Awareness] voice_autostart=True, forcing screen awareness ON")
            GLib.idle_add(self.stt_toggle.set_active, True)
            # Always enable screen awareness in compact/voice mode (hey-linux daemon)
            self.screen_awareness_active = True
            GLib.idle_add(self.screen_toggle.set_active, True)

    def _reset_history(self):
        self.chat_history = [{"role": "system", "content": self.system_prompt}]
        # Remember which agent this fresh conversation belongs to.
        self._conv_agent = self.active_agent

    def _agent_names(self):
        """Ordered list of all agent names: built-in agents first, then custom."""
        return list(BUILTIN_AGENT_NAMES) + [a.get("name", "") for a in self.user_agents]

    def _agent_prompt(self, name):
        """Return the master prompt for the named agent (built-in prompt for
        built-in agents, default for unknown names)."""
        if name == DEFAULT_AGENT_NAME:
            return self._default_system_prompt
        if name == RHYMEXY_AGENT_NAME:
            return RHYMEXY_PROMPT
        for a in self.user_agents:
            if a.get("name") == name:
                return a.get("prompt", self._default_system_prompt)
        return self._default_system_prompt

    def _apply_active_agent(self):
        """Sync self.system_prompt to the active agent's prompt."""
        self.system_prompt = self._agent_prompt(self.active_agent)

    def _clear_chat_ui(self):
        """Remove all message bubbles from the chat listbox."""
        while True:
            row = self.chat_listbox.get_row_at_index(0)
            if row is None:
                break
            self.chat_listbox.remove(row)
        self._last_bubble_role = None
        self._last_bubble_box = None

    def _get_conversations_dir(self):
        os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
        return CONVERSATIONS_DIR

    def _generate_title(self, chat_history):
        """Extract the first user message as a conversation title."""
        for msg in chat_history:
            if msg["role"] == "user":
                stripped = self._strip_system_instructions(msg["content"])
                text = self._extract_text_from_content(stripped)
                title = text.strip().replace("\n", " ")
                if not title:
                    title = _("Image")
                return title[:50] + ("..." if len(title) > 50 else "")
        return _("New Conversation")

    def _save_conversation(self):
        """Persist current chat_history to a JSON file."""
        # Don't save if only system prompt exists (no user messages)
        if len(self.chat_history) <= 1:
            return
        conv_dir = self._get_conversations_dir()
        from datetime import datetime
        conv_data = {
            "id": self.current_conversation_id,
            "title": self._generate_title(self.chat_history),
            "created": getattr(self, '_conv_created', datetime.now().isoformat()),
            "updated": datetime.now().isoformat(),
            "backend": self.backend,
            "agent": getattr(self, '_conv_agent', self.active_agent),
            "chat_history": self.chat_history
        }
        if not hasattr(self, '_conv_created'):
            self._conv_created = conv_data["created"]
        filepath = os.path.join(conv_dir, f"{self.current_conversation_id}.json")
        try:
            with open(filepath, 'w') as f:
                json.dump(conv_data, f, indent=2)
        except Exception as e:
            print(f"Error saving conversation: {e}")

    def _load_conversation(self, conv_id):
        """Load a conversation from disk, replace chat_history and rebuild UI."""
        filepath = os.path.join(self._get_conversations_dir(), f"{conv_id}.json")
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, 'r') as f:
                conv_data = json.load(f)
        except Exception as e:
            print(f"Error loading conversation: {e}")
            return
        # Save current conversation before switching
        self._save_conversation()
        self.current_conversation_id = conv_data["id"]
        self.chat_history = conv_data["chat_history"]
        self._conv_created = conv_data.get("created", "")
        # Remember the agent this conversation was created with (for re-saving).
        self._conv_agent = conv_data.get("agent", DEFAULT_AGENT_NAME)
        # Restore the backend the conversation was created with
        saved_backend = conv_data.get("backend", self.backend)
        # Migrate removed qwen_cli backend to direct
        if saved_backend == "qwen_cli":
            saved_backend = "direct"
        if saved_backend != self.backend:
            self.backend = saved_backend
            self.update_subtitle()
        # Rebuild the chat UI, skipping internal system/command messages
        self._clear_chat_ui()
        import re as _re
        for msg in self.chat_history:
            if msg["role"] == "user":
                # Skip internal command execution results injected by _run_autonomous_commands
                text = self._extract_text_from_content(msg["content"])
                if text.startswith("System Command Execution Results:"):
                    continue
                self.add_message_bubble("user", self._strip_system_instructions(msg["content"]))
            elif msg["role"] == "assistant":
                # Skip assistant replies that are purely bash code blocks (autonomous commands)
                stripped = msg["content"].strip()
                if _re.fullmatch(r'```(?:bash|sh)\n.*?```', stripped, _re.DOTALL):
                    continue
                self.add_message_bubble("assistant", msg["content"])

    def _list_conversations(self, backend_filter=None):
        """Return a list of (id, title, updated, agent) sorted by agent then by
        most recently updated. If backend_filter is given, only return
        conversations for that backend."""
        conv_dir = self._get_conversations_dir()
        conversations = []
        for filename in os.listdir(conv_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(conv_dir, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                if backend_filter:
                    conv_backend = data.get("backend")
                    # Migrate removed qwen_cli backend to direct
                    if conv_backend == "qwen_cli":
                        conv_backend = "direct"
                    if conv_backend != backend_filter:
                        continue
                conversations.append((
                    data.get("id", filename.replace(".json", "")),
                    data.get("title", _("Untitled")),
                    data.get("updated", ""),
                    data.get("agent", DEFAULT_AGENT_NAME)
                ))
            except Exception:
                continue
        # Group by agent (built-in agents first in their fixed order, then
        # custom agents alphabetically), most recent conversation first within
        # each agent group.
        def _agent_sort_key(x):
            ag = x[3] or DEFAULT_AGENT_NAME
            if ag in BUILTIN_AGENT_NAMES:
                return (0, BUILTIN_AGENT_NAMES.index(ag), "")
            return (1, 0, ag.lower())
        conversations.sort(key=lambda x: x[2], reverse=True)
        conversations.sort(key=_agent_sort_key)
        return conversations

    def _delete_conversation(self, conv_id):
        """Delete a conversation file from disk."""
        filepath = os.path.join(self._get_conversations_dir(), f"{conv_id}.json")
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception as e:
            print(f"Error deleting conversation: {e}")

    def _rename_conversation(self, conv_id, new_title):
        """Rename a conversation's title on disk."""
        filepath = os.path.join(self._get_conversations_dir(), f"{conv_id}.json") # type: ignore
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            data["title"] = new_title.strip()
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error renaming conversation: {e}")

    def on_new_conversation_clicked(self, button=None):
        """Save current conversation and start a new one."""
        self._save_conversation()
        self.current_conversation_id = str(uuid.uuid4())
        if hasattr(self, '_conv_created'):
            del self._conv_created
        self._reset_history()
        self._clear_chat_ui()
        self.add_message_bubble("assistant", _("Hello! I am Alexy. How can I help you today?"))

    def on_conversations_toggled(self, button):
        """Toggle the conversations list (left sidebar or full-width panel)."""
        if button.get_active():
            self._rebuild_conv_list()
            self._show_conv()
        else:
            self._hide_conv()

    def _should_use_conv_sidebar(self):
        """Sidebar (push) mode needs Linexin Center compact mode AND a wide widget.

        When Linexin Center is compact its own sidebar shrinks to an icon strip,
        leaving room for Alexy to show conversations as a left push-sidebar beside
        the chat. Otherwise the list slides over the chat as an overlay panel
        (like the settings sidebar)."""
        win = getattr(self, 'window', None)
        compact = bool(getattr(win, '_compact_mode', False)) if win is not None else False
        width = self.get_width()
        if width <= 0:
            width = self.get_allocated_width()
        return compact and width > 1000

    def _attach_conv(self, mode):
        """Move conv_page into the revealer for the requested mode ('sidebar'
        push layout, or 'overlay' floating panel)."""
        target = self.conv_sidebar_revealer if mode == "sidebar" else self.conv_overlay_revealer
        other = self.conv_overlay_revealer if mode == "sidebar" else self.conv_sidebar_revealer
        if target.get_child() is self.conv_page:
            return
        if other.get_child() is self.conv_page:
            other.set_reveal_child(False)
            other.set_child(None)
        target.set_child(self.conv_page)

    def _show_conv(self):
        """Reveal the conversations list, adapting layout to the window size."""
        # Conversations and settings are mutually exclusive.
        if getattr(self, 'settings_revealer', None) is not None and \
                self.settings_revealer.get_reveal_child():
            self._close_settings_sidebar()

        if self._should_use_conv_sidebar():
            # Left push-sidebar: fixed width, chat stays beside it and adapts.
            self._attach_conv("sidebar")
            self.conv_page.set_size_request(340, -1)
            self.conv_page.set_hexpand(False)
            self.conv_page.add_css_class("conv-sidebar")
            self.conv_page.remove_css_class("conv-panel")
            self.conv_scrim.set_visible(False)
            self.conv_overlay_revealer.set_reveal_child(False)
            self.conv_sidebar_revealer.set_reveal_child(True)
        else:
            # Overlay panel that slides over the chat (like the settings sidebar).
            self._attach_conv("overlay")
            self.conv_page.set_size_request(360, -1)
            self.conv_page.set_hexpand(False)
            self.conv_page.add_css_class("conv-panel")
            self.conv_page.remove_css_class("conv-sidebar")
            self.conv_sidebar_revealer.set_reveal_child(False)
            self.conv_scrim.set_visible(True)
            self.conv_overlay_revealer.set_reveal_child(True)
        self.main_stack.set_visible(True)
        self.new_conv_btn.set_sensitive(True)

    def _hide_conv(self):
        """Hide the conversations list and restore the chat view."""
        self.conv_sidebar_revealer.set_reveal_child(False)
        self.conv_overlay_revealer.set_reveal_child(False)
        self.conv_scrim.set_visible(False)
        self.main_stack.set_visible(True)
        self.new_conv_btn.set_sensitive(True)

    def _on_conv_window_resize(self, *args):
        """Re-place the conversations list when the window is resized while it
        is open, so it switches between push-sidebar and overlay on the fly."""
        if not hasattr(self, 'conv_toggle_btn') or not self.conv_toggle_btn.get_active():
            return
        self._show_conv()

    def _rebuild_conv_list(self):
        """Populate the inline conversations list with backend filter."""
        # Discover which backends have saved conversations
        all_conversations = self._list_conversations()
        backend_labels = {
            "direct": _("Online API"),
            "local": _("Local AI"),
            "endpoint": _("Local AI (Endpoint)")
        }
        available_backends = set()
        for conv_id, title, updated, agent in all_conversations:
            filepath = os.path.join(self._get_conversations_dir(), f"{conv_id}.json")
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                available_backends.add(data.get("backend", ""))
            except Exception:
                pass
        # Migrate removed qwen_cli backend to direct
        if "qwen_cli" in available_backends:
            available_backends.discard("qwen_cli")
            available_backends.add("direct")
        available_backends.add(self.backend)

        # Destroy old filter bar and create a fresh one
        if self.conv_filter_box and self.conv_filter_box.get_parent():
            self.conv_page.remove(self.conv_filter_box)

        self.conv_filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.conv_filter_box.set_halign(Gtk.Align.CENTER) # type: ignore
        self.conv_filter_box.set_margin_bottom(4) # type: ignore
        self.conv_page.prepend(self.conv_filter_box)

        if not hasattr(self, '_conv_active_filter'):
            self._conv_active_filter = self.backend
        # Migrate removed qwen_cli filter
        if self._conv_active_filter == "qwen_cli":
            self._conv_active_filter = "direct"

        if len(available_backends) > 1:
            filter_label = Gtk.Label(label=_("Backend:"))
            filter_label.add_css_class("dim-label")
            self.conv_filter_box.append(filter_label) # type: ignore

            btn_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            btn_group.add_css_class("linked")

            first_btn = None
            backend_order = ["direct", "local", "endpoint"]
            for backend_key in backend_order:
                if backend_key not in available_backends:
                    continue
                btn = Gtk.ToggleButton(label=backend_labels.get(backend_key, backend_key))
                if backend_key == self._conv_active_filter:
                    btn.set_active(True)
                if first_btn is not None:
                    btn.set_group(first_btn)
                else:
                    first_btn = btn

                def make_filter_handler(bk):
                    def handler(b):
                        if b.get_active():
                            self._conv_active_filter = bk
                            self._populate_conv_rows()
                    return handler

                btn.connect("toggled", make_filter_handler(backend_key))
                btn_group.append(btn)

            self.conv_filter_box.append(btn_group) # type: ignore
            self.conv_filter_box.set_visible(True) # type: ignore
        else:
            self.conv_filter_box.set_visible(False) # type: ignore
            self._conv_active_filter = self.backend

        self._populate_conv_rows()

    def _populate_conv_rows(self):
        """Fill the inline conversation listbox for the active backend filter."""
        # Clear existing rows
        while True:
            row = self.conv_listbox.get_row_at_index(0)
            if row is None:
                break
            self.conv_listbox.remove(row)

        conversations = self._list_conversations(backend_filter=self._conv_active_filter)
        self.conv_empty_label.set_visible(len(conversations) == 0)
        self.conv_scrolled.set_visible(len(conversations) > 0)

        last_agent = None
        for conv_id, title, updated, agent in conversations:
            agent = agent or DEFAULT_AGENT_NAME
            # Insert a non-selectable header row whenever the agent changes so
            # the user can tell which agent each conversation belongs to.
            if agent != last_agent:
                last_agent = agent
                header_row = Gtk.ListBoxRow()
                header_row.set_selectable(False)
                header_row.set_activatable(False)
                header_lbl = Gtk.Label(label=agent)
                header_lbl.add_css_class("heading")
                header_lbl.add_css_class("dim-label")
                header_lbl.set_halign(Gtk.Align.START)
                header_lbl.set_margin_top(10)
                header_lbl.set_margin_bottom(2)
                header_lbl.set_margin_start(6)
                header_row.set_child(header_lbl)
                self.conv_listbox.append(header_row)

            row = Adw.ActionRow(title=title)
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(updated)
                row.set_subtitle(dt.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                row.set_subtitle(updated)

            if conv_id == self.current_conversation_id:
                row.add_prefix(Gtk.Image.new_from_icon_name(
                    _icon("emblem-ok-symbolic", "emblem-ok", "object-select-symbolic", "object-select")))

            # Edit (rename) button
            edit_btn = Gtk.Button()
            _set_button_icon(edit_btn, "document-edit-symbolic", "document-edit",
                             "edit-rename-symbolic", "edit-rename", text_fallback="\u270e")
            edit_btn.set_valign(Gtk.Align.CENTER)
            edit_btn.add_css_class("flat")
            edit_btn.set_focusable(False)

            def make_edit_handler(cid, t, r):
                def handler(btn):
                    idx = r.get_index()
                    edit_row = Adw.EntryRow(title=_("Rename conversation"))
                    edit_row.set_text(t)
                    edit_row.add_css_class("boxed-list")

                    cancel_btn = Gtk.Button()
                    _set_button_icon(cancel_btn, "window-close-symbolic", "window-close",
                                     "dialog-close", text_fallback="\u2715")
                    cancel_btn.set_valign(Gtk.Align.CENTER)
                    cancel_btn.add_css_class("flat")

                    def on_cancel(b):
                        self._populate_conv_rows()

                    cancel_btn.connect("clicked", on_cancel)
                    edit_row.add_suffix(cancel_btn)

                    def on_apply(entry):
                        new_title = entry.get_text().strip()
                        if new_title:
                            self._rename_conversation(cid, new_title)
                        self._populate_conv_rows()

                    edit_row.connect("apply", on_apply)
                    edit_row.connect("entry-activated", on_apply)

                    self.conv_listbox.remove(r)
                    self.conv_listbox.insert(edit_row, idx)
                    edit_row.grab_focus()
                return handler

            edit_btn.connect("clicked", make_edit_handler(conv_id, title, row))
            row.add_suffix(edit_btn)

            # Delete button
            delete_btn = Gtk.Button()
            _set_button_icon(delete_btn, "user-trash-symbolic", "user-trash",
                             "edit-delete-symbolic", "edit-delete", text_fallback="\U0001f5d1")
            delete_btn.set_valign(Gtk.Align.CENTER)
            delete_btn.add_css_class("flat")
            delete_btn.add_css_class("error")
            delete_btn.set_focusable(False)

            def make_delete_handler(cid):
                def handler(btn):
                    self._delete_conversation(cid)
                    if cid == self.current_conversation_id:
                        # Reset state WITHOUT saving (to avoid re-creating the deleted file)
                        self.current_conversation_id = str(uuid.uuid4())
                        if hasattr(self, '_conv_created'):
                            del self._conv_created
                        self._reset_history()
                        self._clear_chat_ui()
                        self.add_message_bubble("assistant", _("Hello! I am Alexy. How can I help you today?"))
                    self._populate_conv_rows()
                return handler

            delete_btn.connect("clicked", make_delete_handler(conv_id))
            row.add_suffix(delete_btn)

            # Click row to load conversation
            def make_load_handler(cid):
                def handler(r):
                    self._load_conversation(cid)
                    self.conv_toggle_btn.set_active(False)
                return handler

            row.set_activatable(True)
            row.connect("activated", make_load_handler(conv_id))
            self.conv_listbox.append(row)

    def resize_window_deferred(self):
        if self.window:
            try:
                self.window.set_default_size(800, 600)
            except Exception as e:
                print(f"Failed to resize window: {e}")
        return False

    def _discover_themes(self) -> List[Dict[str, Any]]:
        """Scan bundled and user theme directories, return list of theme info dicts."""
        themes: List[Dict[str, Any]] = []
        seen_ids: set = set()
        for themes_root in [BUNDLED_THEMES_DIR, USER_THEMES_DIR]:
            if not os.path.isdir(themes_root):
                continue
            for entry in sorted(os.listdir(themes_root)):
                theme_path = os.path.join(themes_root, entry)
                manifest = os.path.join(theme_path, "theme.json")
                if not os.path.isfile(manifest):
                    continue
                try:
                    with open(manifest, 'r') as f:
                        data = json.load(f)
                    theme_id = entry  # folder name is the theme id
                    if theme_id in seen_ids:
                        continue  # user themes don't override bundled ones by id
                    seen_ids.add(theme_id)
                    themes.append({
                        "id": theme_id,
                        "path": theme_path,
                        "name": data.get("name", theme_id),
                        "author": data.get("author", _("Unknown")),
                        "description": data.get("description", ""),
                        "version": data.get("version", "1.0"),
                        "css": data.get("css", {})
                    })
                except Exception as e:
                    print(f"Error reading theme {entry}: {e}")
        return themes

    def _load_theme(self):
        """Load the currently selected theme's assets and apply CSS overrides."""
        themes = self._discover_themes()
        # Find matching theme by id
        chosen = None
        for t in themes:
            if t["id"] == self.theme:
                chosen = t
                break
        if not chosen and themes:
            chosen = themes[0]  # fallback to first available
        if not chosen:
            self.theme_data = {}
            self.theme_dir = None
            return

        self.theme_data = chosen
        self.theme_dir = chosen["path"]

        # Base CSS for the widget (GTK CSS uses margin-left/right, NOT margin-start/end)
        # Spacing goes on the inner box, NOT the row — so default boxed-list separators align
        css_text = """
        box.message-box { margin-top: 10px; margin-bottom: 10px; margin-left: 12px; margin-right: 12px; }
        .settings-sidebar { background-color: @window_bg_color; border-left: 1px solid @borders; padding-left: 12px; }
        .settings-sidebar-header { padding: 10px 14px; border-bottom: 1px solid @borders; }
        .settings-switcher-bar { padding: 8px 14px 0px 14px; }
        .settings-scrim { background-color: transparent; }
        .conv-sidebar { border-right: 1px solid @borders; margin-right: 6px; padding-right: 12px; }
        .conv-panel { background-color: @window_bg_color; border-right: 1px solid @borders; padding-right: 12px; padding-left: 4px; }
        """

        # Apply CSS overrides from theme.json (legacy support)
        css_overrides = chosen.get("css", {})
        assistant_bg = css_overrides.get("assistant_bubble_bg", "")
        user_bg = css_overrides.get("user_bubble_bg", "")
        accent = css_overrides.get("accent_color", "")
        if assistant_bg:
            css_text += f"box.assistant-bubble {{ background-color: {assistant_bg}; }}\n"
        if user_bg:
            css_text += f"box.user-bubble {{ background-color: {user_bg}; }}\n"
        if accent:
            css_text += f"button.suggested-action {{ background-color: {accent}; }}\n"

        # Apply comprehensive custom stylesheet if present
        if self.theme_dir is not None:
            style_path = os.path.join(str(self.theme_dir), "style.css")
            if os.path.isfile(style_path):
                try:
                    with open(style_path, "r") as f:
                        css_text += "\n" + f.read()
                except Exception as e:
                    print(f"Error loading theme style.css: {e}")

        from gi.repository import Gdk  # type: ignore
        display = Gdk.Display.get_default()

        if display:
            # Scope every rule under this widget's root class so the theme only
            # styles the Alexy AI widget and never leaks into the rest of the
            # Linexin Center app sharing the same display.
            scoped_css = self._scope_css(css_text, ".alexy-ai-root")
            # Use a SINGLE display-shared provider for ALL widget instances and
            # just reload its data on each theme change. Reloading an attached
            # provider fully replaces the previous CSS and reliably invalidates
            # styles across the display. A class-level singleton also prevents a
            # stale provider from a previously-created instance lingering on the
            # display and keeping an old theme's rules applied (the cause of
            # Matrix styling persisting after switching to Default).
            cls = LinexinAISysadminWidget
            if cls._shared_theme_provider is None:
                cls._shared_theme_provider = Gtk.CssProvider()
                Gtk.StyleContext.add_provider_for_display(
                    display, cls._shared_theme_provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1  # type: ignore
                )
            cls._shared_theme_provider.load_from_data(scoped_css.encode("utf-8"))
            self._theme_css_provider = cls._shared_theme_provider

    def _scope_css(self, css_text: str, scope: str) -> str:
        """Prefix every CSS selector with ``scope`` so the rules only apply to
        widgets inside the scoped subtree.

        This is a lightweight, brace-aware transformer (not a full CSS parser).
        It handles comma-separated selectors and nested conditional at-rules
        (@media/@supports/@container), and leaves other at-rules
        (@keyframes/@font-face/@import/@define-color) untouched.
        """
        import re
        # Strip comments to simplify parsing.
        css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
        result = []
        pos = 0
        length = len(css_text)
        while pos < length:
            brace = css_text.find("{", pos)
            if brace == -1:
                tail = css_text[pos:].strip()
                if tail:
                    result.append(tail)
                break
            selector = css_text[pos:brace]
            # Find the matching closing brace, accounting for nesting.
            depth = 1
            j = brace + 1
            while j < length and depth > 0:
                if css_text[j] == "{":
                    depth += 1
                elif css_text[j] == "}":
                    depth -= 1
                j += 1
            body = css_text[brace:j]  # includes the outer { ... }
            sel = selector.strip()
            if sel.startswith("@"):
                keyword = sel.split()[0].lower() if sel else ""
                if keyword in ("@media", "@supports", "@container"):
                    inner = self._scope_css(body[1:-1], scope)
                    result.append(f"{sel} {{{inner}}}")
                else:
                    # Non-conditional at-rules apply globally by design.
                    result.append(sel + " " + body)
            elif sel:
                parts = [p.strip() for p in sel.split(",") if p.strip()]
                scoped_parts = []
                for p in parts:
                    # Descendant form: matches widgets inside the root.
                    variants = [f"{scope} {p}"]
                    # Same-element form: lets selectors whose first compound is a
                    # class/id/pseudo/attribute (e.g. ".dark ...", applied to the
                    # root widget itself) keep matching after scoping.
                    if p[:1] in ".#:[":
                        variants.append(f"{scope}{p}")
                    scoped_parts.extend(variants)
                result.append(", ".join(scoped_parts) + " " + body)
            pos = j
        return "\n".join(result)

    def _get_theme_svg(self, filename: str) -> Optional[str]:
        """Return absolute path to a theme SVG file, or None if it doesn't exist."""
        theme_dir = self.theme_dir
        if theme_dir is not None:
            path = os.path.join(theme_dir, filename)
            if os.path.isfile(path):
                return path
        return None

    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
                    self.backend = config.get("backend", self.backend)
                    # Migrate removed qwen_cli backend to direct
                    if self.backend == "qwen_cli":
                        self.backend = "direct"
                    self.api_key = config.get("api_key", self.api_key)
                    self.api_url = config.get("api_url", self.api_url)
                    self.model = config.get("model", self.model)
                    self.local_model = config.get("local_model", self.local_model)
                    self.endpoint_url = config.get("endpoint_url", self.endpoint_url)
                    self.endpoint_model = config.get("endpoint_model", self.endpoint_model)
                    self.system_prompt = config.get("system_prompt", self.system_prompt)
                    # Agents
                    loaded_agents = config.get("user_agents", [])
                    if isinstance(loaded_agents, list):
                        self.user_agents = [
                            {"name": str(a.get("name", "")), "prompt": str(a.get("prompt", ""))}
                            for a in loaded_agents
                            if isinstance(a, dict) and a.get("name")
                            and a.get("name") not in BUILTIN_AGENT_NAMES
                        ]
                    self.active_agent = config.get("active_agent", DEFAULT_AGENT_NAME)
                    # Migrate a legacy customized master prompt (pre-agents) into
                    # a dedicated "Custom" agent so it is not lost.
                    legacy_prompt = config.get("system_prompt", "")
                    if (legacy_prompt and legacy_prompt != self._default_system_prompt
                            and not self.user_agents
                            and "active_agent" not in config):
                        self.user_agents.append({"name": _("Custom"), "prompt": legacy_prompt})
                        self.active_agent = _("Custom")
                    # Ensure the active agent still exists, then sync the prompt.
                    if self.active_agent not in self._agent_names():
                        self.active_agent = DEFAULT_AGENT_NAME
                    self._apply_active_agent()
                    self.stt_backend = config.get("stt_backend", "whisper")
                    self.whisper_model = config.get("whisper_model", "small")
                    self.vosk_lang = config.get("vosk_lang", "small-en-us-0.15")
                    self.hey_linux_enabled = config.get("hey_linux_enabled", False)
                    self.voice_correction_direct = config.get("voice_correction_direct", False)
                    self.auto_execute_commands = config.get("auto_execute_commands", True)
                    self.compact_screen_awareness = config.get("compact_screen_awareness", True)
                    self.theme = config.get("theme", "default")
            except Exception as e:
                print(f"Error loading config: {e}")

    def save_config(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump({
                    "backend": self.backend,
                    "api_key": self.api_key,
                    "api_url": self.api_url,
                    "model": self.model,
                    "local_model": self.local_model,
                    "endpoint_url": self.endpoint_url,
                    "endpoint_model": self.endpoint_model,
                    "system_prompt": self.system_prompt,
                    "user_agents": self.user_agents,
                    "active_agent": self.active_agent,
                    "stt_backend": self.stt_backend,
                    "whisper_model": self.whisper_model,
                    "vosk_lang": self.vosk_lang,
                    "hey_linux_enabled": self.hey_linux_enabled,
                    "voice_correction_direct": self.voice_correction_direct,
                    "auto_execute_commands": self.auto_execute_commands,
                    "compact_screen_awareness": self.compact_screen_awareness,
                    "theme": self.theme
                }, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def setup_ui(self):
        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_bottom(8)
        
        # The Alexy AI icon is intentionally NOT themeable: it always shows the
        # agent's own icon regardless of the selected theme.
        if os.path.isfile(self.alexy_icon_path):
            system_icon = Gtk.Image.new_from_file(self.alexy_icon_path)
        else:
            system_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        system_icon.set_pixel_size(64)
        self.header_icon_widget = system_icon
        header_box.append(system_icon)
        
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_hexpand(True)
        title_box.set_valign(Gtk.Align.CENTER)
        
        title_label = Gtk.Label(label=_("Alexy AI"))
        title_label.add_css_class("title-1")
        title_label.set_halign(Gtk.Align.START)
        title_box.append(title_label)
        
        self.subtitle_label = Gtk.Label()
        self.update_subtitle()
        self.subtitle_label.add_css_class("title-4")
        self.subtitle_label.add_css_class("dim-label")
        self.subtitle_label.set_halign(Gtk.Align.START)
        title_box.append(self.subtitle_label)
        
        header_box.append(title_box)

        # New conversation button.
        # NOTE: we deliberately render the "+" as a text label rather than the
        # "list-add-symbolic" icon. Some icon themes (e.g. Tela, Breeze on KDE
        # Plasma) ship a list-add-symbolic SVG that GTK4 fails to render
        # (a <style>/transform combination that paints nothing), so the button
        # appeared empty. A label is theme-independent and adapts to light/dark.
        self.new_conv_btn = Gtk.Button()
        _plus_label = Gtk.Label()
        _plus_label.set_markup('<span size="x-large" weight="bold">+</span>')
        self.new_conv_btn.set_child(_plus_label)
        self.new_conv_btn.set_valign(Gtk.Align.CENTER)
        self.new_conv_btn.add_css_class("circular")
        self.new_conv_btn.set_tooltip_text(_("Start a new conversation"))
        self.new_conv_btn.connect("clicked", self.on_new_conversation_clicked)
        header_box.append(self.new_conv_btn)

        # Conversations toggle button
        self.conv_toggle_btn = Gtk.ToggleButton()
        _set_button_icon(self.conv_toggle_btn, "view-list-symbolic", "view-list",
                         "view-list-details-symbolic", "view-list-details", text_fallback="\u2630")
        self.conv_toggle_btn.set_valign(Gtk.Align.CENTER)
        self.conv_toggle_btn.add_css_class("circular")
        self.conv_toggle_btn.set_tooltip_text(_("Browse saved conversations"))
        self.conv_toggle_btn.connect("toggled", self.on_conversations_toggled)
        header_box.append(self.conv_toggle_btn)

        # Settings button
        self.settings_btn = Gtk.Button()
        _set_button_icon(self.settings_btn, "emblem-system-symbolic", "emblem-system",
                         "preferences-system-symbolic", "preferences-system",
                         "configure", text_fallback="\u2699")
        self.settings_btn.set_valign(Gtk.Align.CENTER)
        self.settings_btn.add_css_class("circular")
        self.settings_btn.connect("clicked", self.on_settings_clicked)
        header_box.append(self.settings_btn)

        # Content column (chat header + main stack). Wrapped in an overlay below
        # so the settings sidebar can slide in over it from the right.
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._content_box.set_vexpand(True)
        self._content_box.append(header_box)

        # Main content stack (chat page). The conversations list is shown either
        # as a left sidebar (Linexin Center compact mode + wide widget) or as a
        # full-width panel that hides the chat; see _show_conv()/_hide_conv().
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.main_stack.set_transition_duration(200)
        self.main_stack.set_vexpand(True)
        self.main_stack.set_hexpand(True)

        # === Conversations Page ===
        self.conv_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        # Conversation list
        self.conv_scrolled = Gtk.ScrolledWindow()
        self.conv_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.conv_scrolled.set_vexpand(True)

        self.conv_listbox = Gtk.ListBox()
        self.conv_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.conv_listbox.add_css_class("boxed-list")
        self.conv_scrolled.set_child(self.conv_listbox)
        self.conv_page.append(self.conv_scrolled)

        self.conv_empty_label = Gtk.Label(label=_("No saved conversations yet."))
        self.conv_empty_label.set_margin_top(40)
        self.conv_empty_label.add_css_class("dim-label")
        self.conv_empty_label.set_visible(False)
        self.conv_page.append(self.conv_empty_label)

        # Left conversations sidebar revealer. The same conv_page is reused for
        # the full-width panel mode (it just fills the area and hides the chat).
        self.conv_sidebar_revealer = Gtk.Revealer()
        self.conv_sidebar_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.conv_sidebar_revealer.set_transition_duration(250)
        self.conv_sidebar_revealer.set_halign(Gtk.Align.START)
        self.conv_sidebar_revealer.set_valign(Gtk.Align.FILL)
        self.conv_sidebar_revealer.set_reveal_child(False)
        self.conv_sidebar_revealer.set_child(self.conv_page)

        # Horizontal split: [conversations sidebar | chat stack]. The revealer
        # animates its width, so the chat content adapts (is pushed) smoothly.
        content_split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        content_split.set_vexpand(True)
        content_split.append(self.conv_sidebar_revealer)
        content_split.append(self.main_stack)
        self._content_box.append(content_split)

        # === Chat Page ===
        chat_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.chat_listbox = Gtk.ListBox()
        self.chat_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.chat_listbox.add_css_class("boxed-list")

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_child(self.chat_listbox)
        self.scrolled_window.set_vexpand(True)
        chat_page.append(self.scrolled_window)

        # Image Preview Strip (shown when images are attached)
        self.pending_images = []  # list of (mime_type, base64_data) tuples
        self.image_preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.image_preview_box.set_margin_top(6)
        self.image_preview_box.set_margin_start(4)
        self.image_preview_box.set_visible(False)
        chat_page.append(self.image_preview_box)

        # Input Area
        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_box.set_margin_top(12)

        self.entry = MultilineEntry()
        self.entry.set_placeholder_text(_("Ask a question..."))
        self.entry.set_hexpand(True)
        self.entry.connect_activate(self.on_send_clicked)
        input_box.append(self.entry)

        # Image paste handler (Ctrl+V)
        paste_ctrl = Gtk.EventControllerKey.new()
        paste_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        def _on_paste_key(ctrl, keyval, keycode, state):
            from gi.repository import Gdk  # type: ignore
            if keyval == Gdk.KEY_v and (state & Gdk.ModifierType.CONTROL_MASK):
                clipboard = self.entry.textview.get_clipboard()
                formats = clipboard.get_formats()
                for mime in ["image/png", "image/jpeg", "image/bmp", "image/gif", "image/tiff"]:
                    if formats.contain_mime_type(mime):
                        clipboard.read_texture_async(None, self._on_clipboard_texture_ready)
                        return True
            return False
        paste_ctrl.connect("key-pressed", _on_paste_key)
        self.entry.textview.add_controller(paste_ctrl)

        # Drag & Drop handlers for images
        from gi.repository import Gdk  # type: ignore
        drop_target_file = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target_file.connect("drop", self._on_file_list_drop)
        self.entry.add_controller(drop_target_file)

        drop_target_texture = Gtk.DropTarget.new(Gdk.Texture, Gdk.DragAction.COPY)
        drop_target_texture.connect("drop", self._on_texture_drop)
        self.entry.add_controller(drop_target_texture)

        self._icon_send = _icon("mail-send-symbolic", "mail-send",
                                "document-send-symbolic", "document-send", "go-next-symbolic")
        self._icon_stop = _icon("media-playback-stop-symbolic", "media-playback-stop",
                                "process-stop-symbolic", "process-stop")
        self.send_btn = Gtk.Button(icon_name=self._icon_send)
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_size_request(40, 40)
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", self.on_send_clicked)
        input_box.append(self.send_btn)

        self.stt_toggle = Gtk.ToggleButton()
        self.stt_icon = Gtk.Image.new_from_icon_name(
            _icon("audio-input-microphone-symbolic", "audio-input-microphone"))
        self.stt_toggle.set_child(self.stt_icon)
        self.stt_toggle.set_size_request(40, 40)
        self.stt_toggle.set_valign(Gtk.Align.END)
        self.stt_toggle.connect("toggled", self.on_stt_toggled)
        # Load custom microphone icon from theme if available
        mic_svg = self._get_theme_svg("microphone-icon.svg")
        if mic_svg:
            self.stt_icon.set_from_file(mic_svg)
        self._check_stt_availability()
        input_box.append(self.stt_toggle)

        # Screen Awareness toggle button
        self.screen_toggle = Gtk.ToggleButton()
        self.screen_toggle_icon = Gtk.Image.new_from_icon_name(
            _icon("computer-symbolic", "computer", "video-display-symbolic", "video-display"))
        self.screen_toggle.set_child(self.screen_toggle_icon)
        self.screen_toggle.set_size_request(40, 40)
        self.screen_toggle.set_valign(Gtk.Align.END)
        self.screen_toggle.set_tooltip_text(_("Screen Awareness: include a screenshot with your message"))
        self.screen_toggle.connect("toggled", self._on_screen_toggle)
        input_box.append(self.screen_toggle)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        input_box.append(self.spinner)

        chat_page.append(input_box)
        self.main_stack.add_named(chat_page, "chat")
        self.main_stack.set_visible_child_name("chat")

        # Wrap the content column in an overlay and add the sliding settings
        # sidebar (revealer) plus a dim scrim behind it.
        self.root_overlay = Gtk.Overlay()
        self.root_overlay.set_child(self._content_box)

        # Conversations overlay panel (used in non-compact / narrow mode). It
        # slides over the chat like the settings sidebar and never affects the
        # chat's layout size. In compact + wide mode the same conv_page is moved
        # into the left push-sidebar (conv_sidebar_revealer) instead.
        self.conv_scrim = Gtk.Box()
        self.conv_scrim.add_css_class("settings-scrim")
        self.conv_scrim.set_visible(False)
        _conv_scrim_click = Gtk.GestureClick()
        _conv_scrim_click.connect("released", lambda *a: self.conv_toggle_btn.set_active(False))
        self.conv_scrim.add_controller(_conv_scrim_click)
        self.root_overlay.add_overlay(self.conv_scrim)

        self.conv_overlay_revealer = Gtk.Revealer()
        self.conv_overlay_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.conv_overlay_revealer.set_transition_duration(250)
        self.conv_overlay_revealer.set_halign(Gtk.Align.START)
        self.conv_overlay_revealer.set_valign(Gtk.Align.FILL)
        self.conv_overlay_revealer.set_reveal_child(False)
        self.root_overlay.add_overlay(self.conv_overlay_revealer)

        # Dim scrim behind the sidebar; clicking it dismisses the sidebar.
        self.settings_scrim = Gtk.Box()
        self.settings_scrim.add_css_class("settings-scrim")
        self.settings_scrim.set_visible(False)
        _scrim_click = Gtk.GestureClick()
        _scrim_click.connect("released", lambda *a: self._close_settings_sidebar())
        self.settings_scrim.add_controller(_scrim_click)
        self.root_overlay.add_overlay(self.settings_scrim)

        # Right-hand sliding settings sidebar.
        self.settings_revealer = Gtk.Revealer()
        self.settings_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        self.settings_revealer.set_transition_duration(250)
        self.settings_revealer.set_halign(Gtk.Align.END)
        self.settings_revealer.set_valign(Gtk.Align.FILL)
        self.settings_revealer.set_reveal_child(False)
        self.root_overlay.add_overlay(self.settings_revealer)

        self.append(self.root_overlay)

        # Re-evaluate the conversations layout (sidebar vs overlay) live when the
        # window is resized while the list is open.
        if getattr(self, 'window', None) is not None:
            try:
                self.window.connect("notify::default-width", self._on_conv_window_resize)
            except Exception:
                pass
        self.connect("map", lambda *a: self._on_conv_window_resize())
        style_manager = Adw.StyleManager.get_default()
        def _on_dark_changed(*_args):
            if style_manager.get_dark():
                self.add_css_class("dark")
                self.main_stack.add_css_class("dark")
                if hasattr(self, 'chat_listbox'):
                    self.chat_listbox.add_css_class("dark")
            else:
                self.remove_css_class("dark")
                self.main_stack.remove_css_class("dark")
                if hasattr(self, 'chat_listbox'):
                    self.chat_listbox.remove_css_class("dark")
        style_manager.connect("notify::dark", _on_dark_changed)
        _on_dark_changed()  # Apply initial state

        self.add_message_bubble("assistant", _("Hello! I am Alexy. How can I help you today?"))

    def _check_stt_availability(self):
        """Check if the selected STT backend is available and update mic button state."""
        import importlib.util as _ilu
        if self.stt_backend == "whisper":
            if _ilu.find_spec("whisper") is None:
                self.stt_toggle.set_sensitive(False)
                self.stt_toggle.set_tooltip_text(_("openai-whisper is not installed. Install it via: pip install openai-whisper"))
                return
        elif self.stt_backend == "vosk":
            if _ilu.find_spec("vosk") is None:
                self.stt_toggle.set_sensitive(False)
                self.stt_toggle.set_tooltip_text(_("python-vosk is not installed. You can install it from Settings."))
                return
        self.stt_toggle.set_sensitive(True)
        self.stt_toggle.set_tooltip_text("")

    def on_stt_toggled(self, btn):
        if btn.get_active():
            # Stop any TTS playback before starting mic
            if getattr(self, 'tts_playing', False):
                self._stop_tts()
            proc = self.arecord_proc
            if proc:
                proc.terminate()
                self.arecord_proc = None

            if self.stt_backend == "whisper":
                self._stt_start_whisper(btn)
            else:
                self._stt_start_vosk(btn)
        else:
            self.stt_running = False
            proc = self.arecord_proc
            if proc:
                proc.terminate() # type: ignore
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
                self.arecord_proc = None

    def _play_activation_sound(self):
        """Play the activation sound when STT starts listening."""
        sound_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds", "alexy_activation.ogg")
        if not os.path.isfile(sound_path):
            sound_path = "/usr/share/linexin/widgets/sounds/alexy_activation.ogg"
        if os.path.isfile(sound_path):
            subprocess.Popen(
                ["paplay", sound_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _stt_start_whisper(self, btn):
        """Start Whisper-based speech-to-text: record audio, then transcribe on silence."""
        # If model is already loaded from a previous invocation, skip straight
        # to recording — no import or load needed.
        if hasattr(self, '_whisper_model_obj') and getattr(self, '_whisper_model_name', None) == self.whisper_model:
            self._begin_whisper_recording(btn)
            return

        # Check if the model file needs to be downloaded first (lightweight
        # path check — no heavy imports needed).
        whisper_cache = os.path.expanduser("~/.cache/whisper")
        model_file = os.path.join(whisper_cache, f"{self.whisper_model}.pt")
        if not os.path.exists(model_file):
            # Model not yet downloaded — download via curl with visible progress
            btn.set_active(False)

            # Whisper model download URLs
            whisper_urls = {
                "tiny": "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt",
                "base": "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt",
                "small": "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt",
                "medium": "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt",
            }
            url = whisper_urls.get(self.whisper_model)
            if not url:
                self.add_message_bubble("assistant", _("Unknown Whisper model: {}").format(self.whisper_model))
                return

            model_sizes = {"tiny": "~39 MB", "base": "~74 MB", "small": "~461 MB", "medium": "~1.5 GB"}
            size_label = model_sizes.get(self.whisper_model, "")

            # Create a temp download script that outputs clean progress lines
            dl_script = os.path.join(tempfile.gettempdir(), "linexin_whisper_dl.py")
            with open(dl_script, "w") as sf:
                sf.write(
                    "import urllib.request, sys\n"
                    f"url = '{url}'\n"
                    f"out = '{model_file}'\n"
                    "def progress(block, block_size, total):\n"
                    "    if total > 0:\n"
                    "        pct = min(int(block * block_size * 100 / total), 100)\n"
                    "        done_mb = block * block_size / 1048576\n"
                    "        total_mb = total / 1048576\n"
                    "        print(f'{pct}% {done_mb:.0f}MB/{total_mb:.0f}MB', flush=True)\n"
                    "urllib.request.urlretrieve(url, out, progress)\n"
                    "print('100%', flush=True)\n"
                )

            download_cmd = f"mkdir -p {whisper_cache} && python3 {dl_script}"
            win = _ActionProgressWindow(
                parent=self.window if self.window else self.get_root(),
                title=_("Downloading Voice Recognition Model"),
                cmd_string=download_cmd,
                is_ollama=True,  # enables percentage-based progress bar parsing
                initial_status=_('Downloading Whisper {} model ({})...').format(self.whisper_model, size_label)
            )
            def on_whisper_download_done(success):
                if success:
                    # Auto-activate mic after successful download
                    GLib.idle_add(btn.set_active, True)
                else:
                    self.entry.set_text(_("Failed to download Whisper model."))
                    # Clean up partial download
                    try:
                        if os.path.exists(model_file):
                            os.unlink(model_file)
                    except Exception:
                        pass
            win.on_close_callback = on_whisper_download_done
            win.present()
            return

        # Import whisper + load model entirely in a background thread so
        # neither `import whisper` (which pulls in PyTorch) nor load_model()
        # block the GTK main loop.
        self.entry.set_placeholder_text(_("Loading Whisper model..."))
        self.stt_toggle.set_sensitive(False)

        def _bg_import_and_load():
            try:
                import whisper as whisper_module # type: ignore # pylint: disable=import-error
                model_obj = whisper_module.load_model(self.whisper_model)
                GLib.idle_add(self._on_whisper_model_ready, model_obj, btn)
            except ImportError:
                GLib.idle_add(self._on_whisper_model_failed, "openai-whisper is not installed.", btn)
            except Exception as e:
                GLib.idle_add(self._on_whisper_model_failed, str(e), btn)

        threading.Thread(target=_bg_import_and_load, daemon=True).start()

    def _on_whisper_model_ready(self, model_obj, btn):
        """Called on main thread after background whisper model load succeeds."""
        self._whisper_model_obj = model_obj
        self._whisper_model_name = self.whisper_model
        self.stt_toggle.set_sensitive(True)
        if btn.get_active():
            self._begin_whisper_recording(btn)
        else:
            self.entry.set_placeholder_text(_("Ask a question..."))
        return False

    def _on_whisper_model_failed(self, error_msg, btn):
        """Called on main thread after background whisper model load fails."""
        self.stt_toggle.set_sensitive(True)
        self.add_message_bubble("assistant", _("Error loading Whisper model: ") + error_msg)
        btn.set_active(False)
        self.entry.set_placeholder_text(_("Ask a question..."))
        return False

    def _begin_whisper_recording(self, btn):
        """Start arecord and the whisper listen loop (model already loaded)."""
        import struct, wave
        self.entry.set_placeholder_text(_("Listening..."))
        self._play_activation_sound()

        try:
            self.arecord_proc = subprocess.Popen(
                ["arecord", "-f", "S16_LE", "-c", "1", "-r", "16000", "-q"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            ) # type: ignore
            self.stt_running = True

            def whisper_listen_loop():
                import time as time_mod
                SAMPLE_RATE = 16000
                CHUNK_SIZE = 4000  # ~0.125s of audio at 16kHz mono 16-bit
                SILENCE_THRESHOLD = 360  # Lower threshold to better detect quieter speech
                SILENCE_TIMEOUT = 2.0  # seconds of silence before auto-send
                SPEECH_CONFIRM_FRAMES = 2  # Faster confirmation for softer/short utterances

                audio_frames: list[bytes] = []
                last_speech_time: float = time_mod.time()  # type: ignore
                has_speech = False
                loud_streak = 0  # count consecutive frames above threshold

                while self.stt_running:
                    proc = self.arecord_proc
                    if not isinstance(proc, subprocess.Popen):
                        break
                    if proc.poll() is not None:
                        break

                    stdout = proc.stdout
                    if stdout is None:
                        break
                    data = stdout.read(CHUNK_SIZE) # type: ignore
                    if len(data) == 0:
                        break

                    audio_frames.append(data)

                    # Simple RMS-based voice activity detection
                    try:
                        samples = struct.unpack(f"<{len(data)//2}h", data)
                        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
                    except Exception:
                        rms = 0

                    if rms > SILENCE_THRESHOLD:
                        last_speech_time = time_mod.time()  # type: ignore
                        loud_streak += 1
                        if not has_speech and loud_streak >= SPEECH_CONFIRM_FRAMES:
                            has_speech = True
                            GLib.idle_add(self.entry.set_placeholder_text, _("Listening... (speak now)"))
                    else:
                        loud_streak = 0

                    # If speech was detected and silence timeout reached, transcribe
                    if has_speech and (time_mod.time() - last_speech_time > SILENCE_TIMEOUT):  # type: ignore
                        break
                # Stop recording
                proc = self.arecord_proc
                if proc:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self.arecord_proc = None

                # If the loop exited because the user toggled the button off manually, discard everything
                if not self.stt_running:
                    GLib.idle_add(self.entry.set_placeholder_text, _("Ask a question..."))
                    return

                if not has_speech or not audio_frames:
                    GLib.idle_add(self.entry.set_placeholder_text, _("Ask a question..."))
                    GLib.idle_add(self.stt_toggle.set_active, False)
                    return

                # Write collected audio to a temporary WAV file
                GLib.idle_add(self.entry.set_placeholder_text, _("Transcribing..."))
                tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="linexin-stt-")
                tmp_wav_path = tmp_wav.name
                tmp_wav.close()
                try:
                    with wave.open(tmp_wav_path, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)  # 16-bit = 2 bytes
                        wf.setframerate(SAMPLE_RATE)
                        raw_audio: bytes = b''.join(audio_frames)
                        wf.writeframes(raw_audio)

                    # Transcribe with Whisper
                    result = self._whisper_model_obj.transcribe(
                        tmp_wav_path,
                        fp16=False
                    )
                    text = result.get("text", "").strip() # type: ignore
                    detected_lang = result.get("language", "")  # type: ignore
                    if detected_lang:
                        self._whisper_detected_lang = detected_lang
                finally:
                    try:
                        os.unlink(tmp_wav_path)
                    except Exception:
                        pass

                if text:
                    self._last_input_was_voice = True
                    GLib.idle_add(self.entry.set_placeholder_text, _("Ask a question..."))
                    GLib.idle_add(self.entry.set_text, text)
                    GLib.idle_add(self.stt_toggle.set_active, False)
                    GLib.idle_add(self.send_btn.emit, "clicked")
                else:
                    GLib.idle_add(self.entry.set_placeholder_text, _("Ask a question..."))
                    GLib.idle_add(self.stt_toggle.set_active, False)

            self.stt_thread = threading.Thread(target=whisper_listen_loop, daemon=True)
            self.stt_thread.start()

        except Exception as e:
            self.add_message_bubble("assistant", _("Failed to start mic: {}").format(e))
            btn.set_active(False)

    @staticmethod
    def _kill_hey_linux():
        """Kill any running hey-linux daemon.  Uses os.system to bypass
        the monkey-patched subprocess lock manager, and the bracket trick
        so pkill/shell don't self-match."""
        os.system("pkill -9 -f '[/]usr/bin/hey-linux' 2>/dev/null")
        os.system("pkill -9 -f '[h]ey-linux-venv/bin/python' 2>/dev/null")

    @staticmethod
    def _launch_hey_linux_detached():
        """Launch hey-linux fully detached via double-fork so the lock
        manager is never involved and the window can still close."""
        pid = os.fork()
        if pid == 0:
            # First child — new session leader
            os.setsid()
            pid2 = os.fork()
            if pid2 > 0:
                os._exit(0)  # First child exits; grandchild reparented to init
            # Grandchild — redirect all I/O and exec hey-linux
            devnull_fd = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull_fd, 0)
            os.dup2(devnull_fd, 1)
            os.dup2(devnull_fd, 2)
            if devnull_fd > 2:
                os.close(devnull_fd)
            os.execvp("/usr/bin/hey-linux", ["/usr/bin/hey-linux"])
            os._exit(1)
        else:
            os.waitpid(pid, 0)  # Reap first child immediately

    def _on_hey_linux_toggled(self, row, param):
        self.hey_linux_enabled = row.get_active()
        self.save_config()
        
        autostart_dir = os.path.expanduser("~/.config/autostart")
        desktop_file = os.path.join(autostart_dir, "hey-linux.desktop")
        
        if self.hey_linux_enabled:
            os.makedirs(autostart_dir, exist_ok=True)
            with open(desktop_file, "w") as f:
                f.write("[Desktop Entry]\nName=Hey Alexy Wake Word\nExec=/usr/bin/hey-linux\nType=Application\nNoDisplay=true\n")
            
            self._kill_hey_linux()
            self._launch_hey_linux_detached()
        else:
            if os.path.exists(desktop_file):
                os.unlink(desktop_file)
            self._kill_hey_linux()

    def _stt_start_vosk(self, btn):
        """Start Vosk-based speech-to-text (streaming recognition)."""
        model_path = os.path.expanduser(f"~/.local/share/linexin/vosk-model-{self.vosk_lang}")
        if not os.path.exists(model_path):
            btn.set_active(False)
            url = f"https://alphacephei.com/vosk/models/vosk-model-{self.vosk_lang}.zip"
            cmd_str = f"mkdir -p ~/.local/share/linexin && rm -rf /tmp/vmodel && unzip -q -o /tmp/vmodel.zip -d /tmp/vmodel/ && mv /tmp/vmodel/* {model_path} && rm -rf /tmp/vmodel /tmp/vmodel.zip"

            # Fetch first then extract to ensure curl progress shows correctly
            full_cmd_str = f"curl -L {url} -o /tmp/vmodel.zip && {cmd_str}"

            win = _ActionProgressWindow(
                parent=self.window if self.window else self.get_root(),
                title=_("Downloading Offline Voice Model"),
                cmd_string=full_cmd_str
            )

            def on_download_done(success):
                if success:
                    self.entry.set_text(_("Model downloaded. Click mic to speak."))
                else:
                    self.entry.set_text(_("Failed to download voice model."))
            win.on_close_callback = on_download_done
            win.present()
            return

        try:
            import vosk # type: ignore # pylint: disable=import-error
        except ImportError:
            self.add_message_bubble("assistant", _("python-vosk is not installed. You can install it from Settings."))
            btn.set_active(False)
            return

        vosk.SetLogLevel(-1) # type: ignore
        try:
            self.vosk_model = vosk.Model(model_path)
            self.vosk_recognizer = vosk.KaldiRecognizer(self.vosk_model, 16000)
        except Exception as e:
            self.add_message_bubble("assistant", _("Error loading voice model: {}").format(e))
            btn.set_active(False)
            return

        self.entry.set_placeholder_text(_("Listening..."))
        self._play_activation_sound()

        try:
            self.arecord_proc = subprocess.Popen(
                ["arecord", "-f", "S16_LE", "-c", "1", "-r", "16000", "-q"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            ) # type: ignore
            self.stt_running = True

            def listen_loop():
                import json, time
                last_speech_time = time.time()
                last_text = ""
                while self.stt_running:
                    proc = self.arecord_proc
                    if not isinstance(proc, subprocess.Popen):
                        break
                    if proc.poll() is not None:
                        break

                    stdout = proc.stdout
                    if stdout is None:
                        break
                    data = stdout.read(4000) # type: ignore
                    if len(data) == 0:
                        break

                    current_text = ""
                    if self.vosk_recognizer.AcceptWaveform(data):
                        res = json.loads(self.vosk_recognizer.Result()) # type: ignore
                        if res.get("text"):
                            current_text = res["text"]
                    else:
                        partial_json = json.loads(self.vosk_recognizer.PartialResult()) # type: ignore
                        current_text = partial_json.get("partial", "")

                    if current_text and current_text != last_text:
                        last_speech_time = time.time() # type: ignore
                        last_text = current_text
                        GLib.idle_add(self.entry.set_text, current_text)

                    # If the user has spoken at least something, evaluate the 2.0s silence timeout frame-by-frame
                    if last_text and (time.time() - last_speech_time > 2.0): # type: ignore
                        self._last_input_was_voice = True
                        GLib.idle_add(self.stt_toggle.set_active, False)
                        GLib.idle_add(self.send_btn.emit, "clicked")
                        break

                if hasattr(self, "vosk_recognizer"):
                    try:
                        final_json = json.loads(self.vosk_recognizer.FinalResult()) # type: ignore
                        final_text = final_json.get("text", "")
                        if final_text:
                            GLib.idle_add(self.entry.set_text, final_text)
                    except Exception:
                        pass
                GLib.idle_add(self.entry.set_placeholder_text, _("Ask a question..."))

            self.stt_thread = threading.Thread(target=listen_loop, daemon=True)
            self.stt_thread.start()

        except Exception as e:
            self.add_message_bubble("assistant", _("Failed to start mic: {}").format(e))
            if self.arecord_proc:
                self.arecord_proc.terminate()
                try:
                    self.arecord_proc.wait(timeout=2)
                except Exception:
                    pass
                self.arecord_proc = None
            self.stt_running = False
            btn.set_active(False)

    def update_subtitle(self):
        if self.backend == "direct":
            self.subtitle_label.set_label(_("Online API: {}").format(self.model))
        elif self.backend == "local":
            self.subtitle_label.set_label(_("Local AI: {}").format(self.local_model))
        elif self.backend == "endpoint":
            self.subtitle_label.set_label(_("Local AI: {}").format(self.endpoint_model or _("auto-detected")))

    def _markdown_to_pango(self, text_content):
        """Convert a subset of Markdown to Pango markup for message labels."""
        import html
        import re
        escaped_content = html.escape(text_content)

        # Triple backticks (with optional language specifier)
        parsed_markup = re.sub(r'```[a-zA-Z0-9]*\n?(.*?)```', r'<tt>\1</tt>', escaped_content, flags=re.DOTALL)
        # Single backticks (now supporting multiline)
        parsed_markup = re.sub(r'`(.*?)`', r'<tt>\1</tt>', parsed_markup, flags=re.DOTALL)

        # Protect <tt> blocks from bold/italic processing (underscores in filenames etc.)
        _tt_blocks = []
        def _save_tt(m):
            _tt_blocks.append(m.group(0))
            return f'\x00TT{len(_tt_blocks)-1}\x00'
        parsed_markup = re.sub(r'<tt>.*?</tt>', _save_tt, parsed_markup, flags=re.DOTALL)

        # Headings (up to H3 as they map cleanly to big text in Pango)
        parsed_markup = re.sub(r'^### (.*?)$', r'<span size="large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)
        parsed_markup = re.sub(r'^## (.*?)$', r'<span size="x-large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)
        parsed_markup = re.sub(r'^# (.*?)$', r'<span size="xx-large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)

        # Lists
        parsed_markup = re.sub(r'^[-*]\s+(.*?)$', r'  • \1', parsed_markup, flags=re.MULTILINE)
        parsed_markup = re.sub(r'^(\d+)\.\s+(.*?)$', r'  \1. \2', parsed_markup, flags=re.MULTILINE)

        # Bold
        parsed_markup = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', parsed_markup, flags=re.DOTALL)
        # Italic (Must be parsed after Bold to prevent double-asterisk conflicts)
        parsed_markup = re.sub(r'\*(.*?)\*', r'<i>\1</i>', parsed_markup)
        parsed_markup = re.sub(r'_(.*?)_', r'<i>\1</i>', parsed_markup)

        # Restore <tt> blocks
        for i, block in enumerate(_tt_blocks):
            parsed_markup = parsed_markup.replace(f'\x00TT{i}\x00', block)
        return parsed_markup

    def _scroll_chat_to_bottom(self):
        """Scroll the chat view to the bottom."""
        adj = self.scrolled_window.get_vadjustment()
        if adj:
            adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def add_message_bubble(self, role, content, is_html=False):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.add_css_class("message-row")
        if role == "user":
            row.add_css_class("user-message-row")
        else:
            row.add_css_class("assistant-message-row")
            
        # Handle message grouping for themes (directional tails)
        if not hasattr(self, '_last_bubble_role'):
            self._last_bubble_role = None
            self._last_bubble_box = None
            
        if self._last_bubble_role == role and self._last_bubble_box:
            # Previous message is no longer the last in its group
            self._last_bubble_box.remove_css_class("last-in-group")
            
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("message-box")
        box.add_css_class("last-in-group")
        
        self._last_bubble_role = role
        self._last_bubble_box = box
        
        if role == "user":
            box.add_css_class("user-message-box")
        else:
            box.add_css_class("assistant-message-box")

        # Handle multimodal content (list with text + image_url items)
        image_data_urls = []
        if isinstance(content, list):
            text_content = self._extract_text_from_content(content)
            image_data_urls = self._extract_images_from_content(content)
        else:
            text_content = content

        parsed_markup = self._markdown_to_pango(text_content)

        if role == "user":
            box.set_halign(Gtk.Align.END)
            
            bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bubble.add_css_class("message-bubble")
            bubble.add_css_class("user-bubble")

            # Render attached images as thumbnails inside the bubble
            if image_data_urls:
                from gi.repository import Gdk as _Gdk  # type: ignore
                images_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                images_box.set_halign(Gtk.Align.END)
                for data_url in image_data_urls:
                    try:
                        b64_part = data_url.split(",", 1)[1] if "," in data_url else data_url
                        raw = base64.b64decode(b64_part)
                        texture = _Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
                        picture = Gtk.Picture.new_for_paintable(texture)
                        picture.set_size_request(150, 150)
                        picture.set_can_shrink(True)
                        picture.set_content_fit(Gtk.ContentFit.COVER)
                        frame = Gtk.Frame()
                        frame.set_child(picture)
                        images_box.append(frame)
                    except Exception:
                        pass
                bubble.append(images_box)

            if text_content.strip():
                label = Gtk.Label()
                label.add_css_class("message-label")
                try:
                    label.set_markup(parsed_markup)
                except Exception:
                    label.set_text(text_content)
                label.set_wrap(True)
                label.set_selectable(True)
                label.set_xalign(1.0)
                bubble.append(label)

            box.append(bubble)
        else:
            box.set_halign(Gtk.Align.START)
            # The Alexy AI avatar is intentionally NOT themeable: it always
            # shows the agent's own icon regardless of the selected theme.
            if os.path.isfile(self.alexy_icon_path):
                icon = Gtk.Image.new_from_file(self.alexy_icon_path)
            else:
                icon = Gtk.Image.new_from_icon_name(self.widgeticon)
            icon.set_pixel_size(24)
            icon.set_valign(Gtk.Align.START)
            box.append(icon)
            
            label = Gtk.Label()
            label.add_css_class("message-label")
            try:
                label.set_markup(parsed_markup)
            except Exception:
                label.set_text(text_content)
            label.set_wrap(True)
            label.set_selectable(True)
            label.set_xalign(0.0)
            
            bubble = Gtk.Box()
            bubble.add_css_class("message-bubble")
            bubble.add_css_class("assistant-bubble")
            bubble.append(label)
            box.append(bubble)

        row.set_child(box)
        self.chat_listbox.append(row)
        
        def scroll_to_bottom():
            adj = self.scrolled_window.get_vadjustment()
            if adj:
                adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
            
        GLib.timeout_add(100, scroll_to_bottom)
        
        adj = self.scrolled_window.get_vadjustment()
        GLib.idle_add(lambda: adj.set_value(adj.get_upper() - adj.get_page_size()) if adj.get_upper() > adj.get_page_size() else False)

    def _close_settings_sidebar(self):
        """Apply any pending settings and slide the settings sidebar closed."""
        cb = getattr(self, '_settings_apply_cb', None)
        if cb is not None:
            try:
                cb()
            except Exception as e:
                print(f"Error applying settings: {e}")
            self._settings_apply_cb = None
        if getattr(self, 'settings_revealer', None) is not None:
            self.settings_revealer.set_reveal_child(False)
        if getattr(self, 'settings_scrim', None) is not None:
            self.settings_scrim.set_visible(False)

    def _build_agents_page(self):
        """Build the 'Agents' settings page. Returns (page, get_state) where
        get_state() -> (user_agents_list, active_agent_name) reflecting the
        user's edits, to be committed when settings are applied."""
        page = Adw.PreferencesPage()

        # Working copy of the custom agents (built-in agents are implicit).
        working = [dict(a) for a in self.user_agents]
        names_all = list(BUILTIN_AGENT_NAMES) + [a["name"] for a in working]
        start_active = self.active_agent if self.active_agent in names_all else DEFAULT_AGENT_NAME

        select_group = Adw.PreferencesGroup(
            title=_("Agents"),
            description=_("Agents are named master-prompt profiles. Select one to use for new conversations, or create your own. The built-in agents cannot be edited.")
        )
        page.add(select_group)

        combo = Adw.ComboRow(title=_("Active Agent"), subtitle=_("Applied to new conversations"))
        select_group.add(combo)

        new_row = Adw.ActionRow(title=_("Create New Agent"),
                                subtitle=_("Start from a copy of the default prompt"))
        new_btn = Gtk.Button(label=_("New Agent"), valign=Gtk.Align.CENTER)
        new_btn.add_css_class("suggested-action")
        new_row.add_suffix(new_btn)
        new_row.set_activatable_widget(new_btn)
        select_group.add(new_row)

        editor_group = Adw.PreferencesGroup()
        page.add(editor_group)
        editor_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        editor_group.add(editor_box)

        ed = {"current_name": start_active, "buffer": None, "loading": False}

        def names_list():
            return list(BUILTIN_AGENT_NAMES) + [a["name"] for a in working]

        def find_agent(name):
            for a in working:
                if a["name"] == name:
                    return a
            return None

        def clear_box():
            child = editor_box.get_first_child()
            while child is not None:
                editor_box.remove(child)
                child = editor_box.get_first_child()

        def commit_editor():
            name = ed.get("current_name")
            if name and name not in BUILTIN_AGENT_NAMES and ed["buffer"] is not None:
                a = find_agent(name)
                if a is not None:
                    b = ed["buffer"]
                    a["prompt"] = b.get_text(b.get_start_iter(), b.get_end_iter(), False)

        def make_prompt_view(text, editable):
            view = Gtk.TextView()
            view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            view.set_editable(editable)
            view.set_cursor_visible(editable)
            view.set_top_margin(8)
            view.set_bottom_margin(8)
            view.set_left_margin(8)
            view.set_right_margin(8)
            view.get_buffer().set_text(text)
            if not editable:
                view.add_css_class("dim-label")
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroll.set_min_content_height(220)
            scroll.set_child(view)
            scroll.add_css_class("card")
            return scroll, view

        def build_editor(name):
            clear_box()
            ed["current_name"] = name
            ed["buffer"] = None
            if name in BUILTIN_AGENT_NAMES:
                info = Gtk.Label(label=_("This is a built-in agent. Its prompt is shown for reference and cannot be edited."))
                info.add_css_class("dim-label")
                info.add_css_class("caption")
                info.set_wrap(True)
                info.set_halign(Gtk.Align.START)
                editor_box.append(info)
                scroll, _view = make_prompt_view(self._agent_prompt(name), False)
                editor_box.append(scroll)
            else:
                a = find_agent(name)
                prompt_text = a.get("prompt", self._default_system_prompt) if a else self._default_system_prompt
                scroll, view = make_prompt_view(prompt_text, True)
                ed["buffer"] = view.get_buffer()
                editor_box.append(scroll)

                button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                button_box.set_halign(Gtk.Align.END)

                rename_btn = Gtk.Button(label=_("Rename Agent"))

                def on_rename(btn, nm=name):
                    commit_editor()
                    parent = self.window if self.window else self.get_root()
                    dialog = Adw.MessageDialog(
                        transient_for=parent,
                        heading=_("Rename Agent"),
                        body=_("Enter a new name for this agent.")
                    )
                    entry = Gtk.Entry()
                    entry.set_text(nm)
                    dialog.set_extra_child(entry)
                    dialog.add_response("cancel", _("Cancel"))
                    dialog.add_response("rename", _("Rename"))
                    dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
                    dialog.set_default_response("rename")

                    def on_response(dlg, response):
                        if response == "rename":
                            new_name = entry.get_text().strip()
                            existing = [n.lower() for n in names_list() if n.lower() != nm.lower()]
                            if new_name and new_name.lower() not in existing:
                                target = find_agent(nm)
                                if target is not None:
                                    target["name"] = new_name
                                ed["current_name"] = new_name
                                refresh_combo(new_name)
                        dlg.destroy()
                    dialog.connect("response", on_response)
                    entry.connect("activate", lambda e: dialog.response("rename"))
                    dialog.present()
                rename_btn.connect("clicked", on_rename)
                button_box.append(rename_btn)

                delete_btn = Gtk.Button(label=_("Delete Agent"))
                delete_btn.add_css_class("destructive-action")

                def on_delete(btn, nm=name):
                    target = find_agent(nm)
                    if target is not None:
                        working.remove(target)
                    ed["buffer"] = None
                    ed["current_name"] = DEFAULT_AGENT_NAME
                    refresh_combo(DEFAULT_AGENT_NAME)
                delete_btn.connect("clicked", on_delete)
                button_box.append(delete_btn)

                editor_box.append(button_box)

        def refresh_combo(select_name):
            ed["loading"] = True
            sl = Gtk.StringList()
            for n in names_list():
                sl.append(n)
            combo.set_model(sl)
            names = names_list()
            idx = names.index(select_name) if select_name in names else 0
            combo.set_selected(idx)
            ed["loading"] = False
            build_editor(names[idx])

        def on_combo_changed(row, _pspec):
            if ed["loading"]:
                return
            commit_editor()
            names = names_list()
            sel = row.get_selected()
            if 0 <= sel < len(names):
                build_editor(names[sel])

        combo.connect("notify::selected", on_combo_changed)

        def on_new_agent(btn):
            commit_editor()
            parent = self.window if self.window else self.get_root()
            dialog = Adw.MessageDialog(
                transient_for=parent,
                heading=_("New Agent"),
                body=_("Enter a name for the new agent.")
            )
            entry = Gtk.Entry()
            entry.set_placeholder_text(_("Agent name"))
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("create", _("Create"))
            dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("create")

            def on_response(dlg, response):
                if response == "create":
                    name = entry.get_text().strip()
                    existing = [n.lower() for n in names_list()]
                    if name and name.lower() not in existing:
                        working.append({"name": name, "prompt": self._default_system_prompt})
                        refresh_combo(name)
                dlg.destroy()
            dialog.connect("response", on_response)
            entry.connect("activate", lambda e: dialog.response("create"))
            dialog.present()
        new_btn.connect("clicked", on_new_agent)

        refresh_combo(start_active)

        def get_state():
            commit_editor()
            names = names_list()
            sel = combo.get_selected()
            active = names[sel] if 0 <= sel < len(names) else DEFAULT_AGENT_NAME
            return [dict(a) for a in working], active

        return page, get_state

    def on_settings_clicked(self, button):
        # Toggle: if the sidebar is already open, close it (and apply).
        if getattr(self, 'settings_revealer', None) is not None and self.settings_revealer.get_reveal_child():
            self._close_settings_sidebar()
            return

        # Conversations and settings are mutually exclusive.
        if getattr(self, 'conv_toggle_btn', None) is not None and self.conv_toggle_btn.get_active():
            self.conv_toggle_btn.set_active(False)

        settings_parent = self.window if self.window else self.get_root()

        # Categorized settings hosted inside the sliding sidebar. A ViewStack
        # holds three pages (Assistant, Speech, Theme) selected by a ViewSwitcher
        # in the sidebar header, restoring the original organization.
        settings_view_stack = Adw.ViewStack()
        page_llm = Adw.PreferencesPage()
        settings_view_stack.add_titled_with_icon(
            page_llm, "assistant", _("Assistant"), "preferences-system-symbolic")

        # Agents tab: manage named master-prompt profiles.
        agents_page, get_agents_state = self._build_agents_page()
        settings_view_stack.add_titled_with_icon(
            agents_page, "agents", _("Agents"), "system-users-symbolic")
        
        safety_group = Adw.PreferencesGroup(title=_("General Settings"))
        page_llm.add(safety_group)
        
        auto_exec_row = Adw.SwitchRow(title=_("Auto-Execute Commands"), subtitle=_("Allow AI to silently execute bash commands without prompting for permission first. Leave checked for a continuous experience."))
        auto_exec_row.set_active(self.auto_execute_commands)
        safety_group.add(auto_exec_row)

        compact_screen_row = Adw.SwitchRow(title=_("Compact Mode Screen Awareness"), subtitle=_("Automatically capture and send a screenshot to the AI when using compact voice mode (Hey Alexy)."))
        compact_screen_row.set_active(self.compact_screen_awareness)
        safety_group.add(compact_screen_row)

        general_group = Adw.PreferencesGroup(title=_("Backend Type"), description=_("Configure how Alexy connects to models."))
        page_llm.add(general_group)
        
        backend_row = Adw.ComboRow(title=_("Backend Type"))
        model = Gtk.StringList()
        model.append(_("Direct API (Online)"))
        model.append(_("Local AI (Ollama)"))
        model.append(_("Local AI (Endpoint)"))
        backend_row.set_model(model)
        
        if self.backend == "direct":
            backend_row.set_selected(0)
        elif self.backend == "local":
            backend_row.set_selected(1)
        elif self.backend == "endpoint":
            backend_row.set_selected(2)
            
        general_group.add(backend_row)

        # Dynamic Direct API Group
        direct_group = Adw.PreferencesGroup(description=_("Uses urllib to connect directly to Qwen or OpenAI compatible APIs."))
        page_llm.add(direct_group)
        
        api_key_entry = Adw.PasswordEntryRow(title=_("API Key"))
        api_key_entry.set_text(self.api_key)
        direct_group.add(api_key_entry)

        api_url_entry = Adw.EntryRow(title=_("API URL"))
        api_url_entry.set_text(self.api_url)
        direct_group.add(api_url_entry)

        model_entry = Adw.EntryRow(title=_("Model"))
        model_entry.set_text(self.model)
        direct_group.add(model_entry)

        # Dynamic Local AI (custom endpoint) Group
        endpoint_group = Adw.PreferencesGroup(title=_("Local AI (Endpoint)"), description=_("Connects to a local OpenAI-compatible server via its endpoint URL (e.g. http://localhost:6767/v1). The model is detected automatically — no API key, model name or Ollama required."))
        page_llm.add(endpoint_group)

        endpoint_url_entry = Adw.EntryRow(title=_("Endpoint URL"))
        endpoint_url_entry.set_text(self.endpoint_url)
        endpoint_group.add(endpoint_url_entry)

        # Dynamic Local AI Group
        local_group = Adw.PreferencesGroup(title=_("Local AI"), description=_("Uses Ollama daemon sequentially running on localhost:11434."))
        page_llm.add(local_group)
        
        local_model_row = Adw.ComboRow(title=_("Select Downloaded Model"))
        self._refresh_ollama_models(local_model_row)
        local_group.add(local_model_row)
        
        remove_row = Adw.ActionRow(title=_("Delete from Disk"))
        remove_btn = Gtk.Button(label=_("Remove Model"), valign=Gtk.Align.CENTER)
        remove_btn.add_css_class("destructive-action")
        remove_btn.connect("clicked", lambda b, c=local_model_row: self.on_remove_ollama_clicked(c))
        remove_row.add_suffix(remove_btn)
        remove_row.set_activatable_widget(remove_btn)
        local_group.add(remove_row)

        # Ollama install / uninstall management row
        ollama_installed = self.is_ollama_installed()
        ollama_manage_row = Adw.ActionRow()
        if ollama_installed:
            ollama_manage_row.set_title(_("Ollama is installed"))
            ollama_manage_row.set_subtitle(_("Remove the Ollama daemon and its dependencies from your system."))
            ollama_manage_btn = Gtk.Button(label=_("Uninstall Ollama"), valign=Gtk.Align.CENTER)
            ollama_manage_btn.add_css_class("destructive-action")
            def on_ollama_uninstall_clicked(btn):
                def do_uninstall():
                    cmd = "pacman -Qi ollama &>/dev/null && pacman -Rns ollama --noconfirm || { systemctl disable --now ollama 2>/dev/null; rm -f /usr/local/bin/ollama; rm -rf /usr/local/lib/ollama; rm -f /etc/systemd/system/ollama.service; systemctl daemon-reload; }"
                    win_uninstall = _ActionProgressWindow(
                        parent=settings_parent,
                        title=_("Uninstalling Ollama"),
                        cmd_string=cmd,
                        sudo_manager=self.sudo_manager
                    )
                    def on_uninstall_done(success):
                        if success:
                            ollama_manage_row.set_title(_("Ollama has been uninstalled"))
                            ollama_manage_row.set_subtitle(_("Install the Ollama daemon to use Local AI."))
                            ollama_manage_btn.set_label(_("Install Ollama"))
                            ollama_manage_btn.remove_css_class("destructive-action")
                            ollama_manage_btn.add_css_class("suggested-action")
                            ollama_manage_btn.disconnect_by_func(on_ollama_uninstall_clicked)
                            ollama_manage_btn.connect("clicked", on_ollama_install_settings_clicked)
                            self._refresh_ollama_models(local_model_row)
                    win_uninstall.on_close_callback = on_uninstall_done
                    win_uninstall.present()
                manager = self.sudo_manager
                if not manager or not manager.user_password:
                    self._prompt_for_password_dialog(
                        do_uninstall,
                        _("Please enter your password to uninstall Ollama.")
                    )
                else:
                    do_uninstall()
            ollama_manage_btn.connect("clicked", on_ollama_uninstall_clicked)
        else:
            ollama_manage_row.set_title(_("Ollama is not installed"))
            ollama_manage_row.set_subtitle(_("Install the Ollama daemon to use Local AI."))
            ollama_manage_btn = Gtk.Button(label=_("Install Ollama"), valign=Gtk.Align.CENTER)
            ollama_manage_btn.add_css_class("suggested-action")

        def on_ollama_install_settings_clicked(btn):
            def do_install():
                cmd = "curl -fsSL https://ollama.com/install.sh | sh"
                win_install = _ActionProgressWindow(
                    parent=settings_parent,
                    title=_("Installing Ollama"),
                    cmd_string=cmd,
                    sudo_manager=self.sudo_manager,
                    initial_status=_("Downloading and installing Ollama daemon...")
                )
                def on_install_done(success):
                    if success:
                        ollama_manage_row.set_title(_("Ollama is installed"))
                        ollama_manage_row.set_subtitle(_("Remove the Ollama daemon and its dependencies from your system."))
                        ollama_manage_btn.set_label(_("Uninstall Ollama"))
                        ollama_manage_btn.remove_css_class("suggested-action")
                        ollama_manage_btn.add_css_class("destructive-action")
                        ollama_manage_btn.disconnect_by_func(on_ollama_install_settings_clicked)
                        ollama_manage_btn.connect("clicked", on_ollama_uninstall_clicked)
                        self._refresh_ollama_models(local_model_row)
                win_install.on_close_callback = on_install_done
                win_install.present()
            manager = self.sudo_manager
            if not manager or not manager.user_password:
                self._prompt_for_password_dialog(
                    do_install,
                    _("Please enter your password to install Ollama via system privileges.")
                )
            else:
                do_install()

        if not ollama_installed:
            ollama_manage_btn.connect("clicked", on_ollama_install_settings_clicked)

        ollama_manage_row.add_suffix(ollama_manage_btn)
        ollama_manage_row.set_activatable_widget(ollama_manage_btn)
        local_group.add(ollama_manage_row)

        pull_group = Adw.PreferencesGroup(title=_("Download Model"))
        page_llm.add(pull_group)

        popular_models_row = Adw.ComboRow(title=_("Popular Models"))
        popular_models_list = Gtk.StringList()
        popular_names = [
            "qwen3.5 (9b)", "qwen3 (8b)", "qwen2.5-coder (7b)", "llama3.2 (3b)",
            "llama3.1 (8b)", "mistral (7b)", "gemma2 (9b)", "phi3 (8b)", "deepseek-coder-v2 (16b)"
        ]
        self.real_popular_names = [
            "qwen3.5", "qwen3", "qwen2.5-coder", "llama3.2", "llama3.1", "mistral", "gemma2", "phi3", "deepseek-coder-v2"
        ]
        for name in popular_names:
            popular_models_list.append(name)
        popular_models_row.set_model(popular_models_list)
        pull_group.add(popular_models_row)
        
        pull_entry = Adw.EntryRow(title=_("Model Name (e.g. qwen3.5)"))
        pull_entry.set_text(self.real_popular_names[0])
        pull_group.add(pull_entry)
        
        pull_row = Adw.ActionRow(title=_("Start Download"))
        pull_btn = Gtk.Button(label=_("Pull from Registry"), valign=Gtk.Align.CENTER)
        pull_btn.add_css_class("suggested-action")
        pull_btn.connect("clicked", lambda b, e=pull_entry, c=local_model_row: self.on_pull_ollama_clicked(e.get_text(), combo_row=c))
        pull_row.add_suffix(pull_btn)
        pull_row.set_activatable_widget(pull_btn)
        pull_group.add(pull_row)

        downloaded_base_names = [m.split(":")[0] for m in self.dynamic_models if m]

        def update_pull_btn_state(*args):
            model_name = pull_entry.get_text().strip()
            if model_name in downloaded_base_names:
                pull_btn.set_sensitive(False)
                pull_btn.set_label(_("Already Downloaded"))
            else:
                pull_btn.set_sensitive(True)
                pull_btn.set_label(_("Pull from Registry"))

        def on_popular_selected(row, *args):
            idx = row.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION and idx < len(self.real_popular_names):
                pull_entry.set_text(self.real_popular_names[idx])
            update_pull_btn_state()
                
        popular_models_row.connect("notify::selected", on_popular_selected)
        pull_entry.connect("changed", update_pull_btn_state)
        update_pull_btn_state()

        # Dynamic Backend Group Visibility Controller
        def sync_backend_visibility(*args):
            idx = backend_row.get_selected()
            direct_group.set_visible(idx == 0)
            local_group.set_visible(idx == 1)
            pull_group.set_visible(idx == 1)
            endpoint_group.set_visible(idx == 2)

        backend_row.connect("notify::selected", sync_backend_visibility)
        sync_backend_visibility() # apply initial state

        # Section 2: Speech & Audio
        page_speech = Adw.PreferencesPage()
        settings_view_stack.add_titled_with_icon(
            page_speech, "speech", _("Speech"), "audio-input-microphone-symbolic")
        
        # --- STT Backend Selector ---
        stt_engine_group = Adw.PreferencesGroup(title=_("Voice-to-Text Engine"))
        page_speech.add(stt_engine_group)

        stt_backend_row = Adw.ComboRow(title=_("STT Backend"))
        stt_backends_list = Gtk.StringList()
        stt_backends_list.append(_("OpenAI Whisper (Recommended)"))
        vosk_label = _("Vosk (Lightweight)")
        try:
            import vosk # type: ignore # pylint: disable=import-error # noqa: F401
        except ImportError:
            vosk_label = _("Vosk (Not Installed)")
        stt_backends_list.append(vosk_label)
        stt_backend_row.set_model(stt_backends_list)
        stt_backend_row.set_selected(0 if self.stt_backend == "whisper" else 1)
        stt_engine_group.add(stt_backend_row)

        # --- Whisper options group ---
        whisper_group = Adw.PreferencesGroup(title=_("Whisper Settings"), description=_("OpenAI Whisper provides high-accuracy offline transcription. Model is auto-downloaded on first use."))
        page_speech.add(whisper_group)

        whisper_model_row = Adw.ComboRow(title=_("Model Size"))
        whisper_model_list = Gtk.StringList()
        self._whisper_model_options = ["tiny", "base", "small", "medium"]
        whisper_model_labels = [
            _("Tiny (~39 MB, fastest)"),
            _("Base (~74 MB)"),
            _("Small (~461 MB, recommended)"),
            _("Medium (~1.5 GB, most accurate)")
        ]
        whisper_model_selected = 1  # default: base
        for i, label in enumerate(whisper_model_labels):
            whisper_model_list.append(label)
            if self._whisper_model_options[i] == self.whisper_model:
                whisper_model_selected = i
        whisper_model_row.set_model(whisper_model_list)
        whisper_model_row.set_selected(whisper_model_selected)
        whisper_group.add(whisper_model_row)

        # --- Vosk options group ---
        vosk_group = Adw.PreferencesGroup(title=_("Vosk Settings"), description=_("Vosk provides lightweight, streaming offline transcription."))
        page_speech.add(vosk_group)

        # Install button if vosk is not available
        vosk_available = True
        try:
            import vosk # type: ignore # pylint: disable=import-error # noqa: F401
        except ImportError:
            vosk_available = False

        if not vosk_available:
            vosk_install_row = Adw.ActionRow(title=_("Vosk is not installed"), subtitle=_("Install python-vosk to use the Vosk backend."))
            vosk_install_btn = Gtk.Button(label=_("Install python-vosk"), valign=Gtk.Align.CENTER)
            vosk_install_btn.add_css_class("suggested-action")
            def on_vosk_install_clicked(btn):
                def do_install():
                    win_install = _ActionProgressWindow(
                        parent=settings_parent,
                        title=_("Installing python-vosk"),
                        cmd_string="pacman -Sy python-vosk --noconfirm",
                        sudo_manager=self.sudo_manager
                    )
                    def on_install_done(success):
                        if success:
                            vosk_install_row.set_title(_("Vosk installed successfully!"))
                            vosk_install_row.set_subtitle(_("Vosk backend is now available."))
                            
                            # Update the STT backend dropdown dynamically
                            new_list = Gtk.StringList()
                            new_list.append(_("OpenAI Whisper (Recommended)"))
                            new_list.append(_("Vosk (Lightweight)"))
                            stt_backend_row.set_model(new_list)
                            
                            try:
                                self._check_stt_availability()
                            except Exception:
                                pass
                                
                            vosk_install_btn.set_sensitive(False)
                    win_install.on_close_callback = on_install_done
                    win_install.present()

                manager = self.sudo_manager
                if not manager or not manager.user_password:
                    self._prompt_for_password_dialog(
                        do_install,
                        _("Please enter your password to install python-vosk.")
                    )
                else:
                    do_install()
            vosk_install_btn.connect("clicked", on_vosk_install_clicked)
            vosk_install_row.add_suffix(vosk_install_btn)
            vosk_install_row.set_activatable_widget(vosk_install_btn)
            vosk_group.add(vosk_install_row)

        voice_lang_row = Adw.ComboRow(title=_("Vosk Language Model"))
        
        self.vosk_available_langs = [
            ("small-en-us-0.15", _("English (United States)")),
            ("small-en-in-0.4", _("English (India)")),
            ("small-cn-0.22", _("Chinese")),
            ("small-fr-0.22", _("French")),
            ("small-de-0.15", _("German")),
            ("small-es-0.42", _("Spanish")),
            ("small-pt-0.3", _("Portuguese")),
            ("small-it-0.22", _("Italian")),
            ("small-ru-0.22", _("Russian")),
            ("small-uk-v3-nano", _("Ukrainian")),
            ("small-pl-0.22", _("Polish")),
            ("small-ja-0.22", _("Japanese")),
            ("small-ko-0.22", _("Korean"))
        ]
        
        voice_langs = Gtk.StringList()
        selected_idx = 0
        for i, (model_id, human_name) in enumerate(self.vosk_available_langs):
            voice_langs.append(f"{human_name} ({model_id})")
            if hasattr(self, 'vosk_lang') and model_id == self.vosk_lang:
                selected_idx = i
                
        voice_lang_row.set_model(voice_langs)
        voice_lang_row.set_selected(selected_idx)
        vosk_group.add(voice_lang_row)

        # Toggle visibility based on STT backend selection
        def sync_stt_visibility(*_args):
            is_whisper = stt_backend_row.get_selected() == 0
            whisper_group.set_visible(is_whisper)
            vosk_group.set_visible(not is_whisper)

        stt_backend_row.connect("notify::selected", sync_stt_visibility)
        sync_stt_visibility()  # apply initial state

        # --- Hey Alexy Daemon ---
        hey_linux_group = Adw.PreferencesGroup(
            title=_("Hey Alexy Wake Word"), 
            description=_("Continuously listens for 'Hey Alexy' to activate the assistant. Uses openWakeWord for lightweight wake word detection.")
        )
        page_speech.add(hey_linux_group)

        self.hey_linux_row = Adw.SwitchRow(title=_('Enable "Hey Alexy"'))
        self.hey_linux_row.set_active(self.hey_linux_enabled)
        self.hey_linux_row.connect("notify::active", self._on_hey_linux_toggled)
        hey_linux_group.add(self.hey_linux_row)

        vc_group = Adw.PreferencesGroup(
            title=_("Voice Correction"), 
            description=_("Use an LLM to automatically fix transcribing errors. Note: This currently only functions when an online model (Direct API) is actively configured.")
        )
        page_speech.add(vc_group)

        direct_vc_row = Adw.SwitchRow(title=_("Enable for Direct API"))
        direct_vc_row.set_active(self.voice_correction_direct)
        vc_group.add(direct_vc_row)

        # Section 3: Theme
        page_theme = Adw.PreferencesPage()
        settings_view_stack.add_titled_with_icon(
            page_theme, "theme", _("Theme"), "applications-graphics-symbolic")

        theme_group = Adw.PreferencesGroup(title=_("Appearance"), description=_("Select a theme to customize the look of the AI assistant."))
        page_theme.add(theme_group)

        available_themes = self._discover_themes()
        theme_ids = [t["id"] for t in available_themes]
        theme_names_list = Gtk.StringList()
        current_theme_idx = 0
        for i, t in enumerate(available_themes):
            theme_names_list.append(t["name"])
            if t["id"] == self.theme:
                current_theme_idx = i

        theme_row = Adw.ComboRow(title=_("Theme"))
        theme_row.set_model(theme_names_list)
        theme_row.set_selected(current_theme_idx)
        theme_group.add(theme_row)

        # Preview group
        preview_group = Adw.PreferencesGroup(title=_("Preview"))
        page_theme.add(preview_group)

        preview_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        preview_box.set_margin_top(12)
        preview_box.set_margin_bottom(12)
        preview_box.set_halign(Gtk.Align.CENTER)

        preview_avatar = Gtk.Image()
        preview_avatar.set_pixel_size(64)
        preview_box.append(preview_avatar)

        preview_desc = Gtk.Label()
        preview_desc.add_css_class("dim-label")
        preview_desc.set_wrap(True)
        preview_desc.set_halign(Gtk.Align.CENTER)
        preview_box.append(preview_desc)

        preview_author = Gtk.Label()
        preview_author.add_css_class("caption")
        preview_author.add_css_class("dim-label")
        preview_author.set_halign(Gtk.Align.CENTER)
        preview_box.append(preview_author)

        preview_group.add(preview_box)

        def update_theme_preview(idx):
            if idx < 0 or idx >= len(available_themes):
                return
            t = available_themes[idx]
            avatar_path = os.path.join(t["path"], "assistant-avatar.svg")
            if os.path.isfile(avatar_path):
                preview_avatar.set_from_file(avatar_path)
            else:
                preview_avatar.set_from_icon_name("applications-graphics-symbolic")
            preview_desc.set_label(t.get("description", ""))
            preview_author.set_label(_("by {}").format(t.get("author", _("Unknown"))))

        update_theme_preview(current_theme_idx)

        def on_theme_changed(row, _pspec):
            update_theme_preview(row.get_selected())

        theme_row.connect("notify::selected", on_theme_changed)

        info_group = Adw.PreferencesGroup()
        page_theme.add(info_group)
        info_label = Gtk.Label(
            label=_("Custom themes can be installed to:\n{}").format(USER_THEMES_DIR),
            halign=Gtk.Align.CENTER
        )
        info_label.add_css_class("dim-label")
        info_label.add_css_class("caption")
        info_label.set_wrap(True)
        info_group.add(info_label)

        def apply_settings():
            # Agents: commit the user's agent edits and selection, then sync the
            # active master prompt so new conversations use it.
            self.user_agents, self.active_agent = get_agents_state()
            if self.active_agent not in self._agent_names():
                self.active_agent = DEFAULT_AGENT_NAME
            self._apply_active_agent()

            # If the current conversation is still empty (no messages exchanged
            # yet), retroactively apply the newly selected agent to it so the
            # user does not have to start a new conversation manually.
            if len(self.chat_history) <= 1:
                self._reset_history()

            idx = backend_row.get_selected()
            old_backend = self.backend
            if idx == 0:
                self.backend = "direct"
            elif idx == 1:
                self.backend = "local"
            elif idx == 2:
                self.backend = "endpoint"
            
            if old_backend != self.backend:
                new_backend = self.backend
                self.backend = old_backend
                self._save_conversation()
                self.backend = new_backend
                self.current_conversation_id = str(uuid.uuid4())
                if hasattr(self, '_conv_created'):
                    del self._conv_created
                self._reset_history()
                self._clear_chat_ui()
                self.add_message_bubble("assistant", _("Hello! I am Alexy. How can I help you today?"))
                self.add_message_bubble("assistant", _("Switched backend mode. New conversation started."))
            
            self.api_key = api_key_entry.get_text()
            self.api_url = api_url_entry.get_text()
            self.model = model_entry.get_text()
            new_endpoint_url = endpoint_url_entry.get_text().strip()
            if new_endpoint_url and new_endpoint_url != self.endpoint_url:
                # URL changed: drop the cached auto-detected model so it is
                # re-discovered from the new server on the next request.
                self.endpoint_url = new_endpoint_url
                self.endpoint_model = ""
            
            if len(self.dynamic_models) > 0 and local_model_row.get_selected() < len(self.dynamic_models):
                selected_dynamic = self.dynamic_models[local_model_row.get_selected()]
                if selected_dynamic:
                    self.local_model = selected_dynamic
                
            voice_idx = voice_lang_row.get_selected()
            if voice_idx != Gtk.INVALID_LIST_POSITION and voice_idx < len(self.vosk_available_langs):
                self.vosk_lang = self.vosk_available_langs[voice_idx][0]

            # Save STT backend settings
            self.stt_backend = "whisper" if stt_backend_row.get_selected() == 0 else "vosk"
            whisper_m_idx = whisper_model_row.get_selected()
            if whisper_m_idx < len(self._whisper_model_options):
                self.whisper_model = self._whisper_model_options[whisper_m_idx]


            self._check_stt_availability()
                
            self.voice_correction_direct = direct_vc_row.get_active()
            self.auto_execute_commands = auto_exec_row.get_active()
            self.compact_screen_awareness = compact_screen_row.get_active()

            # Apply selected theme
            selected_theme_idx = theme_row.get_selected()
            if selected_theme_idx < len(theme_ids):
                new_theme = theme_ids[selected_theme_idx]
                if new_theme != self.theme:
                    self.theme = new_theme
                    self._load_theme()
                    # The Alexy AI icon is intentionally NOT themeable; only the
                    # microphone icon may be overridden by a theme.
                    if os.path.isfile(self.alexy_icon_path):
                        self.header_icon_widget.set_from_file(self.alexy_icon_path)
                    else:
                        self.header_icon_widget.set_from_icon_name("system-run-symbolic")
                        
                    if hasattr(self, 'stt_icon'):
                        mic_svg = self._get_theme_svg("microphone-icon.svg")
                        if mic_svg:
                            self.stt_icon.set_from_file(mic_svg)
                        else:
                            self.stt_icon.set_from_icon_name("audio-input-microphone-symbolic")

            self.save_config()
            self.update_subtitle()

        self._settings_apply_cb = apply_settings

        # Build the sidebar chrome (header with title + close) around the page.
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("settings-sidebar")
        sidebar.set_size_request(400, -1)

        sidebar_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sidebar_header.add_css_class("settings-sidebar-header")
        settings_title = Gtk.Label(label=_("Settings"))
        settings_title.add_css_class("title-2")
        settings_title.set_hexpand(True)
        settings_title.set_halign(Gtk.Align.START)
        sidebar_header.append(settings_title)
        close_settings_btn = Gtk.Button()
        _set_button_icon(close_settings_btn, "window-close-symbolic", "window-close", text_fallback="\u2715")
        close_settings_btn.add_css_class("flat")
        close_settings_btn.add_css_class("circular")
        close_settings_btn.set_valign(Gtk.Align.CENTER)
        close_settings_btn.set_tooltip_text(_("Close"))
        close_settings_btn.connect("clicked", lambda *a: self._close_settings_sidebar())
        sidebar_header.append(close_settings_btn)
        sidebar.append(sidebar_header)

        # Category switcher bar (Assistant / Speech / Theme).
        settings_switcher = Adw.ViewSwitcher()
        settings_switcher.set_stack(settings_view_stack)
        settings_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        settings_switcher.set_halign(Gtk.Align.CENTER)
        switcher_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        switcher_bar.add_css_class("settings-switcher-bar")
        switcher_bar.set_halign(Gtk.Align.CENTER)
        switcher_bar.append(settings_switcher)
        sidebar.append(switcher_bar)

        settings_view_stack.set_vexpand(True)
        sidebar.append(settings_view_stack)

        self.settings_revealer.set_child(sidebar)
        try:
            translate_dialog(sidebar)
        except Exception:
            pass

        self.settings_scrim.set_visible(True)
        self.settings_revealer.set_reveal_child(True)

    def launch_in_app_process(self, title, cmd_string, is_ollama=False, initial_status=None, on_close_callback=None, sudo_manager=None, model_name=None):
        """Robustly launch a subprocess and stream its output to a native GTK _ActionProgressWindow."""
        win = _ActionProgressWindow(
            parent=self.window if self.window else self.get_root(),
            title=title,
            cmd_string=cmd_string,
            is_ollama=is_ollama,
            initial_status=initial_status,
            on_close_callback=on_close_callback,
            sudo_manager=sudo_manager,
            model_name=model_name
        )
        win.present()

    def on_pull_ollama_clicked(self, model_name, callback=None, combo_row=None):
        if not model_name:
            self.add_message_bubble("assistant", _("Please enter a model name to pull."))
            return
            
        def after_pull(success):
            if success and combo_row:
                self._refresh_ollama_models(combo_row)
            if success and callback:
                callback(True)
                
        cmd = f"ollama pull {model_name}"
        self.launch_in_app_process(_("Downloading {}").format(model_name), cmd, is_ollama=True, initial_status=_("Initiating download..."), on_close_callback=after_pull, model_name=model_name)

    def on_remove_ollama_clicked(self, combo_row, callback=None):
        idx = combo_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.dynamic_models):
            return
            
        model_name = self.dynamic_models[idx]
        if not model_name:
            self.add_message_bubble("assistant", _("No downloaded model selected to remove."))
            return
            
        def after_rm(success):
            if success:
                self._refresh_ollama_models(combo_row)
                self.add_message_bubble("assistant", _("Model {} has been successfully removed.").format(model_name))
            if callback:
                callback(success)
                
        cmd = f"ollama rm {model_name}"
        self.launch_in_app_process(f"Removing {model_name}", cmd, is_ollama=True, initial_status=_("Deleting model files..."), on_close_callback=after_rm)

    def _refresh_ollama_models(self, combo_row):
        local_models = Gtk.StringList()
        self.dynamic_models = []
        parsed = self.get_ollama_models()
        if parsed:
            for name, size in parsed:
                local_models.append(f"{name} ({size})")
                self.dynamic_models.append(name)
        else:
            if not self.is_ollama_installed():
                local_models.append(_("Ollama Not Installed"))
            else:
                local_models.append(_("No Models Downloaded"))
            self.dynamic_models.append("")
            
        combo_row.set_model(local_models)
        
        try:
            if self.local_model in self.dynamic_models:
                idx = self.dynamic_models.index(self.local_model)
                combo_row.set_selected(idx)
            else:
                combo_row.set_selected(0)
        except Exception:
            combo_row.set_selected(0)


    def is_ollama_installed(self):
        import shutil
        return shutil.which("ollama") is not None

    def get_ollama_models(self):
        models = []
        if not self.is_ollama_installed():
            return models
        try:
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')[1:] # type: ignore
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 3:
                        name = parts[0]
                        size = parts[-3] + " " + parts[-2] if parts[-2] in ["GB", "MB", "KB", "B"] else parts[-2] + " " + parts[-1]
                        # Handling the size column which might be spaced like '4.7 GB'
                        # It's better to just search for the known units
                        size_idx = -1
                        for i, p in enumerate(parts):
                            if p in ["GB", "MB", "KB", "B"]:
                                size_idx = i
                                break
                        if size_idx != -1 and size_idx > 0:
                            size = f"{parts[size_idx-1]} {parts[size_idx]}"
                        else:
                            size = "Unknown Size"
                        models.append((name, size))
        except Exception:
            pass
        return models

    def _prompt_for_password_dialog(self, success_callback, message, cancel_callback=None):
        """Prompt for password via Gtk interface utilizing linexin-center manager"""
        manager = self.sudo_manager
        if not manager:
            self.add_message_bubble("assistant", _("Sudo manager not available in environment."))
            if cancel_callback: cancel_callback()
            return

        dialog = Adw.MessageDialog(
            transient_for=self.window if self.window else self.get_root(),
            heading=_("Authentication Required"),
            body=message
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("authenticate", _("Authenticate"))
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        entry = Gtk.PasswordEntry()
        box.append(entry)
        dialog.set_extra_child(box)
        
        def response_handler(dlg, response):
            if response == "authenticate":
                password = entry.get_text()
                if manager.validate_password(password):
                    manager.set_password(password)
                    success_callback()
                else:
                    err_dlg = Adw.MessageDialog(
                        transient_for=self.window if self.window else self.get_root(),
                        heading=_("Error"),
                        body=_("Invalid password.")
                    )
                    err_dlg.add_response("ok", _("OK"))
                    def err_response(d, r):
                        if cancel_callback: cancel_callback()
                    err_dlg.connect("response", err_response)
                    translate_dialog(err_dlg)
                    err_dlg.present()
            else:
                if cancel_callback: cancel_callback()

        dialog.connect("response", response_handler)
        translate_dialog(dialog)
        dialog.present()

    def on_ollama_install_clicked(self, btn=None, callback=None):
        manager = self.sudo_manager
        if not manager:
            self.add_message_bubble("assistant", _("Error: Sudo manager not available."))
            return
            
        if not manager.user_password:
            self._prompt_for_password_dialog(
                lambda: self.on_ollama_install_clicked(btn, callback), 
                _("Please enter your password to install Ollama via system privileges.")
            )
            return

        def after_install(success):
            if success and callback:
                callback(True)

        cmd = "curl -fsSL https://ollama.com/install.sh | sh"
        self.launch_in_app_process(_("Installing Ollama"), cmd, is_ollama=False, initial_status=_("Downloading and installing Ollama daemon..."), on_close_callback=after_install, sudo_manager=manager)

    def cancel_generation(self):
        # If TTS is playing but LLM is not processing, just stop TTS silently
        if getattr(self, 'tts_playing', False) and not getattr(self, 'llm_processing', False):
            self._stop_tts()
            return
        self._stop_tts()
        self.abort_processing = True
        self.llm_processing = False
        self._stream_discard()
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.entry.set_sensitive(True)
        self.send_btn.set_icon_name(self._icon_send)
        self.stt_toggle.set_sensitive(True)
        self.new_conv_btn.set_sensitive(True)
        self.conv_toggle_btn.set_sensitive(True)
        self.settings_btn.set_sensitive(True)
        self.add_message_bubble("assistant", _("Generation stopped by user."))

    def _stop_tts(self):
        """Kill any running TTS process and reset state."""
        if hasattr(self, '_tts_proc') and self._tts_proc:
            try:
                os.killpg(os.getpgid(self._tts_proc.pid), 9)
            except Exception:
                try:
                    self._tts_proc.kill()
                except Exception:
                    pass
            self._tts_proc = None
        self.tts_playing = False
        self.send_btn.set_icon_name(self._icon_send)
        self.stt_toggle.set_sensitive(True)
        self.new_conv_btn.set_sensitive(True)
        self.conv_toggle_btn.set_sensitive(True)
        self.settings_btn.set_sensitive(True)
        self.entry.set_sensitive(True)
        self.entry.grab_focus()

    def _correct_voice_text(self, raw_text):
        """Use a one-shot LLM call to correct STT transcription.
        Returns the corrected text, or the original on any failure."""
        correction_prompt = (
            "You are a text correction assistant. The following text was produced by "
            "speech-to-text and may contain errors. Fix punctuation, capitalization, "
            "and obviously misheard words. If the text has no sense and you feel the meaning was different by the context of the message, you can change it." 
            "Return ONLY the corrected text, nothing else. "
            "Do not add explanations."
        )
        messages = [
            {"role": "system", "content": correction_prompt},
            {"role": "user", "content": raw_text}
        ]

        try:
            if self.backend == "direct":
                url = self.api_url.rstrip("/")
                if not url.endswith("/chat/completions"):
                    url = url + "/chat/completions"
                data = {"model": self.model, "messages": messages}
                req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}"
                })
                with urllib.request.urlopen(req, timeout=60) as response:
                    result = json.loads(response.read().decode('utf-8'))
                    corrected = result['choices'][0]['message']['content'].strip()
                    return corrected if corrected else raw_text

        except Exception as e:
            print(f"[Voice correction] Failed, using raw text: {e}")
            return raw_text

    # --- Image / Vision support ---

    def _on_screen_toggle(self, btn):
        """Toggle screen awareness on or off."""
        new_state = btn.get_active()
        print(f"[Screen Awareness] _on_screen_toggle called: active={new_state}")
        self.screen_awareness_active = new_state
        if self.screen_awareness_active:
            self.screen_toggle.add_css_class("suggested-action")
        else:
            self.screen_toggle.remove_css_class("suggested-action")

    def _capture_screenshot(self):
        """Capture a screenshot, downscale to a model-friendly resolution, and return (mime_type, base64_data) or None on failure.
        
        Uses the XDG Desktop Portal (org.freedesktop.portal.Screenshot) for
        maximum compatibility across X11, Wayland, GNOME, KDE, sway, etc.
        Falls back to CLI tools if the portal is unavailable.
        """
        screenshot_dir = "/tmp/linexin"
        os.makedirs(screenshot_dir, mode=0o700, exist_ok=True)
        screenshot_path = os.path.join(screenshot_dir, f"screen_{uuid.uuid4().hex}.png")

        captured = False

        print(f"[Screen Awareness] Attempting screenshot -> {screenshot_path}")

        # --- Primary: XDG Desktop Portal ---
        try:
            print("[Screen Awareness] Trying XDG Desktop Portal (org.freedesktop.portal.Screenshot)...")
            captured = self._capture_via_portal(screenshot_path)
            if captured:
                print("[Screen Awareness] Screenshot captured via XDG Desktop Portal")
            else:
                print("[Screen Awareness] XDG Desktop Portal returned no image")
        except Exception as e:
            print(f"[Screen Awareness] Portal screenshot failed: {e}")

        # --- Fallback: CLI tools ---
        if not captured:
            print("[Screen Awareness] Falling back to CLI tools...")
            captured = self._capture_via_cli(screenshot_path)

        if not captured or not os.path.isfile(screenshot_path):
            print("[Screen Awareness] All screenshot methods failed")
            return None

        print(f"[Screen Awareness] Processing screenshot ({os.path.getsize(screenshot_path)} bytes)")
        return self._process_screenshot(screenshot_path)

    def _capture_via_portal(self, dest_path):
        """Take a screenshot using the XDG Desktop Portal D-Bus API.
        Returns True if a screenshot was saved to dest_path, False otherwise."""
        import time
        from gi.repository import Gio  # type: ignore

        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        token = f"linexin_{uuid.uuid4().hex[:8]}"
        sender_name = bus.get_unique_name().replace(".", "_").lstrip(":")
        handle_path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{token}"

        result_uri = [None]  # mutable container for the closure
        got_response = [False]

        def on_signal(_connection, _sender, _object_path, _interface, _signal, parameters):
            response, results = parameters.unpack()
            if response == 0:  # success
                uri = results.get("uri", "")
                if uri:
                    result_uri[0] = uri
            got_response[0] = True

        sub_id = bus.signal_subscribe(
            "org.freedesktop.portal.Desktop",
            "org.freedesktop.portal.Request",
            "Response",
            handle_path,
            None,
            Gio.DBusSignalFlags.NO_MATCH_RULE,
            on_signal,
        )

        try:
            bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                GLib.Variant("(sa{sv})", ("", {
                    "handle_token": GLib.Variant("s", token),
                    "interactive": GLib.Variant("b", False),
                })),
                None,
                Gio.DBusCallFlags.NONE,
                5000,  # 5-second timeout for the D-Bus method call
                None,
            )

            # Pump the GLib main context so the D-Bus Response signal can
            # be dispatched.  A plain threading.Event.wait() would deadlock
            # because on_send_clicked runs on the main thread.
            ctx = GLib.MainContext.default()
            deadline = time.monotonic() + 10
            while not got_response[0] and time.monotonic() < deadline:
                ctx.iteration(False)
                if not got_response[0]:
                    time.sleep(0.02)
        finally:
            bus.signal_unsubscribe(sub_id)

        uri = result_uri[0]
        if not uri:
            return False

        # Portal returns a file:// URI — copy/move to our destination
        src_path = uri.replace("file://", "") if uri.startswith("file://") else uri
        try:
            import shutil
            shutil.copy2(src_path, dest_path)
            if not os.path.isfile(dest_path):
                return False
            # Remove the portal's original file (usually in ~/Pictures)
            # so it does not accumulate after each Screen Awareness query.
            try:
                os.remove(src_path)
                print(f"[Screen Awareness] Removed portal source file {src_path}")
            except Exception as e_rm:
                print(f"[Screen Awareness] Could not remove portal source file: {e_rm}")
            return True
        except Exception as e:
            print(f"[Screen Awareness] Failed to copy portal screenshot: {e}")
            return False

    def _capture_via_cli(self, screenshot_path):
        """Fallback: try CLI screenshot tools. Returns True if captured."""
        import shutil
        commands = [
            ["grim", screenshot_path],                              # Wayland (sway, etc.)
            ["gnome-screenshot", "-f", screenshot_path],            # GNOME
            ["spectacle", "-b", "-n", "-f", "-o", screenshot_path], # KDE
            ["scrot", screenshot_path],                             # X11 fallback
            ["import", "-window", "root", screenshot_path],        # ImageMagick X11
        ]
        for cmd in commands:
            if not shutil.which(cmd[0]):
                print(f"[Screen Awareness]   {cmd[0]}: not found, skipping")
                continue
            try:
                print(f"[Screen Awareness]   Trying {cmd[0]}...")
                result = subprocess.run(
                    cmd, capture_output=True, timeout=10
                )
                if result.returncode == 0 and os.path.isfile(screenshot_path):
                    print(f"[Screen Awareness]   Screenshot captured via {cmd[0]}")
                    return True
                else:
                    print(f"[Screen Awareness]   {cmd[0]} failed (rc={result.returncode})")
            except subprocess.TimeoutExpired:
                continue
            except Exception:
                continue
        return False

    def _process_screenshot(self, screenshot_path):
        """Encode a screenshot file as base64. Returns (mime_type, base64_data) or None."""
        try:
            with open(screenshot_path, "rb") as f:
                raw = f.read()
            b64 = base64.b64encode(raw).decode("ascii")
            return ("image/png", b64)
        except Exception as e:
            print(f"[Screen Awareness] Failed to read screenshot: {e}")
            return None

    def _cleanup_screenshot_tmp(self):
        """Remove the /tmp/linexin screenshot directory and all its contents."""
        import shutil
        for screenshot_dir in ["/tmp/linexin"]:
            if os.path.isdir(screenshot_dir):
                try:
                    shutil.rmtree(screenshot_dir)
                    print(f"[Screen Awareness] Cleaned up {screenshot_dir}")
                except Exception as e:
                    print(f"[Screen Awareness] Failed to clean up {screenshot_dir}: {e}")

    def _on_clipboard_texture_ready(self, clipboard, result):
        """Callback for async clipboard texture read."""
        try:
            texture = clipboard.read_texture_finish(result)
            if texture:
                self._add_image_from_texture(texture)
        except Exception as e:
            print(f"[Image paste] Failed: {e}")

    def _on_texture_drop(self, drop_target, value, x, y):
        """Handle a Gdk.Texture dropped onto the input area."""
        self._add_image_from_texture(value)
        return True

    def _on_file_list_drop(self, drop_target, value, x, y):
        """Handle files dropped from a file manager onto the input area."""
        files = value.get_files()
        for gfile in files:
            path = gfile.get_path()
            if path and self._is_image_file(path):
                self._add_image_from_file(path)
        return True

    def _is_image_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        return ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp")

    def _guess_mime_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        mime_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".tiff": "image/tiff",
            ".tif": "image/tiff", ".webp": "image/webp",
        }
        return mime_map.get(ext, "image/png")

    def _add_image_from_texture(self, texture):
        """Convert a Gdk.Texture to base64 PNG and add it to pending images."""
        try:
            png_bytes = texture.save_to_png_bytes()
            raw = png_bytes.get_data()
            b64 = base64.b64encode(raw).decode('ascii')
            self._add_pending_image(b64, "image/png", texture)
        except Exception as e:
            print(f"[Image] Failed to encode texture: {e}")

    def _add_image_from_file(self, path):
        """Read an image file and add it to pending images."""
        try:
            from gi.repository import Gdk  # type: ignore
            with open(path, 'rb') as f:
                raw = f.read()
            mime = self._guess_mime_type(path)
            b64 = base64.b64encode(raw).decode('ascii')
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
            self._add_pending_image(b64, mime, texture)
        except Exception as e:
            print(f"[Image] Failed to load file {path}: {e}")

    def _add_pending_image(self, b64_data, mime_type, texture=None):
        """Add an image to the pending list and update the preview strip."""
        self.pending_images.append((mime_type, b64_data))
        self._rebuild_image_preview()

    def _remove_pending_image(self, index):
        """Remove an image from the pending list by index."""
        if 0 <= index < len(self.pending_images):
            self.pending_images.pop(index)
            self._rebuild_image_preview()

    def _rebuild_image_preview(self):
        """Rebuild the image preview strip from the pending images list."""
        from gi.repository import Gdk  # type: ignore
        # Clear existing preview children
        while True:
            child = self.image_preview_box.get_first_child()
            if child is None:
                break
            self.image_preview_box.remove(child)

        for idx, (mime_type, b64_data) in enumerate(self.pending_images):
            raw = base64.b64decode(b64_data)
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw))
            except Exception:
                continue

            overlay = Gtk.Overlay()
            picture = Gtk.Picture.new_for_paintable(texture)
            picture.set_size_request(60, 60)
            picture.set_can_shrink(True)
            picture.set_content_fit(Gtk.ContentFit.COVER)
            frame = Gtk.Frame()
            frame.set_child(picture)
            frame.set_size_request(60, 60)
            overlay.set_child(frame)

            close_btn = Gtk.Button()
            _set_button_icon(close_btn, "window-close-symbolic", "window-close",
                             "dialog-close", text_fallback="✕")
            close_btn.add_css_class("circular")
            close_btn.add_css_class("osd")
            close_btn.set_halign(Gtk.Align.END)
            close_btn.set_valign(Gtk.Align.START)
            close_btn.set_margin_top(2)
            close_btn.set_margin_end(2)
            captured_idx = idx
            close_btn.connect("clicked", lambda b, i=captured_idx: self._remove_pending_image(i))
            overlay.add_overlay(close_btn)

            self.image_preview_box.append(overlay)

        self.image_preview_box.set_visible(len(self.pending_images) > 0)

    def _extract_text_from_content(self, content):
        """Extract plain text from a message content (handles both str and multimodal list)."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
        return str(content)

    _SCREEN_AWARENESS_PREFIX = "[A screenshot of my current screen is attached. IMPORTANT: If my question is NOT about the screen content, do NOT describe, mention, reference, or acknowledge the screenshot in any way — just answer my question directly as if no screenshot was provided. Only use the screenshot if my question is specifically about what is on screen. LANGUAGE RULE: The language visible in the screenshot must NEVER influence your reply language. Always reply in the language of my text message, regardless of what language appears on screen.]\n\n"

    def _strip_system_instructions(self, content):
        """Return a display-safe copy of content with the screen-awareness LLM preamble removed."""
        prefix = self._SCREEN_AWARENESS_PREFIX
        if isinstance(content, list):
            stripped = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item["text"].startswith(prefix):
                    clean_text = item["text"][len(prefix):]
                    if clean_text:
                        stripped.append({"type": "text", "text": clean_text})
                else:
                    stripped.append(item)
            return stripped
        if isinstance(content, str) and content.startswith(prefix):
            return content[len(prefix):]
        return content

    def _extract_images_from_content(self, content):
        """Extract image data URLs from a multimodal content list."""
        if not isinstance(content, list):
            return []
        images = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image_url":
                url = item.get("image_url", {}).get("url", "")
                if url:
                    images.append(url)
        return images

    def on_send_clicked(self, widget):
        if getattr(self, 'llm_processing', False) or getattr(self, 'tts_playing', False):
            self.cancel_generation()
            return
            
        text = self.entry.get_text().strip()
        if not text and not self.pending_images:
            return

        if self.stt_toggle.get_active():
            self.stt_toggle.set_active(False)

        is_voice = getattr(self, '_last_input_was_voice', False)
        self._speak_next_response = is_voice
        self._last_input_was_voice = False

        if self.backend == "direct" and not self.api_key:
            self.add_message_bubble("assistant", _("Please configure your API Key in settings first."))
            return

        # Capture pending images before clearing
        images = list(self.pending_images)
        self.pending_images.clear()
        self._rebuild_image_preview()

        # Screen Awareness: capture screenshot and attach it
        # In voice-autostart mode, screen awareness is always forced on
        screen_active = self.screen_awareness_active or getattr(self, '_voice_autostart', False)
        print(f"[Screen Awareness] on_send_clicked: screen_awareness_active={self.screen_awareness_active}, _voice_autostart={getattr(self, '_voice_autostart', False)}, effective={screen_active}")
        has_screen_capture = False
        if screen_active:
            screenshot = self._capture_screenshot()
            if screenshot:
                images.append(screenshot)
                has_screen_capture = True
            else:
                self.add_message_bubble("assistant", _("Failed to capture screenshot. No screenshot tool found (grim, gnome-screenshot, spectacle, scrot, or import)."))

        self.entry.set_text("")
        self.entry.set_sensitive(False)
        self.send_btn.set_icon_name(self._icon_stop)
        self.stt_toggle.set_sensitive(False)
        self.new_conv_btn.set_sensitive(False)
        self.conv_toggle_btn.set_sensitive(False)
        self.settings_btn.set_sensitive(False)
        self.llm_processing = True
        self.abort_processing = False
        self.spinner.set_visible(True)
        self.spinner.start()

        # Check if voice correction is enabled for the active backend
        vc_enabled = (
            (self.backend == "direct" and self.voice_correction_direct)
        )

        if is_voice and vc_enabled:
            # Run voice correction silently in background, then proceed
            def voice_correction_thread():
                corrected = self._correct_voice_text(text)
                if getattr(self, 'abort_processing', False):
                    return
                GLib.idle_add(self._proceed_with_message, corrected, images, has_screen_capture)
            threading.Thread(target=voice_correction_thread, daemon=True).start()
        else:
            self._proceed_with_message(text, images, has_screen_capture)

    def _proceed_with_message(self, text, images=None, has_screen_capture=False):
        """Add the (possibly corrected) user message to the UI and fire the AI call."""
        if images:
            # Build multimodal content (OpenAI vision format)
            content = []
            display_content = []
            if text:
                if has_screen_capture:
                    # Instruct the LLM to only use the screenshot as context when relevant
                    content.append({"type": "text", "text": f"{self._SCREEN_AWARENESS_PREFIX}{text}"})
                else:
                    content.append({"type": "text", "text": text})
                display_content.append({"type": "text", "text": text})
            for mime_type, b64_data in images:
                image_item = {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
                }
                content.append(image_item)
                display_content.append(image_item)
            self.add_message_bubble("user", display_content)
            self.chat_history.append({"role": "user", "content": content})
        else:
            self.add_message_bubble("user", text)
            self.chat_history.append({"role": "user", "content": text})
        self._show_thinking_indicator()
        threading.Thread(target=self.call_ai, daemon=True).start()

    def _show_thinking_indicator(self):
        """Show an animated thinking indicator bubble."""
        self._remove_thinking_indicator()
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row._is_thinking_indicator = True
        row.add_css_class("message-row")
        row.add_css_class("assistant-message-row")
        row.add_css_class("thinking-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("message-box")
        box.add_css_class("assistant-message-box")
        box.add_css_class("last-in-group")
        box.set_halign(Gtk.Align.START)

        # Check for a custom thinking indicator SVG in the theme
        thinking_svg = self._get_theme_svg("thinking-indicator.svg")
        if thinking_svg:
            # Minimalist custom bubble (like iMessage typing indicator)
            avatar = Gtk.Image.new_from_file(thinking_svg)
            avatar.set_pixel_size(-1)  # Use natural SVG size (40x16)

            bubble = Gtk.Box()
            bubble.add_css_class("message-bubble")
            bubble.add_css_class("assistant-bubble")
            bubble.add_css_class("thinking-bubble")
            bubble.append(avatar)
            box.append(bubble)
        else:
            # Fallback to standard animated spinner with avatar
            if os.path.isfile(self.alexy_icon_path):
                icon = Gtk.Image.new_from_file(self.alexy_icon_path)
            else:
                icon = Gtk.Image.new_from_icon_name(self.widgeticon)
            icon.set_pixel_size(24)
            icon.set_valign(Gtk.Align.START)
            box.append(icon)

            inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            spinner = Gtk.Spinner()
            spinner.start()
            inner.append(spinner)
            label = Gtk.Label(label=_("Thinking..."))
            label.add_css_class("dim-label")
            inner.append(label)
            box.append(inner)

        row.set_child(box)
        self.chat_listbox.append(row)
        self._thinking_row = row

        def scroll_to_bottom():
            adj = self.scrolled_window.get_vadjustment()
            if adj:
                adj.set_value(adj.get_upper() - adj.get_page_size())
            return False
        GLib.timeout_add(100, scroll_to_bottom)

    def _remove_thinking_indicator(self):
        """Remove the thinking indicator bubble if present."""
        # This breaks the "last-in-group" chain conceptually since it might be 
        # visually between two assistant messages. We reset it to ensure the 
        # actual next reply gets a proper group class.
        self._last_bubble_role = None
        self._last_bubble_box = None
        
        if hasattr(self, '_thinking_row') and self._thinking_row:
            try:
                self.chat_listbox.remove(self._thinking_row)
            except Exception:
                pass
            self._thinking_row = None

    # ----------------------------------------------------------------- #
    #  Streaming assistant reply (word-by-word typewriter + reasoning)   #
    # ----------------------------------------------------------------- #

    def _stream_begin(self):
        """Create the streaming assistant bubble and start the typewriter.

        Runs on the main loop. Replaces the 'Thinking...' indicator with a real
        assistant bubble that grows as tokens arrive. A collapsible 'reasoning'
        section is added lazily the first time reasoning text appears.
        """
        self._remove_thinking_indicator()

        # Maintain message grouping (directional tails) like add_message_bubble.
        if not hasattr(self, '_last_bubble_role'):
            self._last_bubble_role = None
            self._last_bubble_box = None
        if self._last_bubble_role == "assistant" and self._last_bubble_box:
            self._last_bubble_box.remove_css_class("last-in-group")

        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.add_css_class("message-row")
        row.add_css_class("assistant-message-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.add_css_class("message-box")
        box.add_css_class("assistant-message-box")
        box.add_css_class("last-in-group")
        box.set_halign(Gtk.Align.FILL)
        box.set_hexpand(True)
        self._last_bubble_role = "assistant"
        self._last_bubble_box = box

        if os.path.isfile(self.alexy_icon_path):
            icon = Gtk.Image.new_from_file(self.alexy_icon_path)
        else:
            icon = Gtk.Image.new_from_icon_name(self.widgeticon)
        icon.set_pixel_size(24)
        icon.set_valign(Gtk.Align.START)
        box.append(icon)

        bubble = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bubble.add_css_class("message-bubble")
        bubble.add_css_class("assistant-bubble")
        bubble.set_halign(Gtk.Align.FILL)
        bubble.set_hexpand(True)

        content_label = Gtk.Label()
        content_label.add_css_class("message-label")
        content_label.set_halign(Gtk.Align.FILL)
        content_label.set_hexpand(True)
        content_label.set_wrap(True)
        content_label.set_selectable(True)
        content_label.set_xalign(0.0)
        bubble.append(content_label)

        box.append(bubble)
        row.set_child(box)
        self.chat_listbox.append(row)

        self._stream_ctx = {
            "row": row,
            "bubble": bubble,
            "content_label": content_label,
            "reasoning_expander": None,
            "reasoning_label": None,
            "reasoning_spinner": None,
            # Raw accumulators (filled from the worker thread via _stream_push).
            "raw_content": "",
            "reasoning_field": "",
            # Targets derived from the accumulators.
            "content_target": "",
            "reasoning_target": "",
            # How much has been revealed by the typewriter so far.
            "content_shown": 0,
            "reasoning_shown": 0,
            "done": False,
            "reply": "",
            "speak": False,
            "final": True,
            "finished": False,
        }
        self._stream_tick_id = GLib.timeout_add(16, self._stream_tick, self._stream_ctx)
        GLib.timeout_add(60, self._scroll_chat_to_bottom)

    def _ensure_reasoning_expander(self):
        """Lazily create the collapsible reasoning section inside the bubble."""
        ctx = self._stream_ctx
        if ctx is None or ctx["reasoning_expander"] is not None:
            return
        expander = Gtk.Expander()
        expander.set_expanded(False)
        expander.add_css_class("reasoning-expander")
        expander.set_halign(Gtk.Align.FILL)
        expander.set_hexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_halign(Gtk.Align.FILL)
        header.set_hexpand(True)
        spinner = Gtk.Spinner()
        spinner.start()
        header.append(spinner)
        title = Gtk.Label(label=_("Thinking..."))
        title.add_css_class("dim-label")
        title.add_css_class("caption")
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        header.append(title)
        expander.set_label_widget(header)

        reasoning_label = Gtk.Label()
        reasoning_label.add_css_class("reasoning-text")
        reasoning_label.add_css_class("dim-label")
        reasoning_label.set_halign(Gtk.Align.FILL)
        reasoning_label.set_hexpand(True)
        reasoning_label.set_wrap(True)
        reasoning_label.set_selectable(True)
        reasoning_label.set_xalign(0.0)
        reasoning_label.set_margin_top(6)
        reasoning_label.set_margin_start(6)
        reasoning_label.set_margin_bottom(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(220)
        scroll.set_propagate_natural_height(True)
        scroll.set_halign(Gtk.Align.FILL)
        scroll.set_hexpand(True)
        scroll.set_child(reasoning_label)
        expander.set_child(scroll)

        # Insert the expander above the content label.
        ctx["bubble"].prepend(expander)
        ctx["reasoning_expander"] = expander
        ctx["reasoning_label"] = reasoning_label
        ctx["reasoning_spinner"] = spinner
        ctx["reasoning_title"] = title

    def _recompute_stream_targets(self):
        """Derive visible content + reasoning targets from the raw accumulators.

        Inline <think>...</think> blocks in the content channel are routed to the
        reasoning section, so models that embed their chain-of-thought in the
        normal content stream are handled the same as those using a dedicated
        reasoning field.
        """
        import re
        ctx = self._stream_ctx
        if ctx is None:
            return
        raw = ctx["raw_content"]
        inline_reasoning = []
        visible = []
        inside = False
        for part in re.split(r'(<think>|</think>)', raw):
            if part == "<think>":
                inside = True
                continue
            if part == "</think>":
                inside = False
                continue
            if inside:
                inline_reasoning.append(part)
            else:
                visible.append(part)
        reasoning = ctx["reasoning_field"]
        if inline_reasoning:
            reasoning = reasoning + "".join(inline_reasoning)
        ctx["content_target"] = "".join(visible)
        ctx["reasoning_target"] = reasoning
        if reasoning.strip():
            self._ensure_reasoning_expander()

    def _stream_push(self, content_delta, reasoning_delta):
        """Append newly received tokens (called on the main loop)."""
        ctx = self._stream_ctx
        if ctx is None:
            return False
        if content_delta:
            ctx["raw_content"] += content_delta
        if reasoning_delta:
            ctx["reasoning_field"] += reasoning_delta
        self._recompute_stream_targets()
        return False

    def _stream_tick(self, ctx):
        """Typewriter animation: reveal a few more characters each frame.

        Bound to a specific context so that a follow-up reply (e.g. after
        autonomous command execution starts a new bubble) animates its own
        bubble independently.
        """
        if ctx is None:
            return False

        advanced = False
        # Reveal reasoning text.
        r_target = ctx["reasoning_target"]
        if ctx["reasoning_shown"] < len(r_target):
            remaining = len(r_target) - ctx["reasoning_shown"]
            step = max(3, remaining // 8)
            ctx["reasoning_shown"] = min(len(r_target), ctx["reasoning_shown"] + step)
            if ctx["reasoning_label"] is not None:
                ctx["reasoning_label"].set_text(r_target[:ctx["reasoning_shown"]])
            advanced = True

        # Reveal main content text.
        c_target = ctx["content_target"]
        if ctx["content_shown"] < len(c_target):
            remaining = len(c_target) - ctx["content_shown"]
            step = max(2, remaining // 10)
            ctx["content_shown"] = min(len(c_target), ctx["content_shown"] + step)
            ctx["content_label"].set_text(c_target[:ctx["content_shown"]])
            advanced = True

        if advanced:
            self._scroll_chat_to_bottom()

        caught_up = (ctx["content_shown"] >= len(c_target)
                     and ctx["reasoning_shown"] >= len(r_target))
        if ctx["done"] and caught_up:
            self._stream_complete(ctx)
            return False
        return True

    def _stream_finish(self, reply, speak, final=True):
        """Signal that the network stream has ended (called on the main loop).

        The typewriter keeps running until it catches up, then _stream_complete
        finalizes the bubble. 'final' is False for the intermediate reply that
        triggers autonomous command execution (input stays locked).
        """
        ctx = getattr(self, "_stream_ctx", None)
        if ctx is None:
            # Nothing was streamed (e.g. immediate empty reply): fall back.
            if final:
                self.on_api_success(reply)
            return False
        ctx["reply"] = reply
        ctx["speak"] = speak
        ctx["final"] = final
        ctx["done"] = True
        return False

    def _stream_complete(self, ctx):
        """Finalize the streamed bubble: apply markup, unlock input, TTS."""
        if ctx is None or ctx.get("finished"):
            return
        ctx["finished"] = True

        reply = ctx["reply"]
        speak = ctx["speak"]
        final = ctx["final"]

        # Apply full Markdown -> Pango markup now that the text is complete.
        if reply.strip():
            try:
                ctx["content_label"].set_markup(self._markdown_to_pango(reply))
            except Exception:
                ctx["content_label"].set_text(reply)
        else:
            # No visible content (pure reasoning or empty) -> drop empty label.
            ctx["content_label"].set_visible(False)

        # Finalize the reasoning section: stop the spinner and relabel.
        if ctx["reasoning_expander"] is not None:
            if ctx["reasoning_spinner"] is not None:
                ctx["reasoning_spinner"].stop()
                ctx["reasoning_spinner"].set_visible(False)
            if ctx.get("reasoning_title") is not None:
                ctx["reasoning_title"].set_text(_("Reasoning"))

        # If this is the currently-active context, clear the pointer.
        if getattr(self, "_stream_ctx", None) is ctx:
            self._stream_ctx = None

        # Intermediate reply (autonomous commands running): keep input locked.
        if not final:
            return

        self._cleanup_screenshot_tmp()

        if speak:
            self._speak_next_response = False
            self.llm_processing = False
            self.spinner.stop()
            self.spinner.set_visible(False)
            self._save_conversation()
            self.play_tts(reply)
        else:
            self.llm_processing = False
            self.entry.set_sensitive(True)
            self.send_btn.set_icon_name(self._icon_send)
            self.stt_toggle.set_sensitive(True)
            self.new_conv_btn.set_sensitive(True)
            self.conv_toggle_btn.set_sensitive(True)
            self.settings_btn.set_sensitive(True)
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.entry.grab_focus()
            self._save_conversation()

    def _stream_discard(self):
        """Remove the in-progress streaming bubble (on error/abort)."""
        ctx = getattr(self, "_stream_ctx", None)
        if ctx is None:
            return
        if getattr(self, "_stream_tick_id", None):
            try:
                GLib.source_remove(self._stream_tick_id)
            except Exception:
                pass
            self._stream_tick_id = None
        try:
            self.chat_listbox.remove(ctx["row"])
        except Exception:
            pass
        self._last_bubble_role = None
        self._last_bubble_box = None
        self._stream_ctx = None

    def call_ai(self):
        if self.backend == "direct":
            self.call_direct_api()
        elif self.backend == "local":
            self.call_local_ollama()
        elif self.backend == "endpoint":
            self.call_endpoint_api()

    def call_direct_api(self):
        # Ensure the URL ends with /chat/completions
        url = self.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"

        data = {
            "model": self.model,
            "messages": self.chat_history,
            "stream": True
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        })

        self._execute_urllib_request(req)

    def call_endpoint_api(self):
        """Call a local OpenAI-compatible server using its endpoint URL.

        Unlike the Direct API backend this does not send an Authorization
        header, since local servers (llama.cpp, LM Studio, vLLM, etc.) usually
        run without authentication. The model is auto-detected from the
        server's /models listing, so the user never has to type a model name.
        """
        if not self.endpoint_url:
            GLib.idle_add(self.on_api_error, _("No endpoint URL configured. Please open settings and set the Endpoint URL."))
            return

        # Auto-detect the model from the server if we don't have one cached.
        if not self.endpoint_model:
            detected = self._discover_endpoint_model()
            if detected:
                self.endpoint_model = detected
                GLib.idle_add(self.update_subtitle)

        # Build the chat completions URL from the normalized base.
        url = self._endpoint_base_url() + "/chat/completions"

        data = {
            "model": self.endpoint_model or "local-model",
            "messages": self.chat_history,
            "stream": True
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={
            "Content-Type": "application/json"
        })

        self._execute_urllib_request(req)

    def _endpoint_base_url(self):
        """Return the normalized OpenAI-compatible base URL.

        Strips any trailing /chat/completions and ensures the path ends with the
        standard /v1 segment, which servers like LM Studio, llama.cpp and vLLM
        require. This lets the user enter either "http://127.0.0.1:1234" or
        "http://127.0.0.1:1234/v1" and have it work either way.
        """
        base = (self.endpoint_url or "").strip().rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        base = base.rstrip("/")
        # Add the /v1 segment if the user didn't already include it.
        if base and not (base.endswith("/v1") or "/v1/" in base):
            base = base + "/v1"
        return base

    def _discover_endpoint_model(self):
        """Query the endpoint's OpenAI-compatible /models listing and return the
        first available model id, or None on failure."""
        models_url = self._endpoint_base_url() + "/models"
        try:
            req = urllib.request.Request(models_url, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode('utf-8'))
            items = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(items, list) and items:
                first = items[0]
                if isinstance(first, dict):
                    model_id = first.get("id") or first.get("name")
                    if model_id:
                        print(f"[Endpoint] Auto-detected model: {model_id}")
                        return model_id
        except Exception as e:
            print(f"[Endpoint] Model auto-detection failed: {e}")
        return None



    def call_local_ollama(self):
        if not self.is_ollama_installed():
            def after_install(success):
                if success:
                    # Start the Ollama service after installation
                    try:
                        subprocess.run(["systemctl", "enable", "--now", "ollama"], capture_output=True, timeout=15)
                    except Exception:
                        pass
                    if not self.local_model:
                        GLib.idle_add(self.on_api_error, _("Ollama Installed! Please open Settings to pull an AI model."))
                    else:
                        GLib.idle_add(lambda: threading.Thread(target=self.call_local_ollama).start())
                else:
                    GLib.idle_add(self.on_api_error, _("Ollama installation was cancelled or failed."))
            GLib.idle_add(self.on_ollama_install_clicked, None, after_install)
            return

        if not self.local_model:
            GLib.idle_add(self.on_api_error, _("No local model selected. Please open settings and pull an AI model."))
            return
            
        # Transform messages for Ollama format (images use separate 'images' key)
        ollama_messages = []
        for msg in self.chat_history:
            if isinstance(msg.get("content"), list):
                text_parts = []
                images = []
                for item in msg["content"]:
                    if item.get("type") == "text":
                        text_parts.append(item["text"])
                    elif item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        if "," in url:
                            images.append(url.split(",", 1)[1])
                ollama_msg = {"role": msg["role"], "content": "\n".join(text_parts)}
                if images:
                    ollama_msg["images"] = images
                ollama_messages.append(ollama_msg)
            else:
                ollama_messages.append(msg)

        data = {
            "model": self.local_model,
            "messages": ollama_messages,
            "stream": True
        }
        req = urllib.request.Request(self.local_url, data=json.dumps(data).encode('utf-8'), headers={
            "Content-Type": "application/json"
        })
        
        self._execute_urllib_request(req, is_ollama=True)

    def _execute_urllib_request(self, req, is_ollama=False):
        import re
        try:
            # A generous timeout acts as a safety net against an unresponsive
            # server hanging the worker thread forever, while still allowing
            # slow local models enough time to generate a full response.
            with urllib.request.urlopen(req, timeout=600) as response:
                ctype = (response.headers.get('Content-Type') or '').lower()
                is_sse = 'text/event-stream' in ctype

                # Create the streaming bubble (replaces the Thinking... indicator).
                GLib.idle_add(self._stream_begin)

                content_accum = ""
                reasoning_accum = ""

                if is_sse:
                    # OpenAI-compatible Server-Sent Events stream.
                    for raw in response:
                        if getattr(self, 'abort_processing', False):
                            break
                        line = raw.decode('utf-8', 'replace').strip()
                        if not line or not line.startswith('data:'):
                            continue
                        payload = line[5:].strip()
                        if payload == '[DONE]':
                            break
                        try:
                            obj = json.loads(payload)
                        except Exception:
                            continue
                        choices = obj.get('choices') or []
                        if not choices:
                            continue
                        delta = choices[0].get('delta') or {}
                        c = delta.get('content') or ''
                        r = delta.get('reasoning_content') or delta.get('reasoning') or ''
                        if c or r:
                            content_accum += c
                            reasoning_accum += r
                            GLib.idle_add(self._stream_push, c, r)
                elif is_ollama:
                    # Ollama streams newline-delimited JSON objects.
                    for raw in response:
                        if getattr(self, 'abort_processing', False):
                            break
                        line = raw.decode('utf-8', 'replace').strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        if obj.get('error'):
                            GLib.idle_add(self._stream_discard)
                            GLib.idle_add(self.on_api_error, str(obj.get('error')))
                            return
                        msg = obj.get('message') or {}
                        c = msg.get('content') or ''
                        r = msg.get('thinking') or ''
                        if c or r:
                            content_accum += c
                            reasoning_accum += r
                            GLib.idle_add(self._stream_push, c, r)
                        if obj.get('done'):
                            break
                else:
                    # Non-streaming fallback: the server returned a single JSON
                    # document despite the stream request. Parse it whole and
                    # feed it through the same animated path for consistency.
                    result = json.loads(response.read().decode('utf-8'))
                    try:
                        message = result['choices'][0]['message']
                        content_accum = message.get('content') or ''
                        reasoning_accum = message.get('reasoning_content') or message.get('reasoning') or ''
                    except (KeyError, IndexError, TypeError):
                        err = result.get('error') if isinstance(result, dict) else None
                        if isinstance(err, dict):
                            err = err.get('message', err)
                        GLib.idle_add(self._stream_discard)
                        GLib.idle_add(self.on_api_error, _("Unexpected response from server: {}").format(err or result))
                        return
                    if content_accum or reasoning_accum:
                        GLib.idle_add(self._stream_push, content_accum, reasoning_accum)

                if getattr(self, 'abort_processing', False):
                    GLib.idle_add(self._stream_discard)
                    return

                # The visible reply is the content with any inline <think>
                # blocks removed (those are shown in the reasoning section).
                reply = re.sub(r'<think>.*?</think>', '', content_accum, flags=re.DOTALL).strip()

                self.chat_history.append({"role": "assistant", "content": reply})

                # Autonomous command execution: if the reply contains shell
                # blocks, finalize this bubble (without unlocking input) and let
                # the helper execute the commands and re-fire the API.
                has_cmd = bool(re.findall(r'```(?:bash|sh)\n.*?```', reply, re.DOTALL))
                if has_cmd:
                    GLib.idle_add(self._stream_finish, reply, False, False)
                    if self._run_autonomous_commands(reply, is_ollama):
                        return

                if getattr(self, 'abort_processing', False):
                    return
                speak = getattr(self, '_speak_next_response', False)
                GLib.idle_add(self._stream_finish, reply, speak, True)
                    
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode('utf-8')
            except:
                error_body = ""
            msg = f"HTTP Error {e.code}: {e.reason}\n{error_body}"
            
            if is_ollama and e.code == 404:
                def after_pull(success):
                    if success:
                        GLib.idle_add(lambda: threading.Thread(target=self.call_local_ollama).start())
                    else:
                        GLib.idle_add(self.on_api_error, _("Model download was cancelled or failed."))

                GLib.idle_add(self._stream_discard)
                GLib.idle_add(self.add_message_bubble, "assistant", _("Model '{}' not found locally. Downloading now...").format(self.local_model))
                GLib.idle_add(lambda: self.on_pull_ollama_clicked(self.local_model, after_pull))
                return
                
            GLib.idle_add(self._stream_discard)
            GLib.idle_add(self.on_api_error, msg)
        except urllib.error.URLError as e:
            msg = f"Connection Failed: {str(e)}"
            if is_ollama:
                msg += "\nIs the ollama.service running? Try: `systemctl enable --now ollama`"
            GLib.idle_add(self._stream_discard)
            GLib.idle_add(self.on_api_error, msg)
        except Exception as e:
            msg = f"Error: {str(e)}"
            GLib.idle_add(self._stream_discard)
            GLib.idle_add(self.on_api_error, msg)

    def _run_autonomous_commands(self, reply, is_ollama):
        import re
        import subprocess
        code_blocks = re.findall(r'```(?:bash|sh)\n(.*?)```', reply, re.DOTALL)
        if not code_blocks:
            return False
            
        full_output = ""
        for code in code_blocks:
            if getattr(self, 'abort_processing', False): break

            # Auto-Execution Safety Check
            if not getattr(self, 'auto_execute_commands', True):
                ev_safety = threading.Event()
                safety_allowed = [False]
                def on_safety_allow():
                    safety_allowed[0] = True
                    ev_safety.set()
                def on_safety_deny():
                    ev_safety.set()
                    
                msg_safety = _("The assistant wants to run the following command:\n\n{0}\n\nDo you want to allow this?").format(code)
                
                # Use a standard MessageDialog for the safety prompt
                def show_safety_dialog():
                    dialog = Adw.MessageDialog(
                        transient_for=self.window if self.window else self.get_root(),
                        heading=_("Command Execution Request"),
                        body=msg_safety
                    )
                    dialog.add_response("deny", _("Deny"))
                    dialog.add_response("allow", _("Allow"))
                    dialog.set_response_appearance("allow", Adw.ResponseAppearance.DESTRUCTIVE)
                    
                    def on_response(dlg, response):
                        if response == "allow":
                            on_safety_allow()
                        else:
                            on_safety_deny()
                            
                    dialog.connect("response", on_response)
                    translate_dialog(dialog)
                    dialog.present()
                    
                GLib.idle_add(show_safety_dialog)
                ev_safety.wait()
                
                if not safety_allowed[0]:
                    full_output += f"Command:\n{code}\nExit Code: Denied\n\nSTDERR:\nThe user explicitly denied permission to run this command. You must think of another way or ask the user for clarification.\n\n---\n\n"
                    # We continue rather than break so that multiple commands in one block are individually evaluated or skipped
                    continue

            # Strip sudo from power management commands — logind grants these to the active session user.
            code = re.sub(
                r'\bsudo\s+((?:systemctl\s+)?(?:poweroff|reboot|halt|suspend|hibernate)|shutdown(?:\s+\S+)*)',
                r'\1', code
            )

            manager = self.sudo_manager
            is_privileged = False
            
            if "sudo " in code and manager:
                if not manager.user_password:
                    ev = threading.Event()
                    auth_success = [False]
                    def on_auth():
                        auth_success[0] = True
                        ev.set()
                    def on_cancel():
                        ev.set()
                        
                    msg = _("The assistant wants to run a privileged command:\n\n{0}\n\nPlease authenticate.").format(code)
                    GLib.idle_add(self._prompt_for_password_dialog, on_auth, msg, on_cancel)
                    ev.wait()
                    if not auth_success[0]:
                        full_output += f"Command:\n{code}\nExit Code: Exception\n\nSTDERR:\nUser cancelled sudo authentication.\n\n---\n\n"
                        continue
                
                # Use Linexin Center's native privilege escalation tool
                code_to_run = code.replace("sudo ", f"\"{manager.wrapper_path}\" ")
                manager.start_privileged_session()
                is_privileged = True
                print(f"[DEBUG - AI Sysadmin] Executing autonomous SUDO command:\n{code}\n")
            else:
                print(f"[DEBUG - AI Sysadmin] Executing autonomous non-sudo command:\n{code}\n")
                code_to_run = code
                
            try:
                import time, sys, select
                proc = subprocess.Popen(
                    ["bash", "-c", code_to_run],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, # Neutralizes any Y/N confirm prompts
                    text=True,
                    bufsize=1
                )
                
                stdout_lines: List[str] = []
                start_time = time.time()
                timeout_expired = False
                
                while True:
                    if getattr(self, 'abort_processing', False):
                        proc.terminate()
                        break
                        
                    if time.time() - start_time > 120:
                        proc.kill()
                        timeout_expired = True
                        break
                        
                    stdout = proc.stdout
                    if not stdout:
                        break
                    r, w_fds, x_fds = select.select([stdout], [], [], 0.1)
                    if r:
                        line = stdout.readline() # type: ignore
                        if not line and proc.poll() is not None:
                            break
                        if line:
                            print(line, end="")
                            sys.stdout.flush()
                            stdout_lines.append(str(line))
                    elif proc.poll() is not None:
                        break
                        
                stdout = proc.stdout
                if stdout:
                    remaining = stdout.read() # type: ignore
                    if remaining:
                        print(remaining, end="")
                        stdout_lines.append(str(remaining))
                        
                proc.wait() # Ensure RC is set
                combined_output = "".join(stdout_lines).strip()
                
                if getattr(self, 'abort_processing', False):
                    full_output += f"Command:\n{code}\nExit Code: Aborted\n\nSTDERR:\nUser cancelled the generation.\n\n---\n\n"
                elif timeout_expired:
                    full_output += f"Command:\n{code}\nExit Code: TimeoutExpired\n\nSTDERR:\nThe command took longer than 120 seconds and was terminated.\n\n---\n\n"
                else:
                    combo_out = f"Command:\n{code}\nExit Code: {proc.returncode}"
                    if combined_output: combo_out += f"\n\nOUTPUT:\n{combined_output}"
                    full_output += combo_out + "\n\n---\n\n"
                    print(f"\n[DEBUG - AI Sysadmin] Exit Code: {proc.returncode}")
                    print("-" * 40)
            except Exception as e:
                full_output += f"Command:\n{code}\nExit Code: Exception\n\nSTDERR:\n{str(e)}\n\n---\n\n"
            finally:
                if is_privileged and manager:
                    manager.stop_privileged_session()
                    # Forget password forcibly revokes the token so subsequent sudo requires GUI input
                    manager.forget_password()
                
        if full_output.strip():
            sys_msg = f"System Command Execution Results:\n\n{full_output.strip()}\n\nPlease analyze the output and continue the task, or state that the task is complete."
            self.chat_history.append({"role": "user", "content": sys_msg}) # type: ignore
            
            # Re-fire API recursively in the background thread
            if self.backend == 'local':
                self.call_local_ollama() # type: ignore
            elif self.backend == 'endpoint':
                self.call_endpoint_api() # type: ignore
            else:
                self.call_direct_api() # type: ignore
        return True


    def on_api_success(self, reply):
        if getattr(self, 'abort_processing', False): return
        self._remove_thinking_indicator()
        self._cleanup_screenshot_tmp()

        def _unlock_input():
            self.llm_processing = False
            self.entry.set_sensitive(True)
            self.send_btn.set_icon_name(self._icon_send)
            self.stt_toggle.set_sensitive(True)
            self.new_conv_btn.set_sensitive(True)
            self.conv_toggle_btn.set_sensitive(True)
            self.settings_btn.set_sensitive(True)
            self.spinner.stop()
            self.spinner.set_visible(False)
            self.entry.grab_focus()
            self._save_conversation()

        if getattr(self, '_speak_next_response', False):
            self._speak_next_response = False
            # Show the bubble but keep input disabled while TTS speaks
            self.add_message_bubble("assistant", reply)
            self.llm_processing = False
            self.spinner.stop()
            self.spinner.set_visible(False)
            self._save_conversation()
            # Input stays disabled — TTS stop or natural finish will re-enable it
            self.play_tts(reply)
        else:
            _unlock_input()
            self.add_message_bubble("assistant", reply)

    def play_tts(self, text, on_ready=None):
        import re, subprocess, os, shlex
        
        clean_text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        clean_text = re.sub(r'[*_~`#>]', '', clean_text)
        # Collapse newlines into spaces so TTS reads the full text continuously
        clean_text = re.sub(r'\n+', ' ', clean_text)
        clean_text = re.sub(r'\s{2,}', ' ', clean_text).strip()
        if not clean_text:
            if on_ready:
                GLib.idle_add(on_ready)
            return
        
        lang_map = {
            "small-en-us-0.15": ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium"),
            "small-en-in-0.4": ("en_GB-alba-medium", "en/en_GB/alba/medium"),
            "small-cn-0.22": ("zh_CN-huayan-medium", "zh/zh_CN/huayan/medium"),
            "small-fr-0.22": ("fr_FR-siwis-low", "fr/fr_FR/siwis/low"),
            "small-de-0.15": ("de_DE-thorsten-medium", "de/de_DE/thorsten/medium"),
            "small-es-0.42": ("es_ES-sharvard-medium", "es/es_ES/sharvard/medium"),
            "small-pt-0.3": ("pt_PT-tugao-medium", "pt/pt_PT/tugao/medium"),
            "small-it-0.22": ("it_IT-riccardo-x_low", "it/it_IT/riccardo/x_low"),
            "small-ru-0.22": ("ru_RU-denis-medium", "ru/ru_RU/denis/medium"),
            "small-uk-v3-nano": ("uk_UA-ukromir-medium", "uk/uk_UA/ukromir/medium"),
            "small-pl-0.22": ("pl_PL-gosia-medium", "pl/pl_PL/gosia/medium"),
            "small-ja-0.22": ("ESPEAK", "ja"),
            "small-ko-0.22": ("ESPEAK", "ko")
        }
        fallback = ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium")

        # When using Whisper STT, use its detected language instead of vosk_lang
        if self.stt_backend == "whisper" and getattr(self, '_whisper_detected_lang', ''):
            whisper_lang_map = {
                "en": ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium"),
                "zh": ("zh_CN-huayan-medium", "zh/zh_CN/huayan/medium"),
                "fr": ("fr_FR-siwis-low", "fr/fr_FR/siwis/low"),
                "de": ("de_DE-thorsten-medium", "de/de_DE/thorsten/medium"),
                "es": ("es_ES-sharvard-medium", "es/es_ES/sharvard/medium"),
                "pt": ("pt_PT-tugao-medium", "pt/pt_PT/tugao/medium"),
                "it": ("it_IT-riccardo-x_low", "it/it_IT/riccardo/x_low"),
                "ru": ("ru_RU-denis-medium", "ru/ru_RU/denis/medium"),
                "uk": ("uk_UA-ukromir-medium", "uk/uk_UA/ukromir/medium"),
                "pl": ("pl_PL-gosia-medium", "pl/pl_PL/gosia/medium"),
                "ja": ("ESPEAK", "ja"),
                "ko": ("ESPEAK", "ko"),
            }
            model_name, model_path = whisper_lang_map.get(self._whisper_detected_lang, fallback)
        else:
            model_name, model_path = lang_map.get(self.vosk_lang, fallback)
        
        # Fast-track unsupported AI languages to espeak-ng natively
        if model_name == "ESPEAK":
            def run_espeak():
                print(f"Executing fallback TTS (espeak-ng): -v {model_path}")
                self._tts_proc = subprocess.Popen(["espeak-ng", "-v", model_path, clean_text], preexec_fn=os.setsid)
                self.tts_playing = True
                self.send_btn.set_icon_name(self._icon_stop)
                self.stt_toggle.set_sensitive(False)
                if on_ready:
                    GLib.idle_add(on_ready)
                # Wait for espeak to finish, then reset state
                def watch_espeak():
                    if self._tts_proc:
                        self._tts_proc.wait()
                    GLib.idle_add(self._stop_tts)
                threading.Thread(target=watch_espeak, daemon=True).start()
                return False
            GLib.timeout_add(100, run_espeak)
            return
            
        piper_bin = os.path.expanduser("~/.local/share/linexin/piper/piper")
        model_file = os.path.expanduser(f"~/.local/share/linexin/piper-models/{model_name}.onnx")
        
        def run_piper():
            escaped_text = shlex.quote(clean_text)
            cmd = f"echo {escaped_text} | {piper_bin} --model {model_file} --output_file - | aplay -q"
            print(f"Executing TTS: {cmd}")
            self._tts_proc = subprocess.Popen(["bash", "-c", cmd], preexec_fn=os.setsid)
            self.tts_playing = True
            self.send_btn.set_icon_name(self._icon_stop)
            self.stt_toggle.set_sensitive(False)
            # Wait for piper to finish, then reset state
            def watch_piper():
                if self._tts_proc:
                    self._tts_proc.wait()
                GLib.idle_add(self._stop_tts)
            threading.Thread(target=watch_piper, daemon=True).start()
            return False # GLib timeout requires False to auto-cancel
        
        needs_piper = not os.path.exists(piper_bin)
        needs_model = not os.path.exists(model_file)
        
        if needs_piper or needs_model:
            cmds = ["mkdir -p ~/.local/share/linexin/piper ~/.local/share/linexin/piper-models"]
            if needs_piper:
                cmds.append("curl -sL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz -o /tmp/piper.tar.gz")
                cmds.append("tar -xzf /tmp/piper.tar.gz -C ~/.local/share/linexin/")
                cmds.append("rm -f /tmp/piper.tar.gz")
            if needs_model:
                base_url = f"https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{model_path}/{model_name}.onnx"
                cmds.append(f"curl -sL {base_url} -o {model_file}")
                cmds.append(f"curl -sL {base_url}.json -o {model_file}.json")
                
            full_cmd = " && ".join(cmds)
            
            win = _ActionProgressWindow(
                parent=self.window if self.window else self.get_root(),
                title=_("Downloading Neural TTS Engine/Voice"),
                cmd_string=full_cmd
            )
            def on_done(success):
                if on_ready:
                    GLib.idle_add(on_ready)
                if success:
                    # Detach from the window destroy tick
                    GLib.timeout_add(1000, run_piper)
            win.on_close_callback = on_done
            win.present()
        else:
            if on_ready:
                GLib.idle_add(on_ready)
            run_piper()

    def on_api_error(self, error_msg):
        if getattr(self, 'abort_processing', False): return
        self._remove_thinking_indicator()
        self._cleanup_screenshot_tmp()
        self.llm_processing = False
        self.add_message_bubble("assistant", _("⚠️ Error: ") + error_msg)
        if len(self.chat_history) > 1:
            self.chat_history.pop() # remove failed prompt from history
        self.entry.set_sensitive(True)
        self.send_btn.set_icon_name(self._icon_send)
        self.stt_toggle.set_sensitive(True)
        self.new_conv_btn.set_sensitive(True)
        self.conv_toggle_btn.set_sensitive(True)
        self.settings_btn.set_sensitive(True)
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.entry.grab_focus()

class CompactVoiceWindow(Adw.Window):
    """A small floating pill-shaped voice assistant bar.

    Launched via ``linexin-center -w aa-alexy-ai-widget --voice --compact``
    (e.g. from the hey-linux daemon).  Provides four buttons:

    * Close — terminate the compact window
    * Microphone — toggle speech-to-text recording
    * Settings — open the Alexy AI settings dialog
    * Expand — save the current conversation, open the full Alexy AI widget
      with ``linexin-center -w aa-alexy-ai-widget --conversation <id>``,
      and close the compact window
    """

    _CSS = """
    .compact-voice-window {
        background: transparent;
    }
    .compact-voice-window, .compact-voice-window > * {
        min-width: 0;
        min-height: 0;
    }
    .compact-voice-bar {
        background: alpha(@window_bg_color, 0.92);
        border-radius: 28px;
        border: 1px solid alpha(@borders, 0.35);
        padding: 6px 10px;
        box-shadow: 0 4px 16px alpha(black, 0.18), 0 1px 4px alpha(black, 0.10);
    }
    .compact-voice-bar button {
        border-radius: 50%;
        min-width: 40px;
        min-height: 40px;
        padding: 0;
    }
    .compact-voice-bar .compact-mic-btn {
        background: alpha(@accent_bg_color, 0.12);
        min-width: 48px;
        min-height: 48px;
    }
    .compact-voice-bar .compact-mic-btn:checked {
        background: @accent_bg_color;
        color: @accent_fg_color;
    }
    .compact-voice-bar .compact-mic-btn:disabled {
        opacity: 0.5;
    }
    .compact-status-label {
        font-size: 11px;
        margin-top: 2px;
        margin-bottom: 2px;
    }
    .compact-spinner {
        min-width: 16px;
        min-height: 16px;
    }
    """

    def __init__(self, voice_autostart=False, **kwargs):
        super().__init__(**kwargs)

        self.set_title("Alexy")
        self.set_default_size(260, -1)  # Fixed width matching bar; height shrink-wraps
        self.set_size_request(260, -1)
        self.set_resizable(False)
        self.set_deletable(False)
        self.set_decorated(False)

        # Apply compact CSS
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(self._CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            self.get_display(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 10,
        )

        self.add_css_class("compact-voice-window")

        # Create the hidden AI widget that handles all backend / STT logic.
        # It is never displayed; we use its methods and state only.
        self._ai_widget = LinexinAISysadminWidget(
            hide_sidebar=True,
            window=self,
            voice_autostart=False,  # we control mic ourselves
        )
        # Keep a reference so it is not GC'd
        self._ai_widget.set_visible(False)

        # Track whether voice_autostart was requested
        self._voice_autostart = voice_autostart

        # Enable screen awareness in compact voice mode if configured
        if voice_autostart and self._ai_widget.compact_screen_awareness:
            self._ai_widget.screen_awareness_active = True
            self._ai_widget._voice_autostart = True
            if hasattr(self._ai_widget, 'screen_toggle'):
                self._ai_widget.screen_toggle.set_active(True)

        # ---- Build the pill bar ----
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("compact-voice-bar")
        bar.set_halign(Gtk.Align.CENTER)
        bar.set_valign(Gtk.Align.CENTER)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        bar.set_margin_start(6)
        bar.set_margin_end(6)

        # Close button
        close_btn = Gtk.Button()
        _set_button_icon(close_btn, "window-close-symbolic", "window-close",
                         "dialog-close", text_fallback="✕")
        close_btn.add_css_class("flat")
        close_btn.set_tooltip_text(_("Close"))
        close_btn.connect("clicked", self._on_close_clicked)
        bar.append(close_btn)

        # Microphone toggle
        self._mic_btn = Gtk.ToggleButton()
        mic_icon = Gtk.Image.new_from_icon_name(
            _icon("audio-input-microphone-symbolic", "audio-input-microphone"))
        # Try loading themed mic icon
        theme_mic = self._ai_widget._get_theme_svg("microphone-icon.svg")
        if theme_mic:
            mic_icon.set_from_file(theme_mic)
        self._mic_btn.set_child(mic_icon)
        self._mic_btn.add_css_class("compact-mic-btn")
        self._mic_btn.set_tooltip_text(_("Listen"))
        self._mic_btn.connect("toggled", self._on_mic_toggled)
        bar.append(self._mic_btn)

        # Settings button
        settings_btn = Gtk.Button()
        _set_button_icon(settings_btn, "emblem-system-symbolic", "emblem-system",
                         "preferences-system-symbolic", "preferences-system",
                         "configure", text_fallback="⚙")
        settings_btn.add_css_class("flat")
        settings_btn.set_tooltip_text(_("Settings"))
        settings_btn.connect("clicked", self._on_settings_clicked)
        bar.append(settings_btn)

        # Expand chat button
        expand_btn = Gtk.Button()
        _set_button_icon(expand_btn, "view-fullscreen-symbolic", "view-fullscreen",
                         "view-restore-symbolic", "view-restore", text_fallback="⛶")
        expand_btn.add_css_class("flat")
        expand_btn.set_tooltip_text(_("Expand chat"))
        expand_btn.connect("clicked", self._on_expand_clicked)
        bar.append(expand_btn)

        root_box.append(bar)

        # Status area below the bar — hidden by default so the window stays tight
        self._status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._status_box.set_halign(Gtk.Align.FILL)
        self._status_box.set_margin_start(12)
        self._status_box.set_margin_end(12)
        self._status_box.set_margin_bottom(4)
        self._status_box.set_visible(False)

        # Spinner row (for loading / thinking states)
        spinner_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        spinner_row.set_halign(Gtk.Align.CENTER)
        self._status_spinner = Gtk.Spinner()
        self._status_spinner.add_css_class("compact-spinner")
        self._status_spinner.set_visible(False)
        spinner_row.append(self._status_spinner)
        self._status_spinner_label = Gtk.Label(label="")
        self._status_spinner_label.add_css_class("compact-status-label")
        self._status_spinner_label.add_css_class("dim-label")
        self._status_spinner_label.set_visible(False)
        spinner_row.append(self._status_spinner_label)
        self._spinner_row = spinner_row
        self._status_box.append(spinner_row)

        # Scrollable area for LLM response text
        self._status_scroll = Gtk.ScrolledWindow()
        self._status_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._status_scroll.set_max_content_height(140)
        self._status_scroll.set_propagate_natural_height(True)
        self._status_scroll.set_visible(False)
        self._status_scroll.set_halign(Gtk.Align.FILL)
        self._status_scroll.set_hexpand(True)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("compact-status-label")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_wrap(True)
        self._status_label.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        self._status_label.set_max_width_chars(44)
        self._status_label.set_selectable(True)
        self._status_label.set_xalign(0.5)
        self._status_label.set_halign(Gtk.Align.FILL)
        self._status_scroll.set_child(self._status_label)
        self._status_box.append(self._status_scroll)

        root_box.append(self._status_box)

        self.set_content(root_box)

        # Mirror STT state changes from the hidden widget
        self._ai_widget.stt_toggle.connect("toggled", self._on_widget_stt_changed)

        # Mirror sensitivity changes on the hidden widget's stt_toggle
        self._ai_widget.stt_toggle.connect("notify::sensitive", self._on_widget_stt_sensitivity_changed)

        # Intercept new assistant messages so we can update the status label
        self._original_add_bubble = self._ai_widget.add_message_bubble
        self._ai_widget.add_message_bubble = self._intercepted_add_bubble

        # Intercept Whisper model loading to show feedback in compact mode
        self._original_stt_start_whisper = self._ai_widget._stt_start_whisper
        self._ai_widget._stt_start_whisper = self._intercepted_stt_start_whisper

        # Intercept entry placeholder text to show STT phases
        # (Listening…, Transcribing…, etc.) in compact status bar
        self._original_set_placeholder = self._ai_widget.entry.set_placeholder_text
        self._ai_widget.entry.set_placeholder_text = self._intercepted_set_placeholder

        # Intercept thinking indicator to show "Thinking…" spinner in compact bar
        self._original_show_thinking = self._ai_widget._show_thinking_indicator
        self._original_remove_thinking = self._ai_widget._remove_thinking_indicator
        self._ai_widget._show_thinking_indicator = self._compact_show_thinking
        self._ai_widget._remove_thinking_indicator = self._compact_remove_thinking

        # Intercept play_tts so we can force mic sensitive when TTS starts
        self._original_play_tts = self._ai_widget.play_tts
        self._ai_widget.play_tts = self._intercepted_play_tts

        # Start Whisper model loading immediately in the background so the
        # user gets visual feedback and the model is ready when they press mic
        if voice_autostart and self._ai_widget.stt_backend == "whisper":
            self._preload_whisper_model()
        elif voice_autostart:
            GLib.idle_add(self._mic_btn.set_active, True)

    # -------------------------------------------------------------------
    # Whisper model preloading with visual feedback
    # -------------------------------------------------------------------
    def _preload_whisper_model(self):
        """Preload the Whisper model in background, showing status in the bar."""
        # Check if model is already loaded
        if hasattr(self._ai_widget, '_whisper_model_obj') and \
           getattr(self._ai_widget, '_whisper_model_name', None) == self._ai_widget.whisper_model:
            GLib.idle_add(self._mic_btn.set_active, True)
            return

        # Check if model file exists (needs download first)
        whisper_cache = os.path.expanduser("~/.cache/whisper")
        model_file = os.path.join(whisper_cache, f"{self._ai_widget.whisper_model}.pt")
        if not os.path.exists(model_file):
            # Model not downloaded — delegate to normal flow which shows download dialog
            GLib.idle_add(self._mic_btn.set_active, True)
            return

        # Model file exists but not loaded — show loading indicator
        self._mic_btn.set_sensitive(False)
        self._show_status(_("Loading voice model…"), spinner=True)

        def _bg_load():
            try:
                import whisper as whisper_module  # type: ignore
                model_obj = whisper_module.load_model(self._ai_widget.whisper_model)
                GLib.idle_add(self._on_preload_ready, model_obj)
            except Exception as e:
                GLib.idle_add(self._on_preload_failed, str(e))

        threading.Thread(target=_bg_load, daemon=True).start()

    def _on_preload_ready(self, model_obj):
        self._ai_widget._whisper_model_obj = model_obj
        self._ai_widget._whisper_model_name = self._ai_widget.whisper_model
        self._mic_btn.set_sensitive(True)
        self._hide_status()
        # Now auto-start mic
        GLib.idle_add(self._mic_btn.set_active, True)
        return False

    def _on_preload_failed(self, error_msg):
        self._mic_btn.set_sensitive(True)
        self._show_status(_("Model load failed: ") + error_msg)
        return False

    # -------------------------------------------------------------------
    # Intercept Whisper loading to show feedback in compact mode
    # -------------------------------------------------------------------
    def _intercepted_stt_start_whisper(self, btn):
        """Wrap _stt_start_whisper to show loading status in compact bar."""
        # If model needs loading (not cached), show compact spinner
        if not (hasattr(self._ai_widget, '_whisper_model_obj') and
                getattr(self._ai_widget, '_whisper_model_name', None) == self._ai_widget.whisper_model):
            whisper_cache = os.path.expanduser("~/.cache/whisper")
            model_file = os.path.join(whisper_cache, f"{self._ai_widget.whisper_model}.pt")
            if os.path.exists(model_file):
                # Model file exists but needs importing — show loading spinner
                self._show_status(_("Loading voice model…"), spinner=True)
                # Hook into the ready/failed callbacks for cleanup
                orig_ready = self._ai_widget._on_whisper_model_ready
                orig_failed = self._ai_widget._on_whisper_model_failed
                def _wrapped_ready(model_obj, btn):
                    self._hide_status()
                    return orig_ready(model_obj, btn)
                def _wrapped_failed(error_msg, btn):
                    self._show_status(_("Model load failed"))
                    return orig_failed(error_msg, btn)
                self._ai_widget._on_whisper_model_ready = _wrapped_ready
                self._ai_widget._on_whisper_model_failed = _wrapped_failed

        self._original_stt_start_whisper(btn)

    # -------------------------------------------------------------------
    # Intercept entry placeholder to mirror STT phase in compact bar
    # -------------------------------------------------------------------
    _PLACEHOLDER_STATUS_MAP = None

    @classmethod
    def _get_placeholder_map(cls):
        if cls._PLACEHOLDER_STATUS_MAP is None:
            cls._PLACEHOLDER_STATUS_MAP = {
                _("Listening..."): (_("Listening…"), False),
                _("Listening... (speak now)"): (_("Listening… (speak now)"), False),
                _("Transcribing..."): (_("Transcribing…"), True),
                _("Loading Whisper model..."): (_("Loading voice model…"), True),
            }
        return cls._PLACEHOLDER_STATUS_MAP

    def _intercepted_set_placeholder(self, text):
        """Mirror STT placeholder text changes in the compact status bar."""
        self._original_set_placeholder(text)
        mapping = self._get_placeholder_map()
        if text in mapping:
            label, spinner = mapping[text]
            self._show_status(label, spinner=spinner)
        elif text == _("Ask a question..."):
            # Only hide status if it was showing a transient STT phase
            current = self._status_spinner_label.get_label()
            transient = {v[0] for v in mapping.values()}
            if current in transient:
                self._hide_status()

    # -------------------------------------------------------------------
    # Thinking indicator intercepts
    # -------------------------------------------------------------------
    def _compact_show_thinking(self):
        """Show 'Thinking…' spinner in the compact bar and call original."""
        self._original_show_thinking()
        self._show_status(_('Thinking…'), spinner=True)

    def _compact_remove_thinking(self):
        """Remove thinking indicator from compact bar and call original."""
        self._original_remove_thinking()
        # Only hide the spinner row — an assistant response may follow
        self._status_spinner.stop()
        self._status_spinner.set_visible(False)
        self._status_spinner_label.set_visible(False)

    # -------------------------------------------------------------------
    # Intercept play_tts to force mic button sensitive during TTS
    # -------------------------------------------------------------------
    def _intercepted_play_tts(self, text, on_ready=None):
        """Wrap play_tts to ensure _mic_btn stays sensitive during TTS."""
        self._original_play_tts(text, on_ready=on_ready)
        # _speak_text schedules TTS via GLib.timeout_add(100, run_piper/run_espeak).
        # After it fires and sets tts_playing=True + stt_toggle.set_sensitive(False),
        # we need to re-enable the compact mic button.  Use a slightly longer
        # delay to run after the TTS scheduling callback.
        def _ensure_mic_sensitive():
            if getattr(self._ai_widget, 'tts_playing', False):
                self._mic_btn.set_sensitive(True)
            return False
        GLib.timeout_add(250, _ensure_mic_sensitive)

    # -------------------------------------------------------------------
    # Status helpers
    # -------------------------------------------------------------------
    def _show_status(self, text, spinner=False):
        """Show a transient status in the spinner row (Listening, Thinking, etc)."""
        self._status_box.set_visible(True)
        self._status_spinner_label.set_label(text)
        self._status_spinner_label.set_visible(True)
        self._status_scroll.set_visible(False)
        if spinner:
            self._status_spinner.set_visible(True)
            self._status_spinner.start()
        else:
            self._status_spinner.stop()
            self._status_spinner.set_visible(False)

    def _show_response(self, text):
        """Show an LLM response in the scrollable label."""
        self._status_box.set_visible(True)
        self._status_spinner.stop()
        self._status_spinner.set_visible(False)
        self._status_spinner_label.set_visible(False)
        self._status_label.set_label(text)
        self._status_scroll.set_visible(True)

    def _hide_status(self):
        self._status_box.set_visible(False)
        self._status_spinner.stop()
        self._status_spinner.set_visible(False)
        self._status_spinner_label.set_visible(False)
        self._status_scroll.set_visible(False)

    # -------------------------------------------------------------------
    # Intercept assistant bubbles to reflect in the status label
    # -------------------------------------------------------------------
    def _intercepted_add_bubble(self, role, content, is_html=False):
        """Wrap add_message_bubble to mirror status in compact bar."""
        self._original_add_bubble(role, content, is_html=is_html)
        if role == "assistant":
            text = self._ai_widget._extract_text_from_content(content)
            if text:
                self._show_response(text.strip()[:500])

    # -------------------------------------------------------------------
    # Button handlers
    # -------------------------------------------------------------------
    def _on_close_clicked(self, _btn):
        # Stop any active STT / TTS before closing
        if self._ai_widget.stt_toggle.get_active():
            self._ai_widget.stt_toggle.set_active(False)
        if getattr(self._ai_widget, 'tts_playing', False):
            self._ai_widget._stop_tts()
        self._ai_widget._save_conversation()
        self._ai_widget._cleanup_screenshot_tmp()
        self.close()

    def _on_mic_toggled(self, btn):
        """Forward mic toggle to the hidden AI widget's STT toggle."""
        active = btn.get_active()
        if active:
            # If TTS (Piper) is currently speaking, stop it first
            if getattr(self._ai_widget, 'tts_playing', False):
                self._ai_widget._stop_tts()
                # _stop_tts re-enables stt_toggle sensitivity
            # If the widget's stt_toggle is insensitive (e.g. during LLM processing),
            # queue activation for when it becomes sensitive again
            if not self._ai_widget.stt_toggle.get_sensitive():
                btn.set_active(False)
                self._pending_mic_activate = True
                return
        # Avoid feedback loop
        if self._ai_widget.stt_toggle.get_active() != active:
            self._ai_widget.stt_toggle.set_active(active)
        if active:
            self._show_status(_("Listening…"))
        else:
            current = self._status_spinner_label.get_label()
            if current == _("Listening…"):
                self._hide_status()

    def _on_widget_stt_changed(self, toggle):
        """Sync compact mic button when the widget's STT toggle changes."""
        active = toggle.get_active()
        if self._mic_btn.get_active() != active:
            self._mic_btn.set_active(active)

    def _on_widget_stt_sensitivity_changed(self, toggle, _pspec):
        """Sync compact mic button sensitivity with widget's stt_toggle.

        During TTS playback we deliberately keep _mic_btn sensitive so
        the user can tap it to stop TTS and re-start listening.
        """
        sensitive = toggle.get_sensitive()
        if not sensitive and getattr(self._ai_widget, 'tts_playing', False):
            # TTS is speaking — keep mic clickable so user can interrupt
            self._mic_btn.set_sensitive(True)
            return
        self._mic_btn.set_sensitive(sensitive)
        # If mic was pending activation and stt_toggle just became sensitive again
        if sensitive and getattr(self, '_pending_mic_activate', False):
            self._pending_mic_activate = False
            GLib.idle_add(self._mic_btn.set_active, True)

    def _on_settings_clicked(self, _btn):
        self._ai_widget.on_settings_clicked(_btn)

    def _on_expand_clicked(self, _btn):
        """Save conversation, launch full Alexy AI widget, and close compact window."""
        self._ai_widget._save_conversation()
        conv_id = self._ai_widget.current_conversation_id

        # Stop STT / TTS
        if self._ai_widget.stt_toggle.get_active():
            self._ai_widget.stt_toggle.set_active(False)
        if getattr(self._ai_widget, 'tts_playing', False):
            self._ai_widget._stop_tts()

        # Find linexin-center executable
        import shutil
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            shutil.which("linexin-center"),
            os.path.join(script_dir, "..", "..", "..", "bin", "linexin-center"),
            os.path.join(script_dir, "..", "..", "bin", "linexin-center"),
        ]
        cmd = None
        for c in candidates:
            if c and os.path.isfile(c):
                cmd = os.path.realpath(c)
                break

        if cmd:
            env = os.environ.copy()
            env["LINEXIN_NEW_INSTANCE"] = "1"
            subprocess.Popen(
                [cmd, "-w", "aa-alexy-ai-widget", "--conversation", conv_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )

        self.close()


if __name__ == "__main__":
    import sys as _sys
    _compact = "--compact" in _sys.argv
    _voice = "--voice" in _sys.argv

    class TestApp(Gtk.Application):
        def do_activate(self):
            if _compact:
                win = CompactVoiceWindow(
                    application=self,
                    voice_autostart=_voice,
                )
                win.present()
            else:
                win = Gtk.ApplicationWindow(application=self)
                win.set_title("AI Sysadmin Widget")
                win.set_default_size(800, 600)
                widget = LinexinAISysadminWidget(hide_sidebar=True, window=win)
                win.set_child(widget)
                win.present()

    app = TestApp()
    app.run()
