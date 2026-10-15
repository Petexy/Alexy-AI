# Maintainer: Petexy <https://github.com/Petexy>
#
# Release package built from the checkout this file lives in:
#   makepkg -si
#
# The crate takes the Linexin Center's crates from its repository; Cargo
# fetches them, and Cargo.lock pins the commit.
#
# Same name and files as the Python package it replaces, except that
# aa-alexy-ai-widget.py becomes aa-alexy-ai-widget.so: the Center keeps the
# widget's place and hidden state across the upgrade, and the saved settings
# and conversations in ~/.config/linexin are read as they are.

pkgname=alexy-ai
pkgver=3.0.0
pkgrel=2
pkgdesc="Alexy Assistant"
url='https://github.com/Petexy/Alexy-AI'
arch=('x86_64')
license=('GPL-3.0-or-later')
# python runs the Whisper (and Vosk) helper the widget talks to and the
# hey-linux wake-word daemon; alsa-utils records and plays; python-pyaudio is
# the daemon's. The Python widget's python-gobject and webkitgtk-6.0 are no
# longer needed by the widget itself.
depends=('linexin-center>=4.0.0' 'gtk4' 'libadwaita' 'python' 'alsa-utils'
         'python-openai-whisper' 'python-pyaudio')
makedepends=('cargo')
# No makepkg LTO: it compiles the C code some Rust crates build (ring, the
# TLS library's crypto) into GCC bytecode the Rust linker cannot use, leaving
# those symbols undefined in the built library, which then fails to load.
# Rust's own optimisation is set in Cargo.toml.
options=('!lto')

_root="$(realpath "$startdir")"


build() {
  cd "$_root"
  export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$_root/target}"
  # Distribution builds must run on any x86-64 CPU: drop a host-specific
  # target-cpu from makepkg.conf (e.g. CachyOS's "native") and pin the baseline.
  export RUSTFLAGS="${RUSTFLAGS//-C target-cpu=native/} -C target-cpu=x86-64"
  cargo build --release --locked --lib
}

package() {
  cd "$_root"
  local release="${CARGO_TARGET_DIR:-$_root/target}/release"

  install -Dm644 "$release/libalexy_ai.so" \
    "$pkgdir/usr/share/linexin/widgets/aa-alexy-ai-widget.so"

  # Everything else the Python package shipped, with its modes.
  local file
  while IFS= read -r -d '' file; do
    if [[ -x "data/$file" ]]; then
      install -Dm755 "data/$file" "$pkgdir/$file"
    else
      install -Dm644 "data/$file" "$pkgdir/$file"
    fi
  done < <(cd data && find . -type f -printf '%P\0')

  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
