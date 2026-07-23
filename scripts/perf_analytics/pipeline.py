from __future__ import annotations

import json
import re
from dataclasses import dataclass
from ftplib import FTP
from pathlib import Path
from typing import Tuple, TypedDict

import numpy as np
import pandas as pd
import requests
import shap
import xgboost as xgb


@dataclass
class PipelineResult:
    timer_metric_name: str
    timer_transition_label: str
    timer_change_summary: str
    anomaly_direction: str
    selected_features: pd.DataFrame
    selected_appendix: pd.DataFrame
    analysis_report: str
    top5_coverage_note: str


class TimerState(TypedDict):
    raw_timer_p75_all: float
    raw_timer_p75_anomaly: float
    raw_timer_p75_normal: float
    timer_transition_label: str
    anomaly_direction: str
    timer_change_summary: str
    plain_transition_summary: str


def extract_timer_name_from_filename(filename: str) -> str:
    match = re.match(r"^sample_data_[^_]+_[^_]+_(.+?)_iter5_win1_inter5\.csv$", filename)
    return match.group(1) if match else "timer"


def fetch_csv_from_ftp(
    filename: str,
    ftp_host: str,
    ftp_user: str,
    ftp_password: str,
    ftp_remote_dir: str,
    local_target: Path,
) -> Path:
    local_target.parent.mkdir(parents=True, exist_ok=True)
    with FTP(ftp_host) as ftp:
        ftp.login(user=ftp_user, passwd=ftp_password)
        if ftp_remote_dir:
            ftp.cwd(ftp_remote_dir)
        with local_target.open("wb") as out:
            ftp.retrbinary(f"RETR {filename}", out.write)
    return local_target


def _compute_timer_state(df: pd.DataFrame, y: pd.Series, metric_name: str) -> TimerState:
    p75_all = float(df["timer"].quantile(0.75))
    p75_anomaly = float(df.loc[y == 1, "timer"].quantile(0.75)) if (y == 1).any() else np.nan
    p75_normal = float(df.loc[y == 0, "timer"].quantile(0.75)) if (y == 0).any() else np.nan

    normal_ms = int(round(p75_normal)) if not np.isnan(p75_normal) else None
    anomaly_ms = int(round(p75_anomaly)) if not np.isnan(p75_anomaly) else None
    transition = f"{normal_ms}(ms) -> {anomaly_ms}(ms)" if normal_ms is not None and anomaly_ms is not None else "N/A(ms) -> N/A(ms)"

    if np.isnan(p75_anomaly) or np.isnan(p75_normal):
        direction = "worsening"
        summary = f"Unable to compute a reliable normal-vs-anomaly delta for {metric_name}."
        plain_summary = f"The normal-vs-anomaly transition for {metric_name} is unavailable."
    else:
        delta = p75_anomaly - p75_normal
        direction = "worsening" if delta >= 0 else "improving"
        direction_word = "worsened" if delta >= 0 else "improved"
        summary = (
            f"{metric_name} moved from normal {p75_normal:.2f}ms "
            f"to anomaly {p75_anomaly:.2f}ms ({delta:+.2f}ms), indicating a sudden {direction_word} state."
        )
        plain_summary = f"{metric_name} changed to {transition}, indicating a sudden {direction_word} state."

    return {
        "raw_timer_p75_all": p75_all,
        "raw_timer_p75_anomaly": p75_anomaly,
        "raw_timer_p75_normal": p75_normal,
        "timer_transition_label": transition,
        "anomaly_direction": direction,
        "timer_change_summary": summary,
        "plain_transition_summary": plain_summary,
    }


