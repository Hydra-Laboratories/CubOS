"""Tests for setup_protocol: load all configs, validate, return ready-to-run protocol."""

from __future__ import annotations

import importlib
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cubos.data.data_store import DataStore
from cubos.deck.deck import Deck
from cubos.deck.labware.labware import Coordinate3D
from cubos.deck.labware.well_plate import WellPlate
from cubos.deck.errors import DeckLoaderError
from cubos.gantry.errors import GantryLoaderError
from cubos.gantry.gantry import Gantry
from cubos.gantry.gantry_config import GantryConfig
from cubos.gantry.instrument_mount import InstrumentedGantry
from cubos.protocol_engine.builder import ProtocolBuilder
from cubos.protocol_engine.errors import ProtocolLoaderError
from cubos.protocol_engine.protocol import Protocol
from cubos.protocol_engine.runtime import ProtocolContext
from cubos.protocol_engine.registry import CommandRegistry
from cubos.protocol_engine.commands.scan import scan
from cubos.protocol_engine.setup import run_on_hardware, setup_protocol
from cubos.validation.errors import SetupValidationError


@pytest.fixture(autouse=True)
def _ensure_commands_registered():
    """Ensure protocol commands are registered (may be cleared by other test fixtures)."""
    command_names = CommandRegistry.instance().command_names
    if not command_names:
        import cubos.protocol_engine.commands.measure
        import cubos.protocol_engine.commands.move
        import cubos.protocol_engine.commands.pipette
        import cubos.protocol_engine.commands.scan
        importlib.reload(cubos.protocol_engine.commands.measure)
        importlib.reload(cubos.protocol_engine.commands.move)
        importlib.reload(cubos.protocol_engine.commands.pipette)
        importlib.reload(cubos.protocol_engine.commands.scan)
    elif "measure" not in command_names:
        import cubos.protocol_engine.commands.measure
        importlib.reload(cubos.protocol_engine.commands.measure)


# ── YAML templates ──────────────────────────────────────────────────────

GANTRY_YAML = """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
working_volume:
  x_min: 0.0
  x_max: 300.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    offset_x: 5.0
    offset_y: 0.0
    depth: 0.0
"""

DECK_YAML = """\
labware:
  vial_1:
    type: vial
    name: test_vial
    model_name: standard_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 30.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""

TWO_VIAL_DECK_YAML = DECK_YAML + """\
  vial_2:
    type: vial
    name: destination_vial
    model_name: standard_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 80.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""

PROTOCOL_YAML = """\
protocol:
  - move:
      instrument: pipette
      position: vial_1
"""

UVVIS_GANTRY_YAML = """\
serial_port: /dev/ttyUSB0
gantry_type: cub_xl
cnc:
  factory_z_travel_mm: 90.0
working_volume:
  x_min: 0.0
  x_max: 300.0
  y_min: 0.0
  y_max: 200.0
  z_min: 0.0
  z_max: 80.0
instruments:
  uvvis:
    type: uvvis_ccs
    vendor: thorlabs
    serial_number: TEST123
    dll_path: fake.dll
    offset_x: 0.0
    offset_y: 0.0
    depth: 0.0
"""

PLATE_DECK_YAML = """\
labware:
  plate_1:
    type: well_plate
    name: test_96_well
    model_name: test_96_well
    rows: 1
    columns: 2
    length: 50.0
    width: 30.0
    height: 14.0
    calibration:
      a1:
        x: 100.0
        y: 100.0
        z: 15.0
      a2:
        x: 109.0
        y: 100.0
        z: 15.0
    x_offset: 9.0
    y_offset: 9.0
    capacity_ul: 200.0
    working_volume_ul: 150.0
"""

UVVIS_MEASURE_PROTOCOL_YAML = """\
protocol:
  - measure:
      instrument: uvvis
      position: plate_1.A1
      measurement_height: 0.0
      method: measure
"""


