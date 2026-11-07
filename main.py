# -*- encoding: utf-8 -*-
"""OlivOS 事件入口：只做转发，业务全部在 runtime。

命名空间：newGeYi
消息模式：olivos_string（OP 码）
"""

import logging

from .runtime.app import Runtime

_logger = logging.getLogger('newGeYi.main')

# 插件加载时就要把 Runtime 建起来（init 之后才能拿到 Proc），但**不能让异常冒出去**：
# 以前这里是裸 `_runtime = Runtime()`，配置/数据库/依赖任何一处抛错，OlivOS 那边只会看到
# 「插件加载失败」，日志里连一行原因都没有。现在兜住并留下完整栈，同时给一个空壳，
# 让事件回调不至于 AttributeError 连环炸。
try:
    _runtime = Runtime()
    _load_error = ''
except Exception as _exc:  # noqa: BLE001 - 加载期任何异常都不能让插件静默消失
    _logger.exception('插件初始化失败（配置/数据库/依赖）')
    _load_error = '%s: %s' % (type(_exc).__name__, _exc)

    class _BrokenRuntime:  # pragma: no cover - 只在初始化失败时存在
        config: dict = {}

        def __getattr__(self, name):
            def _noop(*args, **kwargs):
                _logger.error('插件初始化失败（%s），忽略事件 %s', _load_error, name)
                return None
            return _noop

    _runtime = _BrokenRuntime()


_panel = None
_panel_server = None


def _get_panel():
    """复用当前运行时；页面不另建机器人、连接或数据库。"""
    global _panel
    if _panel is None:
        from .panel import Panel

        _panel = Panel(_runtime)
    return _panel


def _start_panel_server():
    """核心没有 OlivOS WebUI 时，用本机 127.0.0.1 端口把同一套页面挂起来。

    0.11.x 的核心不认识 `webui_config`，侧栏永远不会有入口（详见 panel_server.py
    开头的实测清单）。有 WebUI 的核心上可以 `panel_local_enabled=false` 关掉它。
    """
    global _panel_server
    if _panel_server is not None or _load_error:
        return
    try:
        from .panel_server import LocalPanelServer

        _panel_server = LocalPanelServer(_get_panel())
        _panel_server.start()
        # 命令「面板地址」要能拿到同一个实例
        try:
            _runtime.panel_server = _panel_server
        except Exception:  # noqa: BLE001 - 空壳 Runtime 上赋值失败不算问题
            pass
    except Exception:  # noqa: BLE001 - 面板起不来绝不能影响机器人
        _logger.exception('本地面板启动失败，机器人继续运行')
        _panel_server = None


class Event(object):
    @staticmethod
    def init(plugin_event, Proc):
        _runtime.init(Proc)
        if not _load_error:
            try:
                _get_panel()
            except Exception:
                _logger.exception('调试页面初始化失败，机器人继续运行')

    @staticmethod
    def init_after(plugin_event, Proc):
        _runtime.init_after(Proc)
        _start_panel_server()

    @staticmethod
    def group_message(plugin_event, Proc):
        _runtime.handle_message(plugin_event, Proc)

    @staticmethod
    def private_message(plugin_event, Proc):
        _runtime.handle_message(plugin_event, Proc)

    @staticmethod
    def poke(plugin_event, Proc):
        _runtime.handle_poke(plugin_event, Proc)

    @staticmethod
    def menu(plugin_event, Proc):
        data = getattr(plugin_event, 'data', None)
        if (
            getattr(data, 'namespace', '') == 'newGeYi'
            and isinstance(getattr(data, 'webui', None), dict)
        ):
            try:
                if _load_error:
                    raise RuntimeError('runtime unavailable')
                _get_panel().handle_event(plugin_event, Proc)
            except Exception:
                _logger.error('调试页面处理失败，请检查插件加载状态')
                request_id = data.webui.get('request_id')
                if isinstance(request_id, str) and 0 < len(request_id) <= 128:
                    try:
                        plugin_event.send('webui', request_id, {
                            'ok': False,
                            'error': '调试页面暂未就绪，请检查 OlivOS 日志并重载插件。',
                            'code': 'panel_unavailable',
                        })
                    except Exception:
                        _logger.error('调试页面回包失败')
            return
        _runtime.handle_menu(plugin_event, Proc)

    @staticmethod
    def save(plugin_event, Proc):
        global _panel, _panel_server
        try:
            _runtime.save(Proc)
        finally:
            if _panel_server is not None:
                try:
                    _panel_server.stop()
                except Exception:
                    _logger.error('本地面板关闭失败')
                _panel_server = None
                try:
                    _runtime.panel_server = None
                except Exception:  # noqa: BLE001
                    pass
            if _panel is not None:
                try:
                    _panel.close()
                except Exception:
                    _logger.error('调试页面日志监听清理失败')
                _panel = None
