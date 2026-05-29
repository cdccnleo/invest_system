# InvestPilot — 智能投资分析系统

A-share（A股）智能投资分析系统，支持持仓监控、实时行情采集、LLM 驱动分析、盘中异动告警与主动投顾。

## 系统架构

```
Phase 1 — 数据采集层
  fetch_quotes       日/实时行情（东方财富/新浪）
  fetch_news         新闻采集（华尔街见闻 RSS）
  fetch_reports      券商研报（东方财富研报 API）
  fetch_announcements 持仓股公告
  fetch_financial    财务数据
  corporate_actions   分红/配股/拆股事件

Phase 2 — 分析与调度
  run_analysis       完整分析链路
  schedule_runner     定时调度（08:30/11:30/15:30/21:00）
  circuit_breaker    熔断器（市场异常时自动限制买入）
  model_router       LLM 路由（DeepSeek + Ollama 本地模型）
  intraday_monitor   盘中异动监控（每5分钟）

Phase 3 — L3 主动投顾
  l3_dialog_engine   5类触发器：定期签到/风险升级/新闻影响/偏离告警/里程碑
```

## 目录结构

```
scripts/
├── 核心脚本（可直接 python3 scripts/xxx.py 运行）
│   ├── run_analysis.py         一键完整分析
│   ├── schedule_runner.py       定时调度守护进程
│   ├── intraday_monitor.py      盘中异动监控
│   ├── l3_dialog_engine.py     L3 主动对话引擎
│   ├── dashboard.py            Streamlit 仪表盘
│   ├── fetch_*.py              数据采集模块
│   └── credentials.py           统一凭据管理
└── migrations/
    └── l3_phase_a.sql          L3 表结构 DDL

.env                    ← 凭据文件（不提交 Git）
credentials.py          ← 从 Windows Credential Manager 读取密钥
skills/                 ← 技能系统（用户生成内容，不提交）
```

## 快速开始

### 1. 克隆项目

```bash
git clone <your-repo-url>
cd invest_system
```

### 2. 创建虚拟环境

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置凭据

```bash
cp .env.example .env
# 编辑 .env 填入真实密钥（API Key、数据库密码等）
```

凭据管理优先级（从高到低）：
1. **Windows Credential Manager**（WSL2 跨端，推荐）
2. 本地加密文件 `~/.hermes/invest_credentials/store.json`
3. 环境变量

### 4. 初始化数据库

```bash
psql -U invest_admin -d investpilot -f scripts/migrations/l3_phase_a.sql
```

### 5. 启动定时调度

```bash
python3 scripts/schedule_runner.py
```

### 6. 访问仪表盘

```bash
streamlit run scripts/dashboard.py --server.port 8501
```

## 主要功能

| 功能 | 说明 |
|------|------|
| **持仓同步** | 从券商文件（国金/天天基金/广发）同步持仓 |
| **行情采集** | 股票/ETF（新浪）+ 基金净值（东方财富） |
| **午间快讯** | 11:30 推送持仓股上午涨跌排行榜 |
| **盘中异动** | 每5分钟监控：涨跌幅>3%、成交量异常、MA5/MA20 金死叉 |
| **晚间复盘** | 21:00 LLM 驱动全面复盘分析 |
| **研报复盘** | 16:00 采集券商研报 |
| **公告监控** | 20:50 采集持仓股近30天公告 |
| **L3 主动投顾** | 定期签到、风险升级、偏离告警、里程碑追踪 |
| **熔断机制** | 市场异动时自动限制买入仓位 |

## 技术栈

- **数据库**：PostgreSQL 17 + pgvector（向量语义检索）
- **LLM**：DeepSeek（策略分析）+ Ollama gemma4:e4b（本地查询）
- **推送**：Server酱（微信）+ 飞书群机器人
- **Web**：Streamlit 仪表盘
- **调度**：APScheduler
- **加密**：pgcrypto AES-256（持仓敏感字段）

## 注意事项

- 凭据文件 `.env` 包含真实密钥，**不要**提交到 Git
- `skills/` 目录含用户生成内容，不应提交
- 操作系统非交易时段（午休/收盘后），盘中监控自动跳过
