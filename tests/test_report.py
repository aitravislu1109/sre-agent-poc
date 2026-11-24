from datetime import datetime

from report import DeploymentResult, InsightSummary, RunStats, render_report
from store import Goal, Store


def test_render_report_sections():
    s = Store("order-svc 為什麼一直重啟", "t")
    s.time_window = "過去 24 小時"
    s.out_of_scope = ["錯誤 log"]
    s.set_goals([Goal(id="G1", description="restart 原因"), Goal(id="G2", description="記憶體")])
    s.record_tool_output("w", "k8s_pods", {}, "reason=OOMKilled exitCode=137", False)
    f, _ = s.add_fact("G1", "上次終止原因 OOMKilled", "", "reason=OOMKilled exitCode=137", "observed", "tool_output",
                      namespace="shop", deployment="order-svc")
    s.add_fact("G1", "可能是記憶體洩漏", "", "推論", "inferred", "inference")
    s.prove_goal("G1", "anomaly", "OOMKilled", [f.id])
    s.add_anomaly("shop", "order-svc", "oom", "high", "反覆 OOMKilled", "reason=OOMKilled", fact_ids=[f.id])
    summary = InsightSummary(
        conclusion=["order-svc 因記憶體不足反覆 OOMKilled（F1）"], confidence="中", confidence_reason="缺記憶體趨勢",
        next_steps=["調高 limit 前先確認是否洩漏"],
        per_deployment=[DeploymentResult(namespace="shop", deployment="order-svc", conclusion="OOM", fact_ids=["F1"])],
    )
    md = render_report(s, summary, RunStats(model="m", duration_s=12), datetime(2026, 11, 24, 23, 5))
    for heading in ["# SRE 調查報告 · 2026-11-24 23:05", "## 原始問題", "## 結論", "## 證據", "## 推論過程",
                    "## 異常（依 severity 排序）", "## 建議下一步", "## 各 deployment 結果", "## 調查範圍", "## 執行統計"]:
        assert heading in md
    assert "| F1 |" in md and "（推論 F2）" in md
    assert "### [high] shop/order-svc" in md
    assert "子目標 G2 未完成" in md
    assert "超出 PoC 範圍：錯誤 log" in md
