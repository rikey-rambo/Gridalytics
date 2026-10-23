"""SARIMAX / ARIMA forecasters with full exogenous-feature support.

Previously SARIMAX used only 4 hand-picked exogenous variables ("SARIMAX can't
handle 90+ features"). This version feeds the **whole engineered feature matrix**
(the ~93 features) as exogenous regressors, with the conditioning safeguards
that make that actually tractable:

  * numeric columns only, StandardScaled (fit on train);
  * drop zero-variance columns and near-duplicates (|corr| > `max_corr`), which
    is what otherwise makes the MLE singular with ~90 collinear regressors;
  * a fixed (p,d,q)(P,D,Q,m) order by default — `auto_arima` over 90 exog is
    impractically slow — with an optional `auto=True` path (needs pmdarima).

`ARIMAForecaster` is the non-seasonal special case (same exog handling).

Seasonal periods: m=7 for daily, m=24 for hourly. 5-min is refused (m=288).
Both fit the project's BaseForecaster interface, so they drop straight into
`walk_forward_cv` / the training harness.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from sklearn.preprocessing import StandardScaler

from .base import BaseForecaster

SEASONAL_PERIODS = {"daily": 7, "hourly": 24}

# sensible fixed orders when not using auto_arima
DEFAULT_ORDER = {"daily": (2, 1, 2), "hourly": (2, 1, 2)}
DEFAULT_SEASONAL = {"daily": (1, 0, 1, 7), "hourly": (1, 0, 1, 24)}


class SARIMAXForecaster(BaseForecaster):
    """SARIMAX with the full feature matrix as exogenous regressors."""

    def __init__(
        self,
        resolution: str,
        exog: str = "all",            # "all" = use every numeric feature; "key" = the old 4
        seasonal: bool = True,
        order: tuple | None = None,
        seasonal_order: tuple | None = None,
        auto: bool = False,           # auto_arima order search (needs pmdarima); slow with many exog
        max_corr: float = 0.995,      # drop one of any near-duplicate exog pair
        maxiter: int = 100,
        exclude_target_history: bool = True,  # drop delhi_mw_lag/diff/rolling columns
        target_prefix: str = "delhi_mw",
        name: str = "sarimax",
    ):
        super().__init__(resolution=resolution, name=name)
        if resolution == "5min":
            raise ValueError("SARIMAX is not feasible for 5-min resolution (m=288 too large)")
        self.exog_mode = exog
        self.seasonal = seasonal
        self.auto = auto
        self.max_corr = max_corr
        self.maxiter = maxiter
        self.exclude_target_history = exclude_target_history
        self.target_prefix = target_prefix
        self.seasonal_period = SEASONAL_PERIODS.get(resolution, 7)
        self.order = order or DEFAULT_ORDER.get(resolution, (2, 1, 2))
        if seasonal:
            self.seasonal_order = seasonal_order or DEFAULT_SEASONAL.get(resolution, (1, 0, 1, 7))
        else:
            self.seasonal_order = (0, 0, 0, 0)
        self.model_fit = None
        self.scaler = StandardScaler()
        self.feature_names: list[str] = []
        self._exog_cols: list[str] = []

    # -- exogenous selection -------------------------------------------------
    def _choose_exog_cols(self, X: pd.DataFrame) -> list[str]:
        if self.exog_mode == "key":
            return [c for c in ["temperature_2m", "CDD", "is_holiday", "is_weekend"]
                    if c in X.columns]
        # "all": every numeric column, minus degenerate/duplicate ones.
        num = X.select_dtypes(include=[np.number])
        # CRITICAL for linear models: drop target-history columns
        # (delhi_mw_lag_*, _diff_*, _rmean/_rstd/_rmin/_rmax_*). The feature
        # matrix is tree-oriented and contains exact linear identities
        # (delhi_mw = lag_n + diff_n), which a *linear* model exploits to
        # reconstruct the target -> leakage / 0% MAPE. SARIMA already models the
        # target's own autocorrelation via its (p,d,q)(P,D,Q) terms, so exog
        # should be genuine external drivers only.
        if self.exclude_target_history:
            num = num[[c for c in num.columns if not c.startswith(self.target_prefix)]]
        nunique = num.nunique()
        num = num[[c for c in num.columns if nunique[c] > 1]]  # drop constants
        # drop near-duplicate columns (keep the first of each correlated group)
        if num.shape[1] > 1:
            corr = num.corr().abs()
            upper = corr.where(np.triu(np.ones(corr.shape, dtype=bool), k=1))
            drop = [c for c in upper.columns if (upper[c] > self.max_corr).any()]
            num = num.drop(columns=drop)
        return list(num.columns)

    def _exog(self, X: pd.DataFrame, fit: bool) -> np.ndarray | None:
        if not self._exog_cols:
            return None
        X = X if isinstance(X, pd.DataFrame) else pd.DataFrame(X)
        vals = X[self._exog_cols].to_numpy(dtype=float)
        vals = np.nan_to_num(vals, nan=0.0, posinf=0.0, neginf=0.0)
        return self.scaler.fit_transform(vals) if fit else self.scaler.transform(vals)

    # -- fit -----------------------------------------------------------------
    def fit(self, X_train, y_train, X_val=None, y_val=None):
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        self.feature_names = list(X_train.columns)
        self._exog_cols = self._choose_exog_cols(X_train)
        exog_train = self._exog(X_train, fit=True)
        print(f"  SARIMAX exog: using {len(self._exog_cols)} of {X_train.shape[1]} features")

        if self.auto:
            self._auto_order(y_train, exog_train)

        print(f"  Fitting SARIMAX order={self.order} seasonal={self.seasonal_order} ...")
        self.model_fit = SARIMAX(
            np.asarray(y_train, dtype=float),
            exog=exog_train,
            order=self.order,
            seasonal_order=self.seasonal_order,
            enforce_stationarity=False,
            enforce_invertibility=False,
        ).fit(disp=False, maxiter=self.maxiter, method="lbfgs")
        self.is_fitted = True

        fitted = np.asarray(self.model_fit.fittedvalues, dtype=float)
        yt = np.asarray(y_train, dtype=float)
        mask = yt > 0
        metrics = {
            "train_mape": float(np.mean(np.abs((yt[mask] - fitted[mask]) / yt[mask])) * 100),
            "train_mae": float(np.mean(np.abs(yt - fitted))),
            "aic": float(self.model_fit.aic),
            "n_exog": len(self._exog_cols),
            "order": str(self.order),
            "seasonal_order": str(self.seasonal_order),
        }
        if X_val is not None and y_val is not None:
            fc = self.predict(X_val)
            yv = np.asarray(y_val, dtype=float)
            vm = yv > 0
            metrics["val_mape"] = float(np.mean(np.abs((yv[vm] - fc[vm]) / yv[vm])) * 100)
            metrics["val_mae"] = float(np.mean(np.abs(yv - fc)))
        return metrics

    def _auto_order(self, y_train, exog_train):
        try:
            import pmdarima as pm
        except ImportError:
            print("  (pmdarima not installed — keeping fixed order)")
            return
        am = pm.auto_arima(
            np.asarray(y_train, dtype=float), X=exog_train,
            seasonal=self.seasonal, m=self.seasonal_period if self.seasonal else 1,
            max_p=3, max_q=3, max_d=2, max_P=1, max_Q=1, max_D=1,
            stepwise=True, suppress_warnings=True, error_action="ignore", n_fits=20,
        )
        self.order = am.order
        if self.seasonal:
            self.seasonal_order = am.seasonal_order
        print(f"  auto_arima picked order={self.order} seasonal={self.seasonal_order}")

    # -- predict -------------------------------------------------------------
    def predict(self, X):
        exog = self._exog(X, fit=False)
        fc = self.model_fit.forecast(steps=len(X), exog=exog)
        return np.asarray(fc, dtype=float)

    def predict_interval(self, X, alpha=0.05):
        exog = self._exog(X, fit=False)
        res = self.model_fit.get_forecast(steps=len(X), exog=exog)
        point = np.asarray(res.predicted_mean, dtype=float)
        ci = res.conf_int(alpha=alpha)
        ci = np.asarray(ci, dtype=float)
        return point, ci[:, 0], ci[:, 1]

    def get_feature_importance(self):
        """|coefficient| of each exogenous regressor (the model's linear part)."""
        if self.model_fit is None or not self._exog_cols:
            return None
        params = self.model_fit.params
        names = getattr(self.model_fit, "param_names", None)
        if names is None:
            return None
        imp = {}
        for col in self._exog_cols:
            for pn, pv in zip(names, params):
                if pn == col or pn == f"x{self._exog_cols.index(col) + 1}":
                    imp[col] = abs(float(pv))
        if not imp:  # statsmodels names exog as x1..xk when given a bare array
            for i, col in enumerate(self._exog_cols):
                key = f"x{i + 1}"
                if key in names:
                    imp[col] = abs(float(params[list(names).index(key)]))
        return pd.Series(imp).sort_values(ascending=False) if imp else None

    # -- persistence ---------------------------------------------------------
    def save(self, path):
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model_fit, path / "sarimax_fit.joblib")
        joblib.dump(self.scaler, path / "scaler.joblib")
        meta = {
            "name": self.name, "resolution": self.resolution,
            "order": list(self.order), "seasonal_order": list(self.seasonal_order),
            "exog_mode": self.exog_mode, "exog_cols": self._exog_cols,
            "feature_names": self.feature_names,
        }
        with open(path / "meta.json", "w") as f:
            json.dump(meta, f, indent=2)

    @classmethod
    def load(cls, path):
        path = Path(path)
        with open(path / "meta.json") as f:
            meta = json.load(f)
        m = cls(resolution=meta["resolution"], exog=meta.get("exog_mode", "all"),
                order=tuple(meta["order"]), seasonal_order=tuple(meta["seasonal_order"]),
                seasonal=tuple(meta["seasonal_order"])[:3] != (0, 0, 0),
                name=meta.get("name", "sarimax"))
        m.model_fit = joblib.load(path / "sarimax_fit.joblib")
        m.scaler = joblib.load(path / "scaler.joblib")
        m._exog_cols = meta["exog_cols"]
        m.feature_names = meta["feature_names"]
        m.is_fitted = True
        return m

    def get_params(self):
        return {"name": self.name, "resolution": self.resolution,
                "order": str(self.order), "seasonal_order": str(self.seasonal_order),
                "exog_mode": self.exog_mode, "n_exog": len(self._exog_cols)}


class ARIMAForecaster(SARIMAXForecaster):
    """Non-seasonal ARIMA with the full feature matrix as exogenous regressors.

    (ARIMA is SARIMAX with no seasonal component; the ~93 features still enter
    as exogenous regressors, so this is really ARIMAX.)
    """

    def __init__(self, resolution: str, exog: str = "all", order: tuple | None = None,
                 auto: bool = False, **kw):
        super().__init__(resolution=resolution, exog=exog, seasonal=False,
                         order=order, auto=auto, name="arima", **kw)
