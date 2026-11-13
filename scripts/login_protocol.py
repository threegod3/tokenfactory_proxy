# -*- coding: utf-8 -*-
"""纯协议登录 TokenFactory：浏览器只负责产出 reCAPTCHA token，其余走 curl+socks5。

流程：
1. 真 Chrome 打开 /login，页面内 grecaptcha.enterprise.execute(siteKey, {action:'login'}) 取 token
2. GET /api/v1/auth/encryption-key 取 RSA 公钥，RSA-OAEP-256 加密密码
3. POST /internal/auth/login  {email, recaptchaToken, keyPairId, encryptedPassword}
4. 导出 Set-Cookie 到 data/cookies.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tokenfactory" / "tools"))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
SOCKS = "127.0.0.1:10808"
BASE = "https://tokenfactory.kt.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
SITE_KEYS = [
    "6Ld6CAEuAAAAABMcfRzFiXfSsiPjCgehCytp1laq",
    "6LeZEBcuAAAAAMQOk0zy4wrzZPzUbBSeHRnD22eM",
]


def curl(method: str, path: str, body=None, headers=None, timeout=60):
    url = BASE + path
    cmd = ["curl.exe", "-k", "--ssl-no-revoke", "--socks5-hostname", SOCKS,
           "-sS", "-X", method.upper(), url,
           "-H", "Accept: application/json, text/plain, */*",
           "-H", f"Origin: {BASE}",
           "-H", f"Referer: {BASE}/login",
           "-H", f"User-Agent: {UA}"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    cmd += ["-D", "-", "-w", "\n__HTTP__:%{http_code}"]
    p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    hdrs_raw, _, rest = out.partition("\r\n\r\n")
    code = None
    body_text = rest
    if "__HTTP__:" in rest:
        body_text, _, cs = rest.rpartition("__HTTP__:")
        try:
            code = int(cs.strip())
        except Exception:
            code = None
    set_cookies = []
    for line in hdrs_raw.splitlines():
        if line.lower().startswith("set-cookie:"):
            sc = SimpleCookie()
            try:
                sc.load(line.split(":", 1)[1].strip())
                for k, morsel in sc.items():
                    set_cookies.append((k, morsel.value))
            except Exception:
                pass
    j = None
    try:
        j = json.loads(body_text)
    except Exception:
        pass
    return {"status": code, "text": body_text, "json": j, "cookies": set_cookies, "stderr": (p.stderr or b"").decode("utf-8", "replace")}


def get_recaptcha_token(action="login") -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True,
                                    args=["--disable-blink-features=AutomationControlled",
                                          "--no-first-run", "--no-default-browser-check", "--disable-gpu"])
        ctx = browser.new_context(locale="ko-KR", user_agent=UA)
        page = ctx.new_page()
        page.goto(BASE + "/login", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_function(
            "() => window.grecaptcha && window.grecaptcha.enterprise && typeof window.grecaptcha.enterprise.execute === 'function'",
            timeout=60000,
        )
        last = None
        for sk in SITE_KEYS:
            try:
                tok = page.evaluate(
                    """async (a) => {
                        const {sk, action} = a;
                        await new Promise(r => grecaptcha.enterprise.ready(r));
                        return await grecaptcha.enterprise.execute(sk, {action});
                    }""",
                    {"sk": sk, "action": action},
                )
                if tok and isinstance(tok, str) and len(tok) > 30:
                    browser.close()
                    return {"ok": True, "token": tok, "siteKey": sk}
            except Exception as e:
                last = str(e)
        browser.close()
        return {"ok": False, "error": last}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out", default="data/cookies.json")
    ap.add_argument("--tenant", default="")
    ap.add_argument("--workspace", default="")
    ap.add_argument("--retries", type=int, default=5, help="max login attempts with fresh recaptcha tokens")
    ap.add_argument("--retry-wait", type=int, default=20, help="seconds between retries")
    args = ap.parse_args()

    print("[1/4] reCAPTCHA token ...", flush=True)
    rc = get_recaptcha_token("login")
    print("   ", json.dumps({k: (v[:24] + "..." if k == "token" else v) for k, v in rc.items()}, ensure_ascii=False), flush=True)
    if not rc.get("ok"):
        return 1

    print("[2/4] encryption-key ...", flush=True)
    ek = curl("GET", "/api/v1/auth/encryption-key")
    if ek["status"] != 200 or not ek["json"]:
        print("   FAIL", ek["status"], ek["text"][:300], flush=True)
        return 1
    key_pair_id = ek["json"]["keyPairId"]
    pub_jwk = ek["json"]["publicKey"]

    # RSA-OAEP-256
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    def b64u(s: str) -> bytes:
        s = s.replace("-", "+").replace("_", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4))

    n = int.from_bytes(b64u(pub_jwk["n"]), "big")
    e = int.from_bytes(b64u(pub_jwk["e"]), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    enc = pub.encrypt(args.password.encode(), padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    encrypted_password = base64.b64encode(enc).decode()
    print("    keyPairId =", key_pair_id, flush=True)

    print("[3/4] POST /internal/auth/login ...", flush=True)
    body = {
        "email": args.email,
        "recaptchaToken": rc["token"],
        "keyPairId": key_pair_id,
        "encryptedPassword": encrypted_password,
    }
    r = curl("POST", "/internal/auth/login", body=body)
    print("    status:", r["status"], flush=True)
    print("    body:", (r["text"] or "")[:400], flush=True)

    # CMN-111 = recaptcha score too low; retry with fresh tokens from a fresh page context
    attempt = 1
    while (r["status"] != 200 or not r["cookies"]) and attempt < args.retries:
        err = ""
        try:
            err = (r.get("json") or {}).get("errorCode", "")
        except Exception:
            pass
        print(f"    retry {attempt}/{args.retries - 1} (reason={err or r['status']}) ...", flush=True)
        time.sleep(args.retry_wait)
        rc2 = get_recaptcha_token("login")
        if not rc2.get("ok"):
            print("    recaptcha fetch failed, aborting retries", flush=True)
            break
        body["recaptchaToken"] = rc2["token"]
        r = curl("POST", "/internal/auth/login", body=body)
        print("    status:", r["status"], "body:", (r["text"] or "")[:300], flush=True)
        attempt += 1

    print("    cookies:", [c[0] for c in r["cookies"]], flush=True)

    if r["status"] != 200 or not r["cookies"]:
        print(json.dumps({"ok": False, "status": r["status"], "body": r["json"] or r["text"][:600]},
                         ensure_ascii=False, indent=2), flush=True)
        return 1

    print("[4/4] saving cookies ...", flush=True)
    cookies = {k: v for k, v in r["cookies"]}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cookies": [{"name": k, "value": v, "domain": "tokenfactory.kt.com", "path": "/"} for k, v in cookies.items()],
        "email": args.email,
        "saved_at": int(time.time()),
        "tenant_id": args.tenant,
        "workspace_id": args.workspace,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "cookie_names": sorted(cookies.keys()), "out": str(out)},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
