import asyncio
import base64
import json
import random
import time
import traceback
from pathlib import Path
from typing import Optional

import aiohttp

try:
    import aiomysql
except ImportError:
    aiomysql = None

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig


def _xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def obfuscate(text: str, key: str) -> str:
    """本地混淆存储（非加密，防明文浏览）"""
    try:
        return base64.b64encode(_xor_bytes(text.encode(), key.encode())).decode()
    except Exception:
        return ""


def deobfuscate(token: str, key: str) -> str:
    try:
        return _xor_bytes(base64.b64decode(token), key.encode()).decode()
    except Exception:
        return ""


# ============================================================
# 本地存储：QQ <-> NewAPI 账号绑定关系（JSON 文件）
# ============================================================
class Store:
    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = {"bindings": {}}  # qq -> {user_id, username, bound_at, last_checkin}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                if "bindings" not in self.data:
                    self.data = {"bindings": {}}
        except Exception as e:
            logger.error(f"[newapi] 读取绑定数据失败: {e}")
            self.data = {"bindings": {}}

    def _save_sync(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def get(self, qq: str) -> Optional[dict]:
        async with self.lock:
            return self.data["bindings"].get(str(qq))

    async def set(self, qq: str, rec: dict):
        async with self.lock:
            self.data["bindings"][str(qq)] = rec
            self._save_sync()

    async def remove(self, qq: str) -> bool:
        async with self.lock:
            qq = str(qq)
            if qq in self.data["bindings"]:
                del self.data["bindings"][qq]
                self._save_sync()
                return True
            return False

    async def find_by_user_id(self, user_id) -> Optional[str]:
        async with self.lock:
            uid = str(user_id)
            for qq, rec in self.data["bindings"].items():
                if str(rec.get("user_id")) == uid:
                    return qq
            return None


class MySQLQuota:
    """直连站点 MySQL 进行真实扣款/入账（新版 new-api 无管理员改额度接口，仅此途径）"""

    def __init__(self, cfg_getter):
        self._cfg = cfg_getter
        self.pool = None
        self._algo = None  # 缓存的站点密码哈希算法

    def configured(self) -> bool:
        return all(str(self._cfg(k, "") or "").strip()
                   for k in ("db.host", "db.user", "db.name"))

    async def connect(self) -> bool:
        if self.pool is not None:
            return True
        if not self.configured():
            return False
        if aiomysql is None:
            logger.error("[newapi] 缺少 aiomysql 依赖，请重装插件以安装")
            return False
        try:
            self.pool = await aiomysql.create_pool(
                host=str(self._cfg("db.host")),
                port=int(self._cfg("db.port", 3306) or 3306),
                user=str(self._cfg("db.user")),
                password=str(self._cfg("db.pass", "") or ""),
                db=str(self._cfg("db.name")),
                autocommit=True, minsize=1, maxsize=5,
            )
            logger.info("[newapi] 站点数据库连接成功（红包功能可用）")
            return True
        except Exception as e:
            logger.error(f"[newapi] 站点数据库连接失败: {e}")
            return False

    async def query_all(self, sql: str, args=None):
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(sql, args)
                    return await cur.fetchall()
        except Exception as e:
            logger.error(f"[newapi] 数据库查询失败: {e}")
            return None

    async def query_one(self, sql: str, args=None):
        rows = await self.query_all(sql, args)
        return rows[0] if rows else None

    async def execute(self, sql: str, args=None) -> bool:
        if self.pool is None:
            return False
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, args)
                    return True
        except Exception as e:
            logger.error(f"[newapi] 数据库写入失败: {e}")
            return False

    # ---- 用户相关 ----
    async def get_user(self, user_id):
        return await self.query_one(
            "SELECT id, username, display_name, role, status, quota, used_quota, "
            "request_count, `group` FROM users WHERE id=%s AND deleted_at IS NULL",
            (user_id,),
        )

    async def get_user_by_username(self, username):
        return await self.query_one(
            "SELECT id, username, password, role, status, quota, `group` FROM users "
            "WHERE username=%s AND deleted_at IS NULL",
            (username,),
        )

    async def verify_user_password(self, username_or_id: str, password: str):
        """数据库模式下的登录验证（站点密码为标准 bcrypt，兼容常见变体）
        返回 (user, None) 或 (None, 错误类型)：
        user_not_found / no_password / bad_hash_format / wrong_password / error
        支持直接填用户 ID 数字"""
        user = await self.query_one(
            "SELECT id, username, password, role, status, quota, `group` FROM users "
            "WHERE username=%s AND deleted_at IS NULL LIMIT 1",
            (username_or_id,),
        )
        if user is None and str(username_or_id).isdigit():
            user = await self.query_one(
                "SELECT id, username, password, role, status, quota, `group` FROM users "
                "WHERE id=%s AND deleted_at IS NULL LIMIT 1",
                (int(username_or_id),),
            )
        if user is None:
            if self._cfg("debug_mode", False):
                logger.warning(f"[newapi][DEBUG] 登录验证：用户不存在 -> {username_or_id!r}")
            return None, "user_not_found"

        raw_hash = str(user["password"] or "").strip()
        if self._cfg("debug_mode", False):
            logger.info(
                f"[newapi][DEBUG] 登录验证: user={user['username']} "
                f"哈希前缀={raw_hash[:7]!r} 哈希长度={len(raw_hash)}"
            )
        # OAuth（GitHub / LinuxDo / 微信等第三方登录）注册的账号，password 字段为空
        if not raw_hash:
            return None, "no_password"

        import bcrypt
        import hashlib
        pwd = password.encode()
        # $2y$ 与 $2b$ 语义相同（bcrypt 同源），归一化以兼容 PHP 生成的哈希
        if raw_hash.startswith("$2y$"):
            raw_hash = "$2b$" + raw_hash[4:]
        # 标准 bcrypt：$2a$/$2b$/$2x$ 开头且长度恰为 60
        if raw_hash.startswith("$2") and len(raw_hash) == 60:
            try:
                ok = bcrypt.checkpw(pwd, raw_hash.encode())
            except (ValueError, TypeError) as e:
                logger.error(f"[newapi] bcrypt 校验异常: {e}（哈希前缀={raw_hash[:7]!r}）")
                return None, "bad_hash_format"
            return (user, None) if ok else (None, "wrong_password")

        # Argon2（$argon2id/$argon2i/$argon2d，新一代站点分支常用）：本地直接校验
        if raw_hash.startswith("$argon2"):
            try:
                from argon2 import PasswordHasher
                from argon2.exceptions import VerifyMismatchError
            except ImportError:
                pass  # 未装 argon2 库，回退到站点登录接口兜底
            else:
                try:
                    PasswordHasher().verify(raw_hash, password)
                    return user, None
                except VerifyMismatchError:
                    return None, "wrong_password"
                except Exception as e:
                    logger.error(f"[newapi] argon2 校验异常: {e}（哈希前缀={raw_hash[:7]!r}）")
                    return None, "bad_hash_format"

        # 非 bcrypt：站点为魔改分支时可能用单向哈希存密码，做精确比对兜底
        digest_pool = {
            hashlib.sha256(pwd).hexdigest(),
            hashlib.sha256(pwd).hexdigest().upper(),
            hashlib.sha256(hashlib.sha256(pwd).hexdigest().encode()).hexdigest(),
            hashlib.sha256(hashlib.sha256(pwd).hexdigest().encode()).hexdigest().upper(),
            hashlib.sha1(pwd).hexdigest(),
            hashlib.sha1(pwd).hexdigest().upper(),
            hashlib.md5(pwd).hexdigest(),
            hashlib.md5(pwd).hexdigest().upper(),
        }
        if raw_hash in digest_pool:
            return user, None

        logger.error(
            f"[newapi] 无法识别的密码哈希格式（前缀={raw_hash[:7]!r}，长度={len(raw_hash)}），"
            f"该站点可能不是标准 new-api，或该账号密码字段异常"
        )
        return None, "bad_hash_format"

    async def create_user(self, username: str, password: str, group: str):
        """数据库模式注册：按站点密码哈希算法加密 + 插入 users 表，返回新用户 id"""
        hashed = await self.hash_password(password)
        if not hashed:
            return None
        sql = ("INSERT INTO users (username, password, display_name, role, status, "
               "quota, `group`, created_at) VALUES (%s, %s, %s, 1, 1, 0, %s, %s)")
        if self.pool is None:
            return None
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, (username, hashed, username, group, int(time.time())))
                    return cur.lastrowid
        except Exception as e:
            logger.error(f"[newapi] 数据库注册失败: {e}")
            return None

    async def reset_password(self, user_id: int, password: str) -> bool:
        hashed = await self.hash_password(password)
        if not hashed:
            return False
        return await self.execute(
            "UPDATE users SET password=%s WHERE id=%s AND deleted_at IS NULL",
            (hashed, user_id),
        )

    async def _detect_algo(self) -> str:
        """探测站点密码哈希算法（查一个已有用户的 password 前缀），结果缓存"""
        if self._algo:
            return self._algo
        self._algo = "bcrypt"  # 默认标准 new-api
        row = await self.query_one(
            "SELECT password FROM users WHERE password IS NOT NULL AND password <> '' "
            "AND deleted_at IS NULL ORDER BY id LIMIT 1"
        )
        if row and row.get("password"):
            h = str(row["password"])
            if h.startswith("$argon2"):
                self._algo = "argon2"
            elif h.startswith("$2") and len(h) == 60:
                self._algo = "bcrypt"
            elif len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h):
                self._algo = "sha256"
            elif len(h) == 32 and all(c in "0123456789abcdefABCDEF" for c in h):
                self._algo = "md5"
        return self._algo

    async def hash_password(self, password: str) -> str:
        """按站点密码哈希算法生成哈希（保证注册/改密后站点能正常登录）"""
        algo = await self._detect_algo()
        try:
            if algo == "argon2":
                from argon2 import PasswordHasher
                return PasswordHasher().hash(password)
            if algo == "sha256":
                import hashlib
                return hashlib.sha256(password.encode()).hexdigest()
            if algo == "md5":
                import hashlib
                return hashlib.md5(password.encode()).hexdigest()
            import bcrypt
            return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        except Exception as e:
            logger.error(f"[newapi] 密码哈希生成失败（{algo}）: {e}")
            return None

    async def search_users(self, keyword: str, limit: int = 10):
        sql = ("SELECT id, username, quota, `group` FROM users WHERE deleted_at IS NULL AND "
               "(username LIKE %s OR display_name LIKE %s")
        args = [f"%{keyword}%", f"%{keyword}%"]
        if str(keyword).isdigit():
            sql += " OR id = %s"
            args.append(int(keyword))
        sql += ") ORDER BY id LIMIT %s"
        args.append(limit)
        return await self.query_all(sql, args) or []

    async def set_group(self, user_id, group: str) -> bool:
        return await self.execute(
            "UPDATE users SET `group`=%s WHERE id=%s AND deleted_at IS NULL",
            (group, user_id),
        )

    async def soft_delete_user(self, user_id) -> bool:
        """软删除用户（与站点官方删除行为一致），并同步软删其令牌"""
        ok = await self.execute(
            "UPDATE users SET deleted_at=NOW(3) WHERE id=%s AND deleted_at IS NULL",
            (user_id,),
        )
        await self.execute(
            "UPDATE tokens SET deleted_at=NOW(3) WHERE user_id=%s AND deleted_at IS NULL",
            (user_id,),
        )
        return ok

    async def db_checkin(self, user_id, amount: int, date_str: str, ts: int):
        """数据库模式签到：判重 -> 加额度 -> 写 checkins 记录（站点日历可见）"""
        existing = await self.query_one(
            "SELECT quota_awarded FROM checkins WHERE user_id=%s AND checkin_date=%s",
            (user_id, date_str),
        )
        if existing is not None:
            return "already", existing.get("quota_awarded")
        if not await self.execute(
            "UPDATE users SET quota=quota+%s WHERE id=%s AND deleted_at IS NULL",
            (amount, user_id),
        ):
            return "fail", None
        if not await self.execute(
            "INSERT INTO checkins (user_id, checkin_date, quota_awarded, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, date_str, amount, ts),
        ):
            # 记录写入失败，回滚加额，避免两边账目不一致
            await self.execute(
                "UPDATE users SET quota=quota-%s WHERE id=%s", (amount, user_id)
            )
            return "fail", None
        return "ok", amount

    async def adjust(self, user_id: int, delta: int, require_balance: bool = False) -> bool:
        """真实修改 users.quota；require_balance=True 时余额不足则失败（原子操作）"""
        if self.pool is None or delta == 0:
            return False
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    if require_balance and delta < 0:
                        await cur.execute(
                            "UPDATE users SET quota = quota - %s WHERE id = %s AND quota >= %s",
                            (-delta, user_id, -delta),
                        )
                    else:
                        await cur.execute(
                            "UPDATE users SET quota = quota + %s WHERE id = %s",
                            (delta, user_id),
                        )
                    return cur.rowcount == 1
        except Exception as e:
            logger.error(f"[newapi] 数据库额度操作失败: {e}")
            return False


