# 運行說明

這份說明從零開始，在 Windows 上把 PoC 跑起來：裝環境、準備唯讀的 K8s 權限、啟動 Prometheus MCP server、設定 `.env`、檢查連線、下 goal、看報告，以及常見問題。

## 0. 先準備好這些

| 項目 | 說明 |
| --- | --- |
| Python 3.11 以上 | deepagents 要求 3.11+，建議 3.12 |
| 叢集存取 | 能用 `kubectl` 連到測試叢集，而且有建立 ServiceAccount 與 ClusterRole 的權限（一次性，可請叢集管理員代做） |
| Prometheus | 有一個可以查詢的 Prometheus，最好已經抓取 kube-state-metrics 與 cAdvisor |
| Prometheus MCP server | 一個只提供查詢工具的 MCP server（見第 3 節） |
| 內部 LLM | OpenAI 相容的 chat completions 端點，支援 tool calling；需要 base URL、API key、模型名稱 |
| Docker（選用） | 用 docker 跑 Prometheus MCP server 時才需要 |

## 1. 安裝

```powershell
cd C:\Users\Hsuan\sre-agent-poc
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pytest
```

`pytest` 不需要叢集或 LLM，大約 1 到 2 秒就跑完，應該顯示 19 passed。這一步用來確認套件版本都裝對。

公司網路如果要走代理，pip 前先設定：

```powershell
$env:HTTPS_PROXY = "http://proxy.example.com:8080"
```

## 2. 準備唯讀的 K8s 權限

PoC 只呼叫 list / get，但真正的保障是 RBAC。建議另外建一個唯讀的 ServiceAccount，不要直接用自己的管理員 kubeconfig。