def _gantry_with_instruments(instruments_yaml: str) -> str:
    return GANTRY_YAML.split("instruments:\n", 1)[0] + instruments_yaml


def _write_temp_yaml(content: str) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    f.write(content)
    f.close()
    return f.name


class _TempYamlFiles:
    """Context manager that creates temp YAML files and cleans them up."""

    def __init__(
        self,
        gantry: str = GANTRY_YAML,
        deck: str = DECK_YAML,
        protocol: str = PROTOCOL_YAML,
    ):
        self._gantry = gantry
        self._deck = deck
        self._protocol = protocol
        self.paths: list[str] = []

    def __enter__(self):
        self.gantry_path = _write_temp_yaml(self._gantry)
        self.deck_path = _write_temp_yaml(self._deck)
        self.protocol_path = _write_temp_yaml(self._protocol)
        self.paths = [self.gantry_path, self.deck_path, self.protocol_path]
        return self

    def __exit__(self, *args):
        for p in self.paths:
            if os.path.exists(p):
                os.unlink(p)


# ── Tests ────────────────────────────────────────────────────────────────


class TestSetupProtocol:

    def test_setup_returns_protocol_and_context(self):
        with _TempYamlFiles() as f:
            protocol, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert isinstance(protocol, Protocol)
            assert isinstance(context, ProtocolContext)

    def test_context_has_gantry_with_instruments(self):
        with _TempYamlFiles() as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert "pipette" in context.gantry.instruments

    def test_context_has_deck_with_labware(self):
        with _TempYamlFiles() as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert "vial_1" in context.deck

    def test_setup_accepts_nested_holder_yaml_positions(self):
        deck_yaml = """\
labware:
  vial_holder:
    type: vial_holder
    name: panda_vial_holder
    location:
      x: 17.1
      y: 132.9
      z: 20.0
    vials:
      vial_1:
        model_name: 20ml_vial
        height: 57.0
        diameter: 28.0
        location:
          x: 17.1
          y: 0.9
        capacity_ul: 20000.0
        working_volume_ul: 12000.0
"""
        protocol_yaml = """\
protocol:
  - move:
      instrument: pipette
      position: vial_holder.vial_1
"""
        with _TempYamlFiles(deck=deck_yaml, protocol=protocol_yaml) as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )

            assert "vial_holder" in context.deck

    def test_context_has_gantry_config(self):
        with _TempYamlFiles() as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert isinstance(context.gantry_config, GantryConfig)
            assert context.gantry_config.working_volume.x_min == 0.0

    def test_context_carries_optional_data_and_fluid_state_ids(self):
        data_store = object()
        with _TempYamlFiles() as f:
            _, context = setup_protocol(
                f.gantry_path,
                f.deck_path,
                f.protocol_path,
                data_store=data_store,
                campaign_id=42,
                fluid_state_id=73,
            )
            assert context.data_store is data_store
            assert context.campaign_id == 42
            assert context.fluid_state_id == 73

    def test_rejects_negative_space_gantry_config(self):
        legacy_gantry = GANTRY_YAML.replace("  x_min: 0.0\n", "  x_min: -300.0\n")
        with _TempYamlFiles(gantry=legacy_gantry) as f:
            with pytest.raises(ValueError, match="Deck-origin calibration requires"):
                setup_protocol(
                    f.gantry_path, f.deck_path, f.protocol_path,
                )

    def test_protocol_has_expected_steps(self):
        with _TempYamlFiles() as f:
            protocol, _ = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert len(protocol) == 1
            assert protocol.steps[0].command_name == "move"

    def test_setup_accepts_built_protocol_object(self):
        built_protocol = (
            ProtocolBuilder()
            .add_move(instrument="pipette", position="vial_1")
            .build()
        )
        with _TempYamlFiles() as f:
            protocol, context = setup_protocol(
                f.gantry_path, f.deck_path, built_protocol,
            )

            assert protocol is built_protocol
            assert isinstance(context, ProtocolContext)

    def test_setup_protocol_object_runs_bounds_validation(self):
        near_edge_deck = """\
labware:
  vial_1:
    type: vial
    name: test_vial
    model_name: standard_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 2.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
        built_protocol = (
            ProtocolBuilder()
            .add_move(instrument="pipette", position="vial_1")
            .build()
        )
        with _TempYamlFiles(deck=near_edge_deck) as f:
            with pytest.raises(SetupValidationError):
                setup_protocol(f.gantry_path, f.deck_path, built_protocol)

    def test_setup_rejects_invalid_protocol_argument_type(self):
        with _TempYamlFiles() as f:
            with pytest.raises(TypeError, match="YAML path or protocol_engine.Protocol"):
                setup_protocol(f.gantry_path, f.deck_path, object())

    def test_raises_on_deck_position_out_of_bounds(self):
        out_of_bounds_deck = """\
