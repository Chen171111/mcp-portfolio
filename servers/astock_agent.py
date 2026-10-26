"""MCP Server: astock-agent gateway

把毕业设计「A 股智能投研 Agent」（Spring Boot 3 + Spring AI + Vue 3）的
HTTP/SSE 接口包装成 MCP 工具，让 Python 生态的 Agent 能直接复用这个 Java 侧能力。

为什么要这层：Java 侧的 ToolRegistry 里注册的是 Spring AI 的 `@Tool` 方法，
只能在 JVM 内部被模型调用；套一层 MCP 之后，同一批能力对外变成
**跨语言、跨进程、可被任意 MCP 客户端发现**的标准工具。

前置：后端需先启动
    cd E:\\MyWorkAndProject\\astock-agent\\backend && mvn spring-boot:run
默认监听 127.0.0.1:8080；如开启鉴权，设 ASTOCK_TOKEN 环境变量。

启动：python servers/astock_agent.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

from _common import wrap_error

BASE_URL = os.environ.get("ASTOCK_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
TOKEN = os.environ.get("ASTOCK_TOKEN", "")
TIMEOUT = float(os.environ.get("ASTOCK_TIMEOUT", "180"))

server = MCPServer(
    name="astock-agent",
    version="1.0.0",
    instructions=(
        "调用 A 股智能投研 Agent 做自然语言投研问答。"
        "Agent 会自主决定调用哪些数据工具（行情/技术指标/财务/公告），"
        "返回最终答案与完整推理轨迹。需先启动 Java 后端。"
    ),
)


def _headers(extra: dict | None = None) -> dict:
    h = {"Accept": "application/json", "Content-Type": "application/json"}
    if TOKEN:
        h["X-Auth-Token"] = TOKEN
    if extra:
        h.update(extra)
    return h


def _get(path: str) -> object:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


@server.tool(
    description=(
        "探测 Java 后端是否在线，返回服务地址与状态。"
        "调用其他工具前建议先跑这个，避免拿到连接错误。"
    )
)
def check_backend() -> dict:
    try:
        data = _get("/api/auth/status")
        return {"ok": True, "base_url": BASE_URL, "reachable": True, "auth": data}
    except urllib.error.URLError as e:
        return {
            "ok": False,
            "base_url": BASE_URL,
            "reachable": False,
            "hint": "后端未启动。运行: cd astock-agent/backend && mvn spring-boot:run",
            "error": str(e.reason if hasattr(e, "reason") else e),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("check_backend", e)


@server.tool(description="列出 Java 侧 Agent 已注册的工具（名字 / 描述 / 参数 schema）。")
def list_agent_tools() -> dict:
    try:
        data = _get("/api/agent/tools")
        if isinstance(data, list):
            return {"ok": True, "count": len(data), "tools": data}
        return {"ok": True, "raw": data}
    except Exception as e:  # noqa: BLE001
        return wrap_error("list_agent_tools", e)


@server.tool(description="列出历史会话（按最后活跃倒序），含会话 id 与消息数。")
def list_sessions() -> dict:
    try:
        data = _get("/api/agent/sessions")
        return {"ok": True, "count": len(data) if isinstance(data, list) else None, "sessions": data}
    except Exception as e:  # noqa: BLE001
        return wrap_error("list_sessions", e)


@server.tool(
    description=(
        "向投研 Agent 提问，返回最终答案 + 完整推理轨迹（每步思考、实际调用的工具及参数）。"
        "trace 里能看到 Agent 是不是真的去查了数据，而不是编造。"
        "session_id 传同一个值可延续上下文；不传则新建会话。"
    )
)
def ask_agent(question: str, session_id: str = "") -> dict:
    try:
        payload = json.dumps(
            {"sessionId": session_id or "", "message": question}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{BASE_URL}/api/agent/chat",
            data=payload,
            headers=_headers({"Accept": "text/event-stream"}),
            method="POST",
        )

        answer_parts: list[str] = []
        trace: list[dict] = []
        tool_calls: list[dict] = []
        errors: list[str] = []
        final_answer = ""
        steps = 0

        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    evt = json.loads(body)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type")
                step = evt.get("step", 0)
                steps = max(steps, step)

                if etype == "answer_delta":
                    answer_parts.append(evt.get("text") or "")
                elif etype == "answer":
                    final_answer = evt.get("text") or ""
                    trace.append({"step": step, "type": "answer", "text": final_answer[:200]})
                elif etype == "thinking":
                    trace.append({"step": step, "type": "thinking", "text": evt.get("text")})
                elif etype == "tool_call":
                    name = (evt.get("data") or {}).get("name") or evt.get("text")
                    args = (evt.get("data") or {}).get("arguments")
                    tool_calls.append({"step": step, "tool": name, "arguments": args})
                    trace.append({"step": step, "type": "tool_call", "tool": name})
                elif etype == "tool_result":
                    name = (evt.get("data") or {}).get("name") or evt.get("text")
                    trace.append({"step": step, "type": "tool_result", "tool": name})
                elif etype == "error":
                    errors.append(evt.get("text") or "unknown")
                elif etype == "start":
                    sid = (evt.get("data") or {}).get("sessionId")
                    trace.append({"step": 0, "type": "start", "session_id": sid})

        answer = final_answer or "".join(answer_parts)
        return {
            "ok": bool(answer) and not errors,
            "question": question,
            "answer": answer,
            "steps": steps,
            "tool_call_count": len(tool_calls),
            "tool_calls": tool_calls,
            "errors": errors,
            "trace": trace[:60],
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("ask_agent", e)


if __name__ == "__main__":
    server.run(transport="stdio")
