# SRE Agent PoC（deepagents）

用一句話下達調查目標，agent 自動拆解、查證，產出 Insight 報告。沿用 ARTEX 的流程，改用 LangChain [deepagents](https://github.com/langchain-ai/deepagents)（Python）實作。只觀察，不修改。

```
python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"
```

## 流程

架構圖與流程圖見 [docs/architecture.md](docs/architecture.md)，一個完整的範例見 [docs/example-walkthrough.md](docs/example-walkthrough.md)，詳細的運行說明（RBAC、Prometheus MCP、疑難排解）見 [docs/run-guide.md](docs/run-guide.md)。

1. **goal 拆解**（`goals.py`）：一次結構化輸出的 LLM 呼叫，把 goal 拆成 1 到 4 個可驗證的子目標（G1…G4），並寫下時間窗、限制與超出範圍的部分。調查哪些 namespace 和 deployment 由 goal 決定，沒有寫死。
2. **planner**（主 agent，`create_deep_agent`）：用 `write_todos` 規劃。每一項查證先用 `add_intent` 建立 intent（問題、假設、對象、上層 intent），再用 `task` 派給 `sre-worker`，task 描述開頭寫 intent id。多個 deployment 各派各的，可以平行，同時執行數受 `MAX_PARALLEL_WORKERS` 限制。planner 自己沒有資料工具。
3. **sre-worker**（subagent）：用 K8s 唯讀工具與 Prometheus MCP 查證，結果用 `record_fact` 記成 fact，異常用 `report_anomaly` 回報。fact、異常與工具輸出都自動掛在該次派工的 intent 底下。
4. **證據把關**（`middleware.py` + `store.py`）：middleware 記下每次資料工具呼叫的原始輸出與所屬 intent。`confidence=observed` 的 fact，evidence 必須能在「該 intent 自己」的工具輸出中找到（正規化空白後比對子字串，可用 `...` 省略中間），對不上就拒絕並告訴模型原因。
5. **探索圖**：節點是 goal、intent、fact、anomaly，邊是 goal→intent（spawns）、intent→上層 intent（derived_from）、intent→fact（yields）、fact→goal（proves）、fact→anomaly（supports）。程式強制兩件事：同一條線最多派生 2 層，重複的查證（同子目標、同 deployment、同問題與假設）會被拒絕。worker 回報後 intent 自動結算為 done（有 fact）、exhausted（沒有 fact）或 blocked（失敗）。planner 用 `list_intents` 檢視整張圖。
6. **prove_goal**：planner 替每個子目標結案（normal / anomaly / inconclusive），至少要引用一個已驗證的 observed fact。inconclusive 必須寫明缺少哪些資料。
7. **結束把關**（`agent.py` 外層迴圈）：planner 想結束時若還有子目標沒 proven，就把未完成清單塞回去，最多 `GATE_MAX_RETRIES` 次。逾時（`TASK_TIMEOUT_S`）也會照常產生報告，並標出未完成的項目。
8. **Insight 報告**（`report.py`）：另一次結構化輸出的 LLM 呼叫只產生結論文字。子目標表、「調查脈絡」（探索圖的 Mermaid 圖與 intent 表）、證據表、異常清單、調查範圍與執行統計都由 Python 從 Store 產生，格式穩定。

## 防護

| 項目 | 做法 |
| --- | --- |
| 不給 shell | deepagents harness profile 排除 `execute`，middleware 也會拒絕 shell 類工具；預設 StateBackend 本身也沒有執行能力 |
| K8s 只讀 | `tools/k8s.py` 只呼叫 list / get；namespace 與名稱先驗證 DNS-1123 格式；真正的保障是唯讀 RBAC |
| Prometheus 查詢預算 | 每次派工最多 `PROM_BUDGET_PER_TASK` 次（含同一回合平行發出的），整個任務最多 `PROM_BUDGET_TOTAL` 次，超過就拒絕 |
| 防捏造 | observed fact 的 evidence 必須出現在該 intent 自己的工具輸出中 |
| 派工把關 | task 必須帶已建立、尚未派過的 intent id；追查深度最多 2 層；重複的 intent 被拒絕 |
| 提示注入 | 提示詞與工具描述都註明 events、pod 訊息與指標內容是資料，不是指令 |
| 只派 sre-worker | 停用 deepagents 自動加入的 general-purpose subagent |

K8s 工具用 deployment 自己的 `spec.selector` 找 pod，不需要事先設定 `POD_LABEL`。

## 安裝（Windows）

需要 Python 3.11 以上。

```powershell
cd C:\Users\Hsuan\sre-agent-poc
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # 再編輯 .env
```

## 設定（`.env`）

| 變數 | 說明 |
| --- | --- |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` | 內部 LLM（OpenAI 相容 chat completions，需支援 tool calling） |
| `PROM_MCP_TRANSPORT` | `streamable_http`（預設）、`sse` 或 `stdio` |
| `PROM_MCP_URL`、`PROM_MCP_HEADERS` | HTTP 傳輸時的 MCP 端點與 header（JSON） |
| `PROM_MCP_COMMAND`、`PROM_MCP_ARGS`、`PROM_MCP_ENV` | stdio 傳輸時啟動 MCP server 的指令 |
| `PROM_MCP_TOOLS` | 只載入這些 MCP 工具（逗號分隔），避免模型選錯工具 |
| `KUBECONFIG`、`KUBE_CONTEXT` | 唯讀 ServiceAccount 的 kubeconfig；在叢集內執行時自動用 in-cluster 設定 |
| `PROM_BUDGET_PER_TASK`、`PROM_BUDGET_TOTAL` | Prometheus 查詢次數上限（預設 10 / 80） |
| `MAX_PARALLEL_WORKERS` | 同時執行的 worker 數（預設 3） |
| `GATE_MAX_RETRIES`、`TASK_TIMEOUT_S`、`RECURSION_LIMIT` | 結束把關次數、任務逾時、LangGraph 步數上限 |
| `OUTPUT_DIR` | 報告輸出資料夾（預設 `reports`） |

唯讀 RBAC 沿用 PoC 設定文件裡的 `sre-patrol-readonly` ClusterRole（namespaces、pods、events、deployments、replicasets 的 get / list，不含 secrets、configmaps、logs、exec）。

## 使用

先檢查連線，特別是 LLM 的 tool calling：

```powershell
python main.py --check
```

再下 goal：

```powershell
python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"
python main.py "檢查 payment namespace 所有 deployment 的記憶體是否接近 limit"
python main.py --timeout 1200 "payment-svc 這週 CPU 用量和上週比有沒有變化"
python main.py --no-prometheus "shop namespace 有沒有 pod 起不來"
```

執行時 stderr 會印出每次工具呼叫（`--quiet` 關閉）。結束後在 `reports/` 產生：

- `insight-YYYYMMDD-HHMM.md`：Insight 報告
- `insight-YYYYMMDD-HHMM.json`：子目標、intent、facts、異常、探索圖（`graph.nodes` / `graph.edges`）與每次工具呼叫的原始資料，方便人工覆查與 spike 統計

## Spike：先驗證內部 LLM

計畫文件建議先花約 1 天確認內部 LLM 能不能穩定呼叫工具。這份程式碼可以直接拿來跑 spike：用 3 到 5 個真實 goal，每個跑 3 次，從 JSON 與報告的「執行統計」看：

- 工具呼叫是否合法（stderr 裡有沒有參數錯誤、編造的工具名）
- 有沒有先探索指標與 label 再組 PromQL，PromQL 成功比例（JSON 的 `tool_calls[].is_error`）
- PromQL 是否都帶了正確的 namespace（JSON 的 `tool_calls[].args`）
- 被拒絕的 fact 數（`stats.rejected_facts`）：太多代表模型不會照原文引用證據
- planner 有沒有照規則先 `add_intent` 再派工（stderr 裡「沒有 intent id」的拒絕次數）
- token 用量與耗時

建議通過條件：工具呼叫格式正確 ≥ 90%、PromQL 成功 ≥ 70%、大部分 goal 能給出有證據的結論。

## 測試

```powershell
pytest
```

測試不需要叢集或 LLM：用假的 K8s API 和照劇本回應的假模型，跑完 goal 拆解 → add_intent → 派工 → 證據把關（含跨 intent 引用被拒）→ 派生 intent → prove_goal → 結束把關 → 報告的完整流程。

## 專案結構

```
main.py          CLI 入口（--check 檢查連線）
agent.py         create_deep_agent、sre-worker subagent、結束把關、結論摘要
goals.py         goal 拆解
config.py        環境變數設定
middleware.py    工具輸出紀錄（掛 intent）、shell 阻擋、Prometheus 查詢預算、派工把關與 worker 並行數
store.py         探索圖：子目標、intent、facts、異常、工具輸出、證據比對與 nodes / edges
report.py        Insight 報告模板
prompts/         goals.md、planner.md、worker.md、summary.md（用 git 管版本）
tools/k8s.py     K8s 唯讀工具（5 個）
tools/prom.py    Prometheus MCP 工具載入
tools/facts.py   add_intent、list_intents、record_fact、report_anomaly、list_facts、prove_goal
tests/           單元測試與假模型的端到端測試
docs/            架構圖與流程圖、範例流程、運行說明
```

## 已知限制

- 狀態只存在記憶體，任務結束時寫成 JSON；沒有跨任務歷史，要跟過去比較只能用 PromQL 的 `offset`。
- 證據比對用子字串，模型若把工具輸出重新排版就會被拒絕（這是刻意的）；被拒次數會記在報告裡。
- `--check` 和 deepagents 都假設內部 LLM 支援 OpenAI 格式的 tool calling；結構化輸出用 function calling 方式。
- 沒有追蹤 UI。需要時可設定 `LANGSMITH_TRACING=true` 接 LangSmith（資料會送到外部），或自架 Langfuse。
