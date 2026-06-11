#!/usr/bin/env python3
"""三和妖币收割机 —— 本地面板服务。

功能:
  1. 后台定时拉取 dashboard 数据并缓存
  2. 提供本地网页面板 (实时刷新)
  3. 检测到新的高就绪度信号时推送 (Telegram)

只用 Python 标准库。运行: python3 panel.py
"""
import json
import time
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import base64

import fetcher
try:
    import cookie_grabber
    _HAS_GRABBER = True
except Exception:
    _HAS_GRABBER = False

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


def try_refresh_cookie(cfg):
    """从 Chrome 自动抓取最新 cookie 并更新到内存中的 cfg 和 config.json。"""
    if not _HAS_GRABBER:
        return False, "cookie_grabber 模块不可用"
    cookie, err = cookie_grabber.grab_cookie()
    if err:
        return False, err
    if cookie == cfg.get("cookie"):
        return False, "Chrome 里的 cookie 与当前相同(可能也已失效，请在浏览器重新登录)"
    cfg["cookie"] = cookie  # 更新内存
    try:
        ok, msg = cookie_grabber.refresh_config_cookie(fetcher.CONFIG_PATH)
        return ok, msg
    except Exception as e:
        return True, f"内存已更新，但写回文件失败: {e}"


def fetch_freqtrade(cfg, path):
    """读取 freqtrade REST API。失败返回 (None, err)。"""
    ft = cfg.get("freqtrade", {})
    base = ft.get("api_url", "http://127.0.0.1:8080")
    user = ft.get("username", "freqtrader")
    pw = ft.get("password", "")
    url = f"{base}/api/v1/{path}"
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def freqtrade_snapshot(cfg):
    """汇总 freqtrade 真实(dry-run)交易状态: 持仓、历史、盈亏、余额。"""
    status, e1 = fetch_freqtrade(cfg, "status")        # 当前持仓列表
    profit, e2 = fetch_freqtrade(cfg, "profit")        # 累计盈亏统计
    balance, e3 = fetch_freqtrade(cfg, "balance")      # 账户余额
    hist, e4 = fetch_freqtrade(cfg, "trades?limit=50")  # 已平仓历史
    err = e1 or e2 or e3
    if err and status is None:
        return {"error": f"无法连接 freqtrade ({err})，请确认容器在运行且 8080 端口可达"}
    closed = []
    if hist and isinstance(hist, dict):
        for t in hist.get("trades", []):
            if t.get("is_open"):
                continue
            closed.append(t)
        closed.reverse()  # 最新在前
    return {
        "open": status or [],
        "closed": closed,
        "profit": profit or {},
        "balance": balance or {},
    }


def build_pairlist(cfg, parsed):
    """把 sanhe6 入场信号转成币安期货交易对白名单 (freqtrade RemotePairList 格式)。

    规则: 取 entry_sections 指定板块里、就绪度>=阈值、(可选)只做多的币，
    转成 'ARK/USDT:USDT' 这种币安永续格式。去重并保持顺序。
    """
    pl = cfg.get("pairlist", {})
    entry_secs = set(pl.get("entry_sections", ["entryWindow", "opportunities"]))
    min_ready = pl.get("min_readiness", 60)
    only_long = pl.get("only_long", True)
    quote = pl.get("quote", "USDT")
    if not parsed:
        return []
    seen = set()
    pairs = []
    for sec in parsed.get("sections", []):
        if sec["key"] not in entry_secs:
            continue
        for it in sec["items"]:
            tk = it["ticker"]
            if not tk or tk in seen:
                continue
            if (it.get("readiness") or 0) < min_ready:
                continue
            if only_long and it.get("directionCode") == "SHORT":
                continue
            seen.add(tk)
            pairs.append(f"{tk}/{quote}:{quote}")
    return pairs


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
        # 登录失效时，尝试自动从 Chrome 重新抓 cookie 并重试一次
        if err and "登录失效" in err and cfg.get("auto_cookie", True):
            ok, msg = try_refresh_cookie(cfg)
            if ok:
                print(f"[{time.strftime('%H:%M:%S')}] cookie 已自动刷新，重试抓取")
                data, err = fetcher.fetch_dashboard(cfg)
            else:
                print(f"[{time.strftime('%H:%M:%S')}] 自动刷新 cookie 失败: {msg}")
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
            # 读取 freqtrade 真实(dry-run)交易状态
            cfg = fetcher.load_config()
            try:
                snap = freqtrade_snapshot(cfg)
                self._send(200, json.dumps(snap, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"}))
        elif self.path.startswith("/api/refresh-cookie"):
            cfg = fetcher.load_config()
            ok, msg = try_refresh_cookie(cfg)
            self._send(200, json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))
        elif self.path.startswith("/api/pairlist"):
            # 给 freqtrade RemotePairList 用: 把 sanhe6 入场信号转成币安期货白名单
            cfg = fetcher.load_config()
            with LOCK:
                parsed = STATE["data"]
            pairs = build_pairlist(cfg, parsed)
            refresh = cfg.get("pairlist", {}).get("refresh_period", 300)
            self._send(200, json.dumps({"pairs": pairs, "refresh_period": refresh},
                                       ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))


def main():
    cfg = fetcher.load_config()
    # 启动时若开启自动 cookie，先从 Chrome 抓一次最新登录态
    if cfg.get("auto_cookie", True) and _HAS_GRABBER:
        ok, msg = try_refresh_cookie(cfg)
        print(f"自动 cookie: {msg}" if ok else f"自动 cookie 跳过: {msg}")
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
    print(f"轮询间隔: {cfg.get('poll_interval_seconds', 60)} 秒")
    print(f"推送: {'开启 (' + cfg['alert']['channel'] + ')' if cfg['alert']['enabled'] else '关闭'}")
    print("按 Ctrl+C 停止\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

