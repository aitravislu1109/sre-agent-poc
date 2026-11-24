# 範例：一次調查從頭到尾怎麼跑

用一個常見的 goal，一步一步說明這個 PoC 實際做了什麼。叢集資料、指標數值與模型回覆都是示意，但工具名稱、參數、輸出格式、拒絕訊息與報告結構都跟程式碼一致。

```
python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"
```

假設現在是 2026-11-24 09:30（+08:00）。

## 總覽

| 步驟 | 誰做 | 產出 |
| --- | --- | --- |
| ① goal 拆解 | `goals.py`（1 次 LLM 結構化輸出） | 子目標 G1、G2，時間窗，限制 |
| ② 規劃與派工 | planner（主 agent） | intent I1、I2、I3，task 派工 |
| ③ 查證 | sre-worker（subagent） | 工具輸出、fact F1~F5、異常 A1 |
| ④ 結案 | planner | `prove_goal` G1、G2 |
| ⑤ 結束把關 | `agent.py` 外層迴圈 | 檢查是否還有未 proven 的子目標 |
| ⑥ 報告 | 1 次 LLM 結構化輸出 + `report.py` | `reports/insight-20261124-0930.md` 與 `.json` |

終端機（stderr）會即時印出每一步，例如：

```
① 拆解 goal …
   G1：確認 order-svc 從 2026-11-23 22:00 起每次 restart 的時間與終止原因
   G2：確認 restart 與記憶體用量、rollout 的關係
② planner 開始派工 …
  [planner] → add_intent {"goal_id": "G1", ...}
  [planner] → task {"subagent_type": "sre-worker", "description": "I1：G1，..."}
  [planner] ⇒ 派工 I1
  [sre-worker] → list_deployments {"namespace": "shop"}
  [sre-worker/I1] ← list_deployments 412 字元
  ...
  [planner] ⇐ I1 done
④ 產生結論摘要 …
```

## ① goal 拆解

`goals.py` 把 goal 和 `prompts/goals.md` 一起送給 LLM，要求回傳 `GoalPlan`：

```json
{
  "goals": [
    {"id": "G1", "description": "確認 order-svc 從 2026-11-23 22:00 起每次 restart 的時間與終止原因",
     "namespaces": [], "deployments": ["order-svc"]},
    {"id": "G2", "description": "確認 restart 與記憶體用量、rollout 的關係",
     "namespaces": [], "deployments": ["order-svc"]}
  ],
  "time_window": "2026-11-23T22:00+08:00 到現在",
  "constraints": ["只觀察，不修改；範圍是 order-svc", "資料來源只有 K8s 唯讀工具與 Prometheus MCP"],
  "out_of_scope": ["錯誤 log（PoC 沒有 log 來源）"]
}
```

goal 沒寫 namespace，所以 `namespaces` 是空的，要由 worker 自己找。拆解失敗時（例如模型不支援結構化輸出），程式會退回成單一子目標「回答：<goal>」，不會讓整個任務停掉。

## ② planner 建立 intent 並派工

planner 收到子目標後，先用 `write_todos` 列計畫。每一項查證都要先建 intent：

```
add_intent(goal_id="G1",
           question="order-svc 在哪個 namespace；過去 12 小時每次 restart 的時間與終止原因",
           hypothesis="蒐集現況",
           deployment="order-svc")
→ 已建立 I1。派工時 task 描述開頭要寫「I1：」，worker 的 fact 會掛在這個 intent 底下。
```

接著派工：

```
task(subagent_type="sre-worker",
     description="I1：G1。找出 order-svc 所在的 namespace，查 2026-11-23 22:00 之後 pod 的 restart 次數、
                  上次終止原因與時間，以及相關 Warning events。資料來源：K8s。")
```

`TaskGateMiddleware` 會先檢查描述開頭的 `I1` 存在而且還沒派過，然後把 I1 標成 running。如果 planner 忘了寫 intent id，會直接收到：

```
ERROR 拒絕執行：task 描述沒有 intent id。先用 add_intent 建立 intent，再在描述開頭寫「I1：…」。
```

## ③ sre-worker 查證（I1）

worker 只看得到這段 task 描述。它依 `prompts/worker.md` 的規則呼叫工具，每次呼叫都經過 `ToolAuditMiddleware`，輸出會記到 Store 並掛在 I1 底下。

**找 namespace：** `list_namespaces` → 看到 `shop`，再 `list_deployments(namespace="shop")`：

```
namespace=shop
NAME       READY  UP-TO-DATE  AVAILABLE  AGE
cart-svc   3/3    3           3          30d
order-svc  2/3    3           2          30d
```

**查 pod：** `k8s_pods(namespace="shop", name="order-svc")`。工具會先讀 deployment 的 selector（`app=order-svc`），再用它找 pod：

