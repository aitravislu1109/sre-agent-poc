"""goal 拆解：一次帶結構化輸出的 LLM 呼叫，把一句話的 goal 拆成可驗證的子目標。"""

from __future__ import annotations

from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from store import Goal

PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str, **values: object) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class SubGoal(BaseModel):
    id: str = Field(description="G1、G2…")
    description: str = Field(description="可用證據驗證的子目標")
    namespaces: list[str] = Field(default_factory=list, description="涉及的 namespace；未知就留空")
    deployments: list[str] = Field(default_factory=list, description="涉及的 deployment；未知就留空")


class GoalPlan(BaseModel):
    goals: list[SubGoal] = Field(description="1 到 4 個子目標")
    constraints: list[str] = Field(default_factory=list, description="限制")
    time_window: str = Field(description="調查時間窗，例如「2026-11-23T22:00+08:00 到現在」或「過去 24 小時」")
    out_of_scope: list[str] = Field(default_factory=list, description="goal 中現有資料來源無法回答的部分")


def fallback_plan(goal_text: str) -> GoalPlan:
    return GoalPlan(
        goals=[SubGoal(id="G1", description=f"回答：{goal_text}")],
        constraints=["只觀察，不修改", "多個 deployment 各自獨立調查", "資料來源只有 K8s 與 Prometheus"],
        time_window="過去 24 小時",
        out_of_scope=[],
    )


async def decompose_goal(model: BaseChatModel, goal_text: str, now: str) -> tuple[GoalPlan, str | None]:
    """回傳 (plan, 錯誤訊息)。結構化輸出失敗時退回單一子目標，不讓整個任務失敗。"""
    structured = model.with_structured_output(GoalPlan, method="function_calling")
    messages = [
        SystemMessage(content=load_prompt("goals", now=now)),
        HumanMessage(content=f"goal：{goal_text}"),
    ]
    try:
        plan = await structured.ainvoke(messages)
        if not isinstance(plan, GoalPlan) or not plan.goals:
            raise ValueError("模型沒有回傳子目標")
    except Exception as exc:
        return fallback_plan(goal_text), f"{type(exc).__name__}: {exc}"
    # 正規化 id，避免重複或空白
    seen: set[str] = set()
    for i, g in enumerate(plan.goals[:4], start=1):
        if not g.id or g.id in seen:
            g.id = f"G{i}"
        seen.add(g.id)
    plan.goals = plan.goals[:4]
    return plan, None


def plan_to_goals(plan: GoalPlan) -> list[Goal]:
    return [
        Goal(id=g.id, description=g.description, namespaces=list(g.namespaces), deployments=list(g.deployments))
        for g in plan.goals
    ]
