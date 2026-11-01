"""Demand forecasting: LightGBM quantile models for covers + a share model for the dish mix.

- `build_features` turns (date, meal period) into the signal features from Idea.md (day of week,
  payday, start of month, season, holiday, events, heat, reservations, seat cap).
- Covers: one LightGBM quantile regressor per alpha (0.1 / 0.5 / 0.9), clamped to the seat cap.
- Dish mix: a LightGBM regressor of each dish's share of a period's portions (conditioned on the same
  features); dish quantiles = share x covers quantile x portions-per-cover (+ Poisson spread).
- Trained on the first 11 months, evaluated on the last month; models pickled to MODELS_DIR.
- `signal_report` ablates / permutes each signal on the test month (falling back to the full history
  when a signal has too few active rows in the test month).
- If lightgbm cannot be imported, a seasonal-naive (DOW x period) fallback with the same interface is used.
No LLM imports here.
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd

from restaurant_ops.config import settings
from restaurant_ops.core.util import conn_ctx, daterange, iso, r2, to_date
from restaurant_ops.data import catalog as cat
from restaurant_ops.data import generator as gen
from restaurant_ops.db.connection import query_df

try:  # pragma: no cover - environment dependent
    import lightgbm as lgb
except Exception:  # noqa: BLE001
    lgb = None

PERIODS: list[str] = list(settings.MEAL_PERIODS)
QUANTILES = (0.1, 0.5, 0.9)
MODEL_FILE = "forecast_models.pkl"
TEST_DAYS = 30  # last month held out
Z90 = 1.2816  # normal z for the 10th / 90th percentile

HOLIDAY_CODES = {name: i + 1 for i, name in enumerate(sorted(gen.HOLIDAY_EFFECTS))}
FEATURES = ["period", "dow", "dom", "payday", "start_of_month", "woy", "season", "month", "holiday",
            "holiday_code", "sports", "local", "private", "temp_c", "heat", "booked_covers", "lead_days", "seat_cap"]
TRAIN_LEADS = range(0, 8)  # covers model sees each history day as forecast 0..7 days ahead (bookings known then)
MODEL_VERSION = 2  # bump when features change: an older pickle is retrained on load
DISH_IDS = [d.id for d in cat.DISHES]
_DISH_IDX = {d: i for i, d in enumerate(DISH_IDS)}
DISH_FEATURES = [f for f in FEATURES if f != "lead_days"] + ["dish_idx", "dish_cold", "dish_cat", "in_season"]
_CAT_CODE = {"starter": 0, "main": 1, "dessert": 2, "side": 3}
_INSEASON = {(d.id, m): float(cat.in_season(d.seasonal_months, m)) for d in cat.DISHES for m in range(1, 13)}

SIGNALS = ["payday", "start_of_month", "day_of_week", "season", "events", "heat", "reservations"]
SIGNAL_COLS = {
    "payday": ["payday"], "start_of_month": ["start_of_month"], "day_of_week": ["dow"],
    "season": ["season", "month", "woy"], "events": ["holiday", "holiday_code", "sports", "local", "private"],
    "heat": ["heat", "temp_c"], "reservations": ["booked_covers"],
}
EFFECT_THRESHOLD_PCT = 3.0  # |effect| below this is treated as "no effect"

_BUNDLE: dict | None = None
MEM_CHECK_EVERY = 25  # boosting rounds between memory checks


class TrainingMemoryError(RuntimeError):
    """Training aborted because the process went over settings.TRAIN_MAX_MEMORY_MB."""


def rss_mb() -> float:
    """Current resident memory of this process in MB (peak RSS if psutil is unavailable)."""
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2**20
    except Exception:  # noqa: BLE001
        import resource
        import sys

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 2**20 if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KB on Linux


def check_memory(stage: str) -> None:
    """Raise TrainingMemoryError when the process is over the configured limit."""
    limit = settings.TRAIN_MAX_MEMORY_MB
    if limit and (used := rss_mb()) > limit:
        raise TrainingMemoryError(f"training aborted at {stage}: {used:.0f} MB used > TRAIN_MAX_MEMORY_MB={limit}")


def _mem_callback(env) -> None:
    """LightGBM callback: check memory every MEM_CHECK_EVERY rounds (raising aborts the fit)."""
    if env.iteration % MEM_CHECK_EVERY == 0:
        check_memory(f"boosting round {env.iteration}")


# =========================================================================== features


def _seasonal_temp(d: date) -> float:
    """Fallback temperature when the weather table has no row."""
    return 14 + 11 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 200) / 365)


def _event_flags(events: list[dict], period: str) -> tuple[float, float, float]:
    """(sports, local, private) flags for one meal period from event rows."""
    sports = local = private = 0.0
    for e in events:
        ep, et = e.get("meal_period"), e.get("type")
        if et == "sports" and period in ("dinner", "late") and ep in (None, period, "dinner"):
            sports = 1.0
        elif et == "local" and ep in (None, period):
            local = 1.0
        elif et == "private" and ep in (None, period):
            private = 1.0
    return sports, local, private


def build_features(conn: sqlite3.Connection, dates: list[date], periods: list[str] | None = None, *,
                   as_of: date | str | None = None, lead: int = 1) -> pd.DataFrame:
    """Feature rows (one per date x meal period). Columns: date, meal_period + FEATURES (all float).

    `booked_covers` counts only the bookings that existed when the forecast was made, and `lead_days` says how
    far ahead that was. Forecasting (as_of given): bookings created on or before as_of, lead = date - as_of.
    Training (as_of None): bookings created on or before date - lead. A Saturday seen 6 days out has far fewer
    bookings than the day before, and the model must learn that rather than read it as a quiet night.
    Holidays come from the calendar function (not event rows).
    """
    periods = periods or PERIODS
    if not dates:
        return pd.DataFrame(columns=["date", "meal_period"] + FEATURES)
    d0, d1 = iso(min(dates)), iso(max(dates))
    ev = query_df("SELECT date,name,type,meal_period FROM events WHERE date BETWEEN ? AND ?", (d0, d1), conn)
    ev_by: dict[str, list[dict]] = {}
    for r in ev.to_dict("records"):
        ev_by.setdefault(r["date"], []).append(r)
    wx = query_df("SELECT date,temp_c FROM weather WHERE date BETWEEN ? AND ?", (d0, d1), conn)
    temp = dict(zip(wx["date"], wx["temp_c"]))
    asof = to_date(as_of) if as_of is not None else None
    rs = query_df("SELECT date, meal_period, created_date, SUM(party_size) AS booked FROM reservations "
                  "WHERE date BETWEEN ? AND ? GROUP BY date, meal_period, created_date", (d0, d1), conn)
    by_slot: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for a, b, cr, n in zip(rs["date"], rs["meal_period"], rs["created_date"], rs["booked"]):
        by_slot.setdefault((a, b), []).append((cr, float(n)))
    rows = []
    for d in dates:
        k = d.isoformat()
        cutoff = (asof if asof is not None else d - timedelta(days=lead)).isoformat()
        lead_days = float(max(0, (d - asof).days) if asof is not None else lead)
        hol = gen.holiday_for(d)
        t = float(temp.get(k, _seasonal_temp(d)))
        for pi, p in enumerate(periods):
            sports, local, private = _event_flags(ev_by.get(k, []), p)
            rows.append({
                "date": k, "meal_period": p, "period": float(PERIODS.index(p)), "dow": float(d.weekday()),
                "dom": float(d.day), "payday": float(gen.is_payday(d)), "start_of_month": float(d.day <= 3),
                "woy": float(d.isocalendar()[1]), "season": float((d.month % 12) // 3), "month": float(d.month),
                "holiday": float(hol is not None), "holiday_code": float(HOLIDAY_CODES.get(hol, 0)),
                "sports": sports, "local": local, "private": private, "temp_c": t,
                "heat": float(t > gen.HEAT_C),
                "booked_covers": sum(n for cr, n in by_slot.get((k, p), ()) if cr <= cutoff), "lead_days": lead_days,
                "seat_cap": float(settings.seat_cap(p)),
            })
    return pd.DataFrame(rows)


# =========================================================================== models


class _QuantileModel:
    """P10 / P50 / P90 regressor for covers. LightGBM, or seasonal-naive (DOW x period) fallback."""

    def __init__(self, backend: str = "lgbm"):
        self.backend = backend if lgb is not None else "naive"
        self.models: list = []
        self.table: dict = {}

    def fit(self, X: pd.DataFrame, y: np.ndarray) -> "_QuantileModel":
        if self.backend == "lgbm":
            self.models = [
                lgb.LGBMRegressor(objective="quantile", alpha=a, n_estimators=250, learning_rate=0.05,
                                  num_leaves=8, min_child_samples=15, reg_lambda=1.0, subsample=0.9,
                                  subsample_freq=1, colsample_bytree=0.9, random_state=settings.SEED,
                                  n_jobs=1, verbose=-1).fit(X[FEATURES], y, callbacks=[_mem_callback])
                for a in QUANTILES]
        else:
            df = pd.DataFrame({"period": X["period"].to_numpy(), "dow": X["dow"].to_numpy(), "y": y})
            self.table = {k: np.quantile(g["y"], QUANTILES) for k, g in df.groupby(["period", "dow"])}
            self.table_p = {k: np.quantile(g["y"], QUANTILES) for k, g in df.groupby("period")}
        return self

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """(n, 3) array of sorted quantiles, clipped to [0, seat cap]."""
        if self.backend == "lgbm":
            q = np.column_stack([m.predict(X[FEATURES]) for m in self.models])
        else:
            q = np.array([self.table.get((p, d), self.table_p[p])
                          for p, d in zip(X["period"], X["dow"])], dtype=float)
        q = np.sort(q, axis=1)
        return np.clip(q, 0.0, X["seat_cap"].to_numpy()[:, None])


class _ShareModel:
    """Predicts each dish's share of a period's portions. LightGBM, or per (period, dish) mean fallback."""

    def __init__(self, backend: str = "lgbm"):
        self.backend = backend if lgb is not None else "naive"
        self.model = None
        self.table: dict = {}

    def fit(self, D: pd.DataFrame) -> "_ShareModel":
        D = D[D["in_season"] > 0]
        if self.backend == "lgbm":
            self.model = lgb.LGBMRegressor(n_estimators=300, learning_rate=0.05, num_leaves=15,
                                           min_child_samples=20, reg_lambda=1.0, random_state=settings.SEED,
                                           n_jobs=1, verbose=-1).fit(D[DISH_FEATURES], D["share"],
                                                                     callbacks=[_mem_callback])
        else:
            self.table = D.groupby(["period", "dish_idx"])["share"].mean().to_dict()
        return self

    def predict(self, D: pd.DataFrame) -> np.ndarray:
        if self.backend == "lgbm":
            raw = self.model.predict(D[DISH_FEATURES])
        else:
            raw = np.array([self.table.get((p, i), 0.0) for p, i in zip(D["period"], D["dish_idx"])])
        return np.clip(raw, 0.0, None)


def _expand_dishes(feats: pd.DataFrame) -> pd.DataFrame:
    """Cross-join feature rows with the 20 dishes and add dish attributes."""
    dd = pd.DataFrame({"dish_id": DISH_IDS, "dish_idx": [float(i) for i in range(len(DISH_IDS))],
                       "dish_cold": [float(cat.DISHES_BY_ID[d].is_cold) for d in DISH_IDS],
                       "dish_cat": [float(_CAT_CODE[cat.DISHES_BY_ID[d].category]) for d in DISH_IDS]})
    D = feats.merge(dd, how="cross")
    D["in_season"] = [_INSEASON[(d, int(m))] for d, m in zip(D["dish_id"], D["month"])]
    return D


def _normalised_shares(model: _ShareModel, D: pd.DataFrame) -> pd.DataFrame:
    """Predict shares, zero out-of-season dishes, renormalise per (date, period)."""
    D = D.copy()
    D["share"] = model.predict(D) * D["in_season"].to_numpy()
    tot = D.groupby(["date", "meal_period"])["share"].transform("sum")
    D["share"] = np.where(tot > 0, D["share"] / tot.replace(0, np.nan), 0.0)
    D["share"] = D["share"].fillna(0.0)
    return D


# =========================================================================== training


def _history(conn: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(covers feature frame with actual covers, one row per day x period x lead in TRAIN_LEADS;
    dish frame with actual shares, day-ahead rows only) over all history."""
    cov = query_df("SELECT date, meal_period, covers FROM covers ORDER BY date", conn=conn)
    if cov.empty:
        raise RuntimeError("no covers history in the database; run the seed first")
    dates = [to_date(d) for d in sorted(cov["date"].unique())]
    feats = pd.concat([build_features(conn, dates, lead=L) for L in TRAIN_LEADS], ignore_index=True)
    feats = feats.merge(cov, on=["date", "meal_period"], how="inner")
    sales = query_df("SELECT date, meal_period, dish_id, qty FROM pos_sales", conn=conn)
    D = _expand_dishes(feats[feats["lead_days"] == 1].drop(columns=["covers"]))  # shares do not depend on lead
    D = D.merge(sales, on=["date", "meal_period", "dish_id"], how="left")
    D["qty"] = D["qty"].fillna(0.0)
    tot = D.groupby(["date", "meal_period"])["qty"].transform("sum")
    D["share"] = np.where(tot > 0, D["qty"] / tot.replace(0, np.nan), 0.0)
    D["share"] = D["share"].fillna(0.0)
    D["total"] = tot
    return feats, D


