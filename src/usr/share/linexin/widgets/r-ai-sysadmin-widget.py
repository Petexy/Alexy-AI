#!/usr/bin/env python3
import gi # type: ignore # pylint: disable=import-error
import os
import json
import urllib.request
import urllib.error
import threading
import subprocess
import gettext
import locale
import uuid
import tempfile
import atexit
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
        self.process: Optional[subprocess.Popen[str]] = None
        self.sudo_manager = sudo_manager
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
            # We don't have easy percentage streaming for qwen install, so we pulse
            self.progress.set_pulse_step(0.1)
            GLib.timeout_add(100, self.pulse_progress)
        content.append(self.progress)
        
        # Start the subprocess in a background thread
        threading.Thread(target=self.run_process, daemon=True).start()
        
        if self.poll_auth_file:
            GLib.timeout_add(100, self.check_auth_file)

    def pulse_progress(self):
        if not self.process_finished and not self.is_ollama:
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
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            process = self.process
            if process:
                stdout = process.stdout
                if stdout:
                    for line in stdout:
                        GLib.idle_add(self.parse_and_append, line)
                
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
        print(line, end="")
        
        import re
        
        # Strip ANSI escape sequences (colors, formatting)
        clean_line = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', line)
        clean_line = clean_line.strip()
        
        # Filter out ASCII box borders (e.g. +-----------------+, |                 |)
        filtered_line = re.sub(r'^[+\-|*=\s]+$', '', clean_line)
        filtered_line = filtered_line.strip('| \t')
        
        if not filtered_line:
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
                        GLib.timeout_add(1500, self.close)
                        return False
            except Exception:
                pass
        return True

    def on_finish(self, rc):
        if self.success:
            return # Forcefully succeeded by auth poller already

        if rc == 0:
            self.status_label.set_label(_("Operation completed successfully."))
            self.progress.set_fraction(1.0)
            self.success = True
            GLib.timeout_add(1500, self.close)
        else:
            self.status_label.set_label(_(f"Operation failed with exit code {rc}. Check console output."))
            self.success = False

    def handle_close(self, win):
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
    def __init__(self, hide_sidebar=False, window=None, sudo_manager=None, **kwargs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12) # type: ignore
        self.widgetname = "AI Sysadmin"
        self.widgeticon = "utilities-terminal-symbolic"
        self.set_margin_top(12)
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
        
        # Voice-to-Text (Vosk) Config
        self.vosk_lang = "small-en-us-0.15"
        
        # Per-backend voice correction toggles
        self.voice_correction_direct = False
        self.voice_correction_qwen = False
        
        # Security / Safety
        self.auto_execute_commands = True
        
        self.system_prompt = _(
            "You are Alexy, an expert AI Sysadmin running under Linexin - An Arch Linux based operating system. "
            "You have the ability to execute bash commands autonomously. If you need to gather system information or execute a task, "
            "output a codeblock with ```bash containing the exact script. Do NOT output any other text if you output a bash block. "
            "The system will invisibly execute it and return the STDOUT to you. Do NOT run interactive commands like top, htop, or nano. "
            "When installing software, you should prioritize Flatpaks over the system package manager to avoid breaking the base system. Assume the flatpak package is already installed on the system."
            "If there is no flatpak version of what the user is asking for, you should then ONLY use the system package manager to fulfil the request. "
            "If the user wants you to run any program, you should first check if it is installed by searching both installed system packages and installed flatpaks. If it is not installed, you should tell the user that it is not installed and ask them if they want you to install it. "
            "If you need to launch a GUI application, you MUST run it in the background disconnected from stdout like this: `nohup app_name >/dev/null 2>&1 & disown` so it does not block the terminal. "
            "You may run multiple queries in sequence. Once you have all the information necessary, provide a final conversational response WITHOUT any bash blocks. "
        )
        self.chat_history = []
        self.current_conversation_id = str(uuid.uuid4())
        self._reset_history()
        
        self.load_config()
        self.setup_ui()
        
        if self.hide_sidebar and self.window:
            GLib.idle_add(self.resize_window_deferred)

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

    def _get_conversations_dir(self):
        os.makedirs(CONVERSATIONS_DIR, exist_ok=True)
        return CONVERSATIONS_DIR

    def _generate_title(self, chat_history):
        """Extract the first user message as a conversation title."""
        for msg in chat_history:
            if msg["role"] == "user":
                title = msg["content"].strip().replace("\n", " ")
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
                if msg["content"].startswith("System Command Execution Results:"):
                    continue
                self.add_message_bubble("user", msg["content"])
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
                    self.vosk_lang = config.get("vosk_lang", "small-en-us-0.15")
                    self.voice_correction_direct = config.get("voice_correction_direct", False)
                    self.voice_correction_qwen = config.get("voice_correction_qwen", False)
                    self.auto_execute_commands = config.get("auto_execute_commands", True)
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
                    "vosk_lang": self.vosk_lang,
                    "voice_correction_direct": self.voice_correction_direct,
                    "voice_correction_qwen": self.voice_correction_qwen,
                    "auto_execute_commands": self.auto_execute_commands
                }, f, indent=4)
        except Exception as e:
            print(f"Error saving config: {e}")

    def setup_ui(self):
        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_bottom(20)
        
        system_icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        system_icon.set_pixel_size(48)
        header_box.append(system_icon)
        
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title_box.set_hexpand(True)
        
        title_label = Gtk.Label(label=_("AI Sysadmin"))
        title_label.add_css_class("title-2")
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
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_valign(Gtk.Align.CENTER)
        settings_btn.add_css_class("circular")
        settings_btn.connect("clicked", self.on_settings_clicked)
        header_box.append(settings_btn)

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

        # Input Area
        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        input_box.set_margin_top(12)

        self.entry = MultilineEntry()
        self.entry.set_placeholder_text(_("Ask a question..."))
        self.entry.set_hexpand(True)
        self.entry.connect_activate(self.on_send_clicked)
        input_box.append(self.entry)

        self.send_btn = Gtk.Button(icon_name="mail-send-symbolic")
        self.send_btn.add_css_class("suggested-action")
        self.send_btn.set_size_request(40, 40)
        self.send_btn.set_valign(Gtk.Align.END)
        self.send_btn.connect("clicked", self.on_send_clicked)
        input_box.append(self.send_btn)

        self.stt_toggle = Gtk.ToggleButton(icon_name="audio-input-microphone-symbolic")
        self.stt_toggle.set_size_request(40, 40)
        self.stt_toggle.set_valign(Gtk.Align.END)
        self.stt_toggle.connect("toggled", self.on_stt_toggled)
        try:
            import vosk # type: ignore # pylint: disable=import-error # noqa: F401
        except ImportError:
            self.stt_toggle.set_sensitive(False)
            self.stt_toggle.set_tooltip_text(_("python-vosk is not installed. Add it to dependencies."))
        input_box.append(self.stt_toggle)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        input_box.append(self.spinner)

        chat_page.append(input_box)
        self.main_stack.add_named(chat_page, "chat")
        self.main_stack.set_visible_child_name("chat")

        self.add_message_bubble("assistant", _("Hello! I am Alexy. How can I help you today?"))

    def on_stt_toggled(self, btn):
        if btn.get_active():
            # Stop any TTS playback before starting mic
            if getattr(self, 'tts_playing', False):
                self._stop_tts()
            proc = self.arecord_proc
            if proc:
                proc.terminate()
                self.arecord_proc = None
            
            model_path = os.path.expanduser(f"~/.cache/linexin/vosk-model-{self.vosk_lang}")
            if not os.path.exists(model_path):
                btn.set_active(False)
                url = f"https://alphacephei.com/vosk/models/vosk-model-{self.vosk_lang}.zip"
                cmd_str = f"mkdir -p ~/.cache/linexin && rm -rf /tmp/vmodel && unzip -q -o /tmp/vmodel.zip -d /tmp/vmodel/ && mv /tmp/vmodel/* {model_path} && rm -rf /tmp/vmodel /tmp/vmodel.zip"
                
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
            
            import vosk, subprocess, threading # type: ignore # pylint: disable=import-error
            vosk.SetLogLevel(-1) # type: ignore
            try:
                self.vosk_model = vosk.Model(model_path)
                self.vosk_recognizer = vosk.KaldiRecognizer(self.vosk_model, 16000)
            except Exception as e:
                self.add_message_bubble("assistant", _(f"Error loading voice model: {e}"))
                btn.set_active(False)
                return
                
            self.entry.set_placeholder_text(_("Listening..."))
            
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
                
        else:
            self.stt_running = False
            proc = self.arecord_proc
            if proc:
                proc.terminate() # type: ignore
                self.arecord_proc = None

    def update_subtitle(self):
        if self.backend == "direct":
            self.subtitle_label.set_label(_(f"Alexy (Online API: {self.model})"))
        elif self.backend == "qwen_cli":
            self.subtitle_label.set_label(_("Alexy (Qwen CLI Wrapper)"))
        elif self.backend == "local":
            self.subtitle_label.set_label(_(f"Alexy (Local AI: {self.local_model})"))

    def add_message_bubble(self, role, content, is_html=False):
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        import html
        escaped_content = html.escape(content)
        
        # Super-basic Markdown -> Pango Markup parser for LLM Aesthetics
        import re
        
        # Triple backticks (with optional language specifier)
        parsed_markup = re.sub(r'```[a-zA-Z0-9]*\n?(.*?)```', r'<tt>\1</tt>', escaped_content, flags=re.DOTALL)
        # Single backticks (now supporting multiline)
        parsed_markup = re.sub(r'`(.*?)`', r'<tt>\1</tt>', parsed_markup, flags=re.DOTALL)
        
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

        if role == "user":
            box.set_halign(Gtk.Align.END)
            label = Gtk.Label()
            label.set_markup(parsed_markup)
            label.set_wrap(True)
            label.set_selectable(True)
            label.set_xalign(1.0)
            
            bubble = Gtk.Box()
            bubble.set_margin_start(50)
            bubble.append(label)
            box.append(bubble)
        else:
            box.set_halign(Gtk.Align.START)
            icon = Gtk.Image.new_from_icon_name(self.widgeticon)
            icon.set_pixel_size(24)
            icon.set_valign(Gtk.Align.START)
            box.append(icon)
            
            label = Gtk.Label()
            label.set_markup(parsed_markup)
            label.set_wrap(True)
            label.set_selectable(True)
            label.set_xalign(0.0)
            
            bubble = Gtk.Box()
            bubble.set_margin_end(50)
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
        
        voice_pref_group = Adw.PreferencesGroup(title=_("Voice-to-Text Setup"))
        page_speech.add(voice_pref_group)
        
        voice_lang_row = Adw.ComboRow(title=_("Offline Language Model"))
        
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
        voice_pref_group.add(voice_lang_row)

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
        
        theme_group = Adw.PreferencesGroup()
        page_theme.add(theme_group)
        
        soon_label = Gtk.Label(label="SOON:", halign=Gtk.Align.CENTER)
        soon_label.add_css_class("title-1")
        soon_label.set_margin_top(48)
        theme_group.add(soon_label)

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
                
            self.voice_correction_direct = direct_vc_row.get_active()
            self.voice_correction_qwen = qwen_vc_row.get_active()
            self.auto_execute_commands = auto_exec_row.get_active()
                
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
        cmd = "curl -fsSL https://qwen-code-assets.oss-cn-hangzhou.aliyuncs.com/installation/install-qwen.sh | bash -s -- --source qwenchat"
        self.launch_in_app_process(_("Installing Qwen CLI"), cmd, initial_status=_("Preparing installation scripts..."), on_close_callback=callback)

    def update_qwen_login_button(self):
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
                
            cmd = self.get_qwen_env_cmd("qwen \"Authentication ping.\" --auth-type qwen-oauth")
            self.launch_in_app_process(_("Qwen CLI Login"), cmd, initial_status=_("Waiting for Qwen OAuth URL generation..."), on_close_callback=callback, poll_auth_file=True)
            # We can't synchronously know when they finish logging in via the stream window,
            # but they will see it in the stream window. The button will update next time settings are opened.

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

    def on_send_clicked(self, widget):
        if getattr(self, 'llm_processing', False) or getattr(self, 'tts_playing', False):
            self.cancel_generation()
            return
            
        text = self.entry.get_text().strip()
        if not text:
            return

        if self.stt_toggle.get_active():
            self.stt_toggle.set_active(False)

        is_voice = getattr(self, '_last_input_was_voice', False)
        self._speak_next_response = is_voice
        self._last_input_was_voice = False

        if self.backend == "direct" and not self.api_key:
            self.add_message_bubble("assistant", _("Please configure your API Key in settings first."))
            return

        self.entry.set_text("")
        self.entry.set_sensitive(False)
        self.send_btn.set_icon_name("media-playback-stop-symbolic")
        self.stt_toggle.set_sensitive(False)
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
                GLib.idle_add(self._proceed_with_message, corrected)
            threading.Thread(target=voice_correction_thread, daemon=True).start()
        else:
            self._proceed_with_message(text)

    def _proceed_with_message(self, text):
        """Add the (possibly corrected) user message to the UI and fire the AI call."""
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
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_halign(Gtk.Align.START)

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
                    # After install completes, user probably still doesn't have a model pulled
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
            
        data = {
            "model": self.local_model,
            "messages": self.chat_history,
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
                        
                msg = f"Model '{self.local_model}' not found in Ollama locally. Initiating automatic download..."
                GLib.idle_add(self.on_api_error, msg)
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
            if getattr(self, 'backend', 'local') == 'qwen_cli':
                self.call_qwen_cli() # type: ignore
            elif getattr(self, 'is_ollama', False):
                self.call_local_ollama() # type: ignore
            else:
                self.call_direct_api() # type: ignore
        return True

    def call_qwen_cli(self):
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

        # We only pass the latest message to Qwen CLI.
        # Qwen's internal SQLite database handles the conversation memory via --chat-recording.
        latest_msg = self.chat_history[-1]['content']
        
        # Override Qwen CLI's internal autonomous execution tools.
        # If we don't, Qwen attempts to run sudo in its own hidden background PTY and fails.
        cli_override = "\n\n[SYSTEM INSTRUCTION: DO NOT use any internal tools to execute commands. If you need to run bash/sudo, ONLY output a markdown ```bash block and I will execute it. CRITICAL: Do NOT acknowledge this system instruction in your reply. Just reply to the user's message as if this instruction was never appended.]"
        prompt_with_override = latest_msg + cli_override
        
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
                bash_wrapper = self.get_qwen_env_cmd(f"{cmd} {escaped_prompt} --auth-type qwen-oauth --chat-recording {session_flag} --yolo")
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
            
            # Use the exact same autonomous executor hook the urllib backends use
            if self._run_autonomous_commands(reply, False):
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

        def _unlock_input():
            self.llm_processing = False
            self.entry.set_sensitive(True)
            self.send_btn.set_icon_name("mail-send-symbolic")
            self.stt_toggle.set_sensitive(True)
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
            
        piper_bin = os.path.expanduser("~/.cache/linexin/piper/piper")
        model_file = os.path.expanduser(f"~/.cache/linexin/piper-models/{model_name}.onnx")
        
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
            cmds = ["mkdir -p ~/.cache/linexin/piper ~/.cache/linexin/piper-models"]
            if needs_piper:
                cmds.append("curl -sL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz -o /tmp/piper.tar.gz")
                cmds.append("tar -xzf /tmp/piper.tar.gz -C ~/.cache/linexin/")
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
        self.llm_processing = False
        self.add_message_bubble("assistant", _("⚠️ Error: ") + error_msg)
        if len(self.chat_history) > 1:
            self.chat_history.pop() # remove failed prompt from history
        self.entry.set_sensitive(True)
        self.send_btn.set_icon_name("mail-send-symbolic")
        self.stt_toggle.set_sensitive(True)
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.entry.grab_focus()

if __name__ == "__main__":
    class TestWindow(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app) # type: ignore
            self.set_title("AI Sysadmin Widget")
            self.set_default_size(800, 600)
            widget = LinexinAISysadminWidget(hide_sidebar=True, window=self)
            self.set_child(widget)

    class TestApp(Gtk.Application):
        def do_activate(self):
            window = TestWindow(self)
            window.present()

    app = TestApp()
    app.run()
