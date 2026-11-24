"""單次任務的記憶體儲存：子目標、facts、異常、工具輸出紀錄。任務結束時寫成 JSON。

證據驗證（ARTEX 的核心防捏造機制）也在這裡：confidence=observed 的 fact，
evidence 必須能在本次任務實際的工具輸出中找到（正規化空白後比對子字串）。
"""

from __future__ import annotations

import contextvars
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SOURCE_REQUIRES_EVIDENCE = {"tool_output"}

# evidence 中可用 "..." 或 "…" 省略中間內容；每一段都必須出現在工具輸出中
_ELLIPSIS = re.compile(r"\.{3,}|…")
_WS = re.compile(r"\s+")
MIN_FRAGMENT_LEN = 4
MAX_INTENT_DEPTH = 2  # 根 intent 深度 0；從它派生的追查最多再 2 層

# 目前正在執行的 intent（由 worker 的 middleware 在每次工具呼叫時設定，record_fact 讀取）
current_intent: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_intent", default=None)
INTENT_ID_RE = re.compile(r"\bI\d+\b")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass
class Goal:
    id: str
    description: str
    namespaces: list[str] = field(default_factory=list)
    deployments: list[str] = field(default_factory=list)
    status: str = "open"  # open | proven
    conclusion: str | None = None  # normal | anomaly | inconclusive
    summary: str | None = None
    fact_ids: list[str] = field(default_factory=list)
    missing_data: str | None = None


@dataclass
class Intent:
    """一項查證：要回答的問題 + 要驗證的假設，派給一個 worker。"""

    id: str
    goal_id: str
    question: str
    hypothesis: str
    namespace: str | None = None
    deployment: str | None = None
    parent_id: str | None = None
    depth: int = 0
    status: str = "pending"  # pending | running | done | exhausted | blocked
    note: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass
class Fact:
    id: str
    goal_id: str
    summary: str
    detail: str
    evidence: str
    confidence: str  # observed | inferred
    source: str  # tool_output | inference | tool_error
    verified: bool
    namespace: str | None = None
    deployment: str | None = None
    intent_id: str | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class Anomaly:
    id: str
    namespace: str
    deployment: str
    category: str
    severity: str
    summary: str
    evidence: str
    cause: str = ""
    confidence: str = "medium"
    window_start: str | None = None
    window_end: str | None = None
    fact_ids: list[str] = field(default_factory=list)
    intent_id: str | None = None
    merged_count: int = 1
    created_at: str = field(default_factory=now_iso)


@dataclass
class ToolCallRecord:
    seq: int
    agent: str
    tool: str
    args: dict
    output: str
    is_error: bool
    intent_id: str | None = None
    ts: str = field(default_factory=now_iso)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _windows_overlap(a: Anomaly, b_start: str | None, b_end: str | None) -> bool:
    """任一方沒有可解析的時間窗時視為重疊（保守去重）。"""
    a_start, a_end = _parse_time(a.window_start), _parse_time(a.window_end)
    s, e = _parse_time(b_start), _parse_time(b_end)
    if not (a_start or a_end) or not (s or e):
        return True
    a_start = a_start or a_end
    a_end = a_end or a_start
    s = s or e
    e = e or s
    return a_start <= e and s <= a_end


