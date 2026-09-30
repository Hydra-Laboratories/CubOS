"""Supported Python client for the versioned CubOS appliance API."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

import requests


class StationRequestError(RuntimeError):
    """A CubOS appliance request failed or returned an unsuccessful run."""


@dataclass(frozen=True)
class ProtocolBundle:
    """The three YAML inputs required by a CubOS run resource."""

    gantry_config: str
    deck_config: str
    protocol_yaml: str

    @classmethod
    def from_paths(
        cls,
        *,
        gantry_config: Path,
        deck_config: Path,
        protocol_yaml: Path,
    ) -> "ProtocolBundle":
        return cls(
            gantry_config=read_text(gantry_config),
            deck_config=read_text(deck_config),
            protocol_yaml=read_text(protocol_yaml),
        )

    def as_payload(self) -> dict[str, str]:
        return {
            "gantry_config": self.gantry_config,
            "deck_config": self.deck_config,
            "protocol_yaml": self.protocol_yaml,
        }


class StationClient:
    """HTTP client for CubOS `/api/v1` discovery and run resources."""

    TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.api_token = api_token

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_token:
            return {}
        return {"Authorization": f"Bearer {self.api_token}"}

    def health(self, *, timeout: float) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/health",
            timeout=timeout,
            headers=self.headers,
        )

    def submit_run(
        self,
        bundle: ProtocolBundle,
        *,
        run_id: str,
        mock_mode: bool = False,
        metadata: dict[str, Any] | None = None,
        initial_state: dict[str, Any] | None = None,
        fluid_state_id: int | None = None,
        timeout: float,
    ) -> dict[str, Any]:
        """Submit a run. ``initial_state``/``fluid_state_id`` are mutually
        exclusive: pass at most one to attach fluid-state tracking (a new
        state seeded from ``initial_state``, an ``initial-fluids``-style
        mapping — or an existing ``fluid_state_id`` to resume). Passing
        neither keeps the run stateless, exactly like every run submitted
        before Feature 07."""
        if initial_state is not None and fluid_state_id is not None:
            raise StationRequestError(
                "submit_run accepts at most one of initial_state or fluid_state_id"
            )
        payload: dict[str, Any] = {
            "run_id": run_id,
            **bundle.as_payload(),
            "mock_mode": mock_mode,
            "metadata": metadata or {},
        }
        if initial_state is not None:
            payload["state"] = {"initial_state": initial_state}
        elif fluid_state_id is not None:
            payload["state"] = {"fluid_state_id": fluid_state_id}
        return request_json(
            self.session,
            "POST",
            f"{self.base_url}/api/v1/runs",
            payload=payload,
            timeout=timeout,
            headers=self.headers,
            expected_statuses={202},
        )

    def get_run(self, run_id: str, *, timeout: float) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/runs/{run_id}",
            timeout=timeout,
            headers=self.headers,
        )

    def wait_for_run(
        self,
        run_id: str,
        *,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StationRequestError(f"run {run_id!r} did not finish within {timeout}s")
            record = self.get_run(run_id, timeout=min(remaining, 15.0))
            state = record.get("state")
            if state in self.TERMINAL_STATES:
                return record
            time.sleep(min(poll_interval, max(0.0, remaining)))

    def run_protocol(
        self,
        bundle: ProtocolBundle,
        *,
        run_id: str,
        mock_mode: bool = False,
        metadata: dict[str, Any] | None = None,
        timeout: float,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        started = time.monotonic()
        self.submit_run(
            bundle,
            run_id=run_id,
            mock_mode=mock_mode,
            metadata=metadata,
            timeout=min(timeout, 30.0),
        )
        response = self.wait_for_run(
            run_id,
            timeout=max(0.0, timeout - (time.monotonic() - started)),
            poll_interval=poll_interval,
        )
        if response.get("state") != "succeeded":
            raise StationRequestError(
                f"CubOS run {run_id!r} ended as {response.get('state')!r}: "
                f"{response.get('error') or response!r}"
            )
        return response

    def cancel_run(self, run_id: str, *, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "POST",
            f"{self.base_url}/api/v1/runs/{run_id}/cancel",
            payload={},
            timeout=timeout,
            headers=self.headers,
            expected_statuses={202},
        )

    def events(self, run_id: str, *, after: int = 0, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/runs/{run_id}/events?after={after}",
            timeout=timeout,
            headers=self.headers,
        )

    def artifacts(self, run_id: str, *, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/runs/{run_id}/artifacts",
            timeout=timeout,
            headers=self.headers,
        )

    # ── Fluid/tip/cap state (Feature 07) ────────────────────────────────

    def create_fluid_state(
        self,
        *,
        deck_file: str,
        label: str | None = None,
        fluids: dict[str, dict[str, Any]] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"deck_file": deck_file}
        if label is not None:
            payload["label"] = label
        if fluids is not None:
            payload["fluids"] = fluids
        return request_json(
            self.session,
            "POST",
            f"{self.base_url}/api/v1/fluid-states",
            payload=payload,
            timeout=timeout,
            headers=self.headers,
            expected_statuses={201},
        )

    def list_fluid_states(self, *, timeout: float = 15.0) -> list[dict[str, Any]]:
        return request_json_list(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states",
            timeout=timeout,
            headers=self.headers,
        )

    def get_fluid_state(self, fluid_state_id: int, *, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}",
            timeout=timeout,
            headers=self.headers,
        )

    def get_fluid_containers(
        self, fluid_state_id: int, *, timeout: float = 15.0
    ) -> list[dict[str, Any]]:
        return request_json_list(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/containers",
            timeout=timeout,
            headers=self.headers,
        )

    def get_tip_state(self, fluid_state_id: int, *, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/tips",
            timeout=timeout,
            headers=self.headers,
        )

    def get_cap_state(self, fluid_state_id: int, *, timeout: float = 15.0) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/caps",
            timeout=timeout,
            headers=self.headers,
        )

    def get_pending_operations(
        self, fluid_state_id: int, *, pending_only: bool = True, timeout: float = 15.0
    ) -> dict[str, Any]:
        query = "?pending_only=true" if pending_only else "?pending_only=false"
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/operations{query}",
            timeout=timeout,
            headers=self.headers,
        )

    def get_reconciliation_items(
        self, fluid_state_id: int, *, timeout: float = 15.0
    ) -> dict[str, Any]:
        return request_json(
            self.session,
            "GET",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/reconciliation",
            timeout=timeout,
            headers=self.headers,
        )

    def resolve_reconciliation(
        self,
        fluid_state_id: int,
        *,
        domain: str,
        operation_key: str,
        resolution: str,
        operator: str,
        reason: str,
        source_volume_ul: float | None = None,
        source_composition: dict[str, float] | None = None,
        destination_volume_ul: float | None = None,
        destination_composition: dict[str, float] | None = None,
        final_slot_status: str | None = None,
        final_status: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Resolve a pending reconciliation with an auditable operator
        decision. ``operator``/``reason`` are recorded into the journal's
        ``detail`` field — this is the "who/what/why" audit trail."""
        payload: dict[str, Any] = {
            "domain": domain,
            "operation_key": operation_key,
            "resolution": resolution,
            "operator": operator,
            "reason": reason,
            "source_volume_ul": source_volume_ul,
            "source_composition": source_composition,
            "destination_volume_ul": destination_volume_ul,
            "destination_composition": destination_composition,
            "final_slot_status": final_slot_status,
            "final_status": final_status,
        }
        return request_json(
            self.session,
            "POST",
            f"{self.base_url}/api/v1/fluid-states/{fluid_state_id}/reconciliation/resolve",
            payload=payload,
            timeout=timeout,
            headers=self.headers,
        )