def _prepare_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
    df = df.copy()
    df["deviceMemory"] = df["deviceMemory"].astype(str)

    features_to_drop = list(
        set([
            "label",
            "timer",
            "cacherate",
            "transferbyte",
            "bodysize",
            "requestcount",
            "cdncacherate",
            "edgetime",
            "origintime",
            "rtt",
        ])
    )
    X = df.drop(features_to_drop, axis=1)
    y = df["label"]
    categorical_columns = list(X.select_dtypes(include=["object"]).columns)
    X = pd.get_dummies(X, columns=categorical_columns, drop_first=True)

    # Anomaly-centric timer weighting:
    # - weight only anomaly(y=1) samples by sqrt(timer / mean_anomaly_timer)
    # - clip to [1, 5] to prevent outliers from dominating training
    # - keep normal(y=0) samples at weight=1 to preserve classification (PR-AUC)
    timer = df["timer"].to_numpy()
    anomaly_mask = (y == 1).to_numpy()
    anomaly_mean = timer[anomaly_mask].mean() if anomaly_mask.any() else timer.mean()
    weights = np.ones(len(df))
    anomaly_weight = np.sqrt(timer[anomaly_mask] / anomaly_mean)
    anomaly_weight = np.clip(anomaly_weight, 1.0, 5.0)
    weights[anomaly_mask] = anomaly_weight
    weights = pd.Series(weights, index=df.index)
    return X, y, weights


def _build_feature_impact(X: pd.DataFrame, shap_array: np.ndarray, y: pd.Series) -> pd.DataFrame:
    x_matrix = X.to_numpy()
    pos_impact, neg_impact, med_impact, on_ratio, is_binary = [], [], [], [], []
    signed_mean, anom_signed, norm_signed = [], [], []
    on_anom, off_anom, presence_effect = [], [], []

    y_array = np.asarray(y)
    anomaly_mask = y_array == 1
    normal_mask = y_array == 0

    for idx in range(shap_array.shape[1]):
        feature_shap = shap_array[:, idx]
        feature_vals = x_matrix[:, idx]

        unique_vals = np.unique(feature_vals)
        binary_flag = len(unique_vals) <= 2 and np.isin(unique_vals, [0, 1]).all()
        is_binary.append(binary_flag)
        on_ratio.append(float((feature_vals == 1).mean()) if binary_flag else np.nan)

        non_zero = feature_shap[feature_shap != 0]
        if len(non_zero) > 0:
            pos = non_zero[non_zero > 0]
            neg = non_zero[non_zero < 0]
            pos_impact.append(pos.mean() if len(pos) > 0 else 0.0)
            neg_impact.append(neg.mean() if len(neg) > 0 else 0.0)
            med_impact.append(float(np.median(non_zero)))
        else:
            pos_impact.append(0.0)
            neg_impact.append(0.0)
            med_impact.append(0.0)

        signed_mean.append(float(feature_shap.mean()))
        anom_mean = float(feature_shap[anomaly_mask].mean()) if anomaly_mask.any() else 0.0
        norm_mean = float(feature_shap[normal_mask].mean()) if normal_mask.any() else 0.0
        anom_signed.append(anom_mean)
        norm_signed.append(norm_mean)

        if binary_flag and anomaly_mask.any():
            anom_on_mask = anomaly_mask & (feature_vals == 1)
            anom_off_mask = anomaly_mask & (feature_vals == 0)
            on_shap = float(feature_shap[anom_on_mask].mean()) if anom_on_mask.any() else 0.0
            off_shap = float(feature_shap[anom_off_mask].mean()) if anom_off_mask.any() else 0.0
            on_anom.append(on_shap)
            off_anom.append(off_shap)
            presence_effect.append(on_shap - off_shap)
        else:
            on_anom.append(np.nan)
            off_anom.append(np.nan)
            presence_effect.append(np.nan)

    feature_impact = pd.DataFrame(
        {
            "feature": X.columns,
            "importance": np.abs(shap_array).mean(0),
            "positive_impact": pos_impact,
            "negative_impact": neg_impact,
            "median_impact": med_impact,
            "is_binary": is_binary,
            "on_ratio": on_ratio,
            "signed_mean_impact": signed_mean,
            "anomaly_signed_impact": anom_signed,
            "normal_signed_impact": norm_signed,
            "binary_on_anomaly_shap": on_anom,
            "binary_off_anomaly_shap": off_anom,
            "binary_presence_effect": presence_effect,
        }
    )
    feature_impact["net_impact"] = feature_impact["positive_impact"] + feature_impact["negative_impact"]
    feature_impact["anomaly_delta_impact"] = feature_impact["anomaly_signed_impact"] - feature_impact["normal_signed_impact"]
    feature_impact["effect_for_ranking"] = np.where(
        feature_impact["is_binary"],
        feature_impact["binary_presence_effect"].fillna(feature_impact["anomaly_signed_impact"]),
        feature_impact["anomaly_signed_impact"],
    )
    feature_impact["dominant_direction"] = np.where(
        feature_impact["effect_for_ranking"] > 0,
        "worsening",
        np.where(feature_impact["effect_for_ranking"] < 0, "improving", "mixed"),
    )
    return feature_impact.sort_values(by="importance", ascending=False)


