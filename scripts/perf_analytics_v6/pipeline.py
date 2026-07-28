"""v6 anomaly-report pipeline (converted from anomaly_report_pipeline_v6_9.ipynb).

Faithful transplant of the known-good notebook. Deterministic analysis + model
evidence + gateway-LLM verbalization with a self-validating retry loop. The only
behavioral changes vs the notebook:
  * LLM access goes through perf_analytics.llm_gateway (shared with the v1 path)
  * run_v6() takes the CSV path and config as arguments and RETURNS the report
    artifacts; email sending and file moves are the runner's job.
Do not "clean up" the analysis functions — they are validated as-is.
"""
from __future__ import annotations

import json
import os
import re
import re as _re
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import shap
import xgboost as xgb
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split

from perf_analytics.llm_gateway import GatewayConfig, GatewayClient

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# Dataset-agnostic configuration (from notebook Cell 1). Runtime values
# (csv path, processed dir, email) are run_v6() arguments, not globals.
# ---------------------------------------------------------------------------
TIMER_COL, LABEL_COL = "timer", "label"
PRIMARY_DIM   = "page_group"
SECONDARY_DIMS = ["country", "connectiontype", "deviceType", "isp"]
CATEGORICAL   = ["page_group", "country", "deviceType", "os", "browser", "protocol",
                 "connectiontype", "origin_flag", "isp", "landingpage", "paidmedia",
                 "referrer_present"]
DELIVERY_NUMERIC = ["deviceMemory", "rtt", "cacherate", "cdncacherate", "transferbyte",
                    "bodysize", "requestcount", "origintime", "edgetime"]
ARTIFACT_MS       = 60_000
SEVERITY_FLOOR_MS = 50
SAMPLE_DRIFT_TOL  = 3.0
MIN_SEG_N         = 300
MIN_SHARE_PP      = 1.0
MAX_FOCUS         = 3
FEATURE_LABEL_OVERRIDES = {}

MAX_ATTEMPTS = 4

# ---------------------------------------------------------------------------
# Shared gateway LLM binding (replaces notebook Cells 1b/1c/12 call_llm)
# ---------------------------------------------------------------------------
_CLIENT: GatewayClient | None = None

def call_llm(prompt, timeout=300, json_mode=False, max_tokens=None):
    if _CLIENT is None:
        raise RuntimeError("LLM client not initialised; call run_v6() which sets it up.")
    return _CLIENT.chat(prompt, timeout=timeout, json_mode=json_mode, max_tokens=max_tokens)

def list_models(timeout=60):
    if _CLIENT is None:
        raise RuntimeError("LLM client not initialised.")
    return _CLIENT.list_models(timeout=timeout)



# ========================================================================
# Cell 2 — Core statistical modules (v6.8)
"""Core deterministic analysis functions for the LCP anomaly report pipeline.

Design principles (generic, not dataset-specific):
- No hardcoded page names, countries, or segments. All "top movers" are discovered.
- Every reported number is computed here and carried into a findings dict; the
  LLM only verbalizes findings and is validated against the allowed-number set.
"""
import re
import numpy as np
import pandas as pd
from scipy import stats

RNG = np.random.default_rng(42)

# ---------------------------------------------------------------- page groups
def derive_page_group(url_series: pd.Series, top_k: int = 15) -> pd.Series:
    """Extract a coarse page group from URL paths, generic to any site.

    Takes the first path segment after an (optional) short locale segment.
    Groups outside the top_k by traffic volume are folded into 'other'.
    """
    def _seg(u):
        if not isinstance(u, str):
            return "unknown"
        m = re.match(r"^https?://[^/]+/(.*)$", u)
        if not m:
            return "unknown"
        parts = [p for p in m.group(1).split("/") if p]
        if not parts:
            return "home"
        # treat a leading 1-5 char alnum segment as locale (uk, in, sec, latin...)
        if len(parts) >= 2 and re.fullmatch(r"[a-z_\-]{1,5}", parts[0]):
            return parts[1].split("?")[0] or "home"
        return parts[0].split("?")[0] or "home"
    seg = url_series.map(_seg)
    top = seg.value_counts().head(top_k).index
    return seg.where(seg.isin(top), "other")


# ------------------------------------------------------------- window stats
def compute_window_stats(df, timer_col="timer", label_col="label",
                         n_boot=300):
    out = {}
    for lab, key in [(0, "normal"), (1, "anomaly")]:
        s = df.loc[df[label_col] == lab, timer_col]
        out[key] = {
            "count": int(len(s)),
            "mean": round(float(s.mean()), 1),
            "p50": round(float(s.quantile(.50)), 1),
            "p75": round(float(s.quantile(.75)), 2),
            "p90": round(float(s.quantile(.90)), 1),
            "p95": round(float(s.quantile(.95)), 1),
            "p99": round(float(s.quantile(.99)), 1),
        }
    d75 = out["anomaly"]["p75"] - out["normal"]["p75"]
    out["delta_p75_ms"] = round(d75, 2)
    out["delta_p75_pct"] = round(d75 / out["normal"]["p75"] * 100, 2)

    a = df.loc[df[label_col] == 1, timer_col].to_numpy()
    n = df.loc[df[label_col] == 0, timer_col].to_numpy()
    mw = stats.mannwhitneyu(a, n, alternative="two-sided")
    out["mannwhitney_p"] = float(mw.pvalue)

    # bootstrap CI on the p75 difference
    boots = np.empty(n_boot)
    for i in range(n_boot):
        boots[i] = (np.quantile(RNG.choice(a, len(a)), .75)
                    - np.quantile(RNG.choice(n, len(n)), .75))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    out["delta_p75_ci95"] = [round(float(lo), 1), round(float(hi), 1)]
    out["delta_significant"] = bool(lo > 0 or hi < 0)
    return out


def classify_severity(win, abs_floor_ms=50):
    """Severity from the p75 delta, gated by significance and an absolute floor."""
    d, pct = win["delta_p75_ms"], win["delta_p75_pct"]
    if not win["delta_significant"] or abs(d) < abs_floor_ms:
        return "none", "No statistically meaningful change in p75."
    if pct < 0:
        return "improved", "p75 improved versus the normal window."
    if pct < 2:
        return "info", "Very small p75 increase; monitoring only."
    if pct < 5:
        return "low", "Small but significant p75 increase."
    if pct < 15:
        return "warning", "Meaningful p75 degradation."
    return "critical", "Severe p75 degradation."


# ---------------------------------------------------------------- outliers
def audit_outliers(df, timer_col="timer", label_col="label",
                   artifact_ms=60_000):
    """Quantify extreme-tail beacons (likely background-tab artifacts) and
    their influence on the mean, per window."""
    res = {"artifact_threshold_ms": artifact_ms, "windows": {}}
    for lab, key in [(0, "normal"), (1, "anomaly")]:
        s = df.loc[df[label_col] == lab, timer_col]
        art = s[s > artifact_ms]
        mean_all = float(s.mean())
        mean_clean = float(s[s <= artifact_ms].mean()) if (s <= artifact_ms).any() else np.nan
        res["windows"][key] = {
            "artifact_count": int(len(art)),
            "artifact_share_pct": round(len(art) / len(s) * 100, 3),
            "max_timer_ms": int(s.max()),
            "mean_all_ms": round(mean_all, 1),
            "mean_excl_artifacts_ms": round(mean_clean, 1),
            "mean_inflation_ms": round(mean_all - mean_clean, 1),
        }
    return res


# ------------------------------------------------------------ decomposition
def mix_within_decomposition(df, dim, timer_col="timer", label_col="label",
                             stat="mean"):
    """Oaxaca-style decomposition of the overall change in `stat` of timer
    into composition (mix) and within-segment effects along `dim`.
    `dim` may be a column name or a list of columns (joint segments)."""
    if isinstance(dim, (list, tuple)):
        key = df[list(dim)].astype(str).agg("|".join, axis=1)
        dim_name = "x".join(dim)
    else:
        key, dim_name = df[dim].astype(str), dim
    agg = "mean" if stat == "mean" else "median"
    g0 = df[df[label_col] == 0].groupby(key[df[label_col] == 0])[timer_col].agg(["count", agg])
    g1 = df[df[label_col] == 1].groupby(key[df[label_col] == 1])[timer_col].agg(["count", agg])
    j = g0.join(g1, lsuffix="0", rsuffix="1", how="outer").fillna(0)
    j["s0"] = j["count0"] / max(j["count0"].sum(), 1)
    j["s1"] = j["count1"] / max(j["count1"].sum(), 1)
    mix = float(((j.s1 - j.s0) * j[f"{agg}0"]).sum())
    within = float((j.s1 * (j[f"{agg}1"] - j[f"{agg}0"])).sum())
    total = float(df.loc[df[label_col] == 1, timer_col].agg(agg)
                  - df.loc[df[label_col] == 0, timer_col].agg(agg))
    return {"dim": dim_name, "stat": stat, "total_delta_ms": round(total, 1),
            "mix_effect_ms": round(mix, 1), "within_effect_ms": round(within, 1)}


def top_movers(df, dim, timer_col="timer", label_col="label",
               min_n=200, top=5):
    """Discover segments whose traffic share or internal p75 moved the most."""
    n0 = (df[label_col] == 0).sum()
    n1 = (df[label_col] == 1).sum()
    rows = []
    for val, sub in df.groupby(dim, dropna=False):
        c0 = (sub[label_col] == 0).sum()
        c1 = (sub[label_col] == 1).sum()
        if c0 < min_n or c1 < min_n:
            continue
        p75_0 = sub.loc[sub[label_col] == 0, timer_col].quantile(.75)
        p75_1 = sub.loc[sub[label_col] == 1, timer_col].quantile(.75)
        med_0 = sub.loc[sub[label_col] == 0, timer_col].median()
        med_1 = sub.loc[sub[label_col] == 1, timer_col].median()
        rows.append({
            "segment": str(val), "n_normal": int(c0), "n_anomaly": int(c1),
            "share_normal_pct": round(c0 / n0 * 100, 2),
            "share_anomaly_pct": round(c1 / n1 * 100, 2),
            "share_delta_pp": round(c1 / n1 * 100 - c0 / n0 * 100, 2),
            "p75_normal": round(float(p75_0), 1),
            "p75_anomaly": round(float(p75_1), 1),
            "p75_delta_ms": round(float(p75_1 - p75_0), 1),
            "median_normal": round(float(med_0), 1),
            "median_anomaly": round(float(med_1), 1),
        })
    t = pd.DataFrame(rows)
    if t.empty:
        return {"dim": dim, "share_movers": [], "perf_movers": []}
    overall_p75_normal = df.loc[df[label_col] == 0, timer_col].quantile(.75)
    t["slow_segment"] = t["p75_normal"] > overall_p75_normal
    share_movers = t.reindex(t["share_delta_pp"].abs()
                             .sort_values(ascending=False).index).head(top)
    perf_movers = t.reindex(t["p75_delta_ms"].abs()
                            .sort_values(ascending=False).index).head(top)
    return {"dim": dim,
            "share_movers": share_movers.to_dict("records"),
            "perf_movers": perf_movers.to_dict("records")}


def localization_check(df, dim, segment, timer_col="timer", label_col="label"):
    """Is the sitewide p75 change fully explained by one segment?
    Reports overall / segment-only / segment-excluded p75 shifts."""
    def p75s(d):
        return (round(float(d.loc[d[label_col] == 0, timer_col].quantile(.75)), 1),
                round(float(d.loc[d[label_col] == 1, timer_col].quantile(.75)), 1))
    inseg = df[df[dim].astype(str) == str(segment)]
    exseg = df[df[dim].astype(str) != str(segment)]
    o0, o1 = p75s(df); i0, i1 = p75s(inseg); e0, e1 = p75s(exseg)
    return {"dim": dim, "segment": str(segment),
            "overall_p75": [o0, o1], "segment_p75": [i0, i1],
            "excluded_p75": [e0, e1],
            # localized: the rest of the site did not move in the same
            # direction by more than 35% of the overall shift (signed test,
            # so an improvement outside the segment still counts as localized)
            "localized": bool((e1 - e0) < 0.35 * (o1 - o0) if (o1 - o0) > 0
                              else (e1 - e0) > 0.35 * (o1 - o0) if (o1 - o0) < 0
                              else False)}


# ------------------------------------------------------- behavioral signals
def behavior_signals(df, label_col="label",
                     landing_col="landingpage", referrer_col="referrer",
                     clientcache_col="cacherate", transfer_col="transferbyte",
                     conn_col="connectiontype", cellular_value="Cellular"):
    """Generic audience-composition signals. Flags a 'new-visitor influx'
    pattern when session-entry share rises while referrer presence and
    client cache rate fall together. Column names are parameters so the
    module ports to other beacon schemas."""
    sig = {}
    for lab, key in [(0, "normal"), (1, "anomaly")]:
        d = df[df[label_col] == lab]
        sig[key] = {
            "landing_share_pct": round(float((d[landing_col] == True).mean()) * 100, 1)
                                  if landing_col in d else None,
            "referrer_present_pct": round(float(d[referrer_col].notna().mean()) * 100, 1)
                                  if referrer_col in d else None,
            "client_cacherate_median": round(float(d[clientcache_col].median()), 1)
                                  if clientcache_col in d else None,
            "transferbyte_median": int(d[transfer_col].median())
                                  if transfer_col in d else None,
            "cellular_share_pct": round(float((d[conn_col] == cellular_value).mean()) * 100, 1)
                                  if conn_col in d else None,
        }
    n, a = sig["normal"], sig["anomaly"]
    checks = []
    if n["landing_share_pct"] is not None:
        checks.append(a["landing_share_pct"] - n["landing_share_pct"] > 2)
    if n["referrer_present_pct"] is not None:
        checks.append(n["referrer_present_pct"] - a["referrer_present_pct"] > 2)
    if n["client_cacherate_median"] not in (None, 0):
        checks.append(a["client_cacherate_median"]
                      < 0.8 * n["client_cacherate_median"])
    sig["new_visitor_influx"] = bool(checks and sum(checks) >= 2)
    return sig


# -------------------------------------------------------- delivery health
def delivery_health(df, label_col="label", tol_pct=15,
                    metrics=("edgetime", "origintime", "cdncacherate"),
                    origin_flag_col="origin_flag"):
    """CDN/origin health check: verdict is 'clean' unless a delivery metric
    degrades beyond tolerance in the anomaly window."""
    res = {"metrics": {}, "issues": []}
    for m in metrics:
        if m not in df:
            continue
        m0 = float(df.loc[df[label_col] == 0, m].median())
        m1 = float(df.loc[df[label_col] == 1, m].median())
        res["metrics"][m] = {"normal_median": round(m0, 1),
                             "anomaly_median": round(m1, 1)}
        worse = (m1 > m0 * (1 + tol_pct / 100)) if m != "cdncacherate" \
            else (m1 < m0 * (1 - tol_pct / 100))
        base_floor = 20 if m != "cdncacherate" else 0
        if worse and max(m0, m1) > base_floor:
            res["issues"].append(m)
    if origin_flag_col in df:
        o0 = float((df.loc[df[label_col] == 0, origin_flag_col] == "Y").mean()) * 100
        o1 = float((df.loc[df[label_col] == 1, origin_flag_col] == "Y").mean()) * 100
        res["metrics"]["origin_traffic_share_pct"] = {
            "normal_median": round(o0, 1), "anomaly_median": round(o1, 1)}
        if o1 > o0 + 5:
            res["issues"].append("origin_traffic_share")
    res["verdict"] = "degraded" if res["issues"] else "clean"
    return res


# ============================ v3 additions ============================
import re as _re

def representative_urls(frame, group_col="page_group", url_col="url"):
    """Most canonical concrete URL per page group. Prefers the shallowest
    path depth (fewest segments = landing/section root), tie-broken by
    frequency. Query strings and fragments are stripped. Generic to any site."""
    mapping = {}
    if url_col not in frame:
        return mapping
    for grp, sub in frame.groupby(group_col):
        urls = (sub[url_col].dropna().astype(str)
                .map(lambda u: u.split("?")[0].split("#")[0]))
        if urls.empty:
            continue
        vc = urls.value_counts()
        cand = vc.head(8).index.tolist()
        def _depth(u):
            return len([p for p in _re.sub(r"^https?://[^/]+", "", u).split("/") if p])
        mapping[str(grp)] = sorted(cand, key=lambda u: (_depth(u), -int(vc[u])))[0]
    return mapping


def composite_verdict_flags(focus, within_floor_ms=80):
    """Detect whether the focus segment ALSO degraded on its own (a real
    within-segment regression) on top of gaining traffic share. Measured on
    the focus segment's OWN median and p75 deltas — not the site-wide
    decomposition, which nets across many groups and hides local regressions.
    Upgrades a pure 'traffic_mix_shift' verdict to a composite one so the
    within-segment signal is never lost."""
    if not focus:
        return {"within_regression": False, "focus_median_delta_ms": 0.0,
                "focus_p75_delta_ms": 0.0}
    med_d = float(focus.get("median_anomaly", 0) - focus.get("median_normal", 0))
    p75_d = float(focus.get("p75_delta_ms", 0))
    return {"within_regression": bool(med_d >= within_floor_ms
                                      or p75_d >= within_floor_ms),
            "focus_median_delta_ms": round(med_d, 1),
            "focus_p75_delta_ms": round(p75_d, 1)}


