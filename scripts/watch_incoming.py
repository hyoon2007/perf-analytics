#!/usr/bin/env python3
"""FTP-polling monitor (parallel v6 + v1 comparison mode).

Polls the remote FTP server every --interval seconds. When a new .csv appears:
  1. the MONITOR downloads it ONCE into incoming/ (so both pipelines read the
     exact same file — an apples-to-apples comparison);
  2. it runs the v6 runner FIRST (the pipeline we are moving to), then the v1
     runner, each as a SEPARATE subprocess with --no-move;
  3. it moves the file to processed/ if v6 succeeded, else to failed/.

Crash isolation: each runner is its own process, so if v1 dies in SHAP
(OOM/segfault on large data) the monitor and the already-finished v6 run are
unaffected. v1 is best-effort — its exit code never blocks the file lifecycle.
Running the two sequentially (not concurrently) also avoids a combined
memory spike taking down the v6 run.

incoming/ is only a transient staging area here; the monitor always moves the
file out to processed/ or failed/ once both runners return.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from ftplib import FTP, all_errors as FTP_ERRORS
from pathlib import Path

from perf_analytics.config_utils import load_app_config, AppConfig
from perf_analytics.pipeline import fetch_csv_from_ftp


VALID_EXTS = (".csv",)
SCRIPT_DIR = Path(__file__).resolve().parent
V6_RUNNER = SCRIPT_DIR / "run_v6_report.py"
V1_RUNNER = SCRIPT_DIR / "run_timerweight_report.py"


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


def _run_runner(runner_script: Path, filename: str, cfg_path: str, label: str,
                extra_args: list[str]) -> int:
    """Run one pipeline runner against the staged local file. Never raises;
    returns the subprocess exit code (or 1 on launch failure)."""
    cmd = [
        sys.executable, str(runner_script),
        "--input-file", filename,
        "--source", "local",
        "--config", cfg_path,
        "--no-move",
    ] + extra_args
    print(f"[MONITOR] -> {label}: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, check=False)
        print(f"[MONITOR] <- {label} exit={result.returncode} for {filename}")
        return result.returncode
    except Exception as exc:  # never let one runner take down the monitor
        print(f"[MONITOR] {label} launch failed for {filename}: {exc}")
        return 1


def _process_new_file(filename: str, cfg: AppConfig, cfg_path: str,
                      extra_args: list[str]) -> None:
    print(f"[MONITOR] New FTP file detected: {filename}")

    # 1) download ONCE into incoming/
    local_target = cfg.incoming_dir / Path(filename).name
    try:
        fetch_csv_from_ftp(
            filename=Path(filename).name,
            ftp_host=cfg.ftp_host,
            ftp_user=cfg.ftp_user,
            ftp_password=cfg.ftp_password,
            ftp_remote_dir=cfg.ftp_remote_dir,
            local_target=local_target,
        )
    except Exception as exc:
        print(f"[MONITOR] Download failed for {filename}: {exc}")
        return

    name = local_target.name

    # 2) v6 first (the pipeline we are moving to), then v1 (best-effort)
    v6_rc = _run_runner(V6_RUNNER, name, cfg_path, "v6", extra_args)
    v1_rc = _run_runner(V1_RUNNER, name, cfg_path, "v1", extra_args)
    if v1_rc != 0:
        print(f"[MONITOR] v1 returned {v1_rc} (best-effort; not blocking) for {name}")

    # 3) file lifecycle keyed on v6 outcome
    dest_dir = cfg.processed_dir if v6_rc == 0 else cfg.failed_dir
    target = dest_dir / name
    if local_target.exists() and local_target.resolve() != target.resolve():
        shutil.move(str(local_target), str(target))
    print(f"[MONITOR] Moved {name} to {'processed' if v6_rc == 0 else 'failed'}: {target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll FTP every --interval seconds; run v6 + v1 on each new CSV."
    )
    parser.add_argument("--config", default="/opt/perf-analytics/config/ops_pipeline.conf")
    parser.add_argument("--interval", type=int, default=300, help="FTP poll interval in seconds (default: 300).")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-email", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_app_config(args.config)

    extra_args: list[str] = []
    if args.skip_llm:
        extra_args.append("--skip-llm")
    if args.skip_email:
        extra_args.append("--skip-email")

    print(f"[MONITOR] FTP host  : {cfg.ftp_host}/{cfg.ftp_remote_dir}")
    print(f"[MONITOR] Poll every: {args.interval}s")
    print(f"[MONITOR] Runners   : v6={V6_RUNNER.name}, v1={V1_RUNNER.name} (v6 first, v1 best-effort)")

    # Seed initial snapshot so we don't re-process files already on the server.
    seen: set[str] = _list_ftp_files(cfg)
    print(f"[MONITOR] Baseline snapshot: {len(seen)} file(s) on FTP (will not reprocess).")

    while True:
        time.sleep(args.interval)
        current = _list_ftp_files(cfg)
        new_files = current - seen
        for filename in sorted(new_files):
            _process_new_file(filename, cfg, args.config, extra_args)
            seen.add(filename)


if __name__ == "__main__":
    raise SystemExit(main())
