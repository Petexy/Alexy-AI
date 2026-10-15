# Training openWakeWord Models on Arch Linux

This is a local adaptation of the [openWakeWord Colab notebook](https://colab.research.google.com/drive/1q1oe2zOyZp7UsB3jJiQ1IFn8z5YfjwEb) for training custom wake word detection models on an Arch Linux PC.

## Prerequisites

- **Arch Linux** with `sudo` access and an AUR helper (`yay` or `paru`)
- **Python 3.11** will be installed from AUR automatically (`piper-phonemize` has no wheels for 3.12+)
- **8 GB+ RAM** recommended
- **20 GB+ free disk space** (training data is large)
- A GPU is optional — CPU training works fine (30–60 min for default settings)

## Quick Start

```bash
# 1. Make all scripts executable
chmod +x *.sh

# 2. Install dependencies & set up the environment
./01_setup.sh

# 3. Download training data (~15–30 min depending on connection)
./02_download_data.sh

# 4. (Optional) Test that your wake word sounds right
./04_test_tts.sh "hey linux"
# Then play the generated .wav file:  aplay test_wake_word.wav

# 5. Edit the config to set your wake word
nano my_model.yaml    # change target_phrase, model_name, etc.

# 6. Train the model
./03_train.sh
# Output: my_custom_model/<model_name>.onnx and .tflite
```

## File Overview

| File | Purpose |
|---|---|
| `01_setup.sh` | Installs system packages, creates venv, clones repos, downloads models |
| `02_download_data.sh` | Downloads RIRs, background audio (AudioSet, FMA), and pre-computed features |
| `03_train.sh` | Runs the 3-step training pipeline (generate → augment → train) |
| `04_test_tts.sh` | Generates a test audio clip of your wake word to verify pronunciation |
| `05_convert_tflite.sh` | Manually converts ONNX → TFLite if the auto-conversion failed |
| `my_model.yaml` | Training configuration — **edit this** to set your wake word and parameters |

## Configuration Guide (`my_model.yaml`)

Key parameters to adjust:

| Parameter | Default | Description |
|---|---|---|
| `target_phrase` | `"hey linux"` | Your wake word/phrase. Use `_` for phonetic hints (e.g., `"hey_seer_e"`) |
| `model_name` | `"hey_linux"` | Name for the output model files |
| `n_samples` | `1000` | Synthetic training examples. 1,000 is quick; 30,000–50,000 is best |
| `n_samples_val` | `1000` | Validation examples for early stopping |
| `steps` | `10000` | Training steps. More = better (at diminishing returns) |
| `max_negative_weight` | `1500` | Penalty for false activations. Higher = fewer false positives but may miss quiet/noisy speech. Same as `false_activation_penalty` in the simple notebook |
| `layer_size` | `32` | Model width. Larger = more capable but slower inference |

## Step-by-Step Details

### Step 1: Environment Setup (`01_setup.sh`)

Installs:
- System packages: `python`, `git`, `wget`, `ffmpeg`, `espeak-ng`, build tools
- Python venv with PyTorch (CPU), openWakeWord, Piper TTS, and all training dependencies
- Clones `piper-sample-generator` and `openWakeWord` repos
- Downloads the Piper English TTS model and openWakeWord embedding models

### Step 2: Download Data (`02_download_data.sh`)

Downloads ~20 GB of data:
- **MIT Room Impulse Responses** — adds realistic room echo to synthetic clips
- **AudioSet** (1 tar file) — background noise
- **FMA** (1 hour) — background music
- **ACAV100M features** (~2,000 hrs) — pre-computed negative features for training
- **Validation features** (~11 hrs) — for false-positive rate estimation

> **Note:** The data has mixed licenses. Models trained with this data are for **non-commercial personal use only**.

### Step 3: Train (`03_train.sh`)

Runs three sub-steps automatically:
1. **Generate clips** — Creates synthetic speech clips of your wake word using Piper TTS
2. **Augment clips** — Applies noise, reverb, and other augmentations
3. **Train model** — Trains the DNN and exports to `.onnx` and `.tflite`

### Using Your Model

After training, find your model in `my_custom_model/`:

```bash
ls my_custom_model/*.onnx my_custom_model/*.tflite
```

Test it with a microphone:

```bash
source venv/bin/activate
python openwakeword/examples/detect_from_microphone.py \
    --model_path my_custom_model/hey_linux.onnx
```

For **Home Assistant** users: copy the `.tflite` file to the openWakeWord add-on's custom models directory. See [HA docs](https://github.com/home-assistant/addons/blob/master/openwakeword/DOCS.md#custom-wake-word-models).

## Tips for Better Models

1. **More examples** — Set `n_samples: 50000` for significantly better accuracy
2. **More training steps** — Set `steps: 50000` for longer training
3. **GPU acceleration** — If you have an NVIDIA GPU, change the PyTorch install in `01_setup.sh` to use CUDA:
   ```bash
   pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
   ```
4. **Pronunciation** — Use `04_test_tts.sh` to test different spellings. Phonetic hints with underscores help: `"com_pyu_ter"` instead of `"computer"`
5. **False positives** — Increase `max_negative_weight` (e.g., 2000–3000) or add problematic phrases to `custom_negative_phrases`

## Troubleshooting

| Problem | Solution |
|---|---|
| `piper-phonemize` fails to install | Only has wheels for Python 3.9–3.11. The setup script installs `python311` from AUR automatically. If it fails, install manually: `yay -S python311` |
| TFLite conversion fails | Run `./05_convert_tflite.sh` manually, or just use the `.onnx` model |
| `tensorflow-cpu` won't install | Try `pip install tensorflow` instead, or skip TFLite (ONNX works fine) |
| Out of memory during training | Reduce `batch_n_per_class` values in the YAML config |
| Training takes too long | Reduce `n_samples` and `steps`, or use a GPU |
| Wake word sounds wrong | Try phonetic spelling with underscores in `target_phrase` |

## License

The training scripts in this repo are provided as-is. The training data downloaded has **mixed licenses** — models trained with it are for **non-commercial personal use only**. See the individual dataset licenses for details.
