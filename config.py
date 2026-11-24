"""設定：全部由環境變數（或 .env）讀取，CLI 參數可覆寫部分值。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _env_list(name: str) -> list[str]:
    value = _env(name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _env_json(name: str) -> dict:
    value = _env(name)
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必須是 JSON 物件，例如 {{\"Authorization\": \"Bearer xxx\"}}")
    return {str(k): str(v) for k, v in parsed.items()}


@dataclass
class Settings:
    # --- 內部 LLM（OpenAI 相容 API，需支援 tool calling）---
    llm_base_url: str | None = None
    llm_api_key: str = "not-needed"
    llm_model: str = "gpt-4o"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 2

    # --- Prometheus MCP server ---
    prom_mcp_url: str | None = None
    prom_mcp_transport: str = "streamable_http"  # streamable_http | sse | stdio
    prom_mcp_headers: dict = field(default_factory=dict)
    prom_mcp_command: str | None = None  # stdio 用：啟動 MCP server 的指令
    prom_mcp_args: list[str] = field(default_factory=list)
    prom_mcp_env: dict = field(default_factory=dict)
    prom_mcp_tools: list[str] = field(default_factory=list)  # 白名單；空 = 全部載入

    # --- Kubernetes ---
    kubeconfig: str | None = None  # 空 = 預設 ~/.kube/config；在叢集內會自動用 in-cluster 設定
    kube_context: str | None = None
    k8s_timeout_s: int = 15

    # --- 防護與預算 ---
    prom_budget_per_task: int = 10  # 每次派工（一個 worker 呼叫）最多幾次 Prometheus MCP 呼叫
    prom_budget_total: int = 80  # 整次任務最多幾次 Prometheus MCP 呼叫
    max_parallel_workers: int = 3  # 同時執行的 worker 數上限
    gate_max_retries: int = 3  # 結束把關：未 proven 的子目標最多再推幾次
    timeout_s: int = 900  # 整次任務逾時（秒）
    recursion_limit: int = 200  # LangGraph 步數上限

    # --- 輸出 ---
    output_dir: str = "reports"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            llm_base_url=_env("LLM_BASE_URL"),
            llm_api_key=_env("LLM_API_KEY", "not-needed") or "not-needed",
            llm_model=_env("LLM_MODEL", "gpt-4o") or "gpt-4o",
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
            llm_timeout_s=_env_float("LLM_TIMEOUT_S", 120.0),
            llm_max_retries=_env_int("LLM_MAX_RETRIES", 2),
            prom_mcp_url=_env("PROM_MCP_URL"),
            prom_mcp_transport=_env("PROM_MCP_TRANSPORT", "streamable_http") or "streamable_http",
            prom_mcp_headers=_env_json("PROM_MCP_HEADERS"),
            prom_mcp_command=_env("PROM_MCP_COMMAND"),
            prom_mcp_args=_env_list("PROM_MCP_ARGS"),
            prom_mcp_env=_env_json("PROM_MCP_ENV"),
            prom_mcp_tools=_env_list("PROM_MCP_TOOLS"),
            kubeconfig=_env("KUBECONFIG"),
            kube_context=_env("KUBE_CONTEXT"),
            k8s_timeout_s=_env_int("K8S_TIMEOUT_S", 15),
            prom_budget_per_task=_env_int("PROM_BUDGET_PER_TASK", 10),
            prom_budget_total=_env_int("PROM_BUDGET_TOTAL", 80),
            max_parallel_workers=_env_int("MAX_PARALLEL_WORKERS", 3),
            gate_max_retries=_env_int("GATE_MAX_RETRIES", 3),
            timeout_s=_env_int("TASK_TIMEOUT_S", 900),
            recursion_limit=_env_int("RECURSION_LIMIT", 200),
            output_dir=_env("OUTPUT_DIR", "reports") or "reports",
        )
