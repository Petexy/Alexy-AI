#!/bin/bash
# Plays the same steps on the native widget (harness.rs) and the original
# Python widget (python_reference.py) in a private headless KWin and compares
# every pair: PNGs pixel by pixel, the lines both print, the commands both ran,
# the requests the models received and the files they left.
#
#   cargo build --lib --examples
#   examples/compare.sh                 # ONLY=REGEX limits the scenarios
#
# With LXB_LIBRARY_DIR naming a folder with a liblxb_app.so that has
# lxb_app_shot_after and lxb_page_taking_text (lxb-toolkit 0.9.4), the LineXinBar page is
# played too (examples/lxb_run.rs) and what it ran and asked is checked.
#
# Nothing touches the real session, system or network: everything runs in
# private user, mount, PID and network namespaces (so "localhost" is
# examples/fake_ai.py, /tmp is private and no real process can be signalled),
# with a private D-Bus session, HOME and TMPDIR under .test-env, rebuilt before
# every run; fake sudo wrapper, pacman, ollama, curl, recorders, players and
# screenshot tools first on PATH. The host and the Python stand-in accept only
# the password "secret".
set -u
examples=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$examples/.." && pwd)
env_dir=$repo/.test-env
out=$env_dir/compare
fixtures=$env_dir/fixtures
socket=${LINEXIN_TEST_SOCKET:-linexin-test-alexy}
# Where cargo built this checkout (CARGO_TARGET_DIR and cargo's settings
# count), asked before the private session, which has no cargo on PATH.
target=${LINEXIN_COMPARE_TARGET:-$(cd "$repo" && cargo metadata --no-deps --format-version 1 |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["target_directory"])')/debug}
# The original Python widget: installed, or the copy PYTHON_WIDGET names.
python_widget=${PYTHON_WIDGET:-/usr/share/linexin/widgets/aa-alexy-ai-widget.py}

case "${ALEXY_STAGE:-}" in
"")
    harness=$target/examples/harness
    library=$target/libalexy_ai.so
    [ -x "$harness" ] && [ -f "$library" ] || { echo "run: cargo build --lib --examples" >&2; exit 2; }
    [ -f "$python_widget" ] || {
        echo "No original Python widget at $python_widget: install it, or name its file in PYTHON_WIDGET" >&2
        exit 2
    }
    rm -rf "$env_dir"
    mkdir -p "$out/png" "$out/logs" "$env_dir/bin" "$env_dir/ollama-bin" "$env_dir/widgets" "$fixtures"
    cp "$library" "$env_dir/widgets/aa-alexy-ai-widget.so"
    ln -s "$repo/data/usr/share/linexin/widgets/themes" "$env_dir/widgets/themes"
    ln -s "$repo/data/usr/share/linexin/widgets/sounds" "$env_dir/widgets/sounds"
    # A 4x3 PNG the fake screenshot tools "capture".
    python3 - "$fixtures/screen.png" <<'EOF'
import struct, sys, zlib
w, h = 4, 3
raw = b"".join(b"\x00" + b"".join(bytes([60 * x, 80 * y, 200, 255]) for x in range(w)) for y in range(h))
def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) \
    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
open(sys.argv[1], "wb").write(png)
EOF

    cat >"$env_dir/bin/fake-wrapper" <<'EOF'
#!/bin/sh
echo "wrapper $*" >>"$FAKE_LOG"
exec "$@"
EOF
    # Anything that would change the system: logged, with $FAKE_STATUS.
    cat >"$env_dir/bin/fake" <<'EOF'
#!/bin/sh
echo "${0##*/} $*" >>"$FAKE_LOG"
case "${0##*/}" in
    pacman) printf ':: Synchronizing package databases...\n core is up to date\n';;
esac
exit "${FAKE_STATUS:-0}"
EOF
    # Screenshots: grim "captures" the fixture unless $FAKE_GRIM_FAIL.
    cat >"$env_dir/bin/grim" <<'EOF'
#!/bin/sh
echo "grim" >>"$FAKE_LOG"
[ -n "${FAKE_GRIM_FAIL:-}" ] && exit 1
cp "$FAKE_FIXTURES/screen.png" "$1"
EOF
    cat >"$env_dir/bin/fail" <<'EOF'
#!/bin/sh
echo "${0##*/}" >>"$FAKE_LOG"
exit 1
EOF
    # curl: `-o FILE` writes a stand-in for the download; otherwise (the
    # Ollama install script) a script that pretends to install.
    cat >"$env_dir/bin/curl" <<'EOF'