def _select_directional_features(
    feature_impact: pd.DataFrame,
    anomaly_direction: str,
    min_on_ratio: float,
) -> Tuple[pd.DataFrame, pd.DataFrame, str, str, str]:
    support_factor = np.where(
        feature_impact["is_binary"],
        np.clip(feature_impact["on_ratio"].fillna(0), min_on_ratio, 1.0),
        1.0,
    )
    support_weight = np.power(support_factor, 0.25)

    feature_impact["worsening_score"] = (
        feature_impact["importance"] * np.maximum(feature_impact["effect_for_ranking"], 0) * support_weight
    )
    feature_impact["improving_score"] = (
        feature_impact["importance"] * np.abs(np.minimum(feature_impact["effect_for_ranking"], 0)) * support_weight
    )

    worsening_candidates = feature_impact[feature_impact["effect_for_ranking"] > 0].copy()
    worsening_candidates = worsening_candidates[(~worsening_candidates["is_binary"]) | (worsening_candidates["on_ratio"] >= min_on_ratio)]
    worsening_features = worsening_candidates.sort_values(by=["worsening_score", "importance"], ascending=False).head(10).copy()
    worsening_features["impact_type"] = "Performance-Worsening"

    improving_candidates = feature_impact[feature_impact["effect_for_ranking"] < 0].copy()
    improving_candidates = improving_candidates[(~improving_candidates["is_binary"]) | (improving_candidates["on_ratio"] >= min_on_ratio)]
    improving_features = improving_candidates.sort_values(by=["improving_score", "importance"], ascending=False).head(10).copy()
    improving_features["impact_type"] = "Performance-Improving"

    rare_worsening = feature_impact[
        (feature_impact["effect_for_ranking"] > 0)
        & (feature_impact["is_binary"])
        & (feature_impact["on_ratio"] < min_on_ratio)
    ].sort_values(by=["effect_for_ranking", "importance"], ascending=False).head(10)

    rare_improving = feature_impact[
        (feature_impact["effect_for_ranking"] < 0)
        & (feature_impact["is_binary"])
        & (feature_impact["on_ratio"] < min_on_ratio)
    ].sort_values(by=["effect_for_ranking", "importance"], ascending=True).head(10)

    if anomaly_direction == "worsening":
        return worsening_features, rare_worsening, "worsening_score", "effect_for_ranking", "Performance-Worsening"
    return improving_features, rare_improving, "improving_score", "effect_for_ranking", "Performance-Improving"


def _check_top5_coverage(analysis_report: str, selected_features: pd.DataFrame) -> list[str]:
    top5_features = selected_features["feature"].head(5).tolist()
    missing: list[str] = []
    for feat in top5_features:
        tokens = [feat]
        if feat.startswith("url_"):
            tokens.append(feat[len("url_"):])
        elif feat.startswith("referrer_"):
            tokens.append(feat[len("referrer_"):])
        if not any(token in analysis_report for token in tokens):
            missing.append(feat)
    return missing


