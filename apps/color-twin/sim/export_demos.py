"""Export reproducible offline evidence for the three routing demos."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .demos import SAVED_USER_DECK, demos, get_demo
from .runtime import execute
from .server import compare_ordinary


def build_evidence() -> dict:
    bundles = {demo["id"]: demo for demo in demos()}
    evidence = {
        "saved_deck_sha256": hashlib.sha256(SAVED_USER_DECK.read_bytes()).hexdigest(),
        "demos": {},
        "ordinary_comparison": compare_ordinary(),
    }
    if evidence["ordinary_comparison"].get("same_liquid_outcome") is not True:
        raise RuntimeError("ordinary legacy/planned comparison did not match")
    for demo_id, bundle in bundles.items():
        result = execute(
            bundle["gantry_yaml"], bundle["deck_yaml"], bundle["protocol_yaml"],
            routing=bundle.get("routing"), initial_position=bundle.get("initial_position"),
        )
        route = result.get("route") or {}
        if bundle.get("routing", {}).get("planner_enabled"):
            if route.get("source") != "core-motion-plan" or route.get("parity") != {"segments_equal": True, "checked": True}:
                raise RuntimeError(f"{demo_id} did not produce verified core-plan parity")
        evidence["demos"][demo_id] = {"bundle": bundle, "result": result}
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="directory for JSON evidence")
    args = parser.parse_args()
    evidence = build_evidence()
    args.output.mkdir(parents=True, exist_ok=True)
    for demo_id, item in evidence["demos"].items():
        _write_inputs(args.output / demo_id, item["bundle"])
    comparison = evidence["ordinary_comparison"]
    _write_inputs(args.output / "ordinary-legacy", comparison["legacy_inputs"])
    _write_inputs(args.output / "ordinary-planned", comparison["planned_inputs"])
    (args.output / "routing-evidence.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(args.output / "routing-evidence.json")


def _write_inputs(directory: Path, inputs: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("gantry", "deck", "protocol"):
        (directory / f"{name}.yaml").write_text(inputs[f"{name}_yaml"])


if __name__ == "__main__":
    main()
