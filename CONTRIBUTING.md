# 贡献指南

请先说明可复现问题、预期行为和修改范围，优先小范围改进，保持 GPT 主导、Claude 按需及原有预算/控制机制。

从自己的分支提交 Pull Request。运行 `python -m unittest discover -s tests -v` 和 `python scripts/check-publication.py`；注明实际通过、跳过和未验证内容，不将 Mock 测试写成真实模型验收。

默认不运行付费 CLI。真实验收须另行授权模型、次数、上下文范围和预算；不能修改供应商认证、放宽 sandbox 或重发未知已付费请求来让测试通过。

测试数据应完全合成。提交前检查差异，不提交认证文件、API Key、个人路径、账单、日志、数据库或临时工作区。复现材料也要脱敏。敏感问题按 SECURITY.md 处理。
