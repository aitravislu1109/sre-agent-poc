"""用照劇本回應的假模型跑完整流程：goal 拆解 → 派工 → worker 記 fact → 證據把關 → prove_goal → 結束把關 → 報告。"""

import asyncio

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

from agent import run_investigation
from config import Settings
from goals import GoalPlan, SubGoal
from report import InsightSummary
from tests.fakes import FakeApps, FakeCore, ScriptedChatModel, ai, call
from tools.k8s import K8sReader, make_k8s_tools

PROM_OUTPUT = 'max_over_time(container_memory_working_set_bytes{namespace="shop",pod=~"order-svc-.*"}[24h]) = 2.09e9'


def prom_tool():
    def execute_query(query: str) -> str:
        return PROM_OUTPUT

    return StructuredTool.from_function(execute_query, name="execute_query", description="Run an instant PromQL query")


def settings(tmp_path, **kw):
    s = Settings(output_dir=str(tmp_path), timeout_s=60, prom_budget_per_task=1, **kw)
    return s


PLAN = GoalPlan(
    goals=[SubGoal(id="G1", description="確認 order-svc restart 原因", namespaces=["shop"], deployments=["order-svc"])],
    constraints=["只觀察，不修改"],
    time_window="過去 24 小時",
    out_of_scope=["錯誤 log"],
)

SUMMARY = InsightSummary(conclusion=["order-svc 反覆 OOMKilled（F1）"], confidence="中", confidence_reason="有 K8s 與指標佐證")


def worker_script():
    return [
        # worker 第 1 回合：同一回合平行發出 2 次 Prometheus，第 2 次超過每次派工上限（1 次）而被拒絕
        ai(call("k8s_pods", {"namespace": "shop", "name": "order-svc"}, "w1"),
           call("execute_query", {"query": "q1"}, "w2"),
           call("execute_query", {"query": "q2"}, "w3")),
        # 第 2 回合：一個真的證據、一個捏造的證據、回報異常
        ai(call("record_fact", {"goal_id": "G1", "summary": "上次終止原因 OOMKilled，restart 7 次",
                                 "evidence": "restarts=7 lastTerminated reason=OOMKilled exitCode=137",
                                 "confidence": "observed", "source": "tool_output",
                                 "namespace": "shop", "deployment": "order-svc"}, "w4"),
           call("record_fact", {"goal_id": "G1", "summary": "記憶體 limit 1Gi",
                                 "evidence": "memory limit=1Gi", "confidence": "observed", "source": "tool_output"}, "w5"),
           call("report_anomaly", {"namespace": "shop", "deployment": "order-svc", "category": "oom",
                                    "severity": "high", "summary": "反覆 OOMKilled", "evidence": "F1",
                                    "fact_ids": ["F1"]}, "w6")),
        ai(text="已記錄 F1（OOMKilled），回報異常 A1。"),
    ]