#!/bin/sh
echo "curl $*" >>"$FAKE_LOG"
output=
while [ $# -gt 0 ]; do
    [ "$1" = -o ] && { output=$2; shift; }
    shift
done
if [ -n "$output" ]; then
    echo "downloaded" >"$output"
    exit 0
fi
cat <<'SCRIPT'
echo '>>> Installing ollama to /usr/local'
printf '######                    25.0%%\r'
printf '########################  100.0%%\n'
echo '>>> Creating ollama user...'
mkdir -p "$FAKE_STATE/bin" && cp "$FAKE_OLLAMA_SOURCE" "$FAKE_STATE/bin/ollama"
echo '>>> Install complete. Run "ollama" from the command line.'
exit ${FAKE_INSTALL_STATUS:-0}
SCRIPT
EOF
    cat >"$env_dir/ollama-bin/ollama" <<'EOF'
#!/bin/sh
echo "ollama $*" >>"$FAKE_LOG"
case "$1" in
    list)
        echo "NAME                ID              SIZE      MODIFIED"
        [ -n "${FAKE_NO_MODELS:-}" ] && exit 0
        echo "qwen3.5:latest      a1b2c3d4e5f6    6.6 GB    2 days ago"
        echo "llama3.2:latest     0f9e8d7c6b5a    2.0 GB    3 weeks ago"
        echo "tiny:1b             123456789abc    815 MB    5 hours ago"
        ;;
    pull)
        # FAKE_PULL_SLEEP between steps, so a page can be looked at mid-pull.
        pause() { [ -n "${FAKE_PULL_SLEEP:-}" ] && sleep "$FAKE_PULL_SLEEP"; }
        echo "pulling manifest"; pause
        printf 'pulling 8eeb52dfb3bb:  45%% |#####      | 2.9 GB/6.6 GB\r'; pause
        printf 'pulling 8eeb52dfb3bb: 100%% |##########| 6.6 GB/6.6 GB\n'
        echo "verifying sha256 digest"
        echo "writing manifest"
        echo "success"
        touch "$FAKE_STATE/pulled-$2"
        ;;
esac
exit "${FAKE_STATUS:-0}"
EOF
    # tar unpacking Piper: the fake engine.
    cat >"$env_dir/bin/tar" <<'EOF'
#!/bin/sh
echo "tar $*" >>"$FAKE_LOG"
while [ $# -gt 0 ]; do
    [ "$1" = -C ] && { mkdir -p "$2/piper" && cp "$FAKE_PIPER" "$2/piper/piper"; }
    shift
done
EOF
    # Piper reading what to say, writing no sound.
    cat >"$env_dir/bin/fake-piper" <<'EOF'
#!/bin/sh
echo "piper $* <<$(cat)" >>"$FAKE_LOG"
EOF
    cat >"$env_dir/bin/linexin-center" <<'EOF'
#!/bin/sh
echo "linexin-center $* LINEXIN_NEW_INSTANCE=$LINEXIN_NEW_INSTANCE" >>"$FAKE_LOG"
EOF
    # A microphone hearing half a second of a tone (unless FAKE_SILENCE),
    # then silence, in real time.
    cat >"$env_dir/bin/arecord" <<'EOF'
#!/bin/sh
echo "arecord $*" >>"$FAKE_LOG"
exec python3 -c '
import math, os, struct, sys, time
out = sys.stdout.buffer
tone = b"".join(struct.pack("<h", int(8000 * math.sin(i / 5))) for i in range(2000))
quiet = bytes(4000)
chunks = ([quiet] if os.environ.get("FAKE_SILENCE") else [tone] * 4) + [quiet] * 40
for chunk in chunks:
    out.write(chunk)
    out.flush()
    time.sleep(0.125)
'
EOF
    # openai-whisper, as both widgets import it.
    mkdir -p "$fixtures/py/whisper"
    cat >"$fixtures/py/whisper/__init__.py" <<'EOF'
import os


def _log(line):
    with open(os.environ["FAKE_LOG"], "a") as file:
        file.write(line + "\n")


class _Model:
    def transcribe(self, path, fp16=True, **_):
        _log(f"whisper transcribe fp16={fp16} wav={os.path.getsize(path) > 44}")
        return {"text": " " + os.environ.get("FAKE_TRANSCRIPT", "hello") + " ", "language": "en"}


def load_model(name, *_, **__):
    _log(f"whisper load_model {name}")
    if os.environ.get("FAKE_WHISPER_FAIL"):
        raise RuntimeError("CUDA out of memory")
    return _Model()
EOF
    chmod 755 "$env_dir/bin/"* "$env_dir/ollama-bin/"*
    cat >"$env_dir/bin/pkexec" <<'EOF'
#!/bin/sh
echo "pkexec $*" >>"$FAKE_LOG"
exec "$@"
EOF
    chmod 755 "$env_dir/bin/pkexec"
    for name in sudo pacman paru flatpak reboot shutdown poweroff systemctl pkill \
        hey-linux paplay aplay piper espeak-ng espeak notify-send xdg-open; do
        ln -s fake "$env_dir/bin/$name"
    done
    for name in gnome-screenshot spectacle scrot import; do ln -s fail "$env_dir/bin/$name"; done
    cat >"$env_dir/bus.conf" <<'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir=/tmp</listen>
  <policy context="default"><allow send_destination="*"/><allow receive_sender="*"/><allow own="*"/></policy>
</busconfig>
EOF
    # The private /tmp hides anything under the real one: the toolkit's
    # libraries are copied in.
    if [ -n "${LXB_LIBRARY_DIR:-}" ]; then
        mkdir -p "$env_dir/lxb"
        cp "$LXB_LIBRARY_DIR"/liblxb_app.so "$LXB_LIBRARY_DIR"/liblxb_toolkit.so "$env_dir/lxb/" || exit 2
        LXB_LIBRARY_DIR=$env_dir/lxb
    fi
    "$harness" --print-css >"$env_dir/center-style.css" || exit 2
    exec env ALEXY_STAGE=root ALEXY_UID="$(id -u)" ALEXY_GID="$(id -g)" ONLY="${ONLY:-}" \
        LXB_LIBRARY_DIR="${LXB_LIBRARY_DIR:-}" LINEXIN_COMPARE_TARGET="$target" \
        PYTHON_WIDGET="$python_widget" \
        unshare --user --map-root-user --net --mount --pid --fork --mount-proc "$0"
    ;;
root)
    # Inside the namespaces: loopback up, a private /tmp, then back to our uid.
    ip link set lo up || exit 1
    mount -t tmpfs tmpfs /tmp || exit 1
    # The wake-word daemon is started by its absolute path: a fake here.
    if [ -e /usr/bin/hey-linux ]; then
        mount --bind "$env_dir/bin/fake" /usr/bin/hey-linux || exit 1
    fi
    exec env ALEXY_STAGE=user unshare --user --map-user="$ALEXY_UID" --map-group="$ALEXY_GID" "$0"
    ;;
