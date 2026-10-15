# Developing Alexy AI

How this repository is built, tested and put together. What it is and how
to install and use it: [README.md](README.md).

The Alexy AI widget of Linexin Center, rewritten in Rust as a native widget
(`aa-alexy-ai-widget.so`) for the [Rust Linexin Center](https://github.com/Petexy/Linexin-Center).
It replaces the Python `aa-alexy-ai-widget.py` and reads the same settings and
saved conversations (`~/.config/linexin/ai-sysadmin.json` and
`~/.config/linexin/conversations/`), migrating them from
`~/.config/linexin-center` as the original did.

It has three faces sharing the models, the conversations and the voice
(`widget/ai.rs`, `worker.rs`, `config.rs`, `conversations.rs`, `voice.rs`,
`tts.rs`, `screen.rs`):

- **GTK** (every desktop but LineXinBar, and LineXinBar without lxb-toolkit):
  identical to the Python widget — the chat with Markdown replies streaming in
  (reasoning in its own section), the three backends (an online
  OpenAI-compatible API, Ollama, a local OpenAI-compatible endpoint whose model
  is detected), commands the model writes run on their own (or after asking)
  with `sudo` through the Center's password prompt, images pasted or dropped,
  Screen Awareness, voice input (Whisper or Vosk) with spoken replies (Piper,
  espeak-ng), saved conversations grouped by agent with rename and delete, the
  settings sidebar (assistant, agents, speech, theme; Ollama models pulled,
  removed, Ollama installed or removed, python-vosk installed), the "Hey Alexy"
  wake-word switch, the themes (default, iMessage, Matrix, and the user's own)
  and the `--compact` voice window that the wake-word daemon opens.
- **LineXinBar**: the same in LineXinBar's own language, driven by
  controller, keyboard and pointer. The chat: the conversation (replies
  streaming in, their reasoning to open), a field to ask in (LineXinBar's
  on-screen keyboard rises over it with a controller in hand), images
  attached from the file chooser, Listen (Whisper or Vosk; the words are sent
  when the speaker falls silent and the reply is read aloud), Screen
  Awareness, the agent, a new conversation, the saved ones (filtered by
  backend, loaded, renamed, deleted) and Settings: the assistant (its
  switches, the backend, the online API's key, address and model, the
  endpoint, Ollama's models pulled and removed, Ollama installed or removed),
  the agents (created, renamed, their prompts edited, deleted) and speech (the
  engine, Whisper's model, Vosk's language and python-vosk, "Hey Alexy",
  voice correction). The GTK themes, which dress the GTK bubbles, are not
  offered there. Commands are asked about with the shell's dialog where
  auto-execution is off, and `sudo` commands run through `pkexec`, so the
  password is asked by the session's polkit agent.
  `linexin-center -w aa-alexy-ai-widget --voice` (and `--compact`, which has
  no window of its own there) starts listening at once, and SIGUSR1 from the
  wake-word daemon listens again. Typing needs an lxb-toolkit that lets a page
  take text (`lxb_page_taking_text`, lxb-toolkit 0.9.4); with an older one the
  page is a voice assistant, and the settings that are typed show what they
  are set to.

Whisper and Vosk are Python libraries: the widget starts a small Python helper
process (`python3 -c`, see `voice.rs`) that loads the model once and
transcribes, where the original loaded them into its own process. Recording
and silence detection are the original's, in Rust. `/usr/bin/hey-linux`, the
wake-word daemon, is the original Python script, except that it reads its
threshold from `~/.config/linexin/ai-sysadmin.json`, where Alexy keeps its
settings (the original still read `~/.config/linexin-center`, which Alexy moves
away, so the threshold set there was ignored); the old file is read until Alexy
has moved it.

## Build

The crate takes the Linexin Center's crates (`linexin-plugin-api`, `linexin-lxb`, `linexin-localization`)
from [its repository](https://github.com/Petexy/Linexin-Center), so a clone builds on its own. To build
against a local checkout of the Center instead, to change both at once, say so
in `.cargo/config.toml` (not committed):

```toml
[patch."https://github.com/Petexy/Linexin-Center"]
linexin-plugin-api = { path = "/path/to/Linexin-Center/crates/plugin-api" }
linexin-lxb = { path = "/path/to/Linexin-Center/crates/lxb" }
linexin-localization = { path = "/path/to/Linexin-Center/crates/localization" }
```

```sh
cargo build --release --lib
makepkg -si                 # the alexy-ai package
```

## Install layout

```text
/usr/share/linexin/widgets/aa-alexy-ai-widget.so
/usr/share/linexin/widgets/localization/<lang>/aa_alexy_ai_dictionary.py
/usr/share/linexin/widgets/sounds/alexy_activation.ogg
/usr/share/linexin/widgets/themes/{default,imessage,matrix}/
/usr/share/linexin/wakewordmodels/hey_alexy.onnx
/usr/share/icons/github.petexy.alexy.svg
/usr/bin/hey-linux
```

`data/` holds every file of the Python package except the widget itself. The
dictionaries gained a few words for the LineXinBar page ("You", "Stop", "On",
"Off", "Screen Awareness", "Attach an image", "Back", "Clear", "Delete
conversation", "Edit prompt", "Save").

## Tests

```sh
cargo test
cargo clippy --all-targets -- -D warnings
cargo build --lib --examples && examples/compare.sh
```

`examples/compare.sh` plays the same steps on this widget
(`examples/harness.rs`) and on the original Python widget
(`examples/python_reference.py`) in a private headless KWin, and compares every
pair of renders pixel by pixel, with the lines both print, the commands both
ran, the requests the models received and the files both left: English and
Polish, dark mode, the three themes, replies with Markdown, reasoning and
inline `<think>`, commands run (one or several, failing, refused, asked
about, with `sudo` and a right or wrong password), errors of every kind
(HTTP, Ollama's own, unexpected JSON, an endpoint that is down, a missing
model downloaded first, Ollama missing and installed), Stop, Screen
Awareness, the endpoint and online backends, saved conversations (listed,
filtered, loaded, renamed, deleted, in Linexin Center's compact layout),
every settings page and applying them, agents created, renamed and deleted,
the wake-word switch, Vosk, models pulled and removed, voice input (heard,
silence, a model that fails to load, a model or Piper still to download, a
`--voice` launch) and the compact voice window.

Nothing reaches the real machine or network: it all runs in private user,
mount, PID and network namespaces, so `localhost` is `examples/fake_ai.py`
(Ollama on 11434, an OpenAI-compatible server on 6767, scripted replies), `/tmp`
is private and `/usr/bin/hey-linux` is a fake; the sudo wrapper, pacman,
ollama, curl, tar, the microphone (`arecord`, a tone then silence), the
players, the screenshot tools and Piper are fakes first on `PATH`, and
`openai-whisper` is a fake module first on `PYTHONPATH`. HOME, TMPDIR and the
D-Bus session are scratch ones, rebuilt for every run.

With `LXB_LIBRARY_DIR` naming a folder holding a `liblxb_app.so` that has
`lxb_app_shot_after` and lets a page take text, the same script also plays the
LineXinBar page (`examples/lxb_run.rs`, what is typed scripted with
`LINEXIN_LXB_TYPED`) — heard words answered and spoken, a typed question, a
`--voice` launch with a screenshot, a `sudo` command through pkexec, the
endpoint's model detected, a missing model pulled, saved conversations
renamed and deleted, the backend switched, an agent created, "Hey Alexy"
turned on — and checks what it ran, asked and saved.

The comparison needs the original Python widget, installed where its package
puts it or named with `PYTHON_WIDGET=FILE`; it finds this checkout's build
wherever cargo put it (`CARGO_TARGET_DIR` counts), and the Center's stylesheet
comes with the Center's crates.

## Differences from the original

- A cancelled or failed Ollama install, or a model download that failed,
  reports an error and unlocks the chat; the original left it on
  "Thinking..." with every button disabled until the widget was reopened
  (`no-ollama-cancel` in compare.sh shows this one).
- The `--compact` window's words are translated; the original Center loaded
  that window's module so that it used gettext instead of the dictionaries.
- The compact window's Expand looks for an executable `linexin-center`, as
  `shutil.which` did, and the endpoint, model-list and voice-helper answers are
  read with Python's JSON error messages, so the printed lines stay the same.

The original's compact bar shows nothing for a reply that streamed in (it only
watched whole bubbles); this is kept as it was.
