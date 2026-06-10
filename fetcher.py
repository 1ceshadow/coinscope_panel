#!/usr/bin/env python3
"""数据抓取与解析模块 —— 从三和妖币收割机拉取 dashboard 并归一化。

只用 Python 标准库，无第三方依赖。
"""
import json
import gzip
import time
import urllib.request
import urllib.error
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch_dashboard(cfg):
    """拉取 dashboard 原始 JSON。返回 (data_dict, error_str)。"""
    url = cfg["base_url"] + cfg["dashboard_path"]
    req = urllib.request.Request(url, headers={
        "accept": "application/json",
        "accept-encoding": "gzip",
        "accept-language": "zh-CN,zh;q=0.9",
        "cookie": cfg["cookie"],
        "referer": cfg["base_url"] + "/coin",
        "user-agent": cfg["user_agent"],
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            return data, None
    except urllib.error.HTTPError as e:
        if e.code in (302, 401, 403):
            return None, f"登录失效 (HTTP {e.code})，请更新 config.json 里的 cookie"
        return None, f"HTTP错误 {e.code}: {e.reason}"
    except Exception as e:
        return None, f"抓取失败: {type(e).__name__}: {e}"


# 板块 key -> 中文标签
SECTION_LABELS = {
    "entryWindow": "入场窗口", "earlyEntryRadar": "早发现雷达", "earlyRadar": "早期雷达",
    "watchlistItems": "我的关注", "opportunities": "确认/回踩候选", "riskWinnersReview": "复盘警示",
    "repeatCandidateWatch": "多次出现", "oiAnomalyWatch": "OI异动", "breakoutReview": "已启动复盘",
    "delistRiskWatch": "公告风险", "accumulationWatch": "蓄势池", "risingAttention": "热度升温",
    "marketWatch": "市场确认", "futuresMovers": "合约异动", "overheated": "过热回避",
    "recentSignalChanges": "信号轨迹", "teacherWatch": "老师观察", "liquidationWatch": "清算观察",
    "mainstreamReference": "主流参照",
}

# 面板展示顺序（重要的在前）
SECTION_ORDER = [
    "entryWindow", "opportunities", "earlyEntryRadar", "earlyRadar", "oiAnomalyWatch",
    "breakoutReview", "accumulationWatch", "repeatCandidateWatch", "futuresMovers",
    "risingAttention", "marketWatch", "riskWinnersReview", "overheated",
    "recentSignalChanges", "liquidationWatch", "teacherWatch", "mainstreamReference",
    "delistRiskWatch", "watchlistItems",
]


def _safe(d, *keys, default=None):
    """安全地链式取值: _safe(item, 'market', 'oiWindows', 'm5')"""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def parse_item(it):
    """把单个币条目归一化成面板用的精简结构。"""
    m = it.get("market", {}) or {}
    oi = m.get("oiWindows", {}) or {}
    action = _safe(it, "strategy", "action", default={}) or {}
    cls = _safe(it, "market", "contractLaunchSignal", default={}) or {}
    return {
        "ticker": it.get("ticker", "?"),
        "direction": it.get("direction", ""),
        "directionCode": it.get("directionCode", ""),
        "stage": it.get("stage", ""),
        "structure": it.get("opportunityStructure", ""),
        "heatScore": it.get("heatScore", 0),
        "longScore": _safe(it, "strategy", "scores", "longScore", default=0),
        "shortScore": _safe(it, "strategy", "scores", "shortScore", default=0),
        "price": m.get("markPrice"),
        "change24h": m.get("priceChangePercent"),
        "change6h": m.get("priceChange6h"),
        "quoteVolume": m.get("quoteVolume"),
        "fundingRate": m.get("fundingRate"),
        "confirmScore": m.get("confirmScore"),
        "oi5m": oi.get("m5"), "oi1h": oi.get("h1"), "oi6h": oi.get("h6"),
        "oi24h": oi.get("h24"), "oi30d": oi.get("d30"),
        "igniteActive": cls.get("active", False),
        "igniteLabel": cls.get("label", ""),
        "igniteScore": cls.get("score"),
        "actionLabel": action.get("label", ""),
        "actionSide": action.get("side", ""),
        "readiness": action.get("readiness"),
        "why": it.get("why", ""),
    }


def parse_dashboard(data):
    """返回 {generatedAt, counts, sections:[{key,label,items:[...]}]}"""
    out_sections = []
    for key in SECTION_ORDER:
        raw_list = data.get(key)
        if not isinstance(raw_list, list) or not raw_list:
            continue
        items = [parse_item(it) for it in raw_list if isinstance(it, dict)]
        out_sections.append({
            "key": key,
            "label": SECTION_LABELS.get(key, key),
            "count": len(items),
            "items": items,
        })
    return {
        "generatedAt": data.get("generatedAt"),
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": data.get("counts", {}),
        "sections": out_sections,
    }


if __name__ == "__main__":
    cfg = load_config()
    data, err = fetch_dashboard(cfg)
    if err:
        print("错误:", err)
    else:
        parsed = parse_dashboard(data)
        print(f"生成时间: {parsed['generatedAt']}")
        print(f"板块数: {len(parsed['sections'])}")
        for s in parsed["sections"][:3]:
            print(f"\n【{s['label']}】{s['count']}项")
            for it in s["items"][:3]:
                print(f"  {it['ticker']:8} {it['direction']:4} 24h:{it['change24h']}% 就绪:{it['readiness']}")