user)
    exec env -i LINEXIN_COMPARE_TARGET="$target" PYTHON_WIDGET="$python_widget" \
        LINEXIN_CENTER_CSS="$env_dir/center-style.css" PATH="$env_dir/ollama-bin:$env_dir/bin:/usr/bin" HOME="$env_dir/home" TMPDIR="$env_dir/tmp" \
        USER="$(id -un)" \
        XDG_CONFIG_HOME="$env_dir/home/.config" XDG_CACHE_HOME="$env_dir/home/.cache" \
        XDG_DATA_HOME="$env_dir/home/.local/share" XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
        LANG=en_US.UTF-8 MESA_SHADER_CACHE_DISABLE=true ADW_DISABLE_PORTAL=1 GDK_DEBUG=no-portals GTK_A11Y=none \
        FAKE_WRAPPER="$env_dir/bin/fake-wrapper" FAKE_PASSWORD=secret \
        FAKE_FIXTURES="$fixtures" FAKE_STATE="$env_dir/state" FAKE_HTTP_LOG="$out/logs/http.current" \
        FAKE_OLLAMA_SOURCE="$env_dir/ollama-bin/ollama" FAKE_PIPER="$env_dir/bin/fake-piper" \
        ALEXY_STAGE=session ONLY="${ONLY:-}" LXB_LIBRARY_DIR="${LXB_LIBRARY_DIR:-}" \
        dbus-run-session --config-file="$env_dir/bus.conf" -- bash -c '
        kwin_wayland --virtual --no-lockscreen --width 1600 --height 1000 --socket "$1" \
            >"$2/logs/kwin.log" 2>&1 &
        kwin=$!
        python3 -u "$4" >"$2/logs/fake_ai.log" 2>&1 &
        server=$!
        for _ in $(seq 100); do [ -S "$XDG_RUNTIME_DIR/$1" ] && grep -q ready "$2/logs/fake_ai.log" && break; sleep 0.1; done
        WAYLAND_DISPLAY=$1 GDK_BACKEND=wayland "$3"
        status=$?
        kill $kwin $server; wait $kwin $server 2>/dev/null
        exit $status' session "$socket" "$out" "$0" "$examples/fake_ai.py" \
        2> >(grep -v --line-buffered 'ignoring unknown child process' >&2)
    ;;
esac

harness=$target/examples/harness
library=$env_dir/widgets/aa-alexy-ai-widget.so
total=0
differing=0
# Scenarios where the port deliberately differs (README.md, "Deliberate
# differences"): reported, not counted.
KNOWN='^(no-ollama-cancel)$'
known=0

