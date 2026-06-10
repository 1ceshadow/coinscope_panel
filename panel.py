#!/usr/bin/env python3
"""三和妖币收割机 —— 本地面板服务。

功能:
  1. 后台定时拉取 dashboard 数据并缓存
  2. 提供本地网页面板 (实时刷新)
  3. 检测到新的高就绪度信号时推送 (Telegram / Server酱)

只用 Python 标准库。运行: python3 panel.py
"""
import json
import time
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fetcher
import paper_trader

BASE = Path(__file__).parent

# 全局缓存
STATE = {
    "data": None,        # 最近一次解析结果
    "error": None,       # 最近一次错误
    "updated": None,     # 最近更新时间戳
    "alerted": set(),    # 已推送过的信号 key, 避免重复
}
LOCK = threading.Lock()


def send_telegram(cfg, text):
    a = cfg["alert"]
    url = f"https://api.telegram.org/bot{a['telegram_token']}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": a["telegram_chat_id"], "text": text, "parse_mode": "HTML",
    }).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=15)
        return True, None
    except Exception as e:
        return False, str(e)


def send_serverchan(cfg, title, text):
    """Server酱 (微信推送, 国内可用)。需在 sct.ftqq.com 申请 SendKey。"""
    key = cfg["alert"]["serverchan_sendkey"]
    url = f"https://sctapi.ftqq.com/{key}.send"
    payload = urllib.parse.urlencode({"title": title, "desp": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=15)
        return True, None
    except Exception as e:
        return False, str(e)


def dispatch_alert(cfg, title, text):
    ch = cfg["alert"].get("channel", "none")
    if ch == "telegram":
        return send_telegram(cfg, f"<b>{title}</b>\n{text}")
    if ch == "serverchan":
        return send_serverchan(cfg, title, text)
    return False, "未配置推送渠道"


def check_alerts(cfg, parsed):
    """扫描新信号，对符合条件的推送。返回推送条数。"""
    a = cfg["alert"]
    if not a.get("enabled"):
        return 0
    watch = set(a.get("watch_sections", []))
    min_ready = a.get("min_readiness", 60)
    sent = 0
    for sec in parsed["sections"]:
        if sec["key"] not in watch:
            continue
        for it in sec["items"]:
            ready = it.get("readiness") or 0
            if ready < min_ready:
                continue
            # 去重 key: 板块+币+方向 (同一信号只推一次)
            akey = f"{sec['key']}:{it['ticker']}:{it['directionCode']}"
            with LOCK:
                if akey in STATE["alerted"]:
                    continue
                STATE["alerted"].add(akey)
            title = f"妖币信号 {it['ticker']} ({it['direction']})"
            text = (f"板块: {sec['label']}\n"
                    f"阶段: {it['stage']}  就绪度: {ready}\n"
                    f"24h: {it['change24h']}%  OI1h: {it['oi1h']}%\n"
                    f"动作: {it['actionLabel']}\n"
                    f"理由: {it['why'][:120]}")
            ok, err = dispatch_alert(cfg, title, text)
            if ok:
                sent += 1
            else:
                print(f"[推送失败] {akey}: {err}")
    return sent


def poll_loop(cfg):
    """后台轮询线程。"""
    interval = cfg.get("poll_interval_seconds", 180)
    while True:
        data, err = fetcher.fetch_dashboard(cfg)
        with LOCK:
            if err:
                STATE["error"] = err
                print(f"[{time.strftime('%H:%M:%S')}] 抓取失败: {err}")
            else:
                parsed = fetcher.parse_dashboard(data)
                STATE["data"] = parsed
                STATE["error"] = None
                STATE["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                n_sec = len(parsed["sections"])
                print(f"[{time.strftime('%H:%M:%S')}] 已更新 {n_sec} 个板块")
        # 告警检测在锁外做(内部自带锁)
        if not err and STATE["data"]:
            n = check_alerts(cfg, STATE["data"])
            if n:
                print(f"  -> 推送了 {n} 条信号")
            # 模拟交易引擎评估
            try:
                events = paper_trader.evaluate(cfg, STATE["data"])
                for ev in events:
                    if ev["type"] == "open":
                        p = ev["pos"]
                        print(f"  [开仓] {p['ticker']} {p['side']} {p['leverage']}x @ {p['entry_price']}")
                    else:
                        r = ev["rec"]
                        print(f"  [平仓] {r['ticker']} {r['reason']} {r['pnl_pct']:+.1f}% ({r['pnl_usdt']:+.2f}U)")
            except Exception as e:
                print(f"  [交易引擎错误] {type(e).__name__}: {e}")
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默访问日志

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        body = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = (BASE / "static" / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path.startswith("/api/data"):
            with LOCK:
                payload = {
                    "data": STATE["data"],
                    "error": STATE["error"],
                    "updated": STATE["updated"],
                }
            self._send(200, json.dumps(payload, ensure_ascii=False))
        elif self.path.startswith("/api/trades"):
            cfg = fetcher.load_config()
            with LOCK:
                parsed = STATE["data"]
            try:
                snap = paper_trader.get_snapshot(cfg, parsed)
                self._send(200, json.dumps(snap, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"}))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    cfg = fetcher.load_config()
    # 启动时先同步拉一次，保证页面立即有数据
    print("启动中，首次抓取...")
    data, err = fetcher.fetch_dashboard(cfg)
    with LOCK:
        if err:
            STATE["error"] = err
            print("首次抓取失败:", err)
        else:
            STATE["data"] = fetcher.parse_dashboard(data)
            STATE["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"首次抓取成功，{len(STATE['data']['sections'])} 个板块")

    t = threading.Thread(target=poll_loop, args=(cfg,), daemon=True)
    t.start()

    port = cfg.get("server_port", 8090)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n面板已启动: http://127.0.0.1:{port}")
    print(f"轮询间隔: {cfg.get('poll_interval_seconds', 180)} 秒")
    print(f"推送: {'开启 (' + cfg['alert']['channel'] + ')' if cfg['alert']['enabled'] else '关闭'}")
    print("按 Ctrl+C 停止\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

