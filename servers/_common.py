"""共享工具：路径注入、JSON 安全化、异常包装。

三个 MCP server 都要 import 真实项目模块，这里集中处理 sys.path 与类型清洗。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

# 真实项目根目录（本机实况，另一台机器按需改 .env 或环境变量覆盖）
QUANT_ETF_HOME = Path(r"E:\MyWorkAndProject\quant\agentskill")
QUANT_STOCK_HOME = Path(r"E:\MyWorkAndProject\quant2")
ASTOCK_AGENT_HOME = Path(r"E:\MyWorkAndProject\astock-agent")

QUANT_DB = QUANT_ETF_HOME / "state" / "trading.db"


def inject_path(root: Path) -> None:
    """把项目根塞进 sys.path，让它内部的 `import config` 能解析。"""
    p = str(root)
    if p not in sys.path:
        sys.path.insert(0, p)


def jsonable(obj: Any) -> Any:
    """把 numpy / pandas / NaN 递归转成 JSON 可序列化结构。

    MCP 协议要求返回值可序列化，而量化返回值里到处都是 np.float64 和 NaN，
    直接返回会在序列化层炸掉。统一在这里收口。
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
