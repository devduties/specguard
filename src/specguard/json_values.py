"""Decode JSON values without accepting nonfinite numbers."""

import json
import math
from typing import Any


def parse_json(text: str) -> Any:
    return json.loads(text, parse_constant=_invalid_constant, parse_float=_finite_float)


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"Invalid JSON constant: {value}.")


def _finite_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("JSON number exceeds the supported finite range.")
    return value
