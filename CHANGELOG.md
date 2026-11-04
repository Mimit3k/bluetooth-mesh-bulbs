# Changelog

All notable changes to Mesh Bulbs are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The vendored `btmesh` library has its own changelog at
`third_party/btmesh/CHANGELOG.md`.

## [Unreleased]

### Fixed

- Adding a factory-reset bulb no longer fails on the first try. On Windows the
  unprovisioned-device beacon is sometimes missed for ten seconds right after
  the previous scan or proxy link was torn down, which produced `Phase0Failure:
  no 0x1827 unprovisioned-device beacon seen` even though pressing **Add** a
  second time worked. The scan now runs up to three times, two seconds apart;
  it happens before any provisioning traffic, so a retry cannot leave a
  half-provisioned node behind.
- Windows setup now finds Python on Windows PowerShell 5.1. The version probe
  was a multi-line `-c` argument containing double quotes; PowerShell 5.1 does
  not escape those when handing arguments to a native command, so the probe
  arrived at Python as broken source and *every* interpreter — python.org,
  Miniconda, uv, Microsoft Store — was rejected with a `Traceback`. The probe
  is now one line without double quotes, and `$ErrorActionPreference` is relaxed
  around it so a native command's stderr cannot abort the check. The search also
  keeps Microsoft Store installs (the `WindowsApps` path is where the Store
  alias lives) instead of filtering them out, uses the interpreter's real
  `sys.executable`, consults `where.exe` for extra PATH matches, and reports
  why each candidate was rejected. A windowed `pythonw*.exe` is never used to
  build the environment — it is skipped when discovered, mapped to the console
  `python.exe` beside it, or rejected — and setup now verifies that
  `.venv\Scripts\python.exe` exists before installing dependencies. Windows
  creates a windowed interpreter next to every console one, and starting `venv`
  from it raced the dependency install.

- Config model binds are now addressed to the node's primary unicast address,
  with the element address inside the payload, as the mesh specification
  requires. This also means the Light Lightness Setup Server can be bound on
  demand for bulbs that were provisioned before it was part of the model list.
- Control no longer goes silent after an unclean shutdown. If the saved SEQ
  cursor is behind what the bulbs have already accepted, every command is
  discarded as a replay, which looks like all bulbs becoming unreachable at
  once. A run marker next to the state file now detects that case and advances
  SEQ by 10,000 on the next start.
- **Forget** now removes the bulb from the collection immediately. It no
  longer waits for a stale BLE link to close first, which could leave the entry
  visible for a minute after the UI had already reported success.
- Adding a bulb now waits 10 seconds after provisioning for the lamp's reboot
  and retries configuration five times with increasing backoff. The config
  server is often not ready for the first 20-30 seconds after a lamp joins,
  which previously made the wizard give up with a timeout even though
  provisioning had succeeded. Each model bind that times out also reconnects
  to the proxy and retries, and a failed element-0 model aborts the add
  instead of being silently skipped. Secondary-element models are skipped
  because the app always addresses the bulb's primary element. A final failure
  explains that the lamp is already on the mesh and how to retry it without a
  factory reset.
- The **Add bulb** wizard no longer aborts when an unrelated background
  operation fails. A status request for a different, unreachable bulb used to
  emit the shared `failed` signal, which the wizard treated as its own failure
  and closed with a misleading “Could not add the bulb” dialog. Add errors now
  use a dedicated `add_failed` signal.
- Provisioning recovers when BlueZ drops the scanned device object before the
  connect (`device 'dev_…' not found: The device disappeared`). The app scans
  for the address again and retries with the fresh object instead of reporting
  the lamp as unreachable.
- The configuration suite no longer checks POSIX `0600` mode bits on Windows,
  where `os.stat` reports `0o666` for any writable file regardless of its NTFS
  ACLs. This was the Windows CI failure; the check still runs on Linux.

### Changed

- `setup-windows.ps1` now discovers every registered 64-bit CPython 3.12 or
  newer and uses the newest one, instead of checking for hard-coded 3.12/3.13
  versions. Python 3.15+ is accepted automatically. `-PythonVersion` pins a
  major.minor version and `-Python` forces a specific interpreter.
- When an existing `.venv` is found, Windows setup now offers to remove and
  recreate it, so a one-off setup run cannot silently keep stale packages or a
  mismatched Python version.
- Python discovery also checks uv-managed installations, and the no-Python
  error suggests installing Python through uv as well as from python.org.
- The Windows and Linux setup documentation now covers automatic version
  discovery, portable `uv`-managed Python under `.python/`, and why CPython is
  not bundled in the repository.
- Documentation and user-facing messages now say "computer" for the host
  running the app, avoiding confusion with mesh devices (the bulbs).

## [0.1.0] — 2026-10-26

Initial public source release.

### Added

- PyQt6 desktop app and scriptable CLI for LEDVANCE SMART+ Bluetooth SIG-mesh
  lamps, using only the computer's built-in Bluetooth.
- Provisioning of factory-reset lamps, persistent mesh keys, and multiple bulbs.
- Power, brightness, colour temperature and colour control with ten presets.
- Versioned JSON export/import of a whole mesh or a single bulb, with merge,
  replace, validation and rollback.
- Per-computer controller unicast addresses so two PCs can share the same mesh
  one at a time without re-provisioning.
- Windows 10/11 setup script and launchers alongside the Linux setup script.
- Offline regression suites for configuration, controller identity and the
  temperature slider.
