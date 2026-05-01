# A 股量化模拟炒股系统

这是一个纯模拟的 A 股量化交易项目，不连接券商，不会下真实订单。系统使用公开行情数据做 2018 年至今的历史回测，并使用实时行情刷新虚拟账户盈亏、生成调仓信号和模拟成交记录。

当前项目位置：

```text
/home/jerico/projects/a_share_quant_sim
```

## 文件夹结构

```text
/home/jerico/projects/a_share_quant_sim
├─ config.example.json        # 示例配置
├─ config.json                # 你的运行配置
├─ requirements.txt           # Python 依赖
├─ README.md                  # 使用说明
├─ scripts/
│  ├─ run_rebalance.sh        # WSL/cron 定时模拟调仓脚本
│  ├─ run_settle.sh           # WSL/cron 定时每日结算脚本
│  ├─ run_rebalance.bat       # Windows 任务计划程序备用脚本
│  └─ run_settle.bat          # Windows 任务计划程序备用脚本
├─ quant_sim/
│  ├─ cli.py                  # 命令行入口
│  ├─ config.py               # 配置读取
│  ├─ data.py                 # A 股历史/实时行情
│  ├─ indicators.py           # 技术指标
│  ├─ strategy.py             # 趋势动量策略
│  ├─ broker.py               # 虚拟账户、持仓、模拟订单
│  ├─ backtest.py             # 回测引擎
│  └─ paper.py                # 模拟盘实时盈亏、调仓、结算
├─ data/
│  ├─ cache/                  # 行情缓存
│  └─ state/                  # 虚拟账户 JSON 状态
└─ reports/                   # 回测结果、每日结算结果
```

## Conda 安装

在 WSL 里执行：

```bash
cd /home/jerico/projects/a_share_quant_sim
conda create -n a_share_quant python=3.11 -y
conda activate a_share_quant
pip install -r requirements.txt
```

如果以后想退出环境：

```bash
conda deactivate
```

## 配置股票池

打开 `config.json`，修改 `symbols`。支持 6 位代码，也支持精确股票名，示例：

```json
{
  "initial_cash": 200000,
  "start_date": "20180101",
  "symbols": ["600519", "000858", "300750", "招商银行", "比亚迪"]
}
```

建议先用 5 到 20 只股票测试。股票池太大时，公开接口会比较慢。

## 手动运行

运行 2018 年至今回测：

```bash
conda activate a_share_quant
cd /home/jerico/projects/a_share_quant_sim
python -m quant_sim.cli backtest --config config.json --start 20180101
```

创建 20 万本金的虚拟账户：

```bash
python -m quant_sim.cli paper-init --config config.json
```

查看实时信号：

```bash
python -m quant_sim.cli signal --config config.json
```

刷新虚拟账户盈亏：

```bash
python -m quant_sim.cli snapshot --config config.json
```

根据最新策略信号执行一次模拟调仓：

```bash
python -m quant_sim.cli rebalance --config config.json
```

生成当天结算：

```bash
python -m quant_sim.cli settle --config config.json
```

结算结果会输出到：

```text
reports/paper_settlement_YYYYMMDD.json
reports/paper_positions_YYYYMMDD.csv
```

## 自动运行

这个系统可以自动模拟买入、卖出、结算，但需要你的 WSL/电脑在对应时间可运行。推荐节奏：

- 交易日 10:00 执行一次 `scripts/run_rebalance.sh`
- 交易日 14:30 执行一次 `scripts/run_rebalance.sh`
- 交易日 15:10 执行一次 `scripts/run_settle.sh`

A 股常规交易时间是 9:30-11:30 和 13:00-15:00。10:00 避开开盘噪声，14:30 接近尾盘，15:10 用来做收盘后的模拟账户结算。你也可以只每天 14:30 调仓一次，更简单。

### 用 cron 设置自动任务

先给脚本执行权限：

```bash
chmod +x /home/jerico/projects/a_share_quant_sim/scripts/run_rebalance.sh
chmod +x /home/jerico/projects/a_share_quant_sim/scripts/run_settle.sh
```

打开定时任务编辑器：

```bash
crontab -e
```

加入下面三行：

```cron
0 10 * * 1-5 /home/jerico/projects/a_share_quant_sim/scripts/run_rebalance.sh >> /home/jerico/projects/a_share_quant_sim/reports/cron.log 2>&1
30 14 * * 1-5 /home/jerico/projects/a_share_quant_sim/scripts/run_rebalance.sh >> /home/jerico/projects/a_share_quant_sim/reports/cron.log 2>&1
10 15 * * 1-5 /home/jerico/projects/a_share_quant_sim/scripts/run_settle.sh >> /home/jerico/projects/a_share_quant_sim/reports/cron.log 2>&1
```

如果你的 WSL 没有 cron 服务，需要启动：

```bash
sudo service cron start
```

## 股票和模拟交易怎么理解

你可以把这个系统理解成三层：

第一层是股票池。它不会自动扫描全市场几千只股票，而是在 `config.json` 的 `symbols` 里挑选候选股票。初学者建议先放沪深 300、消费、金融、新能源、科技里你能理解的龙头或指数成分股。

第二层是策略。默认策略是趋势动量：价格站上 MA20，MA20 高于 MA60，说明中短期趋势较强；再看 20 日动量、波动率和最近回撤，给股票打分。分数靠前的股票会被分配目标仓位。

第三层是虚拟账户。`rebalance` 会按照目标仓位生成模拟买入或卖出订单，扣除模拟手续费、印花税、滑点，并把结果写入 `data/state/default_account.json`。这不是实盘，不会动你的真实资金。

## 默认策略逻辑

- MA20 高于 MA60，且收盘价高于 MA20 才进入候选。
- 按 20 日动量、波动率、60 日回撤综合打分。
- 最多持有 5 只股票，单只股票最多 22% 仓位。
- 保留 5% 现金。
- 默认止损 12%，止盈 35%。
- 手续费、印花税、滑点、100 股一手都在 `config.json` 里可调。

## 重要说明

本系统只做学习、研究和虚拟模拟，不构成投资建议，也不保证收益。公开行情接口可能受网络、交易日、接口变更影响；真实交易前必须额外接入券商风控、订单确认、异常处理和合规审查。