def _build_feature_context_df(X: pd.DataFrame, shap_array: np.ndarray, selected_features: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for feature in selected_features["feature"]:
        idx = X.columns.get_loc(feature)
        vals = X[feature].to_numpy()
        shap_col = shap_array[:, idx]
        unique_vals = np.unique(vals)
        binary = len(unique_vals) <= 2 and np.isin(unique_vals, [0, 1]).all()
        if binary:
            on_mask = vals == 1
            off_mask = vals == 0
            rows.append({
                "feature": feature,
                "feature_type": "binary(one-hot)",
                "on_ratio": float(on_mask.mean()),
                "on_mean_shap": float(shap_col[on_mask].mean()) if on_mask.any() else 0.0,
                "off_mean_shap": float(shap_col[off_mask].mean()) if off_mask.any() else 0.0,
                "value_shap_corr": np.nan,
                "p50_value": np.nan,
                "p90_value": np.nan,
                "p99_value": np.nan,
            })
        else:
            corr = float(np.corrcoef(vals, shap_col)[0, 1]) if np.std(vals) > 0 and np.std(shap_col) > 0 else 0.0
            rows.append({
                "feature": feature,
                "feature_type": "numeric/continuous",
                "on_ratio": np.nan,
                "on_mean_shap": np.nan,
                "off_mean_shap": np.nan,
                "value_shap_corr": corr,
                "p50_value": float(np.percentile(vals, 50)),
                "p90_value": float(np.percentile(vals, 90)),
                "p99_value": float(np.percentile(vals, 99)),
            })
    return pd.DataFrame(rows)


def _build_prompt(
    plain_transition_summary: str,
    anomaly_direction: str,
    direction_logic_desc: str,
    timer_metric_name: str,
    anomaly_threshold_desc: str,
    dataset_rows: int,
    feature_count: int,
    anomaly_rate: float,
    selected_top5_text: str,
    allowed_feature_list_text: str,
    selected_text: str,
    selected_appendix_text: str,
    feature_context_text: str,
) -> str:
    return f"""You are a website performance analyst.
Your task is to explain only the root causes for the selected anomaly direction.

Language policy (strict):
- Write the entire report in English only.
- Do not use Korean, Chinese, Japanese, or mixed-language output.
- Do not copy or quote any example sentence template from this prompt.

## Report Opening Requirement (must follow)
- In section 1 (Executive Summary), first sentence must use this transition summary in your own wording:
  {plain_transition_summary}
- In section 1, add one additional sentence that summarizes the most likely user-facing cause.
- Do not output any template-style sentence verbatim.

## Direction Decision (must follow strictly)
- Selected anomaly direction: {anomaly_direction}
- Decision rule: {direction_logic_desc}
- If selected direction is worsening: explain only worsening factors.
- If selected direction is improving: explain only improving factors.
- Never describe the opposite direction in this report.

## Ground Truth Context
- Timer metric: {timer_metric_name}
- Timer semantics: Higher timer means worse page load performance.
- Anomaly threshold details: {anomaly_threshold_desc}
- Label semantics: y=1 anomaly, y=0 normal.

## Model & Data Context
- Model: XGBoost classifier with sample weights.
- Weighting rule: anomaly(y=1) samples weighted by sqrt(timer / mean_anomaly_timer), clipped to [1, 5]; normal(y=0) samples keep weight = 1.
- Dataset rows: {dataset_rows}
- Feature count after one-hot encoding: {feature_count}
- Observed anomaly rate: {anomaly_rate:.4f}

## Writing Rules For End Users (must follow)
1) Do not use technical terms such as SHAP, feature importance, one-hot encoding, percentile, score, model, or classifier.
2) Do not include numeric analysis values in the narrative body.
3) Use user-friendly expressions like page section, network condition, mobile users, and traffic pattern.
4) Explain causes and actions in practical, non-technical language.

## Mandatory Requirements
1) Mention only feature names that exist in the Allowed Feature Names list.
2) If a feature is not in the list, do not invent or guess a new name.
3) In Root Causes, explicitly cover Selected Top5 first before adding any other details.
4) If any Selected Top5 is excluded from Root Causes, explain the reason in section 7.

## Selected Top5
{selected_top5_text}

## Allowed Feature Names
{allowed_feature_list_text}

## Directional Feature Data
{selected_text}

## Appendix (rare one-hot)
{selected_appendix_text}

## Feature-wise Context
{feature_context_text}

## Required output format (Markdown)
1. Executive Summary
2. Direction-Matched Root Causes (Top drivers)
3. Business Implications
4. Actionable Recommendations
5. Monitoring Priorities
6. Interpretation Guardrails
7. Selected Top5 Exclusion Reason

## Section 7 Requirement (MANDATORY)
REGARDLESS of whether selected Top5 is covered or missing:
- If ALL selected Top5 are covered in Root Causes: Write a brief statement confirming full coverage.
- If ANY selected Top5 is missing: Explain why those features were excluded and why they are less relevant than the covered ones.
Always write Section 7 - it must appear in every report.

Keep the report concise and practical for performance engineers.
"""


def _enforce_executive_summary_ground_truth(report_text: str, ground_truth_line: str) -> str:
    """Force the first non-empty line under the Executive Summary heading to be
    the timer ground-truth sentence, so the narrative can never contradict the
    measured p75 transition."""
    lines = report_text.splitlines()
    summary_heading_idx = None
    for i, raw in enumerate(lines):
        normalized = raw.strip().lower()
        if normalized in ("### executive summary", "## executive summary", "# executive summary", "1. executive summary"):
            summary_heading_idx = i
            break

    if summary_heading_idx is None:
        return report_text

    for j in range(summary_heading_idx + 1, len(lines)):
        current = lines[j].strip()
        if not current:
            continue
        if current.startswith("#"):
            break
        lines[j] = ground_truth_line
        return "\n".join(lines)

    lines.insert(summary_heading_idx + 1, ground_truth_line)
    return "\n".join(lines)


def _generate_report_with_ollama(prompt: str, ollama_url: str, model: str) -> str:
    try:
        response = requests.post(
            ollama_url,
            json={"model": model, "prompt": prompt, "stream": True, "options": {"temperature": 0}},
            stream=True,
            timeout=300,
        )
        response.raise_for_status()
    except Exception as exc:
        return f"LLM report generation failed: {exc}"

    chunks = []
    for line in response.iter_lines():
        if not line:
            continue
        data = json.loads(line)
        if "response" in data:
            chunks.append(data["response"])
    return "".join(chunks).strip()


def _fallback_report(selected_features: pd.DataFrame, anomaly_direction: str, summary: str) -> str:
    top_rows = selected_features[["feature", "importance", "effect_for_ranking"]].head(5)
    bullets = "\n".join(
        [f"- {r.feature}: importance={r.importance:.4f}, directional_effect={r.effect_for_ranking:.4f}" for r in top_rows.itertuples()]
    )
    return (
        "1. Executive Summary\n"
        f"- {summary}\n\n"
        "2. Direction-Matched Root Causes\n"
        f"- Direction: {anomaly_direction}\n"
        f"{bullets}\n"
    )


def md_to_simple_html(text: str) -> str:
    lines = text.splitlines()
    html = []
    for line in lines:
        if line.startswith("### "):
            html.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            html.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            html.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            html.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            html.append("<br>")
        else:
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            line = re.sub(r"`([^`]+)`", r"<code>\1</code>", line)
            html.append(f"<p>{line}</p>")
    return "<html><body style='font-family:Arial,sans-serif;'>" + "\n".join(html) + "</body></html>"


def run_pipeline(
    csv_path: Path,
    ollama_url: str,
    ollama_model: str,
    min_onehot_on_ratio: float,
    use_llm: bool = True,
) -> PipelineResult:
    df = pd.read_csv(csv_path)
    timer_metric_name = extract_timer_name_from_filename(csv_path.name)

    X, y, weights = _prepare_features(df)
    model = xgb.XGBClassifier(eval_metric="logloss")
    model.fit(X, y, sample_weight=weights)

    explainer = shap.Explainer(model)
    shap_values = explainer(X)
    shap_array = shap_values.values if hasattr(shap_values, "values") else np.asarray(shap_values)

    timer_state = _compute_timer_state(df, y, timer_metric_name)
    feature_impact = _build_feature_impact(X, shap_array, y)

    selected_features, selected_appendix, selected_score_col, _, _ = _select_directional_features(
        feature_impact,
        timer_state["anomaly_direction"],
        min_onehot_on_ratio,
    )

    selected_text = selected_features[
        [
            "feature",
            "importance",
            "positive_impact",
            "negative_impact",
            "signed_mean_impact",
            "anomaly_signed_impact",
            "normal_signed_impact",
            "binary_on_anomaly_shap",
            "binary_off_anomaly_shap",
            "binary_presence_effect",
            "effect_for_ranking",
            "anomaly_delta_impact",
            "median_impact",
            "net_impact",
            selected_score_col,
            "on_ratio",
            "impact_type",
        ]
    ].to_string(index=False)
    selected_appendix_text = (
        selected_appendix[["feature", "importance", "effect_for_ranking", "anomaly_signed_impact", "binary_presence_effect", "anomaly_delta_impact", "on_ratio"]].to_string(index=False)
        if len(selected_appendix) > 0
        else "No rare one-hot features for this direction."
    )

    selected_top5_text = (
        selected_features[["feature", "importance", selected_score_col, "on_ratio"]].head(5).to_string(index=False)
        if len(selected_features) > 0
        else "No selected Top5 features."
    )

    allowed_feature_list_text = "\n".join(
        f"- {name}" for name in sorted(set(selected_features["feature"].tolist()) | set(selected_appendix["feature"].tolist()))
    )

    feature_context_df = _build_feature_context_df(X, shap_array, selected_features)
    feature_context_text = feature_context_df.to_string(index=False) if len(feature_context_df) > 0 else "No context rows available."

    dataset_rows = int(len(X))
    feature_count = int(X.shape[1])
    anomaly_rate = float(y.mean()) if len(y) > 0 else float("nan")
    direction_logic_desc = (
        f"raw {timer_metric_name} p75 comparison: "
        f"anomaly={timer_state['raw_timer_p75_anomaly']:.4f}, "
        f"normal={timer_state['raw_timer_p75_normal']:.4f}, "
        f"all={timer_state['raw_timer_p75_all']:.4f}"
    )
    anomaly_threshold_desc = "unknown statistical threshold"

    prompt = _build_prompt(
        plain_transition_summary=timer_state["plain_transition_summary"],
        anomaly_direction=timer_state["anomaly_direction"],
        direction_logic_desc=direction_logic_desc,
        timer_metric_name=timer_metric_name,
        anomaly_threshold_desc=anomaly_threshold_desc,
        dataset_rows=dataset_rows,
        feature_count=feature_count,
        anomaly_rate=anomaly_rate,
        selected_top5_text=selected_top5_text,
        allowed_feature_list_text=allowed_feature_list_text,
        selected_text=selected_text,
        selected_appendix_text=selected_appendix_text,
        feature_context_text=feature_context_text,
    )

    if use_llm:
        analysis_report = _generate_report_with_ollama(prompt, ollama_url, ollama_model)
        if analysis_report.strip():
            analysis_report = _enforce_executive_summary_ground_truth(
                analysis_report,
                timer_state["timer_change_summary"],
            )
    else:
        analysis_report = _fallback_report(selected_features, timer_state["anomaly_direction"], timer_state["timer_change_summary"])

    if not analysis_report:
        analysis_report = _fallback_report(selected_features, timer_state["anomaly_direction"], timer_state["timer_change_summary"])

    # Section 7 is now generated by first LLM call (mandatory in prompt)
    # No second LLM call needed
    top5_coverage_note = "(Section 7 generated by first LLM call)"

    return PipelineResult(
        timer_metric_name=timer_metric_name,
        timer_transition_label=timer_state["timer_transition_label"],
        timer_change_summary=timer_state["timer_change_summary"],
        anomaly_direction=timer_state["anomaly_direction"],
        selected_features=selected_features,
        selected_appendix=selected_appendix,
        analysis_report=analysis_report,
        top5_coverage_note=top5_coverage_note,
    )
