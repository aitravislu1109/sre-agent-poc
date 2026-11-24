"""CLI 入口：python main.py "<goal>"

範例：
  python main.py "order-svc 昨晚 22:00 後 pod 一直重啟，找出原因"
  python main.py --timeout 1200 "檢查 payment namespace 所有 deployment 的記憶體是否接近 limit"
  python main.py --check        # 只檢查 LLM、K8s、Prometheus MCP 是否連得上
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from config import Settings


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SRE 調查 agent（deepagents PoC，只讀不寫）")
    parser.add_argument("goal", nargs="?", help="一句話的調查目標")
    parser.add_argument("--timeout", type=int, help="整次任務逾時秒數（預設 TASK_TIMEOUT_S 或 900）")
    parser.add_argument("--model", help="覆寫 LLM_MODEL")
    parser.add_argument("--output-dir", help="報告輸出資料夾（預設 OUTPUT_DIR 或 reports）")
    parser.add_argument("--no-prometheus", action="store_true", help="不載入 Prometheus MCP（只用 K8s 工具）")
    parser.add_argument("--quiet", action="store_true", help="不印出每次工具呼叫")
    parser.add_argument("--check", action="store_true", help="只檢查各連線是否正常")
    args = parser.parse_args(argv)
    if not args.check and not args.goal:
        parser.error("請提供 goal，例如：python main.py \"order-svc 為什麼一直重啟\"")
    return args


async def load_tools(settings: Settings, use_prometheus: bool):
    from tools.k8s import K8sReader, make_k8s_tools
    from tools.prom import load_prometheus_tools

    reader = K8sReader.from_kubeconfig(settings.kubeconfig, settings.kube_context, settings.k8s_timeout_s)
    k8s_tools = make_k8s_tools(reader)
    prom_tools = await load_prometheus_tools(settings) if use_prometheus else []
    return reader, k8s_tools, prom_tools


async def check(settings: Settings, use_prometheus: bool) -> int:
    from langchain_core.tools import tool

    from agent import build_model

    ok = True
    print(f"LLM：{settings.llm_base_url or '(OpenAI 預設端點)'}  model={settings.llm_model}")
    try:
        model = build_model(settings)

        @tool
        def ping(text: str) -> str:
            """回傳 text。"""
            return text

        reply = await model.bind_tools([ping], tool_choice="any").ainvoke("請呼叫 ping 工具，text 填 hello")
        if reply.tool_calls:
            print(f"  ✓ tool calling 正常：{reply.tool_calls[0]['name']}({reply.tool_calls[0]['args']})")
        else:
            ok = False
            print(f"  ✗ 模型有回應但沒有呼叫工具（可能不支援 tool calling）：{str(reply.content)[:200]}")
    except Exception as exc:
        ok = False
        print(f"  ✗ {type(exc).__name__}: {exc}")

    print(f"K8s：kubeconfig={settings.kubeconfig or '(預設)'} context={settings.kube_context or '(目前)'}")
    try:
        from tools.k8s import K8sReader

        reader = K8sReader.from_kubeconfig(settings.kubeconfig, settings.kube_context, settings.k8s_timeout_s)
        lines = reader.list_namespaces().splitlines()
        print(f"  ✓ 可列出 namespace（{max(len(lines) - 1, 0)} 個）")
    except Exception as exc:
        ok = False
        print(f"  ✗ {type(exc).__name__}: {str(exc)[:300]}")

    if use_prometheus:
        print(f"Prometheus MCP：transport={settings.prom_mcp_transport} url={settings.prom_mcp_url or '-'} command={settings.prom_mcp_command or '-'}")
        try:
            from tools.prom import load_prometheus_tools

            tools = await load_prometheus_tools(settings)
            print(f"  ✓ 載入 {len(tools)} 個工具：{', '.join(t.name for t in tools)}")
        except Exception as exc:
            ok = False
            print(f"  ✗ {type(exc).__name__}: {str(exc)[:300]}")
    return 0 if ok else 1


async def run(args: argparse.Namespace, settings: Settings) -> int:
    from agent import build_model, run_investigation

    _, k8s_tools, prom_tools = await load_tools(settings, use_prometheus=not args.no_prometheus)
    print(f"工具：K8s {len(k8s_tools)} 個、Prometheus MCP {len(prom_tools)} 個")
    result = await run_investigation(
        args.goal,
        settings,
        build_model(settings),
        k8s_tools,
        prom_tools,
        verbose=not args.quiet,
    )
    s = result.stats
    print("")
    print(f"報告：{result.report_path.resolve()}")
    print(f"原始資料：{result.data_path.resolve()}")
    print(f"耗時 {s.duration_s:.0f} 秒，token 輸入 {s.input_tokens:,} / 輸出 {s.output_tokens:,}，"
          f"未完成子目標 {len(result.store.unproven_goals())} 個")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows 主控台顯示中文
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    load_dotenv()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    settings = Settings.from_env()
    if args.timeout:
        settings.timeout_s = args.timeout
    if args.model:
        settings.llm_model = args.model
    if args.output_dir:
        settings.output_dir = args.output_dir
    if args.check:
        return asyncio.run(check(settings, use_prometheus=not args.no_prometheus))
    return asyncio.run(run(args, settings))


if __name__ == "__main__":
    sys.exit(main())
