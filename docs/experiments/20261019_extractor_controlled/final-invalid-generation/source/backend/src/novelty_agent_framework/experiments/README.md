# 实验性自制数据库导入

此目录记录未来允许用户导入自制本地文献数据库的探索方向。目前仅保留设计位置，功能尚未实现，也未注册到 `RetrievalSourceRegistry`，不会被正式 Workflow 或其他生产代码导入。

此前用于验证解耦的内存模拟文献库已从正式代码剔除。后续若继续实验，应在本目录内完成数据格式、索引、安全边界和来源一致性验证；达到可运行标准前，不得以真实数据库能力对外声明。

预期的实验接口仍遵循正式检索契约：

- `QueryAdapter`
- `SearchTool`
- 可选的 `FullTextTool` 与 `MetadataTool`
- `RetrievalSource` builder

该探索与 `null_catalog` 不同：`null_catalog` 是正式测试辅助能力，只返回空结果；本目录则是尚未实现的用户自制数据库导入尝试。
