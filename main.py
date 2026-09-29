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
                       params=None, headers=None, timeout: int = 15):
        if not self.base_url:
            return 0, {"success": False, "message": "未配置 NewAPI 站点地址(base_url)"}
        url = f"{self.base_url}{path}"
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
        except aiohttp.ClientError as e:
            return 0, {"success": False, "message": f"网络请求失败: {e}"}
        except asyncio.TimeoutError:
            return 0, {"success": False, "message": "请求超时"}

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
            "/newapi绑定 <用户名> <密码> - 绑定账号（建议私聊）\n"
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
