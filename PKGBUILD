# Maintainer: Petexy <https://github.com/Petexy>

pkgname=alexy-ai
pkgver=1.0.0.r
pkgrel=1
_currentdate=$(date +"%Y-%m-%d%H-%M-%S")
pkgdesc='Alexy Assistant'
url='https://github.com/Petexy'
arch=(x86_64)
license=('GPL-3.0')
depends=(
  python-gobject
  gtk4
  libadwaita
  webkitgtk-6.0
  python
  alsa-utils
  python-openai-whisper
  python-pyaudio
)
makedepends=(
)

package() {
   mkdir -p ${pkgdir}/usr/share/linexin/widgets
   mkdir -p ${pkgdir}/usr/bin
   mkdir -p ${pkgdir}/usr/applications
   mkdir -p ${pkgdir}/usr/icons   
   mkdir -p ${pkgdir}/usr/wakewordmodels
   cp -rf ${srcdir}/usr/ ${pkgdir}/
}
