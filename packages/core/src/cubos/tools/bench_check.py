"""Manual bench-test CLI for CubOS instrument drivers.

Instantiates the existing vendor drivers directly (no protocol engine, no
gantry) so an operator can exercise real driver logic and wiring on the
bench: lights, camera, pipette, potentiostat. Every path releases its
port(s) in ``finally``.

Usage:
    python -m cubos.tools.bench_check lights --port /dev/ttyACM0
    python -m cubos.tools.bench_check lights set white 50 --port /dev/ttyACM0
    python -m cubos.tools.bench_check camera --vendor flir --out /tmp/bench_capture.png
    python -m cubos.tools.bench_check pipette --aspirate 50 --dispense 50
    python -m cubos.tools.bench_check pstat --port /dev/ttyUSB0
    python -m cubos.tools.bench_check pstat --vendor emstat --port /dev/ttyACM1 --ocp 5
    python -m cubos.tools.bench_check all --offline

Known firmware quirk: the Pawduino emits a late "OK:Ready" boot banner
after connect that can skew response pairing by one command. This script
hedges against it (see ``_resync_pawduino_link``) but does not touch the
shared link itself.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from cubos.instruments.camera.exceptions import CameraError  # noqa: E402
from cubos.instruments.camera.vendors.flir import FlirCamera  # noqa: E402
from cubos.instruments.camera.vendors.opencv import OpenCVCamera  # noqa: E402
from cubos.instruments.controllers.pawduino import (  # noqa: E402
    PawduinoLink,
    PawduinoLinkError,
)
from cubos.instruments.lighting.exceptions import LightingError  # noqa: E402
from cubos.instruments.lighting.vendors.pawduino import PawduinoLighting  # noqa: E402
from cubos.instruments.pipette.exceptions import PipetteError  # noqa: E402
from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette  # noqa: E402
from cubos.instruments.potentiostat.exceptions import PotentiostatError  # noqa: E402
from cubos.instruments.potentiostat.models import OCPParams  # noqa: E402
from cubos.instruments.potentiostat.vendors.admiral import AdmiralPotentiostat  # noqa: E402
from cubos.instruments.potentiostat.vendors.emstat import EmstatPotentiostat  # noqa: E402

DEFAULT_PAWDUINO_PORT = "/dev/ttyACM0"
DEFAULT_CAPTURE_PATH = "/tmp/bench_capture.png"
_HELLO_CMD = 0
_HELLO_TIMEOUT = 5.0
SEPARATOR = "-" * 60


def _resync_pawduino_link(link: PawduinoLink | None, label: str) -> None:
    """Best-effort hedge against the late boot-banner response skew.

    Sends the hello command (id 0) and waits for its ``Hello`` reply,
    discarding anything stale ahead of it. Never raises; a failed hedge
    just means the first real command may absorb the skew instead.
    """
    if link is None:
        return
    try:
        response = link.send_command(_HELLO_CMD, expect="Hello", timeout=_HELLO_TIMEOUT)
        print(f"  [{label}] hello resync: {response}")
    except PawduinoLinkError as exc:
        print(f"  [{label}] hello resync skipped: {exc}")


def run_lights(
    *, port: str, offline: bool, hold: float, set_channel: tuple[str, int] | None,
) -> bool:
    lights = PawduinoLighting(port=port, offline=offline)
    print(f"[lights] connecting (port={port}, offline={offline})")
    success = True
    try:
        lights.connect()
        print("[lights] connected")
        if not offline:
            _resync_pawduino_link(getattr(lights, "_link", None), "lights")
        if set_channel is not None:
            channel, pct = set_channel
            print(f"[lights] set {channel} -> {pct}%")
            lights.set_channel(channel, pct)
            print(f"[lights] status: {lights.status()}")
        else:
            for channel, levels in lights.channels.items():
                for level in levels:
                    print(f"[lights] {channel} -> {level}%")
                    lights.set_channel(channel, level)
                    print(f"[lights] status: {lights.status()}")
                    time.sleep(hold)
    except LightingError as exc:
        print(f"[lights] FAILED: {exc}")
        success = False
    finally:
        try:
            lights.all_off()
            print(f"[lights] all_off -> status: {lights.status()}")
        except LightingError as exc:
            print(f"[lights] cleanup all_off failed: {exc}")
            success = False
        try:
            lights.disconnect()
            print("[lights] disconnected")
        except LightingError as exc:
            print(f"[lights] disconnect failed: {exc}")
            success = False
    return success


def run_camera(*, vendor: str, offline: bool, out: str, camera_id: int | None) -> bool:
    print(f"[camera] connecting (vendor={vendor}, offline={offline})")
    camera_kwargs = {"offline": offline}
    if camera_id is not None:
        camera_kwargs["camera_id"] = camera_id
    camera = FlirCamera(**camera_kwargs) if vendor == "flir" else OpenCVCamera(**camera_kwargs)
    success = True
    try:
        camera.connect()
        print("[camera] connected")
        saved_path = camera.capture(save_path=out)
        size_bytes = Path(saved_path).stat().st_size
        print(f"[camera] captured {saved_path} ({size_bytes} bytes)")
    except ImportError as exc:
        print(f"[camera] FAILED: vendor SDK not importable: {exc}")
        success = False
    except CameraError as exc:
        print(f"[camera] FAILED: {exc}")
        success = False
    finally:
        try:
            camera.disconnect()
            print("[camera] disconnected")
        except CameraError as exc:
            print(f"[camera] disconnect failed: {exc}")
            success = False
    return success


def run_pipette(
    *, port: str, offline: bool, aspirate: float | None, dispense: float | None,
) -> bool:
    pipette = OpentronsPipette(port=port, offline=offline)
    print(f"[pipette] connecting (port={port}, offline={offline})")
    success = True
    try:
        pipette.connect()
        print("[pipette] connected")
        if not offline:
            _resync_pawduino_link(getattr(pipette, "_link", None), "pipette")
        print(f"[pipette] status: {pipette.get_status()}")
        if aspirate is not None:
            result = pipette.aspirate(aspirate)
            print(f"[pipette] aspirate({aspirate}) -> {result}")
        if dispense is not None:
            result = pipette.dispense(dispense)
            print(f"[pipette] dispense({dispense}) -> {result}")
        print(f"[pipette] status: {pipette.get_status()}")
    except PipetteError as exc:
        print(f"[pipette] FAILED: {exc}")
        success = False
    finally:
        try:
            pipette.disconnect()
            print("[pipette] disconnected")
        except PipetteError as exc:
            print(f"[pipette] disconnect failed: {exc}")
            success = False
    return success


def run_pstat(
    *, port: str, channel: int, offline: bool,
    vendor: str = "admiral", ocp: float | None = None,
) -> bool:
    if not offline and not port:
        print("[pstat] FAILED: --port is required for a hardware run")
        return False
    if vendor == "emstat":
        pstat: AdmiralPotentiostat | EmstatPotentiostat = EmstatPotentiostat(
            port=port, offline=offline,
        )
    else:
        pstat = AdmiralPotentiostat(port=port, channel=channel, offline=offline)
    print(
        f"[pstat] connecting (vendor={vendor}, port={port or '<unset>'}, "
        f"channel={channel}, offline={offline})"
    )
    success = True
    try:
        pstat.connect()
        print("[pstat] connected")
        healthy = pstat.health_check()
        print(
            f"[pstat] status: port={port!r} channel={channel} healthy={healthy}"
        )
        if not healthy:
            print("[pstat] FAILED: health_check reported unhealthy")
            success = False
        elif ocp is not None:
            print(f"[pstat] running OCP for {ocp}s")
            result = pstat.run_OCP(OCPParams(duration_s=ocp))
            print(f"[pstat] OCP samples: {len(result.time_s)}")
            print(f"  {'t/s':>10}  {'E/V':>12}")
            for t, e in zip(result.time_s, result.voltage_v):
                print(f"  {t:>10.3f}  {e:>12.6f}")
            print(f"[pstat] final voltage: {result.final_voltage_v} V")
    except PotentiostatError as exc:
        print(f"[pstat] FAILED: {exc}")
        success = False
    finally:
        try:
            pstat.disconnect()
            print("[pstat] disconnected")
        except PotentiostatError as exc:
            print(f"[pstat] disconnect failed: {exc}")
            success = False
    return success


def cmd_lights(args: argparse.Namespace) -> bool:
    if not args.action:
        set_channel = None
    elif len(args.action) == 3 and args.action[0] == "set":
        try:
            set_channel = (args.action[1], int(args.action[2]))
        except ValueError:
            print(f"[lights] invalid percentage {args.action[2]!r}")
            return False
    else:
        print(f"[lights] invalid action {args.action!r}; expected 'set <channel> <pct>'")
        return False
    return run_lights(
        port=args.port, offline=args.offline, hold=args.hold, set_channel=set_channel,
    )


def cmd_camera(args: argparse.Namespace) -> bool:
    return run_camera(
        vendor=args.vendor, offline=args.offline, out=args.out, camera_id=args.camera_id,
    )


def cmd_pipette(args: argparse.Namespace) -> bool:
    return run_pipette(
        port=args.port, offline=args.offline,
        aspirate=args.aspirate, dispense=args.dispense,
    )


def cmd_pstat(args: argparse.Namespace) -> bool:
    return run_pstat(
        port=args.port, channel=args.channel, offline=args.offline,
        vendor=args.vendor, ocp=args.ocp,
    )


def cmd_all(args: argparse.Namespace) -> bool:
    print(SEPARATOR)
    print("Bench check: lights, camera, pipette, pstat")
    print(SEPARATOR)

    results: list[tuple[str, bool]] = []

    print("\n[lights]")
    results.append((
        "lights",
        run_lights(port=args.port, offline=args.offline, hold=args.hold, set_channel=None),
    ))

    print("\n[camera]")
    results.append((
        "camera",
        run_camera(vendor=args.vendor, offline=args.offline, out=args.out, camera_id=args.camera_id),
    ))

    print("\n[pipette]")
    results.append((
        "pipette",
        run_pipette(port=args.port, offline=args.offline, aspirate=None, dispense=None),
    ))

    print("\n[pstat]")
    results.append((
        "pstat",
        run_pstat(
            port=args.pstat_port, channel=args.pstat_channel,
            offline=args.offline, vendor=args.pstat_vendor,
        ),
    ))

    print()
    print(SEPARATOR)
    print("Summary")
    print(SEPARATOR)
    overall = True
    for instrument, passed in results:
        print(f"  {instrument:<10} {'PASS' if passed else 'FAIL'}")
        overall = overall and passed
    print(SEPARATOR)
    return overall


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manually exercise CubOS instrument drivers on the bench.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    lights_parser = subparsers.add_parser(
        "lights", help="Cycle Pawduino lighting channels, or set one channel/level",
    )
    lights_parser.add_argument("--port", default=DEFAULT_PAWDUINO_PORT)
    lights_parser.add_argument("--offline", action="store_true")
    lights_parser.add_argument(
        "--hold", type=float, default=2.0, help="Seconds to hold each level (cycle mode)",
    )
    lights_parser.add_argument(
        "action", nargs="*",
        help="Optional 'set <channel> <pct>' to set one level instead of cycling",
    )
    lights_parser.set_defaults(func=cmd_lights)

    camera_parser = subparsers.add_parser("camera", help="Capture one image")
    camera_parser.add_argument("--vendor", choices=["flir", "opencv"], default="flir")
    camera_parser.add_argument("--out", default=DEFAULT_CAPTURE_PATH)
    camera_parser.add_argument("--offline", action="store_true")
    camera_parser.add_argument("--camera-id", type=int, default=None)
    camera_parser.set_defaults(func=cmd_camera)

    pipette_parser = subparsers.add_parser(
        "pipette", help="Connect, print status, optionally aspirate/dispense",
    )
    pipette_parser.add_argument("--port", default=DEFAULT_PAWDUINO_PORT)
    pipette_parser.add_argument("--offline", action="store_true")
    pipette_parser.add_argument("--aspirate", type=float, default=None, help="Microliters")
    pipette_parser.add_argument("--dispense", type=float, default=None, help="Microliters")
    pipette_parser.set_defaults(func=cmd_pipette)

    pstat_parser = subparsers.add_parser("pstat", help="Connect and health-check the potentiostat")
    pstat_parser.add_argument("--vendor", choices=["admiral", "emstat"], default="admiral")
    pstat_parser.add_argument("--port", default="")
    pstat_parser.add_argument("--channel", type=int, default=0, help="Admiral only")
    pstat_parser.add_argument("--offline", action="store_true")
    pstat_parser.add_argument(
        "--ocp", type=float, default=None, metavar="SECONDS",
        help="After the health check, run an OCP of this duration and print the trace",
    )
    pstat_parser.set_defaults(func=cmd_pstat)

    all_parser = subparsers.add_parser(
        "all", help="Run lights, camera, pipette, pstat in sequence",
    )
    all_parser.add_argument(
        "--port", default=DEFAULT_PAWDUINO_PORT, help="Shared Pawduino port (lights + pipette)",
    )
    all_parser.add_argument("--pstat-vendor", choices=["admiral", "emstat"], default="admiral")
    all_parser.add_argument("--pstat-port", default="")
    all_parser.add_argument("--pstat-channel", type=int, default=0)
    all_parser.add_argument("--offline", action="store_true")
    all_parser.add_argument("--hold", type=float, default=2.0)
    all_parser.add_argument("--vendor", choices=["flir", "opencv"], default="flir")
    all_parser.add_argument("--out", default=DEFAULT_CAPTURE_PATH)
    all_parser.add_argument("--camera-id", type=int, default=None)
    all_parser.set_defaults(func=cmd_all)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    success = args.func(args)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
