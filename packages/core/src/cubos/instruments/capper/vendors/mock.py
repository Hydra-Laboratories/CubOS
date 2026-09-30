"""Offline mock capper/decapper vendor for dry runs and tests."""

from __future__ import annotations

from typing import List, Optional

from cubos.instruments.capper.interface import CapperInstrument
from cubos.instruments.capper.models import CapperStatus


class MockCapper(CapperInstrument):
    """Deterministic in-memory capper simulation.

    No serial I/O of any kind -- ``capture_cap``/``release_cap`` flip an
    in-memory flag that ``read_cap_present`` reports back immediately.
    ``actuation_log`` records every call in order, which protocol-command
    tests use to assert the approach/engage/capture/retract sequence
    without needing to inspect gantry internals. Tests that need to inject a
    failure (serial timeout, contradictory sensor, mid-motion failure)
    monkeypatch ``capture_cap``/``release_cap``/``read_cap_present`` directly
    on an instance of this class.
    """

    def __init__(
        self,
        *,
        engage_depth_mm: float,
        capture_retries: int = 2,
        capture_settle_s: float = 0.0,
        name: Optional[str] = None,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        depth: float = 0.0,
        offline: bool = True,
        **kwargs,
    ):
        super().__init__(
            engage_depth_mm=engage_depth_mm,
            capture_retries=capture_retries,
            capture_settle_s=capture_settle_s,
            name=name, offset_x=offset_x, offset_y=offset_y,
            depth=depth, offline=offline,
        )
        self._cap_present = False
        self.actuation_log: List[str] = []

    def connect(self) -> None:
        self.logger.info("MockCapper connected (offline)")

    def disconnect(self) -> None:
        self.logger.info("MockCapper disconnected (offline)")

    def health_check(self) -> bool:
        return True

    def capture_cap(self) -> None:
        self.actuation_log.append("capture_cap")
        self._cap_present = True

    def release_cap(self) -> None:
        self.actuation_log.append("release_cap")
        self._cap_present = False

    def read_cap_present(self) -> bool:
        self.actuation_log.append("read_cap_present")
        return self._cap_present

    def get_status(self) -> CapperStatus:
        return CapperStatus(cap_present=self._cap_present)
