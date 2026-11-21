"""Statistical core: what *should* this listing cost, given comparable listings?

Two independent estimates are produced for every listing and then blended:

1. A robust ridge regression on log(price). Huber re-weighting stops genuinely
   odd listings (damaged cars, typos, joke prices) from dragging the market line,
   and the ridge penalty keeps rare categories (a model with 3 listings) honest.
   Predictions are leave-one-out, so a listing never helps to price itself.
2. A nearest-comparables estimate: the median price of the k most similar
   listings in the same market.

When both agree the confidence is high. When they disagree, confidence drops.
"""
from __future__ import annotations

import numpy as np

MAD_TO_SIGMA = 1.4826


def robust_ridge_loo(X: np.ndarray, y: np.ndarray, pen: np.ndarray, iters: int = 6,
                     k: float = 1.5, sigma_floor: float = 0.04) -> dict:
    """Huber-IRLS ridge. Column 0 of X must be the intercept (give it pen 0).

    `pen` is the absolute ridge penalty per column, i.e. roughly "how many observations
    of prior belief pull this coefficient toward zero".
    """
    n, p = X.shape
    w = np.ones(n)
    P = np.diag(pen.astype(float))
    for _ in range(iters):
        XtW = X.T * w
        A = XtW @ X + P
        beta = np.linalg.solve(A, XtW @ y)
        r = y - X @ beta
        s = max(MAD_TO_SIGMA * np.median(np.abs(r - np.median(r))), sigma_floor)
        u = np.abs(r) / (k * s)
        w = np.where(u <= 1.0, 1.0, 1.0 / u)

    XtW = X.T * w
    A = XtW @ X + P
    A_inv = np.linalg.inv(A)
    beta = A_inv @ (XtW @ y)
    r = y - X @ beta
    hat = np.clip(w * np.einsum("ij,jk,ik->i", X, A_inv, X), 0.0, 0.95)
    loo_resid = r / (1.0 - hat)
    sigma = max(MAD_TO_SIGMA * np.median(np.abs(loo_resid - np.median(loo_resid))), sigma_floor)
    return {"beta": beta, "loo_pred": y - loo_resid, "fit_pred": X @ beta,
            "sigma": float(sigma), "weights": w}


def knn_loo(num: np.ndarray, log_price: np.ndarray, k: int = 10,
            cats: list[tuple[np.ndarray, float]] | None = None,
            groups: np.ndarray | None = None, block: int = 400):
    """Median log-price of the k nearest *other* listings; returns (estimate, neighbour index).

    num     (n, d) standardised numeric features.
    cats    [(codes, penalty)] categorical features: each mismatch adds `penalty` to the
            squared distance (e.g. a different district counts as "far").
    groups  neighbours must share the group code (e.g. same city or same model).
    Entries with fewer than 3 eligible neighbours come back as NaN / -1.
    """
    n = len(log_price)
    est = np.full(n, np.nan)
    idx = np.full((n, k), -1, dtype=int)
    sq = (num ** 2).sum(axis=1)
    for start in range(0, n, block):
        stop = min(start + block, n)
        D = sq[start:stop, None] + sq[None, :] - 2.0 * num[start:stop] @ num.T
        for codes, pen in cats or []:
            D += pen * (codes[start:stop, None] != codes[None, :])
        if groups is not None:
            D[groups[start:stop, None] != groups[None, :]] = np.inf
        D[np.arange(stop - start), np.arange(start, stop)] = np.inf  # never your own neighbour
        kk = min(k, n - 1)
        if kk < 3:
            continue
        part = np.argpartition(D, kk - 1, axis=1)[:, :kk]
        for row in range(stop - start):
            cand = part[row]
            cand = cand[np.isfinite(D[row, cand])]
            if len(cand) < 3:
                continue
            cand = cand[np.argsort(D[row, cand])]
            est[start + row] = np.median(log_price[cand])
            idx[start + row, : len(cand)] = cand
    return est, idx


def confidence(n: int, sigma: float, agreement: float = 1.0) -> float:
    """0..1 trust in a fair-value estimate: enough comparables, tight scatter, two methods agree."""
    sample = n / (n + 15.0)
    quality = 1.0 / (1.0 + (sigma / 0.25) ** 2)
    return float(np.clip(sample * quality * (0.5 + 0.5 * agreement), 0.05, 1.0))


def agreement(log_reg: float, log_knn: float) -> float:
    if np.isnan(log_knn):
        return 0.7  # only one method available: a little less sure
    d = abs(log_reg - log_knn)
    return float(1.0 / (1.0 + (d / 0.15) ** 2))


def blend(log_reg: np.ndarray, log_knn: np.ndarray, w_reg: float = 0.7) -> np.ndarray:
    """Log-space blend; falls back to the regression when there is no kNN estimate."""
    return np.where(np.isnan(log_knn), log_reg, w_reg * log_reg + (1 - w_reg) * log_knn)
