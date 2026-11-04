# -*- encoding: utf-8 -*-
"""权限：表驱动分级。

owner：环境变量 GEYI_OWNER_IDS（逗号分隔）或配置 owner_ids
group_admin：群主/群管（仅群聊）
master：owner 或 OlivaDiceCore 骰主（可选集成）
"""

from __future__ import annotations

from ..models import IncomingMessage

def _get_dice_core():
    """在消息事件时再导入骰娘，避免插件加载顺序导致永久识别失败。"""
    try:
        import OlivaDiceCore  # type: ignore

        return OlivaDiceCore
    except Exception:  # noqa: BLE001
        return None


class Permissions:
    def __init__(self, config: dict, owner_ids_env: str = ''):
        self.config = config
        self._extra_owners = owner_ids_env

    def _owner_set(self) -> set[str]:
        """解析主人名单。

        **必须容忍脏配置**：`"owner_ids": null` 或直接写一个数字（`"owner_ids": 1234567890`）
        时，以前 `for x in None` / `for x in 数字` 会 TypeError，
        而这个函数**每条消息都会被调用**——于是每条消息都被入口的 except 吞掉、彻底不回话
        （2026-11-03 审查发现）。这里一律按「能解析出数字就当主人，其余忽略」处理。
        """
        ids: set[str] = set()
        raw = self.config.get('owner_ids') if isinstance(self.config, dict) else None
        if raw is None:
            raw = []
        elif isinstance(raw, (str, int)):
            raw = [raw]
        try:
            items = list(raw)
        except TypeError:
            items = []
        for item in items:
            text = str(item).strip() if item is not None else ''
            if text.isdigit():
                ids.add(text)
        env = self._extra_owners if isinstance(self._extra_owners, str) else ''
        for item in env.replace('，', ',').split(','):
            item = item.strip()
            if item.isdigit():
                ids.add(item)
        return ids

    def is_owner(self, msg: IncomingMessage) -> bool:
        return msg.sender_id in self._owner_set()

    def is_group_admin(self, msg: IncomingMessage) -> bool:
        if not msg.is_group or msg.event is None:
            return False
        try:
            role = str((msg.event.data.sender or {}).get('role', '')).lower()
            return role in ('owner', 'admin', 'sub_admin')
        except Exception:  # noqa: BLE001
            return False

    def is_master(self, msg: IncomingMessage) -> bool:
        """主人或 OlivaDiceCore 骰主均拥有全局管理权限。"""
        if self.is_owner(msg):
            return True
        dice_core = _get_dice_core()
        if dice_core is None or msg.event is None:
            return False
        try:
            user_hash = dice_core.userConfig.getUserHash(
                msg.event.data.user_id, 'user', msg.event.platform['platform']
            )
            return bool(
                dice_core.ordinaryInviteManager.isInMasterList(
                    msg.event.bot_info.hash, user_hash
                )
            )
        except Exception:  # noqa: BLE001
            return False