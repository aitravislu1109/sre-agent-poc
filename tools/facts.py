"""record_fact、report_anomaly、list_facts、prove_goal：綁定到單次任務的 Store。"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from store import Store, current_intent


class RecordFactArgs(BaseModel):
    goal_id: str = Field(description="這個 fact 用來回答的子目標 id，例如 G1")
    summary: str = Field(description="一句話的觀察結論，含數值與時間窗，例如「記憶體峰值 1.8GiB / limit 2GiB（90%），過去 24h」")
    evidence: str = Field(
        description=(
            "工具回傳的關鍵行原文（不要改寫）。多行可用換行分隔，中間省略用 ...。"
            "confidence=observed 時會比對本次任務的工具輸出，對不上就拒絕。"
        )
    )
    confidence: Literal["observed", "inferred"] = Field(
        description="observed = 工具輸出直接看得到；inferred = 你的推論"
    )
    source: Literal["tool_output", "tool_error", "inference"] = Field(
        description="tool_output = 工具正常輸出；tool_error = 工具失敗或查無資料；inference = 推論"
    )
    detail: str = Field(default="", description="補充說明，例如用過的 PromQL、判斷依據")
    namespace: str | None = Field(default=None, description="對象 namespace")
    deployment: str | None = Field(default=None, description="對象 deployment")


class ReportAnomalyArgs(BaseModel):
    namespace: str
    deployment: str
    category: Literal[
        "restart", "oom", "crashloop", "probe", "rollout", "replicas", "scheduling",
        "cpu", "memory", "image", "config", "network", "other",
    ] = Field(description="異常類別")
    severity: Literal["critical", "high", "medium", "low"] = Field(
        description="critical = 服務不可用；high = 正在影響使用者；medium = 有風險但未影響；low = 值得留意"
    )
    summary: str = Field(description="一句話描述現象")
    evidence: str = Field(description="支持的證據（工具輸出原文或 fact id）")
    cause: str = Field(default="", description="推論的原因；不確定就寫不確定")
    confidence: Literal["high", "medium", "low"] = "medium"
    window_start: str | None = Field(default=None, description="異常開始時間（ISO 8601），不知道就留空")
    window_end: str | None = Field(default=None, description="異常結束時間（ISO 8601），持續中或不知道就留空")
    fact_ids: list[str] = Field(default_factory=list, description="支持這個異常的 fact id")


class AddIntentArgs(BaseModel):
    goal_id: str = Field(description="這項查證服務的子目標 id，例如 G1")
    question: str = Field(description="要回答的具體問題，例如「order-svc 過去 24h 每次 restart 的時間與終止原因」")
    hypothesis: str = Field(description="要驗證的假設，例如「restart 是 OOMKilled 造成」；純蒐集事實時寫「蒐集現況」")
    namespace: str | None = Field(default=None, description="對象 namespace")
    deployment: str | None = Field(default=None, description="對象 deployment（一個 intent 只負責一個 deployment）")
    parent_intent_id: str | None = Field(default=None, description="從哪個 intent 的結果派生出來的追查；第一層留空")


class NoArgs(BaseModel):
    pass


class ListFactsArgs(BaseModel):
    goal_id: str | None = Field(default=None, description="只列出某個子目標的 fact；留空列出全部")


class ProveGoalArgs(BaseModel):
    goal_id: str
    conclusion: Literal["normal", "anomaly", "inconclusive"] = Field(
        description="normal = 正常；anomaly = 有異常；inconclusive = 現有資料無法判斷"
    )
    summary: str = Field(description="這個子目標的結論，一到三句")
    fact_ids: list[str] = Field(description="支持結論的 fact id，至少一個已驗證的 observed fact")
    missing_data: str | None = Field(default=None, description="inconclusive 時必填：缺少哪些資料")


def make_worker_fact_tools(store: Store) -> list[BaseTool]:
    def record_fact(
        goal_id: str,
        summary: str,
        evidence: str,
        confidence: str,
        source: str,
        detail: str = "",
        namespace: str | None = None,
        deployment: str | None = None,
    ) -> str:
        _, message = store.add_fact(
            intent_id=current_intent.get(),
            goal_id=goal_id,
            summary=summary,
            detail=detail,
            evidence=evidence,
            confidence=confidence,
            source=source,
            namespace=namespace,
            deployment=deployment,
        )
        return message

    def report_anomaly(
        namespace: str,
        deployment: str,
        category: str,
        severity: str,
        summary: str,
        evidence: str,
        cause: str = "",
        confidence: str = "medium",
        window_start: str | None = None,
        window_end: str | None = None,
        fact_ids: list[str] | None = None,
    ) -> str:
        anomaly, created = store.add_anomaly(
            namespace=namespace,
            deployment=deployment,
            category=category,
            severity=severity,
            summary=summary,
            evidence=evidence,
            cause=cause,
            confidence=confidence,
            window_start=window_start,
            window_end=window_end,
            fact_ids=fact_ids,
            intent_id=current_intent.get(),
        )
        if created:
            return f"已記錄異常 {anomaly.id}（{anomaly.severity}）"
        return f"與既有異常 {anomaly.id} 重複（同對象、同類別、時間窗重疊），已合併"

    return [
        StructuredTool.from_function(
            record_fact,
            name="record_fact",
            description=(
                "記錄一個觀察（fact），正常的結果也要記。數值與狀態類觀察用 confidence=observed、"
                "source=tool_output，evidence 貼工具輸出原文；推論用 confidence=inferred、source=inference。"
                "只有已驗證的 observed fact 能用來 prove_goal。回傳 fact id。"
            ),
            args_schema=RecordFactArgs,
        ),
        StructuredTool.from_function(
            report_anomaly,
            name="report_anomaly",
            description="判斷為異常時才呼叫。同 deployment + 類別 + 重疊時間窗會自動合併。",
            args_schema=ReportAnomalyArgs,
        ),
        make_list_facts_tool(store),
    ]


def make_list_facts_tool(store: Store) -> BaseTool:
    def list_facts(goal_id: str | None = None) -> str:
        return store.facts_for_listing(goal_id)

    return StructuredTool.from_function(
        list_facts,
        name="list_facts",
        description="列出本次任務已記錄的 fact（id、子目標、是否已驗證、對象、摘要）。",
        args_schema=ListFactsArgs,
    )


def make_planner_tools(store: Store) -> list[BaseTool]:
    def add_intent(
        goal_id: str,
        question: str,
        hypothesis: str,
        namespace: str | None = None,
        deployment: str | None = None,
        parent_intent_id: str | None = None,
    ) -> str:
        _, message = store.add_intent(goal_id, question, hypothesis, namespace, deployment, parent_intent_id)
        return message

    def list_intents() -> str:
        return store.intents_for_listing()

    def prove_goal(
        goal_id: str,
        conclusion: str,
        summary: str,
        fact_ids: list[str],
        missing_data: str | None = None,
    ) -> str:
        _, message = store.prove_goal(goal_id, conclusion, summary, fact_ids, missing_data)
        return message

    return [
        StructuredTool.from_function(
            add_intent,
            name="add_intent",
            description=(
                "派工前先建立一項查證（intent），回傳 intent id（I1、I2…）。task 描述開頭必須寫「<intent id>：」。"
                "追查上一個 intent 的發現時填 parent_intent_id；同一條線最多往下 2 層，重複的查證會被拒絕。"
            ),
            args_schema=AddIntentArgs,
        ),
        StructuredTool.from_function(
            list_intents,
            name="list_intents",
            description="列出探索圖裡所有 intent：子目標、上層、狀態（pending/running/done/exhausted/blocked）、假設與產出的 fact。",
            args_schema=NoArgs,
        ),
        StructuredTool.from_function(
            prove_goal,
            name="prove_goal",
            description=(
                "子目標已有結論時呼叫。必須引用至少一個已驗證的 observed fact；"
                "conclusion=inconclusive 時必須寫 missing_data。所有子目標 proven 後才能結束。"
            ),
            args_schema=ProveGoalArgs,
        ),
        make_list_facts_tool(store),
    ]
