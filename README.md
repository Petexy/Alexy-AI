# Alexy AI Agent

<p align="center">
  <img src="https://i.ibb.co/LhDFZvfh/Alexy.png" alt="Alexy AI" width="200" height="200"/>
</p>

**Alexy AI Agent** is the conversational assistant and voice interface of
Linexin: the Alexy widget for [Linexin Center](https://github.com/Petexy/Linexin-Center)
and the `hey-linux` wake-word daemon.

This version of the widget is a rewrite in **Rust** (GTK4 + Libadwaita) and a
drop-in replacement for the Python one: same package, same settings, same saved
conversations, same look.

The app requires `Linexin Center` to be installed.

## Components

### `Alexy AI`

The main Alexy AI Agent UI. It provides:

- A chat-based assistant with replies streaming in, formatted as Markdown, with
  the model's reasoning in its own section.
- Three LLM backends:
  - An online, OpenAI-compatible API.
  - Local Ollama.
  - A local OpenAI-compatible endpoint (its model is detected).
- Voice input with two speech-to-text engines: Whisper and Vosk.
- Optional voice correction for speech transcripts.
- Spoken replies (Piper, with `espeak-ng` for languages Piper has no voice
  for).
- Saved conversations, grouped by agent, to load, rename and delete.
- Agents of your own, each with its own prompt.
- Image input for multimodal prompts (pasted, dropped or attached) and Screen
  Awareness (a screenshot sent with your question).
- Autonomous command execution for sysadmin-style tasks, on its own or after
  asking you; privileged commands ask for your password first.
- Themes for the chat: default, iMessage, Matrix, and your own.

The widget also has a compact voice window used by wake-word activation: a
small floating voice bar with the microphone, settings, close, and a button
that expands it into the full chat.

### `hey-linux` daemon

The wake-word listener for Alexy AI Agent. It provides:

- Continuous microphone listening using `openWakeWord`.
- Automatic first-run setup of its own virtual environment.
- Custom wake-word models from `/usr/share/linexin/wakewordmodels/` ("Hey
  Alexy" is included).
- Launching Alexy directly into its compact voice window after detection, or
  reactivating the one already open instead of opening a duplicate.
- Desktop notifications for activation events.

## How Alexy Works

1. `hey-linux` listens for the wake word in the background (turn it on with
   the "Hey Alexy" switch in Alexy's settings).
2. When triggered, it launches Alexy in compact voice mode:

```bash
linexin-center -w aa-alexy-ai-widget --voice --compact
```

3. The compact window listens, shows when it is loading, listening,
   transcribing and thinking, displays the reply (and reads it aloud), and can
   open the settings or expand into the full chat.

The full widget is also the main interactive UI for text chat, voice chat,
backend selection, model management and conversation history.

On a LineXinBar desktop Alexy is a controller-friendly page with the same
chat, voice, conversations and settings; there the voice command above opens
it listening at once.

## File Locations

- Alexy's settings: `~/.config/linexin/ai-sysadmin.json`
- Saved conversations: `~/.config/linexin/conversations/`
- Your own themes: `~/.config/linexin/themes/`
- Wake-word models: `/usr/share/linexin/wakewordmodels/`
- hey-linux virtual environment: `~/.local/share/linexin/hey-linux-venv`

Settings and conversations from older versions (`~/.config/linexin-center/`)
are moved there automatically.

## Runtime Requirements

The package pulls in:

- Linexin Center 4.0 or newer
- GTK 4 and Libadwaita
- Python 3 with `python-openai-whisper` and `python-pyaudio`
- `alsa-utils` (`arecord`)

Depending on which features you use, Alexy may also use:

- `ollama` (Alexy can install and remove it for you)
- `python-vosk` (Alexy can install it for you)
- Piper or `espeak-ng`, and `paplay`, for spoken replies
- A screenshot tool for Screen Awareness (`grim`, `spectacle`,
  `gnome-screenshot` or `scrot`)

## Running Alexy

### Full widget

```bash
linexin-center -w aa-alexy-ai-widget
```

### Compact voice mode

```bash
linexin-center -w aa-alexy-ai-widget --voice --compact
```

### Wake-word daemon

```bash
hey-linux
```

On first launch, `hey-linux` creates its private virtual environment and
installs `openwakeword` automatically.

## Configuration Notes

Alexy's settings page manages:

- The LLM backend, the online API's address, key and model, and the local
  endpoint.
- Ollama's models (pulled and removed from Alexy).
- Your agents and their prompts.
- The speech-to-text engine, Whisper's model and Vosk's language.
- The "Hey Alexy" wake word and voice correction.
- The theme.
- Whether commands run on their own or after asking.

The wake-word daemon reads its detection threshold (`wakeword_threshold`) from
the same file.


## Screenshots
<p align="center">
  <img src="https://i.ibb.co/27SGp8Fg/alexy-1.png" alt="Alexy AI" width="800"/> <br/><br/>
  <img src="https://i.ibb.co/S4SYjrX5/alexy-3.png" alt="Alexy AI" width="800"/> <br/><br/>
  <img src="https://i.ibb.co/ZR18LFKz/alexy-2.png" alt="Alexy AI" width="800"/> <br/><br/>
</p>

## 🔧 Building from Source

```bash
cargo build --release --lib
makepkg -si
```

Cargo fetches Linexin Center's crates from its repository while building.
Tests, how to build against a local Linexin Center checkout and how the widget
works inside are described in [DEVELOPMENT.md](DEVELOPMENT.md).

## Licence

Copyright (C) 2026 Petexy

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License in `LICENSE` for more
details.