# ============ v6.9.2: focus sub-segment (audience/device) breakdown ============
def mem_bucket(v):
    """Coarse device-memory buckets, used both for the focus breakdown and for
    the interacted mix/within decomposition (keeps cells dense)."""
    try:
        m = float(v)
    except (TypeError, ValueError):
        return "na"
    return "low(<=2GB)" if m <= 2 else "mid(4GB)" if m <= 4 else "high(>=8GB)"


_BREAKDOWN_LABELS = {
    "paidmedia": {"True": "paid-media (ad) entries", "False": "non-paid entries"},
    "connectiontype": {"Cellular": "cellular connections", "Cable/DSL": "cable/DSL connections",
                       "Corporate": "corporate networks", "": "unknown-connection traffic"},
    "mem_bucket": {"low(<=2GB)": "low-memory (<=2GB) devices", "mid(4GB)": "4GB-memory devices",
                   "high(>=8GB)": "high-memory (>=8GB) devices"},
    "deviceType": {"Mobile": "mobile devices", "Desktop": "desktop devices", "Tablet": "tablets"},
}


def _breakdown_label(dim, seg):
    if dim == "country":
        return humanize_region(seg)
    return _BREAKDOWN_LABELS.get(dim, {}).get(str(seg), str(seg))


def focus_breakdown(focus_df, dims, focus_p75_normal, timer_col="timer",
                    label_col="label", min_n=100, min_share_pp=1.0, top=3):
    """Within the focus page type, find the audience/device sub-segments whose
    share GREW and that carry ABOVE-focus TBT — the composition shift that
    actually pushed the focus section's aggregate up (e.g. more paid-media,
    lower-memory, or India traffic on a heavy product page). Returns the top
    movers across all dims, ranked by contribution (share gain x heaviness)."""
    cands = []
    for dim in dims:
        if dim not in focus_df:
            continue
        mv = top_movers(focus_df, dim, timer_col, label_col, min_n=min_n)
        for r in mv["share_movers"]:
            if r["share_delta_pp"] < min_share_pp or r["p75_normal"] <= focus_p75_normal:
                continue  # only sub-segments that GREW and are heavier than the focus itself
            cands.append({
                "dim": dim, "segment": str(r["segment"]),
                "human_label": _breakdown_label(dim, r["segment"]),
                "share_normal_pct": r["share_normal_pct"], "share_anomaly_pct": r["share_anomaly_pct"],
                "share_delta_pp": r["share_delta_pp"],
                "p75_normal": r["p75_normal"], "p75_anomaly": r["p75_anomaly"],
                "contribution_score": round(r["share_delta_pp"] / 100.0 * (r["p75_normal"] - focus_p75_normal), 1),
            })
    cands.sort(key=lambda x: -x["contribution_score"])
    return cands[:top]


def focus_regression_split(focus_df, timer_col="timer", label_col="label",
                           dims=("country", "mem_bucket", "paidmedia"), abs_floor_ms=50):
    """Split a focus page type's OWN p75 change into (a) a shift in its internal
    traffic mix (more India / low-memory / paid traffic inside it) vs (b) a
    genuine same-audience slowdown, using the interacted-cell DFL decomposition
    on the section's own rows. Returns the two effects and a `genuine_regression`
    flag that is True only when the genuine part is BOTH material AND the larger
    of the two — so a rise driven mostly by an internal mix shift is not labelled
    'the page slowed on its own'."""
    key = [d for d in dims if d in focus_df]
    if not key or (focus_df[label_col] == 1).sum() == 0 or (focus_df[label_col] == 0).sum() == 0:
        return {"mix_effect_ms": 0.0, "within_effect_ms": 0.0, "genuine_regression": False}
    dec = quantile_decomposition(focus_df, key, q=0.75, timer_col=timer_col, label_col=label_col)
    mat = effect_materiality(dec, abs_floor_ms=abs_floor_ms)
    within, mix = dec["within_effect_ms"], dec["mix_effect_ms"]
    return {"mix_effect_ms": mix, "within_effect_ms": within,
            "genuine_regression": bool(mat.get("within_material") and within >= mix)}


# ============================ v4 additions ============================
def select_focus_segments(primary_movers, overall_win, dim,
                          min_share_pp=1.0, min_p75_delta=80,
                          min_anom_share=1.5, max_focus=3,
                          min_contribution_ratio=0.15, min_contribution_abs=15.0):
    """v4: return a LIST of problem segments, not one. A segment qualifies if
    EITHER (a) it is slower-than-site AND gained meaningful traffic share, OR
    (b) its own p75 degraded materially while carrying non-trivial traffic.
    Ranked by contribution to the sitewide p75 rise; capped at max_focus with
    the remainder summarized. Fully generic — no segment names referenced.

    v6.9.1 materiality gate: a segment is only kept as a headline focus when its
    contribution to the SITEWIDE p75 rise is material. The lead segment is always
    kept; each further segment must contribute at least `min_contribution_ratio`
    of the lead's contribution AND at least `min_contribution_abs`. This stops a
    tiny segment with a large *internal* p75 jump but negligible sitewide impact
    (e.g. a 2% section that even lost traffic) from being co-headlined next to the
    real driver with no explanation. Sub-threshold segments are summarized under
    additional_segments instead of dropped."""
    share = {r["segment"]: r for r in primary_movers["share_movers"]}
    perf = {r["segment"]: r for r in primary_movers["perf_movers"]}
    universe = {**perf, **share}     # union of both mover views

    picked = {}
    for seg, r in universe.items():
        gained_share = (r["share_delta_pp"] >= min_share_pp and r.get("slow_segment"))
        self_regressed = (r["p75_delta_ms"] >= min_p75_delta
                          and r["share_anomaly_pct"] >= min_anom_share)
        if gained_share or self_regressed:
            # contribution proxy to sitewide p75 rise: share-growth pull + own worsening
            contrib = (max(r["share_delta_pp"], 0) / 100.0) * r["p75_normal"] \
                      + (r["share_anomaly_pct"] / 100.0) * max(r["p75_delta_ms"], 0)
            picked[seg] = {**r,
                           "gained_share": bool(gained_share),
                           "self_regressed": bool(self_regressed),
                           "contribution_score": round(float(contrib), 1)}
    ranked = sorted(picked.values(), key=lambda x: -x["contribution_score"])

    # materiality gate: keep the lead; a further segment must clear both a
    # relative (fraction of the lead) and an absolute contribution floor.
    material = []
    if ranked:
        lead_score = ranked[0]["contribution_score"]
        floor = max(min_contribution_abs, min_contribution_ratio * lead_score)
        material = [ranked[0]] + [r for r in ranked[1:] if r["contribution_score"] >= floor]
    material_segs = {r["segment"] for r in material}
    immaterial = [r["segment"] for r in ranked if r["segment"] not in material_segs]

    focus_list = material[:max_focus]
    additional_segments = [r["segment"] for r in material[max_focus:]] + immaterial
    return {"focus_list": focus_list,
            "additional_count": len(additional_segments),
            "additional_segments": additional_segments,
            "total_qualified": len(ranked)}


def coverage_check(df, dim, focus_segments, timer_col="timer", label_col="label"):
    """What fraction of the sitewide p75 rise is attributable to the chosen
    focus segments? Removes those segments and re-measures the residual p75
    shift. Low coverage => the story is incomplete (widen top_k / try another
    dimension)."""
    def p75(d, lab):
        return float(d.loc[d[label_col] == lab, timer_col].quantile(.75))
    o0, o1 = p75(df, 0), p75(df, 1)
    overall = o1 - o0
    rest = df[~df[dim].astype(str).isin([str(s) for s in focus_segments])]
    r0, r1 = p75(rest, 0), p75(rest, 1)
    residual = r1 - r0
    explained = overall - residual
    cov = (explained / overall) if abs(overall) > 1e-9 else 0.0
    return {"overall_p75_delta": round(overall, 1),
            "residual_p75_delta": round(residual, 1),
            "explained_p75_delta": round(explained, 1),
            "coverage_ratio": round(float(cov), 2),
            "sufficient": bool(cov >= 0.7)}


def other_bucket_watch(primary_movers, other_label="other", p75_floor=100):
    """Flag when the catch-all 'other' bucket (groups beyond top_k) itself
    shows a material p75 rise — a hidden segment may be the real culprit."""
    for r in primary_movers["perf_movers"] + primary_movers["share_movers"]:
        if r["segment"] == other_label:
            if r["p75_delta_ms"] >= p75_floor:
                return {"flagged": True, "p75_delta_ms": r["p75_delta_ms"],
                        "share_anomaly_pct": r["share_anomaly_pct"],
                        "note": ("The 'other' bucket (page groups beyond the top "
                                 "tracked set) degraded materially; increase top_k "
                                 "to expose the hidden group.")}
            return {"flagged": False}
    return {"flagged": False}


# ================== v6.8: quantile (p75) decomposition ==================
# The mean-based Oaxaca split above relies on E[T] = sum(share_i * mean_i).
# Percentiles are NOT additive, so the same algebra cannot produce a p75 split.
# Instead we build a counterfactual by reweighting (DFL): the normal window's
# within-segment distributions carried at the anomaly window's composition.
#   mix    = p75(counterfactual) - p75(normal)
#   within = p75(anomaly)        - p75(counterfactual)
# The two sum to the headline p75 change exactly, so the report can decompose
# the SAME statistic it leads with.

def weighted_quantile(values, weights, q):
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    keep = w > 0
    v, w = v[keep], w[keep]
    if v.size == 0:
        return float("nan")
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w) - 0.5 * w
    cw /= np.sum(w)
    return float(np.interp(q, cw, v))


def quantile_decomposition(df, dim, q=0.75, timer_col="timer", label_col="label"):
    """Composition vs within-segment split of a quantile change (see note above).
    `common_support_pct` reports how much anomaly traffic sits in segments that
    also exist in the normal window; segments unique to the anomaly window
    cannot be reweighted and are excluded from the counterfactual."""
    if isinstance(dim, (list, tuple)):
        key_all = df[list(dim)].astype(str).agg("|".join, axis=1)
        dim_name = "x".join(dim)
    else:
        key_all, dim_name = df[dim].astype(str), dim
    is_norm = df[label_col] == 0
    sn = key_all[is_norm].value_counts(normalize=True)
    sa = key_all[~is_norm].value_counts(normalize=True)
    w = key_all[is_norm].map(lambda s: (sa.get(s, 0.0) / sn[s]) if s in sn.index else 0.0)
    covered = float(sa.reindex(sn.index.intersection(sa.index)).sum())
    q_n = float(df.loc[is_norm, timer_col].quantile(q))
    q_c = weighted_quantile(df.loc[is_norm, timer_col], w, q)
    q_a = float(df.loc[~is_norm, timer_col].quantile(q))
    return {"dim": dim_name, "stat": f"p{int(q * 100)}",
            "q_normal": round(q_n, 1), "q_counterfactual": round(q_c, 1),
            "q_anomaly": round(q_a, 1),
            "total_delta_ms": round(q_a - q_n, 1),
            "mix_effect_ms": round(q_c - q_n, 1),
            "within_effect_ms": round(q_a - q_c, 1),
            "common_support_pct": round(covered * 100, 1)}


def effect_materiality(decomp, abs_floor_ms=50, rel_floor=0.20):
    """An effect counts toward the verdict only if it is both absolutely and
    relatively meaningful, so trivial residuals do not flip the storyline."""
    total = abs(decomp.get("total_delta_ms") or 0.0)
    def _material(v):
        return abs(v) >= abs_floor_ms and (total == 0 or abs(v) / total >= rel_floor)
    mix, within = decomp.get("mix_effect_ms", 0.0), decomp.get("within_effect_ms", 0.0)
    return {"mix_material": bool(_material(mix) and mix > 0),
            "within_material": bool(_material(within) and within > 0),
            "mix_effect_ms": mix, "within_effect_ms": within}



# ========================================================================
# Cell 2b — Per-metric configuration profiles (LCP / FCP / TBT / Waiting time)
"""Per-metric configuration profiles.

The pipeline was originally tuned for LCP. Different timers have different units,
scales, "good/poor" thresholds, and artifact ranges, so a single set of
constants misclassifies them. Each profile carries everything the pipeline
reads per-metric; unknown metrics fall back to a generic profile.

Thresholds follow the standard web-performance bands (good / needs-improvement /
poor). LCP/FCP/TTFB are documented Core-Web-Vitals-family cutoffs; TBT uses the
common Lighthouse lab bands. "Waiting time" is treated as TTFB (server/network
wait) unless overridden.
"""

# name aliases -> canonical profile key
METRIC_ALIASES = {
    "largestcontentfulpaint": "lcp", "lcp": "lcp",
    "firstcontentfulpaint": "fcp", "fcp": "fcp",
    "totalblockingtime": "tbt", "tbt": "tbt",
    "waitingtime": "ttfb", "waiting_time": "ttfb", "waiting": "ttfb",
    "timetofirstbyte": "ttfb", "ttfb": "ttfb",
}

METRIC_PROFILES = {
    "lcp": {
        "display_name": "Largest Contentful Paint",
        "abbrev": "LCP", "unit": "ms", "unit_kind": "duration",
        "good_ms": 2500, "poor_ms": 4000,
        "artifact_ms": 60_000,          # background-tab beacons
        "severity_floor_ms": 100,       # min p75 delta worth alerting
        "effect_floor_ms": 50,          # min mix/within effect to be "material"
        "higher_is_worse": True,
        "hero_element": True,           # LCP has a hero element -> preload advice fits
    },
    "fcp": {
        "display_name": "First Contentful Paint",
        "abbrev": "FCP", "unit": "ms", "unit_kind": "duration",
        "good_ms": 1800, "poor_ms": 3000,
        "artifact_ms": 60_000,
        "severity_floor_ms": 80,
        "effect_floor_ms": 40,
        "higher_is_worse": True,
        "hero_element": False,
    },
    "tbt": {
        "display_name": "Total Blocking Time",
        "abbrev": "TBT", "unit": "ms", "unit_kind": "duration",
        "good_ms": 200, "poor_ms": 600,
        "artifact_ms": 30_000,          # blocking time can't plausibly be minutes
        "severity_floor_ms": 40,        # smaller scale than LCP
        "effect_floor_ms": 25,
        "higher_is_worse": True,
        "hero_element": False,          # JS-bound: preload/hero advice does NOT fit
    },
    "ttfb": {
        "display_name": "Waiting Time (Time to First Byte)",
        "abbrev": "TTFB", "unit": "ms", "unit_kind": "duration",
        "good_ms": 800, "poor_ms": 1800,
        "artifact_ms": 60_000,
        "severity_floor_ms": 50,
        "effect_floor_ms": 30,
        "higher_is_worse": True,
        "hero_element": False,          # server/network-bound: origin/CDN advice fits
        "delivery_first": True,         # TTFB regressions point at delivery, not content
    },
    "_generic": {
        "display_name": "Timer", "abbrev": "TIMER", "unit": "ms",
        "unit_kind": "duration", "good_ms": None, "poor_ms": None,
        "artifact_ms": 60_000, "severity_floor_ms": 100, "effect_floor_ms": 50,
        "higher_is_worse": True, "hero_element": False,
    },
}


def resolve_metric_profile(metric_name):
    key = METRIC_ALIASES.get(str(metric_name).strip().lower(), "_generic")
    prof = dict(METRIC_PROFILES[key])
    prof["profile_key"] = key
    prof["raw_name"] = metric_name
    return prof


def rate_value(profile, value_ms):
    """good / needs-improvement / poor for a p75 value, per the profile bands."""
    g, p = profile.get("good_ms"), profile.get("poor_ms")
    if g is None or p is None or value_ms is None:
        return None
    if value_ms <= g:
        return "good"
    if value_ms <= p:
        return "needs-improvement"
    return "poor"



# ========================================================================
# Cell 3 — Model modules (evidence generators)
"""Model layer for the anomaly report pipeline.

Two models with distinct, honest roles:
1. Window classifier (context features only, NO timer, NO delivery metrics):
   answers "did the traffic composition change?" — gated by holdout AUC.
   Its SHAP output is labeled as a *composition fingerprint*, never as a
   performance cause.
2. Timer regressor on log1p(timer) (context + delivery metrics):
   answers "what drives LCP?" — the per-feature difference in mean SHAP
   between windows attributes the predicted LCP shift to features.
"""
import numpy as np
import pandas as pd
import xgboost as xgb
import shap
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

MAX_CAT_CARDINALITY = 20      # top-N categories kept per column, rest folded
SHAP_SAMPLE = 15_000          # rows sampled per window for SHAP


