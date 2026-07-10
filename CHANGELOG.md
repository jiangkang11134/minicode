# Changelog

## 0.2.0 (2026-07-10)

### 新增
- 两级代码审查系统（宽松 0.7ms + 严格 ~5s）
- Docker 沙箱测试（sandbox_test 工具）
- 运行时审查模式切换（off/loose/strict）
- 三级记忆系统（BM25 六维评分搜索）
- 成本上限控制（MINICODE_API_COST_LIMIT）
- /report 运行报告命令
- CI/CD 流水线（GitHub Actions）

### 改进
- 核心循环从 1225 行单体拆为 3 个模块
- review agent 从 5 轮 LLM 调用降为 1 轮
- test agent 零 LLM 调用直接调 sandbox_test
- 全部 103 文件零编译错误
- 全量 Ruff lint + mypy 配置

### 修复
- 权限系统非交互模式 auto-allow
- 白名单行级前缀检测
- cost_tracker Decimal/float 类型错误
- 200+ 处注释残留导致变量未定义
- Windows GBK 编码问题
