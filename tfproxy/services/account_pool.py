from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..config import AccountConfig, AppConfig
from ..upstream.auth import login_sync
from ..upstream.http import UpstreamSession

logger = logging.getLogger(__name__)


@contextmanager
def accounts_file_lock(path: Path, timeout: float = 15.0) -> Iterator[bool]:
    """跨进程互斥写锁（与 register_and_import.py 使用同一 *.lock 文件）。"""
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
                    logger.warning("accounts file lock timeout: %s", lock_path)
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


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class AccountState:
    email: str
    ok: bool = False
    last_login_at: float = 0.0
    last_error: str = ""
    cooldown_until: float = 0.0
    cookies: Dict[str, str] = field(default_factory=dict)
    tenant_id: str = ""
    workspace_id: str = ""


class AccountPool:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.accounts: List[AccountConfig] = [a for a in cfg.accounts if a.enabled]
        self.states: Dict[str, AccountState] = {a.email: AccountState(email=a.email) for a in self.accounts}
        self._idx = 0
        self._lock = asyncio.Lock()
        self._login_locks: Dict[str, asyncio.Lock] = {}
        self._warming: set = set()
        # 浏览器并发闸门：最多同时 N 个浏览器实例做续期登录
        self.browser_sem = asyncio.Semaphore(max(1, cfg.maintenance.max_browser_concurrency))
        self._load_state()
        self._load_registered_accounts()

    def _accounts_file(self) -> Optional[Path]:
        return self.cfg.resolve_path(self.cfg.storage.accounts_file)

    def _load_registered_accounts(self) -> None:
        """导入 register_and_import.py 产出的账号（含密码与 cookie）。可重复调用，只加新号。"""
        p = self._accounts_file()
        if not p or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("failed to read accounts file: %s", e)
            return
        if not isinstance(data, list):
            return
        known = {a.email for a in self.accounts}
        added = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            email = item.get("email")
            if not email or email in known:
                continue
            self.accounts.append(AccountConfig(
                email=email,
                password=item.get("password") or "",
                enabled=True,
                tenant_id=item.get("tenant_id") or "",
                workspace_id=item.get("workspace_id") or "",
            ))
            known.add(email)
            added += 1
            st = self.states.setdefault(email, AccountState(email=email))
            cookies = item.get("cookies")
            if isinstance(cookies, dict) and cookies:
                st.cookies = {k: str(v) for k, v in cookies.items()}
                st.ok = True
        if self.accounts and added:
            logger.info("account pool loaded %d new account(s) from %s (total %d)",
                        added, p.name, len(self.accounts))

    def reload_accounts(self) -> int:
        """热重载 accounts.json（注册进程持续产出新号，代理需要感知）。返回当前总数。"""
        self._load_registered_accounts()
        return len(self.accounts)

    def _state_path(self) -> Optional[Path]:
        return self.cfg.resolve_path(self.cfg.storage.state_file)

    def _load_state(self) -> None:
        p = self._state_path()
        if not p or not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for email, st in (data.get("accounts") or {}).items():
                if email in self.states and isinstance(st, dict):
                    cur = self.states[email]
                    cur.ok = bool(st.get("ok", cur.ok))
                    cur.last_login_at = float(st.get("last_login_at", 0))
                    cur.last_error = str(st.get("last_error", ""))
                    cur.cooldown_until = float(st.get("cooldown_until", 0))
                    if isinstance(st.get("cookies"), dict):
                        cur.cookies = {k: str(v) for k, v in st["cookies"].items()}
                    cur.tenant_id = str(st.get("tenant_id", ""))
                    cur.workspace_id = str(st.get("workspace_id", ""))
        except Exception as e:
            logger.warning("failed to load account pool state: %s", e)

    def pick_account(self) -> Optional[AccountConfig]:
        if not self.accounts:
            return None
        now = time.time()
        n = len(self.accounts)
        for _ in range(n):
            acc = self.accounts[self._idx % n]
            self._idx = (self._idx + 1) % n
            st = self.states.get(acc.email)
            if st and st.cooldown_until > now:
                continue
            return acc
        return self.accounts[0]

    async def ensure_session(self, session: UpstreamSession) -> bool:
        """确保活动会话真的可用。

        关键约束：ACCESS_TOKEN 寿命约 600s，而且 /internal/auth/refresh 只在
        access token 尚未过期时可用。因此缓存 cookie 必须校验 ACCESS_TOKEN，
        不能只看 REFRESH_TOKEN（14 天，过期前一直"看起来"可用）——否则会把几小时前
        的旧会话当有效返回，上游 chat/turns 直接 401。
        """
        # 1) access token 仍有效 -> 直接可用
        if session.access_token_valid():
            return True
        # 2) 纯协议续期（不需要浏览器）
        if await session.refresh_access_token():
            self.sync_active_cookies(session)
            return True
        if not self.accounts:
            logger.warning("session expired and no accounts configured for re-login")
            return False
        async with self._lock:
            if session.access_token_valid():
                return True
            if await session.refresh_access_token():
                self.sync_active_cookies(session)
                return True
            email = getattr(session, "email", None) or ""
            # 3) 账号池里仍有未过期 access token 的号（10 分钟内登录过）→ 瞬时切换
            hot = [a.email for a in self.accounts
                   if a.email != email
                   and self.states.get(a.email)
                   and self._cookies_usable(self.states[a.email].cookies)]
            for candidate in ([email] if email else []) + hot:
                st = self.states.get(candidate)
                if st and st.cookies and self._cookies_usable(st.cookies):
                    self._apply_cookies(session, candidate, st)
                    if session.access_token_valid():
                        logger.info("reused fresh access token for %s", candidate)
                        return True
            # 4) 需要浏览器完整登录：优先当前账号（保持会话连续），其次其它账号
            order = ([email] if email in self.states else []) + [
                a.email for a in self.accounts if a.email != email]
            for ignore_cooldown in (False, True):
                for candidate in order:
                    st = self.states.get(candidate)
                    if st and not ignore_cooldown and st.cooldown_until > time.time():
                        continue
                    logger.info("session needs browser login: %s", candidate)
                    if await self.relogin_account(candidate, session) is True:
                        self._apply_cookies(session, candidate, self.states[candidate])
                        return True
            logger.error("all accounts failed to produce a usable session")
            return False

    def mark_bad(self, email: Optional[str] = None, error: str = "") -> None:
        if email and email in self.states:
            st = self.states[email]
            st.ok = False
            st.last_error = error
            st.cooldown_until = time.time() + 120
            self._save_state()

    async def switch_session(self, session: UpstreamSession, exclude: Optional[set] = None,
                             quota_remaining=None) -> bool:
        """把活动会话切到下一个有配额的账号。

        1) 热号：cookie 里 ACCESS_TOKEN 仍有效（10 分钟内登录过）→ 纯协议瞬时切换；
        2) 冷号：浏览器完整登录（browser_sem 限制 2 并发），按剩余配额降序最多试 2 个。
        """
        exclude = set(exclude or ())
        if quota_remaining is None:
            quota_remaining = lambda e: 1  # noqa: E731
        email = getattr(session, "email", None)
        if email:
            exclude.add(email)
        cur_token = (session.cookies or {}).get("ACCESS_TOKEN")
        # 1) 热号纯协议切换
        for acc in self.accounts:
            if acc.email in exclude or quota_remaining(acc.email) <= 0:
                continue
            st = self.states.get(acc.email)
            if st and st.cookies and self._cookies_usable(st.cookies):
                if cur_token and st.cookies.get("ACCESS_TOKEN") == cur_token:
                    continue
                self._apply_cookies(session, acc.email, st)
                if session.access_token_valid():
                    logger.info("switched session to %s (hot cookies)", acc.email)
                    return True
        # 2) 冷号浏览器登录
        candidates = [a for a in self.accounts
                      if a.email not in exclude and quota_remaining(a.email) > 0]
        candidates.sort(key=lambda a: quota_remaining(a.email), reverse=True)
        for acc in candidates[:3]:
            if await self.relogin_account(acc.email, session) is True:
                self._apply_cookies(session, acc.email, self.states[acc.email])
                logger.info("switched session to %s (browser login)", acc.email)
                return True
        return False

    async def warm_next(self, quota_remaining=None, exclude: Optional[set] = None,
                        count: int = 1) -> List[str]:
        """后台预热：提前用浏览器登录下一批账号，让换号变成瞬时切换。

        冷号用完后 access token 只有 10 分钟寿命，因此只在"快要用完"时预热，
        避免无谓的浏览器登录与配额浪费。
        """
        if quota_remaining is None:
            quota_remaining = lambda e: 1  # noqa: E731
        exclude = set(exclude or ())
        picked: List[str] = []
        for acc in self.accounts:
            if len(picked) >= count:
                break
            if acc.email in exclude or acc.email in self._warming:
                continue
            if quota_remaining(acc.email) <= 0:
                continue
            st = self.states.get(acc.email)
            if st and st.cookies and self._cookies_usable(st.cookies):
                continue
            picked.append(acc.email)
        for email in picked:
            self._warming.add(email)
            asyncio.create_task(self._warm_one(email))
        return picked

    async def _warm_one(self, email: str) -> None:
        try:
            ok = await self.relogin_account(email, None)
            logger.info("warm-up %s: %s", email, "ready" if ok is True else str(ok)[:200])
        finally:
            self._warming.discard(email)

    def status(self) -> List[Dict[str, Any]]:
        out = []
        for a in self.accounts:
            st = self.states.get(a.email)
            left = self._refresh_left(st.cookies if st else None)
            out.append({
                "email": a.email,
                "enabled": a.enabled,
                "ok": bool(st and st.ok),
                "has_cookies": bool(st and st.cookies),
                "refresh_expires_in_seconds": round(left) if left is not None else None,
                "refresh_expires_in_days": round(left / 86400, 2) if left is not None else None,
                "due_for_renewal": left is None or left <= self.cfg.maintenance.renew_refresh_days * 86400,
                "last_login_at": st.last_login_at if st else 0,
                "last_error": (st.last_error if st else "")[:300],
                "cooldown_until": st.cooldown_until if st else 0,
            })
        return out

    # ---------- REFRESH_TOKEN 到期管理（每号独立计算，浏览器并发受限） ----------

    @staticmethod
    def _refresh_left(cookies: Optional[Dict[str, str]]) -> Optional[float]:
        """从 cookie 集里解析 REFRESH_TOKEN 剩余秒数；缺失返回 None。"""
        raw = (cookies or {}).get("REFRESH_TOKEN_EXPIRES_AT")
        if not raw:
            return None
        try:
            v = float(raw)
        except Exception:
            return None
        exp = v / 1000.0 if v > 1e11 else v
        return exp - time.time()

    @staticmethod
    def _access_left(cookies: Optional[Dict[str, str]]) -> Optional[float]:
        """从 cookie 集里解析 ACCESS_TOKEN 剩余秒数；缺失返回 None。"""
        raw = (cookies or {}).get("ACCESS_TOKEN_EXPIRES_AT")
        if not raw:
            return None
        try:
            v = float(raw)
        except Exception:
            return None
        exp = v / 1000.0 if v > 1e11 else v
        return exp - time.time()

    def _cookies_usable(self, cookies: Optional[Dict[str, str]], leeway: float = 90.0) -> bool:
        """缓存 cookie 只有在 ACCESS_TOKEN 仍有效时才可复用。"""
        left = self._access_left(cookies)
        return left is not None and left > leeway

    def _apply_cookies(self, session: UpstreamSession, email: str, st: AccountState) -> None:
        """把账号 cookie 灌进活动会话；tenant/workspace 必须重建，不能沿用别的号。"""
        session.tenant_id = st.tenant_id or ""
        session.workspace_id = st.workspace_id or ""
        session.set_cookies(dict(st.cookies))
        session.email = email
        if not st.tenant_id and session.tenant_id:
            st.tenant_id = session.tenant_id
        if not st.workspace_id and session.workspace_id:
            st.workspace_id = session.workspace_id

    def _save_accounts_file(self) -> None:
        """把每个账号的最新 cookie 合并写回 accounts.json。

        必须与注册进程互斥并原子替换：注册进程持续追加新号，若这里用
        "读-改-写" 直接覆盖，会把同时新增的账号丢掉。
        """
        p = self._accounts_file()
        if not p or not p.exists():
            return
        with accounts_file_lock(p) as locked:
            if not locked:
                logger.warning("skip accounts.json cookie sync (lock busy)")
                return
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("skip accounts.json cookie sync (read failed: %s)", e)
                return
            if not isinstance(data, list):
                return
            try:
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    st = self.states.get(item.get("email"))
                    if st and st.cookies:
                        item["cookies"] = dict(st.cookies)
                        item["refreshed_at"] = time.time()
                atomic_write_json(p, data)
            except Exception as e:
                logger.warning("failed to save accounts file: %s", e)

    def _save_state(self) -> None:
        p = self._state_path()
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(p, {
                "accounts": {k: asdict(v) for k, v in self.states.items()},
                "saved_at": time.time(),
            })
        except Exception as e:
            logger.warning("failed to save account pool state: %s", e)

    def sync_active_cookies(self, session: UpstreamSession) -> None:
        """纯协议 refresh 会轮换 REFRESH_TOKEN，把活动会话的最新 cookie 同步回账号池。"""
        email = getattr(session, "email", None)
        if not email or email not in self.states:
            return
        st = self.states[email]
        if session.cookies and session.cookies != st.cookies:
            st.cookies = dict(session.cookies)
            st.ok = True
            self._save_accounts_file()
            self._save_state()

    async def relogin_account(self, email: str, session: Optional[UpstreamSession] = None) -> Any:
        """用浏览器取 reCAPTCHA token 并完整重登该账号（受 browser_sem 并发限制）。

        同一账号的登录请求串行化，避免并发重复登录互相踢掉会话。
        """
        acc = next((a for a in self.accounts if a.email == email), None)
        if not acc or not acc.password:
            return "no credentials"
        lock = self._login_locks.setdefault(email, asyncio.Lock())
        async with lock:
            # 等待期间别的任务可能已经登录成功，直接复用
            cur = self.states.get(email)
            if cur and cur.cookies and self._cookies_usable(cur.cookies):
                if session is not None:
                    self._apply_cookies(session, email, cur)
                return True
            async with self.browser_sem:
                active = self.browser_sem._value  # type: ignore[attr-defined]
                total = self.cfg.maintenance.max_browser_concurrency
                logger.info("browser renewal start: %s (browser slots %d/%d used)",
                            email, total - active, total)
                proxies = None
                if self.cfg.upstream.proxy:
                    proxies = {"http": self.cfg.upstream.proxy, "https": self.cfg.upstream.proxy}
                last_err = "unknown"
                for attempt in range(3):
                    try:
                        res = await asyncio.to_thread(
                            login_sync, self.cfg.upstream.base_url, email, acc.password,
                            None, self.cfg.auth.recaptcha_mode, proxies, 90,
                        )
                    except Exception as e:
                        last_err = str(e)
                        res = None
                    if res is not None and res.ok and res.cookies:
                        st = self.states.setdefault(email, AccountState(email=email))
                        st.ok = True
                        st.cookies = dict(res.cookies)
                        st.last_login_at = time.time()
                        st.last_error = ""
                        st.cooldown_until = 0
                        st.tenant_id = acc.tenant_id or st.tenant_id
                        st.workspace_id = acc.workspace_id or st.workspace_id
                        self._save_state()
                        self._save_accounts_file()
                        if session is not None:
                            self._apply_cookies(session, email, st)
                        logger.info("browser renewal ok: %s (access %.0fs, refresh ~%.1f days)",
                                    email, self._access_left(st.cookies) or 0,
                                    (self._refresh_left(st.cookies) or 0) / 86400)
                        return True
                    if res is not None:
                        last_err = res.error or f"status={res.status_code}"
                    await asyncio.sleep(10)
                self.mark_bad(email, last_err)
                logger.warning("browser renewal failed for %s: %s", email, last_err)
                return last_err

    async def maintenance(self, session: Optional[UpstreamSession] = None) -> Dict[str, Any]:
        """巡检所有账号的 REFRESH_TOKEN 剩余寿命，临期的排队用浏览器续期。

        - 每号独立计算剩余时间
        - 最先到期的先续
        - browser_sem 限制并发浏览器数（默认 2）
        - 每轮最多 max_renewals_per_cycle 个，避免一次性全起
        """
        mcfg = self.cfg.maintenance
        if not mcfg.enabled:
            return {"skipped": True}
        threshold = mcfg.renew_refresh_days * 86400
        due: List[tuple] = []
        for acc in self.accounts:
            st = self.states.get(acc.email)
            left = self._refresh_left(st.cookies if st else None)
            if left is None or left <= threshold:
                due.append((left if left is not None else -1.0, acc.email))
        if not due:
            return {}
        due.sort(key=lambda x: x[0])
        due = due[: mcfg.max_renewals_per_cycle]
        logger.info("maintenance: %d account(s) due for browser renewal: %s",
                    len(due), [e for _, e in due])
        tasks = []
        for i, (_, email) in enumerate(due):
            if i:
                await asyncio.sleep(2)  # 错峰启动
            tasks.append(asyncio.create_task(self.relogin_account(email, session)))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        summary: Dict[str, Any] = {}
        for (_, email), r in zip(due, results):
            if r is True:
                summary[email] = "ok"
            elif isinstance(r, Exception):
                summary[email] = f"error: {r}"
            else:
                summary[email] = str(r)[:200]
        return summary