def _mape(actual: np.ndarray, pred: np.ndarray) -> float:
    m = actual > 0
    return float(np.mean(np.abs(actual[m] - pred[m]) / actual[m])) if m.any() else float("nan")


def _evaluate(feats: pd.DataFrame, D: pd.DataFrame, covers_m: _QuantileModel, shares_m: _ShareModel,
              ppc: dict[str, float], test_start: str) -> dict:
    """Metrics on the held-out month."""
    T = feats[feats["date"] >= test_start].reset_index(drop=True)
    q = covers_m.predict(T)
    a = T["covers"].to_numpy(dtype=float)
    cov90 = a <= q[:, 2] + 1e-9
    by_period = {}
    for p in PERIODS:
        m = (T["meal_period"] == p).to_numpy()
        by_period[p] = {"mape": r2(_mape(a[m], q[m, 1]), 4), "p90_coverage": r2(cov90[m].mean(), 3),
                        "p10_p90_coverage": r2(((a[m] >= q[m, 0] - 1e-9) & (a[m] <= q[m, 2] + 1e-9)).mean(), 3),
                        "n": int(m.sum())}
    DT = _normalised_shares(shares_m, D[D["date"] >= test_start])
    one = (T["lead_days"] == 1).to_numpy()  # dish error is scored on the day-ahead forecast
    pmap = {(d, p): q[i, 1] for i, (d, p) in enumerate(zip(T["date"], T["meal_period"])) if one[i]}
    by_lead = {int(L): r2(_mape(a[(T["lead_days"] == L).to_numpy()], q[(T["lead_days"] == L).to_numpy(), 1]), 4)
               for L in sorted(T["lead_days"].unique())}
    pred = np.array([s * pmap[(d, p)] * ppc[p] for s, d, p in zip(DT["share"], DT["date"], DT["meal_period"])])
    act = DT["qty"].to_numpy(dtype=float)
    return {"mape": r2(_mape(a, q[:, 1]), 4), "p90_coverage": r2(cov90.mean(), 3),
            "p10_p90_coverage": r2(((a >= q[:, 0] - 1e-9) & cov90).mean(), 3), "by_period": by_period,
            "dish_wmape": r2(np.abs(act - pred).sum() / max(act.sum(), 1.0), 4), "n_test": int(len(T)),
            "mape_by_lead_days": by_lead}


