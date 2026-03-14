#!/usr/bin/env bash
# =============================================================================
# 02_download_data.sh — Download training data (RIRs, background audio, features)
# =============================================================================
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

source venv/bin/activate

echo "=== [1/3] Downloading Room Impulse Responses ==="
python - <<'PYEOF'
import os
import numpy as np
import scipy.io.wavfile
import datasets
from tqdm import tqdm

output_dir = "./mit_rirs"
if not os.path.exists(output_dir):
    os.mkdir(output_dir)

if len(os.listdir(output_dir)) == 0:
    rir_dataset = datasets.load_dataset(
        "davidscripka/MIT_environmental_impulse_responses",
        split="train", streaming=True
    )
    for row in tqdm(rir_dataset, desc="RIRs"):
        name = row['audio']['path'].split('/')[-1]
        scipy.io.wavfile.write(
            os.path.join(output_dir, name), 16000,
            (row['audio']['array'] * 32767).astype(np.int16)
        )
    print(f"Done — saved RIRs to {output_dir}")
else:
    print(f"Skipping — {output_dir} already has files")
PYEOF

echo "=== [2/3] Downloading background audio (AudioSet + FMA) ==="
python - <<'PYEOF'
import os
import numpy as np
import scipy.io.wavfile
import datasets
from pathlib import Path
from tqdm import tqdm

# --- AudioSet sample ---
if not os.path.exists("audioset"):
    os.mkdir("audioset")

fname = "bal_train09.tar"
out_dir = f"audioset/{fname}"
link = "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/" + fname

if not os.path.exists(f"audioset/audio"):
    os.system(f"wget -O {out_dir} {link}")
    os.system("cd audioset && tar -xvf bal_train09.tar")

output_dir = "./audioset_16k"
if not os.path.exists(output_dir):
    os.mkdir(output_dir)

if len(os.listdir(output_dir)) == 0:
    audioset_dataset = datasets.Dataset.from_dict(
        {"audio": [str(i) for i in Path("audioset/audio").glob("**/*.flac")]}
    )
    audioset_dataset = audioset_dataset.cast_column(
        "audio", datasets.Audio(sampling_rate=16000)
    )
    for row in tqdm(audioset_dataset, desc="AudioSet"):
        name = row['audio']['path'].split('/')[-1].replace(".flac", ".wav")
        scipy.io.wavfile.write(
            os.path.join(output_dir, name), 16000,
            (row['audio']['array'] * 32767).astype(np.int16)
        )
    print(f"Done — saved AudioSet clips to {output_dir}")
else:
    print(f"Skipping — {output_dir} already has files")

# --- Free Music Archive sample ---
output_dir = "./fma"
if not os.path.exists(output_dir):
    os.mkdir(output_dir)

if len(os.listdir(output_dir)) == 0:
    fma_dataset = datasets.load_dataset(
        "rudraml/fma", name="small", split="train", streaming=True
    )
    fma_dataset = iter(fma_dataset.cast_column(
        "audio", datasets.Audio(sampling_rate=16000)
    ))
    n_hours = 1  # 1 hour of clips; increase for better models
    for i in tqdm(range(n_hours * 3600 // 30), desc="FMA"):
        row = next(fma_dataset)
        name = row['audio']['path'].split('/')[-1].replace(".mp3", ".wav")
        scipy.io.wavfile.write(
            os.path.join(output_dir, name), 16000,
            (row['audio']['array'] * 32767).astype(np.int16)
        )
    print(f"Done — saved FMA clips to {output_dir}")
else:
    print(f"Skipping — {output_dir} already has files")
PYEOF

echo "=== [3/3] Downloading pre-computed openWakeWord features ==="
FEATURES_TRAIN="openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
FEATURES_VAL="validation_set_features.npy"
BASE="https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main"

if [ ! -f "$FEATURES_TRAIN" ]; then
    echo "Downloading training features (~2000 hrs, this is a large file)..."
    wget "$BASE/$FEATURES_TRAIN"
fi

if [ ! -f "$FEATURES_VAL" ]; then
    echo "Downloading validation features (~11 hrs)..."
    wget "$BASE/$FEATURES_VAL"
fi

echo ""
echo "========================================"
echo "  Data download complete!"
echo "========================================"
