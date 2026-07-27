from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict


@dataclass
class AppConfig:
    data_dir: Path
    incoming_dir: Path
    processed_dir: Path
    failed_dir: Path
    ftp_host: str
    ftp_user: str
    ftp_password: str
    ftp_password_env: str
    ftp_password_file: Path
    ftp_remote_dir: str
    ollama_url: str
    ollama_model: str
    min_onehot_on_ratio: float
    pipeline_max_retries: int
    pipeline_backoff_seconds: int
    email_max_retries: int
    email_backoff_seconds: int
    aws_secret_file: Path
    ses_conf_file: Path


def _load_kv_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_app_config(conf_path: str) -> AppConfig:
    parser = configparser.ConfigParser()
    read_ok = parser.read(conf_path, encoding="utf-8")
    if not read_ok:
        raise FileNotFoundError(f"Config file not found: {conf_path}")

    base_data = Path(parser.get("paths", "base_data_dir", fallback="/opt/perf-analytics"))

    ftp_password_env = parser.get("ftp", "password_env", fallback="FTP_PASSWORD")
    ftp_password_file = Path(parser.get("ftp", "password_file", fallback="/opt/perf-analytics/.sec/ftp_password"))
    ftp_password = _resolve_ftp_password(
        parser.get("ftp", "password", fallback=""),
        ftp_password_env,
        ftp_password_file,
    )

    cfg = AppConfig(
        data_dir=base_data,
        incoming_dir=Path(parser.get("paths", "incoming_dir", fallback=str(base_data / "incoming"))),
        processed_dir=Path(parser.get("paths", "processed_dir", fallback=str(base_data / "processed"))),
        failed_dir=Path(parser.get("paths", "failed_dir", fallback=str(base_data / "failed"))),
        ftp_host=parser.get("ftp", "host", fallback=""),
        ftp_user=parser.get("ftp", "user", fallback=""),
        ftp_password=ftp_password,
        ftp_password_env=ftp_password_env,
        ftp_password_file=ftp_password_file,
        ftp_remote_dir=parser.get("ftp", "remote_dir", fallback=""),
        ollama_url=parser.get("llm", "url", fallback="http://localhost:11434/api/generate"),
        ollama_model=parser.get("llm", "model", fallback="qwen2.5:7b-instruct"),
        min_onehot_on_ratio=float(parser.get("model", "min_onehot_on_ratio", fallback="0.01")),
        pipeline_max_retries=int(parser.get("retry", "pipeline_max_retries", fallback="3")),
        pipeline_backoff_seconds=int(parser.get("retry", "pipeline_backoff_seconds", fallback="30")),
        email_max_retries=int(parser.get("retry", "email_max_retries", fallback="2")),
        email_backoff_seconds=int(parser.get("retry", "email_backoff_seconds", fallback="15")),
        aws_secret_file=Path(parser.get("email", "aws_secret_file", fallback="/opt/perf-analytics/.sec/aws-ses")),
        ses_conf_file=Path(parser.get("email", "ses_conf_file", fallback="/opt/perf-analytics/config/ses_email.conf")),
    )

    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    cfg.processed_dir.mkdir(parents=True, exist_ok=True)
    cfg.failed_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def load_email_credentials(app_cfg: AppConfig) -> Dict[str, str]:
    secret = _load_kv_file(app_cfg.aws_secret_file)
    ses = _load_kv_file(app_cfg.ses_conf_file)

    def _split(s: str):
        return [e.strip() for e in s.split(",") if e.strip()]

    default_to = ses.get("SES_TO_EMAIL", "").strip()
    to_en = ses.get("SES_TO_EMAIL_EN", "").strip()
    to_ko = ses.get("SES_TO_EMAIL_KO", "").strip()

    # v6 per-language routing:
    #   - SES_TO_EMAIL_KO = recipients who want the Korean report
    #   - SES_TO_EMAIL_EN = explicit English list (optional)
    #   - SES_TO_EMAIL    = full/default list (used for v1 and as the English base)
    # If only KO is set, everyone else in SES_TO_EMAIL automatically stays English
    # (no recipient is silently dropped). If neither is set, everyone gets English.
    ko_list = _split(to_ko)
    if to_en:
        en_list = _split(to_en)
    elif ko_list:
        ko_set = set(ko_list)
        en_list = [e for e in _split(default_to) if e not in ko_set]
    else:
        en_list = _split(default_to)

    return {
        "aws_access_key_id": secret.get("AWS_ACCESS_KEY_ID", ""),
        "aws_secret_access_key": secret.get("AWS_SECRET_ACCESS_KEY", ""),
        "ses_region": ses.get("SES_REGION", "ap-northeast-1"),
        "ses_from_email": ses.get("SES_FROM_EMAIL", ""),
        "ses_to_email": default_to,               # legacy / v1 path (full list)
        "ses_to_email_en": ",".join(en_list),     # v6 English recipients
        "ses_to_email_ko": ",".join(ko_list),     # v6 Korean recipients
    }


def _resolve_ftp_password(config_password: str, env_key: str, secret_file: Path) -> str:
    if config_password:
        return config_password

    env_value = os.getenv(env_key, "")
    if env_value:
        return env_value

    if secret_file.exists():
        content = secret_file.read_text(encoding="utf-8").strip()
        if content:
            return content

    return ""


def resolve_input_path(input_file: str, incoming_dir: Path) -> Path:
    in_path = Path(input_file)
    if in_path.is_absolute():
        return in_path
    if in_path.exists():
        return in_path.resolve()
    return (incoming_dir / input_file).resolve()