def _perm_stats(score, X: pd.DataFrame, cols: list[str], rng: np.random.Generator, repeats: int = 8):
    """(base_score, mean_increase, std_increase) when `cols` are jointly row-permuted. score() = error (lower is better)."""
    base = score(X)
    incs = []
    for _ in range(repeats):
        idx = rng.permutation(len(X))
        Xp = X.copy()
        Xp[cols] = X[cols].to_numpy()[idx]
        incs.append(score(Xp) - base)
    return base, float(np.mean(incs)), float(np.std(incs))


def _neutralise(name: str, X: pd.DataFrame, train_booked: dict[float, float]) -> pd.DataFrame:
    """Copy of X with the signal switched off / set to its reference value."""
    N = X.copy()
    if name == "payday":
        N["payday"] = 0.0
    elif name == "start_of_month":
        N["start_of_month"] = 0.0
    elif name == "day_of_week":
        N["dow"] = 3.0  # Thursday = reference day
    elif name == "season":
        N["month"] = (X["month"] + 5) % 12 + 1
        N["woy"] = (X["woy"] + 25) % 52 + 1
        N["season"] = (X["season"] + 2) % 4
    elif name == "events":
        for c in SIGNAL_COLS["events"]:
            N[c] = 0.0
    elif name == "heat":
        N["heat"] = 0.0
        N["temp_c"] = np.minimum(X["temp_c"], 22.0)
    elif name == "reservations":
        N["booked_covers"] = X["period"].map(train_booked).fillna(0.0)
    return N