def read_text(path: Path) -> str:
    return path.expanduser().resolve().read_text()


def metadata_from_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise StationRequestError(f"--metadata-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise StationRequestError("--metadata-json must decode to a JSON object")
    return parsed


def api_token_from_sources(token_file: Path | None = None) -> str | None:
    """Read a device token from a file or environment without CLI exposure."""
    if token_file is not None:
        token = token_file.expanduser().read_text(encoding="utf-8").strip()
        if not token:
            raise StationRequestError(f"API token file is empty: {token_file}")
        return token
    return os.environ.get("CUBOS_API_TOKEN") or None


def print_json(label: str, payload: Any) -> None:
    print(json.dumps({label: payload}, indent=2, sort_keys=True))


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float,
    headers: dict[str, str] | None = None,
    expected_statuses: set[int] | None = None,
) -> dict[str, Any]:
    try:
        response = session.request(
            method,
            url,
            json=payload,
            timeout=timeout,
            headers=headers or {},
        )
    except requests.RequestException as exc:
        raise StationRequestError(f"{method} {url} failed: {exc}") from exc
    return decode_json(response, url, expected_statuses=expected_statuses)


def decode_json(
    response: requests.Response,
    url: str,
    *,
    expected_statuses: set[int] | None = None,
) -> dict[str, Any]:
    accepted = expected_statuses or set(range(200, 300))
    if response.status_code not in accepted:
        raise StationRequestError(f"{url} -> HTTP {response.status_code}: {safe_body(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise StationRequestError(f"{url} -> non-JSON response: {response.text[:200]!r}") from exc
    if not isinstance(data, dict):
        raise StationRequestError(f"{url} -> JSON response is not an object: {data!r}")
    return data


def request_json_list(
    session: requests.Session,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float,
    headers: dict[str, str] | None = None,
    expected_statuses: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Like ``request_json``, for endpoints whose body is a JSON array
    (e.g. ``GET /api/v1/fluid-states``) rather than a JSON object."""
    try:
        response = session.request(
            method,
            url,
            json=payload,
            timeout=timeout,
            headers=headers or {},
        )
    except requests.RequestException as exc:
        raise StationRequestError(f"{method} {url} failed: {exc}") from exc
    accepted = expected_statuses or set(range(200, 300))
    if response.status_code not in accepted:
        raise StationRequestError(f"{url} -> HTTP {response.status_code}: {safe_body(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise StationRequestError(f"{url} -> non-JSON response: {response.text[:200]!r}") from exc
    if not isinstance(data, list):
        raise StationRequestError(f"{url} -> JSON response is not an array: {data!r}")
    return data


def safe_body(response: requests.Response) -> str:
    try:
        return str(response.json())
    except ValueError:
        return response.text[:300]