**2.1 建立 ServiceAccount 與唯讀 ClusterRole**（存成 `rbac.yaml`）：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: sre-patrol-reader
  namespace: artex
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: sre-patrol-readonly
rules:
  - apiGroups: [""]
    resources: ["namespaces", "pods", "events"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: sre-patrol-readonly
subjects:
  - kind: ServiceAccount
    name: sre-patrol-reader
    namespace: artex
roleRef:
  kind: ClusterRole
  name: sre-patrol-readonly
  apiGroup: rbac.authorization.k8s.io
```

```powershell
kubectl create namespace artex      # 已存在就略過
kubectl apply -f rbac.yaml
```

這份權限不含 secrets、configmaps、pods/log、pods/exec，也沒有任何寫入 verb。PoC 用到的就是這 5 種資源。

**2.2 驗證權限**：

```powershell
kubectl auth can-i list pods -A --as=system:serviceaccount:artex:sre-patrol-reader      # yes
kubectl auth can-i list secrets -A --as=system:serviceaccount:artex:sre-patrol-reader   # no
kubectl auth can-i delete pods -A --as=system:serviceaccount:artex:sre-patrol-reader    # no
```

**2.3 產生唯讀的 kubeconfig**：

```powershell
$TOKEN  = kubectl create token sre-patrol-reader -n artex --duration=24h
$SERVER = kubectl config view --minify -o jsonpath="{.clusters[0].cluster.server}"
$CLUSTER = kubectl config view --minify -o jsonpath="{.clusters[0].name}"
kubectl config view --minify --raw -o jsonpath="{.clusters[0].cluster.certificate-authority-data}" > ca.b64

$KC = "$HOME\.kube\sre-readonly.yaml"
kubectl config --kubeconfig=$KC set-cluster $CLUSTER --server=$SERVER
kubectl config --kubeconfig=$KC set clusters.$CLUSTER.certificate-authority-data (Get-Content ca.b64)
kubectl config --kubeconfig=$KC set-credentials sre-patrol-reader --token=$TOKEN
kubectl config --kubeconfig=$KC set-context sre-readonly --cluster=$CLUSTER --user=sre-patrol-reader
kubectl config --kubeconfig=$KC use-context sre-readonly
Remove-Item ca.b64

kubectl --kubeconfig=$KC get pods -A | Select-Object -First 5     # 應該列得出來
kubectl --kubeconfig=$KC get secrets -A                            # 應該 Forbidden
```

`kubectl create token` 產生的 token 有效期很短（這裡是 24 小時），過期後重跑第一行和 `set-credentials` 那行即可。要長期使用時，改用 `kubernetes.io/service-account-token` 類型的 Secret（PoC 設定文件裡有範本）。

## 3. 啟動 Prometheus MCP server

PoC 不綁定特定的 MCP server，只要求它提供這三類查詢工具：列出指標名稱、查指標 metadata 或 label、執行 PromQL（instant，最好也有 range）。worker 靠前兩類工具自己探索有哪些指標可用。

以下用開源的 [pab1it0/prometheus-mcp-server](https://github.com/pab1it0/prometheus-mcp-server) 當例子。環境變數名稱、HTTP 的 port 與路徑可能隨版本不同，以該專案 README 為準。

**做法 A：stdio，由 PoC 自動啟動（最簡單，適合本機試跑）**

`.env` 設定：

```ini
PROM_MCP_TRANSPORT=stdio
PROM_MCP_COMMAND=docker
PROM_MCP_ARGS=run,-i,--rm,-e,PROMETHEUS_URL,ghcr.io/pab1it0/prometheus-mcp-server:latest
PROM_MCP_ENV={"PROMETHEUS_URL": "http://prometheus.example.com:9090"}
```

`PROM_MCP_ARGS` 用逗號分隔。PoC 每次呼叫工具時都會啟動一次 MCP server，所以會比較慢，試跑沒問題。

**做法 B：HTTP，MCP server 常駐（建議，之後放進叢集也是這種方式）**

先依該專案 README 以 HTTP 傳輸啟動，例如：

```powershell
docker run -d --name prom-mcp -p 8080:8080 `
  -e PROMETHEUS_URL=http://prometheus.example.com:9090 `
  -e PROMETHEUS_MCP_SERVER_TRANSPORT=http `
  -e PROMETHEUS_MCP_BIND_HOST=0.0.0.0 `
  ghcr.io/pab1it0/prometheus-mcp-server:latest
```

`.env` 設定：

```ini
PROM_MCP_TRANSPORT=streamable_http
PROM_MCP_URL=http://localhost:8080/mcp
# 需要認證時
# PROM_MCP_HEADERS={"Authorization": "Bearer xxx"}
```

**只載入需要的工具**：第 5 節的 `--check` 會列出 MCP server 提供的所有工具。工具多的話，用 `PROM_MCP_TOOLS` 只保留查詢類，避免模型選錯：

```ini
PROM_MCP_TOOLS=execute_query,execute_range_query,list_metrics,get_metric_metadata
```

（工具名稱以 `--check` 列出的為準。）

Prometheus 連不到時，可以先用 `--no-prometheus` 只靠 K8s 工具跑。

## 4. 設定 `.env`

```powershell
copy .env.example .env
notepad .env
```

最少要填：

```ini
# 內部 LLM
LLM_BASE_URL=https://llm.internal.example.com/v1
LLM_API_KEY=xxxxx
LLM_MODEL=your-model-name

# Prometheus MCP（第 3 節）
PROM_MCP_TRANSPORT=streamable_http
PROM_MCP_URL=http://localhost:8080/mcp

# K8s（第 2 節）
KUBECONFIG=C:\Users\Hsuan\.kube\sre-readonly.yaml
```

其他可調整的值：

| 變數 | 預設 | 什麼時候調 |
| --- | --- | --- |
| `LLM_TIMEOUT_S` | 120 | 內部 LLM 回應很慢時調高 |
| `KUBE_CONTEXT` | kubeconfig 目前的 context | kubeconfig 裡有多個 context 時 |
| `PROM_BUDGET_PER_TASK` | 10 | 每次派工最多幾次 Prometheus 呼叫 |
| `PROM_BUDGET_TOTAL` | 80 | 整個任務最多幾次 Prometheus 呼叫 |
| `MAX_PARALLEL_WORKERS` | 3 | LLM 端點有速率限制時調低 |
| `GATE_MAX_RETRIES` | 3 | 結束把關最多要求 planner 繼續幾次 |
| `TASK_TIMEOUT_S` | 900 | 整個任務逾時秒數；也可用 `--timeout` |
| `RECURSION_LIMIT` | 200 | 出現 GraphRecursionError 時調高 |
| `OUTPUT_DIR` | reports | 報告輸出位置 |

`.env` 已列在 `.gitignore`，不要 commit。

## 5. 檢查連線

```powershell
python main.py --check
```

正常的輸出：

```
LLM：https://llm.internal.example.com/v1  model=your-model-name
  ✓ tool calling 正常：ping({'text': 'hello'})
K8s：kubeconfig=C:\Users\Hsuan\.kube\sre-readonly.yaml context=(目前)
  ✓ 可列出 namespace（12 個）
Prometheus MCP：transport=streamable_http url=http://localhost:8080/mcp command=-
  ✓ 載入 5 個工具：execute_query, execute_range_query, list_metrics, get_metric_metadata, get_targets
```

三項都打勾再往下。LLM 那一項最重要：如果顯示「模型有回應但沒有呼叫工具」，代表這個模型或端點不支援 tool calling，整個 agent 都跑不動。

## 6. 下 goal

```powershell
python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"
```

其他寫法：

| 類型 | 範例 |
| --- | --- |
| 查原因 | `python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"` |
| 全面檢查 | `python main.py "檢查 payment namespace 所有 deployment 的記憶體是否接近 limit"` |
| 比較 | `python main.py --timeout 1200 "payment-svc 這週 CPU 用量和上週比有沒有變化"` |
| 時間線 | `python main.py "整理 order-svc 與 cart-svc 今天 14:00 到 15:00 的事件時間線"` |
| 只用 K8s | `python main.py --no-prometheus "shop namespace 有沒有 pod 起不來"` |

goal 寫得越具體越好：寫出服務名稱、時間點，以及想要的答案形式（原因、風險、時間線）。有 namespace 就寫，可以省下找 namespace 的步驟。

CLI 參數：

| 參數 | 說明 |
| --- | --- |
| `--timeout 秒數` | 覆寫 `TASK_TIMEOUT_S` |
| `--model 名稱` | 覆寫 `LLM_MODEL` |
| `--output-dir 路徑` | 覆寫 `OUTPUT_DIR` |
| `--no-prometheus` | 不載入 Prometheus MCP |
| `--quiet` | 不印出每次工具呼叫 |
| `--check` | 只檢查連線 |

執行時，stderr 會即時印出每一步：`①` 是拆解 goal，`②` 是派工，`→` 表示呼叫工具，`←` 表示工具回傳，`⇒` / `⇐` 是派出和結算 intent，`✗` 表示被拒絕。完整範例見 [example-walkthrough.md](example-walkthrough.md)。

## 7. 看報告

結束時終端機會印出：

```
報告：C:\Users\Hsuan\sre-agent-poc\reports\insight-20261124-0930.md
原始資料：C:\Users\Hsuan\sre-agent-poc\reports\insight-20261124-0930.json
耗時 214 秒，token 輸入 182,340 / 輸出 9,875，未完成子目標 0 個
```

**`insight-*.md`** 建議這樣讀：

1. **結論**與**信心**：直接回答 goal。
2. **子目標**：每個子目標是否完成、結論是什麼、引用了哪些 fact。
3. **調查脈絡**：探索圖。看 planner 驗證了哪些假設、哪些 intent 沒有產出。用 VS Code（裝 Markdown Preview Mermaid Support）或 GitHub 開啟就能看到圖。
4. **證據**：每個 fact 的原文都在折疊區塊裡，可以拿來對照工具輸出。
5. **異常**、**建議下一步**、**調查範圍**：哪些項目沒查到、為什麼沒查到。

**`insight-*.json`** 用來人工覆查與統計：

| 欄位 | 內容 |
| --- | --- |
| `goals`、`intents`、`facts`、`anomalies` | 完整紀錄 |
| `graph.nodes`、`graph.edges` | 探索圖 |
| `tool_calls[]` | 每次工具呼叫的 intent、工具、參數、輸出（截斷到 4000 字元）、是否失敗 |
| `stats` | 工具呼叫次數、失敗次數、Prometheus 次數、被拒絕的 fact 數 |

快速統計 PromQL 成功率（PowerShell）：

```powershell
$d = Get-Content reports\insight-20261124-0930.json -Raw -Encoding UTF8 | ConvertFrom-Json
$q = $d.tool_calls | Where-Object { $_.tool -like "*query*" }
"PromQL 共 $($q.Count) 次，失敗 $(($q | Where-Object is_error).Count) 次"
$q | Select-Object intent_id, @{n="query";e={$_.args.query}} | Format-Table -Wrap
```

## 8. 常見問題

| 症狀 | 可能原因與處理 |
| --- | --- |
| `--check` 顯示「模型有回應但沒有呼叫工具」 | 模型或端點不支援 tool calling。換模型，或確認端點有開 function calling |
| `OpenAIConnectionError` / 逾時 | `LLM_BASE_URL` 要包含 `/v1`；確認代理設定；調高 `LLM_TIMEOUT_S` |
| 報告的執行問題寫「goal 拆解失敗，改用單一子目標」 | 模型的結構化輸出（function calling）不穩定。任務仍會繼續，但子目標比較粗 |
| 報告的執行問題寫「結論摘要失敗」 | 同上，結論改由各子目標的結論拼成，信心標為低 |
| `ConfigException: Service host/port is not set` | 找不到 kubeconfig。確認 `KUBECONFIG` 路徑，或 `%USERPROFILE%\.kube\config` 存在 |
| 工具回傳 `ERROR K8s API 401` | token 過期，重新 `kubectl create token`（第 2.3 節） |
| 工具回傳 `ERROR K8s API 403` | RBAC 不足，確認第 2.1 節的 ClusterRole 與 binding |
| 工具回傳 `ERROR 參數不合法` | 模型填的 namespace 或名稱不是合法 K8s 名稱，多半是模型自己會修正；一直出現就要調提示詞 |
| `PROM_MCP_TOOLS 裡的 [...] 不存在` | 名稱跟 MCP server 提供的不一致，用 `--check` 看實際名稱 |
| Prometheus MCP 連不上 | HTTP：確認 `PROM_MCP_URL` 的 port 與路徑（常見是 `/mcp`）；stdio：先手動跑一次 docker 指令，確認映像拉得下來 |
| 一直出現「已達本次派工的 Prometheus 查詢上限」 | 模型探索太多次，或查詢一直失敗。看 JSON 的 `tool_calls`；必要時調高 `PROM_BUDGET_PER_TASK` |
| 很多「evidence … 找不到」 | 模型沒有照原文引用。看 `stats.rejected_facts`；這是 spike 要觀察的指標之一 |
| 很多「task 描述沒有 intent id」 | 模型沒有先 `add_intent`。可以在 `prompts/planner.md` 加強說明 |
| 報告寫「任務逾時」 | 調高 `--timeout`，或把 goal 寫得更具體 |
| `GraphRecursionError` | 調高 `RECURSION_LIMIT` |
| 撞到 LLM 速率限制（429） | 調低 `MAX_PARALLEL_WORKERS` |
| 終端機中文亂碼 | 改用 Windows Terminal，或先執行 `chcp 65001` |

## 9. 調整行為

| 想改什麼 | 改哪裡 |
| --- | --- |
| goal 怎麼拆解 | `prompts/goals.md` |
| planner 的派工與追查策略 | `prompts/planner.md` |
| worker 的查詢規則、記錄方式、已知的正常情況 | `prompts/worker.md` |
| 報告結論的寫法 | `prompts/summary.md` |
| 報告格式 | `report.py` |
| 追查深度上限 | `store.py` 的 `MAX_INTENT_DEPTH` |

提示詞是一般的 Markdown 檔，用 git 管版本。改完先跑 `pytest`，再用同一個 goal 重跑比較報告。
