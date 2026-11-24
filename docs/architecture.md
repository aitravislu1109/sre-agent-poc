# SRE Agent PoC 架構與流程

依目前程式碼（deepagents 0.7.41）整理。圖檔版本在同資料夾的 `architecture.png`、`flow.png`、`sequence.png`。

## 1. 架構圖

```mermaid
flowchart LR
    user(["使用者<br/>python main.py &quot;goal&quot;"])

    subgraph app["SRE Agent PoC（本機 Python 程序）"]
        direction TB
        main["main.py<br/>CLI、--check、載入 .env"]
        goals["goals.py<br/>goal 拆解（結構化輸出）"]

        subgraph deep["deepagents（agent.py）"]
            direction TB
            planner["主 agent：planner<br/>write_todos、add_intent、list_intents、<br/>list_facts、prove_goal、task"]
            gate["TaskGateMiddleware<br/>task 必須帶 intent id、<br/>結算 intent 狀態、限制並行數"]
            worker["subagent：sre-worker<br/>K8s 工具、Prometheus MCP 工具、<br/>record_fact、report_anomaly、list_facts"]
            audit["ToolAuditMiddleware<br/>擋 shell、Prometheus 查詢預算、<br/>工具輸出掛到 intent"]
            planner --> gate --> worker
            worker --> audit
        end

        store[("store.py：探索圖<br/>goals、intents、facts、anomalies、<br/>tool outputs、nodes / edges")]
        loop["結束把關（agent.py 外層迴圈）<br/>未 proven 的子目標塞回 planner"]
        report["report.py<br/>Insight 報告模板"]

        main --> goals --> planner
        planner --> loop
        audit -- "記錄原始輸出" --> store
        worker -- "fact / 異常（證據比對）" --> store
        planner -- "intent / prove_goal" --> store
        store --> report
    end

    llm[["內部 LLM<br/>OpenAI 相容、tool calling"]]
    k8s[["K8s API<br/>唯讀 RBAC（get / list）"]]
    mcp[["Prometheus MCP server<br/>HTTP 或 stdio"]]
    prom[("Prometheus")]
    out[/"reports/<br/>insight-*.md、insight-*.json"/]

    user --> main
    goals -. LLM 呼叫 .-> llm
    planner -. LLM 呼叫 .-> llm
    worker -. LLM 呼叫 .-> llm
    report -. 結論摘要 .-> llm
    audit -- "tools/k8s.py<br/>kubernetes client" --> k8s
    audit -- "tools/prom.py<br/>langchain-mcp-adapters" --> mcp --> prom
    report --> out
```

所有資料工具都只讀不寫。worker 的工具呼叫一律經過 ToolAuditMiddleware，planner 的派工一律經過 TaskGateMiddleware。

## 2. 流程圖

```mermaid
flowchart TD
    A(["python main.py &quot;goal&quot;"]) --> B["載入 K8s 工具與 Prometheus MCP 工具"]
    B --> C["① goal 拆解<br/>子目標 G1~G4、時間窗、限制、超出範圍"]
    C --> D["② planner 規劃（write_todos）"]
    D --> E["add_intent<br/>問題、假設、對象、上層 intent"]
    E --> E1{"深度 ≤ 2 層<br/>且不重複？"}
    E1 -- 否 --> D
    E1 -- 是 --> F["task：「I#：…」派給 sre-worker<br/>（獨立的 intent 可平行）"]
    F --> F1{"帶有效且未派過的<br/>intent id？"}
    F1 -- 否 --> D
    F1 -- 是 --> G["sre-worker 查證<br/>K8s 工具、Prometheus MCP（先探索指標與 label）"]
    G --> G1{"Prometheus 查詢<br/>未超過預算？"}
    G1 -- 否 --> G2["拒絕查詢，用現有資料收尾"]
    G1 -- 是 --> H["工具輸出記錄到該 intent"]
    G2 --> I
    H --> I["record_fact / report_anomaly"]
    I --> I1{"observed 的 evidence<br/>出現在該 intent 的輸出中？"}
    I1 -- 否 --> I2["拒絕，要求貼原文"] --> G
    I1 -- 是 --> J["fact 掛到 intent；異常依對象、類別、時間窗去重"]
    J --> K["worker 回報<br/>intent 結算為 done / exhausted / blocked"]
    K --> L{"子目標證據足夠？"}
    L -- "否：補查或追查假設" --> E
    L -- 是 --> M["prove_goal<br/>至少一個已驗證的 observed fact"]
    M --> N{"所有子目標 proven？"}
    N -- 否 --> D
    N -- "是，或 planner 想結束" --> O{"③ 結束把關<br/>還有未 proven？"}
    O -- "是，且未超過重推次數" --> D
    O -- "否，或已達上限 / 逾時" --> P["④ 結論摘要（結構化輸出，只產生文字）"]
    P --> Q["⑤ Python 套模板產生報告<br/>insight-*.md + insight-*.json"]
```

## 3. 一次調查的時序

```mermaid
sequenceDiagram
    autonumber
    actor U as 使用者
    participant M as main.py / agent.py
    participant L as 內部 LLM
    participant P as planner
    participant W as sre-worker
    participant S as Store（探索圖）
    participant K as K8s / Prometheus MCP

    U->>M: python main.py "goal"
    M->>L: goal 拆解（結構化輸出）
    L-->>M: 子目標 G1..Gn
    M->>S: 建立子目標
    M->>P: 子目標、時間窗、限制
    P->>S: add_intent(G1, 問題, 假設)
    S-->>P: I1
    P->>W: task「I1：…」
    W->>K: k8s_pods / PromQL …
    K-->>W: 工具輸出
    W->>S: 記錄輸出（掛 I1）
    W->>S: record_fact(evidence)
    S-->>W: 比對 I1 的輸出後接受或拒絕
    W-->>P: 回報結果
    S->>S: I1 結算為 done / exhausted
    P->>S: prove_goal(G1, fact_ids)
    opt 還有子目標未 proven
        M->>P: 結束把關：列出未完成子目標
    end
    M->>L: 結論摘要（結構化輸出）
    M->>U: insight-*.md、insight-*.json
```
