"""Insight 報告：表格與證據區塊由程式從 Store 產生，只有結論文字來自模型，格式才穩定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from store import SEVERITY_ORDER, Store

CONCLUSION_LABEL = {"normal": "正常", "anomaly": "有異常", "inconclusive": "無法判斷"}
INTENT_STATUS_LABEL = {
    "pending": "未派工",
    "running": "執行中（未完成）",
    "done": "完成",
    "exhausted": "無產出",
    "blocked": "失敗",
}


def _mermaid_label(text: str, limit: int = 40) -> str:
    text = " ".join((text or "").split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    for ch in '"[]{}()<>|#;`':
        text = text.replace(ch, " ")
    return text


def render_mermaid(store: Store) -> str:
    """探索圖：goal → intent（假設）→ 派生 intent → fact → 異常。"""
    lines = ["graph LR"]
    for g in store.goals.values():
        mark = "✓" if g.status == "proven" else "…"
        lines.append(f'  {g.id}["{g.id} {mark} {_mermaid_label(g.description)}"]')
    for i in store.intents.values():
        status = INTENT_STATUS_LABEL.get(i.status, i.status)
        lines.append(f'  {i.id}("{i.id} {status}<br/>{_mermaid_label(i.hypothesis)}")')
        source = i.parent_id or i.goal_id
        lines.append(f"  {source} --> {i.id}")
    for f in store.facts.values():
        if not f.intent_id:
            continue
        mark = "✓" if f.verified else "推論"
        lines.append(f'  {f.id}["{f.id} {mark} {_mermaid_label(f.summary, 30)}"]')
        lines.append(f"  {f.intent_id} --> {f.id}")
    for a in store.anomalies.values():
        lines.append(f'  {a.id}{{{{"{a.id} {a.severity} {_mermaid_label(a.summary, 30)}"}}}}')
        for fid in a.fact_ids:
            lines.append(f"  {fid} -.-> {a.id}")
        if not a.fact_ids and a.intent_id:
            lines.append(f"  {a.intent_id} -.-> {a.id}")
    for g in store.goals.values():
        for fid in g.fact_ids:
            if fid in store.facts and store.facts[fid].intent_id:
                lines.append(f"  {fid} ==> {g.id}")
    return "\n".join(lines)


class DeploymentResult(BaseModel):
    namespace: str = ""
    deployment: str
    conclusion: str = Field(description="一句話結論")
    fact_ids: list[str] = Field(default_factory=list)


class InsightSummary(BaseModel):
    conclusion: list[str] = Field(description="一到三句，直接回答原始 goal")
    confidence: Literal["高", "中", "低"]
    confidence_reason: str
    reasoning: list[str] = Field(default_factory=list, description="驗證過的假設與結果（成立 / 否定 / 無法判斷）")
    other_observations: list[str] = Field(default_factory=list, description="非故障但值得留意的觀察")
    next_steps: list[str] = Field(default_factory=list, description="建議下一步、需要人工確認的項目")
    per_deployment: list[DeploymentResult] = Field(default_factory=list, description="goal 涉及多個 deployment 時各自的結論")


@dataclass
class RunStats:
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    gate_retries: int = 0
    timed_out: bool = False
    errors: list[str] = field(default_factory=list)
    model: str = ""


def _cell(text: str | None, limit: int = 220) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _evidence_block(text: str, limit: int = 1200) -> str:
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + "\n…"
    return "\n".join("    " + line for line in text.splitlines())


def render_report(store: Store, summary: InsightSummary, stats: RunStats, generated_at: datetime) -> str:
    out: list[str] = []
    add = out.append

    add(f"# SRE 調查報告 · {generated_at.strftime('%Y-%m-%d %H:%M')}")
    add("")
    add("## 原始問題")
    add(f"> {store.goal_text}")
    add("")

    add("## 結論")
    for line in summary.conclusion or ["（模型沒有給出結論）"]:
        add(f"- {line}")
    add(f"- 信心：{summary.confidence}（{summary.confidence_reason}）")
    add("")

    add("## 子目標")
    add("| # | 子目標 | 狀態 | 結論 | 引用 fact |")
    add("| --- | --- | --- | --- | --- |")
    for g in store.goals.values():
        status = "已完成" if g.status == "proven" else "未完成"
        concl = CONCLUSION_LABEL.get(g.conclusion or "", "-")
        text = g.summary or ""
        if g.missing_data:
            text += f"（缺：{g.missing_data}）"
        add(f"| {g.id} | {_cell(g.description)} | {status} | {concl}：{_cell(text)} | {', '.join(g.fact_ids) or '-'} |")
    add("")

    if store.intents:
        add("## 調查脈絡")
        add("```mermaid")
        add(render_mermaid(store))
        add("```")
        add("")
        add("| intent | 子目標 | 上層 | 對象 | 問題 / 假設 | 狀態 | 產出 fact |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for i in store.intents.values():
            facts = [f.id for f in store.facts.values() if f.intent_id == i.id]
            target = f"{i.namespace or '-'}/{i.deployment or '-'}"
            add(
                f"| {i.id} | {i.goal_id} | {i.parent_id or '-'} | {_cell(target)} | {_cell(i.question, 140)}<br>假設：{_cell(i.hypothesis, 140)} "
                f"| {INTENT_STATUS_LABEL.get(i.status, i.status)} | {', '.join(facts) or '-'} |"
            )
        add("")

    verified = [f for f in store.facts.values() if f.verified]
    add("## 證據")
    if verified:
        add("| # | 觀察 | 對象 | 來源 | 子目標 |")
        add("| --- | --- | --- | --- | --- |")
        for f in verified:
            target = f"{f.namespace or '-'}/{f.deployment}" if f.deployment else (f.namespace or "-")
            source = "工具失敗 / 查無資料" if f.source == "tool_error" else "工具輸出"
            detail = f"；{_cell(f.detail, 160)}" if f.detail else ""
            add(f"| {f.id} | {_cell(f.summary)}{detail} | {target} | {source} | {f.goal_id} |")
        add("")
        add("<details><summary>證據原文</summary>")
        add("")
        for f in verified:
            add(f"**{f.id}** {_cell(f.summary, 120)}")
            add("")
            add(_evidence_block(f.evidence))
            add("")
        add("</details>")
    else:
        add("（沒有已驗證的證據）")
    add("")

    add("## 推論過程")
    inferred = [f for f in store.facts.values() if not f.verified]
    for line in summary.reasoning:
        add(f"- {line}")
    for f in inferred:
        add(f"- （推論 {f.id}）{_cell(f.summary)}")
    if not summary.reasoning and not inferred:
        add("- （無）")
    add("")

    add("## 異常（依 severity 排序）")
    anomalies = sorted(store.anomalies.values(), key=lambda a: (SEVERITY_ORDER.get(a.severity, 9), a.id))
    if not anomalies:
        add("（沒有回報異常）")
    for a in anomalies:
        add(f"### [{a.severity}] {a.namespace}/{a.deployment}：{_cell(a.summary, 120)}")
        window = f"{a.window_start or '?'} ~ {a.window_end or '持續中 / 未知'}"
        add(f"- 類別：{a.category}；時間窗：{window}；信心：{a.confidence}")
        if a.cause:
            add(f"- 推論原因：{_cell(a.cause, 400)}")
        if a.fact_ids:
            add(f"- 相關 fact：{', '.join(a.fact_ids)}")
        if a.merged_count > 1:
            add(f"- 合併了 {a.merged_count} 次回報")
        add("- 證據：")
        add(_evidence_block(a.evidence, 800))
        add("")
    add("")

    add("## 其他觀察（非故障但值得留意）")
    for line in summary.other_observations or ["（無）"]:
        add(f"- {line}")
    add("")

    add("## 建議下一步")
    steps = list(summary.next_steps)
    for g in store.unproven_goals():
        steps.append(f"子目標 {g.id} 未完成，需人工確認：{g.description}")
    for line in steps or ["（無）"]:
        add(f"- {line}")
    add("")

    if summary.per_deployment:
        add("## 各 deployment 結果")
        add("| deployment | 結論 | 主要證據 |")
        add("| --- | --- | --- |")
        for d in summary.per_deployment:
            name = f"{d.namespace}/{d.deployment}" if d.namespace else d.deployment
            add(f"| {_cell(name)} | {_cell(d.conclusion)} | {', '.join(d.fact_ids) or '-'} |")
        add("")

    add("## 調查範圍")
    add(f"- 時間窗：{store.time_window or '過去 24 小時'}")
    tools_used = sorted({t.tool for t in store.tool_calls})
    add(f"- 用過的資料工具：{', '.join(tools_used) or '（無）'}")
    for item in store.out_of_scope:
        add(f"- 超出 PoC 範圍：{item}")
    failed = [t for t in store.tool_calls if t.is_error]
    if failed:
        add(f"- 工具失敗 {len(failed)} 次：" + "；".join(f"{t.tool} {_cell(t.output, 80)}" for t in failed[:5]))
    if store.rejected_facts:
        add(f"- 證據比對失敗而被拒絕的 fact：{store.rejected_facts} 次")
    if stats.timed_out:
        add("- 任務逾時，以下結果可能不完整")
    for err in stats.errors:
        add(f"- 執行問題：{_cell(err, 300)}")
    add("")

    add("## 執行統計")
    add(f"- 模型：{stats.model}；耗時 {stats.duration_s:.0f} 秒；token：輸入 {stats.input_tokens:,}、輸出 {stats.output_tokens:,}")
    add(
        f"- 工具呼叫 {len(store.tool_calls)} 次（Prometheus {store.prom_calls} 次、失敗 {len(failed)} 次）；"
        f"intent {len(store.intents)} 個；fact {len(store.facts)} 個（已驗證 {len(verified)}）；異常 {len(store.anomalies)} 個；結束把關重推 {stats.gate_retries} 次"
    )
    add("")
    return "\n".join(out)
