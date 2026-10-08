"""Tests for the VernierASMI instrument driver (offline mode)."""

import unittest
from unittest.mock import MagicMock, patch

from cubos.instruments.asmi.vendors.vernier import VernierASMI
from cubos.instruments.asmi.exceptions import ASMICommandError, ASMIConnectionError
from cubos.instruments.asmi.models import ASMIStatus, MeasurementResult


class TestASMIOffline(unittest.TestCase):
    """Verify VernierASMI(offline=True) behaves correctly without hardware."""

    def setUp(self):
        self.asmi = VernierASMI(offline=True, default_force=1.5)

    def test_connect_disconnect_are_noops(self):
        self.asmi.connect()
        self.asmi.disconnect()

    def test_health_check_returns_true(self):
        self.assertTrue(self.asmi.health_check())

    def test_is_connected_returns_true(self):
        self.assertTrue(self.asmi.is_connected())

    def test_measure_returns_default_force(self):
        result = self.asmi.measure(n_samples=3)
        self.assertIsInstance(result, MeasurementResult)
        self.assertEqual(result.mean_n, 1.5)
        self.assertEqual(result.std_n, 0.0)
        self.assertEqual(len(result.readings), 3)

    def test_get_force_reading_returns_default(self):
        self.assertAlmostEqual(self.asmi.get_force_reading(), 1.5)

    def test_get_baseline_force_returns_default(self):
        avg, std = self.asmi.get_baseline_force(samples=5)
        self.assertAlmostEqual(avg, 1.5)
        self.assertAlmostEqual(std, 0.0)

    def test_get_status_offline(self):
        status = self.asmi.get_status()
        self.assertIsInstance(status, ASMIStatus)
        self.assertTrue(status.is_connected)
        self.assertEqual(status.sensor_description, "OfflineSensor")

    def test_indentation_offline_returns_data(self):
        """Offline indentation should return synthetic measurements quickly."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry, well_z=0.0, measurement_height=10.0, indentation_limit_height=8.0, step_size=0.1,
        )

        self.assertIn("measurements", result)
        self.assertIn("baseline_avg", result)
        self.assertIn("data_points", result)
        self.assertEqual(result["data_points"], len(result["measurements"]))
        self.assertGreater(result["data_points"], 0)
        self.assertFalse(result["force_exceeded"])
        self.assertFalse(result["measure_with_return"])

    def test_indentation_offline_with_return_includes_directions(self):
        """Return-mode indentation should include both down and up direction samples."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=8.0,
            step_size=0.1,
            measure_with_return=True,
        )

        self.assertTrue(result["measure_with_return"])
        directions = [step.get("direction") for step in result["measurements"]]
        self.assertIn("down", directions)
        self.assertIn("up", directions)

    def test_indentation_offline_emits_direction_unconditionally(self):
        """Every sample should carry a direction tag, even without return mode."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry, well_z=0.0, measurement_height=10.0, indentation_limit_height=8.0, step_size=0.1,
        )

        self.assertGreater(len(result["measurements"]), 0)
        for step in result["measurements"]:
            self.assertEqual(step["direction"], "down")

    def test_indentation_offline_return_preserves_ordering_and_monotonicity(self):
        """All down samples must precede all up samples. Deck-origin +Z-up:
        descent decreases z toward the deepest plane, return increases z
        back to the action plane."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=8.0,
            step_size=0.1,
            measure_with_return=True,
        )

        steps = result["measurements"]
        directions = [s["direction"] for s in steps]
        last_down = max(i for i, d in enumerate(directions) if d == "down")
        first_up = min(i for i, d in enumerate(directions) if d == "up")
        self.assertLess(last_down, first_up)

        down_z = [s["z_mm"] for s in steps if s["direction"] == "down"]
        up_z = [s["z_mm"] for s in steps if s["direction"] == "up"]
        for prev, curr in zip(down_z, down_z[1:]):
            self.assertLess(curr, prev)
        for prev, curr in zip(up_z, up_z[1:]):
            self.assertGreater(curr, prev)
        # Return terminates at the action plane (well top), never overshoots.
        self.assertAlmostEqual(up_z[-1], 10.0, places=6)

    def test_indentation_offline_return_no_float_drift(self):
        """Descent must reach the deepest plane exactly and the return must
        hit the action plane exactly even when step size doesn't divide
        the range evenly."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        # 0.03 does not evenly divide 2.0 (66.67 steps → ceil to 67).
        result = self.asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=8.0,    # 2 mm of descent
            step_size=0.03,
            measure_with_return=True,
        )

        down_z = [s["z_mm"] for s in result["measurements"] if s["direction"] == "down"]
        up_z = [s["z_mm"] for s in result["measurements"] if s["direction"] == "up"]
        self.assertAlmostEqual(down_z[-1], 8.0, places=6)
        self.assertAlmostEqual(up_z[-1], 10.0, places=6)

    def test_indentation_offline_step_larger_than_span_takes_one_step(self):
        """When step_size exceeds the descent span, one clamped step occurs."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=9.95,   # 0.05 mm of descent
            step_size=0.5,
            measure_with_return=True,
        )

        down_z = [s["z_mm"] for s in result["measurements"] if s["direction"] == "down"]
        up_z = [s["z_mm"] for s in result["measurements"] if s["direction"] == "up"]
        self.assertEqual(len(down_z), 1)
        self.assertAlmostEqual(down_z[0], 9.95, places=6)
        self.assertEqual(len(up_z), 1)
        self.assertAlmostEqual(up_z[0], 10.0, places=6)

    def test_indentation_limit_height_drives_descent(self):
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=9.8,    # 0.2 mm of descent
            step_size=0.1,
        )

        self.assertEqual(result["data_points"], 2)
        self.assertAlmostEqual(result["measurements"][-1]["z_mm"], 9.8)

    def test_indentation_rejects_non_positive_step_size(self):
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        with self.assertRaises(ValueError, msg="step_size"):
            self.asmi.indentation(
                gantry, well_z=0.0, measurement_height=10.0, indentation_limit_height=8.0, step_size=0.0,
            )

    def test_indentation_rejects_indentation_limit_height_above_measurement_height(self):
        """``indentation_limit_height`` must be ≤ ``measurement_height``:
        descending through the well surface is fine, but a deepest plane
        *above* the start plane would mean the descent goes up — meaningless."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        with self.assertRaises(ValueError, msg="indentation_limit_height"):
            self.asmi.indentation(
                gantry, well_z=0.0, measurement_height=10.0,
                indentation_limit_height=10.5, step_size=0.1,
            )

    def test_indentation_equal_offsets_is_legal(self):
        """A zero-descent indentation is the inclusive boundary — the spec
        is ``indentation_limit_height ≤ measurement_height``. The engine and
        validator both accept equality, so the driver does too. The motion
        loop runs zero descent steps and the return loop is a no-op."""
        from cubos.gantry.gantry import Gantry
        gantry = Gantry(offline=True)

        result = self.asmi.indentation(
            gantry, well_z=0.0, measurement_height=10.0,
            indentation_limit_height=10.0, step_size=0.1,
        )

        assert result["data_points"] == 0


class _FakeOnlineGantry:
    """Minimal gantry stub for exercising VernierASMI online indentation loops."""

    def __init__(self, start_z: float):
        self._z = start_z

    def get_coordinates(self) -> dict:
        return {"x": 0.0, "y": 0.0, "z": self._z}

    def get_status(self) -> str:
        return "Idle"

    def move_to(self, x: float, y: float, z: float) -> None:
        self._z = z


class TestASMIOnlineIndentation(unittest.TestCase):
    """Exercise the non-offline indentation code path with mocked hardware I/O."""

    def _make_online_asmi(self) -> VernierASMI:
        asmi = VernierASMI(offline=False, default_force=0.0)
        asmi._offline = False
        return asmi

    def test_move_z_raises_when_gantry_never_goes_idle(self):
        asmi = VernierASMI(offline=False, idle_timeout=0.0)
        gantry = _FakeOnlineGantry(start_z=10.0)
        gantry.get_status = lambda: "Run"

        with self.assertRaises(ASMICommandError):
            asmi._move_z(gantry, 0.0, 0.0, 10.1)

    def test_online_indentation_with_return_records_both_directions(self):
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", return_value=0.1):
            result = asmi.indentation(
                gantry,
                well_z=0.0,
                measurement_height=10.0,
                indentation_limit_height=0.5,    # 9.5 mm of descent
                step_size=0.1,
                force_limit=100.0,
                baseline_samples=1,
                measure_with_return=True,
            )

        directions = [s["direction"] for s in result["measurements"]]
        self.assertIn("down", directions)
        self.assertIn("up", directions)
        up_z = [s["z_mm"] for s in result["measurements"] if s["direction"] == "up"]
        self.assertAlmostEqual(up_z[-1], 10.0, places=6)

    def test_online_return_terminates_even_if_gantry_stalls(self):
        """If gantry Z never retracts, return loop must bail via iteration cap, not spin."""
        asmi = self._make_online_asmi()

        class StalledGantry(_FakeOnlineGantry):
            def move_to(self, x, y, z):
                # Simulate a stalled axis: position never changes.
                pass

        gantry = StalledGantry(start_z=10.0)
        # Prime descent: first move puts z at the action plane, second move
        # takes one descent step; we only need _some_ descent sample to
        # trigger the return block.
        original_move = _FakeOnlineGantry.move_to
        call_count = {"n": 0}

        def move_once_then_stall(self, x, y, z):
            call_count["n"] += 1
            if call_count["n"] == 1:
                original_move(self, x, y, z)  # initial descend to action plane
            elif call_count["n"] == 2:
                original_move(self, x, y, z)  # one descent step
            # subsequent moves no-op → stall during return

        with patch.object(StalledGantry, "move_to", move_once_then_stall), \
             patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", return_value=0.0):
            result = asmi.indentation(
                gantry,
                well_z=0.0,
                measurement_height=10.0,
                indentation_limit_height=0.1,    # 9.9 mm of descent
                step_size=0.1,
                force_limit=100.0,
                baseline_samples=1,
                measure_with_return=True,
            )

        # The test's real requirement: the call returns, i.e. no infinite loop.
        self.assertIn("measurements", result)


class TestASMISurfaceDetection(unittest.TestCase):
    """Exercise detect_surface: coarse search, back-off, re-anchored limit."""

    SURFACE_Z = 6.0
    CONTACT_FORCE = 0.5

    def _make_online_asmi(self) -> VernierASMI:
        asmi = VernierASMI(offline=False, default_force=0.0)
        asmi._offline = False
        return asmi

    def _force_at(self, gantry) -> float:
        """Force model: zero in air, CONTACT_FORCE at/below the surface."""
        return self.CONTACT_FORCE if gantry._z <= self.SURFACE_Z else 0.0

    def _run_detect_indentation(self, asmi, gantry, **overrides):
        kwargs = dict(
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=-1.0,
            step_size=0.5,
            force_limit=100.0,
            baseline_samples=1,
            detect_surface=True,
            surface_search_step=1.0,
            surface_force_threshold=0.01,
            surface_search_max_travel=8.0,
        )
        kwargs.update(overrides)
        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading",
                          side_effect=lambda: self._force_at(gantry)):
            return asmi.indentation(gantry, **kwargs)

    def test_detection_backs_off_one_step_and_reanchors_limit(self):
        """Trigger at Z=6 → surface set one search step above (Z=7); the
        fine descent then runs from the surface to surface + limit."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        result = self._run_detect_indentation(asmi, gantry)

        self.assertTrue(result["detect_surface"])
        self.assertAlmostEqual(result["surface_z_mm"], 7.0, places=6)
        self.assertAlmostEqual(result["surface_trigger_force_n"],
                               self.CONTACT_FORCE, places=6)
        self.assertAlmostEqual(result["surface_search_step_mm"], 1.0)
        self.assertAlmostEqual(result["surface_force_threshold_n"], 0.01)
        # Limit re-anchored: -1.0 below the detected surface, not well_z.
        self.assertAlmostEqual(result["z_target_mm"], 6.0, places=6)
        down_z = [s["z_mm"] for s in result["measurements"]]
        self.assertAlmostEqual(down_z[0], 6.5, places=6)
        self.assertAlmostEqual(down_z[-1], 6.0, places=6)
        # Coarse search samples (Z=9, 8, 7, 6) are not in the record.
        self.assertTrue(all(z <= 7.0 for z in down_z))

    def test_detection_return_sweep_ends_at_detected_surface(self):
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        result = self._run_detect_indentation(
            asmi, gantry, measure_with_return=True,
        )

        up_z = [s["z_mm"] for s in result["measurements"]
                if s["direction"] == "up"]
        self.assertAlmostEqual(up_z[-1], result["surface_z_mm"], places=6)

    def test_transient_spike_is_rejected_and_search_continues(self):
        """A single noisy reading over threshold must not lock the
        surface — only a crossing confirmed on re-read is accepted."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)
        reads_at_spike_z = {"n": 0}
        spike_z = 8.0

        def force_reading():
            z = gantry._z
            if z <= self.SURFACE_Z:
                return self.CONTACT_FORCE
            if z == spike_z:
                reads_at_spike_z["n"] += 1
                # First read spikes above threshold (sensor noise); the
                # confirmation re-read at the same Z reports it's gone.
                return self.CONTACT_FORCE if reads_at_spike_z["n"] == 1 else 0.0
            return 0.0

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", side_effect=force_reading):
            result = asmi.indentation(
                gantry,
                well_z=0.0,
                measurement_height=10.0,
                indentation_limit_height=-1.0,
                step_size=0.5,
                force_limit=100.0,
                baseline_samples=1,
                detect_surface=True,
                surface_search_step=1.0,
                surface_force_threshold=0.01,
                surface_search_max_travel=8.0,
            )

        # The Z=8 crossing was checked and rejected (2 reads: trigger + confirm).
        self.assertEqual(reads_at_spike_z["n"], 2)
        # The real, sustained surface at Z=6 is what gets detected.
        self.assertAlmostEqual(result["surface_z_mm"], 7.0, places=6)
        self.assertAlmostEqual(result["surface_trigger_force_n"],
                               self.CONTACT_FORCE, places=6)

    def test_unconfirmed_crossings_still_raise_if_never_sustained(self):
        """If every threshold crossing fails to hold on re-read, the
        search must still exhaust max_travel and raise, not false-lock."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)
        call_count = {"n": 0}

        def force_reading():
            # Every read alternates above/below threshold, so no crossing
            # is ever confirmed by an immediate re-read at the same Z.
            call_count["n"] += 1
            return self.CONTACT_FORCE if call_count["n"] % 2 == 1 else 0.0

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", side_effect=force_reading):
            with self.assertRaises(ASMICommandError):
                asmi.indentation(
                    gantry,
                    well_z=0.0,
                    measurement_height=10.0,
                    indentation_limit_height=-1.0,
                    step_size=0.5,
                    baseline_samples=1,
                    detect_surface=True,
                    surface_search_step=1.0,
                    surface_force_threshold=0.01,
                    surface_search_max_travel=3.0,
                )

    def test_surface_search_aborts_immediately_on_hard_force_limit(self):
        """A reading past the hard `force_limit` safety cutoff must abort
        immediately, even though it also crosses `surface_force_threshold`
        — it must not be routed through the noise-confirmation path, which
        could otherwise let a real, over-limit contact be second-guessed
        away as noise and stepped through."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)
        reads = {"n": 0}

        def force_reading():
            reads["n"] += 1
            return 5.0  # far past both surface_force_threshold and force_limit

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", side_effect=force_reading):
            with self.assertRaises(ASMICommandError):
                asmi.indentation(
                    gantry,
                    well_z=0.0,
                    measurement_height=10.0,
                    indentation_limit_height=-1.0,
                    step_size=0.5,
                    force_limit=1.0,
                    baseline_samples=1,
                    detect_surface=True,
                    surface_search_step=1.0,
                    surface_force_threshold=0.01,
                    surface_search_max_travel=8.0,
                )

        # Aborted on the very first reading — never reached (or needed) the
        # noise-confirmation re-read.
        self.assertEqual(reads["n"], 1)

    def test_surface_search_aborts_on_hard_force_limit_at_confirmation_read(self):
        """A crossing that's modest on the trigger read but spikes past the
        hard `force_limit` on the confirmation re-read must still abort —
        the confirmation read is a real physical measurement too, not
        exempt from the safety cutoff just because it's a second check."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)
        reads = {"n": 0}

        def force_reading():
            reads["n"] += 1
            # Trigger read: just over surface_force_threshold, under
            # force_limit. Confirmation read: spikes past force_limit.
            return 0.05 if reads["n"] == 1 else 5.0

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", side_effect=force_reading):
            with self.assertRaises(ASMICommandError):
                asmi.indentation(
                    gantry,
                    well_z=0.0,
                    measurement_height=10.0,
                    indentation_limit_height=-1.0,
                    step_size=0.5,
                    force_limit=1.0,
                    baseline_samples=1,
                    detect_surface=True,
                    surface_search_step=1.0,
                    surface_force_threshold=0.01,
                    surface_search_max_travel=8.0,
                )

        self.assertEqual(reads["n"], 2)

    def test_no_surface_within_max_travel_raises(self):
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        with patch.object(asmi, "get_baseline_force", return_value=(0.0, 0.0)), \
             patch.object(asmi, "get_force_reading", return_value=0.0):
            with self.assertRaises(ASMICommandError):
                asmi.indentation(
                    gantry,
                    well_z=0.0,
                    measurement_height=10.0,
                    indentation_limit_height=-1.0,
                    step_size=0.5,
                    baseline_samples=1,
                    detect_surface=True,
                    surface_search_step=1.0,
                    surface_search_max_travel=3.0,
                )

    def test_detect_mode_rejects_positive_limit(self):
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        with self.assertRaises(ValueError, msg="indentation_limit_height"):
            self._run_detect_indentation(
                asmi, gantry, indentation_limit_height=0.5,
            )

    def test_detect_mode_allows_limit_above_measurement_height_frame(self):
        """In detect mode the limit is surface-relative, so a limit above
        measurement_height (different frame) must be accepted."""
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        result = self._run_detect_indentation(
            asmi, gantry, measurement_height=-2.0, well_z=12.0,
        )
        self.assertTrue(result["detect_surface"])

    def test_detect_mode_rejects_non_positive_search_parameters(self):
        asmi = self._make_online_asmi()
        gantry = _FakeOnlineGantry(start_z=10.0)

        for overrides in (
            {"surface_search_step": 0.0},
            {"surface_force_threshold": -0.01},
            {"surface_search_max_travel": 0.0},
        ):
            with self.assertRaises(ValueError):
                self._run_detect_indentation(asmi, gantry, **overrides)

    def test_offline_detection_anchors_at_measurement_plane(self):
        from cubos.gantry.gantry import Gantry
        asmi = VernierASMI(offline=True, default_force=0.0)
        gantry = Gantry(offline=True)

        result = asmi.indentation(
            gantry,
            well_z=0.0,
            measurement_height=10.0,
            indentation_limit_height=-1.0,
            step_size=0.5,
            detect_surface=True,
        )

        self.assertTrue(result["detect_surface"])
        self.assertAlmostEqual(result["surface_z_mm"], 10.0, places=6)
        self.assertAlmostEqual(result["z_target_mm"], 9.0, places=6)
        down_z = [s["z_mm"] for s in result["measurements"]]
        self.assertAlmostEqual(down_z[-1], 9.0, places=6)

    def test_non_detect_result_has_no_surface_fields(self):
        from cubos.gantry.gantry import Gantry
        asmi = VernierASMI(offline=True, default_force=0.0)
        gantry = Gantry(offline=True)

        result = asmi.indentation(
            gantry, well_z=0.0, measurement_height=10.0,
            indentation_limit_height=8.0, step_size=0.1,
        )

        self.assertFalse(result["detect_surface"])
        self.assertNotIn("surface_z_mm", result)


