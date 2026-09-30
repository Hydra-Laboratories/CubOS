"""PalmSens EmStat potentiostat driver.

Wraps the open-source ``hardpotato`` library (BU-KABlab fork, >=1.3.14),
which drives EmStat devices over plain pyserial with MethodSCRIPT — no
vendor SDK, so it runs anywhere pyserial does, including ARM64 (Raspberry
Pi). This is the potentiostat path for the PANDA bench, whose Admiral
alternative (``SquidstatPyLibrary``) has no ARM build.

hardpotato facts this driver relies on (verified against 1.3.14):
  * ``hp.potentiostat.Setup(model, path, folder, port=..., verbose=0)``
    stores module-level state; ``check_connection()`` probes the device on
    the given serial port. The port must be explicit — hardpotato's
    auto-detect path does not return a result from ``check_connection``.
  * Technique classes ``OCP(ttot, dt, ...)``, ``CV(Eini, Ev1, Ev2, Efin,
    sr, dE, nSweeps, ...)``, ``CA(Estep, dt, ttot, ...)`` block in
    ``run()`` and then expose ``.data`` as MethodSCRIPT curves: a list of
    curves, each a list of rows, each a list of variables with a ``.value``
    attribute. Column order is (t, E) for OCP and (t, E, i) for CV/CA.
  * There is no chronopotentiometry technique, so :meth:`run_CP` raises
    ``NotImplementedError``.
  * ``run()`` writes ``<fileName>.mscr``/``.txt`` into the Setup folder;
    the driver points that at a per-connect temp directory unless
    ``data_dir`` is given.

``hardpotato`` is imported lazily inside :meth:`connect`; the package can be
imported, params/results built, and :attr:`offline` runs performed without it.
"""

from __future__ import annotations

import math
import random
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from cubos.instruments.potentiostat.exceptions import (
    PotentiostatCommandError,
    PotentiostatConfigError,
    PotentiostatConnectionError,
)
from cubos.instruments.potentiostat.interface import PotentiostatInstrument
from cubos.instruments.potentiostat.models import (
    CAParams,
    CAResult,
    CPParams,
    CPResult,
    CVParams,
    CVResult,
    OCPParams,
    OCPResult,
)
from cubos.instruments.potentiostat.simulation import (
    simulate_CA,
    simulate_CV,
    simulate_OCP,
)


_VENDOR = "emstat"

DEFAULT_MODEL = "emstat4_lr"


