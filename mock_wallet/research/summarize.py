"""Summarise results/results.csv: what survives out of sample?"""

from __future__ import annotations

import pandas as pd

from evaluate import OUT, PROFILES

pd.set_option("display.width", 200)
pd.set_option("display.max_rows", 200)


def pct(x):
    return f"{x:+.0%}"


def main():
    df = pd.read_csv(OUT / "results.csv")
    bench = pd.read_csv(OUT / "benchmarks.csv")
    print("== Benchmarks (buy and hold)")
    print(bench.to_string(index=False, formatters={c: pct for c in
                                                   ["memes_equal_weight", "memes_median", "btc"]}))

    wide = df.pivot_table(index=["strategy", "params", "profile"], columns="period",
                          values=["ret", "trades", "win_rate", "avg_trade", "max_dd"],
                          aggfunc="first")
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reset_index()

    print("\n== How many configs make money?  (of", wide.groupby("profile").size().iloc[0], ")")
    for prof in PROFILES:
        w = wide[wide.profile == prof]
        tr, te = w.ret_train > 0, w.ret_test > 0
        both = (tr & te).sum()
        print(f"  {prof:16s} train>0: {tr.sum():3d}   test>0: {te.sum():3d}   both: {both:3d}"
              f"   test>0 given train>0: {both}/{tr.sum()}")

    print("\n== Best params per strategy picked on TRAIN, scored on TEST")
    for prof in PROFILES:
        w = wide[wide.profile == prof]
        best = w.loc[w.groupby("strategy").ret_train.idxmax()].sort_values("ret_test",
                                                                           ascending=False)
        print(f"\n-- {prof}")
        print(best[["strategy", "params", "ret_train", "ret_test", "trades_test",
                    "win_rate_test", "avg_trade_test", "max_dd_test"]].to_string(
            index=False, formatters={"ret_train": pct, "ret_test": pct,
                                     "win_rate_test": "{:.0%}".format,
                                     "avg_trade_test": "{:+.2%}".format,
                                     "max_dd_test": "{:.0%}".format}))

    print("\n== Profitable in BOTH years (candidates)")
    cand = wide[(wide.ret_train > 0) & (wide.ret_test > 0) & (wide.profile != "no costs")]
    print(cand.sort_values(["profile", "ret_test"], ascending=[True, False])[
        ["profile", "strategy", "params", "ret_train", "ret_test", "trades_test",
         "max_dd_test"]].to_string(index=False, formatters={
            "ret_train": pct, "ret_test": pct, "max_dd_test": "{:.0%}".format}))
    cand.to_csv(OUT / "candidates.csv", index=False)


if __name__ == "__main__":
    main()