def _active(name: str, X: pd.DataFrame) -> np.ndarray:
    """Rows where the signal is 'on'."""
    if name in ("payday", "start_of_month", "heat"):
        return (X[{"payday": "payday", "start_of_month": "start_of_month", "heat": "heat"}[name]] > 0).to_numpy()
    if name == "day_of_week":
        return (X["dow"] != 3).to_numpy()
    if name == "events":
        return (X[SIGNAL_COLS["events"][0]] + X["sports"] + X["local"] + X["private"] > 0).to_numpy()
    return np.ones(len(X), dtype=bool)


def _compute_signals(feats: pd.DataFrame, D: pd.DataFrame, covers_m: _QuantileModel, shares_m: _ShareModel,
                     test_start: str) -> list[dict]:
    """Counterfactual effect + permutation importance for each signal (test month; history if too sparse)."""
    rng = np.random.default_rng(settings.SEED)
    train = feats[feats["date"] < test_start]
    train_booked = train.groupby("period")["booked_covers"].mean().to_dict()
    test = feats[feats["date"] >= test_start].reset_index(drop=True)
    cold_idx = {i for i, d in enumerate(DISH_IDS) if cat.DISHES_BY_ID[d].is_cold}
    out = []

    def covers_mae(X: pd.DataFrame) -> float:
        return float(np.mean(np.abs(X["covers"].to_numpy() - covers_m.predict(X)[:, 1])))

    for name in SIGNALS:
        cols = SIGNAL_COLS[name]
        scope, base_frame = "test_month", test
        act = _active(name, test)
        if name != "day_of_week" and name not in ("season", "reservations") and act.sum() < 3:
            scope, base_frame = "full_history", feats.reset_index(drop=True)
            act = _active(name, base_frame)
        n_active = int(act.sum())
        if n_active == 0:
            out.append({"signal": name, "effect_pct": 0.0, "importance_pct": 0.0, "significant": False,
                        "n_active_rows": 0, "scope": scope, "note": "never active in the data; cannot be estimated"})
            continue
        rows = base_frame[act].reset_index(drop=True)
        signed = name in ("payday", "start_of_month", "events", "heat")
        if name == "heat":  # heat moves the dish mix (cold dishes), not covers
            Dh = _expand_dishes(rows.drop(columns=["covers"]))
            Dn = _expand_dishes(_neutralise(name, rows, train_booked).drop(columns=["covers"]))
            b, c = _normalised_shares(shares_m, Dh), _normalised_shares(shares_m, Dn)
            cold_b = b[b["dish_idx"].isin(cold_idx)].groupby(["date", "meal_period"])["share"].sum()
            cold_c = c[c["dish_idx"].isin(cold_idx)].groupby(["date", "meal_period"])["share"].sum()
            rel = (cold_b / cold_c.replace(0, np.nan) - 1).dropna().to_numpy()
            what = "cold-dish share of portions"
            Dall = D[D["date"].isin(set(base_frame["date"]))].reset_index(drop=True)

            def share_mae(X: pd.DataFrame) -> float:
                return float(np.mean(np.abs(X["share"].to_numpy() - shares_m.predict(X))))
            Dall = Dall[Dall["in_season"] > 0].reset_index(drop=True)
            base_err, inc, sd = _perm_stats(share_mae, Dall, cols, rng, repeats=5)
        else:
            base = covers_m.predict(rows)[:, 1]
            cf = covers_m.predict(_neutralise(name, rows, train_booked))[:, 1]
            rel = np.where(cf > 0, base / np.where(cf > 0, cf, 1) - 1, 0.0)
            what = "P50 covers"
            base_err, inc, sd = _perm_stats(covers_mae, test, cols, rng)
        effect = float(np.mean(rel) if signed else np.mean(np.abs(rel))) * 100
        importance = inc / max(base_err, 1e-9) * 100
        significant = bool(abs(effect) >= EFFECT_THRESHOLD_PCT and inc > 0 and inc >= 2 * sd)
        verb = "no material effect" if not significant else ("raises" if effect > 0 else "lowers")
        note = (f"{verb} {what}" if not significant else f"{verb} {what} by {abs(effect):.1f}%") + \
               f" (n={n_active} active rows, scope={scope}; permutation importance {importance:+.2f}% MAE)"
        if scope == "full_history":
            note += "; in-sample estimate"
        out.append({"signal": name, "effect_pct": r2(effect, 2), "importance_pct": r2(importance, 2),
                    "significant": significant, "n_active_rows": n_active, "scope": scope, "note": note})
    return out


