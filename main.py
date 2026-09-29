import asyncio
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


# ============================================================
# NewAPI 客户端（超级管理员令牌）
# ============================================================
class NewAPIClient:
    def __init__(self, base_url: str, admin_token: str, admin_user_id: int = 1):
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.admin_user_id = admin_user_id

    def _admin_headers(self):
        # 新版 new-api 要求 New-Api-User 请求头为令牌所属用户的 ID
        return {
            "Authorization": f"Bearer {self.admin_token}",
            "New-Api-User": str(self.admin_user_id),
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, path: str, *, json_body=None,
                       params=None, headers=None, timeout: int = 30, retries: int = 1):
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
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = {"success": False, "message": f"HTTP {resp.status} 非JSON响应"}
                        return resp.status, data
            except asyncio.TimeoutError:
                last_err = "请求超时（站点 30 秒内未响应）"
            except aiohttp.ClientError as e:
                last_err = f"网络请求失败: {e}"
            if attempt < retries:
                await asyncio.sleep(1)  # 网络类失败自动重试一次
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
        )
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
        rate = float(self._cfg("exchange_rate", 7.2) or 7.2)
        usd = q / per
        return f"${usd:.4f}（≈ ¥{usd * rate:.2f}）"

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
    @filter.command("newapi绑定", alias={"绑定账号", "绑定newapi"})
    async def bind(self, event: AstrMessageEvent, username: str = "", password: str = ""):
        """绑定 NewAPI 账号：/newapi绑定 用户名 密码（强烈建议私聊使用）"""
        qq = str(event.get_sender_id()).strip()
        if not username:
            yield event.plain_result(
                "用法：/newapi绑定 <用户名> <密码>\n"
                "⚠️ 密码验证建议私聊使用，群聊中发送会被群成员看到！"
            )
            return

        if not self._cfg("allow_group_bind", False) and self._is_group(event):
            yield event.plain_result("出于安全考虑，请私聊我进行账号绑定（加我为好友后私聊发送）")
            return

        if await self.store.get(qq):
            rec = await self.store.get(qq)
            yield event.plain_result(
                f"你已绑定账号：{rec.get('username')}\n如需更换，请先使用 /newapi解绑"
            )
            return

        verify = bool(self._cfg("bind_verify_password", True))
        uid = None
        if verify:
            if not password:
                yield event.plain_result("请提供密码：/newapi绑定 <用户名> <密码>（建议私聊）")
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
            users = data.get("data") or [] if data.get("success") else []
            exact = [u for u in users if str(u.get("username", "")).lower() == username.lower()]
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
        })
        yield event.plain_result(f"✅ 绑定成功！{username}，发送 /余额 查询额度，/签到 每日打卡")

    @filter.command("newapi解绑", alias={"解绑账号"})
    async def unbind(self, event: AstrMessageEvent, target_qq: str = ""):
        """解绑：/newapi解绑（自己）或 /newapi解绑 <QQ号>（管理员）"""
        qq = str(event.get_sender_id()).strip()
        if target_qq:
            yield event.plain_result(f"解绑他人需管理员权限，请使用管理员命令或联系管理员")
            return
        if await self.store.remove(qq):
            yield event.plain_result("✅ 已解绑 NewAPI 账号")
        else:
            yield event.plain_result("你尚未绑定 NewAPI 账号")

    # ---------- 签到 ----------
    @filter.command("签到", alias={"打卡"})
    async def checkin(self, event: AstrMessageEvent):
        """每日签到，随机额度"""
        if not self._cfg("checkin_enabled", True):
            yield event.plain_result("签到功能未开启")
            return
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("你还没有绑定 NewAPI 账号，请先私聊我使用 /newapi绑定")
            return

        cooldown_h = float(self._cfg("checkin_cooldown_hours", 24) or 24)
        last = int(rec.get("last_checkin") or 0)
        now = time.time()
        remain = last + cooldown_h * 3600 - now
        if remain > 0:
            h = int(remain // 3600)
            m = int((remain % 3600) // 60)
            yield event.plain_result(f"今天已经签到过啦～ 距离下次签到还有 {h} 小时 {m} 分钟")
            return

        status, data = await self.client.get_user(rec["user_id"])
        if not (data.get("success") and data.get("data")):
            yield event.plain_result(f"签到失败：查询账号信息出错（{data.get('message', '未知错误')}）")
            return
        user = data["data"]

        lo = float(self._cfg("checkin_min_usd", 0.1) or 0.1)
        hi = float(self._cfg("checkin_max_usd", 0.5) or 0.5)
        if lo > hi:
            lo, hi = hi, lo
        per = int(self._cfg("quota_per_unit", 500000) or 500000)
        amount_usd = random.uniform(lo, hi)
        amount_quota = int(round(amount_usd * per))

        user["quota"] = int(user.get("quota") or 0) + amount_quota
        status, data = await self.client.update_user(user)
        if not data.get("success"):
            yield event.plain_result(f"签到失败：加额度出错（{data.get('message', '未知错误')}）")
            return

        async with self.store.lock:
            rec["last_checkin"] = int(now)
            self.store.data["bindings"][qq] = rec
            self.store._save_sync()

        yield event.plain_result(
            f"🎉 签到成功！获得额度：${amount_usd:.4f}\n"
            f"当前余额：{self._fmt_quota(user['quota'])}"
        )

    # ---------- 余额查询 ----------
    @filter.command("余额", alias={"查询余额", "我的额度"})
    async def balance(self, event: AstrMessageEvent):
        """查询绑定的 NewAPI 账号余额"""
        qq = str(event.get_sender_id()).strip()
        rec = await self.store.get(qq)
        if not rec:
            yield event.plain_result("你还没有绑定 NewAPI 账号，请先私聊我使用 /newapi绑定")
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
        if not self._cfg("register_enabled", True):
            yield event.plain_result("注册功能未开启")
            return
        if not self._is_group(event):
            yield event.plain_result("请在群聊中使用 /注册")
            return
        qq = str(event.get_sender_id()).strip()
        if await self.store.get(qq):
            rec = await self.store.get(qq)
            yield event.plain_result(
                f"你已拥有账号：{rec.get('username')}，无需重复注册（如需更换请先 /newapi解绑）"
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
        for u in (data.get("data") or []):
            if str(u.get("username")) == username:
                uid, user = u.get("id"), dict(u)
                break
        if uid is None:
            yield event.plain_result("❌ 注册失败：未能查询到用户信息，请联系管理员")
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
        if not user_id or not user_id.isdigit():
            yield event.plain_result("用法：/绑定 <NewAPI用户ID数字>，例如 /绑定 1")
            return
        qq = str(event.get_sender_id()).strip()
        if await self.store.get(qq):
            yield event.plain_result("你已绑定过账号，如需更换请先 /newapi解绑")
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

            login_user = data["data"]
            if str(login_user.get("id")) != str(pending["user_id"]):
                yield event.plain_result(
                    f"该账号（ID:{login_user.get('id')}）与你要绑定的 ID（{pending['user_id']}）不一致，绑定取消"
                )
                del self.pending_binds[qq]
                return

            # 验证通过：写入绑定并更换分组
            user_id = int(pending["user_id"])
            del self.pending_binds[qq]
            await self.store.set(qq, {
                "user_id": user_id,
                "username": login_user.get("username", username),
                "bound_at": int(time.time()),
                "last_checkin": 0,
            })

            group_msg = ""
            bind_group = str(self._cfg("bind_group", "") or "").strip()
            if bind_group and str(login_user.get("group")) != bind_group:
                status, data = await self.client.get_user(user_id)
                if data.get("success") and data.get("data"):
                    u = data["data"]
                    u["group"] = bind_group
                    status, data = await self.client.update_user(u)
                    if data.get("success"):
                        group_msg = f"，分组已更换为 {bind_group}"
                    else:
                        group_msg = f"（⚠️ 更换分组失败：{data.get('message')}）"

            yield event.plain_result(
                f"✅ 绑定成功！账号：{login_user.get('username', username)}{group_msg}\n"
                f"回到群里即可使用 /签到 /余额 等命令"
            )
        except Exception:
            logger.error(f"[newapi] 私聊绑定验证出错:\n{traceback.format_exc()}")

    # ---------- 管理员命令 ----------
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("newapi用户", alias={"newapi查用户"})
    async def admin_search(self, event: AstrMessageEvent, keyword: str = ""):
        """管理员：搜索 NewAPI 用户信息"""
        if not keyword:
            yield event.plain_result("用法：/newapi用户 <用户名或关键词>")
            return
        status, data = await self.client.search_user(keyword)
        users = data.get("data") or [] if data.get("success") else []
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
    @filter.command("newapi强制解绑")
    async def admin_unbind(self, event: AstrMessageEvent, qq: str = ""):
        """管理员：强制解除某个 QQ 的绑定"""
        if not qq:
            yield event.plain_result("用法：/newapi强制解绑 <QQ号>")
            return
        if await self.store.remove(qq):
            yield event.plain_result(f"✅ 已解除 QQ({qq}) 的绑定")
        else:
            yield event.plain_result(f"QQ({qq}) 没有绑定记录")

    @filter.command("newapi帮助", alias={"newapi菜单"})
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "📖 NewAPI 插件命令：\n"
            "/注册 - 自助注册账号（密码私聊发送）\n"
            "/绑定 <ID> - 绑定指定 NewAPI 账号（私聊验证账号密码）并更换分组\n"
            "/newapi绑定 <用户名> <密码> - 验证绑定（建议私聊）\n"
            "/newapi解绑 - 解除绑定\n"
            "/签到 - 每日签到领额度\n"
            "/余额 - 查询账号余额\n"
            "管理员：/newapi用户 <关键词> / /newapi强制解绑 <QQ号>"
        )

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
