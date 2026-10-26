"""MCP 协议层冒烟测试。

不用单元测试框架，直接起一次真实的 stdio 会话：
  拉起 server 进程 → initialize 握手 → list_tools → call_tool → 校验返回。

这比「直接 import 函数」强的地方：能证明它真的是一个 MCP server
（握手、能力协商、schema 下发、JSON-RPC 往返都真实走通）。

用法：
    ./.venv/Scripts/python.exe tests/smoke_test.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"

from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

SERVERS = {
    "quant-core": (ROOT / "servers" / "quant_core.py", [
        ("list_strategy_catalog", {}),
        ("list_pools", {}),
        ("get_risk_config", {}),
    ]),
    "quant-portfolio": (ROOT / "servers" / "quant_portfolio.py", [
        ("get_positions", {}),
        ("get_equity_curve", {"days": 5}),
        ("health_check", {}),
    ]),
    "astock-agent": (ROOT / "servers" / "astock_agent.py", [
        ("check_backend", {}),
    ]),
}


async def probe(name: str, script: Path, calls: list[tuple[str, dict]]) -> dict:
    params = StdioServerParameters(command=str(PY), args=[str(script)], env=None)
    result = {"server": name, "ok": False, "tools": [], "calls": []}
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                # SDK 2.x 用蛇形字段，1.x 用驼峰；两边都兼容，避免报告里出现 "?"
                result["protocol"] = (
                    getattr(init, "protocol_version", None)
                    or getattr(init, "protocolVersion", None)
                    or "unknown"
                )
                server_info = getattr(init, "server_info", None) or getattr(
                    init, "serverInfo", None
                )
                result["server_name"] = getattr(server_info, "name", name)

                listed = await session.list_tools()
                tools = getattr(listed, "tools", [])
                result["tools"] = [t.name for t in tools]
                result["tool_schema_ok"] = all(
                    (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None))
                    is not None
                    for t in tools
                )

                for tool_name, args in calls:
                    resp = await session.call_tool(tool_name, args)
                    payload = None
                    for block in getattr(resp, "content", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            try:
                                payload = json.loads(text)
                            except json.JSONDecodeError:
                                payload = {"raw": text[:200]}
                            break
                    result["calls"].append({
                        "tool": tool_name,
                        "is_error": getattr(resp, "is_error", None)
                        if getattr(resp, "is_error", None) is not None
                        else getattr(resp, "isError", False),
                        "ok": bool(payload.get("ok")) if isinstance(payload, dict) else None,
                        # 工具自己返回 ok=false 但结构完整，属于「正确降级」，
                        # 只要 isError 没置位就算协议层通过
                        "degraded": isinstance(payload, dict) and payload.get("ok") is False,
                        "preview": json.dumps(payload, ensure_ascii=False)[:180]
                        if payload is not None else None,
                    })
                result["ok"] = all(c["is_error"] in (False, None) for c in result["calls"])
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
    return result


async def main() -> int:
    reports = []
    for name, (script, calls) in SERVERS.items():
        print(f"\n=== {name} ===", flush=True)
        r = await probe(name, script, calls)
        reports.append(r)
        if "error" in r:
            print(f"  [FAIL] {r['error']}", flush=True)
            continue
        print(f"  protocol={r.get('protocol')}  server={r.get('server_name')}", flush=True)
        print(f"  tools({len(r['tools'])}): {', '.join(r['tools'])}", flush=True)
        print(f"  schema 完整: {r.get('tool_schema_ok')}", flush=True)
        for c in r["calls"]:
            if c["ok"]:
                flag = "PASS"
            elif c.get("degraded"):
                flag = "PASS*"
            else:
                flag = "FAIL"
            print(f"  [{flag}] {c['tool']}  {c['preview']}", flush=True)

    out = ROOT / "tests" / "smoke_report.json"
    out.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告写入 {out}", flush=True)

    passed = sum(1 for r in reports if r.get("ok"))
    print(f"结果: {passed}/{len(reports)} 个 server 通过", flush=True)
    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
