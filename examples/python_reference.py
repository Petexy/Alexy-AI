#!/usr/bin/env python3
"""Run the ORIGINAL aa-alexy-ai-widget.py the way examples/harness.rs runs the
native port (same options, same steps, same output lines), for comparison.

The original Python Center injected `_`, `translate_dialog` and
`translate_window` into the widget module after loading it, using the
aa_alexy_ai dictionaries; the functions below are its
`_translate_with_patterns_helper`, `_translate_dialog` and helpers, verbatim
apart from the minimal localization manager they read. Its subprocess patch,
which locked the window while a command through the sudo wrapper ran, is
reduced to logging. `sudo_manager` is a stand-in that accepts only
$FAKE_PASSWORD and logs what it is asked. Everything else the widget runs is
a fake command of examples/compare.sh, and the models it talks to are
examples/fake_ai.py."""

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import warnings

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
# The widget prints from other threads: keep the order.
sys.stdout.reconfigure(line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

parser = argparse.ArgumentParser()
# The original widget: installed, or the copy PYTHON_WIDGET names.
parser.add_argument("--widget", default=os.environ.get(
    "PYTHON_WIDGET", "/usr/share/linexin/widgets/aa-alexy-ai-widget.py"))
parser.add_argument("--localization",
                    default=os.path.join(REPO, "data/usr/share/linexin/widgets/localization"))
# The Center's stylesheet: examples/harness --print-css writes it (compare.sh
# does, and names it in LINEXIN_CENTER_CSS).
parser.add_argument("--css", default=os.environ.get("LINEXIN_CENTER_CSS"))
parser.add_argument("--lang", default="en_US")
parser.add_argument("--dark", action="store_true")
parser.add_argument("--width", type=int, default=1300)
parser.add_argument("--height", type=int, default=820)
parser.add_argument("--compact-mode", action="store_true")
parser.add_argument("--voice", action="store_true")
parser.add_argument("--conversation")
parser.add_argument("--compact-window", action="store_true")
parser.add_argument("--library", help="ignored (harness.rs option)")
parser.add_argument("steps", nargs="*")
args = parser.parse_args()
if not args.css:
    parser.error("--css: the Center's stylesheet is needed (examples/harness --print-css >FILE)")

WIDGET_ID = "aa_alexy_ai"
FALLBACK_LANGUAGE = "en_US"


# --- The original Center's localization, reduced to one domain -------------

class LocalizationManager:
    def __init__(self):
        self.system_language = args.lang
        self.widget_translations = {}
        if os.path.isdir(args.localization):
            for language_code in sorted(os.listdir(args.localization)):
                path = os.path.join(args.localization, language_code, f"{WIDGET_ID}_dictionary.py")
                if not os.path.isfile(path):
                    continue
                spec = importlib.util.spec_from_file_location(f"{WIDGET_ID}_{language_code}_dict", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "translations"):
                    self.widget_translations.setdefault(WIDGET_ID, {})[language_code] = module.translations

    def get_text(self, key, widget_name="widget_loader"):
        if widget_name in self.widget_translations:
            widget_trans = self.widget_translations[widget_name]
            if self.system_language in widget_trans:
                if key in widget_trans[self.system_language]:
                    return widget_trans[self.system_language][key]
            if FALLBACK_LANGUAGE in widget_trans:
                if key in widget_trans[FALLBACK_LANGUAGE]:
                    return widget_trans[FALLBACK_LANGUAGE][key]
        return key


_manager = LocalizationManager()


def get_localization_manager():
    return _manager


def _(key, widget_name="widget_loader"):
    return get_localization_manager().get_text(key, widget_name)


def _translate_with_patterns_helper(text, widget_id):
    if not text:
        return ""
    translated = _(text, widget_id)
    if translated != text:
        return translated
    manager = get_localization_manager()
    if widget_id in manager.widget_translations:
        widget_trans = manager.widget_translations[widget_id]
        lang = manager.system_language
        trans_dict = widget_trans.get(lang) or widget_trans.get(FALLBACK_LANGUAGE, {})
        for pattern, translated_pattern in trans_dict.items():
            if '{}' not in pattern:
                continue
            escaped_pattern = re.escape(pattern)
            regex_pattern = escaped_pattern.replace(re.escape("{}"), r"(.*)")
            match = re.search(regex_pattern, text)
            if match and translated_pattern != pattern:
                if match.groups():
                    content = match.group(1)
                    translated_content = _translate_with_patterns_helper(content, widget_id)
                    translated_text = translated_pattern.replace("{}", translated_content, 1)
                    return text.replace(match.group(0), translated_text)
                else:
                    return text.replace(match.group(0), translated_pattern)
    return text


def _translate_dialog(dialog, widget_id=None):
    if widget_id is None:
        try:
            win = dialog.get_transient_for()
            if win and hasattr(win, 'active_widget_id'):
                widget_id = win.active_widget_id
        except Exception:
            pass
    if not widget_id:
        return
    if hasattr(dialog, 'get_heading') and dialog.get_heading():
        translated = _translate_with_patterns_helper(dialog.get_heading(), widget_id)
        if translated != dialog.get_heading():
            dialog.set_heading(translated)
    if hasattr(dialog, 'get_body') and dialog.get_body():
        translated = _translate_with_patterns_helper(dialog.get_body(), widget_id)
        if translated != dialog.get_body():
            dialog.set_body(translated)
    _translate_widget_recursive_static(dialog, widget_id)


def _translate_window(window, widget_id=None):
    if not widget_id:
        return
    _translate_widget_recursive_static(window, widget_id)


def _translate_widget_recursive_static(widget, widget_id):
    try:
        _translate_single_widget_static(widget, widget_id)
        if hasattr(widget, 'get_first_child'):
            child = widget.get_first_child()
            while child:
                _translate_widget_recursive_static(child, widget_id)
                child = child.get_next_sibling()
        elif hasattr(widget, 'get_child'):
            child = widget.get_child()
            if child:
                _translate_widget_recursive_static(child, widget_id)
    except Exception:
        pass


def _translate_single_widget_static(widget, widget_id):
    try:
        if isinstance(widget, Gtk.Label):
            if widget.get_use_markup():
                markup = widget.get_label()
                if markup:
                    translated_markup = _translate_markup_content_static(markup, widget_id)
                    if translated_markup != markup:
                        widget.set_markup(translated_markup)
            else:
                text = widget.get_text()
                if text:
                    translated = _translate_with_patterns_helper(text, widget_id)
                    if translated != text:
                        widget.set_text(translated)
        elif isinstance(widget, Gtk.Button):
            label = widget.get_label()
            if label:
                translated = _translate_with_patterns_helper(label, widget_id)
                if translated != label:
                    widget.set_label(translated)
        elif isinstance(widget, (Gtk.Entry, Gtk.SearchEntry, Gtk.PasswordEntry)):
            placeholder = widget.get_placeholder_text()
            if placeholder:
                translated = _translate_with_patterns_helper(placeholder, widget_id)
                if translated != placeholder:
                    widget.set_placeholder_text(translated)
        elif isinstance(widget, Adw.ActionRow):
            title = widget.get_title()
            if title:
                translated = _translate_with_patterns_helper(title, widget_id)
                if translated != title:
                    widget.set_title(translated)
            subtitle = widget.get_subtitle()
            if subtitle:
                translated = _translate_with_patterns_helper(subtitle, widget_id)
                if translated != subtitle:
                    widget.set_subtitle(translated)
        elif isinstance(widget, Adw.PreferencesGroup):
            title = widget.get_title()
            if title:
                translated = _translate_with_patterns_helper(title, widget_id)
                if translated != title:
                    widget.set_title(translated)
            desc = widget.get_description()
            if desc:
                translated = _translate_with_patterns_helper(desc, widget_id)
                if translated != desc:
                    widget.set_description(translated)
        elif isinstance(widget, (Adw.PreferencesRow, Adw.PreferencesPage)):
            title = widget.get_title()
            if title:
                translated = _translate_with_patterns_helper(title, widget_id)
                if translated != title:
                    widget.set_title(translated)
        elif isinstance(widget, Adw.StatusPage):
            title = widget.get_title()
            if title:
                translated = _translate_with_patterns_helper(title, widget_id)
                if translated != title:
                    widget.set_title(translated)
            desc = widget.get_description()
            if desc:
                translated = _translate_with_patterns_helper(desc, widget_id)
                if translated != desc:
                    widget.set_description(translated)
        elif isinstance(widget, (Gtk.Window, Adw.ApplicationWindow)):
            title = widget.get_title()
            if title:
                translated = _translate_with_patterns_helper(title, widget_id)
                if translated != title:
                    widget.set_title(translated)
    except Exception:
        pass


def _translate_markup_content_static(markup_text, widget_id):
    markup_text = _translate_with_patterns_helper(markup_text, widget_id)

    def replace_text(match):
        text_content = match.group(1)
        if text_content.strip():
            translated = _(text_content.strip(), widget_id)
            if translated != text_content.strip():
                leading_space = text_content[:len(text_content) - len(text_content.lstrip())]
                trailing_space = text_content[len(text_content.rstrip()):]
                return ">" + leading_space + translated + trailing_space + "<"
        return ">" + text_content + "<"
    return re.sub(r'>([^<]+)<', replace_text, markup_text)




# --- Stand-ins for the Center's services -----------------------------------

def log(variable, line):
    path = os.environ.get(variable)
    if path:
        with open(path, "a") as file:
            file.write(line + "\n")


class FakeSudo:
    user_password = None
    wrapper_path = os.environ.get("FAKE_WRAPPER", "/bin/false")
    askpass_script = "/bin/false"

    def validate_password(self, password):
        log("FAKE_LOG", "sudo validate_password")
        expected = os.environ.get("FAKE_PASSWORD", "")
        return bool(expected) and password == expected

    def set_password(self, password):
        log("FAKE_LOG", "sudo set_password")
        self.user_password = password

    def forget_password(self):
        log("FAKE_LOG", "sudo forget_password")
        self.user_password = None

    def start_privileged_session(self):
        log("FAKE_LOG", "sudo start_privileged_session")

    def stop_privileged_session(self):
        log("FAKE_LOG", "sudo stop_privileged_session")

    def get_env(self):
        return os.environ.copy()


sudo = FakeSudo()
_original_popen = subprocess.Popen


class LockedPopen(_original_popen):
    """The original Center's _LockedPopen, logging instead of locking."""

    def __init__(self, *popen_args, **kwargs):
        cmd = popen_args[0] if popen_args else kwargs.get("args")
        exe = ""
        if isinstance(cmd, (list, tuple)) and cmd:
            exe = str(cmd[0])
        elif isinstance(cmd, str):
            exe = cmd.split()[0] if cmd.strip() else ""
        wrapper = sudo.wrapper_path
        locks = bool(exe) and (exe == wrapper or exe.endswith(os.path.basename(wrapper)))
        if isinstance(cmd, str) and wrapper in cmd:
            locks = True
        if locks:
            log("FAKE_LOCK_LOG", "lock")
            super().__init__(*popen_args, **kwargs)

            def monitor():
                try:
                    self.wait()
                finally:
                    log("FAKE_LOCK_LOG", "unlock")
            threading.Thread(target=monitor, daemon=True).start()
        else:
            super().__init__(*popen_args, **kwargs)


subprocess.Popen = LockedPopen


# --- Load the widget module like the original _prepare_module ---------------

spec = importlib.util.spec_from_file_location("aa-alexy-ai-widget", args.widget)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def injected_underscore(message, widget_id=WIDGET_ID):
    return _translate_with_patterns_helper(message, widget_id)


module._ = injected_underscore
module.sudo_manager = sudo
module.translate_dialog = lambda dialog: _translate_dialog(dialog, WIDGET_ID)
module.translate_window = lambda window: _translate_window(window, WIDGET_ID)


# --- Steps (mirror examples/harness.rs) --------------------------------------

def q(text):
    """Rust's {:?} for a string."""
    out = ['"']
    for char in text or "":
        code = ord(char)
        category = unicodedata.category(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t":
            out.append("\\t")
        elif char == "\0":
            out.append("\\0")
        elif (category in ("Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs", "Mn", "Me")
              or (category == "Zs" and char != " ")):
            out.append(f"\\u{{{code:x}}}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def ql(items):
    return "[" + ", ".join(q(item) for item in items) + "]"


def b(value):
    return "true" if value else "false"


def bl(items):
    return "[" + ", ".join(b(item) for item in items) + "]"


def find_all(widget, kind, found=None):
    if found is None:
        found = []
    if isinstance(widget, kind):
        found.append(widget)
    child = widget.get_first_child()
    while child is not None:
        find_all(child, kind, found)
        child = child.get_next_sibling()
    return found


def children(widget):
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def render(widget, path):
    width, height = widget.get_width(), widget.get_height()
    snapshot = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(widget).snapshot(snapshot, width, height)
    node = snapshot.to_node()
    renderer = widget.get_native().get_renderer()
    viewport = Graphene.Rect().init(0, 0, width, height)
    renderer.render_texture(node, viewport).save_to_png(path)
    print(f"rendered {width}x{height} to {path}")


def iterate(seconds):
    context = GLib.MainContext.default()
    until_time = time.monotonic() + seconds
    while time.monotonic() < until_time:
        while context.pending():
            context.iteration(False)
        time.sleep(0.002)


SETTLE = 1.5
PATIENCE = 40


def until(what, ready):
    start = time.monotonic()
    while not ready():
        if time.monotonic() - start > PATIENCE:
            raise RuntimeError(f"timed out waiting for {what}")
        iterate(0.05)


def message_dialogs():
    return [w for w in Gtk.Window.list_toplevels()
            if isinstance(w, Adw.MessageDialog) and w.get_visible()]


def progress_windows():
    return [w for w in Gtk.Window.list_toplevels()
            if isinstance(w, Adw.Window) and not isinstance(w, Adw.MessageDialog)
            and w.get_visible() and w.get_transient_for() is not None]


def label_text(label):
    return label.get_label() or ""


def print_dialogs():
    for dialog in message_dialogs():
        print(f"dialog heading={q(dialog.get_heading())} body={q(dialog.get_body())}")
        responses = [f"{rid}={dialog.get_response_label(rid)}"
                     for rid in ("cancel", "authenticate", "deny", "allow", "ok", "create", "rename")
                     if dialog.has_response(rid)]
        print(f"  responses {ql(responses)}")
    for window in progress_windows():
        labels = [label_text(l) for l in find_all(window, Gtk.Label)
                  if l.get_visible() and label_text(l)]
        fractions = [f"{bar.get_fraction():.2f}" for bar in find_all(window, Gtk.ProgressBar)]
        print(f"progress title={q(window.get_title())} deletable={b(window.get_deletable())} "
              f"labels={ql(labels)} fraction={ql(fractions)}")


class Input:
    pass


def get_input(widget):
    scrolled = [s for s in find_all(widget, Gtk.ScrolledWindow) if s.has_css_class("multiline-entry")]
    if not scrolled:
        return None
    result = Input()
    result.view = find_all(scrolled[0], Gtk.TextView)[0]
    result.send = scrolled[0].get_next_sibling()
    result.mic = result.send.get_next_sibling()
    result.screen = result.mic.get_next_sibling()
    result.spinner = result.screen.get_next_sibling()
    return result


def get_header(widget):
    toggles = [t for t in find_all(widget, Gtk.ToggleButton) if t.has_css_class("circular")]
    if not toggles:
        return None
    toggle = toggles[0]
    return toggle.get_prev_sibling(), toggle, toggle.get_next_sibling()


def button_state(button):
    text = "on" if button.get_sensitive() else "off"
    if isinstance(button, Gtk.ToggleButton) and button.get_active():
        text += ",active"
    if button.has_css_class("suggested-action"):
        text += ",suggested"
    tooltip = button.get_tooltip_text()
    if tooltip is not None:
        text += f",tip={q(tooltip)}"
    return text


def image_state(image):
    kind = image.get_storage_type()
    if kind == Gtk.ImageType.ICON_NAME:
        return f"icon:{image.get_icon_name() or ''}"
    if kind == Gtk.ImageType.PAINTABLE:
        return "file"
    if kind == Gtk.ImageType.GICON:
        return "gicon"
    return "empty"


def classes(widget):
    return ",".join(widget.get_css_classes())


def has_ancestor(widget, kind):
    parent = widget.get_parent()
    while parent is not None:
        if isinstance(parent, kind):
            return True
        parent = parent.get_parent()
    return False


def describe_rows(listbox, lines):
    for row in children(listbox):
        if isinstance(row, Adw.ActionRow):
            icons = [image.get_icon_name() or "" for image in find_all(row, Gtk.Image)
                     if image.get_visible() and image.get_storage_type() == Gtk.ImageType.ICON_NAME]
            lines.append(f"  conv {q(row.get_title())} {q(row.get_subtitle())} icons={ql(icons)}")
        elif isinstance(row, Adw.EntryRow):
            lines.append(f"  rename {q(row.get_title())} text={q(row.get_text())}")
        else:
            labels = [label_text(l) for l in find_all(row, Gtk.Label)]
            lines.append(f"  header {ql(labels)}")


def describe_preferences(page, lines):
    for group in find_all(page, Adw.PreferencesGroup):
        if not group.get_visible():
            continue
        lines.append(f"  group {q(group.get_title())} {q(group.get_description())}")
        for row in find_all(group, Adw.PreferencesRow):
            if not row.get_visible():
                continue
            if isinstance(row, Adw.SwitchRow):
                line = f"switch {q(row.get_title())} active={b(row.get_active())}"
            elif isinstance(row, Adw.ComboRow):
                model = row.get_model()
                items = []
                if model is not None:
                    for i in range(model.get_n_items()):
                        item = model.get_item(i)
                        if isinstance(item, Gtk.StringObject):
                            items.append(item.get_string())
                line = f"combo {q(row.get_title())} selected={row.get_selected()} items={ql(items)}"
            elif isinstance(row, Adw.EntryRow):
                line = f"entry {q(row.get_title())} text={q(row.get_text())}"
            elif isinstance(row, Adw.ActionRow):
                buttons = [f"{bt.get_label()}({button_state(bt)})" for bt in find_all(row, Gtk.Button)
                           if bt.get_visible() and bt.get_label() is not None]
                line = f"row {q(row.get_title())} {q(row.get_subtitle())} {ql(buttons)}"
            else:
                labels = [label_text(l) for l in find_all(row, Gtk.Label)
                          if l.get_visible() and label_text(l)]
                views = []
                for view in find_all(row, Gtk.TextView):
                    buffer = view.get_buffer()
                    start, end = buffer.get_bounds()
                    views.append(q(buffer.get_text(start, end, False)[:60]))
                line = f"other {ql(labels)} views=[{', '.join(views)}]"
            lines.append(f"    {line}")
        for label in find_all(group, Gtk.Label):
            parent = label.get_parent()
            if (label.get_visible() and label_text(label)
                    and not has_ancestor(label, Adw.PreferencesRow)
                    and not has_ancestor(label, Gtk.ListBox)
                    and not label.has_css_class("title")
                    and not label.has_css_class("heading")
                    and parent is not None and not parent.has_css_class("header")):
                lines.append(f"    label {q(label.get_label())}")


def describe(widget):
    compact = describe_compact(widget)
    if compact is not None:
        return compact
    lines = []
    subtitle = ""
    for label in find_all(widget, Gtk.Label):
        if label.has_css_class("title-4") and label.has_css_class("dim-label"):
            subtitle = label.get_label()
            break
    line = f"state subtitle={q(subtitle)}"
    header = get_header(widget)
    if header:
        new, conv, settings = header
        line += f" new={button_state(new)} conv={button_state(conv)} settings={button_state(settings)}"
    inp = get_input(widget)
    if inp:
        buffer = inp.view.get_buffer()
        start, end = buffer.get_bounds()
        child = inp.mic.get_child()
        mic_image = image_state(child) if isinstance(child, Gtk.Image) else ""
        line += (f"\n  input text={q(buffer.get_text(start, end, True))} "
                 f"entry={'on' if inp.view.get_sensitive() else 'off'} "
                 f"send={inp.send.get_icon_name() or ''} mic={button_state(inp.mic)}[{mic_image}] "
                 f"screen={button_state(inp.screen)} spinner={b(inp.spinner.get_visible())}")
    lines.append(line)

    revealers = find_all(widget, Gtk.Revealer)
    shown = [q(b(r.get_reveal_child())) for r in revealers]
    scrims = [q(b(s.get_visible())) for s in find_all(widget, Gtk.Box) if s.has_css_class("settings-scrim")]
    lines.append(f"  revealers=[{', '.join(shown)}] scrims=[{', '.join(scrims)}]")

    for revealer in revealers:
        page = revealer.get_child()
        if page is None or not revealer.get_reveal_child() or not find_all(page, Gtk.ListBox):
            continue
        if page.has_css_class("settings-sidebar"):
            continue
        width = page.get_size_request()[0]
        lines.append(f"  conversations [{classes(page)}] width={width}")
        for toggle in find_all(page, Gtk.ToggleButton):
            if toggle.get_mapped():
                lines.append(f"  filter {q(toggle.get_label() or '')} {button_state(toggle)}")
        for label in find_all(page, Gtk.Label):
            if label.get_mapped() and not has_ancestor(label, Gtk.ListBox):
                lines.append(f"  label {q(label.get_label())}")
        listboxes = find_all(page, Gtk.ListBox)
        if listboxes:
            describe_rows(listboxes[0], lines)

    for revealer in revealers:
        sidebar = revealer.get_child()
        if sidebar is None or not revealer.get_reveal_child() or not sidebar.has_css_class("settings-sidebar"):
            continue
        stacks = find_all(sidebar, Adw.ViewStack)
        if not stacks:
            continue
        stack = stacks[0]
        name = stack.get_visible_child_name() or ""
        pages = stack.get_pages()
        titles = [pages.get_item(i).get_title() or "" for i in range(pages.get_n_items())]
        lines.append(f"  settings page={name} titles={ql(titles)}")
        page = stack.get_visible_child()
        if page is not None:
            describe_preferences(page, lines)

    chats = [l for l in find_all(widget, Gtk.ListBox) if has_ancestor(l, Gtk.Stack)]
    if chats:
        for row in children(chats[0]):
            box = row.get_first_child()
            if box is None:
                continue
            parts = [f"  [{classes(row)}] [{classes(box)}]"]
            for label in find_all(box, Gtk.Label):
                if label.get_visible() and not has_ancestor(label, Gtk.Expander):
                    parts.append(f"label={q(label_text(label))}")
            for expander in find_all(box, Gtk.Expander):
                header_widget = expander.get_label_widget()
                titles = [label_text(l) for l in find_all(header_widget, Gtk.Label)] if header_widget else []
                spinners = [s.get_visible() for s in find_all(expander, Gtk.Spinner)]
                child = expander.get_child()
                texts = [label_text(l) for l in find_all(child, Gtk.Label)] if child else []
                parts.append(f"reasoning expanded={b(expander.get_expanded())} title={ql(titles)} "
                             f"spinner={bl(spinners)} text={ql(texts)}")
            pictures = len(find_all(box, Gtk.Picture))
            if pictures:
                parts.append(f"pictures={pictures}")
            lines.append(" ".join(parts))
    return "\n".join(lines)


def chat_rows(widget):
    chats = [l for l in find_all(widget, Gtk.ListBox) if has_ancestor(l, Gtk.Stack)]
    return len(children(chats[0])) if chats else 0


def compact_bar(widget):
    bars = [b for b in find_all(widget, Gtk.Box) if b.has_css_class("compact-voice-bar")]
    return bars[0] if bars else None


def compact_buttons(widget):
    bar = compact_bar(widget)
    return children(bar) if bar is not None else []


def describe_compact(widget):
    buttons = compact_buttons(widget)
    if not buttons:
        return None
    lines = ["compact " + " | ".join(button_state(b) for b in buttons)]
    status = compact_bar(widget).get_next_sibling()
    if status is not None:
        labels = [label_text(l) for l in find_all(status, Gtk.Label) if l.is_visible()]
        spinners = [s.is_visible() for s in find_all(status, Gtk.Spinner)]
        lines.append(f"  status shown={b(status.get_visible())} spinner={bl(spinners)} labels={ql(labels)}")
    return "\n".join(lines)


def first_mapped_button(root, label):
    for button in find_all(root, Gtk.Button):
        if button.get_label() == label and button.get_mapped():
            return button
    return None


def conv_row(widget, title):
    for row in find_all(widget, Adw.ActionRow):
        if row.get_title() == title and row.get_mapped() and not has_ancestor(row, Adw.PreferencesPage):
            return row
    raise RuntimeError(f"no conversation {title!r}")


def preferences_row(widget, kind, title):
    for row in find_all(widget, kind):
        if row.get_title() == title and row.get_mapped():
            return row
    raise RuntimeError(f"no row {title!r}")


def print_files():
    config = os.path.expanduser("~/.config/linexin")
    try:
        with open(os.path.join(config, "ai-sysadmin.json")) as file:
            text = file.read()
    except OSError:
        text = "(none)"
    print(f"config {text}")
    try:
        names = sorted(os.listdir(os.path.join(config, "conversations")))
    except OSError:
        names = []
    try:
        with open(os.path.expanduser("~/.config/autostart/hey-linux.desktop")) as file:
            print(f"autostart {q(file.read())}")
    except OSError:
        pass
    for name in names:
        with open(os.path.join(config, "conversations", name)) as file:
            print(f"conversation {name}\n{file.read()}")


def step(window, widget, current):
    name, _sep, value = current.partition("=")
    inp = get_input(widget)
    header = get_header(widget)
    if name == "type":
        inp.view.get_buffer().set_text(value)
    elif name == "send":
        inp.send.emit("clicked")
    elif name == "wait-idle":
        iterate(0.3)

        def idle():
            current_input = get_input(widget)
            return (current_input is not None and not current_input.spinner.get_visible()
                    and current_input.view.get_sensitive())
        until("the reply", idle)
    elif name == "wait-dialog":
        until("a dialog", lambda: bool(message_dialogs()))
    elif name == "wait-window":
        until("a progress window", lambda: bool(progress_windows()))
    elif name == "wait-window-done":
        until("the progress window's end", lambda: all(w.get_deletable() for w in progress_windows()))
    elif name == "answer":
        until("a dialog", lambda: bool(message_dialogs()))
        iterate(0.5)
        print_dialogs()
        dialog = message_dialogs()[0]
        if "=" in value:
            response, text = value.split("=", 1)
            passwords = find_all(dialog, Gtk.PasswordEntry)
            if passwords:
                passwords[0].set_text(text)
                dialog.response(response)
                print(f"answered {response} with a password")
            else:
                find_all(dialog, Gtk.Entry)[0].set_text(text)
                dialog.response(response)
                print(f"answered {response} with {q(text)}")
        else:
            dialog.response(value)
            print(f"answered {value}")
        iterate(0.3)
    elif name == "new":
        header[0].emit("clicked")
    elif name == "conversations":
        header[1].set_active(not header[1].get_active())
    elif name == "conv-row":
        conv_row(widget, value).emit("activated")
    elif name in ("conv-edit", "conv-delete"):
        buttons = find_all(conv_row(widget, value), Gtk.Button)
        buttons[0 if name == "conv-edit" else 1].emit("clicked")
    elif name == "rename":
        rows = [r for r in find_all(widget, Adw.EntryRow)
                if r.get_mapped() and not has_ancestor(r, Adw.PreferencesPage)]
        if not rows:
            raise RuntimeError("no rename row")
        rows[0].set_text(value)
        rows[0].emit("entry-activated")
    elif name == "filter":
        toggles = [t for t in find_all(widget, Gtk.ToggleButton)
                   if t.get_label() == value and t.get_mapped()]
        if not toggles:
            raise RuntimeError(f"no filter {value!r}")
        toggles[0].set_active(True)
    elif name == "settings":
        header[2].emit("clicked")
    elif name == "settings-page":
        find_all(widget, Adw.ViewStack)[0].set_visible_child_name(value)
    elif name == "close-settings":
        bars = [h for h in find_all(widget, Gtk.Box)
                if h.has_css_class("settings-sidebar-header") and h.get_mapped()]
        if not bars:
            raise RuntimeError("no settings sidebar")
        find_all(bars[0], Gtk.Button)[0].emit("clicked")
    elif name == "switch":
        row = preferences_row(widget, Adw.SwitchRow, value)
        row.set_active(not row.get_active())
    elif name == "combo":
        title, index = value.rsplit("=", 1)
        preferences_row(widget, Adw.ComboRow, title).set_selected(int(index))
    elif name == "entryrow":
        title, text = value.split("=", 1)
        preferences_row(widget, Adw.EntryRow, title).set_text(text)
    elif name == "click":
        button = first_mapped_button(widget, value)
        if button is None:
            for progress in progress_windows():
                button = first_mapped_button(progress, value)
                if button is not None:
                    break
        if button is None:
            raise RuntimeError(f"no button {value!r}")
        button.emit("clicked")
    elif name == "screen":
        inp.screen.set_active(not inp.screen.get_active())
    elif name == "mic":
        inp.mic.set_active(not inp.mic.get_active())
    elif name == "expand":
        for expander in find_all(widget, Gtk.Expander):
            expander.set_expanded(True)
    elif name == "prompt":
        views = [v for v in find_all(widget, Gtk.TextView)
                 if v.get_editable() and v.get_mapped() and has_ancestor(v, Adw.PreferencesPage)]
        if not views:
            raise RuntimeError("no editable prompt")
        views[0].get_buffer().set_text(value)
    elif name == "wait-rows":
        wanted = int(value)
        until("chat rows", lambda: chat_rows(widget) >= wanted)
    elif name == "close-window":
        progress_windows()[0].close()
    elif name == "compact":
        buttons = [b for b in compact_buttons(widget) if b.get_tooltip_text() == value]
        if not buttons:
            raise RuntimeError(f"no compact button {value!r}")
        if isinstance(buttons[0], Gtk.ToggleButton):
            buttons[0].set_active(not buttons[0].get_active())
        else:
            buttons[0].emit("clicked")
    elif name == "settle":
        iterate(SETTLE)
    elif name == "sleep":
        iterate(float(value or 0.5))
    elif name == "print":
        print(describe(widget))
        print_dialogs()
    elif name == "shot":
        render(window, value)
    elif name == "dialog-shot":
        render(message_dialogs()[0], value)
    elif name == "window-shot":
        render(progress_windows()[0], value)
    elif name == "files":
        print_files()
    else:
        raise RuntimeError(f"unknown step {name!r}")


failed = False


def activate(app):
    global failed
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_DARK if args.dark else Adw.ColorScheme.FORCE_LIGHT)
    css = Gtk.CssProvider()
    with open(args.css) as style:
        css.load_from_string(style.read())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    print(f"widgetname='Alexy AI' icon=/usr/share/icons/github.petexy.alexy.svg id={WIDGET_ID}")
    if args.compact_window:
        window = module.CompactVoiceWindow(application=app, voice_autostart=args.voice)
        window.present()
        widget = window
    else:
        window = Adw.ApplicationWindow(application=app)
        window.set_default_size(args.width, args.height)
        if args.compact_mode:
            window._compact_mode = True
        widget = module.LinexinAISysadminWidget(
            hide_sidebar=False, window=window, voice_autostart=args.voice,
            conversation_id=args.conversation)
        window.set_content(widget)
        window.present()

    def run():
        global failed
        try:
            iterate(SETTLE)
            for current in args.steps:
                step(window, widget, current)
            iterate(0.3)
            for dialog in message_dialogs():
                dialog.response("cancel")
        except Exception as error:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"reference: {error}", file=sys.stderr)
            failed = True
        app.quit()
        return False

    GLib.idle_add(run)


app = Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)
app.connect("activate", activate)
app.run([])
sys.exit(1 if failed else 0)