def build_features(df, categorical_cols, numeric_cols):
    """One-hot with cardinality capping. drop_first=False so every category
    is interpretable on its own (no hidden baseline)."""
    X = pd.DataFrame(index=df.index)
    for c in numeric_cols:
        if c in df:
            X[c] = pd.to_numeric(df[c], errors="coerce")
    cats = pd.DataFrame(index=df.index)
    for c in categorical_cols:
        if c not in df:
            continue
        s = df[c].astype(str).fillna("unknown")
        top = s.value_counts().head(MAX_CAT_CARDINALITY).index
        cats[c] = s.where(s.isin(top), "other")
    if len(cats.columns):
        X = pd.concat([X, pd.get_dummies(cats, drop_first=False)], axis=1)
    return X


def window_classifier_fingerprint(df, categorical_cols, label_col="label",
                                  auc_gate=0.60, seed=42):
    """Train label(window) classifier on context features only.
    Returns AUC and, if the gate passes, the top composition-shift features."""
    X = build_features(df, categorical_cols, numeric_cols=[])
    y = df[label_col].astype(int)
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.25,
                                          stratify=y, random_state=seed)
    clf = xgb.XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.1,
                            eval_metric="logloss", n_jobs=4,
                            random_state=seed)
    clf.fit(Xtr, ytr)
    auc = float(roc_auc_score(yte, clf.predict_proba(Xte)[:, 1]))
    result = {"holdout_auc": round(auc, 3), "gate_passed": auc >= auc_gate,
              "fingerprint": []}
    if auc < auc_gate:
        result["note"] = ("Traffic composition of the two windows is nearly "
                          "indistinguishable; composition fingerprint skipped.")
        return result
    idx = np.random.default_rng(seed).choice(
        len(Xte), min(SHAP_SAMPLE, len(Xte)), replace=False)
    sv = shap.TreeExplainer(clf).shap_values(Xte.iloc[idx])
    imp = pd.Series(np.abs(sv).mean(0), index=X.columns)
    for feat, val in imp.sort_values(ascending=False).head(8).items():
        on_share_normal = float(X.loc[y == 0, feat].mean()) * 100
        on_share_anom = float(X.loc[y == 1, feat].mean()) * 100
        result["fingerprint"].append({
            "feature": feat, "mean_abs_shap": round(float(val), 4),
            "share_normal_pct": round(on_share_normal, 2),
            "share_anomaly_pct": round(on_share_anom, 2),
            "share_delta_pp": round(on_share_anom - on_share_normal, 2)})
    return result


def timer_regressor_drivers(df, categorical_cols, numeric_cols,
                            timer_col="timer", label_col="label",
                            artifact_ms=60_000, seed=42, top=10):
    """Regress log1p(timer) on context + delivery features; attribute the
    window-to-window predicted shift via the per-feature mean-SHAP delta.
    Artifact beacons (timer > artifact_ms) are excluded from training so the
    model explains typical user experience rather than background tabs."""
    d = df[df[timer_col] <= artifact_ms].copy()
    X = build_features(d, categorical_cols, numeric_cols)
    y = np.log1p(d[timer_col].astype(float))
    reg = xgb.XGBRegressor(n_estimators=300, max_depth=6, learning_rate=0.08,
                           n_jobs=4, random_state=seed)
    reg.fit(X, y)
    r2 = float(reg.score(X, y))

    rng = np.random.default_rng(seed)
    parts = []
    for lab in (0, 1):
        pos = np.flatnonzero((d[label_col] == lab).to_numpy())
        parts.append(rng.choice(pos, min(SHAP_SAMPLE, len(pos)), replace=False))
    idx = np.concatenate(parts)
    Xs = X.iloc[idx]
    labs = d[label_col].to_numpy()[idx]
    sv = shap.TreeExplainer(reg).shap_values(Xs)

    mean_normal = sv[labs == 0].mean(0)
    mean_anom = sv[labs == 1].mean(0)
    delta = mean_anom - mean_normal          # log-space contribution shift
    order = np.argsort(-np.abs(delta))[:top]
    drivers = []
    for i in order:
        feat = X.columns[i]
        drivers.append({
            "feature": feat,
            "shap_delta_log": round(float(delta[i]), 4),
            "direction": "worsening" if delta[i] > 0 else "improving",
            "value_median_normal": round(float(
                pd.to_numeric(Xs.loc[labs == 0, feat], errors="coerce").median()), 2),
            "value_median_anomaly": round(float(
                pd.to_numeric(Xs.loc[labs == 1, feat], errors="coerce").median()), 2),
        })
    pred_shift_pct = round(float(np.exp(sv[labs == 1].sum(1).mean()
                                        - sv[labs == 0].sum(1).mean()) - 1) * 100, 2)
    return {"train_r2": round(r2, 3),
            "predicted_shift_pct": pred_shift_pct,
            "drivers": drivers}



# ========================================================================
# Cell 4 — Findings, verdict, validation layer, email HTML (v6.8)
"""Findings assembly, verdict selection, LLM verbalization and validation."""
import json
import re
import re as _re
import numpy as np


# ------------------------------------------------------------- humanization
def humanize_feature(feat, overrides=None):
    """Generic feature-name -> customer-readable phrase. Works for any
    one-hot 'col_value' name without dataset-specific hardcoding."""
    overrides = overrides or {}
    if feat in overrides:
        return overrides[feat]
    generic = {
        "page_group": "the '{v}' page type",
        "country": "traffic from region '{v}'",
        "isp": "users on network provider '{v}'",
        "connectiontype": "'{v}' network connections",
        "deviceType": "'{v}' devices",
        "os": "'{v}' devices",
        "browser": "'{v}' browser sessions",
        "protocol": "connections over '{v}'",
        "landingpage": "session-entry page views" ,
        "referrer_present": "visits arriving without a referrer",
        "paidmedia": "paid-media traffic",
        "origin_flag": "requests served via origin",
    }
    numeric = {
        "cacherate": "browser cache hit rate",
        "cdncacherate": "CDN cache hit rate",
        "transferbyte": "bytes transferred over the network",
        "bodysize": "total page weight",
        "requestcount": "number of page requests",
        "origintime": "origin response time",
        "edgetime": "edge response time",
        "rtt": "network round-trip time",
        "deviceMemory": "device memory",
    }
    if feat in numeric:
        return numeric[feat]
    for col, tpl in generic.items():
        if feat.startswith(col + "_"):
            return tpl.format(v=feat[len(col) + 1:])
        if feat == col:
            return tpl.format(v="")
    return feat.replace("_", " ")


# --------------------------------------------------------- verdict selection
def select_verdict(severity, decomp_primary, localization, behavior,
                   delivery, outliers, within_flags=None,
                   focus_selection=None, coverage=None, materiality=None):
    """Rule-based story selection. v6.8: the mix/within split now comes from the
    p75 decomposition (same statistic as the headline), and an effect counts
    only when it is materially large, so both effects can be reported when both
    are real instead of forcing a single winner."""
    art_norm=outliers["windows"]["normal"]["mean_inflation_ms"]
    art_anom=outliers["windows"]["anomaly"]["mean_inflation_ms"]
    outlier_note=(art_norm>100 or art_anom>100)
    focus_list=(focus_selection or {}).get("focus_list", [])
    n_focus=len(focus_list)
    multi=n_focus>=2
    low_cov=bool(coverage and not coverage.get("sufficient"))

    if severity in ("none","info","improved"):
        s=("No meaningful page-load degradation was found between the two windows.")
        if outlier_note:
            s+=(" A small number of extreme-duration beacons inflate the average; "
                "percentile-based views are recommended.")
        return "no_action", s

    if delivery["verdict"]=="degraded":
        return "delivery_regression", (
            "Delivery-layer metrics degraded in the anomaly window; CDN or origin "
            "behavior should be investigated first.")

    m = materiality or {}
    mix_material = bool(m.get("mix_material"))
    within_material = bool(m.get("within_material",
                                 within_flags and within_flags.get("within_regression")))

    if multi:
        s=(f"The slowdown spans {n_focus} page types rather than one. ")
        # v6.9.4: "slowed on its own" now means the composition-controlled genuine
        # regression, not a raw per-section p75 rise driven by an internal mix shift.
        any_self=any(r.get("genuine_regression") for r in focus_list)
        any_share=any(r.get("gained_share") for r in focus_list)
        # A per-section p75 rise only becomes the headline story when the
        # aggregate within-segment effect is material; otherwise the composition
        # shift is the real driver and the verdict must not contradict the
        # decomposition printed alongside it.
        regression_story = bool(any_self and within_material)
        # Describe what actually happened per role — a co-focus can slow on its
        # own WITHOUT gaining share, so never blanket-claim they all grew share.
        if any_share and any_self:
            s+=("Some grew their share of heavier traffic while others slowed on their "
                "own even as their share fell; each is listed with its own evidence.")
        elif any_share:
            s+=("The slower page types grew their share of traffic while the rest of the "
                "site held steady.")
        else:
            s+=("These page types slowed on their own in the anomaly window even without "
                "gaining traffic share.")
        code="multi_segment_regression" if regression_story else "multi_segment_mix_shift"
    elif mix_material and within_material:
        code="mix_shift_with_local_regression"
        s=("The slowdown has two compounding causes: a slower page type grew its "
           "share of traffic AND that section became genuinely slower on its own in "
           "the anomaly window.")
        if behavior.get("new_visitor_influx"):
            s+=(" The incoming traffic shows a new-visitor pattern (more session "
                "entries, fewer referrers, colder browser caches), consistent with a "
                "campaign or event launch.")
    elif mix_material:
        code="traffic_mix_shift"
        s=("The slowdown is a traffic-composition effect: a slower page type grew "
           "its share of traffic while the rest of the site held steady or improved.")
        if behavior.get("new_visitor_influx"):
            s+=(" The incoming traffic shows a new-visitor pattern (more session "
                "entries, fewer referrers, colder browser caches), consistent with a "
                "campaign or event launch.")
    elif within_material:
        code="segment_regression"
        s=("Specific segments became genuinely slower in the anomaly window; the "
           "change is not explained by traffic composition.")
    else:
        code="traffic_mix_shift_broad"
        s=("The change is spread thinly across segments, with no single composition "
           "or per-section effect dominating.")

    if low_cov:
        s+=(f" Note: the identified sections explain only "
            f"{int(round(coverage['coverage_ratio']*100))}% of the p75 change; a "
            f"further contributor remains unaccounted for.")
    return code, s


# ------------------------------------------------------- findings container
def collect_numbers(obj, acc=None):
    if acc is None:
        acc = set()
    if isinstance(obj, dict):
        for v in obj.values():
            collect_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_numbers(v, acc)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float, np.integer, np.floating)):
        acc.add(round(float(obj), 2))
    return acc


def validate_report_numbers(report_text, allowed, small_int_max=15):
    """Every number in the report must exist in the findings (or be a small
    structural integer). Returns the list of unauthorized numbers."""
    bad = []
    for m in re.finditer(NUMBER_RE, report_text):
        raw = m.group(0).replace(",", "")
        try:
            v = float(raw)
        except ValueError:
            continue
        if v <= small_int_max and v == int(v):
            continue
        cands = {round(v, 2), round(v, 1), float(int(v))}
        if not any(c in allowed for c in cands):
            # tolerate values that round-trip to an allowed number
            if not any(abs(c - a) <= 0.51 for a in allowed for c in [v]):
                bad.append(m.group(0))
    return sorted(set(bad))


# ------------------------------------------------------------ LLM prompting
def build_llm_prompt(findings_json_text):
    return f"""You are a website performance analyst writing for a business customer.

Your ONLY source of truth is the FINDINGS JSON below. Verbalize it; do not analyze.

Strict rules:
- English only.
- Use ONLY numbers that literally appear in the FINDINGS JSON. Never compute,
  convert, or invent numbers. If unsure, omit the number.
- To refer to any page type, write its token EXACTLY as given in
  findings (e.g. the "token" field, like \u27e6PG:unpacked\u27e7). Do NOT
  rewrite, translate, or expand tokens; copy them verbatim. A later step
  turns each token into a readable name and an example URL.
- CAUSE vs SYMPTOM: items under findings.performance_drivers are candidate
  drivers. Items under findings.associated_symptoms (e.g. browser cache hit
  rate, transferred bytes, session-entry share) are INDICATORS of a traffic
  change, NOT causes. Never call a symptom a "driver", "cause", or "root
  cause". Describe symptoms as evidence of who is visiting, not as reasons
  the site got slower.
- No machine-learning jargon (no SHAP, model, classifier, feature, one-hot).
- Follow the verdict: the report storyline must match findings.verdict.sentence.
- If findings.delivery.verdict is "clean", explicitly reassure the customer
  that CDN and origin infrastructure show no regression.

Output format (Markdown, exactly these sections):
## Executive Summary
(2-3 sentences: the transition sentence in your own words, then the verdict.)
## What Changed
(List EVERY page type in findings.segments.focus_list, each with its token
and its own share/p75 change and role. If findings.coverage.sufficient is
false, state plainly that the listed sections do not fully explain the change.
Present behavior items as audience indicators, never as causes.)
## What Did Not Change
(Delivery-layer health; segments that stayed stable or improved.)
## Recommended Actions
(3-4 practical actions matched to the verdict code.)
## Monitoring Notes
(1-2 sentences on what to watch next.)

FINDINGS JSON:
{findings_json_text}
"""


# ------------------------------------------------------- deterministic fall-back
def render_fallback_report(f):
    """Deterministic report. v4: iterates over ALL focus segments and adds a
    coverage note when the identified sections don't fully explain the change."""
    from_token=f.get("_token_fn", lambda s: f"the '{s}' page type")
    h,v=f["headline"], f["verdict"]
    lines=["## Executive Summary", h["transition_sentence"], v["sentence"], ""]
    lines.append("## What Changed")
    focus_list=f["segments"].get("focus_list") or []
    if focus_list:
        for r in focus_list:
            tok=from_token(r["segment"])
            bits=[f"traffic share {fmt_pct(r['share_normal_pct'])} to {fmt_pct(r['share_anomaly_pct'])}",
                  f"p75 {fmt_ms(r['p75_normal'])} to {fmt_ms(r['p75_anomaly'])}"]
            role=("gained share and slowed on its own" if r.get("gained_share") and r.get("self_regressed")
                  else "grew its share of traffic" if r.get("gained_share")
                  else "became slower on its own")
            lines.append(f"- {tok} ({role}): " + "; ".join(bits))
        extra=f["segments"].get("additional_count", 0)
        if extra:
            lines.append(f"- Plus {extra} more page type(s) with smaller contributions.")
    else:
        for mv in f["segments"]["primary_share_movers"][:3]:
            lines.append(f"- {from_token(mv['segment'])}: traffic share "
                         f"{fmt_pct(mv['share_normal_pct'])} to {fmt_pct(mv['share_anomaly_pct'])}; "
                         f"p75 {fmt_ms(mv['p75_normal'])} to {fmt_ms(mv['p75_anomaly'])}")
    b=f.get("behavior", {})
    if b.get("new_visitor_influx"):
        lines.append(f"- Audience indicator (new visitors, not a cause): session-entry "
                     f"share {fmt_pct(b['normal']['landing_share_pct'])} to "
                     f"{fmt_pct(b['anomaly']['landing_share_pct'])}, browser cache hit rate median "
                     f"{fmt_pct(b['normal']['client_cacherate_median'])} to "
                     f"{fmt_pct(b['anomaly']['client_cacherate_median'])}")
    cov=f.get("coverage")
    if cov and not cov.get("sufficient"):
        lines.append(f"- Coverage note: the sections above explain "
                     f"{int(round(cov['coverage_ratio']*100))}% of the p75 change "
                     f"({fmt_ms(cov['explained_p75_delta'])} of {fmt_ms(cov['overall_p75_delta'])}); "
                     f"a further contributor remains unaccounted for.")
    ob=f.get("other_watch")
    if ob and ob.get("flagged"):
        lines.append(f"- The catch-all 'other' bucket also degraded "
                     f"(p75 +{fmt_ms(ob['p75_delta_ms'])}); a page type beyond the tracked "
                     f"set may be involved.")
    lines.append("")
    lines.append("## What Did Not Change")
    _deliv = f.get("delivery") or {}
    _has_cdn = "cdncacherate" in (_deliv.get("metrics") or {})
    if _deliv.get("verdict")=="clean":
        lines.append("- CDN and origin delivery metrics show no regression.")
        if _has_cdn:
            lines.append("- The CDN cache hit rate here is derived from client beacons and "
                         "may differ from the actual cache hit rate reported by the CDN; "
                         "use it for reference only.")
    if f.get("localization", {}).get("localized"):
        ex=f["localization"]["excluded_p75"]
        lines.append(f"- Excluding the focus section(s), sitewide p75 moved from "
                     f"{fmt_ms(ex[0])} to {fmt_ms(ex[1])}.")
    lines.append("")
    lines.append("## Recommended Actions")
    actions={
        "traffic_mix_shift":[
            "Pre-optimize the growing page type's largest visual element (preload, right-sized images) for first-time mobile visitors.",
            "Apply adaptive image/video delivery for cellular connections in the growing regions.",
            "Split alerting by page group and region during campaign periods to avoid composition-driven alerts."],
        "mix_shift_with_local_regression":[
            "Investigate the growing section's own slowdown: compare the LCP element and resource waterfall between the two windows.",
            "Pre-optimize that section's hero image/video (preload, right-sized, adaptive for cellular) for cold-cache first-time visitors.",
            "Verify no recent release or third-party tag change landed on that page type in the anomaly window.",
            "Split alerting by page group and region so the composition shift and the local regression are tracked separately."],
        "multi_segment_mix_shift":[
            "Address each listed section's traffic growth: right-size and preload its main visual element for new mobile visitors.",
            "Prioritize the sections by their contribution score shown above.",
            "Split alerting by page group so simultaneous composition shifts are visible individually."],
        "multi_segment_regression":[
            "Treat each listed section as a separate regression: compare LCP element and resource waterfalls per section between windows.",
            "Check for a shared root cause across the affected sections (common template, shared third-party tag, platform release).",
            "Prioritize remediation by the contribution score shown above.",
            "Split alerting by page group so concurrent regressions are not averaged away."],
        "traffic_mix_shift_broad":[
            "Review campaign traffic routing and landing-page weight across the growing segments.",
            "Split alerting by page group and region."],
        "delivery_regression":[
            "Investigate CDN cache hit rate and origin response times for the anomaly window.",
            "Check recent configuration or deployment changes on the delivery path."],
        "segment_regression":[
            "Debug the slowed segments individually (page weight, third-party tags, recent releases).",
            "Compare resource waterfalls between windows for the affected segments."],
        "no_action":[
            "No action required; continue monitoring.",
            "Consider percentile-based alerting to reduce sensitivity to extreme-duration beacons."],
    }
    for a in actions.get(v["code"], actions["no_action"]):
        lines.append(f"- {a}")
    if cov and not cov.get("sufficient"):
        lines.append("- Widen the tracked page-group set (increase top_k) or analyze another dimension; the current sections do not fully explain the change.")
    lines.append("")
    lines.append("## Monitoring Notes")
    mon = (f.get("narrative_facts") or {}).get("monitoring")
    lines.append(mon or "Watch whether the p75 returns to baseline as the traffic "
                        "composition normalizes.")
    return "\n".join(lines)


