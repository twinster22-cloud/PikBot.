"""Read-only prospective pick audit. No model fitting, betting, or win guarantees.

Usage: python accuracy_audit.py settled_picks.csv
Required columns: issued_at, event_id, sport, player, stat, line, side,
                   actual, p_raw
Optional: platform, model_version, payout_net_per_unit
All rows must be predictions recorded before the event, not reconstructed winners.
"""
import argparse
import math
import pandas as pd

REQUIRED = {"issued_at", "event_id", "sport", "player", "stat", "line", "side", "actual", "p_raw"}

def audit(df):
    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError("Missing columns: " + ", ".join(sorted(missing)))
    x = df.copy()
    x["issued_at"] = pd.to_datetime(x["issued_at"], utc=True, errors="coerce")
    for c in ("line", "actual", "p_raw"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x["side"] = x["side"].astype(str).str.upper()
    if x["issued_at"].isna().any():
        raise ValueError("Invalid issued_at timestamps")
    if not x["p_raw"].between(0, 1).all():
        raise ValueError("Probabilities must be in [0,1]")
    if not x["side"].isin(["MORE", "LESS"]).all():
        raise ValueError("Side must be MORE or LESS")
    if x[["line", "actual"]].isna().any().any():
        raise ValueError("Only settled picks with numeric actuals are accepted")
    key = ["event_id", "player", "stat", "line", "side"]
    x = x.sort_values("issued_at").drop_duplicates(key, keep="first")
    x["result"] = "LOSS"
    x.loc[x.actual.eq(x.line), "result"] = "PUSH"
    x.loc[((x.actual > x.line) & x.side.eq("MORE")) |
          ((x.actual < x.line) & x.side.eq("LESS")), "result"] = "WIN"
    settled = x[x.result.ne("PUSH")].copy()
    settled["hit"] = settled.result.eq("WIN").astype(int)
    settled["brier"] = (settled.p_raw - settled.hit) ** 2
    def summarize(g):
        n = len(g)
        if not n:
            return {"n": 0, "wins": 0, "win_rate": None, "mean_estimate": None, "brier": None}
        return {"n": n, "wins": int(g.hit.sum()),
                "win_rate": round(float(g.hit.mean()), 4),
                "mean_estimate": round(float(g.p_raw.mean()), 4),
                "brier": round(float(g.brier.mean()), 4)}
    groups = {}
    for field in ("sport", "stat", "platform", "model_version"):
        if field in settled.columns:
            groups[field] = {str(k): summarize(g) for k, g in settled.groupby(field, dropna=False)}
    # Calibration buckets are diagnostic, never proof of profitability.
    settled["confidence_bucket"] = pd.cut(settled.p_raw, [0, .5, .6, .7, .8, .9, 1],
        include_lowest=True).astype(str)
    groups["confidence_bucket"] = {
        str(k): summarize(g) for k, g in settled.groupby("confidence_bucket")}
    return {"overall": summarize(settled), "pushes": int(x.result.eq("PUSH").sum()),
            "unique_predictions": len(x), "groups": groups,
            "warning": "Observed results are not a guarantee. Check timestamps, selection bias, payouts and correlations. Do not claim 75% accuracy from a small sample."}

if __name__ == "__main__":
    import json
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv")
    args = p.parse_args()
    print(json.dumps(audit(pd.read_csv(args.csv)), indent=2, allow_nan=False))
