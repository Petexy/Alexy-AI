#!/usr/bin/env bash
# =============================================================================
# 05_convert_tflite.sh — Convert ONNX model to TFLite (optional)
#
# The training script usually does this automatically, but if it fails
# (as it sometimes does), run this script manually.
#
# Usage:
#   ./05_convert_tflite.sh                           # auto-detect model
#   ./05_convert_tflite.sh my_custom_model/hey_linux  # explicit base path
# =============================================================================
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

source venv/bin/activate

if [ -n "${1:-}" ]; then
    ONNX_PATH="${1}.onnx"
    TFLITE_PATH="${1}.tflite"
else
    ONNX_PATH=$(find my_custom_model -name "*.onnx" | head -1)
    if [ -z "$ONNX_PATH" ]; then
        echo "ERROR: No .onnx file found in my_custom_model/"
        exit 1
    fi
    TFLITE_PATH="${ONNX_PATH%.onnx}.tflite"
fi

echo "Converting: $ONNX_PATH -> $TFLITE_PATH"

python - "$ONNX_PATH" "$TFLITE_PATH" <<'PYEOF'
import sys
import os
import tempfile
import onnx
from onnx_tf.backend import prepare
import tensorflow as tf

onnx_model_path = sys.argv[1]
output_path = sys.argv[2]

onnx_model = onnx.load(onnx_model_path)
tf_rep = prepare(onnx_model, device="CPU")

with tempfile.TemporaryDirectory() as tmp_dir:
    tf_rep.export_graph(os.path.join(tmp_dir, "tf_model"))
    converter = tf.lite.TFLiteConverter.from_saved_model(
        os.path.join(tmp_dir, "tf_model")
    )
    tflite_model = converter.convert()
    with open(output_path, 'wb') as f:
        f.write(tflite_model)

print(f"Saved: {output_path}")
PYEOF
