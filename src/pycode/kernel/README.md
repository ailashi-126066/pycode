# Kernel

这是共享原生事实和少量基础能力，不负责组装业务功能。

- `domain/`：Message、ToolSpec、SessionEntry、不可变模型选择/投影事实、通用值/哈希/脱敏。
- `ports/`：历史、事件、时钟和原生工具接口。
- `services/`：仅依赖端口的事件投递。
- `adapters/`：文件锁、受控文件 I/O、系统时钟、内存事件 journal。

根 `__init__.py` 只公开原生契约与纯策略；I/O 实现必须显式从 adapters 使用，纯层不能依赖它。
Skill/MCP 规则、模型展示、消息计费和压缩边界属于对应功能，禁止重新塞回共享目录。
`work_graph.py` 是任务板（Board/Step、转移表、validate/apply/derived），`state_machine.py` 是通用状态机。