# The saved conversations of the CONVS=list fixture.
conversations() {
    python3 - "$HOME/.config/linexin/conversations" <<'EOF'
import json, os, sys
folder = sys.argv[1]
os.makedirs(folder, exist_ok=True)
system = {"role": "system", "content": "You are a test."}
def save(cid, title, updated, backend, agent, history):
    data = {"id": cid, "title": title, "created": "2026-10-01T10:00:00.000001",
            "updated": updated, "backend": backend, "agent": agent,
            "chat_history": [system] + history}
    if agent is None:
        del data["agent"]
    with open(os.path.join(folder, f"{cid}.json"), "w") as f:
        json.dump(data, f, indent=2)
save("11111111-1111-4111-8111-111111111111", "How do I update?", "2026-10-05T09:15:00.123456",
     "local", "Alexy", [
         {"role": "user", "content": "How do I update?"},
         {"role": "assistant", "content": "```bash\nflatpak update -y\n```"},
         {"role": "user", "content": "System Command Execution Results:\n\nCommand:\nflatpak update -y\nExit Code: 0"},
         {"role": "assistant", "content": "Everything is **up to date**."}])
save("22222222-2222-4222-8222-222222222222", "Write a poem", "2026-10-06T21:40:10.5",
     "local", "Rhymexy", [
         {"role": "user", "content": "Write a poem"},
         {"role": "assistant", "content": "Roses are red,\nthe kernel is new."}])
save("33333333-3333-4333-8333-333333333333", "Older chat", "2026-09-30T08:00:00",
     "local", None, [
         {"role": "user", "content": [
             {"type": "text", "text": "[A screenshot of my current screen is attached. IMPORTANT: If my question is NOT about the screen content, do NOT describe, mention, reference, or acknowledge the screenshot in any way — just answer my question directly as if no screenshot was provided. Only use the screenshot if my question is specifically about what is on screen. LANGUAGE RULE: The language visible in the screenshot must NEVER influence your reply language. Always reply in the language of my text message, regardless of what language appears on screen.]\n\nWhat is on screen?"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="}}]},
         {"role": "assistant", "content": "A *terminal*."}])
save("44444444-4444-4444-8444-444444444444", "Online question", "2026-10-07T12:00:00.000002",
     "direct", "Alexy", [
         {"role": "user", "content": "Online question"},
         {"role": "assistant", "content": "Online answer."}])
save("55555555-5555-4555-8555-555555555555", "Legacy qwen", "2026-10-02T12:00:00",
     "qwen_cli", "Alexy", [
         {"role": "user", "content": "Legacy"},
         {"role": "assistant", "content": "Old."}])
save("66666666-6666-4666-8666-666666666666", "Custom agent chat", "2026-10-03T12:00:00",
     "local", "Pirate", [
         {"role": "user", "content": "Ahoy"},
         {"role": "assistant", "content": "Arr!"}])
with open(os.path.join(folder, "broken.json"), "w") as f:
    f.write("{not json")
EOF
}

# fixture CONFIG EXTRA: a fresh HOME and TMPDIR; CONFIG names a config file
# (- for none); EXTRA=list saves the conversations above, voice a downloaded
# Whisper model and Piper engine (voice-nomodel without the model,
# voice-nopiper without Piper).
fixture() {
    rm -rf "$HOME" "$TMPDIR" "$FAKE_STATE"
    mkdir -p "$HOME/.config" "$HOME/.cache" "$HOME/.local/share" "$TMPDIR" "$FAKE_STATE"
    local config=$HOME/.config/linexin/ai-sysadmin.json
    mkdir -p "$HOME/.config/linexin"
    case "$1" in
        -) rmdir "$HOME/.config/linexin" ;;
        endpoint) printf '{"backend": "endpoint", "endpoint_url": "http://127.0.0.1:6767", "endpoint_model": ""}' >"$config" ;;
        endpoint-down) printf '{"backend": "endpoint", "endpoint_url": "http://127.0.0.1:6768/v1/chat/completions", "endpoint_model": "cached-model"}' >"$config" ;;
        direct) printf '{"backend": "direct", "api_key": "test-key", "api_url": "http://127.0.0.1:6767/v1/", "model": "fake-online"}' >"$config" ;;
        direct-nokey) printf '{"backend": "direct", "api_key": "", "model": "fake-online"}' >"$config" ;;
        missing) printf '{"backend": "local", "local_model": "missing-model"}' >"$config" ;;
        ask) printf '{"backend": "local", "auto_execute_commands": false}' >"$config" ;;
        imessage|matrix) printf '{"theme": "%s"}' "$1" >"$config" ;;
        agents) cat >"$config" <<'EOF'
{"backend": "local", "local_model": "llama3.2:latest", "user_agents": [{"name": "Pirate", "prompt": "Talk like a pirate."}, {"name": "Rhymexy", "prompt": "dropped"}, {"prompt": "nameless"}], "active_agent": "Pirate", "stt_backend": "vosk", "theme": "imessage"}
EOF
            ;;
        vosk) printf '{"stt_backend": "vosk"}' >"$config" ;;
        hey) printf '{"hey_linux_enabled": true}' >"$config" ;;
        legacy) printf '{"backend": "qwen_cli", "system_prompt": "My own legacy prompt."}' >"$config" ;;
        broken) printf '{"backend": ' >"$config" ;;
        *) echo "unknown config $1" >&2; return 1 ;;
    esac
    case "$2" in
        list) conversations ;;
        voice|voice-nopiper|voice-nomodel)
            if [ "$2" != voice-nomodel ]; then
                mkdir -p "$HOME/.cache/whisper"
                echo model >"$HOME/.cache/whisper/small.pt"
            fi
            if [ "$2" != voice-nopiper ]; then
                mkdir -p "$HOME/.local/share/linexin/piper" "$HOME/.local/share/linexin/piper-models"
                cp "$FAKE_PIPER" "$HOME/.local/share/linexin/piper/piper"
                echo voice >"$HOME/.local/share/linexin/piper-models/en_US-libritts_r-medium.onnx"
            fi
            ;;
    esac
    return 0
}

