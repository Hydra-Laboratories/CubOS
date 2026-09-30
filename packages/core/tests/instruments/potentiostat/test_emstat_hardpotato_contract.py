"""Exercise the pinned hardpotato CV script generator without serial I/O.

Install the potentiostat-emstat extra to run this optional dependency contract
test. Setup and transport are bypassed; the real CV constructor generates the
script. This does not validate the dependency's device connection support.
"""

from types import SimpleNamespace

import pytest

from cubos.instruments.potentiostat.models import CVParams
from cubos.instruments.potentiostat.vendors.emstat import EmstatPotentiostat


@pytest.mark.parametrize("cycles", [1, 2, 3])
def test_real_hardpotato_script_has_requested_cycle_count(monkeypatch, tmp_path, cycles):
    hp = pytest.importorskip("hardpotato")
    captured = []

    def fake_transport(experiment):
        captured.append(experiment.text)
        experiment.data = [[[
            SimpleNamespace(value=0.0),
            SimpleNamespace(value=-0.2),
            SimpleNamespace(value=1e-6),
        ]]]

    # Select the shared EmStat script generator directly. The pinned package's
    # unpatched Setup has separate device-model/constructor issues; this test
    # deliberately covers CV generation, not connection or model detection.
    monkeypatch.setattr(hp.potentiostat, "model_pstat", "emstatpico")
    monkeypatch.setattr(hp.potentiostat, "folder_save", str(tmp_path))
    monkeypatch.setattr(hp.potentiostat.Technique, "run", fake_transport)
    driver = EmstatPotentiostat(port="unused-offline-test", data_dir=str(tmp_path))
    driver._hp = hp
    driver._folder = str(tmp_path)
    result = driver.run_CV(CVParams(
        start_V=-0.2, vertex1_V=0.2, vertex2_V=-0.2, end_V=-0.2,
        scan_rate_V_per_s=0.1, sampling_interval_s=0.01, cycles=cycles,
    ))
    driver.disconnect()

    assert len(captured) == 1
    assert f"meas_loop_cv p c -200m 200m -200m 1m 100m nscans({cycles})" in captured[0]
    assert result.is_valid
    assert result.cycles == cycles
    assert result.current_a == (1e-6,)
