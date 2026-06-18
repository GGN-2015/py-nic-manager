OpenVPN TAP-Windows6 driver assets
==================================

This directory bundles OpenVPN TAP-Windows6 9.27.0 driver distribution files
from the OpenVPN `tap-windows6` GitHub release:

https://github.com/OpenVPN/tap-windows6/releases/tag/9.27.0

The bundled files are used on Windows to create an Ethernet-like TAP virtual
adapter before falling back to Wintun. TAP adapters are more likely to be
accepted by Windows Internet Connection Sharing as a private/shared interface
than layer-3 TUN adapters.

The TAP-Windows6 driver files keep their own GPLv2 license. See
`COPYRIGHT.GPL` in this directory.
