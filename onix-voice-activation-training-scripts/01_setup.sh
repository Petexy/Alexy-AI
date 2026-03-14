#!/usr/bin/env bash
# =============================================================================
# 01_setup.sh — Install all dependencies for openWakeWord training on Arch Linux
#
# NOTE: piper-phonemize requires Python 3.9–3.11. This script installs
#       python311 from the AUR (via yay) and uses it for the venv.
# =============================================================================
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

echo "=== [1/7] Installing Arch Linux system packages ==="
sudo pacman -S --needed --noconfirm \
    python python-pip \
    git wget ffmpeg espeak-ng \
    base-devel gcc cmake

echo "=== [2/7] Installing Python 3.11 from AUR (required by piper-phonemize) ==="
if ! command -v python3.11 &>/dev/null; then
    if command -v yay &>/dev/null; then
        yay -S --needed --noconfirm python311
    elif command -v paru &>/dev/null; then
        paru -S --needed --noconfirm python311
    else
        echo "ERROR: No AUR helper found (yay or paru)."
        echo "Install python311 manually: https://aur.archlinux.org/packages/python311"
        exit 1
    fi
fi

PYTHON311="$(command -v python3.11)"
echo "Using Python: $PYTHON311 ($($PYTHON311 --version))"

echo "=== [3/7] Creating Python 3.11 virtual environment ==="
if [ ! -d "venv" ]; then
    "$PYTHON311" -m venv venv
fi
source venv/bin/activate

echo "=== [4/7] Installing Python packages ==="
pip install --upgrade pip 'setuptools<81' wheel

# Core ML / audio (pinned versions for compatibility)
pip install 'torch==2.1.2+cpu' 'torchaudio==2.1.2+cpu' --index-url https://download.pytorch.org/whl/cpu
pip install 'numpy==1.26.4' 'scipy==1.11.4' pyyaml tqdm 'datasets==2.14.6' 'pyarrow==14.0.2'

# openWakeWord training dependencies
pip install mutagen==1.47.0
pip install torchinfo==1.8.0
pip install torchmetrics==1.2.0
pip install speechbrain==0.5.14
pip install audiomentations==0.33.0
pip install torch-audiomentations==0.11.0
pip install acoustics==0.2.6
pip install pronouncing==0.2.0
pip install deep-phonemizer==0.0.19
pip install piper-phonemize==1.1.0
pip install webrtcvad
pip install espeak-phonemizer

# Audio decoding (required by datasets library)
pip install soundfile librosa

# ONNX export
pip install onnx onnxruntime

# TFLite conversion (optional — needed only for .tflite output)
pip install tensorflow==2.15.1
pip install tensorflow_probability==0.23.0
pip install onnx_tf==1.10.0

echo "=== [5/7] Cloning piper-sample-generator (dscripka fork) ==="
if [ ! -d "piper-sample-generator" ]; then
    git clone https://github.com/dscripka/piper-sample-generator
fi

# Download the TTS model for English
PIPER_MODEL="piper-sample-generator/models/en-us-libritts-high.pt"
if [ ! -f "$PIPER_MODEL" ]; then
    echo "Downloading Piper TTS model..."
    mkdir -p piper-sample-generator/models
    wget -O "$PIPER_MODEL" \
        'https://github.com/rhasspy/piper-sample-generator/releases/download/v1.0.0/en-us-libritts-high.pt'
fi

echo "=== [6/7] Cloning openWakeWord ==="
if [ ! -d "openwakeword" ]; then
    git clone https://github.com/dscripka/openWakeWord openwakeword
fi
pip install -e ./openwakeword

echo "=== [7/7] Downloading openWakeWord resource models ==="
MODELS_DIR="./openwakeword/openwakeword/resources/models"
mkdir -p "$MODELS_DIR"

BASE_URL="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
for model_file in embedding_model.onnx embedding_model.tflite melspectrogram.onnx melspectrogram.tflite; do
    if [ ! -f "$MODELS_DIR/$model_file" ]; then
        wget "$BASE_URL/$model_file" -O "$MODELS_DIR/$model_file"
    fi
done

echo ""
echo "========================================"
echo "  Setup complete!"
echo "  Activate the venv before running other scripts:"
echo "    source venv/bin/activate"
echo "========================================"
