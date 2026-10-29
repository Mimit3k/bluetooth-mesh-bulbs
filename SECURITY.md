# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's **Security →
Report a vulnerability** feature for this repository. Do not open a public
issue for a vulnerability, and do not include mesh keys or state files.

If private reporting is unavailable, contact the maintainer listed in the
repository profile.

## What counts as sensitive

`state.json` is the family secret of your mesh. It contains:

- the network key (NetKey) and application key (AppKey);
- a device key for every provisioned lamp;
- unicast addresses and the replay-sequence cursor.

Anyone with this file can control every lamp on the mesh. Treat it like a
password file, not like a configuration preference.

Exported files (`bulbctl export`, GUI **Export…**) contain the same secrets.
They are written with private permissions where the platform supports it, but
they remain sensitive after copying.

## Never post publicly

- `state.json`, `*.connect`, or any export file;
- full debug logs that include frame hex dumps or keys;
- screenshots showing MAC addresses, keys, or the state path.

If you accidentally publish a key file, rotate the mesh: there is no
revocation mechanism for a leaked NetKey. A factory reset and re-provisioning
of every lamp is the reliable recovery.

## Operational safety

- Run **one controller at a time**. The file lock is per machine and cannot see
  another computer sharing the same keys.
- Back up `state.json` before moving machines, and delete test exports when
  finished.
- Keep the Python environment and dependencies up to date; `bleak`,
  `cryptography` and PyQt6 are ordinary third-party dependencies.

## Supported versions

Security fixes are applied to the current release on the default branch. The
project is a local desktop tool and does not run a network service.
