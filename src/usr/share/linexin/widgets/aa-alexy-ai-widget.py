#!/usr/bin/env python3
import gi # type: ignore # pylint: disable=import-error
import os
import json
import re as _re
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

CONFIG_DIR = os.path.expanduser("~/.config/linexin-center")
CONFIG_FILE = os.path.join(CONFIG_DIR, "ai-sysadmin.json")
CONVERSATIONS_DIR = os.path.join(CONFIG_DIR, "conversations")
BUNDLED_THEMES_DIR = "/usr/share/linexin/widgets/themes/"
USER_THEMES_DIR = os.path.join(CONFIG_DIR, "themes")

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
                env={'LC_ALL': 'C'}
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

class _OAuthPopupWindow(Adw.Window):
    """Minimal popup for OAuth flows — no URL bar, no controls."""
    def __init__(self, parent, url, on_closed_without_auth=None, auth_file=None):
        super().__init__(title=_("Qwen CLI Login"), transient_for=parent, modal=True) # type: ignore
        self.set_default_size(500, 650)
        self.auth_file = auth_file or os.path.expanduser("~/.qwen/oauth_creds.json")
        self.on_closed_without_auth = on_closed_without_auth
        self.authenticated = False
        self._browser_proc = None

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(box)
        header = Adw.HeaderBar()
        box.append(header)

        # Try WebKitGTK first (embedded, best UX)
        embedded = False
        try:
            gi.require_version("WebKit", "6.0") # type: ignore
            from gi.repository import WebKit # type: ignore # pylint: disable=import-error
            self.webview = WebKit.WebView()
            self.webview.set_vexpand(True)
            self.webview.set_hexpand(True)
            box.append(self.webview)
            self.webview.load_uri(url)
            embedded = True
        except Exception:
            pass

        if not embedded:
            import shutil
            browser_bin = None
            browser_args = []
            track_process = True  # whether we can rely on process exit to detect close

            # Try Chromium/Chrome --app mode (minimal window, no URL bar)
            for ch in ["chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave-browser", "microsoft-edge-stable"]:
                found = shutil.which(ch)
                if found:
                    browser_bin = found
                    browser_args = [f"--app={url}", "--no-first-run", "--disable-extensions"]
                    break

            # Fallback: Firefox in a new window.
            # Firefox delegates to the running instance and exits immediately,
            # so we cannot track the process — rely on auth-file polling only.
            if not browser_bin:
                for ff in ["firefox", "firefox-esr"]:
                    found = shutil.which(ff)
                    if found:
                        browser_bin = found
                        browser_args = ["--new-window", url]
                        track_process = False
                        break

            # Other browsers (trackable process)
            if not browser_bin:
                for br in ["epiphany", "falkon"]:
                    found = shutil.which(br)
                    if found:
                        browser_bin = found
                        browser_args = [url]
                        break

            if browser_bin:
                status = Gtk.Label(label=_("Complete the login in your browser, then close this window."))
                status.set_wrap(True)
                status.set_margin_top(24)
                status.set_margin_start(12)
                status.set_margin_end(12)
                box.append(status)
                self._browser_proc = subprocess.Popen(
                    [browser_bin] + browser_args,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if track_process:
                    GLib.timeout_add(1000, self._check_browser_closed)
            else:
                # Last resort: xdg-open (full browser, can't track window close)
                status = Gtk.Label(label=_("Complete the login in your browser, then close this window."))
                status.set_wrap(True)
                status.set_margin_top(24)
                status.set_margin_start(12)
                status.set_margin_end(12)
                box.append(status)
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.connect("close-request", self._on_close)
        GLib.timeout_add(500, self._poll_auth)

    def _check_browser_closed(self):
        """Monitor the --app browser process; if user closes it, close this window."""
        if self.authenticated:
            return False
        if self._browser_proc and self._browser_proc.poll() is not None:
            if not self.authenticated:
                self.close()
            return False
        return True

        self.connect("close-request", self._on_close)
        GLib.timeout_add(500, self._poll_auth)

    def _poll_auth(self):
        if self.authenticated:
            return False
        try:
            if os.path.exists(self.auth_file):
                with open(self.auth_file, 'r') as f:
                    data = f.read().strip()
                if len(data) > 10:
                    self.authenticated = True
                    self.close()
                    return False
        except Exception:
            pass
        return True

    def _on_close(self, win):
        if self._browser_proc and self._browser_proc.poll() is None:
            self._browser_proc.terminate()
        if not self.authenticated and self.on_closed_without_auth:
            self.on_closed_without_auth()
        return False

class _ActionProgressWindow(Adw.Window):
    def __init__(self, parent=None, title="", cmd_string="", is_ollama=False, initial_status=None, on_close_callback=None, poll_auth_file=False, sudo_manager=None, model_name=None, **kwargs):
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
        self.poll_auth_file = poll_auth_file
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.sudo_manager = sudo_manager
        self._has_real_progress = False
        self.set_deletable(False)
        self.connect("close-request", self.handle_close)
        self._oauth_popup = None
        self._last_error_line = None

        # Detect WebKitGTK once so we know whether to embed or delegate to CLI
        self._has_webkit = False
        if self.poll_auth_file:
            try:
                gi.require_version("WebKit", "6.0")  # type: ignore
                from gi.repository import WebKit  # type: ignore # noqa: F401 pylint: disable=import-error,unused-import
                self._has_webkit = True
            except Exception:
                pass
        
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
            # We don't have easy percentage streaming for qwen install, so we pulse
            self.progress.set_pulse_step(0.1)
            GLib.timeout_add(100, self.pulse_progress)
        content.append(self.progress)
        
        # Start the subprocess in a background thread
        threading.Thread(target=self.run_process, daemon=True).start()
        
        if self.poll_auth_file:
            GLib.timeout_add(100, self.check_auth_file)

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
                
            env = None
            self._noop_browser_dir = None
            if self.poll_auth_file and self._has_webkit:
                # Suppress ALL browser launches from the CLI subprocess so only
                # our embedded WebKit popup opens.  Node.js 'open' uses xdg-open
                # on Linux and ignores $BROWSER, so we must shadow the actual
                # browser binaries with a no-op script in PATH.
                env = os.environ.copy()
                env["BROWSER"] = "/bin/true"
                noop_dir = tempfile.mkdtemp(prefix="linexin-noop-")
                noop_script = os.path.join(noop_dir, "xdg-open")
                with open(noop_script, "w") as _f:
                    _f.write("#!/bin/sh\nexit 0\n")
                os.chmod(noop_script, 0o700)
                # Shadow common browser binaries too (some CLIs call them directly)
                for _name in ["firefox", "firefox-esr", "chromium", "google-chrome-stable", "sensible-browser"]:
                    os.symlink(noop_script, os.path.join(noop_dir, _name))
                env["PATH"] = noop_dir + ":" + env.get("PATH", "")
                self._noop_browser_dir = noop_dir
            
            self.process = subprocess.Popen(
                cmd_args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env
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
            self._cleanup_noop_dir()
                
            GLib.idle_add(self.on_finish, process.returncode if process else 1)
        except Exception as e:
            self.process_finished = True
            if self.sudo_manager:
                self.sudo_manager.stop_privileged_session()
            self._cleanup_noop_dir()
            print(f"Error launching process: {str(e)}")
            GLib.idle_add(self.status_label.set_label, _("Process failed to start."))

    def _cleanup_noop_dir(self):
        """Remove the temporary no-op browser directory."""
        d = getattr(self, '_noop_browser_dir', None)
        if d and os.path.isdir(d):
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
            self._noop_browser_dir = None

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
        
        # Intercept OAuth URLs
        if self.poll_auth_file and not self._oauth_popup:
            # Capture error messages from the CLI for better reporting
            lower = filtered_line.lower()
            if 'failed' in lower or 'error' in lower:
                self._last_error_line = filtered_line
            
            url_match = re.search(r'https?://\S+', filtered_line)
            if url_match:
                url = url_match.group(0)
                self.status_label.set_label(_("Waiting for authorization to complete..."))
                if self._has_webkit:
                    # WebKitGTK available → open embedded popup (CLI browser suppressed)
                    def on_popup_closed_without_auth():
                        self.success = False
                        self.process_finished = True
                        process = self.process
                        if process:
                            process.terminate()
                        self.status_label.set_label(_("Authorization was cancelled."))
                        self.set_deletable(True)
                        GLib.timeout_add(1500, self.close)
                    self._oauth_popup = _OAuthPopupWindow(
                        parent=self,
                        url=url,
                        on_closed_without_auth=on_popup_closed_without_auth
                )
                    self._oauth_popup.present()
                else:
                    # No WebKitGTK → CLI opened browser natively, just mark URL seen
                    self._oauth_popup = True  # prevents re-entry
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
        
    def check_auth_file(self):
        if self.process_finished:
            return False
            
        auth_file = os.path.expanduser("~/.qwen/oauth_creds.json")
        if os.path.exists(auth_file):
            try:
                # Check if it actually contains JSON data instead of an empty file
                with open(auth_file, 'r') as f:
                    data = f.read().strip()
                    if len(data) > 10:
                        self.success = True
                        self.process_finished = True
                        self.status_label.set_label(_("Authentication successful!"))
                        self.progress.set_fraction(1.0)
                        process = self.process
                        if process:
                            process.terminate()
                        self.set_deletable(True)
                        GLib.timeout_add(1500, self.close)
                        return False
            except Exception:
                pass
        return True

    def on_finish(self, rc):
        if self.success:
            return # Forcefully succeeded by auth poller already

        # If popup was cancelled, don't overwrite the "cancelled" message
        if self.poll_auth_file and isinstance(self._oauth_popup, _OAuthPopupWindow) and not self._oauth_popup.authenticated:
            self.set_deletable(True)
            return

        # For OAuth flows, the CLI may exit non-zero even though auth succeeded
        if self.poll_auth_file and rc != 0:
            auth_file = os.path.expanduser("~/.qwen/oauth_creds.json")
            try:
                if os.path.exists(auth_file):
                    with open(auth_file, 'r') as f:
                        data = f.read().strip()
                    if len(data) > 10:
                        self.status_label.set_label(_("Authentication successful!"))
                        self.progress.set_fraction(1.0)
                        self.success = True
                        self.set_deletable(True)
                        GLib.timeout_add(1500, self.close)
                        return
            except Exception:
                pass
            # Show a user-friendly error for OAuth failures
            if self._last_error_line:
                truncated = (self._last_error_line[:80] + '...') if len(self._last_error_line) > 80 else self._last_error_line
                self.status_label.set_label(truncated)
            else:
                self.status_label.set_label(_("Authentication failed. The Qwen OAuth service may be unavailable. Please try again later."))
            self.set_deletable(True)
            self.success = False
            return

        if rc == 0:
            self.status_label.set_label(_("Operation completed successfully."))
            self.progress.set_fraction(1.0)
            self.success = True
            self.set_deletable(True)
            GLib.timeout_add(1500, self.close)
        else:
            self.set_deletable(True)
            self.status_label.set_label(_(f"Operation failed with exit code {rc}. Check console output."))
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
    def __init__(self, hide_sidebar=False, window=None, sudo_manager=None, voice_autostart=False, conversation_id=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12) # type: ignore
        self.widgetname = "Alexy AI"
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
        self.backend = "qwen_cli" # "direct", "qwen_cli", "local"
        
        # Direct API Config
        self.api_key = ""
        self.api_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
        self.model = "qwen-plus"
        
        # Local AI Config
        self.local_model = "qwen3.5"
        self.local_url = "http://localhost:11434/api/chat"
        
        # Voice-to-Text Config
        self.stt_backend = "whisper"  # "whisper" or "vosk"
        self.whisper_model = "small"  # tiny, base, small, medium
        self.vosk_lang = "small-en-us-0.15"
        self.hey_linux_enabled = False
        
        # Per-backend voice correction toggles
        self.voice_correction_direct = False
        self.voice_correction_qwen = False
        
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
        self.chat_history = []
        self.current_conversation_id = str(uuid.uuid4())
        self._reset_history()
        
        self.load_config()
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
        self.qwen_session_id = str(uuid.uuid4())
        self.qwen_session_started = False

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
            "qwen_session_id": self.qwen_session_id,
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
        # Restore the backend the conversation was created with
        saved_backend = conv_data.get("backend", self.backend)
        if saved_backend != self.backend:
            self.backend = saved_backend
            self.update_subtitle()
        # Restore Qwen CLI session if applicable
        saved_qwen_id = conv_data.get("qwen_session_id")
        if saved_qwen_id and conv_data.get("backend") == "qwen_cli":
            self.qwen_session_id = saved_qwen_id
            # Session already exists on Qwen's side, so use --resume
            self.qwen_session_started = True
        else:
            self.qwen_session_id = str(uuid.uuid4())
            self.qwen_session_started = False
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
        """Return a list of (id, title, updated) sorted by most recently updated.
        If backend_filter is given, only return conversations for that backend."""
        conv_dir = self._get_conversations_dir()
        conversations = []
        for filename in os.listdir(conv_dir):
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(conv_dir, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                if backend_filter and data.get("backend") != backend_filter:
                    continue
                conversations.append((
                    data.get("id", filename.replace(".json", "")),
                    data.get("title", _("Untitled")),
                    data.get("updated", "")
                ))
            except Exception:
                continue
        conversations.sort(key=lambda x: x[2], reverse=True)
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
        """Toggle between chat view and inline conversations list."""
        if button.get_active():
            self._rebuild_conv_list()
            self.main_stack.set_visible_child_name("conversations")
            self.new_conv_btn.set_sensitive(False)
        else:
            self.main_stack.set_visible_child_name("chat")
            self.new_conv_btn.set_sensitive(True)

    def _rebuild_conv_list(self):
        """Populate the inline conversations list with backend filter."""
        # Discover which backends have saved conversations
        all_conversations = self._list_conversations()
        backend_labels = {
            "direct": _("Online API"),
            "qwen_cli": _("Qwen CLI"),
            "local": _("Local AI")
        }
        available_backends = set()
        for conv_id, title, updated in all_conversations:
            filepath = os.path.join(self._get_conversations_dir(), f"{conv_id}.json")
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                available_backends.add(data.get("backend", ""))
            except Exception:
                pass
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

        if len(available_backends) > 1:
            filter_label = Gtk.Label(label=_("Backend:"))
            filter_label.add_css_class("dim-label")
            self.conv_filter_box.append(filter_label) # type: ignore

            btn_group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            btn_group.add_css_class("linked")

            first_btn = None
            backend_order = ["direct", "qwen_cli", "local"]
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

        for conv_id, title, updated in conversations:
            row = Adw.ActionRow(title=title)
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(updated)
                row.set_subtitle(dt.strftime("%Y-%m-%d %H:%M"))
            except Exception:
                row.set_subtitle(updated)

            if conv_id == self.current_conversation_id:
                row.add_prefix(Gtk.Image.new_from_icon_name("emblem-ok-symbolic"))

            # Edit (rename) button
            edit_btn = Gtk.Button(icon_name="document-edit-symbolic")
            edit_btn.set_valign(Gtk.Align.CENTER)
            edit_btn.add_css_class("flat")
            edit_btn.set_focusable(False)

            def make_edit_handler(cid, t, r):
                def handler(btn):
                    idx = r.get_index()
                    edit_row = Adw.EntryRow(title=_("Rename conversation"))
                    edit_row.set_text(t)
                    edit_row.add_css_class("boxed-list")

                    cancel_btn = Gtk.Button(icon_name="window-close-symbolic")
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
            delete_btn = Gtk.Button(icon_name="user-trash-symbolic")
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

        # Remove previously applied theme CSS
        if self._theme_css_provider and display:
            Gtk.StyleContext.remove_provider_for_display(display, self._theme_css_provider)
            self._theme_css_provider = None

        if css_text and display:
            provider = Gtk.CssProvider()
            provider.load_from_data(css_text.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1  # type: ignore
            )
            self._theme_css_provider = provider

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
                    self.api_key = config.get("api_key", self.api_key)
                    self.api_url = config.get("api_url", self.api_url)
                    self.model = config.get("model", self.model)
                    self.local_model = config.get("local_model", self.local_model)
                    self.system_prompt = config.get("system_prompt", self.system_prompt)
                    self.stt_backend = config.get("stt_backend", "whisper")
                    self.whisper_model = config.get("whisper_model", "small")
                    self.vosk_lang = config.get("vosk_lang", "small-en-us-0.15")
                    self.hey_linux_enabled = config.get("hey_linux_enabled", False)
                    self.voice_correction_direct = config.get("voice_correction_direct", False)
                    self.voice_correction_qwen = config.get("voice_correction_qwen", False)
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
                    "system_prompt": self.system_prompt,
                    "stt_backend": self.stt_backend,
                    "whisper_model": self.whisper_model,
                    "vosk_lang": self.vosk_lang,
                    "hey_linux_enabled": self.hey_linux_enabled,
                    "voice_correction_direct": self.voice_correction_direct,
                    "voice_correction_qwen": self.voice_correction_qwen,
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
        
        header_svg = self._get_theme_svg("header-icon.svg")
        if header_svg:
            system_icon = Gtk.Image.new_from_file(header_svg)
        elif os.path.isfile(self.alexy_icon_path):
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

        # New conversation button
        self.new_conv_btn = Gtk.Button(icon_name="list-add-symbolic")
        self.new_conv_btn.set_valign(Gtk.Align.CENTER)
        self.new_conv_btn.add_css_class("circular")
        self.new_conv_btn.set_tooltip_text(_("Start a new conversation"))
        self.new_conv_btn.connect("clicked", self.on_new_conversation_clicked)
        header_box.append(self.new_conv_btn)

        # Conversations toggle button
        self.conv_toggle_btn = Gtk.ToggleButton(icon_name="view-list-symbolic")
        self.conv_toggle_btn.set_valign(Gtk.Align.CENTER)
        self.conv_toggle_btn.add_css_class("circular")
        self.conv_toggle_btn.set_tooltip_text(_("Browse saved conversations"))
        self.conv_toggle_btn.connect("toggled", self.on_conversations_toggled)
        header_box.append(self.conv_toggle_btn)

        # Settings button
        self.settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        self.settings_btn.set_valign(Gtk.Align.CENTER)
        self.settings_btn.add_css_class("circular")
        self.settings_btn.connect("clicked", self.on_settings_clicked)
        header_box.append(self.settings_btn)

        self.append(header_box)

        # Main content stack (chat vs conversations list)
        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.main_stack.set_transition_duration(200)
        self.main_stack.set_vexpand(True)
        self.append(self.main_stack)

        # === Conversations Page (added first so slide direction is natural) ===
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

        self.main_stack.add_named(self.conv_page, "conversations")

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

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_size_request(40, 40)
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", self.on_send_clicked)
        input_box.append(self.send_btn)

        self.stt_toggle = Gtk.ToggleButton()
        self.stt_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
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
        self.screen_toggle_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
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

        # Setup real-time dark mode class tracking for themes
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
                self.add_message_bubble("assistant", _(f"Unknown Whisper model: {self.whisper_model}"))
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
                initial_status=_("Downloading Whisper {} model ({})...").format(self.whisper_model, size_label),
                poll_auth_file=False
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
            self.add_message_bubble("assistant", _(f"Failed to start mic: {e}"))
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
                cmd_string=full_cmd_str,
                poll_auth_file=False
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
            self.add_message_bubble("assistant", _(f"Error loading voice model: {e}"))
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
            self.add_message_bubble("assistant", _(f"Failed to start mic: {e}"))
            btn.set_active(False)

    def update_subtitle(self):
        if self.backend == "direct":
            self.subtitle_label.set_label(_(f"Online API: {self.model}"))
        elif self.backend == "qwen_cli":
            self.subtitle_label.set_label(_("Qwen CLI Wrapper"))
        elif self.backend == "local":
            self.subtitle_label.set_label(_(f"Local AI: {self.local_model}"))

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

        import html
        escaped_content = html.escape(text_content)
        
        # Super-basic Markdown -> Pango Markup parser for LLM Aesthetics
        import re
        
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
            avatar_svg = self._get_theme_svg("assistant-avatar.svg")
            if avatar_svg:
                icon = Gtk.Image.new_from_file(avatar_svg)
            elif os.path.isfile(self.alexy_icon_path):
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

    def on_settings_clicked(self, button):
        window = Adw.PreferencesWindow(
            transient_for=self.window if self.window else self.get_root(),
            title=_("Settings"),
            search_enabled=False,
            default_width=600,
            default_height=750
        )

        # Page 1: LLM Connection
        page_llm = Adw.PreferencesPage(title=_("LLM Connection"), icon_name="network-server-symbolic")
        window.add(page_llm)
        
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
        model.append(_("Qwen CLI Wrapper"))
        model.append(_("Local AI (Ollama)"))
        backend_row.set_model(model)
        
        if self.backend == "direct":
            backend_row.set_selected(0)
        elif self.backend == "qwen_cli":
            backend_row.set_selected(1)
        elif self.backend == "local":
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

        # Dynamic Qwen CLI Group
        qwen_group = Adw.PreferencesGroup(description=_("Wraps the Qwen official CLI utility. No API key needed here; login through the CLI instead."))
        page_llm.add(qwen_group)
        
        install_row = Adw.ActionRow(title=_("Install / Update Qwen CLI"))
        install_btn = Gtk.Button(label=_("Install / Update"), valign=Gtk.Align.CENTER)
        install_btn.connect("clicked", self.on_qwen_install_clicked)
        install_row.add_suffix(install_btn)
        install_row.set_activatable_widget(install_btn)
        qwen_group.add(install_row)

        login_row = Adw.ActionRow(title=_("Qwen CLI Authentication"))
        self.login_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.update_qwen_login_button()
        self.login_btn.connect("clicked", self.on_qwen_auth_clicked)
        login_row.add_suffix(self.login_btn)
        login_row.set_activatable_widget(self.login_btn)
        qwen_group.add(login_row)

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
                    cmd = "pacman -Rns ollama --noconfirm"
                    win_uninstall = _ActionProgressWindow(
                        parent=window,
                        title=_("Uninstalling Ollama"),
                        cmd_string=cmd,
                        poll_auth_file=False,
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
                    parent=window,
                    title=_("Installing Ollama"),
                    cmd_string=cmd,
                    poll_auth_file=False,
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
            qwen_group.set_visible(idx == 1)
            local_group.set_visible(idx == 2)
            pull_group.set_visible(idx == 2)

        backend_row.connect("notify::selected", sync_backend_visibility)
        sync_backend_visibility() # apply initial state

        # Page 2: Speech & Audio
        page_speech = Adw.PreferencesPage(title=_("Speech & Audio"), icon_name="audio-speakers-symbolic")
        window.add(page_speech)
        
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
                        parent=window,
                        title=_("Installing python-vosk"),
                        cmd_string="pacman -Sy python-vosk --noconfirm",
                        poll_auth_file=False,
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
            description=_("Use an LLM to automatically fix transcribing errors. Note: This currently only functions when an online model (Direct API / Qwen CLI) is actively configured.")
        )
        page_speech.add(vc_group)

        direct_vc_row = Adw.SwitchRow(title=_("Enable for Direct API"))
        direct_vc_row.set_active(self.voice_correction_direct)
        vc_group.add(direct_vc_row)

        qwen_vc_row = Adw.SwitchRow(title=_("Enable for Qwen CLI wrapper"))
        qwen_vc_row.set_active(self.voice_correction_qwen)
        vc_group.add(qwen_vc_row)

        # Page 3: Theme
        page_theme = Adw.PreferencesPage(title=_("Theme"), icon_name="applications-graphics-symbolic")
        window.add(page_theme)

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

        def on_window_close_request(win):
            idx = backend_row.get_selected()
            old_backend = self.backend
            if idx == 0:
                self.backend = "direct"
            elif idx == 1:
                self.backend = "qwen_cli"
            elif idx == 2:
                self.backend = "local"
            
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
            self.voice_correction_qwen = qwen_vc_row.get_active()
            self.auto_execute_commands = auto_exec_row.get_active()
            self.compact_screen_awareness = compact_screen_row.get_active()

            # Apply selected theme
            selected_theme_idx = theme_row.get_selected()
            if selected_theme_idx < len(theme_ids):
                new_theme = theme_ids[selected_theme_idx]
                if new_theme != self.theme:
                    self.theme = new_theme
                    self._load_theme()
                    # Refresh header icon and stt icon
                    header_svg = self._get_theme_svg("header-icon.svg")
                    if header_svg:
                        self.header_icon_widget.set_from_file(header_svg)
                    elif os.path.isfile(self.alexy_icon_path):
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
            return False
            
        window.connect("close-request", on_window_close_request)
        window.present()

    def get_qwen_env_cmd(self, base_cmd):
        """Helper to try resolving qwen-code from user's global npm dir and nvm"""
        # A bash wrapper that sources nvm and tries to run the command
        wrapper = (
            "export NVM_DIR=\"$HOME/.nvm\"; "
            "[ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\"; "
            "export PATH=\"$HOME/.npm-global/bin:$PATH\"; "
            f"{base_cmd}"
        )
        return wrapper

    def launch_in_app_process(self, title, cmd_string, is_ollama=False, initial_status=None, on_close_callback=None, poll_auth_file=False, sudo_manager=None, model_name=None):
        """Robustly launch a subprocess and stream its output to a native GTK _ActionProgressWindow."""
        win = _ActionProgressWindow(
            parent=self.window if self.window else self.get_root(),
            title=title,
            cmd_string=cmd_string,
            is_ollama=is_ollama,
            initial_status=initial_status,
            on_close_callback=on_close_callback,
            poll_auth_file=poll_auth_file,
            sudo_manager=sudo_manager,
            model_name=model_name
        )
        win.present()

    def is_qwen_installed(self):
        cmd = self.get_qwen_env_cmd("command -v qwen >/dev/null 2>&1 || command -v qwen-code >/dev/null 2>&1")
        rc = subprocess.run(["bash", "-c", cmd]).returncode
        return rc == 0

    def on_qwen_install_clicked(self, btn=None, callback=None):
        # Run the installer, then verify the binary exists (the script may exit non-zero despite success)
        # Use get_qwen_env_cmd so nvm is sourced — the binary may only be findable through nvm's PATH.
        verify = self.get_qwen_env_cmd("command -v qwen >/dev/null 2>&1 || command -v qwen-code >/dev/null 2>&1")
        cmd = f'curl -fsSL https://qwen-code-assets.oss-cn-hangzhou.aliyuncs.com/installation/install-qwen.sh | bash -s -- --source qwenchat 2>&1; {verify}'
        self.launch_in_app_process(_("Installing Qwen CLI"), cmd, initial_status=_("Preparing installation scripts..."), on_close_callback=callback)

    def update_qwen_login_button(self):
        if not hasattr(self, 'login_btn') or not self.login_btn:
            return
        auth_file = os.path.expanduser("~/.qwen/oauth_creds.json")
        if os.path.exists(auth_file):
            self.login_btn.set_label(_("Logout of Qwen CLI"))
            self.login_btn.remove_css_class("suggested-action")
            self.login_btn.add_css_class("destructive-action")
        else:
            self.login_btn.set_label(_("Login to Qwen CLI (OAuth)"))
            self.login_btn.remove_css_class("destructive-action")
            self.login_btn.add_css_class("suggested-action")

    def on_qwen_auth_clicked(self, btn=None, callback=None):
        auth_file = os.path.expanduser("~/.qwen/oauth_creds.json")
        if os.path.exists(auth_file) and btn is not None:
            # perform logout
            try:
                os.remove(auth_file)
                self.add_message_bubble("assistant", _("Logged out of Qwen CLI successfully."))
            except Exception as e:
                self.add_message_bubble("assistant", _(f"Failed to logout: {e}"))
            self.update_qwen_login_button()
        else:
            # perform login
            if not self.is_qwen_installed():
                def after_install(success):
                    if success:
                        self.on_qwen_auth_clicked(None, callback)
                    elif callback:
                        callback(False)
                self.on_qwen_install_clicked(None, callback=after_install)
                return
                
            cmd = self.get_qwen_env_cmd("qwen --auth-type qwen-oauth -p ' '")
            def after_login(success):
                self.update_qwen_login_button()
                if callback:
                    callback(success)
            self.launch_in_app_process(_("Qwen CLI Login"), cmd, initial_status=_("Waiting for Qwen OAuth URL generation..."), on_close_callback=after_login, poll_auth_file=True)

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
                    err_dlg.present()
            else:
                if cancel_callback: cancel_callback()

        dialog.connect("response", response_handler)
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
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.entry.set_sensitive(True)
        self.send_btn.set_icon_name("mail-send-symbolic")
        self.stt_toggle.set_sensitive(True)
        self.new_conv_btn.set_sensitive(True)
        self.conv_toggle_btn.set_sensitive(True)
        self.settings_btn.set_sensitive(True)
        if hasattr(self, 'qwen_proc') and self.qwen_proc:
            try:
                self.qwen_proc.terminate()
            except: pass
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
        self.send_btn.set_icon_name("mail-send-symbolic")
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

            elif self.backend == "qwen_cli":
                import shlex
                escaped = shlex.quote(correction_prompt + "\n\nText: " + raw_text)
                cli_cmds = ["qwen", "qwen-code"]
                for cmd in cli_cmds:
                    try:
                        bash_wrapper = self.get_qwen_env_cmd(f"{cmd} {escaped} --auth-type qwen-oauth --yolo")
                        proc = subprocess.Popen(["bash", "-c", bash_wrapper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        stdout, stderr = proc.communicate(timeout=60)
                        if proc.returncode == 0 and stdout.strip():
                            return stdout.strip()
                    except Exception:
                        continue
                return raw_text

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
        os.makedirs(screenshot_dir, exist_ok=True)
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
        """Remove the /tmp/linexin and ~/.qwen/tmp screenshot directories and all their contents."""
        import shutil
        for screenshot_dir in ["/tmp/linexin", os.path.expanduser("~/.qwen/tmp")]:
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

            close_btn = Gtk.Button(icon_name="window-close-symbolic")
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
        self.send_btn.set_icon_name("media-playback-stop-symbolic")
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
            (self.backend == "direct" and self.voice_correction_direct) or
            (self.backend == "qwen_cli" and self.voice_correction_qwen)
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

    def call_ai(self):
        if self.backend == "direct":
            self.call_direct_api()
        elif self.backend == "qwen_cli":
            self.call_qwen_cli()
        elif self.backend == "local":
            self.call_local_ollama()

    def call_direct_api(self):
        # Ensure the URL ends with /chat/completions
        url = self.api_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = url + "/chat/completions"

        data = {
            "model": self.model,
            "messages": self.chat_history
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        })

        self._execute_urllib_request(req)

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
            "stream": False
        }
        req = urllib.request.Request(self.local_url, data=json.dumps(data).encode('utf-8'), headers={
            "Content-Type": "application/json"
        })
        
        self._execute_urllib_request(req, is_ollama=True)

    def _execute_urllib_request(self, req, is_ollama=False):
        try:
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
                if is_ollama:
                    reply = result.get('message', {}).get('content', '')
                else:
                    reply = result['choices'][0]['message']['content']
                
                self.chat_history.append({"role": "assistant", "content": reply})
                
                # Check for autonomous command execution securely via helper method
                if self._run_autonomous_commands(reply, is_ollama):
                    return

                else:
                    if getattr(self, 'abort_processing', False): return
                    GLib.idle_add(self.on_api_success, reply)
                    
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

                GLib.idle_add(self.add_message_bubble, "assistant", _("Model '{}' not found locally. Downloading now...").format(self.local_model))
                GLib.idle_add(lambda: self.on_pull_ollama_clicked(self.local_model, after_pull))
                return
                
            GLib.idle_add(self.on_api_error, msg)
        except urllib.error.URLError as e:
            msg = f"Connection Failed: {str(e)}"
            if is_ollama:
                msg += "\nIs the ollama.service running? Try: `systemctl enable --now ollama`"
            GLib.idle_add(self.on_api_error, msg)
        except Exception as e:
            msg = f"Error: {str(e)}"
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
            if self.backend == 'qwen_cli':
                self.call_qwen_cli(is_followup=True) # type: ignore
            elif self.backend == 'local':
                self.call_local_ollama() # type: ignore
            else:
                self.call_direct_api() # type: ignore
        return True

    def call_qwen_cli(self, is_followup=False):
        # Prevent exit code 127 if trying to interact with something that isn't installed.
        if not self.is_qwen_installed():
            def after_install(success):
                if success:
                    threading.Thread(target=self.call_qwen_cli).start()
                else:
                    GLib.idle_add(self.on_api_error, _("Qwen CLI installation was cancelled or failed."))
            GLib.idle_add(self.on_qwen_install_clicked, None, after_install)
            return
            
        # Explicitly check for OAuth credentials before passing to Qwen CLI. 
        # If we pass a prompt unauthenticated, Qwen opens a browser window and hangs our subprocess forever.
        auth_file = os.path.expanduser("~/.qwen/oauth_creds.json")
        if not os.path.exists(auth_file):
            def after_auth(success):
                if success:
                    threading.Thread(target=self.call_qwen_cli).start()
                else:
                    GLib.idle_add(self.on_api_error, _("Qwen CLI authentication was cancelled or failed."))
            GLib.idle_add(self.on_qwen_auth_clicked, None, after_auth)
            return

        # We pass the latest message plus an explicit role preamble.
        # Qwen's internal SQLite database handles conversation memory via --chat-recording,
        # but it does not automatically inherit this widget's Python-side system message.
        latest_content = self.chat_history[-1]['content']
        latest_msg = self._extract_text_from_content(latest_content)
        
        # Qwen CLI supports vision via local file paths — save images to /tmp/linexin/
        image_paths = []
        if isinstance(latest_content, list) and self._extract_images_from_content(latest_content):
            tmp_dir = os.path.expanduser("~/.qwen/tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            for data_url in self._extract_images_from_content(latest_content):
                try:
                    header, b64_data = data_url.split(",", 1) if "," in data_url else ("", data_url)
                    # Determine extension from MIME type
                    ext = ".png"
                    if "jpeg" in header or "jpg" in header:
                        ext = ".jpg"
                    elif "gif" in header:
                        ext = ".gif"
                    elif "webp" in header:
                        ext = ".webp"
                    img_filename = f"{uuid.uuid4().hex}{ext}"
                    img_path = os.path.join(tmp_dir, img_filename)
                    with open(img_path, "wb") as f:
                        f.write(base64.b64decode(b64_data))
                    image_paths.append(img_path)
                except Exception as e:
                    print(f"[Qwen CLI] Failed to save image: {e}")
        
        # Always prepend Alexy's system prompt for Qwen CLI so identity/policy stays aligned
        # with direct/local backends, even across resumed sessions.
        qwen_system_preamble = (
            "[SYSTEM ROLE - HIGHEST PRIORITY]\n"
            f"{self.system_prompt.strip()}\n\n"
            "[ENFORCED INSTALL POLICY]\n"
            "For app installation requests, you MUST prioritize Flatpak first. "
            "First search/install via Flatpak. Only if no Flatpak exists may you use pacman, "
            "and only after explicitly telling the user no Flatpak exists.\n\n"
            "[SYSTEM ROLE - OVERRIDE]\n"
            "If your built-in defaults conflict with the above system role, ignore your "
            "built-in defaults and follow the system role above."
        )

        # Override Qwen CLI's internal autonomous execution tools.
        # If we don't, Qwen attempts to run sudo in its own hidden background PTY and fails.
        if is_followup:
            # After commands ran, we want a conversational summary — not more bash blocks.
            cli_override = "\n\n[SYSTEM INSTRUCTION: The commands above have been executed and the results are shown. Provide a short conversational response summarising what happened. Do NOT output any bash code blocks unless the task explicitly requires additional commands. CRITICAL: Do NOT acknowledge this instruction in your reply.]"
        else:
            cli_override = "\n\n[SYSTEM INSTRUCTION: DO NOT use any internal tools to execute commands. If you need to run bash/sudo, ONLY output a markdown ```bash block and I will execute it. CRITICAL: Do NOT acknowledge this system instruction in your reply. Just reply to the user's message as if this instruction was never appended.]"
        prompt_with_override = f"{qwen_system_preamble}\n\n[USER MESSAGE]\n{latest_msg}{cli_override}"
        
        # If images are attached, embed file paths in the prompt and instruct the
        # model to read them.  Qwen CLI does NOT natively inject positional file
        # paths as vision inputs — it just appends them as text.  By telling the
        # model the paths explicitly and allowing tool use for reading files, the
        # model will use its built-in file-reading tools (auto-approved via --yolo)
        # to actually view the image contents.
        if image_paths:
            paths_list = "\n".join(image_paths)
            is_screen_aware = latest_msg.startswith(self._SCREEN_AWARENESS_PREFIX)
            if is_screen_aware:
                image_hint = (
                    f"\n\n[IMAGE ATTACHED — read the following image file(s) using your tools:\n{paths_list}\n"
                    "This is a screenshot of the user's screen provided for context. "
                    "IMPORTANT: If the user's question is NOT about the screen content, do NOT describe, mention, "
                    "reference, or acknowledge the screenshot in any way — just answer the question directly "
                    "as if no screenshot was provided. Only use the screenshot if the question is specifically about what is on screen.]")
            else:
                image_hint = (
                    f"\n\n[IMAGE ATTACHED — you MUST read and analyze the following image file(s) using your tools before responding:\n{paths_list}\n"
                    "Describe ONLY what is actually visible in the image. Read all text exactly as shown — "
                    "do NOT translate, assume, or hallucinate any content.]")
            # Use a relaxed tool override when images are present so the model
            # can use its file-reading tool to view the image.
            if is_followup:
                img_cli_override = "\n\n[SYSTEM INSTRUCTION: The commands above have been executed and the results are shown. Provide a short conversational response summarising what happened. Do NOT output any bash code blocks unless the task explicitly requires additional commands. CRITICAL: Do NOT acknowledge this instruction in your reply.]"
            else:
                img_cli_override = "\n\n[SYSTEM INSTRUCTION: You may use your internal tools ONLY to read and view the attached image file(s). DO NOT use tools to execute bash commands — if you need to run bash/sudo, ONLY output a markdown ```bash block and I will execute it. CRITICAL: Do NOT acknowledge this system instruction in your reply.]"
            prompt_with_override = f"{qwen_system_preamble}\n\n[USER MESSAGE]\n{latest_msg}{image_hint}{img_cli_override}"
        
        # Try both `qwen` and `qwen-code`
        cli_cmds = ["qwen", "qwen-code"]
        success = False
        reply = ""
        
        import shlex
        escaped_prompt = shlex.quote(prompt_with_override)
        
        for cmd in cli_cmds:
            try:
                # Need to use --resume if the session already exists, or --session-id if first prompt
                session_flag = f"--resume {self.qwen_session_id}" if self.qwen_session_started else f"--session-id {self.qwen_session_id}"
                
                # Wrap the command in bash to resolve .nvm / .npm-global
                qwen_cmd = f"{cmd} {escaped_prompt} --auth-type qwen-oauth --chat-recording {session_flag} --yolo"
                bash_wrapper = self.get_qwen_env_cmd(qwen_cmd)
                self.qwen_proc = subprocess.Popen(["bash", "-c", bash_wrapper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                stdout, stderr = self.qwen_proc.communicate(timeout=120)
                
                if getattr(self, 'abort_processing', False): return
                
                if self.qwen_proc.returncode == 0:
                    reply = stdout.strip()
                    if not reply:
                        reply = "Command succeeded, but output was empty."
                    success = True
                    self.qwen_session_started = True
                    break
                else:
                    # Keep trying the next binary name if the exit code was 127 (command not found)
                    if self.qwen_proc.returncode != 127:
                        reply = stderr.strip() or stdout.strip()
                        # If the error wasn't cmd-not-found, we should probably record it but maybe keep trying?
                        # Usually if qwen exists but fails, we shouldn't try qwen-code
            except Exception as e:
                reply = str(e)
                continue
                
        if success:
            self.chat_history.append({"role": "assistant", "content": reply})
            
            # Skip autonomous command execution when the response was for an
            # image analysis request.  Qwen CLI's internal tool-use output
            # (from reading image files via --yolo) may contain code-block
            # artifacts that _run_autonomous_commands would try to execute,
            # keeping the processing state alive and blocking app close.
            if not image_paths and self._run_autonomous_commands(reply, False):
                return
            
            GLib.idle_add(self.on_api_success, reply)
        else:
            if reply:
                msg = f"CLI Error: {reply}"
            else:
                msg = "Could not find 'qwen' or 'qwen-code' binaries in PATH or `~/.npm-global/bin`.\nPlease click 'Install / Update Qwen CLI' in Settings."
            GLib.idle_add(self.on_api_error, msg)


    def on_api_success(self, reply):
        if getattr(self, 'abort_processing', False): return
        self._remove_thinking_indicator()
        self._cleanup_screenshot_tmp()

        def _unlock_input():
            self.llm_processing = False
            self.entry.set_sensitive(True)
            self.send_btn.set_icon_name("mail-send-symbolic")
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
                self.send_btn.set_icon_name("media-playback-stop-symbolic")
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
            self.send_btn.set_icon_name("media-playback-stop-symbolic")
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
                cmd_string=full_cmd,
                poll_auth_file=False
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
        self.send_btn.set_icon_name("mail-send-symbolic")
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
        close_btn = Gtk.Button(icon_name="window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.set_tooltip_text(_("Close"))
        close_btn.connect("clicked", self._on_close_clicked)
        bar.append(close_btn)

        # Microphone toggle
        self._mic_btn = Gtk.ToggleButton()
        mic_icon = Gtk.Image.new_from_icon_name("audio-input-microphone-symbolic")
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
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.add_css_class("flat")
        settings_btn.set_tooltip_text(_("Settings"))
        settings_btn.connect("clicked", self._on_settings_clicked)
        bar.append(settings_btn)

        # Expand chat button
        expand_btn = Gtk.Button(icon_name="view-fullscreen-symbolic")
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
