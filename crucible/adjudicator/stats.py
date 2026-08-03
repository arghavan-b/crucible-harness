"""Small, dependency-free statistics for verdict adjudication (design §8.3).

Welch's t-test and a one-sample t-test with two-sided p-values via the
regularized incomplete beta function (Numerical Recipes `betai`), plus the
inverse (a t critical value by bisection) used to size prediction intervals.
Kept self-contained so the harness has no scipy/numpy dependency; accuracy is
ample for the handful of seeds a claim is run across.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, variance


@dataclass
class TTest:
    t: float
    df: float
    p_two_sided: float
    mean_a: float
    mean_b: float

    def p_greater(self) -> float:
        """One-sided p that mean_a > mean_b arose by chance."""
        return self.p_two_sided / 2 if self.t > 0 else 1 - self.p_two_sided / 2

    def p_less(self) -> float:
        return 1 - self.p_greater()


def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 200, 3.0e-12, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_two_sided_p(t: float, df: float) -> float:
    if df <= 0:
        return 1.0
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def t_critical(df: float, alpha: float = 0.05) -> float:
    """Two-sided critical value: t such that P(|T_df| > t) == alpha.

    Inverts `_t_two_sided_p` by bisection (it is strictly decreasing in t).
    Dependency-free counterpart to `scipy.stats.t.ppf(1 - alpha/2, df)`.
    """
    if df <= 0 or not 0.0 < alpha < 1.0:
        raise ValueError(f"t_critical requires df > 0 and 0 < alpha < 1, got {df=} {alpha=}")
    lo, hi = 0.0, 1.0
    while _t_two_sided_p(hi, df) > alpha:
        hi *= 2.0
        if hi > 1e6:                      # pathological df; interval is unbounded
            return hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _t_two_sided_p(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prediction_interval_halfwidth(values: list[float], alpha: float = 0.05) -> float | None:
    """Half-width of the (1-alpha) prediction interval for ONE new observation.

    This is the right quantity for "would another run of this experiment land
    close enough to the reported value?" — wider than a confidence interval on
    the mean, because it must cover the new run's own sampling noise:

        mean +/- t(1-alpha/2, n-1) * s * sqrt(1 + 1/n)

    Returns None when n < 2, where the spread is not estimable from the data.
    """
    n = len(values)
    if n < 2:
        return None
    s = math.sqrt(variance(values))       # sample stdev, ddof=1
    if s == 0.0:
        return 0.0
    return t_critical(n - 1, alpha) * s * math.sqrt(1.0 + 1.0 / n)


def welch_t_test(a: list[float], b: list[float]) -> TTest:
    na, nb = len(a), len(b)
    ma, mb = fmean(a), fmean(b)
    va, vb = variance(a), variance(b)
    sa, sb = va / na, vb / nb
    se = math.sqrt(sa + sb)
    if se == 0.0:
        # No spread: decisive if the means differ, otherwise indistinguishable.
        p = 0.0 if ma != mb else 1.0
        return TTest(t=math.inf if ma > mb else -math.inf if ma < mb else 0.0,
                     df=float(na + nb - 2), p_two_sided=p, mean_a=ma, mean_b=mb)
    t = (ma - mb) / se
    df = (sa + sb) ** 2 / (sa**2 / (na - 1) + sb**2 / (nb - 1))
    return TTest(t=t, df=df, p_two_sided=_t_two_sided_p(t, df), mean_a=ma, mean_b=mb)


def one_sample_t_test(a: list[float], mu: float) -> TTest:
    n = len(a)
    ma = fmean(a)
    v = variance(a)
    se = math.sqrt(v / n)
    if se == 0.0:
        p = 0.0 if ma != mu else 1.0
        return TTest(t=math.inf if ma > mu else -math.inf if ma < mu else 0.0,
                     df=float(n - 1), p_two_sided=p, mean_a=ma, mean_b=mu)
    t = (ma - mu) / se
    df = float(n - 1)
    return TTest(t=t, df=df, p_two_sided=_t_two_sided_p(t, df), mean_a=ma, mean_b=mu)