# What differs between two runs by chance: random ids, times, temp names.
normalize() {
    sed -E \
        -e 's/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/UUID/g' \
        -e 's/20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?/TIME/g' \
        -e 's/screen_[0-9a-f]{32}/screen_RANDOM/g' \
        -e 's/linexin_[0-9a-f]{8}/linexin_RANDOM/g' \
        -e 's/(You are Alexy, an expert AI Sysadmin)[^"]*"/\1..."/g' \
        -e "s/$(date +%F) [0-9]{2}:[0-9]{2}/TODAY/g" \
        -e "s#$env_dir/widgets/#WIDGETS/#g; s#/usr/share/linexin/widgets/#WIDGETS/#g" \
        "$1"
}

# pair TAG CONFIG CONVS ENV OPTIONS... STEPS...: both sides with the same
# fixture and environment (ENV: VAR=value[;VAR=value]... or "-"); the PNG
# names (@) are rewritten per side, into png/TAG/.
pair() {
    local tag=$1 config=$2 convs=$3 vars=$4
    shift 4
    [ -n "$ONLY" ] && ! [[ $tag =~ $ONLY ]] && return
    local side extra=()
    mkdir -p "$out/png/$tag"
    [ "$vars" != - ] && IFS=';' read -r -a extra <<<"$vars"
    for side in rust py; do
        fixture "$config" "$convs" || return
        local args=() arg
        for arg in "$@"; do args+=("${arg//@/$out/png/$tag/$side}"); done
        local base=$out/logs/$side-$tag
        : >"$base.commands"
        : >"$base.locks"
        rm -f "$out/logs/http.current"
        (
            cd "$HOME" || exit 1
            if [ $side = rust ]; then
                env "${extra[@]}" FAKE_LOG="$base.commands" FAKE_LOCK_LOG="$base.locks" \
                    timeout 150 "$harness" --library "$library" "${args[@]}"
            else
                env "${extra[@]}" FAKE_LOG="$base.commands" FAKE_LOCK_LOG="$base.locks" \
                    timeout 150 python3 -B "$examples/python_reference.py" "${args[@]}"
            fi
        ) >"$base.out" 2>"$base.err"
        echo "exit $?" >>"$base.out"
        mv "$out/logs/http.current" "$base.http" 2>/dev/null || : >"$base.http"
        sort -o "$base.locks" "$base.locks"
        # Commands started together (the activation sound and the
        # recorder) log in whichever order they ran: compared sorted.
        sort -o "$base.commands" "$base.commands"
        ls -A /tmp >"$base.tmp"
    done
    local differences=0 png kind
    for side in rust py; do
        if ! grep -q '^exit 0$' "$out/logs/$side-$tag.out"; then
            echo "$tag: the $side side failed: $(grep -h 'harness:\|reference:' "$out/logs/$side-$tag.err" | tail -1)"
            differences=1
        fi
    done
    if ! diff -q <(normalize "$out/logs/rust-$tag.out" | grep -v '^widgetname\|^rendered') \
        <(normalize "$out/logs/py-$tag.out" | grep -v '^widgetname\|^rendered') >/dev/null; then
        echo "$tag: printed lines differ"
        diff <(normalize "$out/logs/rust-$tag.out" | grep -v '^widgetname\|^rendered') \
            <(normalize "$out/logs/py-$tag.out" | grep -v '^widgetname\|^rendered') | cut -c1-${DIFF_WIDTH:-400} | head -${DIFF_LINES:-14}
        differences=1
    fi
    for kind in commands locks http tmp; do
        if ! diff -q <(normalize "$out/logs/rust-$tag.$kind") <(normalize "$out/logs/py-$tag.$kind") >/dev/null; then
            echo "$tag: $kind differ"
            diff <(normalize "$out/logs/rust-$tag.$kind") <(normalize "$out/logs/py-$tag.$kind") | cut -c1-400 | head -10
            differences=1
        fi
    done
    local pairs=0
    for png in "$out/png/$tag"/rust*.png; do
        [ -e "$png" ] || continue
        pairs=$((pairs + 1))
        local other=$out/png/$tag/py${png##*/rust}
        if ! python3 - "$png" "$other" <<'EOF'
import sys
from PIL import Image, ImageChops
a, b = (Image.open(p).convert("RGBA") for p in sys.argv[1:3])
if a.size != b.size:
    print(f"{sys.argv[1]}: size {a.size} vs {b.size}")
    sys.exit(1)
box = ImageChops.difference(a, b).getbbox()
if box:
    print(f"{sys.argv[1]}: differs in {box}")
    sys.exit(1)
EOF
        then
            differences=1
        fi
    done
    total=$((total + 1))
    if [ $differences = 0 ]; then
        echo "$tag: $pairs image pair(s), identical"
    elif [[ $tag =~ $KNOWN ]]; then
        echo "$tag: differs as documented"
        known=$((known + 1))
    else
        differing=$((differing + 1))
    fi
}

unlock=answer=authenticate=secret
# The look.
for lang in en_US pl_PL; do
    pair "$lang-initial" - - - --lang $lang print shot=@.png files
done
pair "dark" - - - --dark print shot=@.png
pair "narrow" - - - --width 800 --height 600 shot=@.png
for theme in imessage matrix; do
    pair "theme-$theme" $theme - - print shot=@.png
    pair "theme-$theme-dark" $theme - - --dark type=hello send wait-idle settle shot=@.png
done
pair "broken-config" broken - - print files
pair "legacy-config" legacy - - print files settings print
pair "agents-config" agents - - print shot=@.png settings settings-page=agents settle print shot=@-agents.png

# Replies.
pair "hello" - - - type=hello send wait-idle settle print shot=@.png files
pair "hello-pl" - - - --lang pl_PL type=polish send wait-idle settle print shot=@.png
pair "think" - - - type=think send wait-idle settle print shot=@.png expand settle shot=@-expanded.png
pair "inline" - - - type=inline send wait-idle settle print
pair "reasonly" - - - type=reasonly send wait-idle settle print shot=@.png
pair "long" - - - type=long send wait-idle settle print shot=@.png
pair "run" - - - type=run send wait-idle settle print shot=@.png files
pair "two" - - - type=two send wait-idle settle print
pair "sudo" - - - type=sudo send "$unlock" wait-idle settle print shot=@.png
pair "sudo-wrong" - - - type=sudo send answer=authenticate=nope answer=ok wait-idle settle print
pair "sudo-cancel" - - - type=sudo send answer=cancel wait-idle settle print
pair "reboot" - - - type=reboot send wait-idle settle print files
pair "ask-allow" ask - - type=run send wait-dialog dialog-shot=@-dialog.png answer=allow wait-idle settle print
pair "ask-deny" ask - - type=two send answer=deny answer=deny wait-idle settle print
pair "fail" - - - type=fail send wait-idle settle print shot=@.png
pair "ollama-error" - - - type=oerror send wait-idle settle print
pair "missing-model" missing - FAKE_PULL_SLEEP=2 type=hello send wait-window sleep=1 print \
    window-shot=@-pull.png wait-window-done print sleep=2.5 wait-idle settle print
pair "stop" - - - type=slow send sleep=1 send settle print sleep=2 print
pair "screen" - - - screen type=hello send wait-idle settle print shot=@.png
pair "screen-fail" - - FAKE_GRIM_FAIL=1 screen type=hello send wait-idle settle print
pair "empty" - - - type= send settle print
pair "endpoint" endpoint - - print type=hello send wait-idle settle print files
pair "endpoint-think" endpoint - - type=think send wait-idle settle print
pair "endpoint-plain" endpoint - - type=plain send wait-idle settle print
pair "endpoint-weird" endpoint - - type=weird send wait-idle settle print
pair "endpoint-no-models" endpoint - FAKE_NO_MODELS=1 type=hello send wait-idle settle print
pair "endpoint-down" endpoint-down - - type=hello send wait-idle settle print
pair "direct" direct - - print type=run send wait-idle settle print
pair "direct-nokey" direct-nokey - - type=hello send settle print
pair "no-ollama-install" - - "PATH=$env_dir/state/bin:$env_dir/bin:/usr/bin" type=hello send \
    "$unlock" wait-window wait-window-done print sleep=2.5 wait-idle settle print
pair "no-ollama-cancel" - - "PATH=$env_dir/bin:/usr/bin" type=hello send answer=cancel settle print

# Conversations.
pair "conv-empty" - - - conversations settle print shot=@.png
pair "conv-list" - list - conversations settle print shot=@.png filter="Online API" settle print
pair "conv-list-pl" - list - --lang pl_PL conversations settle print shot=@.png
pair "conv-load" - list - conversations settle "conv-row=How do I update?" settle print shot=@.png
pair "conv-load-image" - list - conversations settle "conv-row=Older chat" settle print shot=@.png
pair "conv-launch" - list - --conversation 22222222-2222-4222-8222-222222222222 settle print
pair "conv-compact" - list - --compact-mode conversations settle print shot=@.png
pair "conv-compact-narrow" - list - --compact-mode --width 900 conversations settle print
pair "conv-rename" - list - conversations settle "conv-edit=Write a poem" settle print shot=@.png \
    "rename=  A better title " settle print files
pair "conv-delete" - list - conversations settle "conv-delete=Older chat" settle print
pair "conv-after-chat" - - - type=hello send wait-idle settle conversations settle print new settle print files

# Settings.
pair "settings" - - - settings settle print shot=@.png
pair "settings-pl" - - - --lang pl_PL settings settle print shot=@.png
for page in agents speech theme; do
    pair "settings-$page" - - - settings settings-page=$page settle print shot=@.png
done
pair "settings-dark" - - - --dark settings settings-page=theme settle shot=@.png
pair "settings-no-ollama" - - "PATH=$env_dir/bin:/usr/bin" settings settle print
pair "settings-no-models" - - FAKE_NO_MODELS=1 settings settle print
pair "settings-apply" - - - settings settle "combo=Backend Type=2" "switch=Auto-Execute Commands" \
    settings-page=theme "combo=Theme=1" close-settings settle print shot=@.png files
pair "settings-endpoint-url" endpoint - - settings settle "entryrow=Endpoint URL=http://127.0.0.1:6767/v1" \
    close-settings settle print files

# Agents.
pair "agents-create" - - - settings settings-page=agents settle "click=New Agent" answer=create=Pirate settle \
    print shot=@.png "prompt=Talk like a pirate." close-settings settle print files
pair "agents-rename-delete" agents - - settings settings-page=agents settle "click=Rename Agent" \
    answer=rename=Captain settle print "click=Delete Agent" settle print close-settings settle print files
pair "agents-chat" agents - - type=hello send wait-idle settle print

# Speech.
pair "hey-on" - - - settings settings-page=speech settle "switch=Enable \"Hey Alexy\"" settle print files
pair "hey-off" hey - - settings settings-page=speech settle "switch=Enable \"Hey Alexy\"" settle print files
pair "vosk" vosk - - print settings settings-page=speech settle print shot=@.png
pair "vosk-install" vosk - - settings settings-page=speech settle "click=Install python-vosk" "$unlock" \
    wait-window wait-window-done print sleep=2.5 settle print

# Models from the settings.
pair "pull" - - - settings settle "entryrow=Model Name (e.g. qwen3.5)=tiny:1b" "click=Pull from Registry" \
    wait-window wait-window-done print sleep=2.5 settle print
pair "remove-model" - - - settings settle "click=Remove Model" wait-window wait-window-done print \
    sleep=2.5 settle print
pair "uninstall-ollama" - - - settings settle "click=Uninstall Ollama" "$unlock" wait-window \
    wait-window-done print sleep=2.5 settle print

# Voice.
python=PYTHONPATH=$fixtures/py
pair "voice" - voice "$python" mic sleep=0.5 print wait-rows=3 wait-idle settle print files
pair "voice-silence" - voice "$python;FAKE_SILENCE=1" mic sleep=0.5 print sleep=6 settle print
pair "voice-fails" - voice "$python;FAKE_WHISPER_FAIL=1" mic settle print
pair "voice-no-model" - voice-nomodel "$python" mic wait-window wait-window-done print close-window \
    settle print
pair "voice-no-piper" - voice-nopiper "$python" mic wait-rows=3 wait-window wait-window-done print \
    sleep=2.5 wait-idle settle print
pair "voice-launch" - voice "$python" --voice wait-rows=3 wait-idle settle print

# The compact voice window.
pair "compact" - - - --compact-window print shot=@.png
pair "compact-voice" - voice "$python" --compact-window --voice sleep=8 print shot=@.png
pair "compact-expand" - voice "$python" --compact-window --voice sleep=6 compact=Expand\ chat settle

# The LineXinBar page (no original to compare with): lxb TAG CONFIG EXTRA ENV
# FRAMES STEPS -- WHAT...: play the page, then every WHAT (KIND~TEXT, KIND
# one of out, commands, http) must be found in that log.
lxb() {
    local tag=$1 config=$2 extra=$3 vars=$4 frames=$5
    shift 5
    [ -n "$ONLY" ] && ! [[ $tag =~ $ONLY ]] && return
    if [ -z "$LXB_LIBRARY_DIR" ]; then
        echo "$tag: skipped (no LXB_LIBRARY_DIR)"
        return
    fi
    local steps=() env=()
    while [ $# -gt 0 ] && [ "$1" != -- ]; do steps+=("$1"); shift; done
    shift
    [ "$vars" != - ] && IFS=';' read -r -a env <<<"$vars"
    fixture "$config" "$extra" || return
    local base=$out/logs/lxb-$tag
    : >"$base.commands"
    rm -f "$out/logs/http.current"
    mkdir -p "$out/png/$tag"
    (
        cd "$HOME" || exit 1
        env "${env[@]}" FAKE_LOG="$base.commands" LD_LIBRARY_PATH="$LXB_LIBRARY_DIR" \
            timeout 120 "$target/examples/lxb_run" --library "$library" \
            --out "$out/png/$tag/lxb.png" --frames "$frames" "${steps[@]}"
    ) >"$base.out" 2>"$base.err"
    echo "exit $?" >>"$base.out"
    mv "$out/logs/http.current" "$base.http" 2>/dev/null || : >"$base.http"
    # What the page left: its settings, the saved conversations, autostart.
    cat "$HOME/.config/linexin/ai-sysadmin.json" >"$base.config" 2>/dev/null || : >"$base.config"
    cat "$HOME/.config/linexin/conversations/"*.json >"$base.convs" 2>/dev/null || : >"$base.convs"
    ls "$HOME/.config/autostart" >"$base.files" 2>/dev/null || : >"$base.files"
    total=$((total + 1))
    local missing=0 what kind text
    grep -q '^exit 0$' "$base.out" || { echo "$tag: the page failed: $(tail -1 "$base.err")"; missing=1; }
    for what in "$@"; do
        kind=${what%%~*}
        text=${what#*~}
        # noKIND~TEXT: TEXT must not be there.
        if [ "${kind#no}" != "$kind" ]; then
            if grep -qF -- "$text" "$base.${kind#no}"; then
                echo "$tag: ${kind#no} has $text"
                missing=1
            fi
        elif ! grep -qF -- "$text" "$base.$kind"; then
            echo "$tag: $kind lacks $text"
            missing=1
        fi
    done
    if [ $missing = 0 ]; then
        echo "$tag: the LineXinBar page did what it should"
    else
        differing=$((differing + 1))
    fi
}

lxb "lxb-voice" - voice "$python" 700 5:right 8:accept -- \
    "commands~arecord -f S16_LE -c 1 -r 16000 -q" "commands~whisper load_model small" \
    "commands~whisper transcribe fp16=False wav=True" "commands~piper --model" \
    'http~{"content": "hello", "role": "user"}' "out~Executing TTS: echo 'Hello there!" "commands~paplay"
lxb "lxb-voice-launch" - voice "$python" 700 --voice -- \
    "commands~grim" 'http~"images": ["' "out~Executing TTS:"
lxb "lxb-sudo" - voice "$python;FAKE_TRANSCRIPT=sudo" 800 5:right 8:accept -- \
    "commands~pkexec pacman -Syu --noconfirm" "commands~pacman -Syu --noconfirm" \
    "out~Executing autonomous SUDO command" "out~Executing TTS: echo 'The system is up to date.'"
lxb "lxb-endpoint" endpoint voice "$python" 700 5:right 8:accept -- \
    "http~GET 6767/v1/models" 'http~"model": "fake-endpoint-model"'
lxb "lxb-missing-model" missing voice "$python" 900 5:right 8:accept -- \
    "commands~ollama pull missing-model" 'http~"model": "missing-model"'

# The page is wide enough for the panel: the chat's control is the field,
# and Right goes into the panel: Listen (0), Attach an image (1), Screen
# Awareness (2), Active Agent (3), a new conversation (4), the saved ones (5),
# Settings (6), then the recent conversations.
lxb "lxb-typed" - - "$python;LINEXIN_LXB_TYPED=0.3:hello" 300 40:accept -- \
    'http~{"content": "hello", "role": "user"}' "noout~Executing TTS" 'convs~"hello"'
lxb "lxb-delete-conversation" - - "$python;LINEXIN_LXB_TYPED=0.3:hello" 420 40:accept \
    200:right 202:down 204:down 206:down 208:down 210:down 214:accept 250:menu 270:down 274:accept -- \
    'http~{"content": "hello", "role": "user"}' 'noconvs~"hello"'
# A conversation that is not open: saving the open one gives it its title
# again, as the original did.
lxb "lxb-rename-conversation" - - "$python;LINEXIN_LXB_TYPED=0.3:hello|5:\b\b\b\b\bRenamed" 420 40:accept \
    200:right 202:down 204:down 206:down 208:down 212:accept 220:down 224:accept 250:menu 274:accept \
    330:accept -- 'convs~"title": "Renamed"'
lxb "lxb-backend" - - "$python" 300 20:right $(for f in 22 24 26 28 30 32; do printf '%s:down ' $f; done) \
    40:accept 60:accept 80:down 82:down 84:accept 110:up 114:accept -- 'config~"backend": "direct"'
lxb "lxb-agent" - - "$python;LINEXIN_LXB_TYPED=2:Tester" 300 \
    20:right $(for f in 22 24 26 28 30 32; do printf '%s:down ' $f; done) 40:accept 60:down 62:accept \
    80:down 82:accept 160:accept -- 'config~"active_agent": "Tester"' 'config~"name": "Tester"'
lxb "lxb-hey-alexy" - - "$python" 300 20:right $(for f in 22 24 26 28 30 32; do printf '%s:down ' $f; done) \
    40:accept 60:down 62:down 64:accept 80:down 82:down 84:accept -- \
    "files~hey-linux.desktop" 'config~"hey_linux_enabled": true'

echo "$total scenarios, $differing with differences, $known as documented"
[ $differing = 0 ]
