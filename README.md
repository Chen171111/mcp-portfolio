# mcp-portfolio

把三个真实项目的能力，用 **MCP（Model Context Protocol）** 标准化对外暴露。

> 一句话：**已有的东西不重写，只加一层标准入口。**
> Java 写的投研 Agent、Python 写的量化框架、sqlite 里的实盘账户数据 ——
> 原本只能在各自的程序内部使用，套上 MCP 之后，任何支持 MCP 的客户端
> （Claude Desktop / WorkBuddy / Codex / Dify / Cursor）都能直接调用。

## 三个 server

| Server | 工具数 | 接的是什么 | 传输 |
|---|---|---|---|
| `quant-core` | 5 | `quant/agentskill` 量化框架（回测引擎 / 8 策略 / 9 股票池） | stdio |
| `quant-portfolio` | 6 | `quant/agentskill/state/trading.db` 实盘账户（持仓/净值/订单） | stdio |
| `astock-agent` | 4 | `astock-agent` Spring Boot 投研 Agent 的 HTTP/SSE 接口 | stdio |

**合计 15 个工具**，协议层冒烟测试 3/3 通过（MCP 协议版本 `2025-11-25`）。

## 快速开始

```bash
# 1. 环境（必须 --system-site-packages，否则拿不到项目已有的 pandas）
<有pandas的python> -m venv --system-site-packages .venv
./.venv/Scripts/python.exe -m pip install mcp

# 2. 协议层冒烟测试（不是 import 调函数，是真实 stdio 握手）
./.venv/Scripts/python.exe tests/smoke_test.py
```

## 客户端注册

`claude_desktop_config.json` / WorkBuddy `mcp.json` / 任何标准 MCP 客户端：

```json
{
  "mcpServers": {
    "quant-core": {
      "command": "E:\\MyWorkAndProject\\mcp-portfolio\\.venv\\Scripts\\python.exe",
      "args": ["E:\\MyWorkAndProject\\mcp-portfolio\\servers\\quant_core.py"]
    },
    "quant-portfolio": {
      "command": "E:\\MyWorkAndProject\\mcp-portfolio\\.venv\\Scripts\\python.exe",
      "args": ["E:\\MyWorkAndProject\\mcp-portfolio\\servers\\quant_portfolio.py"]
    },
    "astock-agent": {
      "command": "E:\\MyWorkAndProject\\mcp-portfolio\\.venv\\Scripts\\python.exe",
      "args": ["E:\\MyWorkAndProject\\mcp-portfolio\\servers\\astock_agent.py"],
      "env": {
        "ASTOCK_BASE_URL": "http://127.0.0.1:8080",
        "ASTOCK_TOKEN": ""
      }
    }
  }
}
```

注册后需要重启客户端，MCP server 才会被拉起。

## 工具清单

### quant-core — 量化框架

| 工具 | 作用 |
|---|---|
| `list_strategy_catalog` | 列出 8 个内置策略 |
| `list_pools` | 列出 9 个预置股票池及成分 |
| `run_backtest` | 跑一次真实回测，返回 8 项绩效指标 |
| `get_quote` | 拉单只标的最近 N 日行情 |
| `get_risk_config` | 查看风控与交易成本配置 |

### quant-portfolio — 实盘账户（只读）

| 工具 | 作用 |
|---|---|
| `get_positions` | 当前持仓（代码/数量/成本/峰值/标记价） |
| `get_equity_curve` | 净值曲线 + 累计收益 + 最大回撤 + **缺口检测** |
| `get_orders` | 订单流水（含成交价、手续费、下单原因） |
| `get_runtime_state` | 运行时状态（熔断开关、策略版本） |
| `get_shadow_weights` | 影子权重对照（参数改动 A/B 评估） |
| `health_check` | 账户自洽性体检（市值偏差、断档、挂单） |

### astock-agent — Java 投研 Agent 网关

| 工具 | 作用 |
|---|---|
| `check_backend` | 探测后端是否在线，给出启动指引 |
| `list_agent_tools` | 列出 Java 侧注册的 Spring AI 工具 |
| `list_sessions` | 历史会话 |
| `ask_agent` | 自然语言提问，返回答案 + **完整推理轨迹** |

## 三个设计决策（不是随手写的）

### 1. 一个 server 只接一个项目

`quant/agentskill` 和 `quant2` 都有 `config.py`，`sys.path.insert` 会互相覆盖，
`import config` 可能拿到另一个项目的配置。

所以拆成三个互不干扰的 server：

- `quant-core` → import ETF 项目框架
- `quant-portfolio` → **纯读 sqlite，不 import 任何项目模块**
- `astock-agent` → 走 HTTP，进程隔离

三者可以同时挂在同一个 Agent 上，不冲突。

### 2. 交易类 server 从连接层就只读

```python
sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
```

不是"代码里记得别写"，而是**物理上写不进去**。Agent 误触实盘是不可接受的，
所以这一层防护不能依赖自觉。

同时不提供任何写工具 —— 连"改一个状态位"也不给。

### 3. 返回值统一清洗

量化返回值里到处是 `np.float64`、`NaN`、`pd.Series`，直接返回会在 MCP 序列化层炸掉。
`_common.jsonable()` 统一转成 JSON 安全结构，**NaN 转 `None` 而不是 0**
（0 是有效数值，会把"没数据"伪装成"数据是 0"）。

## 目录

```
mcp-portfolio/
├── servers/
│   ├── _common.py           # 路径注入 / JSON 清洗 / 错误结构
│   ├── quant_core.py        # 量化框架封装
│   ├── quant_portfolio.py   # 实盘账户只读查询
│   └── astock_agent.py      # Java 投研 Agent 网关
├── tests/
│   ├── smoke_test.py        # 协议层冒烟测试
│   └── smoke_report.json    # 最近一次结果
└── README.md
```

## 实测记录

| 项 | 结果 |
|---|---|
| 协议版本 | `2025-11-25` |
| server 通过率 | 3/3 |
| 工具数 | 15 |
| schema 完整 | 全部 true |
| 回测耗时 | 3 秒（数据已缓存，2019→2025 全区间） |
| 回测结果（2023-2025 / ETF全球） | 累计 +17.29%，回撤 -7.00%（基准 -24.19%），夏普 0.84 |

## 已知限制

- `astock-agent` 需要先启动 Java 后端（`mvn spring-boot:run`），未启动时返回结构化降级错误。
- 路径写死在 `servers/_common.py`，换机器需要改 `QUANT_ETF_HOME` / `QUANT_DB`。
- 都是 stdio 传输，没有做 HTTP 部署（需要多客户端共享时再改 `transport="streamable-http"`）。
```