class EmstatPotentiostat(PotentiostatInstrument):
    """Driver for PalmSens EmStat potentiostats via hardpotato.

    Parameters
    ----------
    port:
        Serial device (e.g. ``/dev/ttyACM1``). Required for online runs;
        hardpotato's port auto-detection is deliberately not used.
    model:
        hardpotato model key. ``emstat4_lr`` (EmStat4 low-range) by default;
        ``emstatpico`` is the other EmStat option.
    data_dir:
        Directory where hardpotato writes its MethodSCRIPT and data files.
        Defaults to a fresh temp directory created at connect().
    offline:
        When True, hardware calls are replaced with deterministic synthetic
        traces. Useful for dry-running protocols without a device attached.
    """

    vendor: str = _VENDOR

    def __init__(
        self,
        port: str = "",
        model: str = DEFAULT_MODEL,
        data_dir: Optional[str] = None,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = False,
        **kwargs: Any,
    ):
        super().__init__(
            name=name,
            offset_x=offset_x,
            offset_y=offset_y,
            depth=depth,
            offline=offline,
        )
        if not model:
            raise PotentiostatConfigError("model must be a non-empty string")
        self._port = port
        self._model = model
        self._data_dir = data_dir

        # Populated by connect() when online.
        self._hp: Optional[Any] = None
        self._setup: Optional[Any] = None
        self._folder: Optional[str] = None
        self._run_counter = 0

        # Fixed-seed RNG (seed=0) for reproducible offline synthesis.
        self._offline_rng = random.Random(0)

    # ── BaseInstrument interface ──────────────────────────────────────────

    def connect(self) -> None:
        if self._offline:
            self.logger.info("Potentiostat connected (offline)")
            return

        if not self._port:
            raise PotentiostatConfigError(
                "port is required for an online EmStat connection "
                "(hardpotato auto-detect is not supported)"
            )

        hp = _load_hardpotato()
        folder = self._data_dir or tempfile.mkdtemp(prefix="cubos_emstat_")
        try:
            setup = hp.potentiostat.Setup(
                self._model, ".", folder, port=self._port, verbose=0
            )
        except Exception as exc:
            raise PotentiostatConnectionError(
                f"hardpotato Setup('{self._model}') failed: {exc}"
            ) from exc

        try:
            connected = setup.check_connection()
        except Exception as exc:
            raise PotentiostatConnectionError(
                f"EmStat connection check on '{self._port}' failed: {exc}"
            ) from exc
        if not connected:
            raise PotentiostatConnectionError(
                f"No EmStat ({self._model}) responded on port '{self._port}'"
            )

        self._hp = hp
        self._setup = setup
        self._folder = folder
        self.logger.info(
            "Connected to EmStat (%s) on %s, data dir %s",
            self._model, self._port, folder,
        )

    def disconnect(self) -> None:
        if self._offline:
            self.logger.info("Potentiostat disconnected (offline)")
            return
        # hardpotato opens the serial port per run(); there is no persistent
        # handle to close — dropping the Setup is the whole teardown.
        self._hp = None
        self._setup = None
        self._folder = None
        self.logger.info("Disconnected from potentiostat")

    def health_check(self) -> bool:
        if self._offline:
            return True
        return self._setup is not None

    # ── Experiment methods ────────────────────────────────────────────────

    def run_OCP(self, params: OCPParams) -> OCPResult:
        if self._offline:
            return simulate_OCP(
                params, self._offline_rng, self.vendor, self._offline_metadata()
            )

        def build_OCP(hp: Any, stem: str) -> Any:
            return hp.potentiostat.OCP(
                ttot=params.duration_s,
                dt=params.sampling_interval_s,
                fileName=stem,
                header="OCP",
            )

        curves, metadata = self._run_technique("ocp", build_OCP)
        return OCPResult(
            time_s=_column(curves, 0),
            voltage_v=_column(curves, 1),
            sample_period_s=params.sampling_interval_s,
            duration_s=params.duration_s,
            vendor=self.vendor,
            metadata=metadata,
        )

    def run_CV(self, params: CVParams) -> CVResult:
        step_size_v = _validate_cv_params(params)
        if self._offline:
            return simulate_CV(
                params, self._offline_rng, self.vendor, self._offline_metadata()
            )

        def build_CV(hp: Any, stem: str) -> Any:
            return hp.potentiostat.CV(
                Eini=params.start_V,
                Ev1=params.vertex1_V,
                Ev2=params.vertex2_V,
                Efin=params.end_V,
                sr=params.scan_rate_V_per_s,
                dE=step_size_v,
                # hardpotato's EmStat script emits nscans(nSweeps - 1), while
                # MethodSCRIPT defines nscans as the number of full cycles.
                nSweeps=params.cycles + 1,
                fileName=stem,
                header="CV",
            )

        curves, metadata = self._run_technique("cv", build_CV)
        return CVResult(
            time_s=_column(curves, 0),
            voltage_v=_column(curves, 1),
            current_a=_column(curves, 2),
            scan_rate_v_s=params.scan_rate_V_per_s,
            step_size_v=step_size_v,
            cycles=params.cycles,
            vendor=self.vendor,
            metadata=metadata,
        )

    def run_CA(self, params: CAParams) -> CAResult:
        if self._offline:
            return simulate_CA(
                params, self._offline_rng, self.vendor, self._offline_metadata()
            )

        def build_CA(hp: Any, stem: str) -> Any:
            return hp.potentiostat.CA(
                Estep=params.potential_V,
                dt=params.sampling_interval_s,
                ttot=params.duration_s,
                fileName=stem,
                header="CA",
            )

        curves, metadata = self._run_technique("ca", build_CA)
        return CAResult(
            time_s=_column(curves, 0),
            voltage_v=_column(curves, 1),
            current_a=_column(curves, 2),
            sample_period_s=params.sampling_interval_s,
            duration_s=params.duration_s,
            step_potential_v=params.potential_V,
            vendor=self.vendor,
            metadata=metadata,
        )

    def run_CP(self, params: CPParams) -> CPResult:
        raise NotImplementedError(
            "hardpotato exposes no chronopotentiometry technique for EmStat "
            "devices; use the 'admiral' vendor for CP"
        )

    # ── Shared online plumbing ────────────────────────────────────────────

    def _run_technique(
        self,
        technique: str,
        build: Callable[[Any, str], Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """Run one hardpotato technique to completion and return its curves.

        ``build(hp, stem)`` constructs the technique object; ``run()`` blocks
        on the serial exchange and leaves the parsed MethodSCRIPT curves on
        ``.data``.
        """
        if self._hp is None:
            raise PotentiostatCommandError(
                "Potentiostat is not connected; call connect() first."
            )

        self._run_counter += 1
        stem = f"{technique}_{self._run_counter:04d}"
        started_at = datetime.now(timezone.utc)
        try:
            experiment = build(self._hp, stem)
            # hardpotato only sets .port on techniques when auto-detecting;
            # with an explicit Setup port, run() would hit AttributeError.
            experiment.port = self._port
            experiment.run()
        except Exception as exc:
            raise PotentiostatCommandError(
                f"EmStat {technique.upper()} run failed: {exc}"
            ) from exc
        stopped_at = datetime.now(timezone.utc)

        curves = getattr(experiment, "data", None)
        if not curves:
            raise PotentiostatCommandError(
                f"EmStat {technique.upper()} returned no data packages"
            )

        metadata = {
            "device_id": f"{self._model}@{self._port}",
            "model": self._model,
            "port": self._port,
            "started_at": started_at.isoformat(),
            "stopped_at": stopped_at.isoformat(),
            "aborted": False,
            "stop_reason": None,
            "data_file": f"{self._folder}/{stem}.txt",
        }
        return curves, metadata

    def _offline_metadata(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        return {
            "device_id": "offline",
            "model": self._model,
            "port": self._port,
            "started_at": now,
            "stopped_at": now,
            "aborted": False,
            "stop_reason": None,
        }


def _column(curves: list[Any], column: int) -> tuple[float, ...]:
    """Flatten one variable column out of MethodSCRIPT curves."""
    try:
        return tuple(
            float(row[column].value) for curve in curves for row in curve
        )
    except (IndexError, AttributeError, TypeError, ValueError) as exc:
        raise PotentiostatCommandError(
            f"Unexpected EmStat data package shape (column {column}): {exc}"
        ) from exc


def _validate_cv_params(params: CVParams) -> float:
    """Validate CV settings against hardpotato's millivolt quantization.

    The EmStat MethodSCRIPT generator truncates voltage and rate settings to
    integer millivolts/millivolts-per-second.  It also ignores ``Efin`` and
    uses ``nSweeps - 1`` for the script's full-cycle count.
    """
    values = {
        "start_V": params.start_V,
        "vertex1_V": params.vertex1_V,
        "vertex2_V": params.vertex2_V,
        "end_V": params.end_V,
        "scan_rate_V_per_s": params.scan_rate_V_per_s,
        "sampling_interval_s": params.sampling_interval_s,
    }
    for name, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise PotentiostatConfigError(f"{name} must be finite, got {value}")
    if not isinstance(params.cycles, int) or isinstance(params.cycles, bool):
        raise PotentiostatConfigError(
            f"cycles must be an integer in [1, 9999], got {params.cycles!r}"
        )
    if not 1 <= params.cycles <= 9999:
        raise PotentiostatConfigError(
            f"cycles must be an integer in [1, 9999], got {params.cycles}"
        )
    if params.end_V != params.start_V:
        raise PotentiostatConfigError(
            "EmStat CV requires end_V == start_V; hardpotato ignores Efin"
        )

    step_size_v = params.scan_rate_V_per_s * params.sampling_interval_s
    if not math.isfinite(step_size_v) or step_size_v < 0.001:
        raise PotentiostatConfigError(
            "CV step size must be at least 0.001 V for EmStat millivolt output; "
            f"got {step_size_v} V"
        )
    if params.scan_rate_V_per_s < 0.001:
        raise PotentiostatConfigError(
            "scan_rate_V_per_s must be at least 0.001 V/s for EmStat "
            f"millivolt-per-second output; got {params.scan_rate_V_per_s}"
        )
    if int(params.vertex1_V * 1000) == int(params.vertex2_V * 1000):
        raise PotentiostatConfigError(
            "vertex1_V and vertex2_V must differ after EmStat millivolt "
            "quantization"
        )
    return step_size_v


def _load_hardpotato() -> Any:
    """Lazy-import hardpotato with an actionable install hint."""
    try:
        import hardpotato
        import hardpotato.potentiostat  # noqa: F401  (some versions lazy-load submodules)
    except ImportError as exc:
        raise PotentiostatConnectionError(
            "hardpotato is not installed. "
            "Install with: pip install 'cubos[potentiostat-emstat]'"
        ) from exc
    return hardpotato
