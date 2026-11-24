"""測試用的假物件：照劇本回應的 chat model、假的 K8s API。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda


class ScriptedChatModel(BaseChatModel):
    """依序回傳 responses 裡的 AIMessage；with_structured_output 依序回傳 structured 裡的物件。"""

    responses: list = []
    structured: list = []
    seen: list = []  # 每次呼叫收到的 messages，方便斷言

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        if not self.responses:
            raise RuntimeError("劇本已用完，模型被多呼叫了一次")
        message = self.responses.pop(0)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        def pop(_input):
            if not self.structured:
                raise RuntimeError("沒有結構化輸出的劇本")
            item = self.structured.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return RunnableLambda(pop)


def call(name: str, args: dict, cid: str) -> dict:
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


def ai(*calls: dict, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


NOW = datetime.now(timezone.utc)


def _container(name="app", image="registry/order-svc:1.4.2", mem_limit="2Gi"):
    return NS(
        name=name,
        image=image,
        resources=NS(requests={"cpu": "200m", "memory": "1Gi"}, limits={"cpu": "1", "memory": mem_limit}),
    )


def fake_deployment(name="order-svc"):
    return NS(
        metadata=NS(
            name=name,
            creation_timestamp=NOW - timedelta(days=30),
            annotations={"deployment.kubernetes.io/revision": "7"},
            generation=7,
        ),
        spec=NS(
            replicas=3,
            selector=NS(match_labels={"app": name}),
            strategy=NS(type="RollingUpdate"),
            template=NS(spec=NS(containers=[_container()])),
        ),
        status=NS(
            ready_replicas=2,
            updated_replicas=3,
            available_replicas=2,
            unavailable_replicas=1,
            observed_generation=7,
            conditions=[
                NS(type="Available", status="True", reason="MinimumReplicasAvailable",
                   last_update_time=NOW - timedelta(hours=3), message="Deployment has minimum availability."),
            ],
        ),
    )


def fake_pod(name, restarts, reason="OOMKilled"):
    terminated = NS(reason=reason, exit_code=137, started_at=NOW - timedelta(hours=2), finished_at=NOW - timedelta(hours=1))
    return NS(
        metadata=NS(name=name, creation_timestamp=NOW - timedelta(days=1)),
        spec=NS(node_name="node-a"),
        status=NS(
            phase="Running",
            container_statuses=[
                NS(name="app", ready=restarts == 0, restart_count=restarts,
                   state=NS(waiting=NS(reason="CrashLoopBackOff") if restarts else None),
                   last_state=NS(terminated=terminated if restarts else None)),
            ],
        ),
    )


class FakeCore:
    def __init__(self):
        self.calls = []

    def list_namespace(self, **kw):
        self.calls.append(("list_namespace", kw))
        return NS(items=[NS(metadata=NS(name=n, creation_timestamp=NOW - timedelta(days=90)), status=NS(phase="Active"))
                         for n in ("default", "shop", "kube-system")])

    def list_namespaced_pod(self, namespace, label_selector=None, **kw):
        self.calls.append(("list_namespaced_pod", namespace, label_selector))
        return NS(items=[fake_pod("order-svc-7d9f-abcde", 7), fake_pod("order-svc-7d9f-fghij", 0)])

    def list_namespaced_event(self, namespace, field_selector=None, **kw):
        self.calls.append(("list_namespaced_event", namespace, field_selector))
        mk = lambda obj, reason, msg, mins: NS(
            involved_object=NS(kind="Pod", name=obj), reason=reason, message=msg, count=3,
            last_timestamp=NOW - timedelta(minutes=mins), event_time=None, first_timestamp=None)
        return NS(items=[
            mk("order-svc-7d9f-abcde", "BackOff", "Back-off restarting failed container app", 5),
            mk("cart-svc-55-xyz", "Unhealthy", "Readiness probe failed", 10),
        ])


class FakeApps:
    def __init__(self):
        self.calls = []

    def list_namespaced_deployment(self, namespace, **kw):
        self.calls.append(("list_namespaced_deployment", namespace))
        return NS(items=[fake_deployment("order-svc"), fake_deployment("cart-svc")])

    def read_namespaced_deployment(self, name, namespace, **kw):
        self.calls.append(("read_namespaced_deployment", name, namespace))
        if name == "missing":
            raise _ApiException(404, "Not Found")
        return fake_deployment(name)

    def list_namespaced_replica_set(self, namespace, label_selector=None, **kw):
        rs = lambda rev, hours: NS(
            metadata=NS(name=f"order-svc-{rev}", annotations={"deployment.kubernetes.io/revision": str(rev)},
                        creation_timestamp=NOW - timedelta(hours=hours),
                        owner_references=[NS(kind="Deployment", name="order-svc")]),
            spec=NS(replicas=3 if rev == 7 else 0, template=NS(spec=NS(containers=[_container()]))),
            status=NS(ready_replicas=2 if rev == 7 else 0),
        )
        return NS(items=[rs(6, 72), rs(7, 3)])


class _ApiException(Exception):
    def __init__(self, status, reason):
        super().__init__(f"({status}) {reason}")
        self.status = status
        self.reason = reason
