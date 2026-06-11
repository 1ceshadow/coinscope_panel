# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
"""SanheStrategy —— sanhe6 妖币信号 + 币安实时确认 的组合策略

设计思路:
  - 选币(盯哪些): 由 RemotePairList 从 panel 的 /api/pairlist 动态提供,
    即 sanhe6 入场窗口/确认候选里就绪度达标的币。本策略不管选币。
  - 进场(何时买): 对这些候选币,用币安实时K线做"最优进场确认",
    多因子同时满足才进场,过滤 sanhe6 价格滞后带来的追高风险。
  - 出场(何时卖): ROI 阶梯止盈 + 固定止损 + 趋势走坏提前离场。

进场确认逻辑(全部满足):
  1. 趋势: 快EMA 在 慢EMA 之上 (EMA9 > EMA21), 价格在 EMA21 之上
  2. 突破: 收盘价突破前 N 根的最高价 (动量启动)
  3. 量能: 成交量 > 近期均量 * 倍数 (有资金进场)
  4. 不过热: RSI 在合理区间 (不追已超买的)

⚠️ 10x 杠杆: 价格反向 ~10% 即爆仓。止损必须严格。
"""
from datetime import datetime
from typing import Optional

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade


class SanheStrategy(IStrategy):
    INTERFACE_VERSION = 3

    # 做多为主(妖币策略基本偏多); 期货可做空但本策略只做多
    can_short: bool = False

    timeframe = "5m"

    # ROI 阶梯止盈 (相对名义价值的百分比, freqtrade 已按杠杆换算)
    # 注意: 这里的值是价格变动比例, 10x 下 0.03=价格涨3%=保证金+30%
    minimal_roi = {
        "0": 0.05,
        "30": 0.03,
        "60": 0.015,
        "120": 0.008,
    }

    # 止损: 价格跌 3% (10x 下 = 保证金 -30%)。10x 不能设太松否则一把亏光
    stoploss = -0.03

    # 移动止损: 盈利后锁利
    trailing_stop = True
    trailing_stop_positive = 0.01
    trailing_stop_positive_offset = 0.025
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    startup_candle_count: int = 50

    # 杠杆 (在 leverage() 方法里返回)
    leverage_num = IntParameter(1, 20, default=10, space="buy", optimize=False)

    # —— 可调参数 (供 hyperopt / 手动调) ——
    ema_fast = IntParameter(5, 15, default=9, space="buy", optimize=False)
    ema_slow = IntParameter(15, 30, default=21, space="buy", optimize=False)
    breakout_lookback = IntParameter(5, 30, default=12, space="buy", optimize=False)
    volume_factor = DecimalParameter(1.0, 3.0, default=1.5, space="buy", optimize=False)
    rsi_max = IntParameter(60, 85, default=75, space="buy", optimize=False)

    # PLACEHOLDER_METHODS
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=self.ema_fast.value)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=self.ema_slow.value)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        # 近期最高价(不含当前根, 用于突破判断)
        lb = self.breakout_lookback.value
        dataframe["recent_high"] = dataframe["high"].rolling(lb).max().shift(1)
        # 近期均量
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cond = (
            # 1. 趋势向上
            (dataframe["ema_fast"] > dataframe["ema_slow"])
            & (dataframe["close"] > dataframe["ema_slow"])
            # 2. 突破近期高点(动量启动)
            & (dataframe["close"] > dataframe["recent_high"])
            # 3. 量能放大
            & (dataframe["volume"] > dataframe["vol_ma"] * self.volume_factor.value)
            # 4. 未超买
            & (dataframe["rsi"] < self.rsi_max.value)
            # 有效成交量
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, ["enter_long", "enter_tag"]] = (1, "sanhe_confirm")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 趋势走坏提前离场: 快EMA 跌破 慢EMA
        cond = (
            (dataframe["ema_fast"] < dataframe["ema_slow"])
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[cond, ["exit_long", "exit_tag"]] = (1, "trend_break")
        return dataframe

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, side: str,
                 **kwargs) -> float:
        return min(float(self.leverage_num.value), max_leverage)

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs) -> Optional[str]:
        # sanhe6 信号消失 -> 该币会从 RemotePairList 白名单移除。
        # freqtrade 不会自动平掉已移除白名单的持仓, 这里主动检测:
        # 若当前持仓的币已不在动态白名单中, 且已有微利或持有超过一定时间, 则离场。
        try:
            whitelist = self.dp.current_whitelist()
        except Exception:
            whitelist = []
        if pair not in whitelist:
            hold_min = (current_time - trade.open_date_utc).total_seconds() / 60
            if current_profit > 0.003 or hold_min > 60:
                return "signal_gone"
        return None

