#!/usr/bin/env python3
"""模拟交易引擎 (paper trading)

监听妖币信号 -> 模拟开仓 -> 实时算盈亏 -> 止盈/止损/信号消失平仓。
全程不碰真钱、不连交易所。所有状态持久化到 trades.json。

杠杆合约盈亏模型 (逐仓):
  名义价值 = stake * leverage
  仓位数量 = 名义价值 / 开仓价
  浮动盈亏(USDT) = (现价 - 开仓价) * 数量 * 方向     (做多方向=+1, 做空=-1)
  收益率% = 浮动盈亏 / stake * 100                  (相对本金, 含杠杆放大)
  爆仓: 当浮动盈亏 <= -stake * (1 - buffer) 即亏掉接近全部保证金
"""
import json
import time
import threading
from pathlib import Path

BASE = Path(__file__).parent
TRADES_PATH = BASE / "trades.json"
_LOCK = threading.Lock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _today():
    return time.strftime("%Y-%m-%d")


def load_state(cfg):
    """读取持久化状态，不存在则初始化。"""
    if TRADES_PATH.exists():
        with open(TRADES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "balance": cfg["trader"]["starting_balance"],
        "starting_balance": cfg["trader"]["starting_balance"],
        "open": {},        # ticker -> position dict
        "closed": [],      # 已平仓列表 (最新在前)
        "cooldown": {},    # ticker -> 可再次入场的时间戳
        "stats": {"total_trades": 0, "wins": 0, "losses": 0, "liquidations": 0},
    }


def save_state(state):
    tmp = TRADES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    tmp.replace(TRADES_PATH)


def build_price_map(parsed):
    """从解析后的 dashboard 提取每个币的最新价格和方向信息。
    返回 {ticker: {price, change24h, direction, sections:set, readiness, oi1h, ...}}"""
    pm = {}
    for sec in parsed.get("sections", []):
        for it in sec["items"]:
            tk = it["ticker"]
            price = it.get("price")
            if price is None:
                continue
            entry = pm.setdefault(tk, {
                "price": price, "change24h": it.get("change24h"),
                "directionCode": it.get("directionCode"), "direction": it.get("direction"),
                "sections": set(), "readiness": 0, "oi1h": it.get("oi1h"),
                "igniteActive": it.get("igniteActive"), "why": it.get("why", ""),
            })
            entry["sections"].add(sec["key"])
            # 取各板块里最高就绪度
            r = it.get("readiness") or 0
            if r > entry["readiness"]:
                entry["readiness"] = r
            # 价格用最新的非空值
            entry["price"] = price
    return pm


def _pnl(pos, price):
    """计算某仓位在 price 下的浮动盈亏(USDT) 和 收益率%。"""
    direction = 1 if pos["side"] == "long" else -1
    pnl_usdt = (price - pos["entry_price"]) * pos["qty"] * direction
    pnl_pct = pnl_usdt / pos["stake"] * 100
    return pnl_usdt, pnl_pct


def open_position(state, cfg, ticker, info):
    """模拟开仓。"""
    t = cfg["trader"]
    side = "long"  # only_long=True 时固定做多；妖币策略基本都偏多
    if not t.get("only_long", True) and info.get("directionCode") == "SHORT":
        side = "short"
    price = info["price"]
    stake = t["stake_per_trade"]
    lev = t["leverage"]
    notional = stake * lev
    qty = notional / price
    pos = {
        "ticker": ticker, "side": side, "leverage": lev, "stake": stake,
        "entry_price": price, "qty": qty, "notional": notional,
        "opened_at": _now(), "open_date": _today(),
        "entry_readiness": info.get("readiness"),
        "entry_reason": info.get("why", "")[:160],
        "tp_pct": t["take_profit_pct"], "sl_pct": t["stop_loss_pct"],
        "peak_pct": 0.0,
    }
    state["open"][ticker] = pos
    state["balance"] -= stake  # 保证金从余额划出
    return pos


