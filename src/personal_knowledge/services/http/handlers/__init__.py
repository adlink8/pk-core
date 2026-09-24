"""按域拆分的 HTTP 端点处理器包(api_server 拆分第三阶段,OC-1)。

每个模块提供 handler(handler_instance, ctx) 风格的函数,ctx 含 url/path/qs/body。
函数内部经 `import personal_knowledge.services.api_server as api_server` 延迟解析
依赖符号(backend / contract 函数 / 投影服务类),保证测试对 api_server 命名空间的
monkeypatch(如 api_server.backend、api_server.orchestration_rest_contract、
api_server.TopicProjectionService)照常生效,也避免与 api_server 的循环导入。
"""
