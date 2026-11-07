"""Stress-test the one candidate (MACD + BTC regime filter): neighbouring
params, a different universe, the real 25% kill switch, and bootstrap odds."""
import numpy as np, pandas as pd
from multiprocessing import Pool
from data import MEMES, MAJORS, load
from engine import Panel, backtest
from evaluate import PROFILES, SPLIT
from strategies import macd
from wallet import WalletConfig

def job(args):
    uni, scale, regime, prof, kill = args
    syms = MEMES if uni == "memes" else MAJORS
    p = Panel.load(syms)
    btc = load("BTC").set_index("t")["close"].reindex(p.t)
    sp = macd(p, btc, scale=scale, regime=regime)
    sp.priority = p.vol.rolling(24).mean().fillna(0).to_numpy()
    k = int(np.searchsorted(p.t, SPLIT))
    out = {}
    for per, (a, b) in {"train": (0, k), "test": (k, len(p.t))}.items():
        r = backtest(p, sp, WalletConfig(max_drawdown=kill, **PROFILES[prof]), a, b, keep_curve=True)
        out[per] = r
    return args, out

def boot(curve, n=5000, block=7, horizon=365, seed=0):
    """Block-bootstrap daily returns of the test curve -> P(ending ahead after a year)."""
    d = pd.Series(curve).dropna().iloc[::24].pct_change().dropna().to_numpy()
    rng = np.random.default_rng(seed)
    ends = []
    for _ in range(n):
        idx = np.concatenate([np.arange(s, s + block) % len(d)
                              for s in rng.integers(0, len(d), horizon // block + 1)])[:horizon]
        ends.append(np.prod(1 + d[idx]))
    ends = np.array(ends)
    return (ends > 1).mean(), np.median(ends) - 1

if __name__ == "__main__":
    grid = [("memes", s, r, prof, 1) for s in (2, 3, 4, 5, 6) for r in (100, 200, 400)
            for prof in ("CEX 0.1%", "lean DEX 0.3%")]
    grid += [("majors", 4, 200, prof, 1) for prof in ("CEX 0.1%", "lean DEX 0.3%")]
    grid += [("memes", 4, 200, prof, 0.25) for prof in ("CEX 0.1%", "lean DEX 0.3%", "wallet defaults")]
    with Pool(4) as pool:
        res = pool.map(job, grid)
    rows = []
    for (uni, s, r, prof, kill), out in res:
        tr, te = out["train"], out["test"]
        pa, med = boot(te.curve) if kill == 1 else (np.nan, np.nan)
        rows.append(dict(universe=uni, scale=s, regime=r, costs=prof, kill_switch=kill < 1,
                         train=f"{tr.ret:+.0%}", test=f"{te.ret:+.0%}", test_dd=f"{te.max_dd:.0%}",
                         cooked=te.cooked or tr.cooked, trades=te.trades,
                         p_ahead_1y=pa, median_1y=med))
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False, formatters={"p_ahead_1y": lambda x: "" if pd.isna(x) else f"{x:.0%}",
                                                "median_1y": lambda x: "" if pd.isna(x) else f"{x:+.0%}"}))
    df.to_csv("results/robustness.csv", index=False)
