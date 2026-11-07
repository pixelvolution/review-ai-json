"""Honest fill model, shared by the research replay (Phase 3) and the paper
executor (Phase 4).

Rules:
  * A decision made at time t fills at the FIRST observation at or after
    t + delay_s. Nothing earlier is ever used to fill, so there is no look-ahead.
    If no observation arrives within ``max_fill_wait_s`` of that time, the fill
    fails (a data gap), never a guessed price.
  * Price impact is constant-product against the observation's liquidity: the
    pool's quote side holds liquidity/2. Buying V dollars gets V*R/(R+V) worth
    at the mid; selling tokens worth V gets V*R/(R+V). A drained pool pays ~0.
  * The DEX fee is charged on both legs; a flat tip is paid per transaction.
  * Honeypots: a deterministic share of entries (hash of seed, pair and entry
    time) can't be sold. The sale returns 0, but its tip is still paid.
  * Exits trigger on an observation (take-profit, stop-loss, time stop,
    liquidity collapse) and then fill after the delay like any decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Obs:
    t: float                     # unix seconds the observation was received
    price: float | None          # base token price, USD
    liquidity: float | None      # pool liquidity, USD (None = unknown)


@dataclass(frozen=True)
class FillModel:
    delay_s: float
    fee_rate: float
    tip_usd: float
    honeypot_rate: float
    honeypot_seed: int
    max_fill_wait_s: float


@dataclass(frozen=True)
class ExitRule:
    take_profit: float | None     # +0.5 = exit at +50%
    stop_loss: float | None       # 0.25 = exit at -25%
    time_stop_s: float | None
    liquidity_collapse: float | None  # 0.5 = exit if liquidity falls 50% below entry


@dataclass
class TradeResult:
    status: str                   # closed | no_entry_fill | no_exit_fill | no_liquidity
    entry_t: float | None = None
    entry_price: float | None = None
    entry_impact: float | None = None
    exit_t: float | None = None
    exit_price: float | None = None
    exit_impact: float | None = None
    exit_reason: str | None = None
    honeypot: bool = False
    spent: float | None = None        # notional + fee + tip
    proceeds: float | None = None     # after impact, fee and tip
    net: float | None = None          # proceeds - spent, USD
    price_only_net: float | None = None  # what the price chart alone suggested (no costs)


def is_honeypot(seed: int, pair: str, t: float, rate: float) -> bool:
    if rate <= 0:
        return False
    h = hashlib.sha256(f"{seed}|{pair}|{int(t)}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64 < rate


def quote_side(liquidity: float | None) -> float | None:
    return None if liquidity is None or liquidity <= 0 else liquidity / 2


def cp_value_after_impact(value_usd: float, liquidity: float) -> float:
    """Value (at the mid) actually received when trading `value_usd` into the pool."""
    r = quote_side(liquidity)
    if r is None:
        return 0.0
    return r * value_usd / (r + value_usd)


def fill_at(obs: list[Obs], t_decision: float, m: FillModel) -> Obs | None:
    """First priced observation at or after t_decision + delay, within the max wait."""
    due = t_decision + m.delay_s
    for o in obs:
        if o.t >= due and o.price:
            return o if o.t - due <= m.max_fill_wait_s else None
    return None


def exit_trigger(rule: ExitRule, entry_mid: float, entry_liq: float, entry_t: float,
                 o: Obs) -> str | None:
    """Does observation ``o`` trigger an exit? Shared by replay and live paper trading."""
    if not o.price:
        return None
    ret = o.price / entry_mid - 1
    if rule.take_profit is not None and ret >= rule.take_profit:
        return "take_profit"
    if rule.stop_loss is not None and ret <= -rule.stop_loss:
        return "stop_loss"
    if (rule.liquidity_collapse is not None and o.liquidity is not None and entry_liq
            and o.liquidity < entry_liq * (1 - rule.liquidity_collapse)):
        return "liquidity_collapse"
    if rule.time_stop_s is not None and o.t - entry_t >= rule.time_stop_s:
        return "time_stop"
    return None


def simulate(obs: list[Obs], t_signal: float, size_usd: float, m: FillModel, rule: ExitRule,
             pair: str) -> TradeResult:
    """One round trip. ``obs`` sorted by time; only observations after the signal matter."""
    obs = sorted(obs, key=lambda o: o.t)
    entry = fill_at(obs, t_signal, m)
    if entry is None:
        return TradeResult("no_entry_fill")
    if quote_side(entry.liquidity) is None:
        return TradeResult("no_liquidity", entry_t=entry.t)
    got_value = cp_value_after_impact(size_usd, entry.liquidity)
    tokens = got_value / entry.price
    spent = size_usd * (1 + m.fee_rate) + m.tip_usd
    hp = is_honeypot(m.honeypot_seed, pair, entry.t, m.honeypot_rate)
    res = TradeResult("closed", entry_t=entry.t, entry_price=entry.price,
                      entry_impact=1 - got_value / size_usd, honeypot=hp, spent=spent)

    trigger_t, reason = None, None
    for o in obs:
        if o.t <= entry.t or not o.price:
            continue
        reason = exit_trigger(rule, entry.price, entry.liquidity, entry.t, o)
        if reason:
            trigger_t = o.t
            break
    if trigger_t is None:
        later = [o for o in obs if o.t > entry.t and o.price]
        if not later:
            res.status = "no_exit_fill"
            return res
        exit_obs, reason = later[-1], "end_of_data"
    else:
        exit_obs = fill_at(obs, trigger_t, m)
        if exit_obs is None:
            res.status = "no_exit_fill"
            res.exit_reason = reason
            return res
    res.exit_t, res.exit_price, res.exit_reason = exit_obs.t, exit_obs.price, reason
    res.price_only_net = size_usd * (exit_obs.price / entry.price) - size_usd
    value = tokens * exit_obs.price
    if hp:
        res.proceeds = -m.tip_usd                   # the failed sale still pays its tip
        res.exit_impact = 1.0
    elif quote_side(exit_obs.liquidity) is None:
        res.status = "no_liquidity"                 # can't price the exit honestly
        return res
    else:
        received = cp_value_after_impact(value, exit_obs.liquidity)
        res.exit_impact = 1 - received / value if value else 1.0
        res.proceeds = received * (1 - m.fee_rate) - m.tip_usd
    res.net = res.proceeds - spent
    return res


def model_from_config(cfg: dict) -> FillModel:
    f = cfg["fills"]
    return FillModel(float(f["delay_s"]), float(f["fee_rate"]), float(f["tip_usd"]),
                     float(f["honeypot_rate"]), int(f["honeypot_seed"]),
                     float(f["max_fill_wait_s"]))