class HongbaoStore:
    """红包持久化（JSON）"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = {"packets": {}}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"[newapi] 读取红包数据失败: {e}")
        self.data.setdefault("packets", {})

    def _save_sync(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def add(self, pid: str, packet: dict):
        async with self.lock:
            self.data["packets"][pid] = packet
            self._save_sync()

    async def get(self, pid: str) -> Optional[dict]:
        async with self.lock:
            return self.data["packets"].get(pid)

    async def update(self, pid: str, packet: dict):
        async with self.lock:
            self.data["packets"][pid] = packet
            self._save_sync()

    async def remove(self, pid: str):
        async with self.lock:
            self.data["packets"].pop(pid, None)
            self._save_sync()

    async def all(self) -> dict:
        async with self.lock:
            return dict(self.data["packets"])


def split_red_packet(total: int, n: int) -> list:
    """二倍均值法拼手气拆分（与微信/QQ一致），每个包至少 1 raw quota"""
    shares = []
    remain, rest = total, n
    while rest > 1:
        cap = max(1, remain - (rest - 1))
        avg = remain / rest
        s = random.randint(1, min(cap, max(1, int(avg * 2))))
        shares.append(s)
        remain -= s
        rest -= 1
    shares.append(remain)
    random.shuffle(shares)
    return shares


def _extract_user_list(data) -> list:
    """从 /api/user/search 等响应中稳健地提取用户列表（兼容各种版本/限流响应形态）"""
    if not isinstance(data, dict):
        return []
    d = data.get("data")
    if isinstance(d, list):
        return [u for u in d if isinstance(u, dict)]
    if isinstance(d, dict):
        # 部分版本/分支返回分页对象 {"items": [...]} 等
        for key in ("items", "records", "list", "users", "data"):
            v = d.get(key)
            if isinstance(v, list):
                return [u for u in v if isinstance(u, dict)]
        if "id" in d:  # 单个用户对象
            return [d]
    # data 是字符串（通常是限流/错误提示）→ 视为空结果
    return []


def _extract_at_qqs(event) -> list:
    """提取被 @ 的 QQ 号列表（结构化 At 消息段 > OneBot CQ 码 > '@昵称(QQ号)' 文本），去重保序"""
    import re
    qqs = []
    # 1) 结构化消息段（AstrBot 标准 API）
    try:
        from astrbot.api.message_components import At
        for seg in event.get_messages():
            if isinstance(seg, At):
                q = getattr(seg, "qq", None)
                if q:
                    qqs.append(str(q))
    except Exception:
        pass
    # 2) OneBot 原始事件（CQ 码 array / string）
    raw = getattr(getattr(event, "message_obj", None), "raw_event", None)
    if isinstance(raw, dict):
        msg = raw.get("message")
        if isinstance(msg, list):
            for seg in msg:
                if isinstance(seg, dict) and seg.get("type") == "at":
                    q = (seg.get("data") or {}).get("qq")
                    if q:
                        qqs.append(str(q))
        elif isinstance(msg, str):
            qqs += re.findall(r"\[CQ:at,qq=(\d+)\]", msg)
    # 3) 文本兜底：AstrBot 可能把 @ 渲染成 '@昵称(QQ号)' 或 '@QQ号'
    text = getattr(event, "message_str", "") or ""
    qqs += re.findall(r"\[CQ:at,qq=(\d+)\]", text)
    qqs += re.findall(r"@[^0-9\s]*?\((\d{5,})\)", text)
    qqs += re.findall(r"@(\d{5,})", text)
    seen, out = set(), []
    for q in qqs:
        q = str(q)
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


# ============================================================
# NewAPI 客户端（超级管理员令牌）
# ============================================================
class NewAPIClient:
    def __init__(self, base_url: str, admin_token: str, admin_user_id: int = 1, debug: bool = False):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.admin_user_id = admin_user_id
        self.debug = debug

    def _admin_headers(self):
        # 新版 new-api 要求 New-Api-User 请求头为令牌所属用户的 ID
        return {
            "Authorization": f"Bearer {self.admin_token}",
            "New-Api-User": str(self.admin_user_id),
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, json_body=None,
                       params=None, headers=None, timeout: int = 30, retries: int = 2):
        if not self.base_url:
            return 0, {"success": False, "message": "未配置 NewAPI 站点地址(base_url)"}
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(retries + 1):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as session:
                    async with session.request(
                        method, url, json=json_body, params=params,
                        headers=headers or self._admin_headers(),
                    ) as resp:
                        # 限流/服务端异常：退避后重试
                        if resp.status == 429:
                            last_err = "站点限流(429 Too Many Requests)，已自动重试仍失败，请调高站点 API 限流阈值或将机器人 IP 加白"
                        elif resp.status >= 500:
                            last_err = f"站点服务异常(HTTP {resp.status})"
                        else:
                            try:
                                data = await resp.json(content_type=None)
                            except Exception:
                                data = {"success": False,
                                        "message": f"HTTP {resp.status} 非JSON响应"}
                            if self.debug:
                                logger.info(
                                    f"[newapi][DEBUG] {method} {path} -> HTTP {resp.status} "
                                    f"success={data.get('success') if isinstance(data, dict) else '?'} "
                                    f"message={data.get('message') if isinstance(data, dict) else '?'} "
                                    f"data={str(data.get('data'))[:300] if isinstance(data, dict) else '?'}"
                                )
                            return resp.status, data
            except asyncio.TimeoutError:
                last_err = "请求超时（站点 30 秒内未响应）"
            except aiohttp.ClientError as e:
                last_err = f"网络请求失败: {e}"
            if attempt < retries:
                await asyncio.sleep(2 + attempt * 3)  # 退避：2s、5s
        return 0, {"success": False, "message": str(last_err)}

    async def login(self, username: str, password: str):
        """用户登录验证（无需管理员权限）"""
        return await self._request(
            "POST", "/api/user/login",
            json_body={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
        )

    async def search_user(self, keyword: str):
        """管理员：按关键词搜索用户"""
        return await self._request(
            "GET", "/api/user/search", params={"keyword": keyword}
        )

    async def get_user(self, user_id):
        """管理员：查询用户详情"""
        return await self._request("GET", f"/api/user/{user_id}")

    async def update_user(self, user_dict: dict):
        """管理员：更新用户（用于签到加额度）"""
        return await self._request("PUT", "/api/user/", json_body=user_dict)

    async def delete_user(self, user_id):
        """管理员：删除用户"""
        return await self._request("DELETE", f"/api/user/{user_id}")

    async def create_user(self, username: str, password: str):
        """管理员：创建用户（用于群友自助注册）"""
        return await self._request("POST", "/api/user/", json_body={
            "username": username,
            "password": password,
            "display_name": username,
        })

    async def login_and_checkin(self, username: str, password: str, user_id: int):
        """用户登录后调用官方签到接口 POST /api/user/checkin（返回 success, message, data）"""
        if not self.base_url:
            return False, "未配置站点地址", None
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                cookie_jar=aiohttp.CookieJar(),
            ) as session:
                async with session.post(
                    f"{self.base_url}/api/user/login",
                    json={"username": username, "password": password},
                    headers={"Content-Type": "application/json",
                             "New-Api-User": str(user_id)},
                ) as resp:
                    data = await resp.json(content_type=None)
                    if self.debug:
                        logger.info(f"[newapi][DEBUG] 签到登录: HTTP {resp.status} "
                                    f"success={data.get('success') if isinstance(data, dict) else '?'} "
                                    f"message={data.get('message') if isinstance(data, dict) else '?'}")
                    if not (isinstance(data, dict) and data.get("success")):
                        msg = data.get("message", "登录失败") if isinstance(data, dict) else "登录失败"
                        return False, f"自动登录失败（{msg}）", None
                async with session.post(
                    f"{self.base_url}/api/user/checkin",
                    headers={"New-Api-User": str(user_id), "Accept": "application/json"},
                ) as resp:
                    data = await resp.json(content_type=None)
                    if self.debug:
                        logger.info(f"[newapi][DEBUG] 官方签到: HTTP {resp.status} {str(data)[:300]}")
                    if isinstance(data, dict) and data.get("success"):
                        return True, data.get("message", "签到成功"), data.get("data")
                    msg = data.get("message", "签到失败") if isinstance(data, dict) else "签到失败"
                    return False, msg, None
        except asyncio.TimeoutError:
            return False, "请求超时（站点 30 秒内未响应）", None
        except aiohttp.ClientError as e:
            return False, f"网络请求失败: {e}", None

    async def create_redemption(self, name: str, quota: int):
        """管理员：创建 1 个指定额度的兑换码（备用）"""
        return await self._request("POST", "/api/redemption/", json_body={
            "name": name, "quota": quota, "count": 1,
        })


# ============================================================
# 插件主体
# ============================================================
@register(
    "astrbot_plugin_newapi_helper",
    "YourName",
    "NewAPI 账号绑定/签到/余额查询/退群自动删号",
    "1.0.0",
)
class NewAPIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.store = Store(self._get_data_dir() / "bindings.json")
        self.hongbao = HongbaoStore(self._get_data_dir() / "hongbao.json")
        self.dbq = MySQLQuota(self._cfg)
        self.pending_binds = {}  # qq -> {user_id, username, expire, tries} ID绑定待验证
        self.client = NewAPIClient(
            str(self._cfg("base_url", "")),
            str(self._cfg("admin_token", "")),
            int(self._cfg("admin_user_id", 1) or 1),
            debug=bool(self._cfg("debug_mode", False)),
        )
        self.debug = bool(self._cfg("debug_mode", False))
        logger.info("[newapi] astrbot_plugin_newapi 已加载")

    async def initialize(self):
        """AstrBot 生命周期：红包功能需要时预连接站点数据库"""
        if self._cfg("db.hongbao_enabled", False) and self.dbq.configured():
            await self.dbq.connect()

    # ---------- 基础工具 ----------
    def _cfg(self, key, default=None):
        """读取配置，支持 db.rob.penalty 这类多级点号 key（按 . 递归下钻）"""
        try:
            node = self.config
            for part in str(key).split("."):
                if node is None or not hasattr(node, "get"):
                    return default
                node = node.get(part)
            return node if node is not None else default
        except Exception:
            return default

    @staticmethod
    def _get_data_dir() -> Path:
        try:
            from astrbot.api.star import StarTools
            p = Path(StarTools.get_data_dir("astrbot_plugin_newapi"))
        except Exception:
            p = Path("data/astrbot_plugin_newapi")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _fmt_quota(self, quota) -> str:
        try:
            q = int(quota)
        except Exception:
            q = 0
        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        usd = q / per
        if self._cfg("show_cny", True):
            rate = float(self._cfg("exchange_rate", 7.2) or 7.2)
            return f"${usd:.4f}（≈ ¥{usd * rate:.2f}）"
        return f"${usd:.4f}"

    def _pwd_key(self) -> str:
        return str(self._cfg("admin_token", "")) or "astrbot-plugin-newapi"

    async def _db(self):
        """数据库模式已开启且连接正常时返回连接池，否则返回 None（回退 API 模式）"""
        if not self._cfg("db_enabled", False):
            return None
        if not await self.dbq.connect():
            return None
        return self.dbq

    async def _api_login_confirm(self, username: str, password: str):
        """通过站点登录接口验证账号密码并确认身份。
        返回 (login_id, login_username, login_group, errmsg)；errmsg 为 None 表示成功。
        当站点密码哈希无法在本地识别（bad_hash_format）时，回退到此接口兜底。"""
        status, data = await self.client.login(username, password)
        if not (data.get("success") and data.get("data")):
            return None, None, None, data.get("message", "验证失败")
        login_user = data["data"] if isinstance(data.get("data"), dict) else {}
        login_id = login_user.get("id")
        if login_id is None:
            # 部分版本登录响应不返回 id，用管理员权限按用户名反查确认身份
            status, data = await self.client.search_user(username)
            for u in _extract_user_list(data):
                if str(u.get("username", "")).lower() == username.lower():
                    login_id = u.get("id")
                    break
        if login_id is None:
            return None, None, None, "__no_id__"
        return login_id, login_user.get("username") or username, login_user.get("group"), None

    def _is_group(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        return bool(gid)

    def _group_allowed(self, event: AstrMessageEvent) -> bool:
        """群聊白名单：白名单为空=所有群放行；非空=仅白名单内群号放行；私聊不受限"""
        if not self._is_group(event):
            return True
        wl = [str(g).strip() for g in (self._cfg("whitelist_groups", []) or [])
              if str(g).strip()]
        if not wl:
            return True
        return str(event.get_group_id()) in wl

    @staticmethod
    def _is_aiocqhttp(event: AstrMessageEvent) -> bool:
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            return isinstance(event, AiocqhttpMessageEvent)
        except Exception:
            return False

    async def _get_member_level(self, event: AstrMessageEvent) -> Optional[int]:
        """获取群友的 QQ 群聊等级（OneBot / NapCat）"""
        try:
            if not self._is_aiocqhttp(event):
                return None
            info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
            )
            level = info.get("level")
            return int(level) if level is not None else None
        except Exception as e:
            logger.warning(f"[newapi] 获取群成员等级失败: {e}")
            return None

    async def _get_qq_level(self, event: AstrMessageEvent) -> Optional[int]:
        """获取 QQ 账号等级（OneBot get_stranger_info 的 level 字段）"""
        try:
            if not self._is_aiocqhttp(event):
                return None
            info = await event.bot.api.call_action(
                "get_stranger_info", user_id=int(event.get_sender_id())
            )
            level = info.get("level")
            return int(level) if level is not None else None
        except Exception as e:
            logger.warning(f"[newapi] 获取 QQ 等级失败: {e}")
            return None

    async def _send_private(self, event: AstrMessageEvent, qq: str, text: str) -> bool:
        """主动私聊发送消息"""
        try:
            if self._is_aiocqhttp(event):
                await event.bot.api.call_action(
                    "send_private_msg", user_id=int(qq), message=text
                )
                return True
        except Exception as e:
            logger.error(f"[newapi] 私聊发送失败: {e}")
        return False

    async def _send_group_recall(self, event: AstrMessageEvent, text: str, delay: int) -> bool:
        """私聊失败时的兜底：发到群里，delay 秒后自动撤回"""
        if not self._is_aiocqhttp(event) or not self._is_group(event):
            return False
        try:
            result = await event.bot.api.call_action(
                "send_group_msg", group_id=int(event.get_group_id()), message=text
            )
            mid = result.get("message_id") if isinstance(result, dict) else None
            if mid is None:
                return False
            async def _del():
                await asyncio.sleep(delay)
                try:
                    await event.bot.api.call_action("delete_msg", message_id=mid)
                except Exception:
                    pass
            asyncio.ensure_future(_del())
            return True
        except Exception as e:
            logger.error(f"[newapi] 群发撤回兜底失败: {e}")
            return False

    # ---------- 绑定 ----------
    @filter.command("密码绑定", alias={"newapi绑定", "绑定newapi", "绑定账号"})
    async def bind(self, event: AstrMessageEvent, username: str = "", password: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """绑定 NewAPI 账号：/密码绑定 用户名 密码（强烈建议私聊使用）"""
        async for r in self._bind_impl(event, username, password):
            yield r

    async def _bind_impl(self, event: AstrMessageEvent, username: str = "", password: str = ""):
        qq = str(event.get_sender_id()).strip()
        if not username:
            yield event.plain_result(
                "用法：/密码绑定 <用户名> <密码>\n"
                "⚠️ 密码验证建议私聊使用，群聊中发送会被群成员看到！"
            )
            return

        if not self._cfg("allow_group_bind", False) and self._is_group(event):
            yield event.plain_result("出于安全考虑，请私聊我进行账号绑定（加我为好友后私聊发送）")
            return

        rec = await self.store.get(qq)
        if rec and not self.debug:
            yield event.plain_result(
                f"你已绑定账号：{rec.get('username')}\n如需更换，请先使用 /解绑"
            )
            return

        verify = bool(self._cfg("bind_verify_password", True))
        uid = None
        db = await self._db()
        if db is not None:
            # 数据库模式：bcrypt 直接校验 / 按用户名查询
            if verify:
                user, err = await db.verify_user_password(username, password)
            else:
                user, err = await db.get_user_by_username(username), None
            if user is None:
                if err == "user_not_found":
                    yield event.plain_result("绑定失败：账号不存在（注意填的是账号名，不是数字 ID）")
                elif err == "no_password":
                    yield event.plain_result("绑定失败：该账号未设置密码（可能通过第三方登录注册），请先在网站「个人设置」里设置密码后再绑定")
                elif err == "bad_hash_format":
                    # 站点密码哈希无法本地识别，回退到站点登录接口验证
                    if not password:
                        yield event.plain_result("请提供密码：/密码绑定 <用户名> <密码>（建议私聊）")
                        return
                    uid, _u, _g, emsg = await self._api_login_confirm(username, password)
                    if emsg == "__no_id__":
                        yield event.plain_result("绑定失败：无法确认该账号的身份（用户ID查询失败），请稍后再试")
                        return
                    if emsg:
                        yield event.plain_result(f"绑定失败：用户名或密码错误（{emsg}）")
                        return
                elif err == "error":
                    yield event.plain_result("绑定失败：校验服务异常，请稍后再试")
                else:
                    yield event.plain_result("绑定失败：用户名或密码错误")
                return
            uid = user["id"]
        elif verify:
            if not password:
                yield event.plain_result("请提供密码：/密码绑定 <用户名> <密码>（建议私聊）")
                return
            status, data = await self.client.login(username, password)
            if not (data.get("success") and data.get("data")):
                msg = data.get("message", "验证失败")
                yield event.plain_result(f"绑定失败：用户名或密码错误（{msg}）")
                return
            uid = data["data"].get("id")
        else:
            # 免密模式：管理员搜索用户名精确匹配
            status, data = await self.client.search_user(username)
            exact = [u for u in _extract_user_list(data)
                     if str(u.get("username", "")).lower() == username.lower()]
            if len(exact) != 1:
                yield event.plain_result("绑定失败：未找到该用户名（或存在多个匹配）")
                return
            uid = exact[0].get("id")

        if uid is None:
            yield event.plain_result("绑定失败：未能获取用户 ID")
            return

        await self.store.set(qq, {
            "user_id": uid,
            "username": username,
            "bound_at": int(time.time()),
            "last_checkin": 0,
            "pwd": obfuscate(password, self._pwd_key()),
        })
        yield event.plain_result(f"✅ 绑定成功！{username}，发送 /余额 查询额度，/签到 每日打卡")

    @filter.command("解绑", alias={"newapi解绑", "解绑账号"})
    async def unbind(self, event: AstrMessageEvent, target_qq: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """解绑：/解绑"""
        async for r in self._unbind_impl(event, target_qq):
            yield r

    async def _unbind_impl(self, event: AstrMessageEvent, target_qq: str = ""):
        qq = str(event.get_sender_id()).strip()
        if target_qq:
            yield event.plain_result(f"解绑他人需管理员权限，请使用 /强制解绑 <QQ号>")
            return
        if await self.store.remove(qq):
            yield event.plain_result("✅ 已解绑 NewAPI 账号")
        elif self.debug:
            yield event.plain_result("（调试模式）没有绑定记录，视为解绑成功")
        else:
            yield event.plain_result("你尚未绑定 NewAPI 账号")

    # ---------- 签到 ----------
    @filter.command("签到", alias={"打卡"})
    async def checkin(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """每日签到，随机额度"""
        async for r in self._checkin_impl(event):
            yield r

    async def _checkin_impl(self, event: AstrMessageEvent):
        if not self._cfg("checkin_enabled", True):
            yield event.plain_result("签到功能未开启")
            return
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("你还没有绑定 NewAPI 账号，请先使用 /密码绑定 或 /绑定 <ID>")
            return

        cooldown_h = float(self._cfg("checkin_cooldown_hours", 24) or 24)
        last = int(rec.get("last_checkin") or 0)
        now = time.time()
        remain = last + cooldown_h * 3600 - now
        if remain > 0 and not self.debug:
            h = int(remain // 3600)
            m = int((remain % 3600) // 60)
            yield event.plain_result(f"今天已经签到过啦～ 距离下次签到还有 {h} 小时 {m} 分钟")
            return
        if remain > 0 and self.debug:
            yield event.plain_result("（调试模式）跳过冷却检查")

        db = await self._db()
        if db is not None:
            # 数据库模式：直接写 users.quota + checkins 表（站点日历可见）
            lo = float(self._cfg("checkin_min_usd", 0.1) or 0.1)
            hi = float(self._cfg("checkin_max_usd", 0.5) or 0.5)
            if lo > hi:
                lo, hi = hi, lo
            per = int(self._cfg("quota_per_unit", 500000) or 500000)
            amount = int(round(random.uniform(lo, hi) * per))
            result, awarded = await db.db_checkin(
                int(rec["user_id"]), amount, time.strftime("%Y-%m-%d"), int(now)
            )
            async with self.store.lock:
                rec["last_checkin"] = int(now)
                self.store.data["bindings"][qq] = rec
                self.store._save_sync()
            if result == "already":
                yield event.plain_result("☀️ 今日已签到，明天再来吧～")
            elif result == "fail":
                yield event.plain_result("签到失败：数据库写入出错，请稍后再试")
            else:
                yield event.plain_result(f"🎉 签到成功！获得额度：{self._fmt_quota(awarded)}")
            return

        pwd = deobfuscate(rec.get("pwd", ""), self._pwd_key())
        if not pwd:
            yield event.plain_result(
                "签到失败：绑定记录中没有保存密码（旧版绑定），请 /解绑 后重新绑定"
            )
            return
        ok, msg, data = await self.client.login_and_checkin(
            str(rec.get("username")), pwd, int(rec.get("user_id"))
        )
        if not ok:
            if "未启用" in msg or "enable" in msg.lower():
                yield event.plain_result(
                    "签到失败：站点未开启签到功能，请联系管理员在 NewAPI 后台开启「签到设置」"
                )
                return
            if "已" in msg or "already" in msg.lower():
                # 站内今日已签到过
                async with self.store.lock:
                    rec["last_checkin"] = int(now)
                    self.store.data["bindings"][qq] = rec
                    self.store._save_sync()
                yield event.plain_result("☀️ 今日已签到，明天再来吧～")
                return
            yield event.plain_result(f"签到失败：{msg}")
            return

        async with self.store.lock:
            rec["last_checkin"] = int(now)
            self.store.data["bindings"][qq] = rec
            self.store._save_sync()

        awarded = data.get("quota_awarded") if isinstance(data, dict) else None
        amount_txt = f"获得额度：{self._fmt_quota(awarded)}\n" if awarded is not None else ""
        yield event.plain_result(f"🎉 签到成功！{amount_txt}")

    # ---------- 余额查询 ----------
    @filter.command("余额", alias={"查询余额", "我的额度"})
    async def balance(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """查询绑定的 NewAPI 账号余额"""
        async for r in self._balance_impl(event):
            yield r

    async def _balance_impl(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("你还没有绑定 NewAPI 账号，请先使用 /密码绑定 或 /绑定 <ID>")
            return
        db = await self._db()
        if db is not None:
            u = await db.get_user(rec["user_id"])
            if not u:
                yield event.plain_result("查询失败：数据库中未找到该用户")
                return
            used_usd = int(u.get("used_quota") or 0) / int(self._cfg("quota_per_unit", 500000) or 500000)
            yield event.plain_result(
                f"👤 账号：{u.get('username')}\n"
                f"💰 剩余额度：{self._fmt_quota(u.get('quota'))}\n"
                f"📊 累计消耗：${used_usd:.4f}\n"
                f"🔢 调用次数：{u.get('request_count', 0)}"
            )
            return
        status, data = await self.client.get_user(rec["user_id"])
        if not (data.get("success") and isinstance(data.get("data"), dict)):
            yield event.plain_result(f"查询失败：{data.get('message', '未知错误')}")
            return
        u = data["data"]
        used_usd = int(u.get("used_quota") or 0) / int(self._cfg("quota_per_unit", 500000) or 500000)
        yield event.plain_result(
            f"👤 账号：{u.get('username', rec.get('username'))}\n"
            f"💰 剩余额度：{self._fmt_quota(u.get('quota'))}\n"
            f"📊 累计消耗：${used_usd:.4f}\n"
            f"🔢 调用次数：{u.get('request_count', 0)}"
        )

    # ---------- 红包 ----------
    async def _sweep_expired_hongbao(self):
        """过期红包自动退回未领取部分（真实入账）"""
        expire_h = float(self._cfg("db.hongbao_expire_hours", 24) or 24)
        now = time.time()
        for pid, p in (await self.hongbao.all()).items():
            if now - p.get("created", 0) < expire_h * 3600:
                continue
            remain = sum(p.get("shares", []))
            if remain > 0:
                ok = await self.dbq.adjust(p["sender_uid"], remain)
                logger.info(f"[newapi] 红包 {pid} 过期，退回发起者 {remain} raw quota: {ok}")
            await self.hongbao.remove(pid)

    @filter.command("发红包", alias={"拼手气红包"})
    async def send_hongbao(self, event: AstrMessageEvent, count_str: str = "", amount_str: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """发拼手气红包：/发红包 10个 1.5余额（总金额按美元额度计，真实扣款）"""
        async for r in self._send_hongbao_impl(event, count_str, amount_str):
            yield r

    async def _send_hongbao_impl(self, event: AstrMessageEvent, count_str: str = "", amount_str: str = ""):
        if not self._cfg("db.hongbao_enabled", False):
            yield event.plain_result("红包功能未开启")
            return
        if not self._is_group(event):
            yield event.plain_result("请在群聊中发红包")
            return
        await self._sweep_expired_hongbao()

        import re as _re
        cn = _re.findall(r"\d+(?:\.\d+)?", count_str or "")
        am = _re.findall(r"\d+(?:\.\d+)?", amount_str or "")
        if not cn or not am:
            yield event.plain_result(
                "用法：/发红包 <个数> <总金额>\n例如：/发红包 10个 1.5余额（总额 1.5 美元额度）"
            )
            return
        count = int(float(cn[0]))
        amount = float(am[0])
        if not (1 <= count <= 100):
            yield event.plain_result("红包个数需在 1~100 之间")
            return
        if amount <= 0:
            yield event.plain_result("红包总金额必须大于 0")
            return

        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("发红包前请先绑定 NewAPI 账号（/密码绑定 或 /绑定 <ID>）")
            return

        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        raw_total = int(round(amount * per))
        if raw_total < count:
            yield event.plain_result("红包总金额太小，无法拆分，请提高金额")
            return
        if not await self.dbq.connect():
            yield event.plain_result(
                "红包功能不可用：管理员未正确配置站点数据库连接（插件配置页 db_* 项）"
            )
            return

        # 绑定的账号可能已在站点后台被删除
        if not await self.dbq.get_user(rec["user_id"]):
            await self.store.remove(qq)
            yield event.plain_result(
                "你绑定的 NewAPI 账号已在站点被删除，已自动解除绑定，请重新 /注册 或 /绑定 <ID>"
            )
            return

        # 真实扣款（余额不足则原子失败）
        ok = await self.dbq.adjust(int(rec["user_id"]), -raw_total, require_balance=True)
        if not ok:
            yield event.plain_result("发红包失败：你的余额不足，或扣款操作异常")
            return

        shares = split_red_packet(raw_total, count)
        pid = f"{event.get_group_id()}-{int(time.time() * 1000)}"
        await self.hongbao.add(pid, {
            "group_id": str(event.get_group_id()),
            "sender_qq": qq,
            "sender_uid": int(rec["user_id"]),
            "count": count,
            "shares": shares,
            "claimed": {},
            "created": int(time.time()),
        })
        sender_name = getattr(event, "get_sender_name", lambda: qq)() or qq
        yield event.plain_result(
            f"🧧 {sender_name} 发出拼手气红包（共 {count} 个）！\n发送 /抢红包 领取，手快有手慢无～"
        )

    @filter.command("抢红包")
    async def grab_hongbao(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """抢本群未抢完的拼手气红包，金额真实入账"""
        async for r in self._grab_hongbao_impl(event):
            yield r

    async def _grab_hongbao_impl(self, event: AstrMessageEvent):
        if not self._cfg("db.hongbao_enabled", False):
            yield event.plain_result("红包功能未开启")
            return
        if not self._is_group(event):
            yield event.plain_result("请在群聊中抢红包")
            return
        await self._sweep_expired_hongbao()

        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("需要先绑定 NewAPI 账号才能抢红包（/密码绑定 或 /绑定 <ID>）")
            return
        if not await self.dbq.connect():
            yield event.plain_result("红包功能不可用：站点数据库未连接")
            return

        # 绑定的账号可能已在站点后台被删除
        if not await self.dbq.get_user(rec["user_id"]):
            await self.store.remove(qq)
            yield event.plain_result(
                "你绑定的 NewAPI 账号已在站点被删除，已自动解除绑定，请重新 /注册 或 /绑定 <ID>"
            )
            return

        expire_h = float(self._cfg("db.hongbao_expire_hours", 24) or 24)
        now = time.time()
        gid = str(event.get_group_id())
        target_pid, target = None, None
        for pid, p in (await self.hongbao.all()).items():
            if p.get("group_id") != gid:
                continue
            if now - p.get("created", 0) > expire_h * 3600:
                continue
            if qq in p.get("claimed", {}):
                continue
            if not p.get("shares"):
                continue
            if target is None or p.get("created", 0) > target.get("created", 0):
                target_pid, target = pid, p

        if target is None:
            yield event.plain_result("手慢了，没有可以抢的红包（或你已抢过本群红包）")
            return

        share = target["shares"].pop(random.randrange(len(target["shares"])))
        # 真实入账；失败则把份额放回
        ok = await self.dbq.adjust(int(rec["user_id"]), share)
        if not ok:
            target["shares"].append(share)
            await self.hongbao.update(target_pid, target)
            yield event.plain_result("抢红包失败：入账异常，红包份额已保留，请稍后再试")
            return

        target["claimed"][qq] = share
        await self.hongbao.update(target_pid, target)
        reply = f"🧧 抢到 {self._fmt_quota(share)}！"
        if not target["shares"]:
            reply += f"\n红包已被抢完啦～（共 {target['count']} 个）"
        yield event.plain_result(reply)

    # ---------- 抢劫玩法 ----------
    @filter.command("抢劫", alias={"打劫", "抢钱"})
    async def rob(self, event: AstrMessageEvent, target: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """抢劫群友的 NewAPI 余额：/抢劫 @某人 或 /抢劫 <QQ号/用户名>，成功/失败均真实扣款入账"""
        async for r in self._rob_impl(event, target):
            yield r

    async def _resolve_target_qq(self, event: AstrMessageEvent, arg: str):
        """解析抢劫目标 QQ：优先 @，其次纯数字 QQ 号，再次 NewAPI 用户名；返回 (qq, errmsg)"""
        arg = (arg or "").strip()
        sender = str(event.get_sender_id()).strip()
        try:
            self_id = str(event.get_self_id())
        except Exception:
            self_id = None
        # 1) @ 提及（排除发送者自己与机器人自身）
        for q in _extract_at_qqs(event):
            if q != sender and q != self_id:
                return q, None
        # 2) 纯数字 → QQ 号
        if arg.isdigit():
            if arg == sender:
                return None, "不能抢劫自己"
            return arg, None
        # 3) 用户名 → 反查绑定 QQ
        if arg:
            db = await self._db()
            if db is not None:
                users = await db.search_users(arg, limit=10)
                matched = [u for u in users
                           if str(u.get("username", "")).lower() == arg.lower()]
                if len(matched) == 1:
                    qq = await self.store.find_by_user_id(matched[0].get("id"))
                    if qq and qq != sender:
                        return qq, None
                    return None, f"用户 {arg} 尚未绑定 QQ，无法作为目标"
                return None, f"未找到用户名 {arg}（或存在多个匹配）"
            return None, "当前为 API 模式，无法按用户名定位目标，请用 @ 或 QQ 号"
        return None, "请指定目标：/抢劫 @某人 或 /抢劫 <QQ号/用户名>"

    async def _rob_impl(self, event: AstrMessageEvent, target: str = ""):
        if not self._cfg("db.rob_enabled", False):
            yield event.plain_result("抢劫玩法未开启（需在数据库模式下打开「🔪 抢劫玩法」开关）")
            return
        if not self._is_group(event):
            yield event.plain_result("请在群聊中使用抢劫")
            return

        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("抢劫前请先绑定 NewAPI 账号（/密码绑定 或 /绑定 <ID>）")
            return

        t_qq, err = await self._resolve_target_qq(event, target)
        if err:
            yield event.plain_result(err)
            return

        db = await self._db()
        if db is None:
            yield event.plain_result("抢劫玩法不可用：站点数据库未连接")
            return

        t_rec = await self.store.get(t_qq)
        if not t_rec:
            yield event.plain_result("目标尚未绑定 NewAPI 账号，无法抢劫")
            return

        # 双方账号存在性
        attacker_u = await db.get_user(rec["user_id"])
        if not attacker_u:
            await self.store.remove(qq)
            yield event.plain_result("你绑定的 NewAPI 账号已被站点删除，已自动解绑，请重新绑定")
            return
        target_u = await db.get_user(t_rec["user_id"])
        if not target_u:
            yield event.plain_result("目标绑定的 NewAPI 账号已被站点删除，无法抢劫")
            return

        # 冷却
        def _num(key, default):
            v = self._cfg(key, None)
            if v is None or v == "":
                return default
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        cd = int(_num("db.rob.cooldown_seconds", 300))
        last = int(rec.get("last_rob") or 0)
        now = time.time()
        remain = last + cd - now
        if remain > 0 and not self.debug:
            yield event.plain_result(f"抢劫冷却中，还需 {int(remain)} 秒")
            return

        rate = _num("db.rob.success_rate", 0.5)
        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        amount_min = _num("db.rob.amount_min", 1.0)       # 美元
        amount_max = _num("db.rob.amount_max", 10.0)      # 美元
        penalty_usd = _num("db.rob.penalty", 1.0)         # 美元
        protect_usd = _num("db.rob.protect_balance", 0.0)  # 美元
        if amount_min > amount_max:
            amount_min, amount_max = amount_max, amount_min

        protect = max(0, int(round(protect_usd * per)))
        target_quota = int(target_u.get("quota") or 0)
        if target_quota <= protect:
            yield event.plain_result("目标余额不足（低于保护线），无法抢劫")
            return

        # 按美元金额：在 [min,max] 美元内随机抽，换算为额度扣款，不超过目标余额 - 保护线
        amount_usd = random.uniform(amount_min, amount_max)
        amount = max(1, int(round(amount_usd * per)))
        penalty = max(1, int(round(penalty_usd * per)))
        cap = target_quota - protect
        if amount > cap:
            amount = cap
        if amount <= 0:
            yield event.plain_result("目标余额不足（低于保护线），无法抢劫")
            return

        attacker_uid = int(rec["user_id"])
        target_uid = int(t_rec["user_id"])
        aname = getattr(event, "get_sender_name", lambda: qq)() or qq
        tname = t_rec.get("username") or t_qq

        def _mark_cooled():
            rec["last_rob"] = int(now)
            self.store.data["bindings"][qq] = rec
            self.store._save_sync()

        if random.random() < rate:
            # 成功：先原子扣目标，再加抢劫者（加账失败则回滚目标）
            if not await db.adjust(target_uid, -amount, require_balance=True):
                yield event.plain_result("抢劫失败：目标余额变动异常，请稍后再试")
                return
            if not await db.adjust(attacker_uid, amount):
                await db.adjust(target_uid, amount)
                yield event.plain_result("抢劫失败：入账异常，已回滚，请稍后再试")
                return
            async with self.store.lock:
                _mark_cooled()
            yield event.plain_result(
                f"🔪 抢劫成功！{aname} 从 {tname} 手中抢走 {self._fmt_quota(amount)}"
            )
        else:
            # 失败：先原子扣抢劫者赔偿，再赔给目标（入账失败则回滚）
            if not await db.adjust(attacker_uid, -penalty, require_balance=True):
                yield event.plain_result("😰 抢劫失败，且你的余额不足以支付赔偿，被抓现行！")
                return
            if not await db.adjust(target_uid, penalty):
                await db.adjust(attacker_uid, penalty)
                yield event.plain_result("抢劫失败：赔偿入账异常，已回滚，请稍后再试")
                return
            async with self.store.lock:
                _mark_cooled()
            yield event.plain_result(
                f"😅 抢劫失败！{aname} 被 {tname} 反杀，赔偿 {self._fmt_quota(penalty)}"
            )

    # ---------- 自助注册 ----------
    @filter.command("注册")
    async def register(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """群内自助注册：以 QQ 号为用户名，随机 8 位密码私聊发送"""
        async for r in self._register_impl(event):
            yield r

    async def _register_impl(self, event: AstrMessageEvent):
        if not self._cfg("register_enabled", True):
            yield event.plain_result("注册功能未开启")
            return
        if not self._is_group(event):
            yield event.plain_result("请在群聊中使用 /注册")
            return
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if rec and not self.debug:
            yield event.plain_result(
                f"你已拥有账号：{rec.get('username')}，无需重复注册（如需更换请先 /解绑）"
            )
            return

        # QQ 群等级门槛
        min_level = int(self._cfg("register_min_level", 0) or 0)
        if min_level > 0:
            level = await self._get_member_level(event)
            if level is None:
                yield event.plain_result(
                    "无法获取你的群聊等级，暂时无法注册（请联系管理员检查适配端）"
                )
                return
            if level < min_level:
                yield event.plain_result(
                    f"注册需要群聊等级 Lv.{min_level}，你当前 Lv.{level}，继续水群吧～"
                )
                return

        # QQ 账号等级门槛（与群聊等级双重校验）
        min_qq_level = int(self._cfg("register_min_qq_level", 0) or 0)
        if min_qq_level > 0:
            qq_level = await self._get_qq_level(event)
            if qq_level is None:
                yield event.plain_result(
                    "无法获取你的 QQ 等级，暂时无法注册（请联系管理员检查适配端）"
                )
                return
            if qq_level < min_qq_level:
                yield event.plain_result(
                    f"注册需要 QQ 等级 {min_qq_level} 级，你当前 {qq_level} 级，先把 QQ 养一养吧～"
                )
                return

        username = qq
        password = "".join(random.choices("0123456789", k=8))

        db = await self._db()
        if db is not None:
            # 数据库模式：注册/找回全部走数据库
            reg_group = str(self._cfg("register_group", "default") or "default").strip()
            existing = await db.get_user_by_username(username)
            is_new = existing is None
            if is_new:
                uid = await db.create_user(username, password, reg_group)
                if uid is None:
                    yield event.plain_result("❌ 注册失败：数据库写入出错，请稍后再试")
                    return
            else:
                other = await self.store.find_by_user_id(existing["id"])
                if other and other != qq:
                    yield event.plain_result(f"❌ 注册失败：该账号已被其他 QQ({other}) 绑定")
                    return
                uid = existing["id"]
                if not await db.reset_password(uid, password):
                    yield event.plain_result("❌ 注册失败：重置密码出错，请稍后再试")
                    return
                if not await db.set_group(uid, reg_group):
                    logger.error("[newapi] 设置注册分组失败(DB)")
            await self.store.set(qq, {
                "user_id": uid,
                "username": username,
                "bound_at": int(time.time()),
                "last_checkin": 0,
                "registered": True,
                "pwd": obfuscate(password, self._pwd_key()),
            })
            title = "注册成功！" if is_new else "找回成功！已为你重置密码"
            sent = await self._send_private(event, qq,
                f"🎉 {title}\n"
                f"👤 账号：{username}\n"
                f"🔑 密码：{password}\n"
                f"👥 分组：{reg_group}\n"
                f"🌐 登录：{self.client.base_url}\n"
                f"请妥善保管账号密码，也可使用 /余额 /签到 等命令"
            )
            if sent:
                yield event.plain_result("✅ 注册成功！账号密码已私聊发送给你，请查收～")
            elif self._cfg("recall_when_private_fail", True) and self._is_group(event):
                delay = int(self._cfg("recall_delay_seconds", 10) or 10)
                if await self._send_group_recall(event,
                    f"🎉 {title}\n👤 账号：{username}\n🔑 密码：{password}\n"
                    f"👥 分组：{reg_group}\n🌐 登录：{self.client.base_url}\n"
                    f"⚠️ 本条 {delay} 秒后自动撤回，请立即保存！",
                    delay):
                    yield event.plain_result(
                        f"✅ 注册成功！账号密码已发到群里，{delay} 秒后自动撤回，请立即保存"
                    )
                    return
                yield event.plain_result("注册成功，但私聊发送失败：请先添加我为好友，然后联系管理员处理")
            else:
                yield event.plain_result("注册成功，但私聊发送失败：请先添加我为好友，然后联系管理员处理")
            return

        # 创建用户
        status, data = await self.client.create_user(username, password)
        is_new = bool(data.get("success"))
        if not is_new:
            msg = str(data.get("message", "未知错误"))
            # 用户名已存在（此前注册过）：不报错，走找回流程
            if "Duplicate" not in msg and "已存在" not in msg:
                hint = ""
                if "超时" in msg or "网络" in msg:
                    hint = ("\n排查建议：在机器人所在服务器上访问 "
                            f"{self.client.base_url}/api/status 测试连通性；"
                            "确认站点未卡顿、反代未限流")
                yield event.plain_result(f"❌ 注册失败：{msg}{hint}")
                return

        # 查询用户（新建或找回）
        uid, user = None, None
        status, data = await self.client.search_user(username)
        for u in _extract_user_list(data):
            if str(u.get("username")) == username:
                uid, user = u.get("id"), dict(u)
                break
        if uid is None:
            site_msg = data.get("message", "") if isinstance(data, dict) else ""
            yield event.plain_result(
                f"❌ 注册失败：创建账号后未能查询到用户信息（{site_msg or '可能被站点限流，请稍后再试'}）"
            )
            return

        if not is_new:
            # 找回流程：该账号不能已被其他 QQ 绑定
            other = await self.store.find_by_user_id(uid)
            if other and other != qq:
                yield event.plain_result(f"❌ 注册失败：该账号已被其他 QQ({other}) 绑定")
                return

        # 设置注册默认分组（找回时同时重置密码）
        reg_group = str(self._cfg("register_group", "default") or "default").strip()
        user["group"] = reg_group
        if not is_new:
            user["password"] = password  # 管理员权限重置密码
        status, data = await self.client.update_user(user)
        group_ok = bool(data.get("success"))
        if not group_ok:
            logger.error(f"[newapi] 设置注册分组失败: {data.get('message')}")

        await self.store.set(qq, {
            "user_id": uid,
            "username": username,
            "bound_at": int(time.time()),
            "last_checkin": 0,
            "registered": True,
            "pwd": obfuscate(password, self._pwd_key()),
        })

        base = self.client.base_url
        title = "注册成功！" if is_new else "找回成功！已为你重置密码"
        sent = await self._send_private(event, qq,
            f"🎉 {title}\n"
            f"👤 账号：{username}\n"
            f"🔑 密码：{password}\n"
            f"👥 分组：{reg_group}\n"
            f"🌐 登录：{base}\n"
            f"请妥善保管账号密码，也可使用 /余额 /签到 等命令"
        )
        if sent:
            yield event.plain_result(
                "✅ 注册成功！账号密码已私聊发送给你，请查收～"
                + ("" if group_ok else "\n⚠️ 默认分组设置失败，请联系管理员")
            )
        elif self._cfg("recall_when_private_fail", True) and self._is_group(event):
            delay = int(self._cfg("recall_delay_seconds", 10) or 10)
            if await self._send_group_recall(event,
                f"🎉 注册成功！\n👤 账号：{username}\n🔑 密码：{password}\n"
                f"👥 分组：{reg_group}\n🌐 登录：{base}\n"
                f"⚠️ 本条 {delay} 秒后自动撤回，请立即保存！",
                delay):
                yield event.plain_result(
                    f"✅ 注册成功！账号密码已发到群里，{delay} 秒后自动撤回，请立即保存"
                )
            else:
                yield event.plain_result("注册成功，但私聊发送失败：请先添加我为好友，然后联系管理员处理")
        else:
            yield event.plain_result(
                "注册成功，但私聊发送失败：请先添加我为好友，然后联系管理员处理"
            )

    # ---------- 按 ID 绑定（私聊验证两步流程） ----------
    @filter.command("绑定", alias={"绑定ID", "绑定id"})
    async def bind_id(self, event: AstrMessageEvent, user_id: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """发起 ID 绑定：/绑定 1，随后私聊输入账号与密码完成验证"""
        async for r in self._bind_id_impl(event, user_id):
            yield r

    async def _bind_id_impl(self, event: AstrMessageEvent, user_id: str = ""):
        if not user_id or not user_id.isdigit():
            yield event.plain_result("用法：/绑定 <NewAPI用户ID数字>，例如 /绑定 1")
            return
        qq = str(event.get_sender_id()).strip()
        if not self.debug and await self.store.get(qq):
            yield event.plain_result("你已绑定过账号，如需更换请先 /解绑")
            return

        # 检查该 ID 是否已被其他 QQ 绑定
        other = await self.store.find_by_user_id(user_id)
        if other:
            yield event.plain_result(f"该账号已被 QQ({other}) 绑定，无法重复绑定")
            return

        db = await self._db()
        user = None
        if db is not None:
            user = await db.get_user(user_id)
            if not user:
                yield event.plain_result(f"绑定失败：找不到用户 ID {user_id}")
                return
        else:
            status, data = await self.client.get_user(user_id)
            if not (data.get("success") and isinstance(data.get("data"), dict)):
                yield event.plain_result(f"绑定失败：找不到用户 ID {user_id}")
                return
            user = data["data"]

        # 管理员账号保护（防止他人绑定管理员账号后经退群删号误删）
        if self._cfg("bind_protect_admin", True) and int(user.get("role") or 0) >= 10:
            yield event.plain_result(
                "该账号为管理员账号，受保护无法通过 ID 绑定（可在插件配置中关闭 bind_protect_admin）"
            )
            return

        # 记录待验证绑定，私聊收集账号密码
        self.pending_binds[qq] = {
            "user_id": int(user_id),
            "username": user.get("username", ""),
            "expire": time.time() + 600,
            "tries": 3,
        }
        sent = await self._send_private(event, qq,
            f"🔐 你正在绑定 NewAPI 账号（ID:{user_id}，用户名：{user.get('username', '未知')}）\n"
            f"请在 10 分钟内私聊回复：账号 密码（用空格分隔）\n"
            f"例如：{user.get('username', '账号')} 你的密码\n"
            f"验证通过即完成绑定，共 3 次尝试机会。发送 /取消绑定 可放弃"
        )
        if sent:
            yield event.plain_result("✅ 已私聊你，请按私聊指引回复账号与密码完成绑定")
        else:
            del self.pending_binds[qq]
            yield event.plain_result("私聊发送失败：请先添加我为好友，再重新使用 /绑定 <ID>")

    @filter.command("取消绑定", alias={"取消绑定ID"})
    async def cancel_bind(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        async for r in self._cancel_bind_impl(event):
            yield r

    async def _cancel_bind_impl(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id()).strip()
        if self.pending_binds.pop(qq, None) is not None:
            yield event.plain_result("已取消本次绑定")
        else:
            yield event.plain_result("你没有进行中的绑定操作")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_bind_verify(self, event: AstrMessageEvent):
        """私聊验证：处理待绑定用户发来的 账号 密码"""
        try:
            qq = str(event.get_sender_id()).strip()
            pending = self.pending_binds.get(qq)
            if not pending:
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return  # 让其他命令正常处理

            if time.time() > pending["expire"]:
                del self.pending_binds[qq]
                yield event.plain_result("绑定验证已超时，请回到群里重新使用 /绑定 <ID>")
                return

            parts = text.split()
            if len(parts) != 2:
                yield event.plain_result("格式不对，请回复：账号 密码（用空格分隔）")
                return
            username, password = parts

            db = await self._db()
            if db is not None:
                # 数据库模式：本地校验（bcrypt 或常见变体）
                vuser, verr = await db.verify_user_password(username, password)
                if not vuser:
                    if verr == "bad_hash_format":
                        # 站点密码哈希无法本地识别，回退到站点登录接口验证
                        lid, luname, lgroup, emsg = await self._api_login_confirm(username, password)
                        if emsg is not None:
                            pending["tries"] -= 1
                            reason = "账号或密码错误"
                            if pending["tries"] <= 0:
                                del self.pending_binds[qq]
                                yield event.plain_result(f"{reason}，尝试次数已用完，绑定已取消，请回群重新发起")
                            else:
                                yield event.plain_result(
                                    f"{reason}，还剩 {pending['tries']} 次机会，请重新回复：账号 密码"
                                )
                            return
                        login_id, login_username, login_group = lid, luname, lgroup
                    else:
                        pending["tries"] -= 1
                        if verr == "user_not_found":
                            reason = "账号不存在（注意填的是账号名，不是数字 ID）"
                        elif verr == "no_password":
                            reason = "该账号未设置密码（可能通过第三方登录注册），请先在网站「个人设置」里设置密码后再绑定"
                        elif verr == "error":
                            reason = "校验服务异常"
                        else:
                            reason = "账号或密码错误"
                        if pending["tries"] <= 0:
                            del self.pending_binds[qq]
                            yield event.plain_result(f"{reason}，尝试次数已用完，绑定已取消，请回群重新发起")
                        else:
                            yield event.plain_result(
                                f"{reason}，还剩 {pending['tries']} 次机会，请重新回复：账号 密码"
                            )
                        return
                else:
                    login_id = vuser["id"]
                    login_username = vuser["username"]
                    login_group = vuser.get("group")
            else:
                login_id, login_username, login_group, emsg = await self._api_login_confirm(username, password)
                if emsg == "__no_id__":
                    yield event.plain_result(
                        "绑定失败：无法确认该账号的身份（用户ID查询失败），请稍后再试"
                    )
                    return
                if emsg is not None:
                    pending["tries"] -= 1
                    if pending["tries"] <= 0:
                        del self.pending_binds[qq]
                        yield event.plain_result("账号或密码错误次数过多，绑定已取消，请回群重新发起")
                    else:
                        yield event.plain_result(
                            f"账号或密码错误，还剩 {pending['tries']} 次机会，请重新回复：账号 密码"
                        )
                    return

            if str(login_id) != str(pending["user_id"]):
                yield event.plain_result(
                    f"该账号（ID:{login_id}）与你要绑定的 ID（{pending['user_id']}）不一致，绑定取消"
                )
                del self.pending_binds[qq]
                return

            # 验证通过：写入绑定并更换分组
            user_id = int(pending["user_id"])
            del self.pending_binds[qq]
            await self.store.set(qq, {
                "user_id": user_id,
                "username": login_username,
                "bound_at": int(time.time()),
                "last_checkin": 0,
                "pwd": obfuscate(password, self._pwd_key()),
            })

            group_msg = ""
            bind_group = str(self._cfg("bind_group", "") or "").strip()
            if bind_group and str(login_group) != bind_group:
                if db is not None:
                    ok = await db.set_group(user_id, bind_group)
                    group_msg = f"，分组已更换为 {bind_group}" if ok \
                        else "（⚠️ 更换分组失败：数据库写入异常）"
                else:
                    status, data = await self.client.get_user(user_id)
                    if data.get("success") and isinstance(data.get("data"), dict):
                        u = data["data"]
                        u["group"] = bind_group
                        status, data = await self.client.update_user(u)
                        if data.get("success"):
                            group_msg = f"，分组已更换为 {bind_group}"
                        else:
                            group_msg = f"（⚠️ 更换分组失败：{data.get('message')}）"

            yield event.plain_result(
                f"✅ 绑定成功！账号：{login_username}{group_msg}\n"
                f"回到群里即可使用 /签到 /余额 等命令"
            )
        except Exception:
            logger.error(f"[newapi] 私聊绑定验证出错:\n{traceback.format_exc()}")

    # ---------- 管理员命令 ----------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查用户", alias={"newapi用户", "newapi查用户"})
    async def admin_search(self, event: AstrMessageEvent, keyword: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """管理员：搜索 NewAPI 用户信息"""
        if not keyword:
            yield event.plain_result("用法：/查用户 <用户名或关键词>")
            return
        db = await self._db()
        if db is not None:
            users = await db.search_users(keyword)
        else:
            status, data = await self.client.search_user(keyword)
            users = _extract_user_list(data)
        if not users:
            yield event.plain_result("未找到相关用户")
            return
        lines = []
        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        for u in users[:10]:
            qq = await self.store.find_by_user_id(u.get("id"))
            lines.append(
                f"· {u.get('username')} (ID:{u.get('id')}) "
                f"余额:${int(u.get('quota') or 0)/per:.4f}"
                + (f" | 已绑定QQ:{qq}" if qq else "")
            )
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制解绑", alias={"newapi强制解绑"})
    async def admin_unbind(self, event: AstrMessageEvent, qq: str = ""):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        """管理员：强制解除某个 QQ 的绑定"""
        if not qq:
            yield event.plain_result("用法：/强制解绑 <QQ号>")
            return
        if await self.store.remove(qq):
            yield event.plain_result(f"✅ 已解除 QQ({qq}) 的绑定")
        else:
            yield event.plain_result(f"QQ({qq}) 没有绑定记录")

    @filter.command("帮助", alias={"newapi帮助", "newapi菜单"})
    async def help_cmd(self, event: AstrMessageEvent):
        if not self._cfg("slash_enabled", True):
            return
        if not self._group_allowed(event):
            return
        async for r in self._help_impl(event):
            yield r

    async def _help_impl(self, event: AstrMessageEvent):
        yield event.plain_result(
            "📖 NewAPI 插件命令：\n"
            "/注册 - 自助注册账号（密码私聊发送）\n"
            "/绑定 <ID> - 绑定指定 NewAPI 账号（私聊验证账号密码）并更换分组\n"
            "/密码绑定 <用户名> <密码> - 验证绑定（建议私聊）\n"
            "/解绑 - 解除绑定\n"
            "/签到 - 每日签到领额度\n"
            "/余额 - 查询账号余额\n"
            "/发红包 <个数> <总金额> - 发拼手气红包（真实扣款）\n"
            "/抢红包 - 抢群内红包（真实入账）\n"
            "/抢劫 @某人 - 抢劫群友余额（真实扣款/入账，需开启抢劫玩法）\n"
            "管理员：/查用户 <关键词> / /强制解绑 <QQ号>\n"
            "自定义前缀（如已配置 %）：例如 %签到、%注册 等价于对应命令"
        )

    # ---------- 自定义指令前缀（绕过 LLM） ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_custom_command(self, event: AstrMessageEvent):
        """自定义前缀调度：如 %签到、*注册；处理完成后拦截事件，不进入 LLM"""
        try:
            if not self._group_allowed(event):
                return
            prefixes = [str(p).strip() for p in (self._cfg("custom_command_prefixes", []) or [])
                        if str(p).strip()]
            if not prefixes:
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            for p in prefixes:
                if not text.startswith(p) or len(text) <= len(p):
                    continue
                parts = text[len(p):].strip().split()
                if not parts:
                    return
                cmd, args = parts[0], parts[1:]
                handlers = {
                    "注册": (self._register_impl, 0),
                    "绑定": (self._bind_id_impl, 1),
                    "密码绑定": (self._bind_impl, 2),
                    "解绑": (self._unbind_impl, 0),
                    "签到": (self._checkin_impl, 0),
                    "余额": (self._balance_impl, 0),
                    "取消绑定": (self._cancel_bind_impl, 0),
                    "发红包": (self._send_hongbao_impl, 2),
                    "抢红包": (self._grab_hongbao_impl, 0),
                    "抢劫": (self._rob_impl, 1),
                    "帮助": (self._help_impl, 0),
                }
                if cmd not in handlers:
                    return
                fn, nargs = handlers[cmd]
                if len(args) < nargs:
                    yield event.plain_result(f"参数不足：{p}{cmd} 后面还需要 {nargs} 个参数")
                else:
                    async for r in fn(event, *args):
                        yield r
                event.stop_event()  # 拦截，避免进入 LLM
                return
        except Exception:
            logger.error(f"[newapi] 自定义指令处理出错:\n{traceback.format_exc()}")

    # ---------- 退群监听：自动删号 ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_notice(self, event: AstrMessageEvent):
        """监听群成员减少事件（aiocqhttp / NapCat 等 OneBot 平台）"""
        try:
            raw = getattr(event.message_obj, "raw_event", None)
            if not isinstance(raw, dict):
                return
            if raw.get("post_type") != "notice":
                return
            if raw.get("notice_type") != "group_decrease":
                return
            if raw.get("sub_type") == "kick_me":
                return  # 机器人自己被踢

            watch = [str(g) for g in (self._cfg("watch_groups", []) or [])]
            group_id = str(raw.get("group_id", ""))
            if watch and group_id not in watch:
                return
            # 群聊白名单：非空时仅处理白名单内群聊的退群事件
            wl = [str(g).strip() for g in (self._cfg("whitelist_groups", []) or [])
                  if str(g).strip()]
            if wl and group_id not in wl:
                return

            leave_qq = str(raw.get("user_id", ""))
            rec = await self.store.get(leave_qq)
            if not rec:
                return
            uid = rec.get("user_id")
            username = rec.get("username", "")
            await self.store.remove(leave_qq)
            logger.info(f"[newapi] 群成员 {leave_qq} 退群，已解除绑定 {username}")

            if self._cfg("delete_on_leave", False):
                db = await self._db()
                if db is not None:
                    if await db.soft_delete_user(uid):
                        logger.info(f"[newapi] 已删除(软删) NewAPI 用户 {username} (ID:{uid})")
                    else:
                        logger.error("[newapi] 删除 NewAPI 用户失败(数据库)")
                else:
                    status, data = await self.client.delete_user(uid)
                    if data.get("success"):
                        logger.info(f"[newapi] 已删除 NewAPI 用户 {username} (ID:{uid})")
                    else:
                        logger.error(
                            f"[newapi] 删除 NewAPI 用户失败: {data.get('message')}"
                        )
        except Exception:
            logger.error(f"[newapi] 处理退群事件出错:\n{traceback.format_exc()}")

    async def terminate(self):
        logger.info("[newapi] 插件已卸载")
