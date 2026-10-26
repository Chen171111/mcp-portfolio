"""共享工具：路径解析、JSON 安全化、异常包装。

三个 MCP server 都要 import 真实项目模块，这里集中处理 sys.path 与类型清洗。

路径策略（按优先级）：
  1. 环境变量（QUANT_ETF_HOME / QUANT_STOCK_HOME / ASTOCK_AGENT_HOME / QUANT_DB）
  2. 同仓推断：假定被包装的项目与本仓库同级（../quant/agentskill 等）
  3. 都没有 → 调用工具时返回带 hint 的结构化错误，而不是启动即崩

这样本机开箱即用，别人 clone 下来按 README 设环境变量即可。
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Any

# 本仓库根目录（mcp-portfolio/），其父目录视为「项目工作区」
_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKSPACE = _REPO_ROOT.parent


def _resolve(env_key: str, *candidates: Path) -> Path:
    """环境变量优先，其次按候选顺序找第一个存在的，都没有则返回首选（延迟报错）。"""
    raw = os.environ.get(env_key)
    if raw:
        return Path(raw).expanduser()
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


QUANT_ETF_HOME = _resolve(
    "QUANT_ETF_HOME",
    _WORKSPACE / "quant" / "agentskill",
    _WORKSPACE / "agentskill",
)
QUANT_STOCK_HOME = _resolve("QUANT_STOCK_HOME", _WORKSPACE / "quant2")
ASTOCK_AGENT_HOME = _resolve("ASTOCK_AGENT_HOME", _WORKSPACE / "astock-agent")

QUANT_DB = _resolve(
    "QUANT_DB",
    QUANT_ETF_HOME / "state" / "trading.db",
    _WORKSPACE / "quant" / "agentskill" / "state" / "trading.db",
)


def inject_path(root: Path) -> None:
    """把项目根塞进 sys.path，让它内部的 `import config` 能解析。"""
    p = str(root)
    if p not in sys.path:
        sys.path.insert(0, p)


def require(path: Path, what: str, env_key: str) -> Path:
    """确认依赖存在，不存在时抛带指引的错误（由工具层转成结构化返回）。"""
    if not path.exists():
        raise FileNotFoundError(
            f"{what} 不存在: {path}\n"
            f"请设置环境变量 {env_key} 指向正确位置，或把仓库放在被包装项目的同级目录。"
        )
    return path


def jsonable(obj: Any) -> Any:
    """把 numpy / pandas / NaN 递归转成 JSON 可序列化结构。

    MCP 协议要求返回值可序列化，而量化返回值里到处都是 np.float64 和 NaN，
    直接返回会在序列化层炸掉。统一在这里收口。

    注意 NaN 转 None 而不是 0 —— 0 是有效数值，会把「没数据」伪装成「数据是 0」。
    """
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    # numpy 标量
    item = getattr(obj, "item", None)
    if callable(item) and getattr(obj, "ndim", None) == 0:
        return jsonable(obj.item())
    if hasattr(obj, "to_dict") and hasattr(obj, "index"):  # pandas Series
        return {str(k): jsonable(v) for k, v in obj.to_dict().items()}
    if hasattr(obj, "to_dict") and hasattr(obj, "columns"):  # pandas DataFrame
        return [jsonable(r) for r in obj.to_dict(orient="records")]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    return str(obj)


def wrap_error(tool: str, exc: Exception) -> dict:
    """工具异常统一结构：不吞错，把类型和信息都交出去。"""
    return {
        "ok": False,
        "tool": tool,
        "error_type": type(exc).__name__,
        "error": str(exc)[:800],
    }