def close_position(state, cfg, ticker, price, reason):
    """模拟平仓，结算盈亏，写入 closed。"""
    pos = state["open"].pop(ticker, None)
    if not pos:
        return None
    pnl_usdt, pnl_pct = _pnl(pos, price)
    t = cfg["trader"]
    # 爆仓: 亏损吃掉接近全部保证金
    liquidated = pnl_usdt <= -pos["stake"] * (1 - t.get("liquidation_buffer_pct", 0.5) / 100)
    if liquidated and reason != "liquidation":
        # 若已触发爆仓线，盈亏锁定为 -stake
        pnl_usdt = -pos["stake"]
        pnl_pct = -100.0
        reason = "liquidation"
    # 返还保证金 + 盈亏
    state["balance"] += pos["stake"] + pnl_usdt
    rec = dict(pos)
    rec.update({
        "exit_price": price, "closed_at": _now(),
        "pnl_usdt": round(pnl_usdt, 2), "pnl_pct": round(pnl_pct, 1),
        "reason": reason,
    })
    state["closed"].insert(0, rec)
    # 统计
    st = state["stats"]
    st["total_trades"] += 1
    if reason == "liquidation":
        st["liquidations"] += 1
        st["losses"] += 1
    elif pnl_usdt >= 0:
        st["wins"] += 1
    else:
        st["losses"] += 1
    # 冷却，避免立刻重新入场
    cd_min = t.get("reentry_cooldown_minutes", 120)
    state["cooldown"][ticker] = time.time() + cd_min * 60
    return rec


def _should_enter(cfg, ticker, info, state):
    t = cfg["trader"]
    if ticker in state["open"]:
        return False
    if len(state["open"]) >= t.get("max_open_positions", 5):
        return False
    if time.time() < state["cooldown"].get(ticker, 0):
        return False
    if info["readiness"] < t.get("min_readiness", 60):
        return False
    if t.get("only_long", True) and info.get("directionCode") == "SHORT":
        return False
    # 必须出现在指定入场板块
    entry_secs = set(t.get("entry_sections", []))
    return bool(info["sections"] & entry_secs)


def evaluate(cfg, parsed):
    """每轮调用: 更新持仓盈亏、检查平仓、检查开仓。返回本轮事件列表。"""
    events = []
    if not cfg["trader"].get("enabled"):
        return events
    t = cfg["trader"]
    entry_secs = set(t.get("entry_sections", []))
    with _LOCK:
        state = load_state(cfg)
        pm = build_price_map(parsed)

        # 1) 检查已有持仓是否该平
        for ticker in list(state["open"].keys()):
            pos = state["open"][ticker]
            info = pm.get(ticker)
            price = info["price"] if info else None
            if price is None:
                continue  # 该币本轮无价格，跳过(保持持仓)
            pnl_usdt, pnl_pct = _pnl(pos, price)
            if pnl_pct > pos.get("peak_pct", 0):
                pos["peak_pct"] = round(pnl_pct, 1)
            reason = None
            if pnl_usdt <= -pos["stake"] * (1 - t.get("liquidation_buffer_pct", 0.5) / 100):
                reason = "liquidation"
            elif pnl_pct >= pos["tp_pct"]:
                reason = "take_profit"
            elif pnl_pct <= -pos["sl_pct"]:
                reason = "stop_loss"
            elif t.get("exit_on_signal_gone") and not (info["sections"] & entry_secs):
                reason = "signal_gone"
            if reason:
                rec = close_position(state, cfg, ticker, price, reason)
                events.append({"type": "close", "rec": rec})

        # 2) 检查新开仓
        for ticker, info in pm.items():
            if _should_enter(cfg, ticker, info, state):
                pos = open_position(state, cfg, ticker, info)
                events.append({"type": "open", "pos": pos})

        save_state(state)
    return events


def get_snapshot(cfg, parsed=None):
    """给面板用的当前快照: 余额、持仓(含实时浮盈)、历史、统计。"""
    with _LOCK:
        state = load_state(cfg)
    pm = build_price_map(parsed) if parsed else {}
    open_list = []
    floating = 0.0
    for ticker, pos in state["open"].items():
        info = pm.get(ticker)
        price = info["price"] if info else pos["entry_price"]
        pnl_usdt, pnl_pct = _pnl(pos, price)
        floating += pnl_usdt
        open_list.append({**pos, "cur_price": price,
                          "pnl_usdt": round(pnl_usdt, 2), "pnl_pct": round(pnl_pct, 1)})
    sb = state["starting_balance"]
    equity = state["balance"] + sum(p["stake"] for p in state["open"].values()) + floating
    total_return = (equity - sb) / sb * 100
    realized = sum(c["pnl_usdt"] for c in state["closed"])
    return {
        "balance": round(state["balance"], 2),
        "equity": round(equity, 2),
        "starting_balance": sb,
        "floating_pnl": round(floating, 2),
        "realized_pnl": round(realized, 2),
        "total_return_pct": round(total_return, 1),
        "open": open_list,
        "closed": state["closed"],
        "stats": state["stats"],
    }
