# 三和妖币收割机 · 本地面板

把你账号能看到的"妖币"榜单数据抓到本地，用网页面板实时展示，附带**模拟交易引擎**
（自动跟随信号开/平仓，纯模拟不碰真钱）和可选的信号推送。
纯 Python 标准库，无需安装任何依赖。

## 首次配置

仓库里**不含登录凭据**，需要你自己填：

```bash
cp config.example.json config.json
```

然后按下方『获取登录凭据』把你的 cookie 填进 `config.json`。

## 启动

```bash
python3 panel.py
```

然后浏览器打开 **http://127.0.0.1:8090**

停止：终端按 `Ctrl+C`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `config.example.json` | 配置模板（复制成 `config.json` 后填凭据） |
| `config.json` | 你的真实配置（含凭据，已被 .gitignore 排除，不上传） |
| `fetcher.py` | 抓取 + 解析数据（可单独运行 `python3 fetcher.py` 测试） |
| `paper_trader.py` | 模拟交易引擎：跟随信号开/平仓、算盈亏、爆仓判定 |
| `panel.py` | 主程序：网页服务 + 后台轮询 + 推送 + 交易引擎 |
| `static/index.html` | 网页面板界面（信号面板 + AI实盘交易展示） |
| `trades.json` | 模拟交易运行数据（自动生成，不上传） |

## 面板看什么

**信号面板**：顶部标签切换板块（入场窗口、确认候选、早发现雷达、OI异动…），每个币显示
方向、阶段、价格、24h涨跌、OI 多窗口变化、资金费率、多空评分、就绪度、触发理由。

**AI实盘交易展示**：模拟交易战绩——总收益率、当前持仓（实时浮盈）、按日期分组的历史记录。

## 模拟交易引擎

在 `config.json` 的 `trader` 段配置（默认开启、纯模拟）：

- `stake_per_trade` / `leverage`：每单本金 / 杠杆
- `take_profit_pct` / `stop_loss_pct`：止盈 / 止损线
- `entry_sections` / `min_readiness`：哪些板块、就绪度多少才开仓
- `exit_on_signal_gone`：信号从入场板块消失时是否平仓

> ⚠️ **风险提示**：这是**模拟盘**，不连接任何交易所、不使用真钱。高杠杆下（如 10x）
> 价格反向波动约 10% 即模拟爆仓归零。请先用模拟盘长期验证策略真实表现，
> 切勿凭营销截图直接用真钱交易。

## 获取登录凭据

数据接口需要你自己的登录态。有两种方式：

### 方式一：自动抓取（推荐，免手动）

只要你用 **Chrome** 登录过 sanhe6.com，程序能直接从 Chrome 读取 cookie，
无需手动复制。`config.json` 里 `auto_cookie: true`（默认开启）即可：

- 启动时自动抓一次
- 运行中 cookie 失效时，自动从 Chrome 重新读取并重试
- 面板顶部出现"🔄 刷新Cookie"按钮时，点一下也能手动触发

需要安装一个依赖（用于解密 Chrome cookie）：

```bash
pip install pycryptodome    # 或: pip install -r requirements.txt
```

> cookie 失效时，先确保你在 **Chrome 浏览器里仍是登录状态**（必要时重新登录一次），
> 程序就能自动同步到最新 cookie。

### 方式二：手动填入

1. 浏览器登录 sanhe6.com/coin，F12 → Network → 刷新
2. 找到 `dashboards/public` 请求 → 右键 Copy as cURL
3. 从里面 `-b '...'` 后面那一整段复制出来
4. 替换 `config.json` 里的 `cookie` 字段
5. 重启 `panel.py`

## 开启推送（可选）

编辑 `config.json` 的 `alert` 段：

- `enabled`: 改成 `true`
- `channel`: 选 `"serverchan"`（微信，国内推荐）或 `"telegram"`
- `min_readiness`: 就绪度阈值，达到才推（默认 60）
- `watch_sections`: 监控哪些板块

**注意**：Telegram 在国内网络直连不通（被墙），除非有代理。国内建议用 **Server酱**（微信推送）：
去 https://sct.ftqq.com 用微信扫码登录，拿到 SendKey 填进 `serverchan_sendkey`，`channel` 设为 `"serverchan"`。

## 合规与免责

- 数据来自你自己的付费账号，仅供**个人使用**，请勿公开转售或大规模分发他人数据。
- 本项目仅为技术学习/数据可视化用途，**不构成任何投资建议**。加密货币合约交易风险极高，盈亏自负。

