#!/usr/bin/env python3
"""bulbctl — drive Bluetooth SIG-mesh lamps (LEDVANCE SMART+) from a computer.

A thin command-line layer over :mod:`meshcore`, which also powers the desktop
GUI (``bulbgui``). Mesh crypto/provisioning comes from the vendored ``btmesh``
library and its phase0 harness. No vendor app, no cloud, no root.

Usage:
    ./bulbctl scan
    ./bulbctl provision --name Desk
    ./bulbctl devices
    ./bulbctl export --all mesh-backup.json
    ./bulbctl export --device Desk desk.json
    ./bulbctl import mesh-backup.json
    ./bulbctl controller-address
    ./bulbctl controller-address 0x7fff
    ./bulbctl --device Desk on
    ./bulbctl --device Desk level 40
    ./bulbctl --device Desk temp 2700
    ./bulbctl --device Desk colour ff8800
    ./bulbctl status
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import meshcore
from meshcore import Link, MeshCoreError, State, p0

MODEL_LIGHT_CTL_SERVER = 0x1303


# ------------------------------------------------------------------ commands


async def cmd_scan(args) -> int:
    state = State(args.state)
    result = await meshcore.scan(state, args.timeout)
    if result["unprovisioned"]:
        print("unprovisioned lamps (factory reset, ready to add):")
        for dev in result["unprovisioned"]:
            print(f"  {dev['address']}  uuid={dev['uuid']}  oob=0x{dev['oob_info']:04x}")
    else:
        print("unprovisioned lamps: none")
    if result["proxies"]:
        print("mesh proxies (provisioned lamps):")
        for proxy in result["proxies"]:
            mark = "  <- OURS" if proxy["ours"] else ""
            print(
                f"  {proxy['address']}  {proxy['identification']} "
                f"{proxy['network_id']}{mark}"
            )
    else:
        print("mesh proxies: none")
    return 0


async def cmd_provision(args) -> int:
    state = State(args.state)
    unicast = await meshcore.provision(
        state,
        uuid=args.device_uuid,
        name=args.name,
        static_oob=args.static_oob,
        log_fn=print,
    )
    print(f"\nadded {args.name!r} at 0x{unicast:04x}")
    return 0


async def cmd_configure(args) -> int:
    state = State(args.state)
    unicast = state.resolve(args.device)
    bound, comp = await meshcore.configure_with_retries(
        state, unicast, log_fn=print
    )
    state.device(unicast)["bound_models"] = bound
    state.save()
    print(f"\n{comp.describe()}")
    print("models bound: " + (", ".join(f"0x{m:04x}" for m in bound) or "none"))
    return 0


async def cmd_devices(args) -> int:
    state = State(args.state)
    if not state.devices:
        print("no bulbs yet — run: ./bulbctl provision")
        return 0
    width = max((len(e.get("name", "")) for e in state.devices.values()), default=4)
    print(f"{'name':<{width}}  unicast  address            models")
    for key, entry in state.devices.items():
        models = " ".join(f"0x{m:04x}" for m in entry.get("bound_models") or []) or "-"
        print(
            f"{entry.get('name', ''):<{width}}  0x{int(key, 16):04x}  "
            f"{entry.get('address', '?'):<17}  {models}"
        )
    return 0


async def cmd_rename(args) -> int:
    state = State(args.state)
    unicast = state.resolve(args.device)
    state.rename(unicast, args.name)
    print(f"0x{unicast:04x} is now {args.name!r}")
    return 0


async def cmd_export(args) -> int:
    state = State(args.state)
    if args.all and args.device:
        raise MeshCoreError("choose either --all or --device, not both")
    if args.all:
        summary = meshcore.export_config(state, args.path, kind="mesh")
    elif args.device:
        summary = meshcore.export_config(
            state,
            args.path,
            kind="bulb",
            unicast=state.resolve(args.device),
        )
    else:
        raise MeshCoreError("choose --all (whole mesh) or --device NAME|0xADDR")
    print(summary)
    print(
        "This file contains the mesh keys. Keep it private, and never run "
        "two controllers for the same mesh at the same time."
    )
    return 0


async def cmd_import(args) -> int:
    controller_address = (
        meshcore.parse_unicast(args.controller_address)
        if args.controller_address
        else None
    )
    state = State(args.state)
    package = meshcore.read_config(args.path)
    description = meshcore.describe_config(package)
    same = meshcore.same_mesh(state, package)
    existing = len(state.devices)

    if existing and not same and not args.replace:
        raise MeshCoreError(
            "the file belongs to a different mesh; importing it would replace "
            "this computer's mesh keys and bulbs (use --replace to allow that)"
        )

    if existing:
        if args.replace:
            copy_addr = (
                ""
                if controller_address is not None
                else " This also copies the file's controller address."
            )
            question = (
                f"Replace this computer's mesh ({existing} bulb(s)) with "
                f"{description}?{copy_addr}"
            )
        else:
            question = (
                f"Merge {description} into this mesh "
                f"({existing} existing bulb(s))?"
            )
        print(question)
        if not args.yes:
            try:
                answer = input("Continue? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            if answer not in ("y", "yes"):
                print("cancelled")
                return 1

    summary = meshcore.apply_config(
        state,
        package,
        replace=args.replace,
        controller_address=controller_address,
    )
    print(summary)
    print(
        "If this computer is replacing another controller, keep the other one "
        "closed: both must never use the same mesh keys at once."
    )
    return 0


async def cmd_controller_address(args) -> int:
    state = State(args.state)
    current = int(state.data.get("provisioner_addr", 0x0001))
    next_unicast = int(state.data.get("next_unicast", 0x0002))
    if args.address is None:
        print(f"controller address: 0x{current:04x}")
        print(f"next bulb address:  0x{next_unicast:04x}")
        print(
            "Give each computer its own free unicast address (for example "
            "0x7fff on the second one). Only one controller should run at a time."
        )
        return 0

    address = meshcore.parse_unicast(args.address)
    print(meshcore.set_controller_address(state, address))
    print(
        "The other computer keeps its address and SEQ cursor. Keep one "
        "controller closed while the other is running."
    )
    return 0


async def cmd_remove(args) -> int:
    state = State(args.state)
    unicast = state.resolve(args.device)
    entry = state.device(unicast)
    name = entry.get("name") or f"0x{unicast:04x}"
    if not args.yes:
        print(
            f"removing {name!r} forgets its device key. The lamp keeps working "
            "on this mesh, but you cannot control it again until it is "
            "factory-reset and re-added."
        )
        try:
            answer = input(f"Remove {name!r}? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("cancelled")
            return 1
    state.remove(unicast)
    print(f"removed {name!r}")
    return 0


async def cmd_on(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        await link.set_power(True)
    print("on")
    return 0


async def cmd_off(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        await link.set_power(False)
    print("off")
    return 0


async def cmd_toggle(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        st = await link.status()
        want = not st.get("on", False)
        await link.set_power(want)
    print("on" if want else "off")
    return 0


async def cmd_level(args) -> int:
    pct = max(0, min(100, args.percent))
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        await link.set_level(pct)
        st = await link.status()
    mn, mx = st["range"]
    print(f"level {pct}% -> {st.get('level')}/{mx} (range {mn}..{mx})")
    return 0


async def cmd_temp(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        if not link.has(MODEL_LIGHT_CTL_SERVER):
            raise MeshCoreError("lamp has no Light CTL model — no colour temperature")
        await link.set_temperature(args.kelvin)
    print(f"temperature: {args.kelvin}K")
    return 0


async def cmd_colour(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        if not link.has(0x1307):
            raise MeshCoreError("lamp has no Light HSL model — no colour control")
        hue, sat = await link.set_colour(args.hex)
    print(f"colour {args.hex.lstrip('#')}: hue={hue} saturation={sat}")
    return 0


async def cmd_status(args) -> int:
    state = State(args.state)
    async with Link(state, state.resolve(args.device), log_fn=_debug) as link:
        st = await link.status()
    name = state.device(st["unicast"]).get("name")
    print(f"bulb: {name} (0x{st['unicast']:04x})")
    if "level" in st:
        mn, mx = st["range"]
        print(f"lightness: {st['level']}/{mx} ({st['level_pct']}%)")
    print(f"onoff: {1 if st.get('on') else 0}")
    if st.get("temperature") is not None:
        print(f"temperature: {st['temperature']}K")
    return 0


async def cmd_dump(args) -> int:
    state = State(args.state)
    unicast = state.resolve(args.device)
    async with Link(state, unicast, log_fn=_debug) as link:
        comp = await link.composition()
    print(comp.describe())
    for index, element in enumerate(comp.elements):
        print(f"  element {index} @ 0x{unicast + index:04x}")
        for model_id in element.sig_models:
            name = meshcore.CONTROLLABLE_MODELS.get(model_id, "")
            print(f"    sig    0x{model_id:04x} {name}".rstrip())
        for model in getattr(element, "vendor_models", ()):
            print(f"    vendor {model}")
    return 0


# -------------------------------------------------------------------- parser


def _debug(message: str) -> None:
    logging.getLogger("bulbctl").debug(message)


def _add_device_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-d", "--device", metavar="NAME|0xADDR",
        help="bulb to control (default: the first in the collection)",
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bulbctl",
        description="Control Bluetooth SIG-mesh lamps from this computer.",
    )
    ap.add_argument("--state", default=str(meshcore.DEFAULT_STATE),
                    help="state file (default: %(default)s)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("-d", "--device", dest="device_global", metavar="NAME|0xADDR",
                    help="bulb to control (also accepted after the command)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="listen for unprovisioned lamps and mesh proxies")
    s.add_argument("--timeout", type=float, default=10.0)
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("provision", help="add a factory-reset lamp to the collection")
    s.add_argument("--name", default="New bulb", help="name for this bulb")
    s.add_argument("--device-uuid", help="target UUID if several beacons answer")
    s.add_argument("--static-oob", metavar="HEX",
                   help="static OOB value if the lamp demands one")
    s.set_defaults(fn=cmd_provision)

    s = sub.add_parser("devices", help="list the bulb collection")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser(
        "export", help="export mesh keys and bulbs to a JSON file"
    )
    s.add_argument("path", help="destination file, e.g. mesh-backup.json")
    s.add_argument(
        "--all", action="store_true", help="export the whole mesh (recommended)"
    )
    _add_device_arg(s)
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser(
        "import", help="import mesh keys and bulbs from a JSON file"
    )
    s.add_argument("path", help="file created by 'export'")
    s.add_argument(
        "--replace",
        action="store_true",
        help="replace this computer's mesh instead of merging into it",
    )
    s.add_argument(
        "--controller-address",
        metavar="0xADDR",
        help="give this computer its own controller address after importing",
    )
    s.add_argument(
        "-y", "--yes", action="store_true", help="do not ask for confirmation"
    )
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser(
        "controller-address",
        help="show or change this computer's mesh unicast address",
    )
    s.add_argument(
        "address",
        nargs="?",
        metavar="0xADDR",
        help="free unicast address for this computer, e.g. 0x7fff",
    )
    s.set_defaults(fn=cmd_controller_address)

    s = sub.add_parser("configure", help="(re)install AppKey and bind models")
    _add_device_arg(s)
    s.set_defaults(fn=cmd_configure)

    s = sub.add_parser("rename", help="rename a bulb")
    s.add_argument("name", help="new name")
    _add_device_arg(s)
    s.set_defaults(fn=cmd_rename)

    s = sub.add_parser("remove", help="forget a bulb (keeps its place in the mesh)")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask")
    _add_device_arg(s)
    s.set_defaults(fn=cmd_remove)

    for name, fn, help_text in (
        ("on", cmd_on, "switch on"),
        ("off", cmd_off, "switch off"),
        ("toggle", cmd_toggle, "flip current state"),
        ("status", cmd_status, "read back state"),
        ("dump", cmd_dump, "print the lamp's composition data"),
    ):
        s = sub.add_parser(name, help=help_text)
        _add_device_arg(s)
        s.set_defaults(fn=fn)

    s = sub.add_parser("level", help="brightness in percent")
    s.add_argument("percent", type=int)
    _add_device_arg(s)
    s.set_defaults(fn=cmd_level)

    s = sub.add_parser("temp", help="colour temperature in Kelvin")
    s.add_argument("kelvin", type=int)
    _add_device_arg(s)
    s.set_defaults(fn=cmd_temp)

    s = sub.add_parser("colour", aliases=["color"], help="set RGB colour, e.g. ff8800")
    s.add_argument("hex")
    _add_device_arg(s)
    s.set_defaults(fn=cmd_colour)
    return ap


async def async_main(args) -> int:
    if getattr(args, "device_global", None) and not getattr(args, "device", None):
        args.device = args.device_global
    return await args.fn(args)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname).1s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(async_main(args))
    except MeshCoreError as exc:
        print(f"error: {exc}")
        return 2
    except p0.Phase0Failure as exc:
        print()
        print(f"{exc.phase} failed: {exc}")
        for cause in exc.causes:
            print(f"  - {cause}")
        return 2
    except TimeoutError:
        print("the lamp did not answer — is it powered and in range?")
        return 2
    except KeyboardInterrupt:
        print("\ncancelled")
        return 130
    except Exception as exc:  # bleak's not-available error lives in another module
        if type(exc).__name__ == "BleakBluetoothNotAvailableError":
            print("Bluetooth is off or blocked. Run:  sudo rfkill unblock bluetooth")
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
