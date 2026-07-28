"""Shared LLM client for the OpenAI-compatible inference gateway.

Both the v1 (timer-weight/SHAP) and v6 pipelines call the same Casdoor-authed
gateway (`qwen3-14b-awq`) through this module, so credential handling, token
caching and body-field negotiation live in exactly one place.

The logic mirrors the known-good v6_9 notebook (Cells 1, 1b, 12):
  * OAuth2 password-grant token, cached until 60s before expiry
  * chat/completions with optional fields (temperature/top_p/max_tokens/...)
    dropped automatically when the gateway answers 400/422
  * optional local Ollama fallback when backend == "ollama"

No secrets are hardcoded; they are read from a KEY=value secrets file
(default: <project>/.sec/inference-gateway) and/or the environment.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import requests

# Optional body fields, ordered least -> most essential. Anything the gateway
# rejects is dropped and remembered for the process lifetime.
OPTIONAL_BODY_FIELDS = ["chat_template_kwargs", "response_format",
                        "max_tokens", "top_p", "temperature"]
CAPABILITY_ERROR_CODES = (400, 422)  # 422 = FastAPI/Pydantic body validation

REQUIRED_GATEWAY_KEYS = ["LLM_BASE_URL", "OAUTH_TOKEN_URL", "OAUTH_CLIENT_ID",
                         "OAUTH_CLIENT_SECRET", "OAUTH_USERNAME", "OAUTH_PASSWORD"]


def load_key_value_file(path) -> List[str]:
    """Load KEY=value lines from a secrets file into os.environ (values never
    printed). Existing environment values always win. Returns the keys loaded."""
    path = Path(path)
    if not path.is_file():
        return []
    loaded: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and not os.getenv(key):
            os.environ[key] = val
            loaded.append(key)
    return loaded


@dataclass
class GatewayConfig:
    backend: str = "gateway"
    base_url: str = ""
    model: str = "qwen3-14b-awq"
    token_url: str = ""
    scope: str = "openid"
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""
    temperature: float = 0.7
    top_p: float = 0.8
    max_tokens: int = 2048
    enable_thinking: bool = False
    disabled_fields: set = field(default_factory=set)
    # Ollama fallback (used only when backend == "ollama")
    ollama_url: str = "http://localhost:11434/api/generate"
    ollama_model: str = "qwen2.5:7b-instruct"

    @classmethod
    def load(cls, secrets_file: Optional[Path] = None) -> "GatewayConfig":
        """Build config from a secrets file + environment, mirroring the
        notebook's Cell 1 resolution order (env wins, then .sec file)."""
        if secrets_file is not None:
            load_key_value_file(secrets_file)

        backend = os.getenv("LLM_BACKEND", "gateway")
        model = os.getenv("LLM_MODEL", "qwen3-14b-awq")
        ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
        if backend == "ollama":
            model = os.getenv("LLM_MODEL", ollama_model)

        disabled = {f.strip() for f in os.getenv("LLM_DISABLED_FIELDS", "").split(",") if f.strip()}
        return cls(
            backend=backend,
            base_url=os.getenv("LLM_BASE_URL", ""),
            model=model,
            token_url=os.getenv("OAUTH_TOKEN_URL", ""),
            scope=os.getenv("OAUTH_SCOPE", "openid"),
            client_id=os.getenv("OAUTH_CLIENT_ID", ""),
            client_secret=os.getenv("OAUTH_CLIENT_SECRET", ""),
            username=os.getenv("OAUTH_USERNAME", ""),
            password=os.getenv("OAUTH_PASSWORD", ""),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            top_p=float(os.getenv("LLM_TOP_P", "0.8")),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2048")),
            enable_thinking=os.getenv("LLM_ENABLE_THINKING", "0") == "1",
            disabled_fields=disabled,
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate"),
            ollama_model=ollama_model,
        )

    def missing_gateway_keys(self) -> List[str]:
        if self.backend != "gateway":
            return []
        present = {
            "LLM_BASE_URL": self.base_url,
            "OAUTH_TOKEN_URL": self.token_url,
            "OAUTH_CLIENT_ID": self.client_id,
            "OAUTH_CLIENT_SECRET": self.client_secret,
            "OAUTH_USERNAME": self.username,
            "OAUTH_PASSWORD": self.password,
        }
        return [k for k, v in present.items() if not v]


