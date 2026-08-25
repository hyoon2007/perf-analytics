#!/usr/bin/env python3
"""Test / reprocess helper: run the v6 pipeline on one or more CSVs and SAVE
each generated report to a folder — WITHOUT sending email and WITHOUT moving
the input CSV.

Why this exists: the production runner (run_v6_report.py) only ever emails the
report and then moves the CSV; the report body itself is never written to disk.
That makes it hard to review, diff, or archive reports produced during
reprocessing or ad-hoc testing (e.g. a regression sweep). This utility calls
run_v6() directly — which builds the report but neither emails nor moves files —
and writes the report body to an output directory, one file per input.

Production is untouched: this script imports run_v6 but shares no state with the
runner or the FTP monitor, sends no email, and moves nothing.

Examples
--------
    # one file
    venv/bin/python scripts/save_test_reports.py processed/sample_data_8-25_122_waitingtime_iter5_win1_inter5.csv

    # a whole directory of CSVs into a chosen folder, also saving the HTML body
    venv/bin/python scripts/save_test_reports.py processed/ --out-dir /tmp/run_2026-08-25 --html

    # a shell glob (expanded by the shell) plus an index
    venv/bin/python scripts/save_test_reports.py processed/sample_data_8-*.csv --index
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from perf_analytics.config_utils import load_app_config
from perf_analytics_v6.pipeline import run_v6


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the v6 pipeline on CSV(s) and save the report(s) to a "
                    "folder. Never emails, never moves the input CSV.")
    p.add_argument("inputs", nargs="+",
                   help="CSV file(s) and/or directories. A directory expands to "
                        "its *.csv files. Shell globs work (the shell expands them).")
    p.add_argument("--out-dir", default=None,
                   help="Where to write reports (default: <base_data_dir>/test_reports).")
    p.add_argument("--config", default="/opt/perf-analytics/config/ops_pipeline.conf",
                   help="Path to ops config file.")
    p.add_argument("--sec-dir", default=None,
                   help="Secrets dir (default: <base_data_dir>/.sec).")
    p.add_argument("--html", action="store_true",
                   help="Also save the HTML email body (<stem>.html).")
    p.add_argument("--index", action="store_true",
                   help="Write an index.tsv summarising every report in --out-dir.")
    return p.parse_args()


def _sanitize(part: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(part))


def _collect_csvs(inputs: list[str]) -> list[Path]:
    """Expand the positional inputs into a de-duplicated, ordered CSV list.
    Each entry may be a CSV file or a directory (→ its *.csv, sorted)."""
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in inputs:
        pth = Path(raw)
        if pth.is_dir():
            found = sorted(pth.glob("*.csv"))
            if not found:
                print(f"[WARN] no *.csv in directory: {pth}")
            candidates = found
        elif pth.is_file():
            candidates = [pth]
        else:
            print(f"[WARN] not found, skipping: {pth}")
            candidates = []
        for c in candidates:
            rp = c.resolve()
            if rp not in seen:
                seen.add(rp)
                out.append(c)
    return out


def main() -> int:
    args = parse_args()
    cfg = load_app_config(args.config)
    sec_dir = Path(args.sec_dir) if args.sec_dir else (cfg.data_dir / ".sec")
    out_dir = Path(args.out_dir) if args.out_dir else (cfg.data_dir / "test_reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    csvs = _collect_csvs(args.inputs)
    if not csvs:
        print("[ERROR] no input CSVs resolved.")
        return 2

    print(f"[INFO] {len(csvs)} CSV(s) -> {out_dir}  (no email, no move)")
    rows: list[tuple[str, str, str, str, str]] = []  # stem, metric, verdict, severity, source|ERROR
    ok = err = 0

    for csv_path in csvs:
        stem = csv_path.stem
        try:
            result = run_v6(
                csv_path=csv_path,
                sec_dir=sec_dir,
                processed_dir=cfg.processed_dir,   # used for lookups only; run_v6 does NOT move the CSV
                subject_prefix="",
            )
        except Exception as exc:  # noqa: BLE001 — capture per-file, keep going
            err += 1
            errfile = out_dir / f"{_sanitize(stem)}__ERROR.txt"
            errfile.write_text(f"{exc}\n\n{traceback.format_exc()}", encoding="utf-8")
            rows.append((stem, "?", "ERROR", "-", str(exc)[:80]))
            print(f"  [FAIL] {stem}: {exc}  -> {errfile.name}")
            continue

        verdict = result.get("verdict_code", "?")
        severity = result.get("severity", "?")
        source = result.get("report_source", "?")
        metric = result.get("metric_name", "?")
        name = f"{_sanitize(stem)}__{_sanitize(verdict)}_{_sanitize(severity)}__{_sanitize(source)}"
        md_file = out_dir / f"{name}.md"
        md_file.write_text(result.get("report_md", ""), encoding="utf-8")
        if args.html:
            (out_dir / f"{name}.html").write_text(result.get("email_html", ""), encoding="utf-8")
        ok += 1
        rows.append((stem, metric, verdict, severity, source))
        print(f"  [OK]   {stem}: {verdict}/{severity} ({source}) -> {md_file.name}")

    if args.index:
        idx = out_dir / "index.tsv"
        with idx.open("w", encoding="utf-8") as fh:
            fh.write("input_stem\tmetric\tverdict\tseverity\tsource\n")
            for r in rows:
                fh.write("\t".join(r) + "\n")
        print(f"[INFO] index written: {idx}")

    print(f"[DONE] saved={ok} failed={err} out_dir={out_dir}")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
