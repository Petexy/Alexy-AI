#!/usr/bin/env bash
# =============================================================================
# 03_train.sh — Generate clips, augment, and train an openWakeWord model
#
# Usage:
#   ./03_train.sh                          # uses my_model.yaml
#   ./03_train.sh my_custom_config.yaml    # uses a custom config
# =============================================================================
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

source venv/bin/activate

CONFIG="${1:-my_model.yaml}"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config file '$CONFIG' not found."
    echo "Copy and edit my_model.yaml first (see README)."
    exit 1
fi

echo "Using config: $CONFIG"
echo ""

# -------------------------------------------------------
# Step 1: Generate synthetic clips with Piper TTS
# -------------------------------------------------------
echo "=== Step 1/3: Generating synthetic clips ==="
python openwakeword/openwakeword/train.py \
    --training_config "$CONFIG" \
    --generate_clips

# -------------------------------------------------------
# Step 2: Augment generated clips
# -------------------------------------------------------
echo "=== Step 2/3: Augmenting clips ==="
python openwakeword/openwakeword/train.py \
    --training_config "$CONFIG" \
    --augment_clips

# -------------------------------------------------------
# Step 3: Train the model
# -------------------------------------------------------
echo "=== Step 3/3: Training model ==="
python openwakeword/openwakeword/train.py \
    --training_config "$CONFIG" \
    --train_model

echo ""
echo "========================================"
echo "  Training complete!"
echo "  Your model files are in: my_custom_model/"
echo "  Look for .onnx and .tflite files."
echo "========================================"