def train_models(conn: sqlite3.Connection | None = None) -> dict:
    """Train on the first 11 months, evaluate on the last, refit on all history, pickle, return metrics."""
    global _BUNDLE
    check_memory("start")
    with conn_ctx(conn) as c:
        feats, D = _history(c)
        check_memory("features")
        last = to_date(feats["date"].max())
        test_start = (last - timedelta(days=TEST_DAYS - 1)).isoformat()
        backend = "lgbm" if lgb is not None else "naive"
        tr = feats[feats["date"] < test_start]
        tr_D = D[D["date"] < test_start]

        def fit_pair(f: pd.DataFrame, dd: pd.DataFrame, be: str):
            return (_QuantileModel(be).fit(f, f["covers"].to_numpy(dtype=float)), _ShareModel(be).fit(dd))

        try:
            eval_c, eval_s = fit_pair(tr, tr_D, backend)
            prod_c, prod_s = fit_pair(feats, D, backend)
        except TrainingMemoryError:
            raise
        except Exception:  # noqa: BLE001  lightgbm failed at fit time -> naive fallback
            backend = "naive"
            eval_c, eval_s = fit_pair(tr, tr_D, backend)
            prod_c, prod_s = fit_pair(feats, D, backend)
        sales_tot = D.groupby("meal_period")["qty"].sum()
        cov_tot = feats[feats["lead_days"] == 1].groupby("meal_period")["covers"].sum()  # one row per day
        ppc = {p: float(sales_tot[p] / max(cov_tot[p], 1)) for p in PERIODS}
        metrics = _evaluate(feats, D, eval_c, eval_s, ppc, test_start)
        metrics.update({"backend": backend, "train_days": int(tr["date"].nunique()), "test_start": test_start,
                        "n_train": int(len(tr))})
        check_memory("evaluation")
        signals = _compute_signals(feats, D, eval_c, eval_s, test_start)
        check_memory("signals")
        bundle = {"version": MODEL_VERSION, "backend": backend, "covers": prod_c, "shares": prod_s, "ppc": ppc,
                  "metrics": metrics, "signals": signals}
        settings.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        path = settings.MODELS_DIR / MODEL_FILE
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:  # write-then-rename: an interrupted run never leaves a half-written pickle
            pickle.dump(bundle, f)
        os.replace(tmp, path)
        _BUNDLE = bundle
        return metrics


