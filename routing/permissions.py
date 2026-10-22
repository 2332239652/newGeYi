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
        ids = {str(x).strip() for x in self.config.get('owner_ids', []) if str(x).strip().isdigit()}
        for item in self._extra_owners.replace('，', ',').split(','):
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