from tests.fakes import FakeApps, FakeCore
from tools.k8s import K8sReader, make_k8s_tools


def tools():
    reader = K8sReader(FakeCore(), FakeApps())
    return reader, {t.name: t for t in make_k8s_tools(reader)}


def test_list_namespaces_table():
    _, t = tools()
    out = t["list_namespaces"].invoke({})
    assert "NAME" in out and "shop" in out


def test_pods_use_deployment_selector_and_show_oom():
    reader, t = tools()
    out = t["k8s_pods"].invoke({"namespace": "shop", "name": "order-svc"})
    assert ("list_namespaced_pod", "shop", "app=order-svc") in reader.core.calls
    assert "reason=OOMKilled exitCode=137" in out
    assert "CrashLoopBackOff" in out


def test_deployment_shows_limits_and_rollout_history():
    _, t = tools()
    out = t["k8s_deployment"].invoke({"namespace": "shop", "name": "order-svc"})
    assert "memory req=1Gi lim=2Gi" in out
    assert "order-svc-7" in out and "order-svc-6" in out
    assert out.index("order-svc-7") < out.index("order-svc-6")  # 新 revision 在前


def test_events_filtered_by_prefix():
    _, t = tools()
    out = t["k8s_events"].invoke({"namespace": "shop", "name_prefix": "order-svc"})
    assert "BackOff" in out and "cart-svc" not in out


def test_invalid_names_rejected_without_api_call():
    reader, t = tools()
    out = t["k8s_pods"].invoke({"namespace": "Shop;rm", "name": "x"})
    assert out.startswith("ERROR 參數不合法")
    assert not reader.core.calls and not reader.apps.calls


def test_api_errors_returned_as_text():
    _, t = tools()
    out = t["k8s_deployment"].invoke({"namespace": "shop", "name": "missing"})
    assert out == "ERROR K8s API 404 Not Found"