```
deployment=shop/order-svc selector=app=order-svc
NAME                       READY  STATUS            RESTARTS  NODE    AGE
order-svc-7d9f5c-abcde     0/1    CrashLoopBackOff  7         node-a  1d
order-svc-7d9f5c-fghij     1/1    Running           0         node-b  1d
order-svc-7d9f5c-klmno     1/1    Running           2         node-c  1d
last terminated:
order-svc-7d9f5c-abcde/app: restarts=7 lastTerminated reason=OOMKilled exitCode=137 startedAt=2026-11-24T00:41:10Z finishedAt=2026-11-24T01:12:55Z
order-svc-7d9f5c-klmno/app: restarts=2 lastTerminated reason=OOMKilled exitCode=137 startedAt=2026-11-23T18:02:31Z finishedAt=2026-11-23T18:40:02Z
```

**記錄 fact：** evidence 必須是上面輸出的原文：

```
record_fact(goal_id="G1",
            summary="order-svc 有 2 個 pod 反覆重啟（7 次與 2 次），上次終止原因都是 OOMKilled（exit 137）",
            evidence="order-svc-7d9f5c-abcde/app: restarts=7 lastTerminated reason=OOMKilled exitCode=137\norder-svc-7d9f5c-klmno/app: restarts=2 lastTerminated reason=OOMKilled exitCode=137",
            confidence="observed", source="tool_output")
→ 已記錄 F1（已驗證）
```

如果模型改寫了原文，例如把 evidence 寫成「pod 因記憶體不足被殺掉 7 次」，會被拒絕：

```
拒絕：confidence=observed 的 evidence 必須是工具輸出的原文（可用 ... 省略中間），
以下片段在I1 自己的工具輸出中找不到：pod 因記憶體不足被殺掉 7 次。
請貼上工具回傳的關鍵行原文，或改用 confidence=inferred。
```

worker 接著查 `k8s_events(namespace="shop", name_prefix="order-svc")`，記下 F2（BackOff events）。它判斷這是異常，於是回報：

```
report_anomaly(namespace="shop", deployment="order-svc", category="oom", severity="high",
               summary="order-svc 反覆 OOMKilled，1 個 pod 處於 CrashLoopBackOff",
               evidence="F1、F2", cause="記憶體不足，原因待查", fact_ids=["F1", "F2"])
→ 已記錄異常 A1（high）
```

最後 worker 回報三到五行摘要給 planner。`TaskGateMiddleware` 發現 I1 產出了 fact，把它結算為 **done**。

## ② 再派工：平行查證與追查假設

planner 用 `list_facts` 和 `list_intents` 看目前的探索圖：

```
I1 [G1] [done] [shop/order-svc] 假設：蒐集現況；facts：F1, F2
```

G1 已經有答案，G2 還需要資料。planner 在同一回合建立兩個互不相關的 intent，並平行派出：

```
add_intent(G2, "order-svc 最近一次 rollout 的時間與 image 變化", "OOM 從新版本上線後開始",
           namespace="shop", deployment="order-svc", parent_intent_id="I1")        → I2
add_intent(G2, "OOM 前記憶體用量相對 limit 的變化", "記憶體在 OOM 前逐步上升到接近 limit",
           namespace="shop", deployment="order-svc", parent_intent_id="I1")        → I3
task("I2：G2，查 rollout 歷史 …")   task("I3：G2，用 Prometheus 查記憶體 …")
```

**I2（K8s）：** `k8s_deployment` 的 rollout 歷史顯示 revision 7 在 2026-11-23T14:20:03Z（22:20 +08:00）建立，image 從 `1.4.1` 換成 `1.4.2`，resources 是 `memory req=512Mi lim=1Gi`。worker 記下 F3。

**I3（Prometheus）：** worker 不能假設指標名稱，所以先探索：

1. 列指標名稱並用 `memory` 過濾 → 找到 `container_memory_working_set_bytes`
2. 查 metadata 確認單位是 bytes、類型是 gauge
3. 查它在 `namespace="shop"` 下的 label，確認有 `pod`、`container`
4. 組 PromQL：

```
max by (pod) (max_over_time(container_memory_working_set_bytes{namespace="shop", pod=~"order-svc-.*", container="app"}[12h]))
```

輸出顯示三個 pod 的峰值都在 1.0e9 左右，也就是接近 1Gi limit。worker 記下 F4（可用指標）和 F5（峰值接近 limit）。PromQL 會寫在 fact 的 detail，方便人工覆查。

每次派工最多呼叫 Prometheus `PROM_BUDGET_PER_TASK` 次（預設 10），超過就回傳拒絕訊息，worker 只能用已取得的資料收尾。

如果 planner 想從 I3 再往下派 I4、再派 I5，`add_intent` 在第 3 層會回：

```
拒絕：同一條線已追查 2 層（I1 → I3 → I4）。停止下探，用現有 fact 結案，並在結論中標成「需人工確認」。
```

## ④ prove_goal

```
prove_goal(goal_id="G1", conclusion="anomaly",
           summary="22:00 後 order-svc 有 2 個 pod 共 9 次 OOMKilled", fact_ids=["F1", "F2"])
→ G1 已 proven（anomaly）。尚未完成：G2

prove_goal(goal_id="G2", conclusion="anomaly",
           summary="OOM 從 22:20 的 rollout（1.4.1 → 1.4.2）後開始，記憶體峰值接近 1Gi limit",
           fact_ids=["F3", "F5"])
→ G2 已 proven（anomaly）。所有子目標都已完成，可以結束。
```

