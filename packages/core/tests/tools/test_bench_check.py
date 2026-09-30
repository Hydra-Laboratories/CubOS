"""Offline-path tests for the bench-test CLI.

Every test runs with --offline; a fixture asserts no real serial port is
ever opened, so these tests never touch hardware.
"""

from __future__ import annotations

import re

import pytest
import serial

from cubos.tools.bench_check import main


@pytest.fixture(autouse=True)
def _forbid_real_serial(monkeypatch):
    def _guard(*args, **kwargs):
        raise AssertionError("Offline bench-check tests must never open a real serial port")

    monkeypatch.setattr(serial.Serial, "__init__", _guard)


def _run(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc_info:
        main(argv)
    return exc_info.value.code


def test_lights_cycle_offline_exits_zero(capsys):
    rc = _run(["lights", "--offline", "--hold", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[lights] connected" in out
    assert "[lights] disconnected" in out
    assert "all_off" in out


def test_lights_set_offline_exits_zero(capsys):
    rc = _run(["lights", "--offline", "set", "white", "50"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "set white -> 50%" in out


def test_lights_set_invalid_action_exits_nonzero():
    rc = _run(["lights", "--offline", "set", "white"])
    assert rc == 1


def test_lights_set_invalid_percentage_exits_nonzero():
    rc = _run(["lights", "--offline", "set", "white", "not-a-number"])
    assert rc == 1


@pytest.mark.parametrize("vendor", ["flir", "opencv"])
def test_camera_offline_exits_zero(tmp_path, vendor):
    out_path = tmp_path / "capture.png"
    rc = _run(["camera", "--vendor", vendor, "--offline", "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_pipette_offline_exits_zero(capsys):
    rc = _run(["pipette", "--offline", "--aspirate", "20", "--dispense", "20"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aspirate(20.0)" in out
    assert "dispense(20.0)" in out


def test_pipette_offline_without_volumes_exits_zero():
    rc = _run(["pipette", "--offline"])
    assert rc == 0


def test_pstat_offline_exits_zero(capsys):
    rc = _run(["pstat", "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "healthy=True" in out


def test_pstat_online_without_port_fails_cleanly():
    rc = _run(["pstat"])
    assert rc == 1


def test_pstat_emstat_offline_exits_zero(capsys):
    rc = _run(["pstat", "--vendor", "emstat", "--offline"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "vendor=emstat" in out
    assert "healthy=True" in out


def test_pstat_emstat_online_without_port_fails_cleanly():
    rc = _run(["pstat", "--vendor", "emstat"])
    assert rc == 1


def test_pstat_offline_ocp_prints_trace(capsys):
    rc = _run(["pstat", "--vendor", "emstat", "--offline", "--ocp", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "running OCP for 1.0s" in out
    assert "OCP samples: 10" in out
    assert "final voltage:" in out


def test_pstat_ocp_failure_exits_nonzero(monkeypatch, capsys):
    from cubos.instruments.potentiostat.exceptions import PotentiostatCommandError
    from cubos.instruments.potentiostat.vendors import emstat as emstat_module

    def _broken_ocp(self, params):
        raise PotentiostatCommandError("simulated OCP failure")

    monkeypatch.setattr(emstat_module.EmstatPotentiostat, "run_OCP", _broken_ocp)
    rc = _run(["pstat", "--vendor", "emstat", "--offline", "--ocp", "1"])
    assert rc == 1
    assert "simulated OCP failure" in capsys.readouterr().out


def test_all_offline_exits_zero_with_pass_summary(tmp_path, capsys):
    out_path = tmp_path / "capture.png"
    rc = _run(["all", "--offline", "--hold", "0", "--out", str(out_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert re.search(r"lights\s+PASS", out)
    assert re.search(r"camera\s+PASS", out)
    assert re.search(r"pipette\s+PASS", out)
    assert re.search(r"pstat\s+PASS", out)


def test_all_offline_reports_failure_for_broken_instrument(monkeypatch, tmp_path, capsys):
    from cubos.instruments.potentiostat.exceptions import PotentiostatConnectionError
    from cubos.instruments.potentiostat.vendors import admiral as admiral_module

    def _broken_connect(self):
        raise PotentiostatConnectionError("simulated bench failure")

    monkeypatch.setattr(admiral_module.AdmiralPotentiostat, "connect", _broken_connect)

    out_path = tmp_path / "capture.png"
    rc = _run(["all", "--offline", "--hold", "0", "--out", str(out_path)])
    assert rc == 1

    out = capsys.readouterr().out
    assert re.search(r"lights\s+PASS", out)
    assert re.search(r"camera\s+PASS", out)
    assert re.search(r"pipette\s+PASS", out)
    assert re.search(r"pstat\s+FAIL", out)
    assert "simulated bench failure" in out
