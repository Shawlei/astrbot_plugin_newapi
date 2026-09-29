import asyncio
import base64
import json
import random
import time
import traceback
from pathlib import Path
from typing import Optional

import aiohttp

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
        """管理员：创建 1 个指定额度的兑换码"""
        return await self._request("POST", "/api/redemption/", json_body={
            "name": name, "quota": quota, "count": 1,
        })

    async def find_redemption_key(self, name: str):
        """管理员：按名称查找兑换码的 key"""
        status, data = await self._request(
            "GET", "/api/redemption/", params={"p": 1, "size": 50}
        )
        for item in _extract_user_list(data):
            if str(item.get("name")) == name:
                return item.get("key")
        return None


# ============================================================
# 插件主体
# ============================================================
@register(
    "astrbot_plugin_newapi",
    "YourName",
    "NewAPI 账号绑定/签到/余额查询/退群自动删号",
    "1.0.0",
)
class NewAPIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.store = Store(self._get_data_dir() / "bindings.json")
        self.pending_binds = {}  # qq -> {user_id, username, expire, tries} ID绑定待验证
        self.client = NewAPIClient(
            str(self._cfg("base_url", "")),
            str(self._cfg("admin_token", "")),
            int(self._cfg("admin_user_id", 1) or 1),
            debug=bool(self._cfg("debug_mode", False)),
        )
        self.debug = bool(self._cfg("debug_mode", False))
        logger.info("[newapi] astrbot_plugin_newapi 已加载")

    # ---------- 基础工具 ----------
    def _cfg(self, key, default=None):
        try:
            return self.config.get(key, default)
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

    def _is_group(self, event: AstrMessageEvent) -> bool:
        gid = event.get_group_id()
        return bool(gid)

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

    # ---------- 绑定 ----------
    @filter.command("密码绑定", alias={"newapi绑定", "绑定newapi", "绑定账号"})
    async def bind(self, event: AstrMessageEvent, username: str = "", password: str = ""):
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
        if verify:
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

        mode = str(self._cfg("checkin_mode", "official") or "official").strip().lower()
        if mode == "official":
            async for r in self._checkin_official(event, qq, rec):
                yield r
        else:
            async for r in self._checkin_code(event, qq, rec):
                yield r

    async def _checkin_official(self, event: AstrMessageEvent, qq: str, rec: dict):
        """官方签到模式：本地保存的密码自动登录 -> POST /api/user/checkin"""
        pwd = deobfuscate(rec.get("pwd", ""), self._pwd_key())
        if not pwd:
            yield event.plain_result(
                "签到失败：绑定记录中没有保存密码（旧版绑定），请 /解绑 后重新绑定，"
                "或将签到模式切换为兑换码模式"
            )
            return
        ok, msg, data = await self.client.login_and_checkin(
            str(rec.get("username")), pwd, int(rec.get("user_id"))
        )
        if not ok:
            if "未启用" in msg or "enable" in msg.lower():
                # 站点未开启官方签到，自动降级为兑换码模式
                async for r in self._checkin_code(event, qq, rec, reason=msg):
                    yield r
                return
            yield event.plain_result(f"签到失败：{msg}")
            return

        async with self.store.lock:
            rec["last_checkin"] = int(time.time())
            self.store.data["bindings"][qq] = rec
            self.store._save_sync()

        awarded = data.get("quota_awarded") if isinstance(data, dict) else None
        amount_txt = f"获得额度：{self._fmt_quota(awarded)}\n" if awarded is not None else ""
        yield event.plain_result(f"🎉 签到成功！{amount_txt}")

    async def _checkin_code(self, event: AstrMessageEvent, qq: str, rec: dict, reason: str = ""):
        """兑换码模式：管理员创建兑换码，私聊发给用户手动兑换"""
        lo = float(self._cfg("checkin_min_usd", 0.1) or 0.1)
        hi = float(self._cfg("checkin_max_usd", 0.5) or 0.5)
        if lo > hi:
            lo, hi = hi, lo
        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        amount_quota = int(round(random.uniform(lo, hi) * per))

        name = f"bot-{rec.get('user_id')}-{int(time.time())}"
        status, data = await self.client.create_redemption(name, amount_quota)
        if not data.get("success"):
            yield event.plain_result(f"签到失败：创建兑换码出错（{data.get('message', '未知错误')}）")
            return
        key = await self.client.find_redemption_key(name)
        if not key:
            yield event.plain_result("签到失败：兑换码已创建但未查询到，请联系管理员")
            return

        async with self.store.lock:
            rec["last_checkin"] = int(time.time())
            self.store.data["bindings"][qq] = rec
            self.store._save_sync()

        prefix = f"（{reason}，已切换为兑换码签到）\n" if reason else ""
        sent = await self._send_private(event, qq,
            f"🎁 你的签到兑换码：{key}\n"
            f"请到网站 钱包/充值 页面兑换，额度：${amount_quota / per:.4f}"
        )
        if sent:
            yield event.plain_result(f"🎉 签到成功！{prefix}兑换码已私聊发给你，请到网站兑换")
        else:
            yield event.plain_result(
                f"🎉 签到成功！{prefix}\n兑换码：{key}\n（私聊发送失败，请到网站 钱包/充值 页面兑换）"
            )

    # ---------- 余额查询 ----------
    @filter.command("余额", alias={"查询余额", "我的额度"})
    async def balance(self, event: AstrMessageEvent):
        """查询绑定的 NewAPI 账号余额"""
        async for r in self._balance_impl(event):
            yield r

    async def _balance_impl(self, event: AstrMessageEvent):
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("你还没有绑定 NewAPI 账号，请先使用 /密码绑定 或 /绑定 <ID>")
            return
        status, data = await self.client.get_user(rec["user_id"])
        if not (data.get("success") and data.get("data")):
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

    # ---------- 自助注册 ----------
    @filter.command("注册")
    async def register(self, event: AstrMessageEvent):
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
        else:
            yield event.plain_result(
                "注册成功，但私聊发送失败：请先添加我为好友，然后联系管理员处理"
            )

    # ---------- 按 ID 绑定（私聊验证两步流程） ----------
    @filter.command("绑定", alias={"绑定ID", "绑定id"})
    async def bind_id(self, event: AstrMessageEvent, user_id: str = ""):
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

        status, data = await self.client.get_user(user_id)
        if not (data.get("success") and data.get("data")):
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

            status, data = await self.client.login(username, password)
            if not (data.get("success") and data.get("data")):
                pending["tries"] -= 1
                if pending["tries"] <= 0:
                    del self.pending_binds[qq]
                    yield event.plain_result("账号或密码错误次数过多，绑定已取消，请回群重新发起")
                else:
                    yield event.plain_result(
                        f"账号或密码错误，还剩 {pending['tries']} 次机会，请重新回复：账号 密码"
                    )
                return

            login_user = data["data"] if isinstance(data.get("data"), dict) else {}

            # 部分版本登录响应不返回 id，用管理员权限按用户名反查确认身份
            login_id = login_user.get("id")
            if login_id is None:
                status, data = await self.client.search_user(username)
                for u in _extract_user_list(data):
                    if str(u.get("username", "")).lower() == username.lower():
                        login_id = u.get("id")
                        break
            if login_id is None:
                yield event.plain_result(
                    "绑定失败：无法确认该账号的身份（用户ID查询失败），请稍后再试"
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
                "username": login_user.get("username") or username,
                "bound_at": int(time.time()),
                "last_checkin": 0,
                "pwd": obfuscate(password, self._pwd_key()),
            })

            group_msg = ""
            bind_group = str(self._cfg("bind_group", "") or "").strip()
            if bind_group and str(login_user.get("group")) != bind_group:
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
                f"✅ 绑定成功！账号：{login_user.get('username') or username}{group_msg}\n"
                f"回到群里即可使用 /签到 /余额 等命令"
            )
        except Exception:
            logger.error(f"[newapi] 私聊绑定验证出错:\n{traceback.format_exc()}")

    # ---------- 管理员命令 ----------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("查用户", alias={"newapi用户", "newapi查用户"})
    async def admin_search(self, event: AstrMessageEvent, keyword: str = ""):
        """管理员：搜索 NewAPI 用户信息"""
        if not keyword:
            yield event.plain_result("用法：/查用户 <用户名或关键词>")
            return
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
            "管理员：/查用户 <关键词> / /强制解绑 <QQ号>\n"
            "自定义前缀（如已配置 %）：例如 %签到、%注册 等价于对应命令"
        )

    # ---------- 自定义指令前缀（绕过 LLM） ----------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_custom_command(self, event: AstrMessageEvent):
        """自定义前缀调度：如 %签到、*注册；处理完成后拦截事件，不进入 LLM"""
        try:
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

            leave_qq = str(raw.get("user_id", ""))
            rec = await self.store.get(leave_qq)
            if not rec:
                return
            uid = rec.get("user_id")
            username = rec.get("username", "")
            await self.store.remove(leave_qq)
            logger.info(f"[newapi] 群成员 {leave_qq} 退群，已解除绑定 {username}")

            if self._cfg("delete_on_leave", False):
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
