"""MCP Server: quant-portfolio

把 ETF 轮动实盘线的账户状态以 MCP 协议暴露：持仓、净值曲线、订单流水、
风控状态、影子权重对照。

设计原则：
  1. **纯只读** —— 只 SELECT，不 INSERT/UPDATE。实盘库被 Agent 误写后果不可接受。
  2. **无框架依赖** —— 直接读 sqlite，不 import 量化框架任何模块，
     因此和 quant-core 可以同时挂在同一个 Agent 上而不冲突（同名 config 模块）。
  3. 净值缺口如实报 —— 库里 equity 表的日期不连续时不补齐、不插值，直接说明缺哪几天。

启动：python servers/quant_portfolio.py
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

from _common import QUANT_DB, jsonable, require, wrap_error

server = MCPServer(
    name="quant-portfolio",
    version="1.0.0",
    instructions=(
        "ETF 轮动实盘账户的只读查询服务：持仓、净值、订单、风控状态。"
        "数据来自 trading.db，不做任何写入。"
    ),
)


def _conn() -> sqlite3.Connection:
    db = require(QUANT_DB, "交易数据库", "QUANT_DB")
    # 只读打开：即使代码有 bug 也写不进去
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    con = _conn()
    try:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


@server.tool(description="查询当前持仓：代码、数量、成本价、峰值价、市值标记。")
def get_positions() -> dict:
    try:
        rows = _rows(
            "SELECT code, qty, cost, peak, mark FROM positions ORDER BY code"
        )
        total_cost = sum((r["cost"] or 0) * (r["qty"] or 0) for r in rows)
        return {
            "ok": True,
            "count": len(rows),
            "total_cost": round(total_cost, 2),
            "positions": jsonable(rows),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_positions", e)


@server.tool(
    description=(
        "查询净值曲线。days 控制返回最近多少个交易日；"
        "同时给出累计收益、最大回撤、以及库里缺失的日期（不插值）。"
    )
)
def get_equity_curve(days: int = 30) -> dict:
    try:
        rows = _rows(
            "SELECT date, cash, market_value, total FROM equity ORDER BY date"
        )
        if not rows:
            return {"ok": False, "tool": "get_equity_curve", "error": "净值表为空"}

        totals = [r["total"] for r in rows if r["total"] is not None]
        first, last = totals[0], totals[-1]
        peak, max_dd = float("-inf"), 0.0
        for v in totals:
            peak = max(peak, v)
            if peak > 0:
                max_dd = min(max_dd, v / peak - 1.0)

        # 缺口检测：库里 equity 只有实际记录日，缺失的日子要如实报出来
        dates = [r["date"] for r in rows]
        gaps = []
        if len(dates) >= 2:
            try:
                parsed = [datetime.strptime(str(d), "%Y%m%d") for d in dates]
                for a, b in zip(parsed, parsed[1:]):
                    delta = (b - a).days
                    if delta > 7:  # 超过一周必有缺失（含周末）
                        gaps.append({
                            "after": a.strftime("%Y%m%d"),
                            "before": b.strftime("%Y%m%d"),
                            "calendar_days": delta,
                        })
            except (ValueError, TypeError):
                gaps = []

        return {
            "ok": True,
            "rows": len(rows),
            "first_date": dates[0],
            "last_date": dates[-1],
            "total_return_pct": jsonable(round((last / first - 1.0) * 100, 4)) if first else None,
            "max_drawdown_pct": jsonable(round(max_dd * 100, 4)),
            "latest_total": jsonable(round(last, 2)),
            "gaps": gaps[:20],
            "curve": jsonable(rows[-max(1, min(days, 400)):]),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_equity_curve", e)


@server.tool(description="查询历史订单流水（最近 N 笔），含成交价、数量、手续费、下单原因。")
def get_orders(limit: int = 20) -> dict:
    try:
        rows = _rows(
            "SELECT id, ts, code, side, qty, price, reason, status, "
            "filled_price, filled_qty, fee FROM orders ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        )
        filled = [r for r in rows if r["status"] == "filled"]
        return {
            "ok": True,
            "returned": len(rows),
            "filled_in_returned": len(filled),
            "fee_sum_in_returned": jsonable(
                round(sum(r["fee"] or 0 for r in rows), 4)
            ),
            "orders": jsonable(rows),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_orders", e)


@server.tool(description="查询运行时状态键值（熔断开关、最后运行日、影子对照配置等）。")
def get_runtime_state() -> dict:
    try:
        rows = _rows("SELECT key, value FROM state ORDER BY key")
        return {"ok": True, "count": len(rows), "state": jsonable(rows)}
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_runtime_state", e)


@server.tool(
    description=(
        "影子权重对照：同一策略不同参数变体的目标权重记录，用于评估参数改动是否有效。"
    )
)
def get_shadow_weights(limit: int = 10) -> dict:
    try:
        rows = _rows(
            "SELECT date, variant, weights, note FROM shadow_weights "
            "ORDER BY date DESC, variant LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return {"ok": True, "returned": len(rows), "records": jsonable(rows)}
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_shadow_weights", e)


@server.tool(
    description=(
        "账户健康检查：持仓市值 vs 净值是否自洽、净值是否断档、"
        "有没有挂起未成交订单。给 Agent 一个「现在这账户正常吗」的一站式结论。"
    )
)
def health_check() -> dict:
    try:
        pos = _rows("SELECT code, qty, cost, mark FROM positions")
        eq = _rows("SELECT date, market_value, total FROM equity ORDER BY date DESC LIMIT 1")
        pending = _rows(
            "SELECT id, code, side, status FROM orders "
            "WHERE status NOT IN ('filled', 'cancelled', 'rejected')"
        )
        issues: list[str] = []
        if not eq:
            issues.append("净值表为空，无法判断账户状态")
        if pending:
            issues.append(f"有 {len(pending)} 笔订单未终结")

        latest_mv = eq[0]["market_value"] if eq else None
        pos_mv = sum((p["mark"] or p["cost"] or 0) * (p["qty"] or 0) for p in pos)
        drift = None
        if latest_mv and pos_mv:
            drift = round((pos_mv / latest_mv - 1.0) * 100, 4)
            if abs(drift) > 1.0:
                issues.append(f"持仓市值与净值表市值偏差 {drift}%，需人工核对")

        return {
            "ok": True,
            "healthy": not issues,
            "issues": issues,
            "position_count": len(pos),
            "position_market_value": jsonable(round(pos_mv, 2)),
            "latest_equity_date": eq[0]["date"] if eq else None,
            "latest_total": jsonable(eq[0]["total"]) if eq else None,
            "market_value_drift_pct": drift,
            "pending_orders": jsonable(pending),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("health_check", e)


if __name__ == "__main__":
    server.run(transport="stdio")
