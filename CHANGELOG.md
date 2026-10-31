# Changelog

All notable changes to Mesh Bulbs are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The vendored `btmesh` library has its own changelog at
`third_party/btmesh/CHANGELOG.md`.

## [Unreleased]

### Fixed

- Control no longer goes silent after an unclean shutdown. If the saved SEQ
  cursor is behind what the bulbs have already accepted, every command is
  discarded as a replay, which looks like all bulbs becoming unreachable at
  once. A run marker next to the state file now detects that case and advances
  SEQ by 10,000 on the next start.
- **Forget** now removes the bulb from the collection immediately. It no
  longer waits for a stale BLE link to close first, which could leave the entry
  visible for a minute after the UI had already reported success.
- Adding a bulb no longer fails with a configuration timeout when the lamp is
  still rebooting after provisioning. The AppKey install and model binding are
  retried up to three times, and a final failure explains that the lamp is
  already on the mesh and how to retry it without a factory reset.
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