# ------------------------------------------------------------- email html
def md_to_html(text):
    lines, html, in_list = text.splitlines(), [], False
    for line in lines:
        if line.startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{line[2:]}</li>")
            continue
        if in_list:
            html.append("</ul>")
            in_list = False
        if line.startswith("### "):
            html.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            html.append(f"<h1>{line[2:]}</h1>")
        elif line.strip() == "":
            html.append("")
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            html.append(f"<p>{line}</p>")
    if in_list:
        html.append("</ul>")
    return ("<html><body style='font-family:Arial,sans-serif;max-width:720px'>"
            + "\n".join(html) + "</body></html>")


# ==================== email localization (translate the validated English) ====
# The English report is the single validated source. For non-English recipients
# we translate the finished English email via the gateway LLM, keeping every
# number, unit, URL and product/metric name verbatim, and verify that nothing
# load-bearing was dropped. On any failure the caller falls back to English.
_URL_RE = r"https?://[^\s)\]}>\"']+"
_LANG_NAMES = {"ko": "Korean"}

# Metric and Akamai product/technical names that MUST stay in English. They are
# swapped for opaque placeholders before translation and restored afterwards, so
# the model cannot translate them (prompt instructions alone were not reliable).
# Longest-first so multi-word names are protected before their abbreviations.
_PROTECTED_TERMS = [
    "Image & Video Manager", "Adaptive Acceleration", "Tiered Distribution",
    "Total Blocking Time", "Largest Contentful Paint", "First Contentful Paint",
    "Time to First Byte", "Interaction to Next Paint", "Cumulative Layout Shift",
    "First Input Delay", "Script Management", "Cloud Wrapper", "DataStream 2",
    "EdgeWorkers", "mPulse", "TBT", "LCP", "FCP", "TTFB", "INP", "CLS", "FID",
]