labware:
  vial_1:
    type: vial
    name: test_vial
    model_name: standard_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 306.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
        with _TempYamlFiles(deck=out_of_bounds_deck) as f:
            with pytest.raises(SetupValidationError) as exc_info:
                setup_protocol(
                    f.gantry_path, f.deck_path, f.protocol_path,
                )
            assert len(exc_info.value.violations) >= 1

    def test_raises_on_gantry_position_out_of_bounds(self):
        # vial at x=2.0, pipette offset_x=5.0 -> gantry_x = 2.0 - 5.0 = -3.0 < x_min=0
        near_edge_deck = """\
labware:
  vial_1:
    type: vial
    name: test_vial
    model_name: standard_vial
    height: 66.75
    diameter: 28.0
    location:
      x: 2.0
      y: 40.0
      z: 20.0
    capacity_ul: 1500.0
    working_volume_ul: 1200.0
"""
        with _TempYamlFiles(deck=near_edge_deck) as f:
            with pytest.raises(SetupValidationError) as exc_info:
                setup_protocol(
                    f.gantry_path, f.deck_path, f.protocol_path,
                )
            assert any(v.coordinate_type == "gantry" for v in exc_info.value.violations)

    def test_raises_on_missing_gantry_file(self):
        with _TempYamlFiles() as f:
            with pytest.raises(GantryLoaderError):
                setup_protocol(
                    "/nonexistent/gantry.yaml", f.deck_path, f.protocol_path,
                )

    def test_raises_on_missing_deck_file(self):
        with _TempYamlFiles() as f:
            with pytest.raises(DeckLoaderError):
                setup_protocol(
                    f.gantry_path, "/nonexistent/deck.yaml", f.protocol_path,
                )

    def test_raises_when_gantry_has_no_instruments(self):
        gantry_without_instruments = GANTRY_YAML.split("instruments:\n", 1)[0]
        with _TempYamlFiles(gantry=gantry_without_instruments) as f:
            with pytest.raises(GantryLoaderError, match="instruments"):
                setup_protocol(
                    f.gantry_path, f.deck_path, f.protocol_path,
                )

    def test_raises_on_missing_protocol_file(self):
        with _TempYamlFiles() as f:
            with pytest.raises(ProtocolLoaderError):
                setup_protocol(
                    f.gantry_path, f.deck_path, "/nonexistent/protocol.yaml",
                )

    def test_uses_mock_gantry_by_default(self):
        with _TempYamlFiles() as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
            )
            assert isinstance(context.gantry, InstrumentedGantry)
            assert context.gantry.controller is not None

    def test_mock_mode_swaps_instrument_types(self):
        gantry_yaml = _gantry_with_instruments("""\
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    offset_x: -5.0
    offset_y: 0.0
    depth: 0.0
""")
        with _TempYamlFiles(gantry=gantry_yaml) as f:
            _, context = setup_protocol(
                f.gantry_path, f.deck_path, f.protocol_path,
                mock_mode=True,
            )
            from cubos.instruments.pipette.vendors.opentrons import OpentronsPipette
            assert isinstance(context.gantry.instruments["pipette"], OpentronsPipette)
            assert context.gantry.instruments["pipette"]._offline is True


