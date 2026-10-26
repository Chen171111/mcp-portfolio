"""MCP Server: quant-core

把个人 A 股量化框架（E:\\MyWorkAndProject\\quant\\agentskill）的能力
以 MCP 协议标准化暴露，任何支持 MCP 的 Agent（Claude / WorkBuddy / Codex / Dify）
都能直接调用，无需了解框架内部结构。

设计原则：
  1. 只读 —— 本 server 不下单、不写交易库，避免 Agent 误触实盘。
  2. 真实 —— 所有数据来自框架的 DataStore 缓存与 BacktestEngine，不做模拟。
  3. 结构化 —— 返回 JSON，指标名与框架 compute_metrics 口径一致。

启动：python servers/quant_core.py          （stdio，供 MCP 客户端拉起）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer

from _common import QUANT_ETF_HOME, inject_path, jsonable, wrap_error

inject_path(QUANT_ETF_HOME)

import config  # noqa: E402  —— 必须在 inject_path 之后
import pipeline  # noqa: E402
from dataprovider.store import DataStore  # noqa: E402
from strategies.registry import list_strategies  # noqa: E402

server = MCPServer(
    name="quant-core",
    version="1.0.0",
    instructions=(
        "A 股量化研究框架的 MCP 封装。可查询策略清单、股票池、跑真实回测、"
        "拉行情与绩效指标。只读，不下单。"
    ),
)


@server.tool(description="列出框架内置的全部策略及其默认参数。回测前先调这个确认策略名。")
def list_strategy_catalog() -> dict:
    try:
        from strategies.registry import STRATEGIES

        catalog = []
        for name, cls in STRATEGIES.items():
            doc = (cls.__doc__ or "").strip().splitlines()
            catalog.append({
                "name": name,
                "summary": doc[0] if doc else "",
                "module": cls.__module__,
            })
        return {"ok": True, "count": len(catalog), "strategies": catalog}
    except Exception as e:  # noqa: BLE001
        return wrap_error("list_strategy_catalog", e)


@server.tool(description="列出预置股票池（ETF 宽基/行业/防御/全球/科技进攻 等），返回代码与名称。")
def list_pools() -> dict:
    try:
        pools = []
        for name, codes in config.RECOMMENDED_POOLS.items():
            pools.append({
                "pool": name,
                "count": len(codes),
                "codes": [
                    {"code": c, "name": config.ETF_NAMES.get(c, "")} for c in codes
                ],
            })
        return {"ok": True, "count": len(pools), "pools": pools}
    except Exception as e:  # noqa: BLE001
        return wrap_error("list_pools", e)


@server.tool(
    description=(
        "跑一次真实回测，返回累计收益/年化/最大回撤/夏普/索提诺/卡玛/胜率等指标。"
        "pool 传股票池名（见 list_pools），不传则用策略默认池。耗时通常 3~15 秒。"
    )
)
def run_backtest(
    strategy: str = "etf_rotation",
    pool: str = "ETF全球",
    start: str = "20190101",
    end: str = "20251231",
    topk: int = 5,
    rebalance: int = 5,
) -> dict:
    try:
        if pool not in config.RECOMMENDED_POOLS:
            return {
                "ok": False,
                "tool": "run_backtest",
                "error": f"未知股票池: {pool}",
                "available": list(config.RECOMMENDED_POOLS),
            }
        codes = list(config.RECOMMENDED_POOLS[pool])
        out = pipeline.run_backtest(
            codes,
            strategy=strategy,
            start=start,
            end=end,
            topk=topk,
            rebalance=rebalance,
        )
        return {
            "ok": True,
            "strategy": strategy,
            "pool": pool,
            "codes": codes,
            "period": {"start": out["meta"]["start"], "end": out["meta"]["end"]},
            "metrics": jsonable(out["metrics"]),
            "equity_tail": jsonable(
                out["result"].equity.tail(5).reset_index().to_dict(orient="records")
            ),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("run_backtest", e)


@server.tool(description="拉单只标的最近 N 个交易日的行情（开高低收量），用于快速看盘。")
def get_quote(code: str, days: int = 20) -> dict:
    try:
        store = DataStore()
        store.ensure([code])
        df = store.read(code)
        if df is None or df.empty:
            return {"ok": False, "tool": "get_quote", "error": f"{code} 本地无数据"}
        tail = df.tail(max(1, min(days, 250))).reset_index()
        cols = [c for c in ("date", "open", "high", "low", "close", "volume")
                if c in tail.columns]
        latest = tail.iloc[-1]
        return {
            "ok": True,
            "code": code,
            "name": config.ETF_NAMES.get(code, ""),
            "rows": len(tail),
            "latest_close": jsonable(latest.get("close")),
            "bars": jsonable(tail[cols].to_dict(orient="records")),
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_quote", e)


@server.tool(description="查看框架的默认风控与成本配置（熔断、波动率目标、手续费口径）。")
def get_risk_config() -> dict:
    try:
        return {
            "ok": True,
            "risk": jsonable(config.RISK),
            "trading_cost": jsonable(config.TRADING_COST),
            "defaults": {
                "strategy": config.DEFAULT_STRATEGY,
                "topk": config.DEFAULT_TOP_K,
                "rebalance": config.DEFAULT_REBALANCE,
                "dd_circuit": config.DEFAULT_DD_CIRCUIT,
                "vol_target": config.DEFAULT_VOL_TARGET,
                "timing": config.DEFAULT_TIMING,
            },
        }
    except Exception as e:  # noqa: BLE001
        return wrap_error("get_risk_config", e)


if __name__ == "__main__":
    server.run(transport="stdio")