def _preserved_tokens(text):
    """Numbers (comma-normalized) and URLs that MUST survive translation."""
    nums = {m.replace(",", "") for m in re.findall(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?", text)}
    urls = set(re.findall(_URL_RE, text))
    return nums, urls


def _protect_terms(text):
    """Replace protected English terms with opaque ⟦X#⟧ placeholders."""
    mapping = {}
    for i, term in enumerate(_PROTECTED_TERMS):
        ph = f"⟦X{i}⟧"
        new, n = re.subn(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", ph, text)
        if n:
            text, mapping[ph] = new, term
    return text, mapping


def _translate_once(text, target_lang_name):
    protected, mapping = _protect_terms(text)
    prompt = (
        f"You are a senior web-performance analyst. Rewrite the following report in "
        f"fluent, natural {target_lang_name} for a professional audience.\n"
        "Guidance:\n"
        f"- Write idiomatic, easy-to-read {target_lang_name} as a native analyst would — "
        "convey the meaning naturally, do NOT translate word-for-word or keep stiff "
        "English sentence structure.\n"
        "- Keep every number, unit (ms, %, etc.) and URL EXACTLY as in the source — "
        "never add, drop, round, or reformat a number.\n"
        "- Do NOT translate or alter any ⟦X…⟧ placeholder token; copy each one exactly "
        "as-is, in place (these are product/metric names kept in English).\n"
        "- Preserve the Markdown structure: keep every '##' heading and every '-' bullet, "
        "and translate ALL of them through to the very last line — do not stop early.\n"
        "- Output only the translated Markdown. No code fences, no notes, no reasoning.\n\n"
        f"{protected}"
    )
    # long reports need more than the default 2048-token cap or the tail is cut off
    out = call_llm(prompt, max_tokens=4096)
    out = re.sub(r"<think>.*?</think>", "", out, flags=re.DOTALL | re.IGNORECASE)
    out = re.sub(r"</?think>", "", out)
    missing = [ph for ph in mapping if ph not in out]
    if missing:
        raise RuntimeError(f"translation dropped protected tokens: {missing[:3]}")
    for ph, term in mapping.items():
        out = out.replace(ph, term)
    return out.strip()


def localize_email(target_lang, subject, plain_md, max_attempts=2):
    """Translate the English email (subject + plain-Markdown body) into
    target_lang and return (subject, plain, html). Returns None on any failure —
    LLM error, unknown language, or a number/URL that did not survive
    translation — so the caller falls back to the English email. Numbers, units,
    URLs and product/metric names are required to be preserved verbatim."""
    if target_lang == "en":
        return subject, plain_md, md_to_html(plain_md)
    lang_name = _LANG_NAMES.get(target_lang)
    if not lang_name:
        return None

    # body is load-bearing: every number and URL must survive
    src_nums, src_urls = _preserved_tokens(plain_md)
    body = None
    for _ in range(max_attempts):
        try:
            cand = _translate_once(plain_md, lang_name)
        except Exception as exc:
            print(f"[i18n] body translation error: {exc}")
            continue
        c_nums, c_urls = _preserved_tokens(cand)
        if cand and src_nums <= c_nums and src_urls <= c_urls:
            body = cand
            break
        print(f"[i18n] retry ({target_lang}): missing numbers={sorted(src_nums - c_nums)[:5]} "
              f"urls={sorted(src_urls - c_urls)[:2]}")
    if body is None:
        print(f"[i18n] {target_lang} body failed number/URL preservation; falling back to English")
        return None

    # subject is best-effort: keep the English subject if translation is unsafe
    subj_out = subject
    subj_nums, _ = _preserved_tokens(subject)
    try:
        cand_s = _translate_once(subject, lang_name).splitlines()
        cand_s = cand_s[0].strip() if cand_s else ""
        cs_nums, _ = _preserved_tokens(cand_s)
        if cand_s and subj_nums <= cs_nums and "[v6]" in cand_s:
            subj_out = cand_s
    except Exception as exc:
        print(f"[i18n] subject translation error (keeping English): {exc}")

    return subj_out, body, md_to_html(body)


# ============================ v3 additions ============================
PG_TOKEN = "\u27e6PG:{}\u27e7"          # ⟦PG:unpacked⟧ — LLM-opaque placeholder

def page_token(segment):
    return PG_TOKEN.format(segment)

def render_page_tokens(text, url_map, label_map):
    """Deterministically replace ⟦PG:<seg>⟧ tokens with a customer-readable
    label + a concrete representative URL on FIRST mention, label-only after.
    This does NOT depend on the LLM reproducing any exact phrase, so URLs can
    never be silently dropped or mangled."""
    seen = set()
    def _repl(m):
        seg = m.group(1)
        label = label_map.get(seg, f"the '{seg}' page type")
        url = url_map.get(seg)
        if seg in seen or not url:
            return label
        seen.add(seg)
        return f"{label} (e.g., {url})"
    out = _re.sub(r"\u27e6PG:([^\u27e7]+)\u27e7", _repl, text)
    # tidy any accidental double article the LLM may have written before a token
    out = _re.sub(r"\b(the|The)\s+the\b", r"\1", out)
    return out


CAUSAL_WORDS = ("driver", "drivers", "cause", "caused", "causes", "root cause",
                "responsible for", "led to", "leading to", "due to")

def check_causal_misuse(text, symptom_labels):
    """Flag drafts that describe an ASSOCIATED SYMPTOM (e.g. browser cache hit
    rate) as a causal driver. Returns the offending symptom labels."""
    offenders = []
    low = text.lower()
    for lbl in symptom_labels:
        l = lbl.lower()
        if l not in low:
            continue
        for m in _re.finditer(_re.escape(l), low):
            window = low[max(0, m.start() - 60): m.end() + 60]
            if any(w in window for w in CAUSAL_WORDS):
                offenders.append(lbl)
                break
    return sorted(set(offenders))


# ============================ v6 validation layer ============================
# Fixes the deadlock and false-positive problems found in v5 operation:
#   - numbers we ourselves put in the prompt were never whitelisted
#   - a single soft violation discarded an otherwise-good draft
#   - the causal gate matched across sentence boundaries

# Shared numeric literal pattern. The leading minus MUST be captured: findings
# legitimately hold negative deltas (a residual p75 change of -44 ms), and
# reading that back as "44" made our own sentence fail the binding check.
NUMBER_RE = r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?"

STRICT_SECTIONS = ("executive summary", "what changed", "what did not change")

def build_allowed_numbers(findings, findings_text, extra=()):
    """v6: whitelist = numeric values in findings  UNION  every number that
    literally appears in the prompt text we hand the model (playbook actions,
    lever names, hypotheses...). Without the second half the model is punished
    for faithfully quoting our own strings."""
    allowed = set(collect_numbers(findings))
    for m in re.finditer(NUMBER_RE, findings_text or ""):
        try:
            allowed.add(round(float(m.group(0).replace(",", "")), 2))
        except ValueError:
            pass
    for v in extra:
        try:
            allowed.add(round(float(v), 2))
        except (TypeError, ValueError):
            pass
    return allowed


def _split_sections(text):
    """Return [(section_title_lower, body), ...] for a markdown report."""
    parts = re.split(r"^##\s*(.+)$", text, flags=re.MULTILINE)
    out, i = [], 1
    if parts and parts[0].strip():
        out.append(("", parts[0]))
    while i + 1 < len(parts) + 1 and i < len(parts):
        title = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        out.append((title, body))
        i += 2
    return out


def validate_report_numbers_v6(report_text, allowed, small_int_max=15):
    """Section-aware number validation. Numbers inside claim sections are HARD
    violations; numbers elsewhere (recommendations, monitoring, hypotheses —
    where product names and protocol numbers legitimately appear) are SOFT and
    can be auto-repaired instead of triggering a full regeneration."""
    hard, soft = [], []
    for title, body in _split_sections(report_text):
        body_wo_urls = re.sub(r"https?://\S+", "", body)
        strict = any(s in title for s in STRICT_SECTIONS)
        for m in re.finditer(NUMBER_RE, body_wo_urls):
            raw = m.group(0).replace(",", "")
            try:
                v = float(raw)
            except ValueError:
                continue
            if v <= small_int_max and v == int(v):
                continue
            if any(abs(v - a) <= 0.51 for a in allowed):
                continue
            (hard if strict else soft).append(m.group(0))
    return {"hard": sorted(set(hard)), "soft": sorted(set(soft))}


# ---------------------------------------------------------- causal gate v6
CAUSAL_PATTERNS = [
    r"\bis (?:the|a|one of the)?\s*(?:most significant |main |primary |key |top )?"
    r"(?:driver|cause|root cause|reason)\b",
    r"\bwas (?:the|a)\s*(?:driver|cause|root cause|reason)\b",
    r"\bdrove\b", r"\bcaused\b", r"\bled to\b", r"\bresponsible for\b",
    r"\bmade the (?:site|page|experience) slow", r"\bstemmed from\b",
    r"\bthe culprit\b", r"\bis what (?:made|caused)\b",
]
NEGATION_MARKERS = ("not a cause", "not the cause", "not a driver",
                    "not the driver", "rather than a cause", "not because",
                    "is not responsible", "indicator", "symptom",
                    "not a performance cause")


def _sentences(text):
    return re.split(r"(?<=[.!?])\s+|\n", text)


def check_causal_misuse_v6(text, symptom_labels):
    """v6: sentence-scoped and syntactically bound. A symptom is flagged only
    when a causal construction appears IN THE SAME SENTENCE and that sentence
    is not explicitly disclaiming causality. Deliberately conservative — the
    critic pass handles subtler cases."""
    offenders = []
    for sent in _sentences(text):
        low = sent.lower()
        if not low.strip():
            continue
        if any(n in low for n in NEGATION_MARKERS):
            continue
        if not any(re.search(p, low) for p in CAUSAL_PATTERNS):
            continue
        for lbl in symptom_labels:
            if lbl.lower() in low:
                offenders.append(lbl)
    return sorted(set(offenders))


# ------------------------------------------------------- repair & scoring
def repair_soft_numbers(report_text, soft_numbers):
    """Deterministically neutralize soft violations: drop the offending
    numeric token (and a bare parenthetical wrapper if that's all it held)
    rather than discarding an otherwise-valid draft."""
    out = report_text
    units = r"(?:\s*(?:ms|s|kb|mb|gb|%|px|KB|MB|GB|MS))?"
    for num in soft_numbers:
        out = re.sub(r"\s*\(\s*" + re.escape(num) + units + r"\s*\)", "", out)
        out = re.sub(r"(?<![\w.])" + re.escape(num) + units + r"(?![\w.])", "", out)
    out = re.sub(r"\(\s*[/,;]?\s*\)", "", out)      # empty parens left behind
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([.,;])", r"\1", out)
    return out


def score_draft(num_result, raw_feats, causal_bad, scope_bad, has_summary):
    """Lower is better. Hard problems weigh far more than soft ones so a
    near-miss draft still beats the generic template."""
    return (100 * len(num_result["hard"]) + 100 * len(raw_feats)
            + 60 * len(causal_bad) + 60 * len(scope_bad)
            + 5 * len(num_result["soft"]) + (0 if has_summary else 200))


# ========================== v6.1: draft normalization ==========================
def normalize_draft(text):
    """Absorb harmless formatting variance instead of rejecting it. Strips code
    fences and <think> blocks (Qwen3 emits these), and normalizes any heading
    style to '## Title'. Heading spelling is unrelated to factual accuracy, so
    tolerating it saves regeneration cycles on smaller models."""
    t = text.strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"^\s*```[a-zA-Z]*\s*\n?", "", t)
    t = re.sub(r"\n?```\s*$", "", t)
    t = re.sub(r"^\s*\*\*(.{3,60}?)\*\*\s*:?\s*$", r"## \1", t, flags=re.M)
    t = re.sub(r"^\s{0,3}#{1,6}\s*\d+[.)]\s*", "## ", t, flags=re.M)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "## ", t, flags=re.M)
    return t.strip()


REQUIRED_SECTION = r"^##\s*executive\s+summary"

def has_required_structure(text):
    return bool(re.search(REQUIRED_SECTION, text, re.M | re.I))


# ===================== v6.1: year = hard violation anywhere =====================
def validate_report_numbers_v61(report_text, allowed, small_int_max=15):
    """As v6, plus: a bare 4-digit year (19xx/20xx) is never a legitimate metric
    in this report, so it is a HARD violation regardless of section."""
    res = validate_report_numbers_v6(report_text, allowed, small_int_max)
    promoted = [n for n in res["soft"] if re.fullmatch(r"(19|20)\d{2}", n.replace(",", ""))]
    if promoted:
        res = {"hard": sorted(set(res["hard"]) | set(promoted)),
               "soft": [n for n in res["soft"] if n not in promoted]}
    return res


# ================== v6.1: model-independent example-URL injection ==================
def inject_segment_urls(text, url_map, label_map, focus_segments=None):
    """Guarantees every focus section carries a concrete example URL WITHOUT
    depending on the model emitting anything. Three stages:
      1. resolve ⟦PG:seg⟧ tokens (if the model cooperated),
      2. otherwise annotate the first plain-prose mention of the segment name
         (quoted / hyphen / underscore / case variants),
      3. append a 'Page Types Referenced' block for anything still missing.
    """
    out = text

    seen = set()
    def _tok(m):
        seg = m.group(1)
        lbl = label_map.get(seg, f"the '{seg}' page type")
        url = url_map.get(seg)
        if seg in seen or not url:      # annotate the first mention only
            return lbl
        seen.add(seg)
        return f"{lbl} (e.g., {url})"
    out = re.sub(r"\u27e6PG:([^\u27e7]+)\u27e7", _tok, out)

    annotated = {s for s, u in url_map.items() if u and u in out}
    # Stage 2 is restricted to the FOCUS sections and requires the mention to
    # look like a section reference (quoted, or next to section/page wording).
    # Without this a group named e.g. "event" would match the ordinary English
    # word in "campaign or event launch".
    ctx = r"(?:page\s+section|section|page|pages|area|group)"
    for seg in (focus_segments or []):
        url = url_map.get(seg)
        if not url or seg in annotated:
            continue
        variants = sorted({seg, seg.replace("-", " "), seg.replace("_", " ")},
                          key=len, reverse=True)
        name = "(?:" + "|".join(re.escape(v) for v in variants) + ")"
        candidates = [
            r"[\"'\u201c]" + name + r"[\"'\u201d]",            # quoted mention
            name + r"\s+" + ctx,                                # "unpacked section"
            ctx + r"\s+" + name,                                # "section unpacked"
        ]
        for pat in candidates:
            m = re.search(r"(?<![\w/-])" + pat + r"(?![\w/-])", out, re.IGNORECASE)
            if m:
                out = out[:m.end()] + f" (e.g., {url})" + out[m.end():]
                annotated.add(seg)
                break

    missing = [s for s in (focus_segments or [])
               if s not in annotated and url_map.get(s)]
    if missing:
        block = ["", "## Page Types Referenced"]
        block += [f"- {label_map.get(s, s)}: {url_map[s]}" for s in missing]
        out = out.rstrip() + "\n" + "\n".join(block)
    return out


def verify_urls_present(text, url_map, focus_segments):
    """Final assertion: which focus sections still lack their example URL."""
    return [s for s in focus_segments if url_map.get(s) and url_map[s] not in text]


# ================== v6.1: number-context binding (misassignment) ==================
# Metric buckets are derived from the FINDINGS KEY PATHS rather than a
# hand-written list of fields. Hand enumeration silently missed
# coverage.*_p75_delta and then localization.excluded_p75, each time rejecting
# our own pre-written sentence. Any future key whose name carries the metric
# keyword is now picked up automatically.
BINDING_KEY_RULES = {
    "p75":           ("p75", "ci95"),
    "share":         ("share",),
    "traffic":       ("share",),
    "cache":         ("cacherate",),
    "session-entry": ("landing",),
    "landing":       ("landing",),
    "mix effect":    ("mix_effect", "within_effect", "total_delta"),
    "composition":   ("mix_effect", "within_effect", "total_delta"),
}
# Every numeric under these top-level keys belongs to the metric named here,
# even when the individual field name does not repeat it (headline.delta_ms).
BINDING_SECTION_RULES = {"headline": "p75", "localization": "p75"}


def _walk_numeric(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_numeric(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_numeric(v, path)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        yield path, float(obj)


def build_metric_bindings(findings):
    """Map a metric keyword to the values legitimately associated with it.
    Catches what a plain whitelist cannot: a number that EXISTS in findings but
    is attached to the wrong metric (e.g. quoting the mean-based decomposition
    total as the p75 change)."""
    bind = {k: set() for k in BINDING_KEY_RULES}
    for path, value in _walk_numeric(findings):
        low = path.lower()
        top = low.split(".")[0]
        section_metric = BINDING_SECTION_RULES.get(top)
        if section_metric:
            bind[section_metric].add(round(value, 2))
        for metric, keywords in BINDING_KEY_RULES.items():
            if any(kw in low for kw in keywords):
                bind[metric].add(round(value, 2))
    return {k: v for k, v in bind.items() if v}


def check_number_binding(report_text, bindings, small_int_max=15, preapproved=()):
    """Flag a number only when it cannot belong to ANY metric mentioned in its
    sentence. A sentence often carries several metrics legitimately (share and
    p75, session-entry and cache rate), so judging every number against a single
    keyword produced false rejections. `preapproved` holds numbers taken from
    text we supplied ourselves, which are always acceptable."""
    pre = {round(float(v), 2) for v in preapproved if isinstance(v, (int, float))}
    issues = []
    for sent in re.split(r"(?<=[.!?])\s+|\n", report_text):
        low = sent.lower()
        if not low.strip():
            continue
        keys = [k for k in bindings if k in low]
        if not keys:
            continue
        allowed_vals = set().union(*(bindings[k] for k in keys)) | pre
        clean = re.sub(r"https?://\S+", "", sent)
        for m in re.finditer(NUMBER_RE, clean):
            try:
                v = float(m.group(0).replace(",", ""))
            except ValueError:
                continue
            if v <= small_int_max and v == int(v):
                continue
            if not any(abs(v - a) <= 0.51 for a in allowed_vals):
                issues.append(f"'{m.group(0)}' used with '{keys[0]}'")
    return sorted(set(issues))


def numbers_in_text(text):
    """Every numeric literal appearing in text we hand the model."""
    out = set()
    for m in re.finditer(NUMBER_RE, text or ""):
        try:
            out.add(round(float(m.group(0).replace(",", "")), 2))
        except ValueError:
            pass
    return out


def collect_supplied_text(findings):
    """Every string the prompt asks the model to reuse. self-check must cover
    ALL of it: v6.5 checked only narrative_facts, so the localization sentence
    that exists only in section_facts slipped through and deadlocked the loop."""
    supplied = {}
    for key, sentence in (findings.get("narrative_facts") or {}).items():
        supplied[f"narrative_facts[{key}]"] = sentence
    for section, sentences in (findings.get("section_facts") or {}).items():
        for i, sentence in enumerate(sentences):
            supplied[f"section_facts[{section}][{i}]"] = sentence
    for entry in (findings.get("remediation_playbook") or []):
        supplied[f"playbook[{entry.get('id')}]"] = (
            entry.get("action", "") + " " + " ".join(entry.get("levers", [])))
    for i, h in enumerate(findings.get("hypotheses") or []):
        supplied[f"hypotheses[{i}]"] = h
    return supplied


def self_check_supplied_text(findings_or_facts, bindings, allowed_numbers):
    """Guard rail learned the hard way three times (the '103' lever, the
    pre-written fact sentences, the localization sentence): ANY text we give the
    model must pass our own validators, or the model is punished for obeying us
    and the retry loop cannot converge. Run before the LLM loop and fix the
    pipeline, not the model."""
    supplied = (collect_supplied_text(findings_or_facts)
                if isinstance(findings_or_facts, dict)
                and ("narrative_facts" in findings_or_facts
                     or "section_facts" in findings_or_facts)
                else dict(findings_or_facts))
    bugs = []
    for key, sentence in supplied.items():
        if not isinstance(sentence, str):
            continue
        bad = check_number_binding(sentence, bindings)
        if bad:
            bugs.append(f"{key} fails number-binding: {bad}")
        degen = check_degenerate_comparison(sentence)
        if degen:
            bugs.append(f"{key} {degen}")
        unknown = [n for n in numbers_in_text(sentence)
                   if not any(abs(n - a) <= 0.51 for a in allowed_numbers)]
        if unknown:
            bugs.append(f"{key} has numbers outside the whitelist: {unknown}")
    return bugs

# ===================== v6.1: pre-rendered fact sentences =====================
def build_narrative_facts(findings):
    """Python writes the sentences for every load-bearing figure; the model is
    told to reuse them. This removes the need for the model to pick the right
    field out of a large JSON — the root cause of number misassignment."""
    facts = {}
    h = findings.get("headline", {})
    if h.get("transition_sentence"):
        facts["headline"] = h["transition_sentence"]

    # v6.8: decompose the SAME statistic the report leads with (p75). The
    # previous mean-based sentence quoted a different figure (56.1 ms) next to
    # the p75 headline (174 ms) and read like a contradiction.
    d = findings.get("decomposition") or {}
    if d.get("total_delta_ms") is not None:
        facts["decomposition"] = (
            f"Of the {fmt_ms(d['total_delta_ms'])} {d.get('stat','p75')} increase, "
            f"{fmt_ms(d.get('mix_effect_ms', 0))} comes from a shift in traffic "
            f"composition toward heavier page/device/traffic-type mixes and only "
            f"{fmt_ms(d.get('within_effect_ms', 0))} from segments genuinely slowing on "
            f"their own (holding that mix constant).")

    # v6.9.1 FIX: only assert a "new-visitor" audience shift when the
    # new_visitor_influx signal actually fired. Previously this sentence was
    # emitted unconditionally and hardcoded "more first-time visitors", so on
    # windows where session-entry share FELL (and the flag was False) the report
    # still claimed more first-time visitors — contradicting its own numbers.
    b = findings.get("behavior") or {}
    n, a = b.get("normal") or {}, b.get("anomaly") or {}
    if n.get("landing_share_pct") is not None and a.get("landing_share_pct") is not None:
        if b.get("new_visitor_influx"):
            cache_note = ""
            if n.get("client_cacherate_median") is not None:
                # cache hit rate is a percentage (upstream sends values <= 100)
                cache_note = (f", and the median browser cache hit rate from "
                              f"{fmt_pct(n['client_cacherate_median'])} to "
                              f"{fmt_pct(a.get('client_cacherate_median', 0))}")
            # v6.9.1: these audience figures are computed on the FOCUS section, not
            # the whole site — label the scope so the numbers are not mistaken for
            # sitewide values (e.g. the section cache median differs from sitewide).
            scope_seg = b.get("scope")
            scoped = bool(scope_seg and scope_seg != "overall")
            scope_prefix = f"Within {page_token(scope_seg)}, " if scoped else ""
            subject = "session-entry page views" if scope_prefix else "Session-entry page views"
            facts["audience"] = (
                f"{scope_prefix}{subject} moved from {fmt_pct(n['landing_share_pct'])} to "
                f"{fmt_pct(a['landing_share_pct'])}{cache_note}, an indicator of more "
                f"first-time visitors, not a cause of the slowdown.")
        elif not (findings.get("focus_breakdown")):
            # signal did not fire AND no sub-segment composition shift → state
            # plainly that audience mix is not a factor. (When focus_breakdown is
            # present, composition IS the story — see the focus_growth fact — so
            # this "not a factor" line would contradict it and is suppressed.)
            facts["audience_stable"] = (
                "Audience-mix signals (session-entry share and browser cache rate) were "
                "essentially unchanged between the windows, so audience composition is "
                "not a factor in this change.")

    # v6.9.2: name the audience/device sub-segments that drove the focus section's
    # rise (e.g. more paid-media, lower-memory, or India traffic on a heavy page),
    # so the report explains WHERE the growth came from instead of only that the
    # page group grew.
    fb = findings.get("focus_breakdown") or []
    foc = (findings.get("segments") or {}).get("focus") or {}
    if fb and foc.get("segment"):
        parts = ", ".join(
            f"{bi['human_label']} ({fmt_pct(bi['share_normal_pct'])} to "
            f"{fmt_pct(bi['share_anomaly_pct'])} of that page type)" for bi in fb)
        abbr = (findings.get("meta") or {}).get("metric_abbrev", "TBT")
        facts["focus_growth"] = (
            f"Within {page_token(foc['segment'])}, the growth is concentrated in {parts} — "
            f"all higher-{abbr} sub-segments, so the rise reflects a shift toward heavier "
            f"traffic on that page type rather than the page itself slowing down.")

    # v6.9.1 improvement: absolute-severity context. The delta can be small while
    # the baseline is already catastrophic (e.g. TBT p75 far past the 'poor' band).
    # Number-free so it never trips the numeric validators.
    meta = findings.get("meta") or {}
    hl = findings.get("headline") or {}
    metric_name = meta.get("metric")
    r_norm, r_anom = hl.get("rating_normal"), hl.get("rating_anomaly")
    if metric_name and r_anom == "poor":
        if r_norm == "poor":
            facts["severity_context"] = (
                f"Both windows already rate 'poor' for {metric_name}, so its level is a "
                f"chronic baseline issue well above the acceptable range — this window's "
                f"change sits on top of an already-poor baseline.")
        else:
            facts["severity_context"] = (
                f"{metric_name} crossed into the 'poor' range in the anomaly window, "
                f"pushing it past the acceptable threshold rather than staying healthy.")

    # v6.9.1 improvement: for main-thread metrics with a genuine self-regression
    # and clean delivery, point responsibility client-side (JS / third-party tags)
    # so the reader is not left looking at the network. Number-free.
    prof = meta.get("profile")
    within = (findings.get("within_regression") or {}).get("within_regression")
    deliv = (findings.get("delivery") or {}).get("verdict")
    if prof == "tbt" and within and deliv == "clean":
        facts["client_side"] = (
            "With CDN and origin delivery clean, the added blocking time originates "
            "client-side — main-thread JavaScript and third-party tags on the affected "
            "section — rather than in network delivery.")

    for r in (findings.get("segments", {}).get("focus_list") or []):
        # v6.9.4: base the role on the composition-controlled split, not the raw
        # p75 delta. `genuine_regression` is True only when the section genuinely
        # slowed holding its OWN internal traffic mix constant — so a rise driven
        # by an internal shift (e.g. more India / paid traffic inside it) reads as
        # a mix effect, matching the sub-segment breakdown that follows.
        gained, genuine = r.get("gained_share"), r.get("genuine_regression")
        if genuine and gained:
            role = (" — it grew its share AND, holding its own internal traffic mix "
                    "constant, still slowed genuinely; investigate both.")
        elif genuine:
            role = (" — its share fell yet, holding its own internal mix constant, it "
                    "genuinely slowed — a real local regression to investigate directly.")
        elif gained:
            role = (" — its p75 rose mainly because it drew more of its own heavier "
                    "traffic (a shift in its internal country/device/traffic mix), not "
                    "because the page itself slowed down.")
        else:
            role = (" — holding its own internal mix constant it did not genuinely slow "
                    "down; the p75 move reflects a shift in its internal traffic mix.")
        facts[f"section::{r['segment']}"] = (
            f"{page_token(r['segment'])} moved from {fmt_pct(r['share_normal_pct'])} to "
            f"{fmt_pct(r['share_anomaly_pct'])} of traffic, with its own p75 going from "
            f"{fmt_ms(r['p75_normal'])} to {fmt_ms(r['p75_anomaly'])}{role}")

    c = findings.get("coverage") or {}
    if c.get("coverage_ratio") is not None:
        # v6.7: "account for 178.0ms of the 174.0ms change" read like a typo.
        # State it via the residual, which is what the reader actually needs.
        residual = c.get("residual_p75_delta")
        if c.get("coverage_ratio", 0) >= 0.95 and residual is not None:
            facts["coverage"] = (
                f"Removing those sections leaves the rest of the site essentially "
                f"flat (residual p75 change of {fmt_ms(residual)}), so they account "
                f"for the whole p75 shift.")
        else:
            facts["coverage"] = (
                f"Those sections explain {fmt_ms(c['explained_p75_delta'])} of the "
                f"{fmt_ms(c['overall_p75_delta'])} p75 change, leaving "
                f"{fmt_ms(residual)} unaccounted for.")

    # v6.7: concrete resolution criteria instead of generic monitoring advice
    fl = findings.get("segments", {}).get("focus_list") or []
    if fl:
        r0 = fl[0]
        facts["monitoring"] = (
            f"The alert should clear as {page_token(r0['segment'])} returns toward its "
            f"baseline share of {fmt_pct(r0['share_normal_pct'])} and its p75 back "
            f"toward {fmt_ms(r0['p75_normal'])}; if the share normalizes but the p75 "
            f"stays near {fmt_ms(r0['p75_anomaly'])}, treat it as a genuine regression.")

    # v6.7: scope/impact line for the executive summary
    sev = (findings.get("headline") or {}).get("severity")
    if sev and fl:
        n_sec = len(fl)
        scope = ("one page type" if n_sec == 1 else f"{n_sec} page types")
        facts["impact"] = (
            f"Severity is {sev}: the change is statistically significant but confined "
            f"to {scope}, so the rest of the site is unaffected.")
    return facts


def check_degenerate_comparison(report_text, tol=0.01):
    """Catch 'X vs X' / 'from X to X' style claims where both sides are the same
    value — a comparison that carries no information and almost always means the
    model pulled the same field twice (e.g. '79.8% vs 79.8%')."""
    issues = []
    pats = [r"(\d[\d,]*(?:\.\d+)?)\s*%?\s*(?:vs\.?|versus|compared to)\s*(\d[\d,]*(?:\.\d+)?)\s*%?",
            r"from\s+(\d[\d,]*(?:\.\d+)?)\s*%?\s*(?:ms)?\s*to\s+(\d[\d,]*(?:\.\d+)?)\s*%?"]
    for p in pats:
        for m in re.finditer(p, report_text, re.I):
            try:
                a = float(m.group(1).replace(",", "")); b = float(m.group(2).replace(",", ""))
            except ValueError:
                continue
            if abs(a - b) <= tol:
                issues.append(f"degenerate comparison '{m.group(0).strip()}'")
    return sorted(set(issues))


# ====================== v6.3: section completeness ======================
# The v6.2 run passed every gate yet shipped a report missing four of five
# mandated sections: the model turned the narrative_facts keys into headings
# and dropped What Changed / What Did Not Change / Recommended Actions /
# Monitoring Notes — i.e. all the playbook value. Structure is now validated.

REQUIRED_SECTIONS = ["Executive Summary", "What Changed", "What Did Not Change",
                     "Recommended Actions", "Monitoring Notes"]
OPTIONAL_SECTIONS = ["Hypotheses", "Page Types Referenced"]


def report_headings(text):
    return [h.strip() for h in re.findall(r"^##\s*(.+?)\s*:?\s*$", text, re.M)]


def check_section_completeness(text, required=None, expected_optional=None):
    """Verify the mandated sections are all present and flag headings invented
    outside the spec (the narrative_facts-as-headings failure mode)."""
    required = required or REQUIRED_SECTIONS
    expected_optional = expected_optional or OPTIONAL_SECTIONS
    found = [h.lower() for h in report_headings(text)]
    missing = [s for s in required if not any(s.lower() in h for h in found)]
    known = [s.lower() for s in list(required) + list(expected_optional)]
    unexpected = [h for h in report_headings(text)
                  if not any(k in h.lower() for k in known)]
    return {"missing": missing, "unexpected": unexpected,
            "complete": not missing}


def splice_missing_sections(draft, reference, missing):
    """Last-resort repair: copy the missing sections verbatim from the
    deterministic reference report so an incomplete draft never ships."""
    if not missing:
        return draft
    ref_blocks = {}
    parts = re.split(r"^##\s*(.+)$", reference, flags=re.M)
    for i in range(1, len(parts) - 1, 2):
        ref_blocks[parts[i].strip().lower()] = parts[i + 1].rstrip()
    out = draft.rstrip()
    for sec in missing:
        body = ref_blocks.get(sec.lower())
        if body:
            out += f"\n\n## {sec}\n{body.strip()}"
    return out


# ============== v6.3: facts mapped to the section that consumes them ==============
def build_section_facts(findings):
    """Return sentences grouped BY THE SECTION THEY BELONG TO. v6.2 handed the
    model a flat dict whose keys looked like a table of contents, which invited
    it to emit those keys as headings. Grouping makes the intended placement
    explicit: these are sentences to use *inside* the listed sections."""
    flat = build_narrative_facts(findings)
    sec = {"Executive Summary": [], "What Changed": [], "What Did Not Change": [],
           "Monitoring Notes": []}
    if "headline" in flat:
        sec["Executive Summary"].append(flat["headline"])
    # v6.8: once the summary was fed explicit sentences the model stopped adding
    # the verdict narrative, leaving a summary that stated the number but not the
    # reason. Supply the verdict explicitly.
    verdict_sentence = (findings.get("verdict") or {}).get("sentence")
    if verdict_sentence:
        sec["Executive Summary"].append(verdict_sentence)
    if "severity_context" in flat:
        sec["Executive Summary"].append(flat["severity_context"])
    if "impact" in flat:
        sec["Executive Summary"].append(flat["impact"])
    for key, sentence in flat.items():
        if key.startswith("section::"):
            sec["What Changed"].append(sentence)
    for key in ("focus_growth", "decomposition", "audience", "client_side", "coverage"):
        if key in flat:
            sec["What Changed"].append(flat[key])

    if "monitoring" in flat:
        sec["Monitoring Notes"] = [flat["monitoring"]]

    if "audience_stable" in flat:
        sec["What Did Not Change"].append(flat["audience_stable"])
    delivery = findings.get("delivery") or {}
    has_cdn = "cdncacherate" in (delivery.get("metrics") or {})
    # v6.9.1: the CDN cache hit rate is beacon-derived, not the CDN's own figure —
    # add a reference-only caveat wherever it informed the delivery assessment.
    cdn_caveat = ("The CDN cache hit rate here is derived from client beacons and may "
                  "differ from the actual cache hit rate reported by the CDN; use it for "
                  "reference only.")
    if delivery.get("verdict") == "clean":
        sec["What Did Not Change"].append(
            "CDN and origin delivery metrics show no regression in the anomaly window.")
        if has_cdn:
            sec["What Did Not Change"].append(cdn_caveat)
    elif has_cdn and "cdncacherate" in (delivery.get("issues") or []):
        sec.setdefault("What Changed", []).append(cdn_caveat)
    loc = findings.get("localization") or {}
    if loc.get("localized") and loc.get("excluded_p75"):
        e = loc["excluded_p75"]
        direction = "improved slightly" if e[1] < e[0] else "held steady"
        sec["What Did Not Change"].append(
            f"Excluding the focus section, sitewide p75 {direction}, moving from "
            f"{fmt_ms(e[0])} to {fmt_ms(e[1])}.")
    return {k: v for k, v in sec.items() if v}


# ====================== v6.7: customer-facing number formatting ======================
# Raw findings values read as machine output ("2806.0ms", "22.39%"). These
# helpers keep the VALUE identical (so the validators still match within their
# 0.51 tolerance) while presenting it the way a reader expects.
def fmt_ms(v):
    v = float(v)
    return f"{v:,.0f} ms" if abs(v - round(v)) < 0.05 else f"{v:,.1f} ms"

def fmt_pct(v):
    v = float(v)
    return f"{v:.0f}%" if abs(v - round(v)) < 0.05 else f"{v:.1f}%"

def fmt_num(v):
    v = float(v)
    return f"{v:,.0f}" if abs(v - round(v)) < 0.05 else f"{v:,.1f}"



# ========================================================================
# Cell 4b — LLM-enhancement layer: Akamai playbook, hypotheses, critic, scope guard (v6.9)
"""v5 LLM-enhancement layer.

Adds, on top of the deterministic findings pipeline:
  1. Playbook-grounded recommendations — Akamai-lever remediation actions
     selected by matching findings signals to a structured playbook, so the
     LLM specifies concrete, in-scope actions instead of generic advice.
  2. Critic pass — a second LLM call that audits the draft against the
     findings (numbers, symptom/cause discipline, verdict alignment,
     out-of-scope recommendations) and returns a structured verdict.
  3. Hypothesis layer — findings-consistent external explanations, always
     confined to a clearly-labeled "Hypotheses (to verify)" section and
     never mixed with established facts.

Everything is generic: the playbook is data, not code, and matching is by
signal predicates evaluated against the findings dict.
"""
import json
import re


# ============================================================= PLAYBOOK
# Akamai-lever remediation playbook (draft). Each entry:
#   id, when: list of signal predicates (ALL must hold), levers, action (LLM
#   rewrites into customer prose), scope_tag (used by the scope guard).
# Signals are simple dotted-path + operator checks against findings.
AKAMAI_PLAYBOOK = [
    {
        "id": "ivm_hero_cold_cache",
        "applies_to": ["lcp", "fcp"],          # visual paint metrics with a hero element
        "when": ["verdict in traffic_mix_shift,mix_shift_with_local_regression,"
                 "multi_segment_mix_shift,multi_segment_regression",
                 "behavior.new_visitor_influx == true"],
        "levers": ["Image & Video Manager (adaptive/right-sized hero media)",
                   "EdgeWorkers (LCP element preload hint injection)"],
        "action": ("Right-size and preload the growing section's LCP element "
                   "(hero image/video) for cold-cache first-time visitors, and "
                   "serve adaptive quality on cellular connections."),
        "scope_tag": "edge_media",
    },
    {
        "id": "prefetch_event_landing",
        "applies_to": ["lcp", "fcp", "ttfb"],  # paint + server-wait
        "when": ["behavior.new_visitor_influx == true",
                 "behavior.anomaly.landing_share_pct > 70"],
        "levers": ["EdgeWorkers (Early Hints)",
                   "Adaptive Acceleration (automatic push/preload)"],
        "action": ("Enable early-hints/preload for the critical render-path "
                   "resources on the campaign landing pages so first paint is "
                   "not blocked for direct-entry visitors."),
        "scope_tag": "edge_hints",
    },
    {
        "id": "offload_regional_origin",
        "applies_to": ["lcp", "fcp", "ttfb"],
        "when": ["delivery.verdict == clean",
                 "within_regression.within_regression == true"],
        "levers": ["Tiered Distribution / cache key review",
                   "Cloud Wrapper (origin offload for cold regions)"],
        "action": ("Confirm the growing region is served from a nearby edge "
                   "tier and review the cache key so first-time visitors in "
                   "that region still benefit from a warm shared cache."),
        "scope_tag": "edge_cache",
    },
    {
        "id": "delivery_investigate",
        "applies_to": ["lcp", "fcp", "ttfb", "tbt"],
        "when": ["delivery.verdict == degraded"],
        "levers": ["mPulse + DataStream 2 correlation",
                   "Origin health / offload review"],
        "action": ("Investigate the delivery-path regression: correlate the "
                   "affected window in DataStream 2 with origin response times "
                   "and cache offload before any content change."),
        "scope_tag": "delivery_ops",
    },
    {
        "id": "third_party_release_audit",
        "applies_to": ["lcp", "fcp", "ttfb", "tbt"],
        "when": ["within_regression.within_regression == true"],
        "levers": ["Script Management / third-party tag review",
                   "release-change correlation"],
        "action": ("Verify no recent release or third-party tag change landed "
                   "on the affected page type in the anomaly window; compare the "
                   "resource waterfall between the two windows."),
        "scope_tag": "app_change",
    },
    {
        "id": "reduce_main_thread_blocking",
        "applies_to": ["tbt"],
        "when": ["verdict in traffic_mix_shift,mix_shift_with_local_regression,"
                 "multi_segment_mix_shift,multi_segment_regression,segment_regression"],
        "levers": ["Script Management (defer/async non-critical JS)",
                   "EdgeWorkers (offload work from the client)",
                   "third-party tag audit (long tasks)"],
        "action": ("Reduce main-thread blocking on the affected page type: defer or "
                   "split long-running scripts, remove or delay non-critical "
                   "third-party tags, and break up long tasks so the page stays "
                   "responsive."),
        "scope_tag": "app_change",
    },
]

VALID_SCOPE_TAGS = {p["scope_tag"] for p in AKAMAI_PLAYBOOK}


def _get_path(d, path):
    cur = d
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _eval_predicate(findings, pred):
    """Evaluate one predicate string against findings. Supports:
       'a.b == x', 'a.b > n', 'a.b < n', 'a.b in x,y,z'."""
    m = re.match(r"^(.+?)\s+(==|>|<|in)\s+(.+)$", pred.strip())
    if not m:
        return False
    path, op, rhs = m.group(1), m.group(2), m.group(3).strip()
    val = _get_path(findings, path.strip())
    if op == "in":
        opts = [x.strip() for x in rhs.split(",")]
        return str(val) in opts
    if op == "==":
        if rhs in ("true", "false"):
            return bool(val) == (rhs == "true")
        return str(val) == rhs
    try:
        num = float(rhs); val = float(val)
    except (TypeError, ValueError):
        return False
    return val > num if op == ">" else val < num


def match_playbook(findings, playbook=AKAMAI_PLAYBOOK, metric_key=None):
    """Return remediation entries whose predicates hold AND that apply to the
    active metric. metric_key comes from the metric profile (lcp/fcp/tbt/ttfb);
    when None, applicability is not filtered (back-compat)."""
    ctx = dict(findings)
    ctx["verdict"] = findings.get("verdict", {}).get("code")
    selected = []
    for entry in playbook:
        applies = entry.get("applies_to")
        if metric_key and applies and metric_key not in applies:
            continue                      # lever does not suit this metric
        if all(_eval_predicate(ctx, p) for p in entry["when"]):
            selected.append({"id": entry["id"], "levers": entry["levers"],
                             "action": entry["action"],
                             "scope_tag": entry["scope_tag"]})
    return selected


# ============================================================= HYPOTHESES
# ISO-ish region codes seen in beacon data -> customer-readable names. Unknown
# codes fall through unchanged, so this never blocks a new market appearing.
REGION_NAMES = {
    "in": "India", "tr": "Turkey", "za": "South Africa", "sec": "Korea",
    "th": "Thailand", "jp": "Japan", "us": "the United States",
    "uk": "the United Kingdom", "de": "Germany", "fr": "France",
    "br": "Brazil", "mx": "Mexico", "latin": "Latin America",
    "ph": "the Philippines", "vn": "Vietnam", "id": "Indonesia",
    "my": "Malaysia", "sg": "Singapore", "ae": "the UAE", "sa": "Saudi Arabia",
    "eg": "Egypt", "pl": "Poland", "it": "Italy", "es": "Spain",
    "nl": "the Netherlands", "au": "Australia", "ca": "Canada",
    "cn": "China", "hk": "Hong Kong", "tw": "Taiwan", "ru": "Russia",
    "levant": "the Levant", "africa_en": "English-speaking Africa",
}

def humanize_region(code):
    c = str(code).strip().lower()
    return REGION_NAMES.get(c, str(code))


def derive_hypotheses(findings):
    """Findings-consistent external explanations. Deterministic seeds the LLM
    may phrase; each is explicitly a hypothesis, never asserted as fact."""
    hyps = []
    beh = findings.get("behavior", {})
    if beh.get("new_visitor_influx"):
        where = ""
        dd = findings.get("segments", {}).get("drilldown", {}).get("country")
        if dd:
            gainers = [r for r in dd if r.get("share_delta_pp", 0) > 1]
            if gainers:
                names = [humanize_region(r["segment"]) for r in gainers[:2]]
                where = " concentrated in " + " and ".join(names)
        hyps.append("A marketing campaign or product-announcement event drove "
                    "a burst of first-time visitors" + where + ", which arrive "
                    "with empty browser caches and therefore slower first loads.")
    if findings.get("within_regression", {}).get("within_regression"):
        hyps.append("A content or third-party change may have shipped to the "
                    "affected section shortly before the window, adding to its "
                    "load time independently of the traffic shift.")
    cov = findings.get("coverage")
    if cov and not cov.get("sufficient"):
        hyps.append("Part of the change originates outside the identified "
                    "sections; a broader release or a page group beyond the "
                    "tracked set may contribute.")
    return hyps


# ============================================================= CRITIC
def build_critic_prompt(report_text, findings_text):
    return f"""You are a strict reviewer auditing a performance report draft
against its source findings. Return ONLY a JSON object, no prose.

Check for these problems:
- "invented_numbers": any number in the report not present in FINDINGS.
- "symptom_as_cause": any associated symptom (e.g. cache hit rate, bytes,
  session-entry share) described as a driver/cause/root cause.
- "verdict_conflict": any statement contradicting findings.verdict.sentence.
- "unsupported_causal_claim": a causal claim not backed by findings.
- "missing_focus_section": a page type in findings.segments.focus_list not
  mentioned in the report.
- "missing_required_section": the report omits any of these headings —
  Executive Summary, What Changed, What Did Not Change, Recommended Actions,
  Monitoring Notes (and Hypotheses when findings.hypotheses is non-empty).
- "invented_section": a heading that is not part of that required set, such as
  a heading copied from a findings key rather than the report spec.
- "missing_recommendations": findings.remediation_playbook is non-empty but the
  report contains no corresponding actions.

Return exactly:
{{"pass": true|false,
  "issues": [{{"type": "<one of the above>", "detail": "<short quote or note>"}}],
  "severity": "none|minor|major"}}

FINDINGS:
{findings_text}

REPORT DRAFT:
{report_text}
"""


def parse_critic_response(text):
    """Robustly extract the critic JSON."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"pass": True, "issues": [], "severity": "none",
                "_parse_error": True}
    try:
        obj = json.loads(m.group(0))
        obj.setdefault("pass", not obj.get("issues"))
        obj.setdefault("issues", [])
        obj.setdefault("severity", "none")
        return obj
    except json.JSONDecodeError:
        return {"pass": True, "issues": [], "severity": "none",
                "_parse_error": True}


# ============================================================= SCOPE GUARD
def check_recommendation_scope(report_text, allowed_playbook_actions):
    """Deterministic guard: the Recommended Actions section should not stray
    into levers outside the matched playbook. We check that the section does
    not introduce recommendation verbs about systems we never selected.
    Returns a list of out-of-scope hints (advisory)."""
    # extract the actions section
    m = re.search(r"##\s*Recommended Actions(.*?)(?:\n##|\Z)", report_text,
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    section = m.group(1).lower()
    # systems that must only appear if a matching playbook entry was selected
    guarded = {
        "waf": "security", "rate limit": "security", "captcha": "security",
        "database": "backend", "sql": "backend",
        "autoscal": "backend", "kubernetes": "backend",
    }
    offenders = []
    for term, area in guarded.items():
        if term in section:
            offenders.append(f"out-of-scope ({area}): '{term}'")
    return offenders



# ========================================================================
# Cell 4c — v5 fallback extension: playbook-grounded actions + hypotheses in the deterministic report
def render_fallback_report_v5(f):
    base = render_fallback_report(f)  # emits tokens; resolved by caller
    # replace the generic Recommended Actions block with playbook actions when present
    pb = f.get("remediation_playbook") or []
    if pb:
        lines = base.split("\n")
        out, i = [], 0
        while i < len(lines):
            if lines[i].strip().lower().startswith("## recommended actions"):
                out.append("## Recommended Actions")
                for e in pb:
                    levers = "; ".join(e["levers"])
                    out.append(f"- {e['action']} (Akamai: {levers})")
                # skip original action bullets until next section
                i += 1
                while i < len(lines) and not lines[i].startswith("## "):
                    i += 1
                continue
            out.append(lines[i]); i += 1
        base = "\n".join(out)
    # append hypotheses section
    hyps = f.get("hypotheses") or []
    if hyps:
        base += "\n\n## Hypotheses (to verify)\n" + "\n".join(f"- {h}" for h in hyps)
    return base



# ========================================================================  (from Cell 12: LLM prompt builder)
def build_llm_prompt_v63(findings_text, section_facts, want_hypotheses):
    """v6.3: the pre-written-facts guidance sits in the MIDDLE and the section
    spec goes LAST. In v6.2 the facts instruction was the final thing the model
    read, and a 7B model let it override the structure spec — it emitted the
    fact keys as headings and dropped four required sections."""
    base = build_llm_prompt(findings_text)
    base = base.replace(
        "## Recommended Actions\n(3-4 practical actions matched to the verdict code.)",
        "## Recommended Actions\n(Use ONLY the actions in findings.remediation_playbook. "
        "Phrase each as a concrete step and name its Akamai lever(s). Do NOT invent "
        "actions or mention systems not in the playbook.)")

    facts_block = ["", "PRE-WRITTEN SENTENCES — already grouped by the section that should",
                   "contain them. Use each sentence inside its listed section, verbatim or",
                   "with minimal rewording. These labels are NOT headings: never create a",
                   "section named after them, and never output the JSON keys themselves.",
                   "Do not re-derive figures from elsewhere in the JSON."]
    for sec, sentences in section_facts.items():
        facts_block.append(f"  [use inside '## {sec}']")
        for s_ in sentences:
            facts_block.append(f"    - {s_}")
    base += "\n" + "\n".join(facts_block)

    required = list(REQUIRED_SECTIONS)
    if want_hypotheses:
        required.insert(4, "Hypotheses (to verify)")
    base += ("\n\nOUTPUT CONTRACT (this overrides anything above if they conflict):\n"
             "Return plain Markdown with EXACTLY these '##' headings, in this order, and no "
             "others:\n" + "\n".join(f"  ## {s_}" for s_ in required) + "\n"
             "Every one of these sections must be present and non-empty. Do not add extra "
             "sections. No code fences, no reasoning traces.")
    return base



def run_v6(csv_path, *, sec_dir, processed_dir=None, metadata_path=None,
           timer_metric=None, subject_prefix="[v6] "):
    """Run the v6 pipeline on one CSV and return report artifacts.

    Returns a dict: report_md, report_source, email_subject, email_plain,
    email_html, severity, verdict_code, findings, metric_name.
    """
    global _CLIENT
    sec_dir = Path(sec_dir)
    _CLIENT = GatewayClient(GatewayConfig.load(sec_dir / "inference-gateway"))
    LLM_MODEL = _CLIENT.cfg.model
    if processed_dir is not None:
        processed_dir = Path(processed_dir)
    if metadata_path is None and processed_dir is not None:
        metadata_path = processed_dir / (Path(csv_path).stem + ".meta.json")


    import re
    # Cell 5 — Data load + sample-integrity gate
    import json, warnings
    import pandas as pd
    warnings.filterwarnings("ignore")

    csv_path = Path(csv_path)
    # Derive the metric (timer) name from the filename, e.g.
    #   sample_data_7-23_1118_firstcontentfulpaint_iter5_win1_inter5.csv -> firstcontentfulpaint
    # Falls back to the TIMER_METRIC env override, then to a safe default.
    _KNOWN_TIMERS = ["largestcontentfulpaint", "firstcontentfulpaint",
                     "firstinputdelay", "cumulativelayoutshift", "timetofirstbyte",
                     "interactiontonextpaint", "firstpaint", "domcontentloaded",
                     "pageloadtime", "loadeventend"]
    def _derive_metric_name(filename):
        stem = Path(filename).stem.lower()
        for t in _KNOWN_TIMERS:                       # exact known metric wins
            if t in stem:
                return t
        # otherwise take the token between the timestamp and the run params
        m = re.search(r"_(\d{3,4})_([a-z][a-z0-9]+?)_(?:iter|win|inter)", stem)
        if m:
            return m.group(2)
        return os.getenv("TIMER_METRIC", "timer")

    METRIC_NAME = timer_metric or _derive_metric_name(csv_path.name)
    METRIC_PROFILE = resolve_metric_profile(METRIC_NAME)
    # per-metric constants override the LCP-era defaults from Cell 1
    ARTIFACT_MS       = int(os.getenv("ARTIFACT_MS", METRIC_PROFILE["artifact_ms"]))
    SEVERITY_FLOOR_MS = int(os.getenv("SEVERITY_FLOOR_MS", METRIC_PROFILE["severity_floor_ms"]))
    EFFECT_FLOOR_MS   = int(os.getenv("EFFECT_FLOOR_MS", METRIC_PROFILE["effect_floor_ms"]))
    print(f"metric: {METRIC_NAME} -> profile '{METRIC_PROFILE['profile_key']}' "
          f"({METRIC_PROFILE['abbrev']}); good<{METRIC_PROFILE['good_ms']} "
          f"poor>{METRIC_PROFILE['poor_ms']} artifact>{ARTIFACT_MS} "
          f"severity_floor={SEVERITY_FLOOR_MS}ms hero_element={METRIC_PROFILE['hero_element']}")

    df = pd.read_csv(csv_path)
    print(f"loaded {len(df):,} rows from {csv_path}")

    # ---- ground truth: prefer upstream sidecar metadata over the (possibly sampled) CSV
    source_meta = None
    if metadata_path and Path(metadata_path).is_file():
        source_meta = json.loads(Path(metadata_path).read_text())
        print("sidecar metadata found — report numbers will use SOURCE stats")
        for lab, key in [(0, "normal"), (1, "anomaly")]:
            sp = float(df.loc[df[LABEL_COL]==lab, TIMER_COL].quantile(.75))
            gt = float(source_meta["windows"][key]["p75"])
            drift = abs(sp - gt) / gt * 100
            print(f"  {key}: sample p75={sp:.1f} vs source p75={gt:.1f} (drift {drift:.2f}%)")
            if drift > SAMPLE_DRIFT_TOL:
                raise RuntimeError(
                    f"Sample does not represent source data for '{key}' window "
                    f"({drift:.1f}% p75 drift > {SAMPLE_DRIFT_TOL}%). "
                    "Fix upstream sampling (use stratified label x timer-decile sampling) before reporting.")
    else:
        print("[warn] no sidecar metadata — falling back to CSV-computed stats. "
              "Upstream should emit window counts + percentiles at export time.")

    # ---- derived, dataset-agnostic features
    df["page_group"] = derive_page_group(df["url"]) if "url" in df else "all"
    df["referrer_present"] = df["referrer"].notna() if "referrer" in df else False
    df["mem_bucket"] = df["deviceMemory"].map(mem_bucket) if "deviceMemory" in df else "na"

    # ---- representative URL per page group, so the report can cite a concrete
    #      example page instead of only an abstract group name.
    def representative_urls(frame, group_col="page_group", url_col="url",
                            skip=("other", "unknown", "all")):
        """Most frequent concrete URL (query string stripped) per page group."""
        mapping = {}
        if url_col not in frame:
            return mapping
        for grp, sub in frame.groupby(group_col):
            if str(grp) in skip:
                continue
            urls = sub[url_col].dropna().astype(str).map(lambda u: u.split("?")[0])
            if urls.empty:
                continue
            mapping[str(grp)] = urls.value_counts().index[0]
        return mapping

    PAGE_GROUP_URLS = representative_urls(df)
    print(f"representative URLs mapped for {len(PAGE_GROUP_URLS)} page groups")


    # Cell 6 — Step A: window statistics + severity gate
    win = compute_window_stats(df, TIMER_COL, LABEL_COL)
    if source_meta:   # override headline percentiles with source ground truth
        for key in ("normal", "anomaly"):
            win[key].update({k: source_meta["windows"][key][k]
                             for k in ("count","mean","p50","p75","p90","p95","p99")
                             if k in source_meta["windows"][key]})
        win["delta_p75_ms"]  = round(win["anomaly"]["p75"] - win["normal"]["p75"], 2)
        win["delta_p75_pct"] = round(win["delta_p75_ms"] / win["normal"]["p75"] * 100, 2)

    severity, severity_msg = classify_severity(win, abs_floor_ms=SEVERITY_FLOOR_MS)
    print(f"p75: {win['normal']['p75']} -> {win['anomaly']['p75']} "
          f"({win['delta_p75_ms']:+}ms, {win['delta_p75_pct']:+}%)  "
          f"CI95={win['delta_p75_ci95']}  MW-p={win['mannwhitney_p']:.2e}")
    print(f"severity: {severity} — {severity_msg}")


    # Cell 7 — Step B: artifact/outlier audit
    outliers = audit_outliers(df, TIMER_COL, LABEL_COL, ARTIFACT_MS)
    for k, v in outliers["windows"].items():
        print(f"{k:>7}: {v['artifact_count']} beacons >{ARTIFACT_MS/1000:.0f}s "
              f"({v['artifact_share_pct']}%), mean inflated by {v['mean_inflation_ms']}ms, "
              f"max={v['max_timer_ms']:,}ms")


    # Cell 8 — Step C: decomposition, mover discovery, MULTI-focus selection (v4)
    decomp={}
    for d in [PRIMARY_DIM, *SECONDARY_DIMS[:2], [PRIMARY_DIM, SECONDARY_DIMS[0]]]:
        r=mix_within_decomposition(df, d, TIMER_COL, LABEL_COL)
        decomp[r["dim"]]=r
        print(f"[{r['dim']:<22}] Δmean={r['total_delta_ms']:+}ms = "
              f"mix {r['mix_effect_ms']:+} + within {r['within_effect_ms']:+}")

    # v6.8: decompose the SAME statistic the report leads with. Percentiles are not
    # additive, so this uses a reweighted counterfactual (DFL) rather than the
    # mean-based Oaxaca split, which quoted a different figure from the headline.
    # v6.9.2: decompose on an INTERACTED cell key (page_group x device-memory x
    # paid-media) so a composition shift toward heavier sub-segments (e.g. more
    # paid / low-memory traffic within a page group) is counted as MIX, not
    # misattributed to a genuine per-section slowdown (WITHIN).
    _decomp_key = [PRIMARY_DIM] + [c for c in ("mem_bucket", "paidmedia") if c in df]
    p75_decomp = quantile_decomposition(df, _decomp_key, q=0.75, timer_col=TIMER_COL,
                                        label_col=LABEL_COL)
    materiality = effect_materiality(p75_decomp, abs_floor_ms=EFFECT_FLOOR_MS)
    print(f"[p75 decomposition] Δ{p75_decomp['total_delta_ms']}ms = "
          f"mix {p75_decomp['mix_effect_ms']:+} + within {p75_decomp['within_effect_ms']:+} "
          f"(common support {p75_decomp['common_support_pct']}%) | "
          f"material: mix={materiality['mix_material']}, within={materiality['within_material']}")

    primary_movers=top_movers(df, PRIMARY_DIM, TIMER_COL, LABEL_COL, min_n=MIN_SEG_N)

    # v4: select ALL qualifying problem sections, ranked by contribution
    focus_selection=select_focus_segments(primary_movers, win, PRIMARY_DIM,
                                          min_share_pp=MIN_SHARE_PP, min_p75_delta=SEVERITY_FLOOR_MS,
                                          max_focus=MAX_FOCUS)
    focus_list=focus_selection["focus_list"]
    focus=focus_list[0] if focus_list else None          # primary, for localization/behavior drill
    focus_segments=[r["segment"] for r in focus_list]

    # coverage: do the chosen sections actually explain the sitewide p75 rise?
    coverage=coverage_check(df, PRIMARY_DIM, focus_segments, TIMER_COL, LABEL_COL) if focus_segments else None
    other_watch=other_bucket_watch(primary_movers)

    localization, drilldown = None, {}
    focus_composition = []
    focus_df=df
    if focus:
        localization=localization_check(df, PRIMARY_DIM, focus["segment"], TIMER_COL, LABEL_COL)
        focus_df=df[df[PRIMARY_DIM].astype(str)==focus["segment"]]
        for d in SECONDARY_DIMS:
            drilldown[d]=top_movers(focus_df, d, TIMER_COL, LABEL_COL, min_n=max(100, MIN_SEG_N//2))
        # v6.9.2: which audience/device sub-segments drove the focus section's rise
        focus_composition=focus_breakdown(
            focus_df, ["paidmedia", "mem_bucket", "country", "connectiontype", "deviceType"],
            float(focus["p75_normal"]), TIMER_COL, LABEL_COL, min_n=max(100, MIN_SEG_N//2))
        # v6.9.3: align the focus section's example URL with the growth story. If
        # the breakdown pins the growth to a country, cite a URL from THAT country
        # in the anomaly window rather than the overall most-frequent one (which
        # can be a different region than the one the report calls out).
        _top_country = next((b["segment"] for b in focus_composition if b["dim"] == "country"), None)
        if _top_country and "url" in focus_df and "country" in focus_df:
            _sub = focus_df[(focus_df[LABEL_COL] == 1)
                            & (focus_df["country"].astype(str) == _top_country)]
            _urls = _sub["url"].dropna().astype(str).map(lambda u: u.split("?")[0])
            if len(_urls):
                PAGE_GROUP_URLS[focus["segment"]] = _urls.value_counts().index[0]

    # v6.9.4: for EACH focus page type, split its own p75 rise into an internal
    # mix shift vs a genuine same-audience slowdown, so the role label reflects
    # what really happened (a section whose rise is mostly an internal India /
    # device / paid shift is NOT called 'the page slowed on its own').
    for _r in focus_list:
        _seg_df = df[df[PRIMARY_DIM].astype(str) == _r["segment"]]
        _split = focus_regression_split(_seg_df, TIMER_COL, LABEL_COL, abs_floor_ms=EFFECT_FLOOR_MS)
        _r["genuine_regression"] = _split["genuine_regression"]
        _r["subcomp_mix_ms"] = _split["mix_effect_ms"]
        _r["genuine_within_ms"] = _split["within_effect_ms"]

    print(f"\nfocus sections ({len(focus_list)}):")
    for r in focus_list:
        role=("share+regression" if r["gained_share"] and r["self_regressed"]
              else "share-gain" if r["gained_share"] else "self-regression")
        print(f"   {r['segment']:<22} score={r['contribution_score']:<7} [{role}] "
              f"share {r['share_normal_pct']}->{r['share_anomaly_pct']}%  "
              f"p75 {r['p75_normal']}->{r['p75_anomaly']}")
    if focus_selection["additional_count"]:
        print(f"   (+{focus_selection['additional_count']} more: {focus_selection['additional_segments']})")
    if coverage:
        print(f"coverage: focus explains {int(round(coverage['coverage_ratio']*100))}% of the "
              f"p75 change (sufficient={coverage['sufficient']})")
    if other_watch["flagged"]:
        print(f"[watch] 'other' bucket degraded: p75 +{other_watch['p75_delta_ms']}ms")

    # v4: within-regression is per-focus; compute for the primary focus (kept for compatibility)
    within_flags=composite_verdict_flags(focus)
    # v6.9.2: the focus's raw p75 rise includes sub-composition (more paid /
    # low-memory traffic within it). Only call it a genuine regression when the
    # composition-controlled interacted decomposition ALSO shows a material
    # within effect — otherwise the rise is a mix shift, not a page slowdown.
    within_flags["within_regression"] = bool(
        within_flags["within_regression"] and materiality.get("within_material"))
    if focus:
        print(f"primary focus within-regression: {within_flags['within_regression']} "
              f"(median Δ={within_flags['focus_median_delta_ms']}ms, p75 Δ={within_flags['focus_p75_delta_ms']}ms)")


    # Cell 9 — Step D+E: audience behavior signals + delivery-layer health
    behavior_overall = behavior_signals(df, LABEL_COL)
    behavior_focus   = behavior_signals(focus_df, LABEL_COL)
    print("new-visitor influx — overall:", behavior_overall["new_visitor_influx"],
          "| focus segment:", behavior_focus["new_visitor_influx"])

    delivery = delivery_health(df, LABEL_COL)
    print("delivery verdict:", delivery["verdict"], delivery["issues"] or "")
    for m, v in delivery["metrics"].items():
        print(f"   {m}: {v['normal_median']} -> {v['anomaly_median']}")


    # Cell 10 — Step F: models as evidence (skipped when nothing to explain)
    fingerprint, drivers = None, None
    if severity not in ("none", "improved"):
        fingerprint = window_classifier_fingerprint(df, CATEGORICAL, LABEL_COL)
        print(f"composition fingerprint — holdout AUC={fingerprint['holdout_auc']} "
              f"(gate {'passed' if fingerprint['gate_passed'] else 'FAILED — windows statistically similar'})")
        for f_ in fingerprint["fingerprint"][:6]:
            print(f"   {f_['feature']:<38} share {f_['share_normal_pct']}% -> "
                  f"{f_['share_anomaly_pct']}%")
        drivers = timer_regressor_drivers(df, CATEGORICAL, DELIVERY_NUMERIC,
                                          TIMER_COL, LABEL_COL, ARTIFACT_MS)
        print(f"\nLCP drivers — regressor R2={drivers['train_r2']}, "
              f"predicted window shift={drivers['predicted_shift_pct']}%")
        for d_ in drivers["drivers"][:8]:
            print(f"   {d_['feature']:<38} {d_['direction']:<9} "
                  f"median {d_['value_median_normal']} -> {d_['value_median_anomaly']}")
    else:
        print("severity gate: models skipped")

    # v3: separate causal driver candidates from associated symptoms (endogenous
    # indicators of *who* is visiting, not reasons the site slowed down)
    SYMPTOM_FEATURES = {"cacherate", "cdncacherate", "transferbyte", "bodysize",
                        "requestcount", "landingpage", "referrer_present", "paidmedia"}
    driver_candidates, associated_symptoms = [], []
    if drivers:
        for d_ in drivers["drivers"]:
            base = d_["feature"].split("_")[0]
            target = (associated_symptoms
                      if (d_["feature"] in SYMPTOM_FEATURES or base in SYMPTOM_FEATURES)
                      else driver_candidates)
            target.append(d_)
        print("\ncausal driver candidates:", [d_["feature"] for d_ in driver_candidates][:5])
        print("associated symptoms (NOT causes):", [d_["feature"] for d_ in associated_symptoms][:5])


    # Cell 11 — Step G: findings JSON + verdict (single source of truth)  [v3]
    from datetime import datetime, timezone

    verdict_code, verdict_sentence = select_verdict(
        severity, decomp[PRIMARY_DIM], localization, behavior_focus, delivery,
        outliers, within_flags=within_flags,
        focus_selection=focus_selection, coverage=coverage, materiality=materiality)

    def _label_movers(movers, dim):
        out = []
        for r in movers:
            r = dict(r)
            r["human_label"] = humanize_feature(f"{dim}_{r['segment']}", FEATURE_LABEL_OVERRIDES)
            r["token"] = page_token(r["segment"])          # ⟦PG:seg⟧ for the LLM to copy
            r["example_url"] = PAGE_GROUP_URLS.get(r["segment"])
            out.append(r)
        return out

    # customer-readable label per page group (used by the deterministic token renderer)
    PAGE_GROUP_LABELS = {}
    for r in primary_movers["share_movers"]:
        PAGE_GROUP_LABELS.setdefault(
            r["segment"], humanize_feature(f"{PRIMARY_DIM}_{r['segment']}", FEATURE_LABEL_OVERRIDES))
    for r in focus_list:
        PAGE_GROUP_LABELS[r["segment"]] = humanize_feature(
            f"{PRIMARY_DIM}_{r['segment']}", FEATURE_LABEL_OVERRIDES)

    def _label_focus(r):
        r=dict(r)
        r["human_label"]=humanize_feature(f"{PRIMARY_DIM}_{r['segment']}", FEATURE_LABEL_OVERRIDES)
        r["token"]=page_token(r["segment"])
        r["example_url"]=PAGE_GROUP_URLS.get(r["segment"])
        return r

    symptom_labels = ([humanize_feature(d_["feature"], FEATURE_LABEL_OVERRIDES)
                       for d_ in associated_symptoms] if drivers else [])

    timer_name = METRIC_PROFILE["display_name"]     # e.g. "Largest Contentful Paint"
    timer_abbrev = METRIC_PROFILE["abbrev"]
    # good/needs-improvement/poor rating of each window's p75
    p75_rating = {"normal": rate_value(METRIC_PROFILE, win["normal"]["p75"]),
                  "anomaly": rate_value(METRIC_PROFILE, win["anomaly"]["p75"])}
    findings = {
        "meta": {"metric": timer_name, "metric_abbrev": timer_abbrev, "profile": METRIC_PROFILE["profile_key"], "p75_rating": p75_rating, "rows": int(len(df)),
                 "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "stats_source": "sidecar_metadata" if source_meta else "csv_sample"},
        "headline": {
            "p75_normal": win["normal"]["p75"], "p75_anomaly": win["anomaly"]["p75"],
            "delta_ms": win["delta_p75_ms"], "delta_pct": win["delta_p75_pct"],
            "ci95": win["delta_p75_ci95"], "severity": severity, "rating_normal": p75_rating["normal"], "rating_anomaly": p75_rating["anomaly"], "abbrev": timer_abbrev,
            "transition_sentence": (
                f"{timer_name} p75 moved from {fmt_ms(win['normal']['p75'])} (normal window) to "
                f"{fmt_ms(win['anomaly']['p75'])} (anomaly window), a change of "
                f"{fmt_ms(win['delta_p75_ms'])} ({fmt_pct(win['delta_p75_pct'])}).")},
        "verdict": {"code": verdict_code, "sentence": verdict_sentence},
        "within_regression": within_flags,
        "focus_breakdown": focus_composition,
        "segments": {
            "primary_dim": PRIMARY_DIM,
            "primary_share_movers": _label_movers(primary_movers["share_movers"][:5], PRIMARY_DIM),
            "focus_list": [_label_focus(r) for r in focus_list],
            "additional_count": focus_selection["additional_count"],
            "additional_segments": focus_selection["additional_segments"],
            "focus": (dict(focus,
                           human_label=humanize_feature(f"{PRIMARY_DIM}_{focus['segment']}", FEATURE_LABEL_OVERRIDES),
                           token=page_token(focus["segment"]),
                           example_url=PAGE_GROUP_URLS.get(focus["segment"]),
                           reason=focus.get("reason","primary problem section")) if focus else None),
            "drilldown": {d: _label_movers(t["share_movers"][:4], d)
                          for d, t in drilldown.items()}},
        "localization": localization,
        "behavior": behavior_focus | {"scope": (focus["segment"] if focus else "overall")},
        "delivery": delivery,
        "outliers": outliers,
        "decomposition": p75_decomp,
        "decomposition_mean": decomp[PRIMARY_DIM],
        "coverage": coverage,
        "other_watch": other_watch,
        "composition_fingerprint": ([{**f_, "human_label": humanize_feature(
            f_["feature"], FEATURE_LABEL_OVERRIDES)} for f_ in fingerprint["fingerprint"][:6]]
            if fingerprint and fingerprint["gate_passed"] else []),
        "performance_drivers": ([{**d_, "human_label": humanize_feature(
            d_["feature"], FEATURE_LABEL_OVERRIDES)} for d_ in driver_candidates[:6]]
            if drivers else []),
        "associated_symptoms": ([{**d_, "human_label": humanize_feature(
            d_["feature"], FEATURE_LABEL_OVERRIDES),
            "note": "indicator of audience change, not a performance cause"}
            for d_ in associated_symptoms[:6]] if drivers else []),
    }
    # v5: attach Akamai-playbook remediation and findings-consistent hypotheses
    findings["remediation_playbook"] = match_playbook(findings, metric_key=METRIC_PROFILE["profile_key"])
    findings["hypotheses"] = derive_hypotheses(findings)

    # v6.1: Python pre-writes every load-bearing sentence so the model never has to
    # pick the right field out of the JSON (root cause of number misassignment)
    findings["narrative_facts"] = build_narrative_facts(findings)
    # v6.3: same sentences, grouped by the section that should contain them, so the
    # model places them instead of turning the keys into headings
    findings["section_facts"] = build_section_facts(findings)

    allowed_numbers = collect_numbers(findings)
    # example URLs contain digits that are references, not metrics — exclude from the number whitelist check
    findings_text = json.dumps(findings, indent=2, ensure_ascii=False, default=str)
    print(f"verdict={verdict_code} | severity={severity} | "
          f"{len(allowed_numbers)} numbers whitelisted | "
          f"{len(PAGE_GROUP_URLS)} example URLs ready")


    want_hypotheses = bool(findings.get("hypotheses"))
    required_sections = list(REQUIRED_SECTIONS)
    if want_hypotheses:
        required_sections.insert(4, "Hypotheses")
    prompt = build_llm_prompt_v63(findings_text, findings["section_facts"], want_hypotheses)
    allowed_numbers = build_allowed_numbers(findings, findings_text)
    metric_bindings = build_metric_bindings(findings)

    # v6.2 GUARD: anything we hand the model must pass our own validators, or the
    # model gets punished for obeying us and the retry loop can never converge.
    # (This is what caused the persistent '178.0 used with p75' rejections.)
    # covers narrative_facts, section_facts, playbook actions AND hypotheses —
    # v6.5 checked only narrative_facts, so a sentence living solely in
    # section_facts (the localization line) deadlocked every attempt
    _bugs = self_check_supplied_text(findings, metric_bindings, allowed_numbers)
    if _bugs:
        print("[PIPELINE BUG] text supplied to the model fails our own gates:")
        for b in _bugs:
            print("   -", b)
        raise AssertionError("Fix the pipeline (bindings/fact wording) before running the LLM.")
    print(f"self-check passed: {len(collect_supplied_text(findings))} supplied strings are gate-clean")
    playbook_actions = findings.get("remediation_playbook", [])
    focus_segments = [r["segment"] for r in (findings["segments"].get("focus_list") or [])]

    best = {"score": None, "text": None, "num": None}
    report_md, report_source = None, "fallback_template"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            cand = call_llm(prompt)
        except Exception as e:
            print(f"[warn] LLM unavailable ({e}); using deterministic fallback")
            break

        # v6.1: absorb formatting variance BEFORE judging content
        cand = normalize_draft(cand)
        cand = inject_segment_urls(cand, PAGE_GROUP_URLS, PAGE_GROUP_LABELS, focus_segments)

        num        = validate_report_numbers_v61(cand, allowed_numbers)
        raw_feat   = [d_["feature"] for d_ in findings["performance_drivers"] if d_["feature"] in cand]
        causal_bad = check_causal_misuse_v6(cand, symptom_labels)
        scope_bad  = check_recommendation_scope(cand, playbook_actions)
        bind_bad   = check_number_binding(cand, metric_bindings)
        degen_bad  = check_degenerate_comparison(cand)
        url_missing= verify_urls_present(cand, PAGE_GROUP_URLS, focus_segments)
        structure  = has_required_structure(cand)
        sections   = check_section_completeness(cand, required_sections)

        score = (score_draft(num, raw_feat, causal_bad, scope_bad, structure)
                 + 100 * len(bind_bad) + 80 * len(degen_bad) + 50 * len(url_missing)
                 + 90 * len(sections["missing"]) + 10 * len(sections["unexpected"]))
        if best["score"] is None or score < best["score"]:
            best = {"score": score, "text": cand, "num": num}

        blocking = bool(num["hard"] or raw_feat or causal_bad or scope_bad
                        or bind_bad or degen_bad or url_missing or not structure
                        or sections["missing"])
        if not blocking:
            clean = repair_soft_numbers(cand, num["soft"]) if num["soft"] else cand
            try:
                critic = parse_critic_response(
                    call_llm(build_critic_prompt(clean, findings_text), json_mode=True))
            except Exception as e:
                critic = {"pass": True, "issues": [], "severity": "none", "_skipped": str(e)}
            if critic.get("pass") or critic.get("severity") in ("none", "minor"):
                report_md, report_source = clean, f"llm+critic ({LLM_MODEL})"
                if critic.get("issues"):
                    print(f"[critic] minor issues noted: {critic['issues'][:2]}")
                break
            print(f"[attempt {attempt}] critic rejected (severity={critic.get('severity')}): "
                  f"{critic.get('issues', [])[:2]}")
            fixes = "; ".join(i.get("detail", "") for i in critic.get("issues", [])[:3])
        else:
            # v6.1: every blocking condition is now visible, including structure
            print(f"[attempt {attempt}] rejected — hard:{num['hard'][:3]} soft:{num['soft'][:3]} "
                  f"raw_feats:{raw_feat[:2]} symptom-as-cause:{causal_bad[:2]} "
                  f"out-of-scope:{scope_bad[:2]} number-binding:{bind_bad[:2]} "
                  f"degenerate:{degen_bad[:2]} url_missing:{url_missing[:2]} "
                  f"structure_ok:{structure} missing_sections:{sections['missing']} "
                  f"unexpected_sections:{sections['unexpected'][:3]}")
            # v6.3: show the head of the rejected draft so structure failures are
            # diagnosable from the log alone
            print("           draft head: " + " / ".join(cand.strip().splitlines()[:3])[:200])
            f_ = []
            if num["hard"]:   f_.append("remove numbers absent from findings: " + ", ".join(num["hard"][:5]))
            if bind_bad:      f_.append("wrong number for that metric — use narrative_facts: " + "; ".join(bind_bad[:3]))
            if degen_bad:     f_.append("both sides of the comparison are identical: " + "; ".join(degen_bad[:2]))
            if causal_bad:    f_.append("do not call these a driver/cause: " + ", ".join(causal_bad))
            if scope_bad:     f_.append("remove out-of-scope recommendations: " + ", ".join(scope_bad))
            if raw_feat:      f_.append("use human_label instead of raw field names: " + ", ".join(raw_feat[:3]))
            if not structure: f_.append("start with a '## Executive Summary' heading, plain Markdown, no code fences")
            if sections["missing"]:
                f_.append("add the missing sections: " + ", ".join(sections["missing"]))
            if sections["unexpected"]:
                f_.append("remove sections outside the contract: " + ", ".join(sections["unexpected"][:4]))
            fixes = "; ".join(f_)
        prompt += f"\n\nREVISE your previous draft with MINIMAL edits. Fix exactly: {fixes}"

    if report_md is None and best["text"] is not None and best["score"] < 100:
        report_md = repair_soft_numbers(best["text"], best["num"]["soft"])
        # v6.3: never ship an incomplete report — splice any missing section from the
        # deterministic reference so the playbook actions are always included
        findings["_token_fn"] = page_token
        reference = render_page_tokens(render_fallback_report_v5(findings),
                                       PAGE_GROUP_URLS, PAGE_GROUP_LABELS)
        gaps = check_section_completeness(report_md, required_sections)["missing"]
        if gaps:
            report_md = splice_missing_sections(report_md, reference, gaps)
            print(f"[repair] spliced missing sections from the deterministic report: {gaps}")
        report_source = f"llm best-of-{MAX_ATTEMPTS}+repaired ({LLM_MODEL})"
        print(f"[fallback] no fully clean draft; shipping best attempt (score={best['score']})")

    if report_md is None:
        findings["_token_fn"] = page_token
        report_md = inject_segment_urls(
            render_page_tokens(render_fallback_report_v5(findings), PAGE_GROUP_URLS, PAGE_GROUP_LABELS),
            PAGE_GROUP_URLS, PAGE_GROUP_LABELS, focus_segments)

    # final assertion: no focus section may ship without its example URL
    assert not verify_urls_present(report_md, PAGE_GROUP_URLS, focus_segments), "example URL missing"
    _final = check_section_completeness(report_md, required_sections)
    assert _final["complete"], f"report is missing sections: {_final['missing']}"
    print(f"report source: {report_source}\n")
    print(report_md)


    # Cell 13 — Step I: email assembly + send (env-configured, DRY_RUN by default)
    email_subject = subject_prefix + (f"[{severity.upper()}] {timer_name} p75 "
                     f"{win['normal']['p75']} -> {win['anomaly']['p75']}ms "
                     f"({win['delta_p75_pct']:+}%) — {verdict_code.replace('_',' ')}")
    email_plain = "\n".join([f"# {timer_name} anomaly report", "",
                              findings["headline"]["transition_sentence"], "",
                              report_md])
    email_html = md_to_html(email_plain)


    return {
        "report_md": report_md,
        "report_source": report_source,
        "email_subject": email_subject,
        "email_plain": email_plain,
        "email_html": email_html,
        "severity": severity,
        "verdict_code": verdict_code,
        "metric_name": METRIC_NAME,
        "findings": findings,
    }