class Store:
    def __init__(self, goal_text: str, run_id: str):
        self.goal_text = goal_text
        self.run_id = run_id
        self.started_at = now_iso()
        self.goals: dict[str, Goal] = {}
        self.intents: dict[str, Intent] = {}
        self.facts: dict[str, Fact] = {}
        self.anomalies: dict[str, Anomaly] = {}
        self.tool_calls: list[ToolCallRecord] = []
        self.constraints: list[str] = []
        self.out_of_scope: list[str] = []
        self.time_window: str = ""
        self.prom_calls = 0
        self.rejected_facts = 0
        self._normalized_outputs: list[tuple[str | None, str]] = []  # (intent_id, 正規化輸出)
        self._lock = threading.Lock()

    # ---------- 子目標 ----------
    def set_goals(self, goals: list[Goal]) -> None:
        with self._lock:
            self.goals = {g.id: g for g in goals}

    def unproven_goals(self) -> list[Goal]:
        return [g for g in self.goals.values() if g.status != "proven"]

    # ---------- intent（探索圖的查證節點）----------
    def add_intent(
        self,
        goal_id: str,
        question: str,
        hypothesis: str,
        namespace: str | None = None,
        deployment: str | None = None,
        parent_id: str | None = None,
    ) -> tuple[Intent | None, str]:
        if goal_id not in self.goals:
            return None, f"拒絕：goal_id {goal_id!r} 不存在。可用的子目標：{', '.join(self.goals) or '(無)'}"
        depth = 0
        if parent_id:
            parent = self.intents.get(parent_id)
            if parent is None:
                return None, f"拒絕：上層 intent {parent_id!r} 不存在。"
            depth = parent.depth + 1
            if depth > MAX_INTENT_DEPTH:
                return None, (
                    f"拒絕：同一條線已追查 {MAX_INTENT_DEPTH} 層（{self._chain(parent_id)}）。"
                    "停止下探，用現有 fact 結案，並在結論中標成「需人工確認」。"
                )
        key = (goal_id, deployment or "", normalize(hypothesis).lower(), normalize(question).lower())
        with self._lock:
            for existing in self.intents.values():
                same = (
                    existing.goal_id,
                    existing.deployment or "",
                    normalize(existing.hypothesis).lower(),
                    normalize(existing.question).lower(),
                )
                if same == key and existing.status != "blocked":
                    return None, f"拒絕：與 {existing.id}（{existing.status}）重複，不要重複同樣的查證。"
            iid = f"I{len(self.intents) + 1}"
            intent = Intent(
                id=iid,
                goal_id=goal_id,
                question=question,
                hypothesis=hypothesis,
                namespace=namespace,
                deployment=deployment,
                parent_id=parent_id,
                depth=depth,
            )
            self.intents[iid] = intent
        return intent, f"已建立 {iid}。派工時 task 描述開頭要寫「{iid}：」，worker 的 fact 會掛在這個 intent 底下。"

    def _chain(self, intent_id: str) -> str:
        chain = []
        cur = self.intents.get(intent_id)
        while cur:
            chain.append(cur.id)
            cur = self.intents.get(cur.parent_id) if cur.parent_id else None
        return " → ".join(reversed(chain))

    def start_intent(self, intent_id: str) -> tuple[bool, str]:
        intent = self.intents.get(intent_id)
        if intent is None:
            return False, f"{intent_id} 不存在，先用 add_intent 建立。"
        if intent.status != "pending":
            return False, f"{intent_id} 已經是 {intent.status}，不能再派一次。需要再查請用 add_intent 建立新的（可設 parent_intent_id）。"
        with self._lock:
            intent.status = "running"
        return True, ""

    def finish_intent(self, intent_id: str, failed: bool = False, note: str = "") -> Intent | None:
        """worker 回報後結算狀態：有 fact = done；沒有 fact = exhausted；執行失敗 = blocked。"""
        intent = self.intents.get(intent_id)
        if intent is None:
            return None
        yielded = [f for f in self.facts.values() if f.intent_id == intent_id]
        with self._lock:
            if failed:
                intent.status = "blocked"
            elif yielded:
                intent.status = "done"
            else:
                intent.status = "exhausted"
            intent.note = note[:300]
        return intent

    # ---------- 工具輸出紀錄 ----------
    def record_tool_output(
        self, agent: str, tool: str, args: dict, output: str, is_error: bool, intent_id: str | None = None
    ) -> None:
        with self._lock:
            self.tool_calls.append(
                ToolCallRecord(
                    seq=len(self.tool_calls) + 1,
                    agent=agent,
                    tool=tool,
                    args=args,
                    output=output,
                    is_error=is_error,
                    intent_id=intent_id,
                )
            )
            self._normalized_outputs.append((intent_id, normalize(output)))

    def try_consume_prom_budget(self, total_budget: int) -> bool:
        with self._lock:
            if self.prom_calls >= total_budget:
                return False
            self.prom_calls += 1
            return True

    def find_unverified_fragments(self, evidence: str, intent_id: str | None = None) -> list[str]:
        """回傳在工具輸出中找不到的 evidence 片段；空 list 代表通過。

        有 intent_id 時只比對該 intent 自己的工具輸出（跟 ARTEX 一樣），否則比對整個任務。
        """
        fragments: list[str] = []
        for line in evidence.splitlines():
            for piece in _ELLIPSIS.split(line):
                piece = normalize(piece).strip("\"'`「」")
                piece = normalize(piece)
                if len(piece) >= MIN_FRAGMENT_LEN:
                    fragments.append(piece)
        if not fragments:
            return [evidence or "(空的 evidence)"]
        with self._lock:
            outputs = [o for iid, o in self._normalized_outputs if intent_id is None or iid == intent_id]
        return [frag for frag in fragments if not any(frag in out for out in outputs)]

    # ---------- facts ----------
    def add_fact(
        self,
        goal_id: str,
        summary: str,
        detail: str,
        evidence: str,
        confidence: str,
        source: str,
        namespace: str | None = None,
        deployment: str | None = None,
        intent_id: str | None = None,
    ) -> tuple[Fact | None, str]:
        if intent_id is not None:
            intent = self.intents.get(intent_id)
            if intent is None:
                return None, f"拒絕：intent {intent_id!r} 不存在。"
            if goal_id != intent.goal_id:
                goal_id = intent.goal_id  # fact 一律掛在 intent 所屬的子目標
            namespace = namespace or intent.namespace
            deployment = deployment or intent.deployment
        if goal_id not in self.goals:
            return None, f"拒絕：goal_id {goal_id!r} 不存在。可用的子目標：{', '.join(self.goals) or '(無)'}"
        if confidence == "observed" and source == "inference":
            return None, "拒絕：source=inference 的 fact 只能用 confidence=inferred。"

        verified = False
        if confidence == "observed":
            missing = self.find_unverified_fragments(evidence, intent_id)
            if missing:
                with self._lock:
                    self.rejected_facts += 1
                shown = "; ".join(m[:120] for m in missing[:3])
                scope = f"{intent_id} 自己" if intent_id else "本次任務"
                return None, (
                    "拒絕：confidence=observed 的 evidence 必須是工具輸出的原文（可用 ... 省略中間），"
                    f"以下片段在{scope}的工具輸出中找不到：{shown}。"
                    "請貼上工具回傳的關鍵行原文，或改用 confidence=inferred。"
                )
            verified = True

        with self._lock:
            fid = f"F{len(self.facts) + 1}"
            fact = Fact(
                id=fid,
                goal_id=goal_id,
                summary=summary,
                detail=detail,
                evidence=evidence,
                confidence=confidence,
                source=source,
                verified=verified,
                namespace=namespace,
                deployment=deployment,
                intent_id=intent_id,
            )
            self.facts[fid] = fact
        return fact, f"已記錄 {fid}（{'已驗證' if verified else '推論，未驗證'}）"

    # ---------- 異常 ----------
    def add_anomaly(
        self,
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
        intent_id: str | None = None,
    ) -> tuple[Anomaly, bool]:
        """回傳 (異常, 是否為新建)。同 namespace + deployment + category 且時間窗重疊時合併。"""
        fact_ids = [f for f in (fact_ids or []) if f in self.facts]
        with self._lock:
            for existing in self.anomalies.values():
                if (
                    existing.namespace == namespace
                    and existing.deployment == deployment
                    and existing.category == category
                    and _windows_overlap(existing, window_start, window_end)
                ):
                    if SEVERITY_ORDER.get(severity, 9) < SEVERITY_ORDER.get(existing.severity, 9):
                        existing.severity = severity
                    if evidence and evidence not in existing.evidence:
                        existing.evidence += "\n" + evidence
                    if cause and cause not in existing.cause:
                        existing.cause = f"{existing.cause}\n{cause}".strip()
                    existing.fact_ids = sorted(set(existing.fact_ids) | set(fact_ids))
                    existing.merged_count += 1
                    return existing, False
            aid = f"A{len(self.anomalies) + 1}"
            anomaly = Anomaly(
                id=aid,
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
                intent_id=intent_id,
            )
            self.anomalies[aid] = anomaly
            return anomaly, True

    # ---------- prove_goal ----------
    def prove_goal(
        self,
        goal_id: str,
        conclusion: str,
        summary: str,
        fact_ids: list[str],
        missing_data: str | None = None,
    ) -> tuple[bool, str]:
        goal = self.goals.get(goal_id)
        if goal is None:
            return False, f"拒絕：goal_id {goal_id!r} 不存在。"
        unknown = [f for f in fact_ids if f not in self.facts]
        if unknown:
            return False, f"拒絕：fact {', '.join(unknown)} 不存在。先用 list_facts 查看已記錄的 fact。"
        observed = [
            f for f in fact_ids if self.facts[f].confidence == "observed" and self.facts[f].verified
        ]
        if not observed:
            return False, (
                "拒絕：prove_goal 至少要引用一個已驗證的 observed fact。"
                "若是資料不足，請派 worker 記錄「查不到 / 工具失敗」的 observed fact（evidence 引用錯誤輸出），"
                "再以 conclusion=inconclusive 結案。"
            )
        if conclusion == "inconclusive" and not (missing_data or "").strip():
            return False, "拒絕：conclusion=inconclusive 時必須在 missing_data 寫明缺少哪些資料。"
        with self._lock:
            goal.status = "proven"
            goal.conclusion = conclusion
            goal.summary = summary
            goal.fact_ids = fact_ids
            goal.missing_data = missing_data
        remaining = [g.id for g in self.unproven_goals()]
        tail = f"尚未完成：{', '.join(remaining)}" if remaining else "所有子目標都已完成，可以結束。"
        return True, f"{goal_id} 已 proven（{conclusion}）。{tail}"

    # ---------- 輸出 ----------
    def facts_for_listing(self, goal_id: str | None = None) -> str:
        rows = [f for f in self.facts.values() if goal_id is None or f.goal_id == goal_id]
        if not rows:
            return "(目前沒有 fact)"
        lines = []
        for f in rows:
            tag = "observed✓" if f.verified else f.confidence
            target = f"{f.namespace}/{f.deployment}" if f.deployment else (f.namespace or "-")
            lines.append(f"{f.id} [{f.goal_id}/{f.intent_id or '-'}] [{tag}] [{target}] {f.summary}")
        return "\n".join(lines)

    def intents_for_listing(self) -> str:
        if not self.intents:
            return "(目前沒有 intent)"
        lines = []
        for i in self.intents.values():
            facts = [f.id for f in self.facts.values() if f.intent_id == i.id]
            parent = f" ← {i.parent_id}" if i.parent_id else ""
            lines.append(
                f"{i.id} [{i.goal_id}{parent}] [{i.status}] [{i.namespace or '-'}/{i.deployment or '-'}] "
                f"假設：{i.hypothesis}；facts：{', '.join(facts) or '-'}"
            )
        return "\n".join(lines)

    # ---------- 探索圖 ----------
    def graph(self) -> dict[str, list[dict]]:
        """nodes 與 edges：goal -spawns-> intent、intent -derived_from-> 上層 intent、
        intent -yields-> fact、fact -proves-> goal、fact -supports-> anomaly。"""
        nodes: list[dict] = []
        edges: list[dict] = []
        for g in self.goals.values():
            nodes.append({"id": g.id, "kind": "goal", "label": g.description, "status": g.status})
        for i in self.intents.values():
            nodes.append({"id": i.id, "kind": "intent", "label": i.question, "hypothesis": i.hypothesis, "status": i.status})
            if i.parent_id:
                edges.append({"from": i.id, "to": i.parent_id, "kind": "derived_from"})
            else:
                edges.append({"from": i.goal_id, "to": i.id, "kind": "spawns"})
        for f in self.facts.values():
            nodes.append({"id": f.id, "kind": "fact", "label": f.summary, "verified": f.verified})
            if f.intent_id:
                edges.append({"from": f.intent_id, "to": f.id, "kind": "yields"})
        for g in self.goals.values():
            for fid in g.fact_ids:
                edges.append({"from": fid, "to": g.id, "kind": "proves"})
        for a in self.anomalies.values():
            nodes.append({"id": a.id, "kind": "anomaly", "label": a.summary, "severity": a.severity})
            for fid in a.fact_ids:
                edges.append({"from": fid, "to": a.id, "kind": "supports"})
        return {"nodes": nodes, "edges": edges}

    def to_dict(self, max_output_chars: int = 4000) -> dict[str, Any]:
        def trimmed(rec: ToolCallRecord) -> dict:
            d = asdict(rec)
            if len(d["output"]) > max_output_chars:
                d["output"] = d["output"][:max_output_chars] + f"... (截斷，原長 {len(rec.output)} 字元)"
            return d

        return {
            "run_id": self.run_id,
            "goal": self.goal_text,
            "started_at": self.started_at,
            "time_window": self.time_window,
            "constraints": self.constraints,
            "out_of_scope": self.out_of_scope,
            "goals": [asdict(g) for g in self.goals.values()],
            "intents": [asdict(i) for i in self.intents.values()],
            "facts": [asdict(f) for f in self.facts.values()],
            "anomalies": [asdict(a) for a in self.anomalies.values()],
            "graph": self.graph(),
            "stats": {
                "tool_calls": len(self.tool_calls),
                "tool_errors": sum(1 for t in self.tool_calls if t.is_error),
                "prom_calls": self.prom_calls,
                "rejected_facts": self.rejected_facts,
            },
            "tool_calls": [trimmed(t) for t in self.tool_calls],
        }

    def dump_json(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