`prove_goal` 至少要引用一個已驗證的 observed fact。只引用 inferred fact，或結論是 inconclusive 卻沒寫 `missing_data`，都會被拒絕。

## ⑤ 結束把關

如果 planner 在 G2 還沒 proven 時就停止呼叫工具，`agent.py` 不會就此結束，而是把下面這段塞回對話，讓它繼續：

```
還有子目標沒有 prove_goal，不能結束：
- G2：確認 restart 與記憶體用量、rollout 的關係
請用 list_intents 與 list_facts 檢查探索圖：fact 足夠就 prove_goal；不夠就 add_intent 再派 sre-worker 補查；…
```

最多重推 `GATE_MAX_RETRIES` 次（預設 3）。超過次數或逾時（`TASK_TIMEOUT_S`）時照樣產生報告，未完成的子目標會寫在「建議下一步」。

## ⑥ 報告

結論摘要只有文字來自 LLM，其餘都由 `report.py` 從 Store 產生。報告節錄：

```markdown
# SRE 調查報告 · 2026-11-24 09:30

## 原始問題
> order-svc 昨晚 22:00 後 pod 一直重啟，找出原因

## 結論
- order-svc 自 22:20 rollout 到 1.4.2 後反覆 OOMKilled，記憶體峰值接近 1Gi limit（F1、F3、F5）
- 信心：中（K8s 與指標互相吻合，但沒有 log 確認是否為記憶體洩漏）

## 子目標
| # | 子目標 | 狀態 | 結論 | 引用 fact |
| --- | --- | --- | --- | --- |
| G1 | 確認 order-svc 從 2026-11-23 22:00 起每次 restart 的時間與終止原因 | 已完成 | 有異常：22:00 後 2 個 pod 共 9 次 OOMKilled | F1, F2 |
| G2 | 確認 restart 與記憶體用量、rollout 的關係 | 已完成 | 有異常：OOM 從 22:20 的 rollout 後開始… | F3, F5 |

## 調查脈絡
（Mermaid 圖：G1 → I1 → I2、I3；I1 → F1、F2；I2 → F3；I3 → F4、F5；F1、F2 -.-> A1）

| intent | 子目標 | 上層 | 對象 | 問題 / 假設 | 狀態 | 產出 fact |
| --- | --- | --- | --- | --- | --- | --- |
| I1 | G1 | - | shop/order-svc | …<br>假設：蒐集現況 | 完成 | F1, F2 |
| I2 | G2 | I1 | shop/order-svc | …<br>假設：OOM 從新版本上線後開始 | 完成 | F3 |
| I3 | G2 | I1 | shop/order-svc | …<br>假設：記憶體在 OOM 前逐步上升到接近 limit | 完成 | F4, F5 |

## 證據
| # | 觀察 | 對象 | 來源 | 子目標 |
| F1 | order-svc 有 2 個 pod 反覆重啟（7 次與 2 次），上次終止原因都是 OOMKilled | shop/order-svc | 工具輸出 | G1 |
…（下方折疊區塊附每個 fact 的證據原文）

## 異常（依 severity 排序）
### [high] shop/order-svc：order-svc 反覆 OOMKilled，1 個 pod 處於 CrashLoopBackOff

## 建議下一步
- 比對 1.4.1 與 1.4.2 的變更，確認是否有記憶體用量增加的改動（需人工確認）

## 調查範圍
- 時間窗：2026-11-23T22:00+08:00 到現在
- 用過的資料工具：k8s_deployment, k8s_events, k8s_pods, list_deployments, list_namespaces, execute_query, list_metrics, …
- 超出 PoC 範圍：錯誤 log（PoC 沒有 log 來源）
- 證據比對失敗而被拒絕的 fact：1 次

## 執行統計
- 模型：…；耗時 … 秒；token：輸入 …、輸出 …
- 工具呼叫 … 次（Prometheus … 次、失敗 … 次）；intent 3 個；fact 5 個（已驗證 5）；異常 1 個；結束把關重推 0 次
```

同名的 `insight-20261124-0930.json` 會保存完整的子目標、intent、fact、異常、探索圖（`graph.nodes`、`graph.edges`），以及每次工具呼叫的參數與輸出，方便人工覆查。

## 這個範例展示的防護

| 防護 | 範例中的位置 |
| --- | --- |
| 派工一定要先建 intent | ②：沒寫 intent id 的 task 被拒 |
| 證據只能引用該 intent 自己的工具輸出 | ③：改寫過的 evidence 被拒 |
| 不假設指標名稱 | ③ I3：先列指標、查 metadata 與 label，再組 PromQL |
| 查詢預算 | ③ I3：每次派工的 Prometheus 呼叫有上限 |
| 追查深度 ≤ 2 層 | ②：第 3 層的 add_intent 被拒 |
| 結案必須有已驗證的證據 | ④：prove_goal 的規則 |
| 不會半途結束 | ⑤：結束把關 |
