#!/usr/bin/env bash
# =============================================================================
# 04_test_tts.sh — Test that Piper TTS pronounces your wake word correctly
#
# Usage:
#   ./04_test_tts.sh "hey linux"
#   ./04_test_tts.sh "hey_seer_e"      # phonetic spelling
# =============================================================================
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKDIR"

source venv/bin/activate

TARGET="${1:-hey linux}"

echo "Generating a test clip for: '$TARGET'"

python - "$TARGET" <<'PYEOF'
import sys
import subprocess
import os

target = sys.argv[1]

# Generate a single test clip using piper-sample-generator
output_file = "test_wake_word.wav"

# Use the piper-sample-generator to create a sample
cmd = [
    sys.executable,
    "piper-sample-generator/generate_samples.py",
    "--text", target.replace("_", " "),
    "--model", "piper-sample-generator/models/en_US-libritts_r-medium.pt",
    "--output-dir", ".",
    "--max-samples", "1",
    "--file-names", output_file.replace(".wav", ""),
]

print(f"Running: {' '.join(cmd)}")
subprocess.run(cmd, check=True)

if os.path.exists(output_file):
    print(f"\nGenerated: {output_file}")
    print("Play it with:  aplay test_wake_word.wav")
    print("  or:          mpv test_wake_word.wav")
    print("  or:          paplay test_wake_word.wav")
else:
    # Check if it was saved with a different name
    wav_files = [f for f in os.listdir(".") if f.endswith(".wav") and "test_wake" not in f]
    if wav_files:
        latest = max(wav_files, key=os.path.getmtime)
        print(f"\nGenerated: {latest}")
        print(f"Play it with:  aplay {latest}")
PYEOF
