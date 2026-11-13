# -*- coding: utf-8 -*-
"""纯协议注册 + 自动登录 + 导入反代机（一步到位）。

浏览器仅用于产出 reCAPTCHA token；其余全部走 curl + socks5。

    python scripts/register_and_import.py --count 1
    python scripts/register_and_import.py --count 1 --cookies-out data/cookies.json --no-import

产出:
    data/accounts.json   新注册账号（含密码），供反代机账号池使用
    data/cookies.json    最新账号的会话 cookie
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import string
import subprocess
import sys
import time
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@contextmanager
def accounts_file_lock(path: Path, timeout: float = 30.0):
    """与 tfproxy AccountPool 共用同一 *.lock，跨进程互斥写账号文件。"""
    lock_path = path.with_name(path.name + ".lock")
    fh = None
    locked = False
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fh = open(lock_path, "a+")
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if fh:
                    fh.close()
                    fh = None
                if time.time() > deadline:
                    break
                time.sleep(0.2)
        yield locked
    finally:
        if fh:
            try:
                if locked and os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def acquire_instance_lock(path: Path):
    """注册单实例锁。

    多个注册进程会共享同一个 CloudMail 管理员会话：一个进程重新登录会把另一个
    踢下线，双方不停 401/503，结果是"邮件已经到了但读不到验证码"，白白烧掉
    收信额度。这里保证同一时间只有一个注册进程。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except OSError:
        fh.close()
        return None