def test_full_flow_with_gate(tmp_path):
    model = ScriptedChatModel(
        responses=[
            # planner：建立 intent 後派工
            ai(call("add_intent", {"goal_id": "G1", "question": "order-svc 過去 24h restart 原因",
                                   "hypothesis": "restart 是 OOMKilled 造成", "namespace": "shop",
                                   "deployment": "order-svc"}, "p0")),
            ai(call("task", {"subagent_type": "sre-worker",
                             "description": "I1：G1，查 shop/order-svc 過去 24h restart 原因"}, "p1")),
            *worker_script(),
            # 派生追查 I2；先故意派一個沒有 intent id 的 task（被拒），再正確派 I2
            ai(call("add_intent", {"goal_id": "G1", "question": "記憶體是否在 OOM 前持續上升",
                                   "hypothesis": "記憶體洩漏", "namespace": "shop", "deployment": "order-svc",
                                   "parent_intent_id": "I1"}, "p4")),
            ai(call("task", {"subagent_type": "sre-worker", "description": "查記憶體趨勢"}, "p5")),
            ai(call("task", {"subagent_type": "sre-worker", "description": "I2：G1，記憶體是否持續上升"}, "p6")),
            # I2 的 worker 想引用 I1 的工具輸出 → 證據範圍不符被拒，沒有產出 fact
            ai(call("record_fact", {"goal_id": "G1", "summary": "借用 I1 的輸出",
                                     "evidence": "reason=OOMKilled exitCode=137",
                                     "confidence": "observed", "source": "tool_output"}, "w7")),
            ai(text="I2：沒有查到記憶體趨勢。"),
            # planner 沒 prove 就想結束 → 會被結束把關擋回來
            ai(text="看起來是 OOM。"),
            # 第一次 prove 用錯 fact（不存在），被拒；再用正確的
            ai(call("prove_goal", {"goal_id": "G1", "conclusion": "anomaly", "summary": "x", "fact_ids": ["F9"]}, "p2")),
            ai(call("prove_goal", {"goal_id": "G1", "conclusion": "anomaly",
                                   "summary": "order-svc 反覆 OOMKilled", "fact_ids": ["F1"]}, "p3")),
            ai(text="G1 完成：order-svc 因 OOMKilled 反覆重啟（F1）。"),
        ],
        structured=[PLAN, SUMMARY],
        seen=[],
    )
    k8s = make_k8s_tools(K8sReader(FakeCore(), FakeApps()))
    result = asyncio.run(run_investigation("order-svc 為什麼一直重啟", settings(tmp_path), model, k8s, [prom_tool()],
                                           verbose=False, log=lambda *_: None))
    store = result.store

    assert not model.responses, "劇本應剛好用完"
    assert result.stats.gate_retries == 1
    assert store.goals["G1"].status == "proven"
    # 證據把關：捏造的 fact 被拒絕，只剩一個已驗證 fact
    assert list(store.facts) == ["F1"] and store.facts["F1"].verified
    assert store.rejected_facts == 2  # 捏造 1 次 + 跨 intent 引用 1 次
    assert store.facts["F1"].intent_id == "I1"
    # 探索圖
    assert store.intents["I1"].status == "done" and store.intents["I2"].status == "exhausted"
    assert store.intents["I2"].parent_id == "I1" and store.intents["I2"].depth == 1
    assert {t.intent_id for t in store.tool_calls} == {"I1"}
    assert store.anomalies["A1"].intent_id == "I1"
    edges = {(e["from"], e["kind"], e["to"]) for e in store.graph()["edges"]}
    assert {("G1", "spawns", "I1"), ("I2", "derived_from", "I1"), ("I1", "yields", "F1"),
            ("F1", "proves", "G1"), ("F1", "supports", "A1")} <= edges
    # Prometheus 每次派工上限 1：第 1 次執行、第 2 次被拒
    assert store.prom_calls == 1
    tool_msgs = [m for msgs in model.seen for m in msgs if isinstance(m, ToolMessage)]
    assert any("Prometheus 查詢上限" in str(m.content) for m in tool_msgs)
    assert any("找不到" in str(m.content) for m in tool_msgs)
    assert any("I1 自己" in str(m.content) for m in tool_msgs)
    assert any("沒有 intent id" in str(m.content) for m in tool_msgs)
    # 工具輸出有被記錄
    assert {t.tool for t in store.tool_calls} == {"k8s_pods", "execute_query"}
    # 報告
    md = result.report_path.read_text(encoding="utf-8")
    assert "order-svc 反覆 OOMKilled（F1）" in md and "### [high] shop/order-svc" in md
    assert "## 調查脈絡" in md and "```mermaid" in md and "I1 --> F1" in md and "I1 --> I2" in md
    assert result.data_path.exists()


def test_no_execute_tool_and_only_sre_worker(tmp_path):
    """主 agent 看不到 execute，也沒有 general-purpose subagent。"""
    from agent import build_agent
    from store import Store

    model = ScriptedChatModel(responses=[], structured=[], seen=[])
    store = Store("g", "r")
    k8s = make_k8s_tools(K8sReader(FakeCore(), FakeApps()))
    agent = build_agent(model, store, settings(tmp_path), k8s, [prom_tool()], now="now", verbose=False)
    tool_node = agent.get_graph().nodes  # 確認能組起來
    assert tool_node

    # 從 graph 裡取出主 agent 會綁定的工具名稱
    captured = {}

    class Capture(ScriptedChatModel):
        def bind_tools(self, tools, **kwargs):
            captured["names"] = sorted(getattr(t, "name", None) or t.get("name") or t["function"]["name"] for t in tools)
            return self

    cap = Capture(responses=[ai(text="done")], structured=[], seen=[])
    agent = build_agent(cap, store, settings(tmp_path), k8s, [prom_tool()], now="now", verbose=False)
    asyncio.run(agent.ainvoke({"messages": [("user", "hi")]}, {"configurable": {"thread_id": "x"}}))
    names = captured["names"]
    assert "execute" not in names
    assert "task" in names and "prove_goal" in names and "add_intent" in names and "list_intents" in names
    assert "k8s_pods" not in names  # planner 不直接查資料
    task_desc = [m for m in cap.seen[0] if m.type == "system"][0].content
    assert "general-purpose" not in str(task_desc)
