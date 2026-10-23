#!/usr/bin/env python3
"""Evaluate alternative models that use the FULL engineered feature matrix.

SARIMAX and ARIMA previously used only 4 exogenous features; they now take all
~93 (scaled, de-collinearized). NeuralProphet now takes a configurable regressor
set. This script runs each via walk-forward CV and compares MAPE to a LightGBM
baseline, so the additions are actually exercised.

Classical/NN models are slow, so by default we evaluate on the last few folds.

Run (project env + edfs.db):
    python3 scripts/eval_classical.py sarimax arima            # daily, 3 folds
    python3 scripts/eval_classical.py sarimax --resolution hourly --folds 2
    python3 scripts/eval_classical.py neuralprophet lightgbm   # needs torch
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.db.session import get_session
from src.features.pipeline import FeaturePipeline
from src.evaluation.cross_validation import generate_folds
from src.evaluation.metrics import compute_all_metrics

SUB_REGIONAL_COLS = ["brpl_mw", "bypl_mw", "ndpl_mw", "ndmc_mw", "mes_mw"]
TARGET = "delhi_mw"
logging.basicConfig(level=logging.ERROR, format="%(message)s")


def make_model(name: str, resolution: str):
    if name == "lightgbm":
        from src.models.lightgbm_model import LightGBMForecaster
        return LightGBMForecaster(resolution=resolution)
    if name == "sarimax":
        from src.models.sarimax_model import SARIMAXForecaster
        return SARIMAXForecaster(resolution=resolution, exog="all")
    if name == "arima":
        from src.models.sarimax_model import ARIMAForecaster
        return ARIMAForecaster(resolution=resolution, exog="all")
    if name == "neuralprophet":
        from src.models.neuralprophet_model import NeuralProphetForecaster
        return NeuralProphetForecaster(resolution=resolution, epochs=80)
    raise ValueError(f"unknown model {name}")


def build_features(resolution: str):
    with get_session() as session:
        pipe = FeaturePipeline(resolution, session)
        df = pipe.build()
        feats = pipe.get_feature_names(df)
    feats = [f for f in feats if f not in SUB_REGIONAL_COLS]
    null_pct = df[feats].isnull().mean()
    feats = [f for f in feats if null_pct.get(f, 0) <= 0.1]
    df[feats] = df[feats].ffill().fillna(0)
    return df, feats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*", default=["sarimax", "arima"])
    ap.add_argument("--resolution", default="daily")
    ap.add_argument("--folds", type=int, default=3, help="evaluate the last N folds")
    args = ap.parse_args()
    models = args.models or ["sarimax", "arima"]

    print(f"\n{'#' * 66}")
    print(f"  Classical/alt model eval ({args.resolution}) — full feature matrix")
    print(f"{'#' * 66}")

    df, feats = build_features(args.resolution)
    print(f"  Rows: {len(df):,} | features: {len(feats)} | "
          f"range {df.index.min().date()}..{df.index.max().date()}")

    folds = generate_folds(len(df), args.resolution)
    folds = folds[-args.folds:] if args.folds else folds
    print(f"  Evaluating on {len(folds)} folds\n")

    results = {}
    for name in models:
        maes, mapes, t0 = [], [], time.time()
        print(f"  == {name} ==", flush=True)
        for fold in folds:
            tr = df.iloc[fold.train_start:fold.train_end]
            va = df.iloc[fold.val_start:fold.val_end]
            try:
                model = make_model(name, args.resolution)
                model.fit(tr[feats], tr[TARGET])
                pred = model.predict(va[feats])
                m = compute_all_metrics(va[TARGET].values, np.asarray(pred, dtype=float))
                mapes.append(m["mape"]); maes.append(m["mae"])
                print(f"     fold {fold.fold_idx + 1}: MAPE {m['mape']:.2f}%  "
                      f"({va.index[0].date()}..{va.index[-1].date()})", flush=True)
            except Exception as exc:
                print(f"     fold {fold.fold_idx + 1}: FAILED ({type(exc).__name__}: {exc})", flush=True)
        if mapes:
            results[name] = (float(np.mean(mapes)), float(np.mean(maes)), time.time() - t0)
            print(f"     -> mean MAPE {results[name][0]:.2f}%  "
                  f"(mean MAE {results[name][1]:.0f} MW, {results[name][2]:.0f}s)\n")
        else:
            print(f"     -> no successful folds\n")

    if results:
        print(f"{'=' * 66}\n  SUMMARY (mean CV MAPE, last {len(folds)} folds)\n{'=' * 66}")
        for name, (mape, mae, secs) in sorted(results.items(), key=lambda kv: kv[1][0]):
            print(f"  {name:<14} {mape:6.2f}%   MAE {mae:7.0f} MW   {secs:5.0f}s")


if __name__ == "__main__":
    main()
