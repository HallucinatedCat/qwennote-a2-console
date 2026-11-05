"""Command-line entry point.

Run ``python -m a2ble --help`` (see README.md for the full walkthrough).
Safety: this CLI only ever builds the commands enumerated in
:mod:`a2ble.commands`; nothing here can reset, reflash, format, reboot,
delete a file or change a vendor/gray-switch setting on the device.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import asyncio
import json
import logging
import sys

from .ble_transport import BleTransport, TransportError
from .pairing import PairingError, default_pairing_path, load_pairing
from .protocol import DeviceSession, ProtocolError


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True))


async def _cmd_scan(args) -> int:
    try:
        devices = await BleTransport.scan(timeout=args.timeout)
    except TransportError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # bleak backend errors (no adapter, BlueZ/dbus not running, etc.)
        print("error: Bluetooth scan failed: %s" % exc, file=sys.stderr)
        print("Check that Bluetooth is turned on and this machine has a usable adapter.",
              file=sys.stderr)
        return 2
    if not devices:
        print("no QwenNote A2 found (is it powered on and advertising?)")
        return 1
    for d in devices:
        name = getattr(d, "name", None) or "?"
        address = getattr(d, "address", None) or "?"
        print("%s  %s" % (address, name))
    return 0


async def _open_session(args) -> DeviceSession:
    pairing = load_pairing(args.pairing)
    transport = BleTransport()
    await transport.connect(args.address, timeout=args.timeout)
    session = DeviceSession(transport, pairing)
    await session.connect(send_auth_step=args.send_auth_step)
    return session


async def _run_one(args, action) -> int:
    session = None
    try:
        session = await _open_session(args)
        result = await action(session)
        _print(result)
        return 0
    except (TransportError, ProtocolError, PairingError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except Exception as exc:  # bleak backend errors (no adapter, dbus/WinRT issues, etc.)
        print("error: %s" % exc, file=sys.stderr)
        return 2
    finally:
        if session is not None:
            try:
                await session.transport.disconnect()
            except Exception:
                pass


def _cmd_connect(args):
    return _run_one(args, lambda s: s.sync_dev_status())


def _cmd_status(args):
    return _run_one(args, lambda s: s.sync_dev_status())


def _cmd_battery(args):
    return _run_one(args, lambda s: s.battery_percent())


def _cmd_firmware(args):
    return _run_one(args, lambda s: s.query_fw_version())


def _cmd_files(args):
    return _run_one(args, lambda s: s.get_file_list(args.start_fid, args.end_fid, args.recently))


def _cmd_record(args):
    action = {
        "start": lambda s: s.record_start(),
        "stop": lambda s: s.record_stop(),
        "pause": lambda s: s.record_pause(),
        "resume": lambda s: s.record_resume(),
        "status": lambda s: s.record_status(),
    }[args.action]
    return _run_one(args, action)


async def _cmd_analyze(args) -> int:
    from .btsnoop import analyze, format_report
    from .pairing import load_pairing
    pairing = load_pairing(args.pairing) if args.pairing else None
    try:
        report = analyze(Path(args.log).read_bytes(), pairing, show_values=args.show_values)
    except (OSError, ValueError) as error:
        print(f"Could not read the log: {error}")
        return 2
    print(format_report(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="a2ble", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    def add_device_args(sp):
        sp.add_argument("--address", required=True, help="BLE address/identifier from 'scan'")
        sp.add_argument("--pairing", default=str(default_pairing_path()),
                         help="path to the pairing JSON file (default: %(default)s)")
        sp.add_argument("--timeout", type=float, default=10.0)
        sp.add_argument("--send-auth-step", action="store_true",
                         help="also send AUTH (cmd 9) before CONNECT_DEVICE. Source "
                              "tracing found the official app only sends AUTH from its "
                              "unbind flow, not on a normal reconnect; this flag is "
                              "off by default and provided only for parity/testing.")

    sp = sub.add_parser("scan", help="find a QwenNote A2 advertising nearby")
    sp.add_argument("--timeout", type=float, default=6.0)
    sp.set_defaults(func=_cmd_scan)

    sp = sub.add_parser("connect", help="connect and authenticate, then print device status")
    add_device_args(sp)
    sp.set_defaults(func=_cmd_connect)

    sp = sub.add_parser("status", help="SYNC_DEV_STATUS")
    add_device_args(sp)
    sp.set_defaults(func=_cmd_status)

    sp = sub.add_parser("battery", help="SYS_CONTROL battery read")
    add_device_args(sp)
    sp.set_defaults(func=_cmd_battery)

    sp = sub.add_parser("firmware", help="QUERY_FW_VERSION")
    add_device_args(sp)
    sp.set_defaults(func=_cmd_firmware)

    sp = sub.add_parser("files", help="GET_FILE_LIST")
    add_device_args(sp)
    sp.add_argument("--start-fid", default="")
    sp.add_argument("--end-fid", default="")
    sp.add_argument("--recently", type=int, default=0)
    sp.set_defaults(func=_cmd_files)

    sp = sub.add_parser("record", help="AUDIO_RECORD_OPT start/stop/pause/resume/status")
    add_device_args(sp)
    sp.add_argument("action", choices=["start", "stop", "pause", "resume", "status"])
    sp.set_defaults(func=_cmd_record)

    sp = sub.add_parser("analyze", help="check framing and AES against an Android btsnoop_hci.log of the official app")
    sp.add_argument("log", help="btsnoop_hci.log captured while the official app connected to the A2")
    sp.add_argument("--pairing", help="pairing JSON; with it, checks the CONNECT_DEVICE token")
    sp.add_argument("--show-values", action="store_true", help="also print frame bodies (serials, tokens): keep the output private")
    sp.set_defaults(func=_cmd_analyze)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                         format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
