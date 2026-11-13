"""CloudMail 客户端（vendored，供注册脚本独立运行）。

凭据从环境变量或本目录 mail_credentials.json 读取，不写入代码。
"""
import json
import os
import re
import time
from pathlib import Path

import requests

_HERE = Path(__file__).resolve().parent


def _load_creds() -> dict:
    env = {
        "base": os.getenv("CLOUDMAIL_BASE_URL"),
        "email": os.getenv("CLOUDMAIL_ADMIN_EMAIL"),
        "password": os.getenv("CLOUDMAIL_ADMIN_PASSWORD"),
        "domain": os.getenv("CLOUDMAIL_DOMAIN"),
    }
    f = _HERE / "mail_credentials.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for k_env, k_file in (("base", "base"), ("email", "email"),
                                  ("password", "password"), ("domain", "domain")):
                if not env[k_env] and data.get(k_file):
                    env[k_env] = data[k_file]
        except Exception:
            pass
    return env


_creds = _load_creds()
BASE = _creds.get("base") or ""
ADMIN_EMAIL = _creds.get("email") or ""
ADMIN_PASSWORD = _creds.get("password") or ""
DOMAIN = _creds.get("domain") or ""
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

_CODE_PATTERNS = [
    re.compile(r"verification\s+code[^0-9]{0,80}?(\d{6})", re.I),
    re.compile(r"code[^0-9]{0,60}?(\d{6})", re.I),
    re.compile(r"(?:passcode|pin|otp|one[-\s]?time)[^0-9]{0,50}?(\d{6})", re.I),
    re.compile(r"验证码[^0-9]{0,30}?(\d{6})"),
    re.compile(r"(\d{3})\s*-\s*(\d{3})"),
]


class CloudMail:
    def __init__(self, timeout=25):
        self.sess = requests.Session()
        self.sess.headers["User-Agent"] = UA
        self.timeout = timeout
        self.token = None

    def login(self):
        last = None
        for attempt in range(4):
            try:
                r = self.sess.post(BASE + "/api/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=self.timeout)
                try:
                    d = r.json()
                except Exception:
                    last = RuntimeError(f"login non-json (HTTP {r.status_code})")
                    time.sleep(10 + attempt * 15)
                    continue
                if d.get("code") != 200:
                    raise RuntimeError(f"login failed: {d}")
                self.token = d["data"]["token"]
                return self.token
            except requests.RequestException as e:
                last = e
                time.sleep(10 + attempt * 15)
        raise RuntimeError(f"login failed after retries: {last}")

    def _call(self, method, path, **kw):
        kw.setdefault("timeout", self.timeout)
        extra_headers = kw.pop("headers", {}) or {}
        last_exc = None
        for attempt in range(3):
            try:
                tok = self.token or self.login()
                hdrs = dict(extra_headers)
                hdrs["Authorization"] = tok
                r = self.sess.request(method, BASE + path, headers=hdrs, **kw)
                # 限流 / 网关错误：退避后重试
                if r.status_code in (429, 500, 502, 503, 504):
                    last_exc = RuntimeError(f"{path} HTTP {r.status_code}")
                    time.sleep(8 + attempt * 12)
                    continue
                try:
                    d = r.json()
                except Exception:
                    last_exc = RuntimeError(f"{path} non-json response (HTTP {r.status_code})")
                    time.sleep(8 + attempt * 12)
                    continue
                if d.get("code") == 401 and attempt < 2:
                    self.token = None
                    continue
                if d.get("code") != 200:
                    raise RuntimeError(f"{path} failed: {d}")
                return d.get("data")
            except requests.RequestException as e:
                last_exc = e
                time.sleep(8 + attempt * 12)
        raise RuntimeError(f"{path} failed after retries: {last_exc}")

    def create_address(self, local=None):
        if not local:
            import secrets
            import string
            local = "tf" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
        addr = f"{local}@{DOMAIN}"
        d = self._call("POST", "/api/account/add", json={"email": addr})
        return addr, d.get("accountId")

    def list_latest(self, size=50):
        d = self._call("GET", "/api/allEmail/list", params={"num": 1, "size": size})
        return (d or {}).get("list") or []

    def mark(self, addr):
        ids = [m["emailId"] for m in self.list_latest() if (m.get("toEmail") or "").lower() == addr.lower() and isinstance(m.get("emailId"), int)]
        return max(ids) if ids else 0

    @staticmethod
    def _strip_html(s):
        s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s or "")
        s = re.sub(r"(?s)<[^>]+>", " ", s)
        return re.sub(r"\s+", " ", s)

    @classmethod
    def extract_code(cls, msg):
        body = cls._strip_html(msg.get("content") or "")
        whole = (msg.get("subject") or "") + "\n" + body
        for pat in _CODE_PATTERNS:
            m = pat.search(whole)
            if not m:
                continue
            if pat.groups == 2:
                return m.group(1) + m.group(2)
            code = m.group(1)
            if re.match(r"^(?:19|20)\d{2}$", code):
                continue
            return code
        for m in re.finditer(r"(?<!\d)(\d{6})(?!\d)", whole):
            if not re.match(r"^(?:19|20)\d{2}$", m.group(1)):
                return m.group(1)
        return None

    def wait_code(self, addr, after_id=0, timeout=240, interval=4.0):
        t0 = time.time()
        errors = 0
        while time.time() - t0 < timeout:
            try:
                for m in self.list_latest():
                    if (m.get("toEmail") or "").lower() != addr.lower():
                        continue
                    mid = m.get("emailId")
                    if after_id and isinstance(mid, int) and mid <= after_id:
                        continue
                    code = self.extract_code(m)
                    if code:
                        return code, m
                errors = 0
            except Exception:
                errors += 1
                if errors >= 3:
                    # 连续失败说明被限流：重置会话并拉长间隔
                    try:
                        self.token = None
                        self.login()
                    except Exception:
                        pass
                    time.sleep(15)
            time.sleep(interval)
        return None, None
