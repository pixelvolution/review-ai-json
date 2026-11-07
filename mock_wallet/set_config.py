"""Change the live config by hand. Every change is logged.

    python set_config.py scale 5 --reason "validated #3"
    python set_config.py --suggestion 3 --reason "evidence looks good"   # apply a suggestion
    python set_config.py --check     # log any hand edits made to live_config.json
    python set_config.py --log       # show the change log
"""

from __future__ import annotations

import argparse
import json
import sys

import control
import live_config


def set_param(parameter, value, reason, suggestion_id=None, force=False,
              db=None, live_config_path=None) -> dict:
    if not str(reason or "").strip():
        raise ValueError("--reason is required: every change is logged with why")
    control.check_unlogged_edits(db, live_config_path)
    cfg = live_config.load(live_config_path)
    if suggestion_id is not None:
        sug = control.get_suggestion(suggestion_id, db)
        if parameter is None:
            parameter, value = sug["parameter"], sug["proposed_value"]
        elif (live_config.normalize_parameter(cfg, parameter) != sug["parameter"]
              or live_config.coerce_value(cfg, parameter, value) != sug["proposed_value"]):
            raise ValueError(f"suggestion {suggestion_id} proposes {sug['parameter']} = "
                             f"{sug['proposed_value']}, not {parameter} = {value}")
        if sug["status"] != "validated" and not force:
            raise ValueError(f"suggestion {suggestion_id} is '{sug['status']}', not validated. "
                             f"Use --force to apply it anyway (that is logged too).")
    name = live_config.normalize_parameter(cfg, parameter)
    key = name.split(".", 1)[1]
    old = cfg["params"].get(key)
    new_cfg = live_config.with_override(cfg, name, value)
    new = new_cfg["params"][key]
    if old == new:
        raise ValueError(f"{name} is already {old}")
    live_config.write(new_cfg, live_config_path)
    note = reason
    if suggestion_id is not None:
        sug = control.get_suggestion(suggestion_id, db)
        if sug["status"] != "validated":
            note += f" [FORCED: suggestion status was '{sug['status']}']"
        control.set_status(suggestion_id, "applied", f"applied by hand: {reason}", db)
    control.log_config_change("set_config", new_cfg, name, old, new, note, suggestion_id, db)
    return new_cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Change the live config (logged).")
    ap.add_argument("parameter", nargs="?")
    ap.add_argument("value", nargs="?")
    ap.add_argument("--reason")
    ap.add_argument("--suggestion", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--check", action="store_true", help="log hand edits to the config file")
    ap.add_argument("--log", action="store_true", help="show the config change log")
    a = ap.parse_args(argv)
    try:
        if a.check:
            changed = control.check_unlogged_edits()
            print("hand edits logged: " + ", ".join(changed) if changed else "no unlogged edits")
            return 0
        if a.log:
            for c in control.config_changes():
                print(f"{c['ts']}  {c['source']:22s} {c['parameter'] or '':16s} "
                      f"{c['old_value']} -> {c['new_value']}  {c['reason'] or ''}")
            return 0
        if a.parameter is None and a.suggestion is None:
            ap.error("give PARAMETER VALUE or --suggestion ID")
        value = json.loads(a.value) if a.value not in (None, "null", "none") else None
        cfg = set_param(a.parameter, value, a.reason, a.suggestion, a.force)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"not changed: {e}", file=sys.stderr)
        return 1
    print(f"live config now: {json.dumps(cfg['params'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
