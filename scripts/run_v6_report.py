#!/usr/bin/env python3
"""Runner for the v6 anomaly-report pipeline.

Mirrors run_timerweight_report.py (the v1 runner) so the FTP monitor can invoke
both against the SAME downloaded CSV during the parallel-comparison period.

File lifecycle: by default the runner moves the CSV to processed/ (or failed/)
when run standalone. Pass --no-move so the caller (the monitor) owns the move
after BOTH pipelines have run — this keeps v1 and v6 reading the same file.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from perf_analytics.config_utils import load_app_config, load_email_credentials, resolve_input_path
from perf_analytics.emailer import send_email
from perf_analytics.pipeline import fetch_csv_from_ftp
from perf_analytics_v6.pipeline import run_v6, localize_email


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v6 anomaly report pipeline.")
    parser.add_argument("--input-file", required=True, help="Input CSV file name or path.")
    parser.add_argument("--source", choices=["local", "ftp"], default="local", help="Input source type.")
    parser.add_argument("--config", default="/opt/perf-analytics/config/ops_pipeline.conf", help="Path to ops config file.")
    parser.add_argument("--sec-dir", default=None, help="Secrets dir (default: <base_data_dir>/.sec).")
    parser.add_argument("--subject-prefix", default="[v6] ", help="Email subject prefix tag.")
    parser.add_argument("--skip-email", action="store_true", help="Skip SES email send.")
    parser.add_argument("--no-move", action="store_true", help="Do not move the CSV after processing (caller owns it).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_app_config(args.config)
    sec_dir = Path(args.sec_dir) if args.sec_dir else (cfg.data_dir / ".sec")

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

    print(f"[INFO][v6] Processing file: {csv_path}")

    result = None
    pipeline_error = None
    for attempt in range(1, cfg.pipeline_max_retries + 1):
        try:
            result = run_v6(
                csv_path=csv_path,
                sec_dir=sec_dir,
                processed_dir=cfg.processed_dir,
                subject_prefix=args.subject_prefix,
            )
            break
        except Exception as exc:
            pipeline_error = exc
            if attempt < cfg.pipeline_max_retries:
                wait_s = cfg.pipeline_backoff_seconds * attempt
                print(f"[WARN][v6] Pipeline attempt {attempt} failed: {exc}")
                print(f"[INFO][v6] Retrying in {wait_s}s...")
                time.sleep(wait_s)

    if result is None:
        if not args.no_move:
            target = cfg.failed_dir / csv_path.name
            if csv_path.exists():
                shutil.move(str(csv_path), str(target))
                print(f"[INFO][v6] Moved file to failed: {target}")
        print(f"[ERROR][v6] Pipeline failed after retries: {pipeline_error}")
        return 1

    print(f"[INFO][v6] verdict={result['verdict_code']} severity={result['severity']} "
          f"source={result['report_source']}")

    if not args.skip_email:
        email_cfg = load_email_credentials(cfg)
        en_to = email_cfg.get("ses_to_email_en", "").strip()
        ko_to = email_cfg.get("ses_to_email_ko", "").strip()
        en_subj, en_plain, en_html = (
            result["email_subject"], result["email_plain"], result["email_html"])

        def _send(to_email, subject, text_body, html_body, tag):
            err = None
            for attempt in range(1, cfg.email_max_retries + 1):
                try:
                    mid = send_email(
                        aws_access_key_id=email_cfg["aws_access_key_id"],
                        aws_secret_access_key=email_cfg["aws_secret_access_key"],
                        ses_region=email_cfg["ses_region"],
                        from_email=email_cfg["ses_from_email"],
                        to_email=to_email,
                        subject=subject,
                        text_body=text_body,
                        html_body=html_body,
                    )
                    print(f"[INFO][v6] {tag} email sent. MessageId: {mid}")
                    return mid
                except Exception as exc:
                    err = exc
                    if attempt < cfg.email_max_retries:
                        wait_s = cfg.email_backoff_seconds * attempt
                        print(f"[WARN][v6] {tag} email attempt {attempt} failed: {exc}")
                        print(f"[INFO][v6] Retrying {tag} email in {wait_s}s...")
                        time.sleep(wait_s)
            print(f"[ERROR][v6] {tag} email failed after retries: {err}")
            return None

        # English recipients
        if en_to:
            _send(en_to, en_subj, en_plain, en_html, "EN")

        # Korean recipients: translate the validated English email; on any
        # failure fall back to sending the English version (never drop the send).
        if ko_to:
            loc = None
            try:
                loc = localize_email("ko", en_subj, en_plain)
            except Exception as exc:
                print(f"[WARN][v6] KO localization error: {exc}")
            if loc is None:
                print("[INFO][v6] KO localization unavailable; sending English to KO recipients")
                _send(ko_to, en_subj, en_plain, en_html, "KO(fallback-EN)")
            else:
                ko_subj, ko_plain, ko_html = loc
                _send(ko_to, ko_subj, ko_plain, ko_html, "KO")

    if not args.no_move:
        target = cfg.processed_dir / csv_path.name
        if csv_path.exists() and csv_path.resolve() != target.resolve():
            shutil.move(str(csv_path), str(target))
        print(f"[INFO][v6] Completed. Processed file moved to: {target}")
    else:
        print("[INFO][v6] Completed (file left in place; caller owns move).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
