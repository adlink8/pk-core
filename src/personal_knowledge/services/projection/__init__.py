"""Cockpit UI projection 包(拆分自 services/ui_projection.py,CONCERNS.md OC-5)。

每个投影一个模块,统一暴露 ``build(db, read_service, params) -> dict``
(返回完整 envelope,与原 CockpitProjectionService.invoke 的返回值同构)。
共享基础设施(_shared)与纯函数归属各投影模块;ui_projection 退化为注册表。
"""
