"""SCOUT — تشخیص ده ستاپ روی کندل‌های بستهٔ M15 (فقط تشخیص؛ هیچ سفارشی ثبت نمی‌شود).
 
هر ستاپ برای هر کندل بسته ارزیابی می‌شود و در صورت فعال‌شدن، جهت و سطوح پیشنهادی
(ورود، حد ضرر ساختاری، حد سود) را برمی‌گرداند. همهٔ شرط‌ها فقط از کندل‌های بسته
استفاده می‌کنند؛ هیچ دادهٔ آینده‌ای در تصمیم دخالت ندارد.
"""
from __future__ import annotations
 
import numpy as np
import pandas as pd
 
from core.indicators import atr as _atr, ema as _ema
 
SETUPS = ["kijun_pullback", "tenkan_momentum", "range_break", "pullback_resume",
          "cloud_break", "tk_cross", "rejection", "engulfing", "ignition", "exhaustion"]
 
 
def indicators(m15: pd.DataFrame, h1: pd.DataFrame | None = None,
               h1_ema_period: int = 20) -> pd.DataFrame:
    f = m15.copy()
    h, l, c, o = (f[x].astype("float64") for x in ("high", "low", "close", "open"))
    f["atr"] = _atr(h, l, c, 14)
    f["ema200"] = _ema(c, 200)
    f["tenkan"] = (h.rolling(9).max() + l.rolling(9).min()) / 2
    f["kijun"] = (h.rolling(26).max() + l.rolling(26).min()) / 2
    sb = (h.rolling(52).max() + l.rolling(52).min()) / 2
    sa = (f.tenkan + f.kijun) / 2
    f["cloud_top"] = np.maximum(sa, sb)
    f["cloud_bot"] = np.minimum(sa, sb)
    f["swing_hi"] = h.rolling(20).max().shift(1)
    f["swing_lo"] = l.rolling(20).min().shift(1)
    f["rng"] = (h - l).clip(lower=0.01)
    f["body"] = c - o
    f["atr_mean5"] = f.atr.shift(1).rolling(5).mean()
    f["vol_mean20"] = f.get("tick_volume", pd.Series(1.0, index=f.index)).astype("float64").shift(1).rolling(20).mean()
    if h1 is not None and not h1.empty:
        e = _ema(h1["close"].astype("float64"), h1_ema_period)
        al = pd.DataFrame({"ema": e}).shift(1).reindex(
            pd.Index(f.index).union(h1.index)).sort_index().ffill().reindex(f.index)
        f["h1_ema"] = al["ema"]
        f["h1_bull"] = (f.close >= f.h1_ema) | f.h1_ema.isna()
        f["h1_bear"] = (f.close <= f.h1_ema) | f.h1_ema.isna()
    else:
        f["h1_ema"] = np.nan
        f["h1_bull"] = True
        f["h1_bear"] = True
    return f
 
 
def detect(f: pd.DataFrame) -> pd.DataFrame:
    """برای هر کندل، فهرست ستاپ‌های فعال را به‌صورت جدول بلند برمی‌گرداند."""
    a, c, o, h, l = f.atr, f.close, f.open, f.high, f.low
    pc, ph, pl, po = c.shift(1), h.shift(1), l.shift(1), o.shift(1)
    up = f.h1_bull & (c > f.ema200) & (c > f.cloud_top)
    dn = f.h1_bear & (c < f.ema200) & (c < f.cloud_bot)
    touch_t = l <= f.tenkan + 0.2 * a
    touched3 = touch_t.shift(1).rolling(3).max() > 0
    atr_up = a > f.atr_mean5
    vol_up = f.get("tick_volume", pd.Series(np.nan, index=f.index)).astype("float64") >= 1.5 * f.vol_mean20
 
    rules = {
        "kijun_pullback": (up & (l <= f.kijun + 0.2 * a) & (c >= f.kijun),
                           dn & (h >= f.kijun - 0.2 * a) & (c <= f.kijun)),
        "tenkan_momentum": (up & atr_up & (f.tenkan > f.kijun) & touch_t & (c > f.tenkan) & (f.body > 0.3 * f.rng),
                            dn & atr_up & (f.tenkan < f.kijun) & (h >= f.tenkan - 0.2 * a) & (c < f.tenkan) & (-f.body > 0.3 * f.rng)),
        "range_break": (up & (c > f.swing_hi) & (f.body > 0.5 * f.rng),
                        dn & (c < f.swing_lo) & (-f.body > 0.5 * f.rng)),
        "pullback_resume": (up & touched3 & (c > ph) & (c > f.tenkan) & (f.body > 0),
                            dn & (h >= f.tenkan - 0.2 * a).shift(1).rolling(3).max().astype(bool) & (c < pl) & (c < f.tenkan) & (f.body < 0)),
        "cloud_break": ((c > f.cloud_top) & (pc <= f.cloud_top.shift(1)) & atr_up,
                        (c < f.cloud_bot) & (pc >= f.cloud_bot.shift(1)) & atr_up),
        "tk_cross": ((f.tenkan > f.kijun) & (f.tenkan.shift(1) <= f.kijun.shift(1)),
                     (f.tenkan < f.kijun) & (f.tenkan.shift(1) >= f.kijun.shift(1))),
        "rejection": ((l < f.swing_lo) & (c > f.swing_lo) & ((np.minimum(c, o) - l) >= 2 * f.body.abs()),
                      (h > f.swing_hi) & (c < f.swing_hi) & ((h - np.maximum(c, o)) >= 2 * f.body.abs())),
        "engulfing": ((f.body > 0) & (po > pc) & (c > po) & (o < pc) & (f.body.abs() > (po - pc)),
                      (f.body < 0) & (po < pc) & (c < po) & (o > pc) & (f.body.abs() > (pc - po))),
        "ignition": ((f.rng > 2 * a) & (f.body > 0.6 * f.rng) & vol_up,
                     (f.rng > 2 * a) & (-f.body > 0.6 * f.rng) & vol_up),
        "exhaustion": ((f.tenkan - c > 3 * a),      # خیلی زیر تنکان -> برگشت صعودی
                       (c - f.tenkan > 3 * a)),     # خیلی بالای تنکان -> برگشت نزولی
    }
 
    rows = []
    for name, (lng, sht) in rules.items():
        for side, mask in (("BUY", lng), ("SELL", sht)):
            m = mask.fillna(False) & a.notna() & (a > 0) & f.ema200.notna() & f.swing_lo.notna()
            idx = f.index[m.to_numpy()]
            if len(idx) == 0:
                continue
            g = f.loc[idx]
            if side == "BUY":
                stop = np.minimum(g.low, g.tenkan) - 0.2 * g.atr
                sl_d = (g.close - stop).clip(lower=1.0 * g.atr, upper=2.5 * g.atr)
            else:
                stop = np.maximum(g.high, g.tenkan) + 0.2 * g.atr
                sl_d = (stop - g.close).clip(lower=1.0 * g.atr, upper=2.5 * g.atr)
            rows.append(pd.DataFrame({
                "bar": idx, "setup": name, "side": side,
                "ref_close": g.close.values, "atr": g.atr.values,
                "sl_dist": sl_d.values, "tp_dist": (2.0 * sl_d).values,
            }))
    if not rows:
        return pd.DataFrame(columns=["bar", "setup", "side", "ref_close", "atr", "sl_dist", "tp_dist"])
    return pd.concat(rows).sort_values("bar").reset_index(drop=True)