class GatewayClient:
    """OAuth2 + chat/completions client. One instance holds the token cache."""

    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg
        self._token_cache = {"token": None, "expires_at": 0.0}

    # -- auth ---------------------------------------------------------------
    def _require_credentials(self) -> None:
        missing = [n for n, v in (("OAUTH_CLIENT_ID", self.cfg.client_id),
                                  ("OAUTH_CLIENT_SECRET", self.cfg.client_secret),
                                  ("OAUTH_USERNAME", self.cfg.username),
                                  ("OAUTH_PASSWORD", self.cfg.password)) if not v]
        if missing:
            raise RuntimeError(
                "Missing credentials in the environment: " + ", ".join(missing)
                + ". Provide them via the secrets file or environment before running.")

    def get_access_token(self, force_refresh: bool = False, timeout: int = 30) -> str:
        now = time.time()
        cache = self._token_cache
        if not force_refresh and cache["token"] and now < cache["expires_at"] - 60:
            return cache["token"]
        self._require_credentials()
        resp = requests.post(
            self.cfg.token_url, timeout=timeout,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "password",
                  "client_id": self.cfg.client_id,
                  "client_secret": self.cfg.client_secret,
                  "username": self.cfg.username,
                  "password": self.cfg.password,
                  "scope": self.cfg.scope})
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError(f"token endpoint returned no access_token (keys: {sorted(payload)[:6]})")
        cache["token"] = token
        cache["expires_at"] = now + float(payload.get("expires_in", 3600))
        return token

    def list_models(self, timeout: int = 60) -> List[str]:
        r = requests.get(f"{self.cfg.base_url}/models", timeout=timeout,
                         headers={"Authorization": f"Bearer {self.get_access_token()}"})
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]

    # -- chat ---------------------------------------------------------------
    @staticmethod
    def _rejected_fields(resp) -> set:
        fields: set = set()
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("detail"), list):
            for item in data["detail"]:
                loc = item.get("loc") if isinstance(item, dict) else None
                if isinstance(loc, list):
                    fields |= {p for p in loc if isinstance(p, str) and p != "body"}
        text = (resp.text or "").lower()
        fields |= {f for f in OPTIONAL_BODY_FIELDS if f in text}
        return fields

    def chat(self, prompt: str, timeout: int = 300, json_mode: bool = False,
             max_tokens: int | None = None) -> str:
        """Return the assistant message content. Refreshes the token on 401/403;
        drops optional body fields the gateway refuses (400/422). `max_tokens`
        overrides the configured default (e.g. long translations need more)."""
        cfg = self.cfg
        mt = max_tokens if max_tokens is not None else cfg.max_tokens
        if cfg.backend == "ollama":
            r = requests.post(cfg.ollama_url, timeout=timeout, json={
                "model": cfg.model, "prompt": prompt, "stream": False,
                "options": {"temperature": cfg.temperature, "top_p": cfg.top_p,
                            "num_predict": mt},
                **({"format": "json"} if json_mode else {})})
            r.raise_for_status()
            return r.json().get("response", "")

        url = f"{cfg.base_url}/chat/completions"
        payload = {"model": cfg.model,
                   "messages": [{"role": "user", "content": prompt}]}
        optional = {"temperature": cfg.temperature,
                    "top_p": cfg.top_p,
                    "max_tokens": mt,
                    "chat_template_kwargs": {"enable_thinking": cfg.enable_thinking}}
        if json_mode:
            optional["response_format"] = {"type": "json_object"}
        payload.update({k: v for k, v in optional.items() if k not in cfg.disabled_fields})

        def _post(body):
            headers = {"Authorization": f"Bearer {self.get_access_token()}",
                       "Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            if resp.status_code in (401, 403):
                headers["Authorization"] = f"Bearer {self.get_access_token(force_refresh=True)}"
                resp = requests.post(url, headers=headers, json=body, timeout=timeout)
            return resp

        r = _post(payload)
        for _ in range(len(OPTIONAL_BODY_FIELDS)):
            if r.status_code not in CAPABILITY_ERROR_CODES:
                break
            bad = self._rejected_fields(r) & set(payload) & set(OPTIONAL_BODY_FIELDS)
            if not bad:
                remaining = [f for f in OPTIONAL_BODY_FIELDS if f in payload]
                if not remaining:
                    break
                bad = {remaining[0]}
            for f in bad:
                payload.pop(f, None)
                cfg.disabled_fields.add(f)
            print(f"[llm] gateway rejected {sorted(bad)} (HTTP {r.status_code}); retrying without them")
            r = _post(payload)

        if r.status_code >= 400:
            raise RuntimeError(f"gateway HTTP {r.status_code} for {url} "
                               f"(body keys: {sorted(payload)}): {r.text[:400]}")
        return r.json()["choices"][0]["message"]["content"]
