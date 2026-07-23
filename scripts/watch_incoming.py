#!/usr/bin/env python3
"""FTP-polling monitor.

Polls the remote FTP server every --interval seconds.
When a new .csv file is detected, calls the pipeline runner with --source ftp
so the runner downloads the file itself.

Downloaded files are staged in incoming/, processed to processed/,
or moved to failed/ on error. incoming/ acts as an audit trail;
set keep_incoming=false in [ftp] config to delete after processing instead.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from ftplib import FTP, all_errors as FTP_ERRORS
from pathlib import Path

from perf_analytics.config_utils import load_app_config, AppConfig


VALID_EXTS = (".csv",)


def _list_ftp_files(cfg: AppConfig) -> set[str]:
    """Return the set of CSV filenames currently on the FTP server."""
    try:
        with FTP(cfg.ftp_host) as ftp:
            ftp.login(user=cfg.ftp_user, passwd=cfg.ftp_password)
            if cfg.ftp_remote_dir:
                ftp.cwd(cfg.ftp_remote_dir)
            names = ftp.nlst()
        return {n for n in names if Path(n).suffix.lower() in VALID_EXTS}
    except FTP_ERRORS as exc:
        print(f"[MONITOR] FTP list error: {exc}")
        return set()


def _run_pipeline(
    filename: str,
    runner_script: Path,
    cfg_path: str,
    max_retries: int,
    backoff_seconds: int,
    extra_args: list[str],
) -> None:
    cmd = [
        sys.executable,
        str(runner_script),
        "--input-file", filename,
        "--source", "ftp",
        "--config", cfg_path,
    ] + extra_args

    print(f"[MONITOR] New FTP file detected: {filename}")
    for attempt in range(1, max_retries + 1):
        result = subprocess.run(cmd, check=False)
        print(f"[MONITOR] Attempt {attempt}/{max_retries} exit={result.returncode} for {filename}")
        if result.returncode == 0:
            return
        if attempt < max_retries:
            wait_s = backoff_seconds * attempt
            print(f"[MONITOR] Retry in {wait_s}s...")
            time.sleep(wait_s)
    print(f"[MONITOR] All retries exhausted for {filename}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll FTP server every --interval seconds and run pipeline on new CSV files."
    )
    parser.add_argument("--config", default="/opt/perf-analytics/config/ops_pipeline.conf")
    parser.add_argument("--interval", type=int, default=300, help="FTP poll interval in seconds (default: 300).")
    parser.add_argument("--runner-max-retries", type=int, default=3)
    parser.add_argument("--runner-backoff-seconds", type=int, default=30)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_app_config(args.config)
    runner_script = Path(__file__).resolve().parent / "run_timerweight_report.py"

    extra_args: list[str] = []
    if args.skip_llm:
        extra_args.append("--skip-llm")
    if args.skip_email:
        extra_args.append("--skip-email")

    print(f"[MONITOR] FTP host  : {cfg.ftp_host}/{cfg.ftp_remote_dir}")
    print(f"[MONITOR] Poll every: {args.interval}s")

    # Seed initial snapshot so we don't re-process files already on server.
    seen: set[str] = _list_ftp_files(cfg)
    print(f"[MONITOR] Baseline snapshot: {len(seen)} file(s) on FTP (will not reprocess).")

    while True:
        time.sleep(args.interval)
        current = _list_ftp_files(cfg)
        new_files = current - seen
        for filename in sorted(new_files):
            _run_pipeline(
                filename=filename,
                runner_script=runner_script,
                cfg_path=args.config,
                max_retries=args.runner_max_retries,
                backoff_seconds=args.runner_backoff_seconds,
                extra_args=extra_args,
            )
            seen.add(filename)


if __name__ == "__main__":
    raise SystemExit(main())
