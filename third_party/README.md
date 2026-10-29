# Third-party code

Mesh Bulbs is an application built on top of the MIT-licensed
[`btmesh`](https://github.com/dasimon135/ha-bluetooth-mesh) project by David
Simon. Two parts of that project are vendored here so the app works from a
plain clone with no extra packaging step.

| Vendored code | Location | Upstream | Licence |
|---|---|---|---|
| `btmesh` mesh stack, v0.11.0 | `src/btmesh/` | https://github.com/dasimon135/ha-bluetooth-mesh | MIT |
| Phase 0 provisioning harness | `phase0/` | same repository | MIT |

The upstream Python metadata and changelog are kept in this directory for
provenance and re-syncing:

- `pyproject.toml` — the upstream `btmesh` package metadata (reference only;
  its build paths assume the upstream repository layout).
- `CHANGELOG.md` — the upstream changelog, not this application's changelog.
- `LICENSE` — the upstream MIT licence text.

PyPI carries an older `btmesh` release (0.1.0 at the time of writing) than the
0.11.0 tree vendored here, so the app deliberately imports its local copy.
`meshcore.py` adds `src/` and `phase0/` to `sys.path` instead of installing the
library.

## Re-syncing

To update the vendored stack, copy `src/btmesh/` and the Phase 0 harness from a
known upstream commit, then run the offline suites:

```bash
python -m pytest -q
```

Record the upstream commit or release in this file whenever you re-sync, and
keep the upstream MIT notice intact.
