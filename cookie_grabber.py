#!/usr/bin/env python3
"""自动从本地 Chrome 读取 sanhe6 的登录 cookie。

原理:
  Chrome 在 Linux 下把 cookie 用 AES-128-CBC (v11) 加密存在 SQLite 库里，
  加密密钥存在系统钥匙串 (KWallet 或 GNOME Keyring)。
  本模块取出密钥、解密 cookie，拼成可直接用于请求的 Cookie 字符串。

依赖: pycryptodome (Crypto)。钥匙串工具: kwallet-query 或 secret-tool。
仅读取本机当前用户自己的 Chrome 数据，等价于你自己在浏览器里的登录态。
"""
import sqlite3
import hashlib
import shutil
import subprocess
import tempfile
import os
from pathlib import Path

try:
    from Crypto.Cipher import AES
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

# Chrome 系浏览器的 cookie 库候选路径
COOKIE_DB_CANDIDATES = [
    "~/.config/google-chrome/Default/Cookies",
    "~/.config/chromium/Default/Cookies",
    "~/.config/microsoft-edge/Default/Cookies",
    "~/.config/BraveSoftware/Brave-Browser/Default/Cookies",
]

# 各浏览器在钥匙串里的密钥名 (folder, key/label)
KEYRING_NAMES = [
    ("Chrome Safe Storage", "Chrome Keys"),
    ("Chromium Safe Storage", "Chromium Keys"),
    ("Microsoft Edge Safe Storage", "Microsoft Edge Keys"),
    ("Brave Safe Storage", "Brave Keys"),
]


def _find_cookie_db():
    for c in COOKIE_DB_CANDIDATES:
        p = Path(os.path.expanduser(c))
        if p.exists():
            return p
    return None


def _get_storage_password():
    """从 KWallet 或 GNOME Keyring 取 Chrome 存储密钥。失败返回 'peanuts'(默认回退)。"""
    # 1) KWallet
    if shutil.which("kwallet-query"):
        for label, folder in KEYRING_NAMES:
            try:
                r = subprocess.run(
                    ["kwallet-query", "-r", label, "-f", folder, "kdewallet"],
                    capture_output=True, text=True, timeout=10)
                out = r.stdout.strip()
                if out and "不存在" not in out and "not found" not in out.lower():
                    return out
            except Exception:
                pass
    # 2) GNOME Keyring (secret-tool)
    if shutil.which("secret-tool"):
        for label, _ in KEYRING_NAMES:
            try:
                r = subprocess.run(["secret-tool", "lookup", "label", label],
                                   capture_output=True, text=True, timeout=10)
                if r.stdout.strip():
                    return r.stdout.strip()
            except Exception:
                pass
    # 3) 回退默认
    return "peanuts"


# PLACEHOLDER_DECRYPT
def _decrypt_value(enc, key):
    """解密单个 Chrome cookie 值 (v10/v11)。"""
    if enc[:3] not in (b"v10", b"v11"):
        try:
            return enc.decode("utf-8")
        except Exception:
            return None
    cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
    dec = cipher.decrypt(enc[3:])
    if not dec:
        return None
    dec = dec[:-dec[-1]]  # 去 PKCS7 填充
    # 新版 Chrome (v130+) 在明文前加 32 字节 domain hash
    try:
        return dec.decode("utf-8")
    except UnicodeDecodeError:
        return dec[32:].decode("utf-8", "replace")


def grab_cookie(domain="sanhe6.com"):
    """读取并解密指定域名的全部 cookie，返回 (cookie_str, error)。"""
    if not _HAS_CRYPTO:
        return None, "缺少 pycryptodome，请先安装: pip install pycryptodome"
    db = _find_cookie_db()
    if not db:
        return None, "未找到 Chrome cookie 库，请确认已安装并登录过 Chrome"

    # 复制一份再读，避免 Chrome 正在占用导致锁库
    tmp = Path(tempfile.gettempdir()) / "_cs_cookies.db"
    try:
        shutil.copy2(db, tmp)
    except Exception as e:
        return None, f"复制 cookie 库失败: {e}"

    password = _get_storage_password()
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1, 16)

    try:
        con = sqlite3.connect(str(tmp))
        rows = con.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",)).fetchall()
        con.close()
    except Exception as e:
        return None, f"读取 cookie 库失败: {e}"
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

    if not rows:
        return None, f"Chrome 里没有 {domain} 的 cookie，请先在 Chrome 登录该网站"

    pairs = []
    for name, enc in rows:
        val = _decrypt_value(enc, key)
        if val:
            pairs.append(f"{name}={val}")
    if not pairs:
        return None, "cookie 解密失败 (可能 Chrome 密钥获取不正确)"
    return "; ".join(pairs), None


def refresh_config_cookie(config_path):
    """抓取最新 cookie 并写回 config.json 的 cookie 字段。返回 (ok, msg)。"""
    import json
    cookie, err = grab_cookie()
    if err:
        return False, err
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["cookie"] = cookie
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True, f"已更新 cookie ({len(cookie)} 字符)"


if __name__ == "__main__":
    cookie, err = grab_cookie()
    if err:
        print("失败:", err)
    else:
        print("成功抓取 cookie:")
        print(cookie)