def _load_pending(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_pending(path: Path, items: list) -> None:
    try:
        atomic_write_json(path, items)
    except Exception:
        pass


def _pending_add(path: Path, entry: dict) -> None:
    items = [x for x in _load_pending(path) if x.get("email") != entry.get("email")]
    items.append(entry)
    _save_pending(path, items)


def _pending_remove(path: Path, email: str) -> None:
    items = [x for x in _load_pending(path) if x.get("email") != email]
    _save_pending(path, items)

# 邮箱组件：优先用本目录下的 vendored cloudmail.py，其次找 tokenfactory/tools
_HERE = Path(__file__).resolve().parent
_TOOLS = Path(r"C:\Users\39455\workspace\tokenfactory\tools")
if (_HERE / "cloudmail.py").exists():
    sys.path.insert(0, str(_HERE))
elif _TOOLS.exists():
    sys.path.insert(0, str(_TOOLS))

BASE = "https://tokenfactory.kt.com"
SOCKS = "127.0.0.1:10808"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
SITE_KEYS = [
    "6Ld6CAEuAAAAABMcfRzFiXfSsiPjCgehCytp1laq",
    "6LeZEBcuAAAAAMQOk0zy4wrzZPzUbBSeHRnD22eM",
]
TERM_CONSENTS = [
    {"termId": 1, "versionId": 1},
    {"termId": 4, "versionId": 4},
    {"termId": 5, "versionId": 5},
    {"termId": 8, "versionId": 8},
]

_CODE_PATTERNS = [
    re.compile(r"(?:인증\s*코드|verification\s*code)[^0-9]{0,60}?(\d{6})", re.I),
    re.compile(r"code[^0-9]{0,60}?(\d{6})", re.I),
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
]


# ---------------- HTTP (curl over socks5) ----------------

def _curl(method, path, body=None, headers=None, cookie="", timeout=120, want_headers=False):
    cmd = ["curl.exe", "-k", "--ssl-no-revoke", "--socks5-hostname", SOCKS,
           "-sS", "-X", method.upper(), BASE + path,
           "-H", "Accept: */*",
           "-H", "Accept-Language: ko-KR,ko;q=0.9",
           "-H", f"Origin: {BASE}",
           "-H", f"User-Agent: {UA}",
           "-H", "sec-fetch-site: same-origin",
           "-H", "sec-fetch-mode: cors",
           "-H", "sec-fetch-dest: empty"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    if cookie:
        cmd += ["-H", f"Cookie: {cookie}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if want_headers:
        cmd += ["-D", "-"]
    cmd += ["-w", "\n__HTTP__:%{http_code}"]

    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", "replace")
    head = ""
    if want_headers and "\r\n\r\n" in out:
        head, _, out = out.partition("\r\n\r\n")
    text, _, code = out.rpartition("__HTTP__:")
    try:
        status = int(code.strip())
    except Exception:
        status = None
    js = None
    try:
        js = json.loads(text)
    except Exception:
        pass
    set_cookies = {}
    for line in head.splitlines():
        if line.lower().startswith("set-cookie:"):
            sc = SimpleCookie()
            try:
                sc.load(line.split(":", 1)[1].strip())
                for k, m in sc.items():
                    set_cookies[k] = m.value
            except Exception:
                pass
    return status, js, text.strip()[:800], set_cookies


# ---------------- reCAPTCHA ----------------

def mint_token(pool, action="signup"):
    """用真 Chrome 页面内执行 grecaptcha.enterprise.execute 取 token。"""
    page = pool.get_page()
    page.goto(BASE + "/signup", wait_until="domcontentloaded", timeout=90000)
    page.wait_for_function(
        "() => window.grecaptcha && window.grecaptcha.enterprise "
        "&& typeof window.grecaptcha.enterprise.execute === 'function'",
        timeout=60000,
    )
    last = None
    for sk in SITE_KEYS:
        try:
            tok = page.evaluate(
                """async (a) => { await new Promise(r => grecaptcha.enterprise.ready(r));
                     return await grecaptcha.enterprise.execute(a.sk, {action: a.action}); }""",
                {"sk": sk, "action": action},
            )
            if tok and len(tok) > 30:
                return tok
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"recaptcha mint failed: {last}")


class BrowserPool:
    """保持一个浏览器实例，多次注册复用，避免反复启动。"""

    def __init__(self, headless=True):
        self._pw = None
        self.browser = None
        self.ctx = None
        self.headless = headless
        self._page = None

    def start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            executable_path=CHROME, headless=self.headless,
            proxy={"server": f"socks5://{SOCKS}"},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check", "--disable-gpu"],
        )
        self.ctx = self.browser.new_context(locale="ko-KR", user_agent=UA,
                                            viewport={"width": 1366, "height": 900})

    def get_page(self):
        if self._page is None or self._page.is_closed():
            self._page = self.ctx.new_page()
        return self._page

    def reset(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = self.browser.new_context(locale="ko-KR", user_agent=UA,
                                            viewport={"width": 1366, "height": 900})
        self._page = None

    def close(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


# ---------------- 流程 ----------------

def rand_password(n=14):
    chars = string.ascii_letters + string.digits + "!@#"
    pw = [random.choice(string.ascii_lowercase), random.choice(string.ascii_uppercase),
          random.choice(string.digits), random.choice("!@#")]
    pw += [random.choice(chars) for _ in range(n - 4)]
    random.shuffle(pw)
    return "".join(pw)


def rand_nickname():
    return "tf" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8))


def encrypt_password(password: str, jwk: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    def b64u(s: str) -> bytes:
        s = s.replace("-", "+").replace("_", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4))

    n = int.from_bytes(b64u(jwk["n"]), "big")
    e = int.from_bytes(b64u(jwk["e"]), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    ct = pub.encrypt(password.encode(), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(ct).decode()


def extract_code(msg) -> str | None:
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", msg.get("content") or "")
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    whole = (msg.get("subject") or "") + "\n" + body
    for pat in _CODE_PATTERNS:
        m = pat.search(whole)
        if not m:
            continue
        code = m.group(1)
        if re.match(r"^(?:19|20)\d{2}$", code):
            continue
        return code
    return None


def wait_code(cm, addr, after_id=0, timeout=420, interval=4.0):
    """在最近 200 封里找该地址的验证码。

    旧实现只看最近 50 封、异常时静默空转：高峰期邮件一多，验证码被挤出窗口或
    列表接口限流，就会出现"信到了却读不到"，最后超时，白烧一封额度。
    """
    t0 = time.time()
    errors = 0
    while time.time() - t0 < timeout:
        try:
            for m in cm.list_latest(200):
                if (m.get("toEmail") or "").lower() != addr.lower():
                    continue
                mid = m.get("emailId")
                if after_id and isinstance(mid, int) and mid <= after_id:
                    continue
                c = extract_code(m)
                if c:
                    return c
            errors = 0
        except Exception as e:
            errors += 1
            print(f"    cloudmail poll error ({errors}): {str(e)[:120]}", flush=True)
            if errors >= 3:
                try:
                    cm.token = None
                    cm.login()
                except Exception:
                    pass
                time.sleep(10)
        time.sleep(interval)
    return None


PENDING_PATH: "Path | None" = None


def register_one(pool: BrowserPool, cm, idx: int) -> dict:
    """入口：出现任何异常都要把已完成的阶段信息带回来。

    旧版 main() 的 catch-all 只保留 stage/error，email 和 signupRequestId 丢掉，
    那封已经发出的验证邮件就彻底变成"死信"，无法恢复只能重发。
    """
    out = {"idx": idx, "ts": int(time.time())}
    try:
        return _register_one_impl(pool, cm, idx, out)
    except Exception as e:
        out.update(ok=False, stage="exception",
                   error=f"{type(e).__name__}: {str(e)[:300]}")
        return out


def _register_one_impl(pool: BrowserPool, cm, idx: int, out: dict) -> dict:

    # 建邮箱 + 游标，CloudMail 过载时退避重试
    email = cursor = None
    for attempt in range(5):
        try:
            email, aid = cm.create_address()
            cursor = cm.mark(email)
            break
        except Exception as e:
            if attempt == 4:
                out.update(ok=False, stage="cloudmail_create", error=str(e)[:300])
                return out
            print(f"    cloudmail busy ({str(e)[:80]}), retry {attempt + 1}/5 "
                  f"in {20 + attempt * 15}s", flush=True)
            time.sleep(20 + attempt * 15)
            try:
                cm.token = None
                cm.login()
            except Exception:
                pass
    password = rand_password()
    nickname = rand_nickname()
    out.update(email=email, accountId=aid, password=password, nickname=nickname)
    print(f"  mail={email} aid={aid}", flush=True)

    st, ek, tx, _ = _curl("GET", "/api/v1/auth/encryption-key")
    if st != 200 or not ek:
        out.update(ok=False, stage="encryption-key", error=tx)
        return out

    # 最多 4 次尝试：CMN-111 时换新 token
    for attempt in range(4):
        token = mint_token(pool, "signup")
        body = {
            "keyPairId": ek["keyPairId"],
            "email": email,
            "nickname": nickname,
            "encryptedPassword": encrypt_password(password, ek["publicKey"]),
            "termConsents": TERM_CONSENTS,
        }
        st, js, tx, _ = _curl("POST", "/api/v1/account/signup", body,
                              headers={"X-Recaptcha-Token": token})
        print(f"    signup attempt {attempt + 1}: {st} {json.dumps(js or tx, ensure_ascii=False)[:160]}", flush=True)
        if st in (200, 201) and js and js.get("signupRequestId"):
            out["signupRequestId"] = js["signupRequestId"]
            # 立刻落盘：这封验证邮件已经发出，超时/中断后要能回来继续用它
            if PENDING_PATH is not None:
                _pending_add(PENDING_PATH, {
                    "email": email,
                    "password": password,
                    "nickname": nickname,
                    "signupRequestId": js["signupRequestId"],
                    "cursor": cursor or 0,
                    "ts": int(time.time()),
                })
            break
        # 任何失败都重试（CMN-111 评分低 / 网关瞬时 403 / 限流），换新 token + 新上下文
        if attempt < 3:
            pool.reset()
            time.sleep(6 + attempt * 4)
            continue
        out.update(ok=False, stage="signup", error=js or tx, http=st)
        return out
    else:
        out.update(ok=False, stage="signup", error="retries exhausted")
        return out

    code = wait_code(cm, email, cursor, timeout=240)
    if not code:
        # 上游邮件可能整体停发：用官方 resend 接口兜底一次，再等一轮
        try:
            st_r, js_r, _, _ = _curl("POST", "/api/v1/account/email-verifications/resend",
                                     {"signupRequestId": out["signupRequestId"]})
            print(f"    resend: {st_r} {json.dumps(js_r or {}, ensure_ascii=False)[:80]}", flush=True)
        except Exception as e:
            print(f"    resend failed: {str(e)[:80]}", flush=True)
        code = wait_code(cm, email, cursor, timeout=240)
    if not code:
        out.update(ok=False, stage="mail_blocked",
                   error="no verification email (upstream mail delivery looks blocked)")
        return out
    out["emailCode"] = code
    print(f"    code={code}", flush=True)

    st, js, tx, _ = _curl("POST", "/api/v1/account/email-verifications/confirm",
                          {"signupRequestId": out["signupRequestId"], "code": code})
    if st != 200:
        out.update(ok=False, stage="confirm", error=js or tx, http=st)
        return out
    out["accountStatus"] = (js or {}).get("accountStatus")
    print(f"    confirmed: {out['accountStatus']}", flush=True)

    # 立刻登录拿会话
    sess = protocol_login(email, password, pool)
    if sess.get("ok"):
        out["cookies"] = sess["cookies"]
        out["login"] = "ok"
    else:
        out["login"] = "failed"
        out["loginError"] = sess.get("error")
    out["ok"] = True
    out["stage"] = "done"
    return out


def protocol_login(email, password, pool: BrowserPool | None = None, retries=4):
    st, ek, tx, _ = _curl("GET", "/api/v1/auth/encryption-key")
    if st != 200 or not ek:
        return {"ok": False, "error": f"encryption-key {st}"}
    enc = encrypt_password(password, ek["publicKey"])

    own_pool = pool is None
    if own_pool:
        pool = BrowserPool(headless=True)
        pool.start()
    try:
        for attempt in range(retries):
            try:
                token = mint_token(pool, "login")
            except Exception as e:
                return {"ok": False, "error": f"mint: {e}"}
            body = {"email": email, "recaptchaToken": token,
                    "keyPairId": ek["keyPairId"], "encryptedPassword": enc}
            st, js, tx, cookies = _curl("POST", "/internal/auth/login", body,
                                        want_headers=True)
            print(f"    login attempt {attempt + 1}: {st} {json.dumps(js or tx, ensure_ascii=False)[:120]}", flush=True)
            if st == 200 and cookies:
                return {"ok": True, "cookies": cookies}
            err = (js or {}).get("errorCode", "")
            if err == "CMN-111" and attempt < retries - 1:
                pool.reset()
                time.sleep(8)
                continue
            return {"ok": False, "error": js or tx, "http": st}
        return {"ok": False, "error": "login retries exhausted"}
    finally:
        if own_pool:
            pool.close()


def import_to_proxy(cookies: dict, cookies_path: Path, accounts_path: Path, account: dict):
    cookies_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": [{"name": k, "value": v, "domain": "tokenfactory.kt.com", "path": "/"}
                    for k, v in cookies.items()],
        "email": account.get("email"),
        "saved_at": int(time.time()),
    }
    cookies_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with accounts_file_lock(accounts_path) as locked:
        if not locked:
            print("  [warn] accounts.json lock timeout; write anyway")
        accounts = []
        if accounts_path.exists():
            try:
                accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
                if not isinstance(accounts, list):
                    accounts = []
            except Exception:
                accounts = []
        accounts = [a for a in accounts if a.get("email") != account.get("email")]
        accounts.append({
            "email": account.get("email"),
            "password": account.get("password"),
            "nickname": account.get("nickname"),
            "cookies": cookies,
            "created_at": int(time.time()),
        })
        atomic_write_json(accounts_path, accounts)


def _load_accounts(path: Path) -> list:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def resume_pending(cm, pool, accounts_path: Path, cookies_path: Path,
                   pending_path: Path, max_age_hours: float = 36.0) -> int:
    """被动恢复未完成的注册：只用已经收到的邮件，不再新发。

    超时中断时验证码往往已经躺在邮箱里了；这里把 pending_signups.json 里的号
    找回来（确认 + 登录 + 入库），避免重发邮件浪费收信额度。
    """
    items = _load_pending(pending_path)
    if not items:
        return 0
    known = {a.get("email") for a in _load_accounts(accounts_path)}
    recovered = 0
    for ent in items:
        email = ent.get("email")
        if not email:
            _pending_remove(pending_path, "")
            continue
        if email in known:
            _pending_remove(pending_path, email)
            continue
        if time.time() - float(ent.get("ts") or 0) > max_age_hours * 3600:
            print(f"  [pending] drop stale entry {email}", flush=True)
            _pending_remove(pending_path, email)
            continue
        code = None
        try:
            for m in cm.list_latest(200):
                if (m.get("toEmail") or "").lower() != email.lower():
                    continue
                c = extract_code(m)
                if c:
                    code = c
                    break
        except Exception as e:
            print(f"  [pending] inbox read failed: {str(e)[:100]}", flush=True)
        if not code:
            continue
        print(f"  [pending] found code for {email}, resuming", flush=True)
        st, js, tx, _ = _curl("POST", "/api/v1/account/email-verifications/confirm",
                              {"signupRequestId": ent.get("signupRequestId"), "code": code})
        if st != 200:
            # 可能已经确认过（重复确认会报错），继续尝试登录
            print(f"  [pending] confirm {st}: {str(js or tx)[:120]}", flush=True)
        sess = protocol_login(email, ent.get("password") or "", pool)
        if not sess.get("ok"):
            print(f"  [pending] login failed: {str(sess.get('error'))[:120]}", flush=True)
            continue
        import_to_proxy(sess["cookies"], cookies_path, accounts_path,
                        {"email": email, "password": ent.get("password"),
                         "nickname": ent.get("nickname")})
        _pending_remove(pending_path, email)
        recovered += 1
        print(f"  [pending] recovered {email}", flush=True)
    return recovered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=0, help="注册 N 个（0=配合 --target 自动算剩余）")
    ap.add_argument("--target", type=int, default=0, help="账号池总目标数；已存在的不重复注册")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--cookies-out", default=str(ROOT / "data" / "cookies.json"))
    ap.add_argument("--accounts-out", default=str(ROOT / "data" / "accounts.json"))
    ap.add_argument("--results-out", default=str(ROOT / "data" / "register_results.json"))
    ap.add_argument("--no-import", action="store_true")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    global PENDING_PATH
    accounts_path = Path(args.accounts_out)
    PENDING_PATH = accounts_path.with_name("pending_signups.json")

    # 单实例：两个注册进程会互相踢掉 CloudMail 会话，导致"邮件到了也读不到"
    inst_lock = acquire_instance_lock(accounts_path.with_name("registrar.lock"))
    if inst_lock is None:
        print("another registrar instance is already running; exiting.", flush=True)
        return 2

    # 计算本轮数量：target 模式下补齐差额
    n = args.count
    if args.target > 0:
        existing = _existing_accounts(Path(args.accounts_out))
        remaining = max(0, args.target - existing)
        n = min(args.batch_size, remaining) if args.count == 0 else min(args.count, remaining)
        print(f"target={args.target} existing={existing} -> registering {n} this round", flush=True)
    if n <= 0:
        print("nothing to register (target already reached)", flush=True)
        return 0

    from cloudmail import CloudMail

    cm = CloudMail()
    # CloudMail 可能被限流（503）：耐心等待恢复，每 60s 探一次，最多 1 小时
    print("waiting for CloudMail to recover ...", flush=True)
    for attempt in range(60):
        try:
            cm.login()
            cm.list_latest(size=5)
            print(f"CloudMail healthy (after {attempt} probe(s))", flush=True)
            break
        except Exception as e:
            print(f"  probe {attempt + 1}/60 failed: {str(e)[:100]}; sleep 60s", flush=True)
            time.sleep(60)
    else:
        print("CloudMail still unhealthy after 1 hour; aborting this round.", flush=True)
        return 3
    pool = BrowserPool(headless=not args.headed)
    pool.start()

    # 先把上次没走完的号（验证码可能已在邮箱里）被动救回来，不新发邮件
    try:
        fixed = resume_pending(cm, pool, accounts_path, Path(args.cookies_out), PENDING_PATH)
        if fixed:
            print(f"resumed {fixed} pending registration(s) from existing mail", flush=True)
    except Exception as e:
        print(f"resume_pending skipped: {str(e)[:120]}", flush=True)

    results = []
    results_path = Path(args.results_out)
    # 断点续存：先载入历史结果
    if results_path.exists():
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
            if not isinstance(results, list):
                results = []
        except Exception:
            results = []

    registered = 0
    consecutive_failures = 0
    interrupted = False
    try:
        for i in range(1, n + 1):
            print(f"=== register {i}/{n} (pure protocol) ===", flush=True)
            try:
                r = register_one(pool, cm, i)
            except KeyboardInterrupt:
                # 只中断当前注册：已完成的号已在 accounts.json 里，不丢失
                print("KeyboardInterrupt: aborting current registration; "
                      "previously registered accounts are kept.", flush=True)
                interrupted = True
                break
            except Exception as e:
                r = {"idx": i, "ok": False, "stage": "exception", "error": str(e)}
            results.append(r)
            # 增量保存：每号落盘一次，崩溃/被杀都不丢已完成账号
            _flush_results(results_path, results)
            if r.get("ok"):
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                print(f"    FAILED stage={r.get('stage')} err={str(r.get('error'))[:160]}", flush=True)
            if r.get("ok") and r.get("cookies") and not args.no_import:
                import_to_proxy(r["cookies"], Path(args.cookies_out),
                                Path(args.accounts_out), r)
                if PENDING_PATH is not None and r.get("email"):
                    _pending_remove(PENDING_PATH, r["email"])
                registered += 1
                print(f"  imported -> {args.cookies_out} (pool now has "
                      f"{_existing_accounts(Path(args.accounts_out))} accounts)", flush=True)
            # 失败簇：连续 3 个失败说明被限流，冷却后继续
            # mail_blocked = 上游验证邮件整体停发（不是本地问题），必须长冷却
            if r.get("stage") == "mail_blocked":
                print("    verification mail flow looks blocked upstream; "
                      "cooldown 1800s before next attempt", flush=True)
                # 不要 break：驱动脚本会在 20s 后重启本脚本，等于没有冷却
                time.sleep(1800)
                consecutive_failures = 0
                continue
            if consecutive_failures >= 3:
                cooldown = 600 if r.get("stage") in ("cloudmail_create", "wait_code") else 180
                print(f"    {consecutive_failures} consecutive failures; cooling down {cooldown}s",
                      flush=True)
                time.sleep(cooldown)
                consecutive_failures = 0
            elif i % 10 == 0:
                time.sleep(45)  # 每 10 个号缓一口气，减轻 CloudMail 与上游压力
            else:
                time.sleep(5)
    except KeyboardInterrupt:
        print("KeyboardInterrupt: stopping batch; completed accounts are kept.", flush=True)
        interrupted = True
    finally:
        pool.close()
        _flush_results(results_path, results)

    ok = sum(1 for r in results if r.get("ok"))
    total = _existing_accounts(Path(args.accounts_out))
    print(f"\nDONE: this_round_ok={ok}/{n}, pool_total={total}, "
          f"interrupted={interrupted}", flush=True)
    if interrupted:
        return 0  # 中断不算失败
    return 0 if ok == n else 1


def _existing_accounts(accounts_path: Path) -> int:
    try:
        data = json.loads(Path(accounts_path).read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0


def _flush_results(path: Path, results: list) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"warn: flush results failed: {e}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
