from store import Goal, Store


def make_store() -> Store:
    s = Store("order-svc 為什麼一直重啟", "test")
    s.set_goals([Goal(id="G1", description="確認 restart 原因"), Goal(id="G2", description="記憶體")])
    s.record_tool_output("w", "k8s_pods", {}, "order-svc-abc/app: restarts=7 lastTerminated reason=OOMKilled exitCode=137\nfoo   bar", False)
    return s


def test_observed_fact_requires_evidence_in_tool_output():
    s = make_store()
    fact, msg = s.add_fact("G1", "OOMKilled", "", "reason=OOMKilled exitCode=137", "observed", "tool_output")
    assert fact is not None and fact.verified, msg

    fact, msg = s.add_fact("G1", "捏造", "", "reason=Error exitCode=1", "observed", "tool_output")
    assert fact is None and "找不到" in msg
    assert s.rejected_facts == 1


def test_evidence_whitespace_and_ellipsis():
    s = make_store()
    fact, msg = s.add_fact("G1", "x", "", "restarts=7   lastTerminated ... exitCode=137\nfoo bar", "observed", "tool_output")
    assert fact is not None, msg


def test_inferred_fact_not_checked_and_cannot_prove():
    s = make_store()
    fact, _ = s.add_fact("G1", "推論", "", "我覺得是記憶體洩漏", "inferred", "inference")
    assert fact is not None and not fact.verified
    ok, msg = s.prove_goal("G1", "anomaly", "x", [fact.id])
    assert not ok and "observed" in msg


def test_observed_with_inference_source_rejected():
    s = make_store()
    fact, msg = s.add_fact("G1", "x", "", "reason=OOMKilled", "observed", "inference")
    assert fact is None


def test_unknown_goal_rejected():
    s = make_store()
    fact, msg = s.add_fact("G9", "x", "", "reason=OOMKilled", "observed", "tool_output")
    assert fact is None and "G9" in msg


def test_prove_goal_rules():
    s = make_store()
    f, _ = s.add_fact("G1", "OOM", "", "reason=OOMKilled", "observed", "tool_output")
    ok, msg = s.prove_goal("G1", "inconclusive", "x", [f.id])
    assert not ok and "missing_data" in msg
    ok, msg = s.prove_goal("G1", "anomaly", "OOMKilled 7 次", [f.id])
    assert ok and "G2" in msg
    assert [g.id for g in s.unproven_goals()] == ["G2"]
    ok, _ = s.prove_goal("G2", "normal", "x", ["F99"])
    assert not ok


def test_anomaly_dedupe_by_target_category_and_window():
    s = make_store()
    a1, new1 = s.add_anomaly("shop", "order-svc", "oom", "medium", "OOM", "e1",
                             window_start="2026-11-24T10:00:00Z", window_end="2026-11-24T11:00:00Z")
    a2, new2 = s.add_anomaly("shop", "order-svc", "oom", "high", "OOM again", "e2",
                             window_start="2026-11-24T10:30:00Z", window_end="2026-11-24T12:00:00Z")
    a3, new3 = s.add_anomaly("shop", "order-svc", "oom", "low", "later", "e3",
                             window_start="2026-11-25T10:00:00Z", window_end="2026-11-25T11:00:00Z")
    a4, new4 = s.add_anomaly("shop", "cart-svc", "oom", "low", "other", "e4")
    assert new1 and not new2 and new3 and new4
    assert a2.id == a1.id and a1.severity == "high" and a1.merged_count == 2 and "e2" in a1.evidence
    assert len(s.anomalies) == 3


def test_to_dict_truncates_outputs():
    s = make_store()
    s.record_tool_output("w", "big", {}, "x" * 10000, False)
    d = s.to_dict(max_output_chars=100)
    assert len(d["tool_calls"][-1]["output"]) < 200
    assert d["stats"]["tool_calls"] == 2


def test_intent_depth_limit_and_dedupe():
    s = make_store()
    i1, _ = s.add_intent("G1", "restart 原因", "OOM", "shop", "order-svc")
    i2, _ = s.add_intent("G1", "記憶體趨勢", "洩漏", "shop", "order-svc", parent_id=i1.id)
    i3, _ = s.add_intent("G1", "哪個容器", "sidecar", "shop", "order-svc", parent_id=i2.id)
    assert (i1.depth, i2.depth, i3.depth) == (0, 1, 2)
    i4, msg = s.add_intent("G1", "更深", "x", "shop", "order-svc", parent_id=i3.id)
    assert i4 is None and "I1 → I2 → I3" in msg
    dup, msg = s.add_intent("G1", "restart  原因", "OOM", "shop", "order-svc")
    assert dup is None and "I1" in msg
    other, _ = s.add_intent("G1", "restart 原因", "OOM", "shop", "cart-svc")  # 不同 deployment 可以
    assert other is not None
    bad, msg = s.add_intent("G9", "x", "y")
    assert bad is None


def test_intent_lifecycle_and_scoped_evidence():
    s = make_store()  # make_store 的工具輸出沒有 intent
    i1, _ = s.add_intent("G1", "q", "h", "shop", "order-svc")
    ok, _ = s.start_intent(i1.id)
    assert ok and not s.start_intent(i1.id)[0]  # 不能派兩次
    # 任務層級的輸出不屬於 I1，不能當 I1 的證據
    fact, msg = s.add_fact("G1", "x", "", "reason=OOMKilled", "observed", "tool_output", intent_id=i1.id)
    assert fact is None and "I1 自己" in msg
    s.record_tool_output("w", "k8s_events", {}, "BackOff Pod/order-svc-abc", False, intent_id=i1.id)
    fact, msg = s.add_fact("G2", "x", "", "BackOff Pod/order-svc-abc", "observed", "tool_output", intent_id=i1.id)
    assert fact is not None and fact.goal_id == "G1" and fact.deployment == "order-svc"  # 跟著 intent
    assert s.finish_intent(i1.id).status == "done"
    i2, _ = s.add_intent("G1", "q2", "h2")
    s.start_intent(i2.id)
    assert s.finish_intent(i2.id).status == "exhausted"
    i3, _ = s.add_intent("G1", "q3", "h3")
    assert s.finish_intent(i3.id, failed=True).status == "blocked"
    assert "I1 [G1] [done]" in s.intents_for_listing()
