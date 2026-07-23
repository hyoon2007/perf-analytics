#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from perf_analytics.config_utils import load_app_config, load_email_credentials, resolve_input_path
from perf_analytics.emailer import send_email
from perf_analytics.pipeline import fetch_csv_from_ftp, md_to_simple_html, run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run timer-weight anomaly report pipeline.")
    parser.add_argument("--input-file", required=True, help="Input CSV file name or path.")
    parser.add_argument("--source", choices=["local", "ftp"], default="local", help="Input source type.")
    parser.add_argument("--config", default="/opt/perf-analytics/config/ops_pipeline.conf", help="Path to ops config file.")
    parser.add_argument("--skip-llm", action="store_true", help="Skip Ollama report generation.")
    parser.add_argument("--skip-email", action="store_true", help="Skip SES email send.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_app_config(args.config)

    if args.source == "ftp":
        local_csv = cfg.incoming_dir / Path(args.input_file).name
        csv_path = fetch_csv_from_ftp(
            filename=Path(args.input_file).name,
            ftp_host=cfg.ftp_host,
            ftp_user=cfg.ftp_user,
            ftp_password=cfg.ftp_password,
            ftp_remote_dir=cfg.ftp_remote_dir,
            local_target=local_csv,
        )
    else:
        csv_path = resolve_input_path(args.input_file, cfg.incoming_dir)

    if not csv_path.exists():
        print(f"[ERROR] Input file not found: {csv_path}")
        return 2

    print(f"[INFO] Processing file: {csv_path}")

    result = None
    pipeline_error = None
    for attempt in range(1, cfg.pipeline_max_retries + 1):
        try:
            result = run_pipeline(
                csv_path=csv_path,
                ollama_url=cfg.ollama_url,
                ollama_model=cfg.ollama_model,
                min_onehot_on_ratio=cfg.min_onehot_on_ratio,
                use_llm=not args.skip_llm,
            )
            break
        except Exception as exc:
            pipeline_error = exc
            if attempt < cfg.pipeline_max_retries:
                wait_s = cfg.pipeline_backoff_seconds * attempt
                print(f"[WARN] Pipeline attempt {attempt} failed: {exc}")
                print(f"[INFO] Retrying in {wait_s}s...")
                time.sleep(wait_s)

    if result is None:
        target = cfg.failed_dir / csv_path.name
        if csv_path.exists():
            shutil.move(str(csv_path), str(target))
        print(f"[ERROR] Pipeline failed after retries: {pipeline_error}")
        print(f"[INFO] Moved file to failed: {target}")
        return 1

    print("[INFO] Top direction-matched features")
    print(result.selected_features[["feature", "importance", "effect_for_ranking", "impact_type"]].head(10).to_string(index=False))

    if not args.skip_email:
        email_cfg = load_email_credentials(cfg)
        subject = f"[Alert Report] {result.timer_metric_name} | {result.timer_transition_label}"
        body_text = "\n".join(
            [
                "## Timer Change Summary",
                "",
                result.timer_change_summary,
                "",
                "## AI Analysis",
                "",
                result.analysis_report,
                "",
                "### Selected Top5 Exclusion Reason",
                "",
                result.top5_coverage_note,
            ]
        )
        body_html = md_to_simple_html(body_text)
        email_error = None
        message_id = None
        for attempt in range(1, cfg.email_max_retries + 1):
            try:
                message_id = send_email(
                    aws_access_key_id=email_cfg["aws_access_key_id"],
                    aws_secret_access_key=email_cfg["aws_secret_access_key"],
                    ses_region=email_cfg["ses_region"],
                    from_email=email_cfg["ses_from_email"],
                    to_email=email_cfg["ses_to_email"],
                    subject=subject,
                    text_body=body_text,
                    html_body=body_html,
                )
                break
            except Exception as exc:
                email_error = exc
                if attempt < cfg.email_max_retries:
                    wait_s = cfg.email_backoff_seconds * attempt
                    print(f"[WARN] Email attempt {attempt} failed: {exc}")
                    print(f"[INFO] Retrying email in {wait_s}s...")
                    time.sleep(wait_s)

        if message_id:
            print(f"[INFO] Email sent. MessageId: {message_id}")
        else:
            print(f"[ERROR] Email failed after retries: {email_error}")

    target = cfg.processed_dir / csv_path.name
    if csv_path.exists() and csv_path.resolve() != target.resolve():
        shutil.move(str(csv_path), str(target))
    print(f"[INFO] Completed. Processed file moved to: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
