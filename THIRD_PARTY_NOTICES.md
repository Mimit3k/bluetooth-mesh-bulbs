# Third-party notices

Mesh Bulbs itself is MIT-licensed (see `LICENSE`). It depends on and vendors
the following components. Their licences apply to those components, not to the
application code.

## Vendored source

### btmesh 0.11.0

- Copyright (c) 2026 David Simon
- Upstream: https://github.com/dasimon135/ha-bluetooth-mesh
- Licence: MIT
- Location: `src/btmesh/`
- Licence text: `third_party/btmesh/LICENSE`

### Phase 0 provisioning harness

- Copyright (c) 2026 David Simon
- Upstream: https://github.com/dasimon135/ha-bluetooth-mesh
- Licence: MIT
- Location: `phase0/`
- Licence text: `third_party/btmesh/LICENSE`

The files in `src/btmesh/` and `phase0/` are unmodified upstream code apart
from import bootstrapping in `meshcore.py`. Keep their MIT notices when
redistributing.

## Runtime dependencies

| Package | Licence | Notes |
|---|---|---|
| `cryptography` | Apache-2.0 OR BSD-3-Clause | AES-CMAC, AES-CCM, key derivation |
| `bleak` | MIT | BLE transport (BlueZ on Linux, WinRT on Windows) |
| `bleak-retry-connector` | MIT | Connection retries |
| `PyQt6` | GPL-3.0 or commercial | Desktop GUI |

### Important: PyQt6 is not MIT

PyQt6 is dual-licensed under the **GNU GPL v3** or a commercial licence from
Riverbank Computing. The MIT grant in `LICENSE` covers this project's own
source code, but **distributing a built application that links PyQt6 requires
complying with the GPL** (or holding a Riverbank commercial licence).

For a permissively licensed binary release, the practical options are:

1. Keep this repository source-only for now, with PyQt6 installed by the user.
2. Move the GUI from PyQt6 to **PySide6**, which is LGPL-3.0 and compatible
   with an MIT application when dynamically linked.
3. Licence the application under GPL-3.0.
4. Purchase a PyQt6 commercial licence.

This notice is not legal advice; review the licences before publishing binaries.

## Development-only dependencies

`pytest`, `pytest-asyncio` and `ruff` are used for testing and linting and are
not distributed with the application.
