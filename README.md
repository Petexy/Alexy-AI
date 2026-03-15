# Alexy AI Agent

Alexy AI Agent is the conversational assistant and voice interface implemented by the Alexy widget and the hey-linux wake-word daemon.

This README is intentionally scoped to these two components only:

- `src/usr/share/linexin/widgets/aa-alexy-ai-widget.py`
- `src/usr/bin/hey-linux`

It does not document the broader `linexin-center` application beyond the fact that Alexy is launched through it.

## Components

### `aa-alexy-ai-widget.py`

This is the main Alexy AI Agent UI and runtime. It provides:

- Chat-based assistant UI built with GTK4 and libadwaita.
- Multiple LLM backends:
  - Direct API mode.
  - Qwen CLI wrapper mode.
  - Local Ollama mode.
- Voice input with two speech-to-text engines:
  - Whisper.
  - Vosk.
- Optional voice correction for speech transcripts.
- Text-to-speech playback for spoken responses.
- Conversation persistence and history browsing.
- Image input support for multimodal prompts.
- Theme support for icons, avatars, and CSS styling.
- Autonomous command execution support, including guarded privileged execution through the widget's sudo manager.

The widget also exposes a compact voice window mode used by wake-word activation. In compact mode, Alexy opens as a small floating voice bar with controls for microphone, settings, close, and expanding back into the full chat UI.

### `hey-linux`

This is the wake-word listener daemon for Alexy AI Agent. It provides:

- Continuous microphone listening using `openWakeWord`.
- Automatic first-run bootstrap into a dedicated virtual environment.
- Loading of custom wake-word models from `/usr/share/linexin/wakewordmodels/`.
- Launching Alexy directly into compact voice mode after detection.
- Re-trigger behavior that reactivates the existing compact instance instead of opening duplicates.
- Desktop notifications for activation events.

By default, it listens for a configured wake word and then launches Alexy with voice input already active.

## How Alexy Works

Alexy AI Agent is split into two runtime paths:

1. `hey-linux` listens for a wake word in the background.
2. When triggered, it launches Alexy in compact voice mode using:

```bash
linexin-center -w aa-alexy-ai-widget --voice --compact
```

3. The compact window can:
   - start speech recognition,
   - show model-loading, listening, transcribing, and thinking states,
   - display the assistant reply,
   - open settings,
   - expand into the full Alexy chat window.

The full widget can also be used directly as the main interactive UI for text chat, voice chat, backend selection, model management, and conversation history.

## Features

### LLM backends

- Direct API mode for OpenAI-compatible endpoints.
- Qwen CLI integration with OAuth-based authentication.
- Local Ollama integration for offline or self-hosted usage.

### Voice stack

- Whisper for higher-quality offline speech recognition.
- Vosk for lightweight streaming recognition.
- Wake-word activation through `hey-linux` and `openWakeWord`.
- Text-to-speech output using Piper where available, with `espeak-ng` fallback for unsupported voices.

### UX features

- Full chat interface and compact voice interface.
- Persistent saved conversations stored under the user config directory.
- Themeable assets and CSS.
- Optional image attachments for multimodal prompts.
- Automatic command execution flow for sysadmin-style tasks.

## File Locations

- Main widget:
  - `src/usr/share/linexin/widgets/aa-alexy-ai-widget.py`
- Wake-word daemon:
  - `src/usr/bin/hey-linux`
- Shared Alexy config:
  - `~/.config/linexin-center/ai-sysadmin.json`
- Saved conversations:
  - `~/.config/linexin-center/conversations/`
- User themes:
  - `~/.config/linexin-center/themes/`
- Wake-word models:
  - `/usr/share/linexin/wakewordmodels/`
- hey-linux virtual environment:
  - `~/.local/share/linexin/hey-linux-venv`

## Runtime Requirements

Alexy AI Agent depends on a Linux desktop environment with the following available at runtime:

