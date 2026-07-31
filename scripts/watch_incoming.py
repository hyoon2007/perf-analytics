#!/usr/bin/env python3
"""FTP-polling monitor (parallel v6 + v1 comparison mode).

Polls the remote FTP server every --interval seconds. When a new .csv appears:
  1. the MONITOR downloads it ONCE into incoming/ (so both pipelines read the
     exact same file — an apples-to-apples comparison);
  2. it runs the v6 runner FIRST (the pipeline we are moving to), then the v1
     runner, each as a SEPARATE subprocess with --no-move;
  3. it moves the file to processed/ if v6 succeeded, else to failed/.

Durability (v6.9.3):
  * The set of handled files is persisted to a state file, so a restart does
    NOT re-baseline the whole FTP listing. Files that arrived while the monitor
    was down are picked up on the next poll instead of being silently ignored.
  * A file is marked handled right after v6 runs, so a later crash (e.g. v1
    OOM taking the process down) never causes v6 to re-send its report.
  * On startup, any file left in incoming/ that is already marked handled is
    swept to processed/ (its move was interrupted).
"""
from __future__ import annotations

import argparse
import os
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

# v1 (legacy XGBoost/SHAP pipeline) is DISABLED — v6 is now the sole production
# pipeline. The parallel-comparison period is over. Set V1_ENABLED=1 in the
# environment to re-enable the best-effort v1 run for a future comparison.
V1_ENABLED = os.getenv("V1_ENABLED", "0") == "1"


def _state_path(cfg: AppConfig) -> Path:
    return cfg.data_dir / ".state" / "seen_ftp.txt"


def _load_seen(path: Path):
    """Return the persisted handled-file set, or None if there is no state yet."""
    if not path.exists():
        return None
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _persist_seen(path: Path, seen: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic


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
                      extra_args: list[str], seen: set[str], state_path: Path) -> None:
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
        # transient — do NOT mark handled, so it is retried on the next poll
        print(f"[MONITOR] Download failed for {filename}: {exc}")
        return

    name = local_target.name

    # 2) v6 first (the pipeline we are moving to)
    v6_rc = _run_runner(V6_RUNNER, name, cfg_path, "v6", extra_args)

    # Mark handled as soon as v6 has run: if the process dies during v1 (e.g. an
    # OOM), a restart must not re-send v6's report for this file.
    seen.add(filename)
    _persist_seen(state_path, seen)

    # 3) v1 (DISABLED by default; best-effort — its outcome never blocks the
    #    file lifecycle). Re-enable with V1_ENABLED=1 for a comparison run.
    if V1_ENABLED:
        v1_rc = _run_runner(V1_RUNNER, name, cfg_path, "v1", extra_args)
        if v1_rc != 0:
            print(f"[MONITOR] v1 returned {v1_rc} (best-effort; not blocking) for {name}")

    # 4) file lifecycle keyed on v6 outcome
    dest_dir = cfg.processed_dir if v6_rc == 0 else cfg.failed_dir
    target = dest_dir / name
    if local_target.exists() and local_target.resolve() != target.resolve():
        shutil.move(str(local_target), str(target))
    print(f"[MONITOR] Moved {name} to {'processed' if v6_rc == 0 else 'failed'}: {target}")


def _sweep_incoming_orphans(cfg: AppConfig, seen: set[str]) -> None:
    """A file left in incoming/ that is already marked handled means its move was
    interrupted (v6 had run). Move it to processed/ so it does not linger."""
    for f in cfg.incoming_dir.glob("*.csv"):
        if f.name in seen:
            target = cfg.processed_dir / f.name
            if f.resolve() != target.resolve():
                shutil.move(str(f), str(target))
            print(f"[MONITOR] Swept orphaned incoming file to processed: {f.name}")


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
    state_path = _state_path(cfg)

    extra_args: list[str] = []
    if args.skip_llm:
        extra_args.append("--skip-llm")
    if args.skip_email:
        extra_args.append("--skip-email")

    print(f"[MONITOR] FTP host  : {cfg.ftp_host}/{cfg.ftp_remote_dir}")
    print(f"[MONITOR] Poll every: {args.interval}s")
    _v1_state = f"v1={V1_RUNNER.name} best-effort" if V1_ENABLED else "v1 DISABLED"
    print(f"[MONITOR] Runners   : v6={V6_RUNNER.name} ({_v1_state})")
    print(f"[MONITOR] State file: {state_path}")

    seen = _load_seen(state_path)
    if seen is None:
        # First ever run: baseline the current FTP listing so we do not reprocess
        # history. Every later restart loads the persisted set instead.
        seen = _list_ftp_files(cfg)
        _persist_seen(state_path, seen)
        print(f"[MONITOR] No state file — baselined {len(seen)} file(s) on FTP (will not reprocess).")
    else:
        print(f"[MONITOR] Loaded {len(seen)} handled file(s) from state.")

    # Recover anything interrupted by a previous crash/restart.
    _sweep_incoming_orphans(cfg, seen)

    first = True
    while True:
        if not first:
            time.sleep(args.interval)
        first = False
        current = _list_ftp_files(cfg)
        # New = on FTP but not yet handled. This catches files that arrived while
        # the monitor was down (the old baseline-on-restart logic swallowed them).
        new_files = current - seen
        if new_files:
            print(f"[MONITOR] {len(new_files)} unhandled file(s) to process.")
        for filename in sorted(new_files):
            _process_new_file(filename, cfg, args.config, extra_args, seen, state_path)


if __name__ == "__main__":
    raise SystemExit(main())
