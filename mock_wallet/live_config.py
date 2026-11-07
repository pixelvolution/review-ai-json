"""The live trading config: which strategy runs, with which parameters.

This is the only config the engine reads. It's a plain JSON file
(``live_config.json``) and it only changes when a person runs
``python set_config.py``, which logs every change. A hand edit of the file is
detected and logged too.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / "live_config.json"
sys.path.insert(0, str(HERE / "research"))


def config_path(path=None) -> Path:
    return Path(path or os.environ.get("MOCK_WALLET_LIVE_CONFIG") or DEFAULT_PATH)


def load(path=None) -> dict:
    return json.loads(config_path(path).read_text())


def sha(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:16]


def write(cfg: dict, path=None) -> None:
    config_path(path).write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")


class InvalidParameter(ValueError):
    pass


def tunable_parameters(cfg: dict) -> list[str]:
    """Strategy parameters that may be changed: ``params.<name>``.

    Costs, the kill switch, the vault and the validation rules are not
    tunable. They describe reality or protect the account, so a proposed change
    can't loosen them.
    """
    from strategies import STRATEGIES
    fn = STRATEGIES[cfg["strategy"]][0]
    names = [n for n in inspect.signature(fn).parameters if n not in ("p", "btc", "seed")]
    return [f"params.{n}" for n in names]


def normalize_parameter(cfg: dict, parameter: str) -> str:
    name = parameter if parameter.startswith("params.") else f"params.{parameter}"
    if name not in tunable_parameters(cfg):
        raise InvalidParameter(
            f"'{parameter}' is not tunable for strategy '{cfg['strategy']}'. "
            f"Tunable: {', '.join(tunable_parameters(cfg))}")
    return name


def coerce_value(cfg: dict, parameter: str, value):
    """Validate a proposed value: a positive number (integer where the current
    value is an integer), or null to switch an optional rule off."""
    name = normalize_parameter(cfg, parameter)
    key = name.split(".", 1)[1]
    current = cfg["params"].get(key)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("null", "none", ""):
            value = None
        else:
            try:
                value = json.loads(v)
            except json.JSONDecodeError:
                raise InvalidParameter(f"value {value!r} is not a number or null") from None
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InvalidParameter(f"value {value!r} is not a finite number or null")
    if value <= 0:
        raise InvalidParameter(f"value {value!r} must be > 0")
    if isinstance(current, int) and not isinstance(current, bool):
        if float(value) != int(value):
            raise InvalidParameter(f"{name} is an integer parameter")
        return int(value)
    return float(value)


def with_override(cfg: dict, parameter: str, value) -> dict:
    """A copy of ``cfg`` with one parameter changed. Never writes anything."""
    name = normalize_parameter(cfg, parameter)
    out = copy.deepcopy(cfg)
    out["params"][name.split(".", 1)[1]] = coerce_value(cfg, name, value)
    return out
