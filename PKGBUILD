# Maintainer: Petexy <https://github.com/Petexy>

pkgname=alexy-ai
pkgver=1.1.2.r
pkgrel=1
pkgdesc='Alexy Assistant'
url='https://github.com/Petexy'
arch=('x86_64')
license=('GPL-3.0')
depends=(
  'python-gobject'
  'gtk4'
  'libadwaita'
  'webkitgtk-6.0'
  'python'
  'alsa-utils'
  'python-openai-whisper'
  'python-pyaudio'
)

package() {
    cd "${srcdir}"

    find usr -type f | while IFS= read -r _file; do
        if [[ "${_file}" == usr/bin/* ]]; then
            install -Dm755 "${_file}" "${pkgdir}/${_file}"
        else
            install -Dm644 "${_file}" "${pkgdir}/${_file}"
        fi
    done
}
