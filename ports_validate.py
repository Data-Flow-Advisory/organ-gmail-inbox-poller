#!/usr/bin/env python3
"""
Ports conformance validator for organ-gmail-inbox-poller.

Implements the port check the Connection Standard adds to conformance
(CONNECTORS.md, "Conformance gains a port check"):

  1. ``ports.json`` parses and has the right shape.
  2. Every declared port ``type`` exists in the shared vocabulary
     (``types.json``, vendored here from the orchestrator).
  3. ``decide`` actually **reads** each declared input ``name`` under ``state``
     and **writes** each declared output ``name`` under ``output`` — sampled
     against the organ's own committed samples.

Reads are proven structurally: ``state`` is wrapped in a dict that records
every key access, decide() is run on a real sample, and each declared input
name must show up in the access log. Writes are proven by inspecting the
``output`` dict decide() returns. This makes the manifest a *checked* claim,
not documentation — a port that decide() doesn't touch fails CI.

Exit 0 = conformant, non-zero = a specific violation (printed).
Usable both from pytest (``validate()`` returns a report) and from the
conformance workflow (``python3 ports_validate.py``).
"""

from __future__ import annotations

import glob
import json
import os
import sys

import organ

_HERE = os.path.dirname(os.path.abspath(__file__))
_PORTS = os.path.join(_HERE, "ports.json")
_TYPES = os.path.join(_HERE, "types.json")
_SAMPLES_DIR = os.path.join(_HERE, "samples")


class _TrackingDict(dict):
    """A dict that records every key read via __getitem__ / .get / 'in'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads: set = set()

    def __getitem__(self, key):
        self.reads.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.reads.add(key)
        return super().__contains__(key)


def _load_json(path: str):
    with open(path) as fh:
        return json.load(fh)


def _pick_sample_state():
    """A well-formed sample state that exercises the keep path (so every
    declared input name is genuinely reachable). Falls back to any sample."""
    paths = sorted(glob.glob(os.path.join(_SAMPLES_DIR, "*.json")))
    if not paths:
        raise SystemExit("ports_validate: no samples/*.json to exercise decide()")
    # Prefer a sample whose state carries the input keys non-trivially.
    preferred = os.path.join(_SAMPLES_DIR, "kept_thread.json")
    chosen = preferred if os.path.exists(preferred) else paths[0]
    payload = _load_json(chosen)
    return payload["state"], payload.get("context")


def validate() -> list:
    """Run the port checks. Returns a list of violation strings (empty == OK)."""
    violations: list = []

    # 1. ports.json parses + shape.
    try:
        ports = _load_json(_PORTS)
    except Exception as e:  # pragma: no cover - exercised via bad-file tests
        return [f"ports.json does not parse: {e}"]

    if not isinstance(ports, dict):
        return ["ports.json must be a JSON object"]
    inputs = ports.get("inputs", [])
    outputs = ports.get("outputs", [])
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        return ["ports.json 'inputs' and 'outputs' must be arrays"]
    if not outputs:
        violations.append("ports.json declares no outputs (an organ must produce something)")

    for label, ports_list in (("input", inputs), ("output", outputs)):
        for p in ports_list:
            if not isinstance(p, dict) or "name" not in p or "type" not in p:
                violations.append(
                    f"{label} port missing 'name'/'type': {p!r}"
                )

    # 2. Every type exists in the vocabulary.
    try:
        vocab = _load_json(_TYPES).get("types", {})
    except Exception as e:
        return violations + [f"types.json does not parse: {e}"]

    for label, ports_list in (("input", inputs), ("output", outputs)):
        for p in ports_list:
            t = p.get("type") if isinstance(p, dict) else None
            if t is not None and t not in vocab:
                violations.append(
                    f"{label} port {p.get('name')!r} declares type {t!r} "
                    f"which is not in the vocabulary (types.json)"
                )

    # 3. decide() reads each declared input name and writes each output name.
    state, context = _pick_sample_state()
    tracked = _TrackingDict(state)
    result = organ.decide(tracked, context)

    for p in inputs:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if name not in tracked.reads:
            violations.append(
                f"input port {name!r} is declared but decide() never reads "
                f"state[{name!r}] (read keys: {sorted(tracked.reads)})"
            )

    out = result.get("output", {}) if isinstance(result, dict) else {}
    if not isinstance(out, dict):
        violations.append("decide() did not return an 'output' object")
        out = {}
    for p in outputs:
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if name not in out:
            violations.append(
                f"output port {name!r} is declared but decide() never writes "
                f"output[{name!r}] (output keys: {sorted(out)})"
            )

    return violations


def main() -> int:
    violations = validate()
    if violations:
        print("PORTS CONFORMANCE: FAIL")
        for v in violations:
            print(f"  - {v}")
        return 1
    ports = _load_json(_PORTS)
    ins = ", ".join(f"{p['name']}:{p['type']}" for p in ports.get("inputs", []))
    outs = ", ".join(f"{p['name']}:{p['type']}" for p in ports.get("outputs", []))
    print("PORTS CONFORMANCE: OK")
    print(f"  inputs  -> {ins or '(none)'}")
    print(f"  outputs -> {outs or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