class TestRunOnHardwareLifecycle:
    """run_on_hardware drives the full session against an offline gantry.

    These tests pass an explicit offline Gantry so the shared orchestration
    path (connect → prepare → connect instruments → health check → execute →
    disconnect) runs without touching real hardware.
    """

    def test_run_on_hardware_connects_and_disconnects(self):
        with _TempYamlFiles() as f:
            results = run_on_hardware(
                f.gantry_path, f.deck_path, f.protocol_path,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=DataStore(":memory:"),
            )
            assert isinstance(results, list)

    @pytest.mark.parametrize(
        "initial_fluids",
        [
            {
                "vial_1": {
                    "volume_ul": 250.0,
                    "composition": {"buffer": 250.0},
                }
            },
            {
                "fluids": {
                    "vial_1": {
                        "volume_ul": 250.0,
                        "composition": {"buffer": 250.0},
                    }
                }
            },
        ],
        ids=["direct-target-mapping", "yaml-shaped-mapping"],
    )
    def test_run_on_hardware_creates_state_and_attaches_context_and_campaign(
        self,
        initial_fluids,
    ):
        store = DataStore(":memory:")
        observed = {}
        protocol = Protocol([])

        def capture_context(context):
            observed["fluid_state_id"] = context.fluid_state_id
            observed["campaign_id"] = context.campaign_id
            return []

        protocol.execute = capture_context
        with _TempYamlFiles() as f:
            results = run_on_hardware(
                f.gantry_path,
                f.deck_path,
                protocol,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=store,
                initial_fluids=initial_fluids,
            )

        state_id = observed["fluid_state_id"]
        assert results == []
        assert isinstance(state_id, int)
        assert store.get_campaign_fluid_state_id(observed["campaign_id"]) == state_id
        snapshot = store.get_fluid_snapshot(state_id)
        vial = next(
            item for item in snapshot["containers"]
            if item["labware_key"] == "vial_1"
        )
        assert vial["current_volume_ul"] == 250.0
        assert vial["composition"] == {"buffer": 250.0}
        store.close()

    def test_run_on_hardware_resumes_state_and_attaches_caller_campaign(self):
        store = DataStore(":memory:")
        observed = {}
        with _TempYamlFiles() as f:
            run_on_hardware(
                f.gantry_path,
                f.deck_path,
                Protocol([]),
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=store,
                initial_fluids={
                    "vial_1": {
                        "volume_ul": 100.0,
                        "composition": {"water": 100.0},
                    }
                },
            )
            first_campaign = store._conn.execute(
                "SELECT id FROM campaigns ORDER BY id LIMIT 1"
            ).fetchone()[0]
            state_id = store.get_campaign_fluid_state_id(first_campaign)
            campaign_id = store.create_campaign("resume")
            protocol = Protocol([])

            def capture_context(context):
                observed["fluid_state_id"] = context.fluid_state_id
                return []

            protocol.execute = capture_context
            run_on_hardware(
                f.gantry_path,
                f.deck_path,
                protocol,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=store,
                campaign_id=campaign_id,
                fluid_state_id=state_id,
            )

        assert observed["fluid_state_id"] == state_id
        assert store.get_campaign_fluid_state_id(campaign_id) == state_id
        status = store._conn.execute(
            "SELECT status FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()[0]
        assert status == "completed"
        store.close()

    def test_run_on_hardware_inherits_linked_campaign_state_and_journals_transfer(
        self,
    ):
        store = DataStore(":memory:")
        observed = {}

        with _TempYamlFiles(deck=TWO_VIAL_DECK_YAML) as f:
            _, seed_context = setup_protocol(
                f.gantry_path,
                f.deck_path,
                Protocol([]),
                gantry=Gantry(offline=True),
                mock_mode=True,
            )
            state_id = store.create_fluid_state(
                f.deck_path,
                seed_context.deck,
                initial_fluids={
                    "vial_1": {
                        "volume_ul": 100.0,
                        "composition": {"water": 100.0},
                    }
                },
            )
            campaign_id = store.create_campaign(
                "linked resume",
                fluid_state_id=state_id,
            )
            protocol = Protocol([])

            def transfer_and_capture(context):
                from cubos.protocol_engine.commands.pipette import transfer

                observed["fluid_state_id"] = context.fluid_state_id
                transfer(
                    context,
                    source="vial_1",
                    destination="vial_2",
                    volume_ul=25.0,
                )
                return []

            protocol.execute = transfer_and_capture
            run_on_hardware(
                f.gantry_path,
                f.deck_path,
                protocol,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=store,
                campaign_id=campaign_id,
            )

        snapshot = store.get_fluid_snapshot(state_id)
        containers = {
            item["labware_key"]: item for item in snapshot["containers"]
        }
        assert observed["fluid_state_id"] == state_id
        assert containers["vial_1"]["current_volume_ul"] == 75.0
        assert containers["vial_2"]["current_volume_ul"] == 25.0
        assert len(snapshot["operations"]) == 1
        assert snapshot["operations"][0]["campaign_id"] == campaign_id
        assert snapshot["operations"][0]["status"] == "applied"
        store.close()

    def test_run_on_hardware_rejects_seed_for_linked_campaign_before_connect(self):
        store = DataStore(":memory:")

        with _TempYamlFiles() as f:
            _, seed_context = setup_protocol(
                f.gantry_path,
                f.deck_path,
                Protocol([]),
                gantry=Gantry(offline=True),
                mock_mode=True,
            )
            state_id = store.create_fluid_state(f.deck_path, seed_context.deck)
            campaign_id = store.create_campaign(
                "linked seed conflict",
                fluid_state_id=state_id,
            )
            gantry = Gantry(offline=True)
            gantry.connect = MagicMock()
            state_count_before = store._conn.execute(
                "SELECT COUNT(*) FROM fluid_state_sessions"
            ).fetchone()[0]

            with pytest.raises(ValueError, match="already attached.*initial_fluids"):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    Protocol([]),
                    gantry=gantry,
                    mock_mode=True,
                    data_store=store,
                    campaign_id=campaign_id,
                    initial_fluids={"vial_1": {"volume_ul": 1.0}},
                )

        state_count_after = store._conn.execute(
            "SELECT COUNT(*) FROM fluid_state_sessions"
        ).fetchone()[0]
        assert state_count_after == state_count_before
        gantry.connect.assert_not_called()
        store.close()

    def test_run_on_hardware_rejects_conflicting_linked_state_before_connect(self):
        store = DataStore(":memory:")

        with _TempYamlFiles() as f:
            _, seed_context = setup_protocol(
                f.gantry_path,
                f.deck_path,
                Protocol([]),
                gantry=Gantry(offline=True),
                mock_mode=True,
            )
            linked_state_id = store.create_fluid_state(
                f.deck_path,
                seed_context.deck,
            )
            conflicting_state_id = store.create_fluid_state(
                f.deck_path,
                seed_context.deck,
            )
            campaign_id = store.create_campaign(
                "linked state conflict",
                fluid_state_id=linked_state_id,
            )
            gantry = Gantry(offline=True)
            gantry.connect = MagicMock()

            with pytest.raises(
                ValueError,
                match=f"already attached to fluid state {linked_state_id}",
            ):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    Protocol([]),
                    gantry=gantry,
                    mock_mode=True,
                    data_store=store,
                    campaign_id=campaign_id,
                    fluid_state_id=conflicting_state_id,
                )

        gantry.connect.assert_not_called()
        store.close()

    def test_run_on_hardware_rejects_resume_and_seed_before_setup(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            run_on_hardware(
                "missing-gantry.yaml",
                "missing-deck.yaml",
                "missing-protocol.yaml",
                fluid_state_id=1,
                initial_fluids={"vial_1": {"volume_ul": 1.0}},
            )

    def test_run_on_hardware_disconnects_on_failure(self):
        bad_protocol = """\
protocol:
  - move:
      instrument: nonexistent_instrument
      position: vial_1
"""
        with _TempYamlFiles(protocol=bad_protocol) as f:
            with pytest.raises(Exception):
                run_on_hardware(
                    f.gantry_path, f.deck_path, f.protocol_path,
                    gantry=Gantry(offline=True),
                    mock_mode=True,
                    data_store=DataStore(":memory:"),
                )

    def test_run_on_hardware_mock_mode(self):
        gantry_yaml = _gantry_with_instruments("""\
instruments:
  pipette:
    type: pipette
    vendor: opentrons
    offset_x: -5.0
    offset_y: 0.0
    depth: 0.0
""")
        with _TempYamlFiles(gantry=gantry_yaml) as f:
            results = run_on_hardware(
                f.gantry_path, f.deck_path, f.protocol_path,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=DataStore(":memory:"),
            )
            assert isinstance(results, list)

    def test_run_on_hardware_auto_creates_campaign_and_persists_uvvis_measure(self):
        store = DataStore(":memory:")
        with _TempYamlFiles(
            gantry=UVVIS_GANTRY_YAML,
            deck=PLATE_DECK_YAML,
            protocol=UVVIS_MEASURE_PROTOCOL_YAML,
        ) as f:
            results = run_on_hardware(
                f.gantry_path,
                f.deck_path,
                f.protocol_path,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=store,
            )

        assert len(results) == 1
        campaign_count = store._conn.execute(
            "SELECT COUNT(*) FROM campaigns"
        ).fetchone()[0]
        experiment_count = store._conn.execute(
            "SELECT COUNT(*) FROM experiments"
        ).fetchone()[0]
        uvvis_count = store._conn.execute(
            "SELECT COUNT(*) FROM uvvis_measurements"
        ).fetchone()[0]
        assert campaign_count == 1
        assert experiment_count == 1
        assert uvvis_count == 1
        status = store._conn.execute(
            "SELECT status, finished_at FROM campaigns"
        ).fetchone()
        assert status[0] == "completed"
        assert status[1] is not None

    def test_run_on_hardware_marks_campaign_failed_on_execution_error(
        self, monkeypatch,
    ):
        store = DataStore(":memory:")
        campaign_id = store.create_campaign(description="failed run")

        class FailingProtocol:
            def execute(self, context):
                raise RuntimeError("boom")

        def fake_setup_protocol(*args, **kwargs):
            return FailingProtocol(), ProtocolContext(
                gantry=SimpleNamespace(
                    safe_z=None,
                    connect_instruments=MagicMock(),
                    disconnect_instruments=MagicMock(),
                ),
                deck=MagicMock(),
                data_store=kwargs["data_store"],
                campaign_id=kwargs["campaign_id"],
            )

        monkeypatch.setattr(
            "cubos.protocol_engine.setup.setup_protocol", fake_setup_protocol,
        )

        with _TempYamlFiles() as f:
            with pytest.raises(RuntimeError, match="boom"):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    f.protocol_path,
                    gantry=Gantry(offline=True),
                    mock_mode=True,
                    data_store=store,
                    campaign_id=campaign_id,
                )

        status = store._conn.execute(
            "SELECT status, finished_at FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        assert status[0] == "failed"
        assert status[1] is not None
        store.close()

    def test_run_on_hardware_marks_keyboard_interrupt_failed_and_disconnects(self):
        store = DataStore(":memory:")
        campaign_id = store.create_campaign(description="interrupted run")
        protocol = Protocol([])

        def interrupt(_context):
            raise KeyboardInterrupt

        protocol.execute = interrupt
        gantry = Gantry(offline=True)
        original_disconnect = gantry.disconnect
        gantry.disconnect = MagicMock(wraps=original_disconnect)

        with _TempYamlFiles() as f:
            with pytest.raises(KeyboardInterrupt):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    protocol,
                    gantry=gantry,
                    mock_mode=True,
                    data_store=store,
                    campaign_id=campaign_id,
                )

        status = store._conn.execute(
            "SELECT status, finished_at FROM campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        assert status[0] == "failed"
        assert status[1] is not None
        gantry.disconnect.assert_called_once_with()
        store.close()

    def test_run_on_hardware_full_lifecycle_order(self, monkeypatch):
        calls = []

        # Track the gantry-level lifecycle steps.
        for name in ("connect", "prepare_for_protocol_run", "disconnect"):
            original = getattr(Gantry, name)

            def make_tracker(method, label):
                def tracker(self, *args, **kwargs):
                    calls.append(label)
                    return method(self, *args, **kwargs)
                return tracker

            monkeypatch.setattr(Gantry, name, make_tracker(original, name))

        original_is_healthy = Gantry.is_healthy

        def tracking_is_healthy(self):
            calls.append("is_healthy")
            return original_is_healthy(self)

        monkeypatch.setattr(Gantry, "is_healthy", tracking_is_healthy)

        # Track the instrument-level connect/disconnect steps.
        original_connect = InstrumentedGantry.connect_instruments
        original_disconnect = InstrumentedGantry.disconnect_instruments

        def tracking_connect(self):
            calls.append("connect_instruments")
            return original_connect(self)

        def tracking_disconnect(self):
            calls.append("disconnect_instruments")
            return original_disconnect(self)

        monkeypatch.setattr(
            InstrumentedGantry, "connect_instruments", tracking_connect,
        )
        monkeypatch.setattr(
            InstrumentedGantry, "disconnect_instruments", tracking_disconnect,
        )

        built_protocol = (
            ProtocolBuilder()
            .add_move(instrument="pipette", position="vial_1")
            .build()
        )
        with _TempYamlFiles() as f:
            results = run_on_hardware(
                f.gantry_path,
                f.deck_path,
                built_protocol,
                gantry=Gantry(offline=True),
                mock_mode=True,
                data_store=DataStore(":memory:"),
            )

        assert isinstance(results, list)
        assert calls == [
            "connect",
            "prepare_for_protocol_run",
            "connect_instruments",
            "is_healthy",
            "disconnect_instruments",
            "disconnect",
        ]

    def test_mid_scan_failure_retracts_to_safe_z_before_disconnect(self, monkeypatch):
        events = []

        class Controller:
            def connect(self):
                events.append(("connect",))

            def prepare_for_protocol_run(self):
                events.append(("prepare",))

            def is_healthy(self):
                events.append(("healthy",))
                return True

            position = {"x": 0.0, "y": 0.0, "z": 0.0}

            def get_coordinates(self):
                return dict(self.position)

            def move_to(self, x, y, z, travel_z=None):
                events.append(("move_to", x, y, z, travel_z))
                self.position = {"x": x, "y": y, "z": z}

            def disconnect(self):
                events.append(("disconnect",))

        class FailingInstrument:
            name = "probe"
            offset_x = 0.0
            offset_y = 0.0
            depth = 0.0
            effective_depth = 0.0

            def __init__(self):
                self.calls = 0

            def connect(self):
                events.append(("instrument_connect",))

            def disconnect(self):
                events.append(("instrument_disconnect",))

            def measure(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("instrument boom")
                return True

        controller = Controller()
        plate = WellPlate(
            name="test_plate",
            model_name="test_2x1",
            rows=1,
            columns=2,
            wells={
                "A1": Coordinate3D(x=10.0, y=20.0, z=5.0),
                "A2": Coordinate3D(x=20.0, y=20.0, z=5.0),
            },
            capacity_ul=200.0,
            working_volume_ul=150.0,
        )
        context = ProtocolContext(
            gantry=InstrumentedGantry(
                controller=controller,
                instruments={"probe": FailingInstrument()},
                safe_z=80.0,
            ),
            deck=Deck({"plate_1": plate}),
        )

        class ScanProtocol:
            def execute(self, runtime_context):
                return scan(
                    runtime_context,
                    plate="plate_1",
                    instrument="probe",
                    method="measure",
                    measurement_height=0.0,
                    interwell_scan_height=10.0,
                )

        def fake_setup_protocol(*args, **kwargs):
            return ScanProtocol(), context

        monkeypatch.setattr(
            "cubos.protocol_engine.setup.setup_protocol", fake_setup_protocol,
        )

        with _TempYamlFiles() as f:
            with pytest.raises(RuntimeError, match="instrument boom"):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    f.protocol_path,
                    gantry=controller,
                    mock_mode=True,
                    data_store=DataStore(":memory:"),
                )

        retract = ("move_to", 20.0, 20.0, 80.0, 80.0)
        assert retract in events
        assert events.index(retract) < events.index(("disconnect",))

    def test_disconnect_failure_preserves_root_exception_and_closes_store(
        self, monkeypatch,
    ):
        closed = []

        class ClosingStore:
            def get_campaign_fluid_state_id(self, campaign_id):
                return None

            def close(self):
                closed.append(True)

        class DisconnectFails:
            def connect(self):
                pass

            def prepare_for_protocol_run(self):
                pass

            def is_healthy(self):
                return True

            def disconnect(self):
                raise RuntimeError("disconnect boom")

        class FailingProtocol:
            def execute(self, context):
                raise ValueError("root cause")

        from cubos import data
        store = ClosingStore()
        monkeypatch.setattr(data, "DataStore", lambda: store)

        def fake_setup_protocol(*args, **kwargs):
            return FailingProtocol(), ProtocolContext(
                gantry=SimpleNamespace(
                    safe_z=None,
                    connect_instruments=MagicMock(),
                    disconnect_instruments=MagicMock(),
                ),
                deck=MagicMock(),
                data_store=kwargs["data_store"],
                campaign_id=1,
            )

        monkeypatch.setattr(
            "cubos.protocol_engine.setup.setup_protocol", fake_setup_protocol,
        )

        with _TempYamlFiles() as f:
            with pytest.raises(ValueError, match="root cause"):
                run_on_hardware(
                    f.gantry_path,
                    f.deck_path,
                    f.protocol_path,
                    gantry=DisconnectFails(),
                    mock_mode=True,
                    campaign_id=1,
                )

        assert closed == [True]

    def test_run_on_hardware_mock_mode_without_gantry_constructs_offline_gantry(
        self, monkeypatch,
    ):
        constructed = []

        class FakeGantry:
            def __init__(self, config=None, offline=False):
                constructed.append({"config": config, "offline": offline})

            def connect(self):
                pass

            def prepare_for_protocol_run(self):
                pass

            def is_healthy(self):
                return True

            def disconnect(self):
                pass

        class EmptyProtocol:
            def execute(self, context):
                return []

        def fake_setup_protocol(*args, **kwargs):
            return EmptyProtocol(), ProtocolContext(
                gantry=SimpleNamespace(
                    safe_z=None,
                    connect_instruments=MagicMock(),
                    disconnect_instruments=MagicMock(),
                ),
                deck=MagicMock(),
                data_store=kwargs["data_store"],
                campaign_id=1,
            )

        monkeypatch.setattr("cubos.protocol_engine.setup.Gantry", FakeGantry)
        monkeypatch.setattr(
            "cubos.protocol_engine.setup.setup_protocol", fake_setup_protocol,
        )

        with _TempYamlFiles() as f:
            store = DataStore(":memory:")
            campaign_id = store.create_campaign("mock gantry construction")
            results = run_on_hardware(
                f.gantry_path,
                f.deck_path,
                f.protocol_path,
                mock_mode=True,
                data_store=store,
                campaign_id=campaign_id,
            )

        assert results == []
        assert constructed == [{"config": None, "offline": True}]
        store.close()