- Python 3
- GTK4 and libadwaita Python bindings
- `arecord`
- `notify-send`
- `bash`

Depending on which features are enabled, Alexy may also use:

- `ollama`
- `qwen` or `qwen-code`
- `paplay`
- `aplay`
- `espeak-ng`
- `curl`
- `unzip`

The wake-word daemon bootstraps `openWakeWord` for itself on first run by creating its own virtual environment.

## Running Alexy

### Full widget

Alexy can be launched through the Linexin widget host with the Alexy widget ID:

```bash
linexin-center -w aa-alexy-ai-widget
```

### Compact voice mode

To open Alexy directly in compact voice mode:

```bash
linexin-center -w aa-alexy-ai-widget --voice --compact
```

### Wake-word daemon

To run the wake-word listener:

```bash
hey-linux
```

On first launch, `hey-linux` creates its private virtual environment and installs `openwakeword` automatically.

## Configuration Notes

Alexy stores its settings in `~/.config/linexin-center/ai-sysadmin.json`. The widget manages settings for:

- Selected LLM backend.
- API endpoint and API key for direct mode.
- Selected Ollama model.
- Selected STT engine and model.
- Wake-word integration toggle.
- Voice correction toggles.
- Theme selection.
- Command auto-execution behavior.

The wake-word daemon reads the shared config file for its detection threshold and model behavior.

## Wake-Word Models

`hey-linux` loads custom wake-word models from:

```text
/usr/share/linexin/wakewordmodels/
```

Supported model file types:

- `.onnx`
- `.tflite`

If no custom models are present, `hey-linux` falls back to the built-in `openWakeWord` models.

## Scope

This repository may contain packaging, debug artifacts, training scripts, and the separate `linexin-center` host application layout. This document is only for the Alexy AI Agent itself:

- the Alexy widget in `aa-alexy-ai-widget.py`
- the wake-word daemon in `hey-linux`
# Linexin Center

<p align="center">
  <img src="https://i.ibb.co/cc59HQRQ/logo.png" alt="LinexinCenter" with="200" height="200"/>
</p>

**Linexin Center** is a modular, dynamic widget loader application built with Python, GTK4, and Libadwaita. It serves as a centralized hub (control center) that dynamically loads, displays, and manages system utility widgets from a specific directory.

Designed to be the core interface for the Linexin OS/Tooling ecosystem, it features a robust localization system, safety locking for subprocesses, and a responsive user interface that respects GNOME system settings.

## 🌟 Key Features

* **Dynamic Widget Loading:** Automatically discovers and loads Python-based widgets from `/usr/share/linexin/widgets`.
* **Modern UI:** Built with GTK4 and Libadwaita for a native GNOME look and feel, featuring a responsive sidebar and split-view layout.
* **Robust Localization (L10n):** Custom localization engine that supports per-widget translation dictionaries, recursive pattern matching (e.g., handling variables inside translated strings), and dynamic text updates.
* **Safety Locking:** Automatically locks the UI and window controls when a widget executes a subprocess (via monkey-patched `subprocess` calls) to prevent user interference during critical operations.
* **Single Widget Mode:** Can be launched via command line to display a specific widget in a standalone window without the sidebar.
* **System Integration:** Respects system button layouts (close/minimize/maximize placement) and follows system dark/light mode preferences.

## 🛠️ Dependencies

To run Linexin Center, you need the following system dependencies installed:

* Python 3.8+
* GTK 4
* Libadwaita (`libadwaita-1`)
* PyGObject (`python3-gi`)

## 📂 Directory Structure

The application relies on a specific file structure to function correctly:

```text
/usr/share/linexin/
├── widgets/                    # Place widget .py files here
│   ├── localization/           # Translation files
│   │   ├── en_US/
│   │   ├── pl_PL/
│   │   └── ...
│   ├── my_utility.py
│   └── system_monitor.py
└── linexin-center.py           # Main application entry point
