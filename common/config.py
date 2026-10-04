"""Central configuration. Everything is env-driven with sane local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no external dep needed at import time)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppCredentials:
    username: str
    password: str


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    data_dir: Path
    runs_dir: Path
    workspace_dir: Path

    orchestrator_port: int
    vendor_portal_port: int
    ap_system_port: int

    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    anthropic_base_url: str | None
    anthropic_max_tokens: int

    vendor_portal_credentials: AppCredentials
    ap_system_credentials: AppCredentials

    fault_injection: bool

    agent_max_steps: int
    agent_max_tokens: int
    agent_timeout_seconds: float
    agent_max_repairs: int

    approval_amount_threshold: float
    approval_amount_currency: str

    log_level: str
    seed: int
    world_today: str

    policy_path: Path
    browser_headless: bool = True
    browser_timeout_ms: int = 15000
    #: How long a run waits for a human before it gives up on its own question.
    #: An approval that expires is denied, never assumed.
    question_timeout_seconds: float = 900.0

    @property
    def vendor_portal_url(self) -> str:
        return f"http://127.0.0.1:{self.vendor_portal_port}"

    @property
    def ap_system_url(self) -> str:
        return f"http://127.0.0.1:{self.ap_system_port}"

    @property
    def vendor_db_path(self) -> Path:
        return self.data_dir / "vendor_portal.db"

    @property
    def ap_db_path(self) -> Path:
        return self.data_dir / "ap_system.db"

    @property
    def runs_db_path(self) -> Path:
        return self.data_dir / "runs.db"


@dataclass
class BrowserPolicyConfig:
    """Loaded from config/policy.yaml."""

    raw: dict = field(default_factory=dict)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    runs_dir = Path(os.environ.get("RUNS_DIR", str(REPO_ROOT / "runs"))).resolve()
    workspace_dir = Path(os.environ.get("WORKSPACE_DIR", str(REPO_ROOT / "workspace"))).resolve()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None

    return Settings(
        repo_root=REPO_ROOT,
        data_dir=data_dir,
        runs_dir=runs_dir,
        workspace_dir=workspace_dir,
        orchestrator_port=_env_int("ORCHESTRATOR_PORT", 8000),
        vendor_portal_port=_env_int("VENDOR_PORTAL_PORT", 8001),
        ap_system_port=_env_int("AP_SYSTEM_PORT", 8002),
        llm_provider=os.environ.get("LLM_PROVIDER", "scripted").strip().lower(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5").strip(),
        anthropic_base_url=base_url,
        anthropic_max_tokens=_env_int("ANTHROPIC_MAX_TOKENS", 4096),
        vendor_portal_credentials=AppCredentials(
            username=os.environ.get("VENDOR_PORTAL_USER", "buyer@northwind.example"),
            password=os.environ.get("VENDOR_PORTAL_PASSWORD", "portal-demo-2026"),
        ),
        ap_system_credentials=AppCredentials(
            username=os.environ.get("AP_SYSTEM_USER", "ap@northwind.example"),
            password=os.environ.get("AP_SYSTEM_PASSWORD", "ap-demo-2026"),
        ),
        fault_injection=_env_bool("FAULT_INJECTION", False),
        agent_max_steps=_env_int("AGENT_MAX_STEPS", 120),
        agent_max_tokens=_env_int("AGENT_MAX_TOKENS", 400_000),
        agent_timeout_seconds=_env_float("AGENT_TIMEOUT_SECONDS", 600.0),
        agent_max_repairs=_env_int("AGENT_MAX_REPAIRS", 2),
        question_timeout_seconds=_env_float("QUESTION_TIMEOUT_SECONDS", 900.0),
        approval_amount_threshold=_env_float("APPROVAL_AMOUNT_THRESHOLD", 10_000.0),
        approval_amount_currency=os.environ.get("APPROVAL_AMOUNT_CURRENCY", "USD"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        seed=_env_int("SEED", 20260401),
        world_today=os.environ.get("WORLD_TODAY", "2026-04-01"),
        policy_path=REPO_ROOT / "config" / "policy.yaml",
        browser_headless=_env_bool("BROWSER_HEADLESS", True),
        browser_timeout_ms=_env_int("BROWSER_TIMEOUT_MS", 15000),
    )


@lru_cache(maxsize=1)
def load_policy() -> dict:
    settings = get_settings()
    if not settings.policy_path.exists():
        return {}
    return yaml.safe_load(settings.policy_path.read_text()) or {}