def load_models(conn: sqlite3.Connection | None = None, force_train: bool = False) -> dict:
    """Lazy-load the pickled models; train (and pickle) them first if missing or unreadable."""
    global _BUNDLE
    if _BUNDLE is not None and not force_train:
        return _BUNDLE
    path = settings.MODELS_DIR / MODEL_FILE
    if path.exists() and not force_train:
        try:
            with open(path, "rb") as f:
                b = pickle.load(f)
            if b.get("version") == MODEL_VERSION:
                _BUNDLE = b
                return _BUNDLE
        except Exception:  # noqa: BLE001
            pass
    train_models(conn)
    return _BUNDLE  # type: ignore[return-value]


def reset_cache() -> None:
    """Drop the in-memory model bundle (forces a reload from disk)."""
    global _BUNDLE
    _BUNDLE = None


# =========================================================================== forecasting


def forecast_adjustments(conn: sqlite3.Connection) -> list[dict]:
    """Approved / executed `adjust_forecast` decisions as [{date, meal_period, factor}]."""
    try:
        rows = conn.execute("SELECT payload FROM decisions WHERE action_type='adjust_forecast' "
                            "AND status IN ('approved','executed') ORDER BY id").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for (p,) in rows:
        try:
            pl = json.loads(p) if isinstance(p, str) else dict(p)
            out.append({"date": pl["date"], "meal_period": pl.get("meal_period", "all"), "factor": float(pl["factor"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _factor(adjs: list[dict], d: str, p: str) -> float:
    f = 1.0
    for a in adjs:
        if str(a["date"])[:10] == d and a.get("meal_period", "all") in ("all", None, p):
            f *= float(a["factor"])
    return f


def _covers_frame(c: sqlite3.Connection, bundle: dict, dates: list[date], adjust: bool,
                  adjustments: list[dict] | None, as_of: str | None = None) -> tuple[pd.DataFrame, np.ndarray]:
    """Feature frame + (n,3) covers quantiles (adjusted, clamped to cap, p10<=p50<=p90).
    Bookings are those visible on `as_of` (default: the first forecast date)."""
    feats = build_features(c, dates, as_of=min(to_date(as_of), dates[0]) if as_of else dates[0])
    q = bundle["covers"].predict(feats)
    adjs = list(adjustments or []) + (forecast_adjustments(c) if adjust else [])
    if adjs:
        q = q * np.array([_factor(adjs, d, p) for d, p in zip(feats["date"], feats["meal_period"])])[:, None]
    q = np.sort(np.clip(q, 0.0, feats["seat_cap"].to_numpy()[:, None]), axis=1)
    return feats, q


def forecast_covers(start_date: str, days: int = 3, conn: sqlite3.Connection | None = None,
                    adjust: bool = True, adjustments: list[dict] | None = None, as_of: str | None = None) -> list[dict]:
    """P10/P50/P90 covers per meal period for `days` days from `start_date`.

    `adjust=True` applies approved `adjust_forecast` decisions from the decisions table;
    `adjustments` = [{date, meal_period ('all' ok), factor}] are applied on top.
    Returns [{date, meal_period, p10, p50, p90, capacity_cap}].
    """
    with conn_ctx(conn) as c:
        bundle = load_models(c)
        feats, q = _covers_frame(c, bundle, daterange(start_date, days), adjust, adjustments, as_of)
        return [{"date": d, "meal_period": p, "p10": r2(a, 1), "p50": r2(b, 1), "p90": r2(e, 1),
                 "capacity_cap": int(cap)}
                for d, p, (a, b, e), cap in zip(feats["date"], feats["meal_period"], q, feats["seat_cap"])]


def forecast_dish_mix(start_date: str, days: int = 3, conn: sqlite3.Connection | None = None,
                      adjust: bool = True, adjustments: list[dict] | None = None, as_of: str | None = None) -> list[dict]:
    """Portions per dish: P10/P50/P90 = predicted share x covers quantile x portions-per-cover.

    The P10/P90 also include a Poisson spread (+/- 1.28 sqrt(mean)). Out-of-season dishes are omitted.
    Returns [{date, meal_period, dish_id, p10, p50, p90}].
    """
    with conn_ctx(conn) as c:
        bundle = load_models(c)
        feats, q = _covers_frame(c, bundle, daterange(start_date, days), adjust, adjustments, as_of)
        D = _normalised_shares(bundle["shares"], _expand_dishes(feats))
        key = {(d, p): q[i] for i, (d, p) in enumerate(zip(feats["date"], feats["meal_period"]))}
        ppc = bundle["ppc"]
        out = []
        for d, p, dish, s in zip(D["date"], D["meal_period"], D["dish_id"], D["share"]):
            if s <= 0:
                continue
            m10, m50, m90 = (s * v * ppc[p] for v in key[(d, p)])
            lo, hi = max(0.0, m10 - Z90 * math.sqrt(m10)), m90 + Z90 * math.sqrt(m90)
            if hi < 0.05:
                continue
            out.append({"date": d, "meal_period": p, "dish_id": dish, "p10": r2(lo, 1), "p50": r2(m50, 1),
                        "p90": r2(hi, 1)})
        return out


def signal_report(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Which signals matter here? [{signal, effect_pct, significant, note, importance_pct, ...}].

    effect_pct = average % change in the P50 forecast when the signal is switched off / neutralised
    (heat: change in the cold-dish share). `significant` needs |effect| >= 3% AND a positive
    permutation-importance on held-out error.
    """
    return load_models(conn)["signals"]


def evaluate(conn: sqlite3.Connection | None = None) -> dict:
    """Held-out (last month) accuracy: {mape, p90_coverage, by_period, dish_wmape, ...}."""
    return load_models(conn)["metrics"]


def main(argv: list[str] | None = None) -> int:
    """`python -m restaurant_ops.core.forecasting [--max-memory-mb N]`: force a retrain on the seeded DB."""
    import argparse

    from restaurant_ops.db.connection import get_conn

    ap = argparse.ArgumentParser(description="Retrain the forecasting models (overwrites the pickle)")
    ap.add_argument("--db", default=str(settings.DB_PATH), help="seeded database to train on")
    ap.add_argument("--max-memory-mb", type=int, default=None, help="override TRAIN_MAX_MEMORY_MB (0 = no limit)")
    args = ap.parse_args(argv)
    if args.max_memory_mb is not None:
        settings.TRAIN_MAX_MEMORY_MB = args.max_memory_mb
    conn = get_conn(args.db)
    try:
        metrics = load_models(conn, force_train=True)["metrics"]
    except TrainingMemoryError as e:
        print(e)
        return 1
    finally:
        conn.close()
    print(json.dumps(metrics, indent=2))
    print(f"saved {settings.MODELS_DIR / MODEL_FILE}  (peak process memory {rss_mb():.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
