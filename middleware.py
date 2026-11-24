"""工具呼叫的 middleware：

- ToolAuditMiddleware：記錄每次資料工具（K8s、Prometheus MCP）的參數與原始輸出，供 record_fact 比對證據；
  擋下 shell 類工具；執行 Prometheus 查詢次數上限（每次派工與整個任務）。
- TaskGateMiddleware：主 agent 的 task 必須帶已建立的 intent id；結算 intent 狀態；限制同時派出的 worker 數。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from store import INTENT_ID_RE, Store, current_intent

BLOCKED_TOOLS = {"execute", "shell", "bash"}


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _log(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _short(args: Any, limit: int = 160) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class ToolAuditMiddleware(AgentMiddleware):
    def __init__(
        self,
        store: Store,
        agent_label: str,
        data_tool_names: set[str],
        prom_tool_names: set[str],
        prom_budget_per_task: int,
        prom_budget_total: int,
        verbose: bool = True,
    ):
        super().__init__()
        self.store = store
        self.agent_label = agent_label
        self.data_tool_names = data_tool_names
        self.prom_tool_names = prom_tool_names
        self.prom_budget_per_task = prom_budget_per_task
        self.prom_budget_total = prom_budget_total
        self.verbose = verbose

    @property
    def name(self) -> str:  # 每個 agent 一個實例，名稱要不同才不會互相取代
        return f"ToolAuditMiddleware[{self.agent_label}]"

    # ---------- 共用邏輯 ----------
    def _precheck(self, request: Any) -> ToolMessage | None:
        call = request.tool_call
        name = call["name"]
        if self.verbose:
            _log(f"  [{self.agent_label}] → {name} {_short(call.get('args', {}))}")
        if name.lower() in BLOCKED_TOOLS:
            return self._reject(call, "此任務只觀察不修改，shell 類工具一律停用。")
        if name in self.prom_tool_names:
            used = self._prom_calls_before(request.state, call.get("id"))
            if used >= self.prom_budget_per_task:
                return self._reject(
                    call,
                    f"已達本次派工的 Prometheus 查詢上限（{self.prom_budget_per_task} 次）。"
                    "請用已取得的資料記錄 fact，查不到的部分寫成「無法判斷」。",
                )
            if not self.store.try_consume_prom_budget(self.prom_budget_total):
                return self._reject(
                    call,
                    f"已達整個任務的 Prometheus 查詢上限（{self.prom_budget_total} 次）。請用現有資料收尾。",
                )
        return None

    def _prom_calls_before(self, state: Any, call_id: str | None) -> int:
        """數這次派工中、排在目前這個呼叫之前的 Prometheus 工具呼叫（含同一回合平行發出的）。

        subagent 的 messages 只屬於這一次派工，所以直接數 AIMessage 裡的 tool_calls 即可。
        """
        messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
        count = 0
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for tc in message.tool_calls or []:
                if tc.get("id") == call_id:
                    return count
                if tc.get("name") in self.prom_tool_names:
                    count += 1
        return count

    def _reject(self, call: dict, reason: str) -> ToolMessage:
        if self.verbose:
            _log(f"  [{self.agent_label}] ✗ {call['name']} 被拒絕：{reason}")
        return ToolMessage(content=f"ERROR 拒絕執行：{reason}", tool_call_id=call["id"], name=call["name"], status="error")

    def _intent_of(self, request: Any) -> str | None:
        """worker 收到的第一則訊息就是 task 描述，開頭寫著 intent id（例如「I3：」）。"""
        messages = request.state.get("messages", []) if isinstance(request.state, dict) else getattr(request.state, "messages", [])
        for message in messages:
            if getattr(message, "type", None) == "human":
                match = INTENT_ID_RE.search(message_text(message.content))
                if match and match.group(0) in self.store.intents:
                    return match.group(0)
                return None
        return None

    def _record(self, request: Any, result: Any, intent_id: str | None) -> None:
        call = request.tool_call
        if call["name"] not in self.data_tool_names:
            return
        if isinstance(result, ToolMessage):
            text = message_text(result.content)
            is_error = result.status == "error" or text.startswith("ERROR")
        else:  # Command 等其他型別
            text = str(result)
            is_error = False
        self.store.record_tool_output(self.agent_label, call["name"], call.get("args", {}), text, is_error, intent_id)
        if self.verbose:
            _log(f"  [{self.agent_label}/{intent_id or '-'}] ← {call['name']} {len(text)} 字元{'（錯誤）' if is_error else ''}")

    def _error_result(self, request: Any, exc: Exception) -> ToolMessage:
        call = request.tool_call
        return ToolMessage(
            content=f"ERROR 工具執行失敗：{type(exc).__name__}: {str(exc)[:500]}",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )

    # ---------- hooks ----------
    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        rejected = self._precheck(request)
        if rejected is not None:
            return rejected
        intent_id = self._intent_of(request)
        token = current_intent.set(intent_id)
        try:
            result = handler(request)
        except Exception as exc:
            result = self._error_result(request, exc)
        finally:
            current_intent.reset(token)
        self._record(request, result, intent_id)
        return result

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        rejected = self._precheck(request)
        if rejected is not None:
            return rejected
        intent_id = self._intent_of(request)
        token = current_intent.set(intent_id)
        try:
            result = await handler(request)
        except Exception as exc:
            result = self._error_result(request, exc)
        finally:
            current_intent.reset(token)
        self._record(request, result, intent_id)
        return result


class TaskGateMiddleware(AgentMiddleware):
    """主 agent 的 task（派工）把關：

    - task 描述必須帶一個用 add_intent 建立、尚未派過的 intent id，否則拒絕。
    - worker 回報後結算 intent 狀態（有 fact = done、沒有 = exhausted、失敗 = blocked）。
    - 限制同一時間執行的 worker 數，避免撞到內部 LLM 的速率限制。
    """

    def __init__(self, store: Store, max_parallel: int, verbose: bool = True):
        super().__init__()
        self.store = store
        self.max_parallel = max(1, max_parallel)
        self.verbose = verbose
        self._async_sem: asyncio.Semaphore | None = None
        self._sync_sem = threading.Semaphore(self.max_parallel)

    def _sem(self) -> asyncio.Semaphore:
        if self._async_sem is None:
            self._async_sem = asyncio.Semaphore(self.max_parallel)
        return self._async_sem

    def _start(self, call: dict) -> tuple[str | None, ToolMessage | None]:
        description = str(call.get("args", {}).get("description", ""))
        match = INTENT_ID_RE.search(description)
        if not match:
            reason = "task 描述沒有 intent id。先用 add_intent 建立 intent，再在描述開頭寫「I1：…」。"
            return None, ToolMessage(content=f"ERROR 拒絕執行：{reason}", tool_call_id=call["id"], name="task", status="error")
        ok, reason = self.store.start_intent(match.group(0))
        if not ok:
            return None, ToolMessage(content=f"ERROR 拒絕執行：{reason}", tool_call_id=call["id"], name="task", status="error")
        if self.verbose:
            _log(f"  [planner] ⇒ 派工 {match.group(0)}")
        return match.group(0), None

    def _finish(self, intent_id: str, result: Any) -> Any:
        text = message_text(result.content) if isinstance(result, ToolMessage) else str(result)
        failed = isinstance(result, ToolMessage) and (result.status == "error" or text.startswith("ERROR"))
        intent = self.store.finish_intent(intent_id, failed=failed, note=text)
        if self.verbose and intent:
            _log(f"  [planner] ⇐ {intent_id} {intent.status}")
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        if request.tool_call["name"] != "task":
            return handler(request)
        intent_id, rejected = self._start(request.tool_call)
        if rejected is not None:
            return rejected
        with self._sync_sem:
            try:
                result = handler(request)
            except Exception as exc:
                self.store.finish_intent(intent_id, failed=True, note=str(exc))
                raise
        return self._finish(intent_id, result)

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if request.tool_call["name"] != "task":
            return await handler(request)
        intent_id, rejected = self._start(request.tool_call)
        if rejected is not None:
            return rejected
        async with self._sem():
            try:
                result = await handler(request)
            except Exception as exc:
                self.store.finish_intent(intent_id, failed=True, note=str(exc))
                raise
        return self._finish(intent_id, result)