class TestASMIOnlineRequiresHardware(unittest.TestCase):
    """Verify VernierASMI(offline=False) raises without hardware."""

    def test_measure_without_connect_raises(self):
        asmi = VernierASMI(offline=False)
        from cubos.instruments.asmi.exceptions import ASMICommandError
        with self.assertRaises(ASMICommandError):
            asmi.measure()

    def test_health_check_without_connect_returns_false(self):
        asmi = VernierASMI(offline=False)
        self.assertFalse(asmi.health_check())

    def test_is_connected_without_connect_returns_false(self):
        asmi = VernierASMI(offline=False)
        self.assertFalse(asmi.is_connected())

    def test_failed_sensor_read_raises_command_error(self):
        asmi = VernierASMI(offline=False)
        asmi._device = MagicMock()
        asmi._device.read.return_value = False
        asmi._sensor = MagicMock()
        asmi._sensor.values = [0.0]

        with self.assertRaises(ASMICommandError):
            asmi.measure()
        asmi._device.stop.assert_called_once()

    def test_health_check_catches_raw_sdk_exception(self):
        asmi = VernierASMI(offline=False)
        asmi._device = MagicMock()
        asmi._sensor = MagicMock()
        with patch.object(asmi, "measure", side_effect=RuntimeError("usb lost")):
            self.assertFalse(asmi.health_check())

    def test_connect_wraps_godirect_constructor_exception(self):
        with patch.dict(
            "sys.modules",
            {"godirect": MagicMock(GoDirect=MagicMock(side_effect=RuntimeError("usb")))}
        ):
            asmi = VernierASMI(offline=False)
            with self.assertRaises(ASMIConnectionError):
                asmi.connect()
