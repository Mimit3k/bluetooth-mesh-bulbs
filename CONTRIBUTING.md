# Contributing

Thanks for taking a look. This is a small, hardware-oriented project; the most
useful contributions are reproducible bug reports and fixes that keep the
offline test suite green.

## Development setup

Linux:

```bash
git clone https://github.com/Mimit3k/bluetooth-mesh-bulbs.git
cd bluetooth-mesh-bulbs
./setup-linux.sh
.venv/bin/python -m pip install -r requirements-dev.txt
```

Windows: run `setup-windows.ps1`, then install the development requirements
with `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`.

## Checks before opening a pull request

```bash
.venv/bin/python -m ruff check .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

The suites are offline: they create temporary mesh states and never touch a
real lamp. On Windows omit the `QT_QPA_PLATFORM` prefix if a desktop session is
available, or set it to `offscreen` in PowerShell.

## Ground rules

- **Never commit mesh keys.** Do not add `state.json`, `*.connect`, exports,
  logs, or screenshots containing MAC addresses. `.gitignore` covers the usual
  names, but check `git status` before committing.
- Keep the vendored `src/btmesh/` and `phase0/` trees unchanged. If an upstream
  sync is required, do it as its own commit and update
  `third_party/README.md`.
- The SEQ cursor is safety-critical. Changes to state saving, import/export or
  controller addresses must include an offline regression test.
- Match the existing style: standard library first, no new runtime dependency
  unless it is clearly justified.
- Describe hardware you tested with in the pull request, and say explicitly if
  a change is untested on real hardware.

## Licensing

By contributing you agree that your contribution is licensed under the MIT
licence in `LICENSE`. Vendored components keep their upstream MIT notices; see
`THIRD_PARTY_NOTICES.md`, including the PyQt6 GPL/commercial note.
