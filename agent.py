"""組裝 deepagents：主 agent（planner）+ sre-worker subagent，並執行一次完整調查。

流程：goal 拆解 → 主 agent 派工 → worker 記錄 fact / 異常 → prove_goal → 結束把關 → 結論摘要 → Insight 報告。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver

from config import Settings
from goals import GoalPlan, decompose_goal, load_prompt, plan_to_goals
from middleware import TaskGateMiddleware, ToolAuditMiddleware, message_text
from report import InsightSummary, RunStats, render_report
from store import Store
from tools.facts import make_planner_tools, make_worker_fact_tools
from tools.k8s import K8S_TOOL_NAMES

WORKER_NAME = "sre-worker"
WORKER_DESCRIPTION = (
    "SRE 調查員：用 K8s 唯讀工具與 Prometheus MCP 查證一個具體問題（一次只負責一個 deployment），"
    "把觀察記成 fact、異常回報成 anomaly。task 描述開頭寫 intent id（先用 add_intent 建立），"
    "再寫子目標 id、namespace、deployment、時間窗與要驗證的假設。"
)


def register_profiles(model: BaseChatModel) -> None:
    """不給 execute（shell），也不自動加 general-purpose subagent，讓主 agent 只能派 sre-worker。

    harness profile 依模型的 provider 套用（ChatOpenAI 是 "openai"）。
    """
    try:
        from deepagents._models import get_model_provider  # 內部 API，版本已鎖定

        provider_keys = {get_model_provider(model) or "openai", "openai"}
    except Exception:
        provider_keys = {"openai"}
    profile = HarnessProfile(
        excluded_tools=frozenset({"execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    for key in provider_keys:
        register_harness_profile(key, profile)


def build_model(settings: Settings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
        use_responses_api=False,  # 內部服務通常只相容 chat completions
    )


def build_agent(
    model: BaseChatModel,
    store: Store,
    settings: Settings,
    k8s_tools: list[BaseTool],
    prom_tools: list[BaseTool],
    now: str,
    verbose: bool = True,
):
    register_profiles(model)
    prom_names = {t.name for t in prom_tools}
    data_names = K8S_TOOL_NAMES | prom_names

    worker_audit = ToolAuditMiddleware(
        store,
        agent_label=WORKER_NAME,
        data_tool_names=data_names,
        prom_tool_names=prom_names,
        prom_budget_per_task=settings.prom_budget_per_task,
        prom_budget_total=settings.prom_budget_total,
        verbose=verbose,
    )
    planner_audit = ToolAuditMiddleware(
        store,
        agent_label="planner",
        data_tool_names=set(),
        prom_tool_names=set(),
        prom_budget_per_task=0,
        prom_budget_total=0,
        verbose=verbose,
    )

    worker = {
        "name": WORKER_NAME,
        "description": WORKER_DESCRIPTION,
        "system_prompt": load_prompt("worker", now=now, prom_budget=settings.prom_budget_per_task),
        "tools": [*k8s_tools, *prom_tools, *make_worker_fact_tools(store)],
        "middleware": [worker_audit],
    }

    return create_deep_agent(
        model=model,
        tools=make_planner_tools(store),
        system_prompt=load_prompt("planner", now=now),
        subagents=[worker],
        middleware=[TaskGateMiddleware(store, settings.max_parallel_workers, verbose=verbose), planner_audit],
        checkpointer=InMemorySaver(),
        name="sre-planner",
    )


def kickoff_message(store: Store, plan: GoalPlan) -> str:
    lines = [f"原始 goal：{store.goal_text}", "", "子目標："]
    for g in plan.goals:
        scope = []
        if g.namespaces:
            scope.append("namespace=" + ",".join(g.namespaces))
        if g.deployments:
            scope.append("deployment=" + ",".join(g.deployments))
        lines.append(f"- {g.id}：{g.description}" + (f"（{'；'.join(scope)}）" if scope else ""))
    lines += ["", f"時間窗：{plan.time_window}", "限制："]
    lines += [f"- {c}" for c in plan.constraints] or ["- 只觀察，不修改"]
    if plan.out_of_scope:
        lines += ["超出 PoC 範圍（不要查，報告會註明）："] + [f"- {x}" for x in plan.out_of_scope]
    lines += ["", "請開始規劃並派工。所有子目標都 prove_goal 之後才結束。"]
    return "\n".join(lines)


def gate_message(store: Store) -> str:
    pending = "\n".join(f"- {g.id}：{g.description}" for g in store.unproven_goals())
    return (
        "還有子目標沒有 prove_goal，不能結束：\n"
        f"{pending}\n"
        "請用 list_intents 與 list_facts 檢查探索圖：fact 足夠就 prove_goal；不夠就 add_intent 再派 sre-worker 補查；"
        "現有來源確定無法回答時，先讓 worker 記錄一個說明原因的 observed fact，再以 conclusion=inconclusive 結案。"
    )


def last_ai_text(state: dict) -> str:
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage) and message.content:
            return message_text(message.content)
    return ""


async def summarize(model: BaseChatModel, store: Store, planner_text: str) -> InsightSummary:
    data = store.to_dict(max_output_chars=0)
    data.pop("tool_calls", None)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    structured = model.with_structured_output(InsightSummary, method="function_calling")
    result = await structured.ainvoke([
        SystemMessage(content=load_prompt("summary")),
        HumanMessage(content=f"調查資料（JSON）：\n{payload}\n\nplanner 的最後總結：\n{planner_text or '（無）'}"),
    ])
    if not isinstance(result, InsightSummary):
        raise ValueError("模型沒有回傳 InsightSummary")
    return result


def fallback_summary(store: Store, planner_text: str, reason: str) -> InsightSummary:
    proven = [g for g in store.goals.values() if g.status == "proven"]
    conclusion = [f"{g.id}：{g.summary}" for g in proven if g.summary] or [planner_text[:500] or "沒有取得結論"]
    return InsightSummary(
        conclusion=conclusion[:3],
        confidence="低",
        confidence_reason=f"結論摘要步驟失敗（{reason[:120]}），以下直接取自子目標結論",
        next_steps=["檢查 LLM 是否支援結構化輸出（function calling）"],
    )


@dataclass
class RunResult:
    report_path: Path
    data_path: Path
    store: Store
    stats: RunStats


async def run_investigation(
    goal_text: str,
    settings: Settings,
    model: BaseChatModel,
    k8s_tools: list[BaseTool],
    prom_tools: list[BaseTool],
    verbose: bool = True,
    log=print,
) -> RunResult:
    started = time.monotonic()
    now_dt = datetime.now().astimezone()
    now = now_dt.isoformat(timespec="minutes")
    run_id = now_dt.strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:6]
    store = Store(goal_text, run_id)
    usage = UsageMetadataCallbackHandler()
    stats = RunStats(model=getattr(model, "model_name", None) or getattr(model, "model", "") or type(model).__name__)

    # 1. goal 拆解
    log("① 拆解 goal …")
    plan, err = await decompose_goal(model, goal_text, now)
    if err:
        stats.errors.append(f"goal 拆解失敗，改用單一子目標：{err}")
        log(f"   goal 拆解失敗，改用單一子目標：{err}")
    store.set_goals(plan_to_goals(plan))
    store.constraints = plan.constraints
    store.out_of_scope = plan.out_of_scope
    store.time_window = plan.time_window
    for g in plan.goals:
        log(f"   {g.id}：{g.description}")

    # 2. 主 agent 規劃與派工
    agent = build_agent(model, store, settings, k8s_tools, prom_tools, now, verbose=verbose)
    config: dict[str, Any] = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": settings.recursion_limit,
        "callbacks": [usage],
    }
    final_state: dict = {}

    async def drive() -> None:
        nonlocal final_state
        log("② planner 開始派工 …")
        final_state = await agent.ainvoke({"messages": [HumanMessage(content=kickoff_message(store, plan))]}, config)
        # 3. 結束把關：還有子目標沒 proven 就塞回去
        while store.unproven_goals() and stats.gate_retries < settings.gate_max_retries:
            stats.gate_retries += 1
            pending = ", ".join(g.id for g in store.unproven_goals())
            log(f"③ 結束把關：{pending} 尚未 proven，第 {stats.gate_retries} 次要求繼續 …")
            final_state = await agent.ainvoke({"messages": [HumanMessage(content=gate_message(store))]}, config)

    try:
        await asyncio.wait_for(drive(), timeout=settings.timeout_s)
    except asyncio.TimeoutError:
        stats.timed_out = True
        log(f"   任務逾時（{settings.timeout_s} 秒），以目前結果產生報告")
    except Exception as exc:  # 讓報告仍然產生，方便 spike 時分析失敗原因
        stats.errors.append(f"agent 執行失敗：{type(exc).__name__}: {exc}")
        log(f"   agent 執行失敗：{type(exc).__name__}: {exc}")

    planner_text = last_ai_text(final_state) if final_state else ""

    # 4. 結論摘要（只有這段文字來自模型）
    log("④ 產生結論摘要 …")
    try:
        summary = await asyncio.wait_for(summarize(model, store, planner_text), timeout=max(60, settings.llm_timeout_s * 2))
    except Exception as exc:
        stats.errors.append(f"結論摘要失敗：{type(exc).__name__}: {exc}")
        summary = fallback_summary(store, planner_text, str(exc))

    # 5. 報告
    for meta in usage.usage_metadata.values():
        stats.input_tokens += int(meta.get("input_tokens", 0) or 0)
        stats.output_tokens += int(meta.get("output_tokens", 0) or 0)
    stats.duration_s = time.monotonic() - started

    out_dir = Path(settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_dt.strftime("%Y%m%d-%H%M")
    report_path = out_dir / f"insight-{stamp}.md"
    if report_path.exists():
        report_path = out_dir / f"insight-{stamp}-{run_id[-6:]}.md"
    report_path.write_text(render_report(store, summary, stats, now_dt), encoding="utf-8")
    data_path = report_path.with_suffix(".json")
    store.dump_json(data_path)
    return RunResult(report_path=report_path, data_path=data_path, store=store, stats=stats)
