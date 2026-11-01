# 源码来源与复用记录

## 固定来源

- Pi：`earendil-works/pi`。本次对齐（11.1）的出处是 HEAD `2c8d1fe`，下表文件头写
  `# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/<路径>`。更早移植的文件仍写最初的提交
  `42a3497d03ad17e308a2299fa824727894f2c0ec`（`packages/agent` 与 `packages/coding-agent` 版本 `1.1.0`）。
- OpenCode：`anomalyco/opencode`，提交 `055d95bb7e278c94baf06235a52cac79dd13ba67`（`packages/opencode` `1.18.35`）。
  工具结果驱逐与 Git tree 快照的思路来自这里。
- MewCode：只读本地目录 `../3_mewcode-python`，版本 `0.2.0`，没有 Git 基线。用户于 2026-10-10 说明这是其
  自行编写的代码，授权直接复用和改造；这一授权不扩大第三方依赖许可证。文件头用本地 SHA-256 标明出处，
  清单在本文件末尾。
- Exercism Python（评测题库，`Aider-AI/polyglot-benchmark@7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f`）。

## 许可证

Pi、OpenCode、Exercism 都是 MIT，完整声明在 `licenses/pi-MIT.txt`、`licenses/opencode-MIT.txt`、
`licenses/exercism-MIT.txt`，随 wheel 和 sdist 发布。MewCode 的作者授权在 `licenses/local-author-permission.txt`，
其中保留了原作者的署名（“公众号@小林coding / xiaolincoding.com / xiaolinnote.com / jianli.xiaolinnote.com”）；
源码文件头不再重复这段署名。

## 照 Pi HEAD 2c8d1fe 实现的文件

| pycode 文件 | Pi 文件 |
|---|---|
| `agent/domain/run_state.py` | `packages/agent/src/agent-loop.ts` |
| `context/adapters/summary.py` | `packages/coding-agent/src/core/compaction/compaction.ts` |
| `context/adapters/summary.py` | `packages/coding-agent/src/core/compaction/utils.ts` |
| `context/domain/compaction.py` | `packages/coding-agent/src/core/compaction/compaction.ts` |
| `context/domain/context_meter.py` | `packages/coding-agent/src/core/compaction/compaction.ts` |
| `context/domain/file_ops.py` | `packages/coding-agent/src/core/compaction/utils.ts` |
| `context/domain/summary_source.py` | `packages/coding-agent/src/core/compaction/compaction.ts` |
| `context/domain/tasks.py` | `packages/coding-agent/examples/extensions/todo.ts` |
| `context/services/context_projector.py` | `packages/coding-agent/src/core/session-manager.ts` |
| `kernel/domain/events.py` | `packages/agent/src/types.ts` |
| `kernel/domain/history.py` | `packages/coding-agent/src/core/session-manager.ts` |
| `kernel/domain/messages.py` | `packages/ai/src/types.ts` |
| `kernel/domain/messages.py` | `packages/coding-agent/src/core/messages.ts` |
| `kernel/services/events.py` | `packages/agent/src/agent.ts` |
| `models/adapters/messages.py` | `packages/ai/src/api/transform-messages.ts` |
| `plans/adapters/tools/task_board.py` | `packages/coding-agent/examples/extensions/todo.ts` |
| `tools/adapters/builtin/edit.py` | `packages/coding-agent/src/core/tools/edit.ts` |
| `tools/adapters/builtin/edit.py` | `packages/coding-agent/src/core/tools/edit-diff.ts` |
| `tools/adapters/builtin/find.py` | `packages/coding-agent/src/core/tools/find.ts` |
| `tools/adapters/builtin/grep.py` | `packages/coding-agent/src/core/tools/grep.ts` |
| `tools/adapters/builtin/ls.py` | `packages/coding-agent/src/core/tools/ls.ts` |
| `tools/adapters/builtin/path_utils.py` | `packages/coding-agent/src/core/tools/path-utils.ts` |
| `tools/adapters/builtin/read.py` | `packages/coding-agent/src/core/tools/read.ts` |
| `tools/adapters/builtin/write.py` | `packages/coding-agent/src/core/tools/write.ts` |
| `tools/adapters/discovery.py` | `packages/coding-agent/src/core/system-prompt.ts` |
| `tools/adapters/file_mutation_queue.py` | `packages/coding-agent/src/core/tools/file-mutation-queue.ts` |
| `tools/adapters/output_store.py` | `packages/coding-agent/src/core/tools/output-accumulator.ts` |
| `tools/adapters/output_store.py` | `packages/coding-agent/src/core/tools/bash.ts` |
| `tools/adapters/schema.py` | `packages/ai/src/utils/validation.ts` |

各概念的对照和用到的框架机制见 [pi-map.md](pi-map.md)。

## 其他复用边界

- Agent 循环：Deep Agents 0.7.32 的 `create_deep_agent` 是唯一的图（`agent/adapters/agent.py`），Pi 的循环钩子由
  middleware 实现，不另写调度循环。
- 会话树：Pi session-manager 的树结构存进 LangGraph `SqliteStore`（`session/adapters/storage/session.py`）。
- 后台摘要：MewCode 的两阶段后台摘要（何时压缩）加上 Pi 的摘要写法（怎么压缩）。
- Skills：目录、可信来源和 GitHub 安装来自 MewCode，加载与校验照 Pi；Deep Agents `SkillsMiddleware` 负责披露。
- MCP：LangChain `MCPAdapter` 是唯一的连接与协议实现。
- Hooks：MewCode 的条件、模板和 command/prompt 动作；条件在进程内求值。
- 记忆：LangMem 的 Store Manager 和 manage 工具，底层 BaseStore 是一条一个 Markdown 文件。
- Subagent：照 Claude Code 的行为自己写管理层（`subagents/`），子 agent 内部仍用同一个 Deep Agents 图。
- TUI / CLI：MewCode 的 Textual 设计；`ui/adapters/commands.py`、`completion.py` 参考
  `code-python/mewcode/commands/{registry,parser,completion}.py`。
- 评测（`evals/`，独立于 pycode 包）：本项目自己的流水线。

## 移植规则

每个直接复制或翻译的文件在文件头写出处（Pi 写 GitHub 链接和提交，MewCode 写本地路径和 SHA-256）以及改造范围。
新源码不包含旧产品名或旧配置路径；本文件、许可证和出处注释可以出现 MewCode 名称。

### Provider 的最小适配

LangChain 负责 HTTP、SSE、消息转换和工具绑定。`models/adapters/observation.py` 只观察 SDK 已解码的对象，
补回流式归一化后丢失的用量（DeepSeek cache miss、Anthropic message_start 输入用量）。
`ManagedChatAnthropic` 是 `ChatAnthropic` 的子类，只重载两个惰性 client 访问器以注入有生命周期所有者的
client。升级 LangChain 集成时要重跑四种 HTTP 协议测试和 usage 回归。

## 本地来源 SHA-256 清单

生成前没有修改参考项目。清单只包含 package、tests、docs 及项目文本文件，
不读取或迁移 `.mewcode`、`.venv`、缓存、密钥或运行数据。后续移植必须核对清单中的哈希。

<!-- LOCAL_SOURCE_MANIFEST -->

| 来源路径 | 字节数 | SHA-256 |
| --- | ---: | --- |
| `docs/migration-audit.md` | 86385 | `37835995b56a75de17c56eeea171ab3f50adb63104874d5fc6f7938b5d61dd05` |
| `docs/migration-baseline-trace.json` | 73124 | `f0c16eb6f992dad9626046dd1a2f7d8f5e307c7b798a8910f21b088cc882cb47` |
| `docs/migration-sdk-retry-trace.json` | 5591 | `7da68113da0e452d6f42068278ba3f870ee81f126e569b8d4b9eab1b1bd7add9` |
| `docs/skill-install-online-result.json` | 589 | `fb266daad80b03a34f48e30a400ab2e08693b915c479b370c07d44f77a004183` |
| `mewcode/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/__main__.py` | 14830 | `1d622ecac5a5eb83be9297ee41716915f1ea849cda595f18d819a26272244719` |
| `mewcode/agent.py` | 36409 | `11a7ede05ee6be2f740b0053f88578a9a02ebec92e65b3e49cd80e7c14ef3947` |
| `mewcode/agents/__init__.py` | 929 | `3e6260fe06a9cc1593522431bcf58b6020b5a6c7de910beabbbecbadb2d8e968` |
| `mewcode/agents/builtins/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/agents/builtins/explore.md` | 749 | `7280798d56b7dc8f76aef6faf1476cafeff8d367670510ae589863a1b7c95a6e` |
| `mewcode/agents/builtins/general-purpose.md` | 589 | `a2e77e9eb91ab3c792c15b4f5c8d355bf7641e9fe33f1f9bbaf87d544df3bd56` |
| `mewcode/agents/builtins/plan.md` | 776 | `f6f7f8e5f5353479bff9325d7b48d1b88236d8941a9db2d037fb1565c692ef02` |
| `mewcode/agents/builtins/verification.md` | 1243 | `b55b15d09ad8f0dd569fa80fdec1de093013f4e09ab0133d7af0fa3fcb0f53bf` |
| `mewcode/agents/fork.py` | 1767 | `458f347e3d27f09182a5c54ab17a2337c718848725cbaa6b5e6a15e8016aa1bd` |
| `mewcode/agents/loader.py` | 4936 | `05ec8dbaf361ffbf75c54b6912a7f0f070b1dd476e3a0e2a753989195486aaf8` |
| `mewcode/agents/notification.py` | 1583 | `228694016bc8d2a81f202dc0f9db7b67c0b7dbd80d5a3d7ce8b404354ca890da` |
| `mewcode/agents/parser.py` | 3846 | `e4cfec318e0165062338ea4813588b38c5492cf9b2b5b7a32a615ed6e04a5e3d` |
| `mewcode/agents/task_manager.py` | 6442 | `85120b26fb2412c349a7f67521f6a5262d429a1e3af61bd7959c1f9abfdd2e91` |
| `mewcode/agents/tool_filter.py` | 7376 | `85ae8fc5c01b5f4a122a3c56bdd5a57ab6bae7d2cf4a8e3d53c34b5de9b34f6c` |
| `mewcode/agents/trace.py` | 2568 | `01197d87cb4d78072f71374630dfb92eddc64abbe115a1ffb89a8f820b786d20` |
| `mewcode/app.py` | 82723 | `2680a87b05640a668323ae5712bed84eec2bc424cb47ec70e3d876450ec81696` |
| `mewcode/askuser_dialog.py` | 10106 | `aa6315c63c290b3e5afccb9d3319016c3830d9e56e7fc010543dd300bd8d96cc` |
| `mewcode/cache.py` | 853 | `dfeb38d59ab568bb05580bb1621559fd6cb89d189715877776f326a618d4b17b` |
| `mewcode/client.py` | 25837 | `ac88d28c9b351ebfec0ededb763e9857755341bccfd1be73282bd765a6206e5a` |
| `mewcode/commands/__init__.py` | 535 | `0cb8a3e01f815f03a9c54786ef12c80a0ca149cd07072bf649368f855eb64026` |
| `mewcode/commands/completion.py` | 2350 | `1c5c4c40a5a10c33624d6f09186965a6d16e859bdaadeb2514a94256cb175fc7` |
| `mewcode/commands/handlers/__init__.py` | 1428 | `663da7e4d533da63641a07748107916145bac9517c6543266365c4e0aafc9294` |
| `mewcode/commands/handlers/clear.py` | 1102 | `b3df8a32bf720dc1ad1bb649c532cf87661db70d1917ccf1a04e7a51812915e7` |
| `mewcode/commands/handlers/compact.py` | 1617 | `4e648c708ec0d748f507caaac7bf77ee641204e02dde4dc77a4bde4383d2b354` |
| `mewcode/commands/handlers/help.py` | 1665 | `636ee0585df09c7fa24f76ed419b39ffefa3dcd37d7ca6b7b3f80c241d78481c` |
| `mewcode/commands/handlers/mcp.py` | 1419 | `1cb6fff1af7a3aa3ffc680f75a8e4f24aff3857d86e2636592499b606a5e7ecc` |
| `mewcode/commands/handlers/memory.py` | 1327 | `3269e54a8e50218e474d98873f8e3c3d938fac37df29761f7ed693b6a3bae6df` |
| `mewcode/commands/handlers/permission.py` | 4298 | `41bf2322220499fe310b0e6a2999e8ad6be1aebe8e58d2c12d8cc92d10b7f4ef` |
| `mewcode/commands/handlers/plan.py` | 699 | `63ddca398a8ea41642196993d9d64a3ff7e4b96e979b931aeaf5df139f6b3fb4` |
| `mewcode/commands/handlers/review.py` | 816 | `704f2dd5163c276f773580aa424f39b8832fbb7b625d9630d8e2a0fcd5b01894` |
| `mewcode/commands/handlers/rewind.py` | 3540 | `797939b8372c9dbaa7ace4f345ea99121acc4a5273e5ba8aba396a58a960fe5a` |
| `mewcode/commands/handlers/sandbox.py` | 1239 | `c6a10d6f00e73b2122bd37b7496543e81dc674ee36414c06a3dd6b440a44155b` |
| `mewcode/commands/handlers/session.py` | 6748 | `8544b3377865bf6cfe9e6fdff46d4f173d8eefa6b7abd85662c02f6f56113d33` |
| `mewcode/commands/handlers/skill.py` | 3655 | `245006b5eb296fadead8d29984a53e85af8f8edc19160226e8abc4846e162718` |
| `mewcode/commands/handlers/skill_register.py` | 3856 | `fa17e439e1e7ad2a6f72deade10199945320ff5ce8a28786299169d28776ad1f` |
| `mewcode/commands/handlers/status.py` | 1779 | `5dc4265386cd373433ea2da2106089160a7c48635b089016c25c2798715bef89` |
| `mewcode/commands/handlers/subagent.py` | 1250 | `5016032c0e1cd8abac2ffa1769ca7577149654343e51210e46832259e586455b` |
| `mewcode/commands/handlers/tasks.py` | 3438 | `7ff315f0c635654e3b60de2785ca1674ac2b7783ca39659119024c20451f0327` |
| `mewcode/commands/handlers/trace.py` | 2927 | `2f41a5db79787bad7fb42cf9a37a9bbdeb6628f9bbb8276366a794c324322108` |
| `mewcode/commands/handlers/worktree.py` | 6226 | `6bc2be21a6c2781431f766eecf797d3df04a3e6a0d608c4e4b4944f42c8342fe` |
| `mewcode/commands/parser.py` | 1370 | `a603158122bef60ed9e3fb155f313e9c6cef94b3ebfff282521e922b11d5f6aa` |
| `mewcode/commands/registry.py` | 3137 | `fbfe1a4ae8a29b77b2cb82a138f4666b318d370f05d74a02e39f233220a9e453` |
| `mewcode/config.py` | 8918 | `3c5d66daafef264996b9e2392f7c5db7de3c3d47ab42eab229bb4957789b56e1` |
| `mewcode/context/__init__.py` | 1386 | `d5ee5cee20b0b499690db20ad0463780603caa4d0e1cd5850683307a73bcf69d` |
| `mewcode/context/manager.py` | 29030 | `227dd672ef3a5dd6893a5d63e5b6c5abed196c017877160c43fadee261f0ab75` |
| `mewcode/context/summary_cache.py` | 13886 | `e291f27c5264240e6a535b4ec997e0958cec665f5be1e36f0f9f5f7b08621db0` |
| `mewcode/conversation.py` | 7828 | `a764400d3bba7321e3555ce9cca7e9c3d470dcb2ce54c2a48301e2269ed1af04` |
| `mewcode/conversation_pairing.py` | 1946 | `533aa1f53b8241e3ac6db13c11c87e1d58856ee60c73406371d5ff761e0cb0cb` |
| `mewcode/driver.py` | 1463 | `405a42a35b3bb8163456053168660683129f1b9c811e6945a40668c7c4d5c651` |
| `mewcode/filehistory/__init__.py` | 249 | `9c51aa48decf8958cb4cc9058e651f279093ca9ec458592aff9e6d9c12cf209c` |
| `mewcode/filehistory/history.py` | 13850 | `d1d93ccd6d37f05c7cd8873c2d4da6475aa78f40708be88414ca0dc27de35896` |
| `mewcode/hooks/__init__.py` | 818 | `eeadeda2a7005fef1b6736466449cec121f75d949dfd014de90fa46a873641c9` |
| `mewcode/hooks/conditions.py` | 2808 | `d11485198df8f4ebf76a009c4b13eaf51883ae0a1bbc14f734779d684817f870` |
| `mewcode/hooks/engine.py` | 4294 | `91e9d3162556ea54e9c1d211feb3ecf4126a302f50f7eb3e5f584194be9e9b29` |
| `mewcode/hooks/events.py` | 868 | `80ddc57659d63b4a6edca6254f1e95d6e0b6e06a6101749e33368503cd6ee4e8` |
| `mewcode/hooks/executors.py` | 3234 | `2b16275a172f0c998ee24da7b693c3aa0bb248e374bff66e7b027c15672806f3` |
| `mewcode/hooks/loader.py` | 3963 | `f70cd9c61d908f98f7666171aae1789f806cdd5bc04ea518f2e6219c01d38914` |
| `mewcode/hooks/models.py` | 2369 | `0a07d8f977875f523398ca5972eb11c1be0a074df7045711e758f92bfb416e19` |
| `mewcode/mcp/__init__.py` | 255 | `8fe9751b468d5a0f52822766dde9f09ca99a1e11d8db60a76c00233a59e3dc41` |
| `mewcode/mcp/client.py` | 3935 | `4d045fe98a5a39e719accded4eb117b88aa7d2b46dc40246653249b549af4076` |
| `mewcode/mcp/loading_strategy.py` | 2290 | `d34db2524575d47b93c7c68fe8b93340bc84d4cc23a21477e86018059763092b` |
| `mewcode/mcp/manager.py` | 2684 | `c43972ec32c050d47ab665e17274795d20fd382af4f439ac8db352e0aa61cd0b` |
| `mewcode/mcp/tool_wrapper.py` | 4395 | `aeb2cfda21b0b9c03a490762ac64ec3208ed1cea4fafb637220e820aec55840a` |
| `mewcode/memory/__init__.py` | 1004 | `d1e5484b48011e737c175c0a2b374d604ecb8f7f4ffe8e4fb25cd99327ed4c48` |
| `mewcode/memory/auto_memory.py` | 9181 | `cd4e739d1ed9d6f20cd85256df89f7767fc36545653c3bbce763eed9d96fcec8` |
| `mewcode/memory/instructions.py` | 4181 | `28c871f9f00a466058bf4f43145e3ced9903bfcdaad9d5cb0d4096a1d89425c0` |
| `mewcode/memory/recall.py` | 12151 | `4830e291e0aed2ffadfd4ec7d54198975044ae86041d62eee0ee996b12fdb7bd` |
| `mewcode/memory/session.py` | 22770 | `718af12df910f1fe72f1224dd89a5a1be84a3260c104ea7800ca58e66f8c633c` |
| `mewcode/permission_dialog.py` | 2956 | `87f1c767a00123f05660c08c2c73749b9530a0d5f6040f29ff8b3bab27e2de57` |
| `mewcode/permissions/__init__.py` | 745 | `4ba4fce645a7b225cc349bb15c785282b238d2f7a5f0e6b21d2c0df67dd6ed0b` |
| `mewcode/permissions/checker.py` | 4182 | `b5aaf31e6d66c26dfe3a1c2a09600698a35406a4e48eeec9ec5cda37ec4ab730` |
| `mewcode/permissions/dangerous.py` | 2425 | `6de120e749f1aaa3ea12fe657891c0b2bf944d26e5de33bdefecbd0bf6443d95` |
| `mewcode/permissions/modes.py` | 1216 | `f97ddd6681b2edc2f6cd8641f56d00789ad5aa697d786962bd8fa2b116fa7ce1` |
| `mewcode/permissions/rules.py` | 3263 | `5a52754c380571c6b54c39813991c051bf1f0a142ec5c7af793572af7cb90ad5` |
| `mewcode/permissions/sandbox.py` | 1747 | `c88bdf021bf0b093079d871e4c8fa33de351d9f4a7e23000c4ee6344dd957d53` |
| `mewcode/plan_dialog.py` | 3860 | `3b866fd6ee3b7db1a9b9810cc1a8e6f36656f5070a223be15cbf7b9b967267db` |
| `mewcode/processes.py` | 1287 | `a8049900efb1977eb4866bd871d3bcd101abaffe35d70452c75da69da45096b4` |
| `mewcode/prompts.py` | 17187 | `020776cd8a5de344c89c36189ce6f9800c166c2c3a16348a8901a62f46133283` |
| `mewcode/runtime/__init__.py` | 74 | `ef3873fa1fe2b3b6ab186c313bb7e838ab911e8bb0ee8c96e5296cd02f037cd5` |
| `mewcode/runtime/agent.py` | 35461 | `4c245699e2d74f670a05767d1ce7d4f354dfad6a83854f46ced4c618d59ecb9a` |
| `mewcode/runtime/lifecycle.py` | 2422 | `5451570bc262e6b034a1ca951db1599d08f666519b3fc153fbdc59793708bec8` |
| `mewcode/runtime/messages.py` | 3114 | `0a71550e40110cd86e06a672543e51eaca7664c8e4caa4584b0a9a22f0cc5fa6` |
| `mewcode/runtime/model.py` | 2888 | `213af177b743e6d3d76df2aa938c7f00eee42d8a58d614a3ada0859ce8e6c7a0` |
| `mewcode/runtime/output.py` | 3859 | `1726aae4ccd94dc643956234638c60c2e148ad1215afc87bea6ddd40168bee71` |
| `mewcode/runtime/session.py` | 5782 | `47885c12029ed34c8ecfd0abe17ab0c695261d1fd199bdc579cf4dee05773a12` |
| `mewcode/runtime/storage.py` | 8666 | `f14f79be5fa64e53d4301b47a921dd7efc2d95d96f174d5c59be516365220185` |
| `mewcode/runtime/tools.py` | 1131 | `51d494b22d8b8bb00e4a4d2d356805158a0db3e3749f7c77c4d09284776fd12b` |
| `mewcode/sandbox/__init__.py` | 2782 | `5f17b63c2581469920c3000149222a7a1effd2a50f67b5e4de8c9abdaafd0164` |
| `mewcode/sandbox/seatbelt.py` | 1870 | `b16d640a21d057388a858dd047302916167e49e08bafbc23c4b2f058750267af` |
| `mewcode/serialization.py` | 7437 | `5bfa1863b81326fee7a26a4d043ca11e1296c1aa2a05ab9dc0381800e3a7969b` |
| `mewcode/session_dialog.py` | 5133 | `7c82287bf3afc05b3301d1cdcdbb5c436ceece1137d32c134c2bff8dbd5a75e0` |
| `mewcode/skills/__init__.py` | 492 | `971d715e71311925bbdf8a0b63df718c46d8a04918856c6d33f14540e8348bbf` |
| `mewcode/skills/builtins/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/skills/builtins/backend-interview/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/skills/builtins/backend-interview/references/parse_resume.py` | 4096 | `c3ce20cf943453186304e266ba2f2feffeebc5f2e5ccd5b9d4b013bb2a26eaf3` |
| `mewcode/skills/builtins/backend-interview/SKILL.md` | 1669 | `a49520d25c6fbb28fb8b689d5f25d96275c56dcb3d292281f78c1d520ae4d899` |
| `mewcode/skills/builtins/backend-interview/tool.json` | 392 | `7eee6390e4386ab241fe36f09de757c743b542c92205725d60464349fc9b7ebf` |
| `mewcode/skills/builtins/commit/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/skills/builtins/commit/SKILL.md` | 1008 | `944aba323e35b679e9d748eb178733cdffa0266845e5fc05578b26cceed33683` |
| `mewcode/skills/builtins/review/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/skills/builtins/review/SKILL.md` | 1439 | `0a76cd452bc226feb6691c41f2d63d31349487c6b9eb78e7e2827aee2aa71642` |
| `mewcode/skills/builtins/test/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/skills/builtins/test/SKILL.md` | 1203 | `d42cb0a885af511d86b08b93b4a65c490c9efbc670d26ac22efc8357ee08d46b` |
| `mewcode/skills/directory.py` | 5437 | `2b04c95e8b75a23b8f65990b846e2ac76d08aaf5d18dadf55d76e0f7ff6b500f` |
| `mewcode/skills/executor.py` | 6846 | `40d521d72a2990885c630437f9733ea900f6beb5aa10571978afd4ef3a7fc927` |
| `mewcode/skills/install.py` | 12604 | `0393811fad87caf067df6c006cc652298ebb151c86ee9883087d557bc85178b0` |
| `mewcode/skills/integration.py` | 678 | `efaaacaf6e70baf8fe4e208b0c14bcf5eed3bbbf39b6d9fb90c4e133248b9548` |
| `mewcode/skills/loader.py` | 6190 | `5888d912116f3a08cca7d75e54234299d6d2e8aa2789ca42c017d804390cc9b3` |
| `mewcode/skills/parser.py` | 3234 | `eba23547c0b392cd13211b38d1f45bc71a86c7de21a05b9998a3cd227ca3a11d` |
| `mewcode/styles.tcss` | 3302 | `bef599b42c5e490a7a7d5fd6f923910fb3163f7b1ee5f7e16ec4e515b346c826` |
| `mewcode/teammate_tree.py` | 2095 | `744b0ffdd0ed84385e2b3e5732cdafc1c384f9ee57f8f424e39c9ea30543b940` |
| `mewcode/teams/__init__.py` | 829 | `0fbc8c2d541304b512cb416ee347980280dc5b88688f0461e9ac6a13100d3517` |
| `mewcode/teams/backend_detect.py` | 2092 | `8d7d0e64b05899736a0bbb302d4117a76d7af8fe16516dbf021f935c18fab63f` |
| `mewcode/teams/coordinator.py` | 11283 | `f6aa905257ead944b7efb841f749e80d880481839ef4bc86ab81e55591582063` |
| `mewcode/teams/mailbox.py` | 4752 | `19ddabb3e23eed160fb3ac9b7db95aa5bdc6da1a27fdb384be696e4b5377f101` |
| `mewcode/teams/manager.py` | 12438 | `58ac2d14908bbf739538868c9d0bb304d76a0f23fcc301cb5344e273b59c84a4` |
| `mewcode/teams/models.py` | 5612 | `fa36b2d12beae0b0387c5ea4df2599b87ba0c5b10686800a8a62f95c7d52d4f6` |
| `mewcode/teams/progress.py` | 4316 | `3fd0b2adf15690dff0273015558f787226c4d14bdb0167b521923531ff55cd3e` |
| `mewcode/teams/protocol.py` | 1540 | `d231550f0f56d395ad4ac5f24a0e8deae968d9cb090c5de09185027269f58bcc` |
| `mewcode/teams/registry.py` | 1228 | `b292cd09bfc03576cf6a401d816ac44a047d5aea13ab2fdfaf289230fa661cf3` |
| `mewcode/teams/shared_task.py` | 3913 | `0bbab37fefc2c23ee53a98f086444e30eb597396acae72b694b6c60134724c26` |
| `mewcode/teams/spawn_inprocess.py` | 3321 | `84d04d616978e7ee9358163a76e37dbcfc422bf578c376dcbbcb0d5ae232bebb` |
| `mewcode/teams/spawn_iterm2.py` | 2570 | `6caecf3ceb56257272c50156841f9e062337fb6dc3d9642c809a78fb88dd9be5` |
| `mewcode/teams/spawn_tmux.py` | 3738 | `cbbc2c30bbdfb86c451b8e299484d208997e499deac1d2ba7bde7b0c1fca163f` |
| `mewcode/teams/transcript.py` | 3017 | `98fe1d12ad09a19cb35c88ab7d0e35ef3832e791ac15096eadf64137742838dd` |
| `mewcode/teams/worker.py` | 12702 | `7ad6de3372819dcb2fb78a29ee71dd29237a7513a2b3c7695bd332867519bfa5` |
| `mewcode/tools/__init__.py` | 7413 | `cf8ad1625c8b3ba91c3dc370b8ad561c53393a4e85c040ba3137616432c67779` |
| `mewcode/tools/agent_tool.py` | 28891 | `1472a750aa26fb26b3b1813976405e5bbbcaa7670687cbeac7c83d2567253344` |
| `mewcode/tools/ask_user.py` | 2416 | `cd6219bb50d96486774156bae2167284abe4c6e512a990d83e4c94b7cd7deaf3` |
| `mewcode/tools/base.py` | 2587 | `a57340d0d02f2e4c3ca2c1c4442f0c0e46085faad744f983bad64e614b47b064` |
| `mewcode/tools/bash.py` | 2157 | `24e05e37c804c4fc9bec2a50e95090b33fd4f1f89d4d7c7baeddeae79d9b84b3` |
| `mewcode/tools/diff.py` | 1151 | `60f1cb0c1915e5be8767ea1dd5896f1ac654d80f3b3148f34240240861539acc` |
| `mewcode/tools/edit_file.py` | 3462 | `3b550fb761586524a665f8427a57fca64eb54d3e4b67f4c0af071251a812e69b` |
| `mewcode/tools/enter_worktree.py` | 2315 | `badb05e6c9c5e18961f8ff2ffa251a02a60acdab185ee97eb03dc60353b3d123` |
| `mewcode/tools/exit_plan_mode.py` | 1733 | `754375d9f9710fca1b799c96045edf6dc7b76dd719bb160d2663ed8e5c5b2aa4` |
| `mewcode/tools/exit_worktree.py` | 4248 | `d39b812097ca2584e1049113f0a9562800d85aad97440a84960838e42283440c` |
| `mewcode/tools/file_changes.py` | 377 | `e9dd56027b88f07afdc919c5a4e06eb4e823e5b1039109f1b86bec8f65d1dcd2` |
| `mewcode/tools/file_state_cache.py` | 2159 | `03d51ef660c4bdceeccf6dd756df15a474b3cbf9a406508b6beac743a79b301d` |
| `mewcode/tools/glob.py` | 1622 | `01032f4af915bd76fd732ba4820edd6c493b53883ff7287e5531028995d5ab6a` |
| `mewcode/tools/grep.py` | 2379 | `1be2830e8f5b0c1cb76c7c2d16e606b636f2cf1d66ef4ae7da7ba4fc68eaebf0` |
| `mewcode/tools/impl/__init__.py` | 149 | `912b03d585da1e8fa109dc2773d2ef81879503771b988d8a0f50f090d2ea7ed6` |
| `mewcode/tools/impl/tool_search.py` | 3331 | `8c3d4a824a5b85c7eceff597c93e65adcb24789a3dad93272abb7eac20db8046` |
| `mewcode/tools/install_skill.py` | 3456 | `ddfe8417192295c6197aca4095787d35893ed0c8f9b1a6d819682816e443c052` |
| `mewcode/tools/load_skill.py` | 2965 | `2eb89e486d11167ceae3a18b973accecf12658bcb60c593ea7ea47641c682f52` |
| `mewcode/tools/mcp_call.py` | 4050 | `f7930ad22ae1abb38ef152434bf8749127fdc3fc462e3d2567c6bee70a764e00` |
| `mewcode/tools/read_file.py` | 2515 | `671338dcc03f8124f4d3e18b68e6e53ade7e656137a47ceef9d2abf24043ced1` |
| `mewcode/tools/send_message.py` | 4257 | `9eee22b05f9f8a7078692088dc120302266912ae96770dbdd153ed1f1144ee56` |
| `mewcode/tools/synthetic_output.py` | 2318 | `0da216faf1f29f03d4d165e6aed7427bee2f7c24b72c0c481f08defd56d3a115` |
| `mewcode/tools/task_create.py` | 1932 | `95d37b386b289f5e9d7e014ec091d9bfb4503c688f7fe2674f96e9b2a0459276` |
| `mewcode/tools/task_get.py` | 1881 | `7f897811a516511bbde82b7981af6f94fdff1ba0b7ee4df756177d30d20f1f0f` |
| `mewcode/tools/task_list.py` | 2277 | `fa3b3014d26f95a100b27bb220c787f93405cdd7d70fd856f6b110c017a9913f` |
| `mewcode/tools/task_stop.py` | 988 | `f5cd86055646581eb7d6359d0f34d71a417c063556192d637f8797fe470d46bf` |
| `mewcode/tools/task_update.py` | 2720 | `f7337199668ca8f66a0a4179cb863f33b2d68a458907d3e104aab44cfef553f9` |
| `mewcode/tools/team_create.py` | 5147 | `e943f214be8be61de4f283c2c1c6a4499c169bf13ec5d86aea2e70401188b621` |
| `mewcode/tools/team_delete.py` | 2509 | `6debc282628e0385eba0f63eb58d56cc67666e6e468ccaa50969383d41e05fd0` |
| `mewcode/tools/workspace.py` | 1587 | `7b649f7aac8f2b014ea708563b70ba02e160a3dfa88bc7a66d884e005ce93d0b` |
| `mewcode/tools/write_file.py` | 2767 | `2ffddfba2e2ad54df82021d2e93258df6f4e26071dda4d6b1ea2fd4f94c39075` |
| `mewcode/validator.py` | 10689 | `fd523a432a41fe01ee5bcb93514c18e643ac9e6c85659a652a2e719bac1052bc` |
| `mewcode/worktree/__init__.py` | 993 | `3a585e9cdc8bb6cc49ee9295389dee68a739b3c4802dfc5691a2a98ab470d364` |
| `mewcode/worktree/changes.py` | 2069 | `c6fb2558390f33730f600d9da23dbdb32d2680658271ea03d67cf5368529e1d4` |
| `mewcode/worktree/cleanup.py` | 3018 | `682639b1b2e5cd2cbb0e589858ea91aa4ae03a0cd7114ce4b1376728039cf2d2` |
| `mewcode/worktree/integration.py` | 1084 | `e30207887798be4876871542de048d59473c33d9bc6bba50a6e4f742655cfcc3` |
| `mewcode/worktree/manager.py` | 11093 | `5926c8adef0a381bb12e1c60e868f49766e21d14fa6e0fae8273af16ab82df92` |
| `mewcode/worktree/models.py` | 641 | `96ae44187b0f16061909feddaebe31a3accbb9cf3f1e9d1abfce82adab9a5eff` |
| `mewcode/worktree/session.py` | 2003 | `ad66f663ee5ac4115663b3699deb348a623e4c38636762c750159affad6e9844` |
| `mewcode/worktree/setup.py` | 3791 | `220d3d72283334ad7d68a28734a0b207ca395b88fa3c66b8b24496efcf8f7bd6` |
| `mewcode/worktree/slug.py` | 911 | `bad85f5d429d23a8355b113d324964fc0f8d18f71a4004cc9eecc68ab1957fa5` |
| `MEWCODE.md` | 109 | `f45ca3616774a67387656bab7e2895d718ce16213119990b0facc00244682e92` |
| `pyproject.toml` | 735 | `9dd304b5157ec60d90ffd226ef927502f2a5d777570476a457c204fe50c92829` |
| `README.md` | 11790 | `b9389a1fa76057c4e7a810c0f67b959407ac3f4772a9a7cc1c758f7c4362f4df` |
| `tests/test_agent.py` | 17177 | `e82822e9807536954f93e764d431a918cc37a4c0de15422129470490fafb64de` |
| `tests/test_anthropic_wire.py` | 4702 | `497518e4376278e660e8e553d149cbc12bb08cd1218e03da6b23dfc975fa1533` |
| `tests/test_client_ownership.py` | 12552 | `e2a857498c2846a296a2cdbca7d3459356ed8383916dbdb132ef7adb6cf7c13f` |
| `tests/test_commands.py` | 17403 | `159b0e2c2c184600e1549e2e58bfb3b4695615e813a21302aaaa52a365e69422` |
| `tests/test_compatible_usage.py` | 2150 | `0cba8476c747b4bf69858e2e1f6c03b22841b72df8df19e4426b318e866459e5` |
| `tests/test_context.py` | 28064 | `e596807ec0468b909f6898c0582b4fbbb2ec6bc501c26a7b4d37d9297556d387` |
| `tests/test_context_window.py` | 9479 | `060b88f7ecb3768bdb99ab35542bfc9899fdfd86f20663127199aa30231dd695` |
| `tests/test_file_checkpoints.py` | 10040 | `0544315df7f0e64e61a9621d0676218068e83714f253afd777fc46abb3fa3a20` |
| `tests/test_framework_runtime.py` | 23070 | `69c46dd88cad28ff2ef3bf481384c2e8d4850341594a291fe69ce8d6ae416e0d` |
| `tests/test_hooks.py` | 20883 | `fe8b488efa8bc769e5ee4b29b0285646fa8889224a3b2bff386b6af80f036515` |
| `tests/test_legacy_assets.py` | 12468 | `9950978455b3526f55f9d11110edf228d3abc46fcdb256b0d4c7f374eb40b270` |
| `tests/test_lifecycle_audit.py` | 11967 | `df3586f0c262b851171962ab5412e642e54df90f07306746e86740daf385e0e6` |
| `tests/test_mcp.py` | 10719 | `32350b14e00aa45d460818f60f260927adab3263a95689eab0d679fbbb576996` |
| `tests/test_mcp_loading.py` | 14271 | `3a8098b290ffb6fadd19ed100f372f9fac96c7a7ddec5f22ead18d69993ab934` |
| `tests/test_memory.py` | 30104 | `5865feac1653110449fd0897d33744c1521de21232def57a5d8d5b69834e01eb` |
| `tests/test_migration_basics.py` | 7751 | `ccf0e23e27d029ff466a81d87ba01c486183da072c8411daddd9cb207934412e` |
| `tests/test_os_sandbox.py` | 5757 | `beff88ea21e32a27b120d180521efc1a2bb970ca331129892e22081775acb76b` |
| `tests/test_output_lifecycle.py` | 3272 | `8e374d25dc93d312b29eb761e7f26daf6d0581bc1e84fa54aa52b7efbf379148` |
| `tests/test_permissions.py` | 24291 | `826a87e4af14daf7a61304510577b8d80c5c489c0c5d13e72bd684afe20ccc95` |
| `tests/test_plan_audit.py` | 1554 | `e6aa8327f427209524a3c99af697cda523b0366ff5b2d6f0291922d44d6db1af` |
| `tests/test_process_cleanup.py` | 1616 | `bf8527278b8abfb92a8cd721f3019f47796298a3759fa602118b2b660cc4216e` |
| `tests/test_recovery.py` | 2525 | `d5457b2c2fdf95cb073de9543843ffc922ff1c1e6f1e7c50fa498dbd48270f06` |
| `tests/test_replacement_state.py` | 9832 | `f7de35f75a7bab500772857bdf01a4bc349d3fc62ce9f7fee7866a394a7f7230` |
| `tests/test_responses_wire.py` | 7168 | `1f6ec2d09a5d46ca22deb6c07bed6c8b6494972930093976ed068f153566bae1` |
| `tests/test_serialization.py` | 4283 | `8791767a8229d5761ec91e542089c81093abaada2839f184c8e958ec6504961e` |
| `tests/test_skill_execution_routes.py` | 6319 | `f023d81b2ecaf15f5c6a81ab162113f537cdbb17e616f2aed0f5779e9589f9fb` |
| `tests/test_skill_install.py` | 7196 | `4ac6d61de194cb9e36e1a3b22e5d55de99b4c0aae97289f5293c432935a4ed61` |
| `tests/test_skills.py` | 18795 | `eed0713948faa550a626815080882a750a25b6fa8212aac116e9ff36c172d313` |
| `tests/test_storage_migration.py` | 14786 | `1c25498e8adb80ff37f8b8c417b1a5cf07c945795cc7ec56d825958fc3f8b4f3` |
| `tests/test_stream_json.py` | 5983 | `9c44013e219b3a77d6f4cf84334c6784c425cdb36178afc60a32f6d5561bf869` |
| `tests/test_subagent.py` | 27639 | `4e022bbc47deceef90a5830182c1f5e4f086d497b960d4b9fbaa2e93a4fabdc3` |
| `tests/test_subagent_control.py` | 9846 | `6aaa442399fb11b563f07c1a51871a8f5dc506c8706088da1821da2ce8f57faa` |
| `tests/test_summary_cache.py` | 15923 | `58a524d8f1f8790a3fba976d6098d770f45be31e0c7fc3c04e6344684019df61` |
| `tests/test_team_workers.py` | 8260 | `c848bbf5cda69e3aff590b5032cdf868525e0d86a24f59cc25723348cdc37490` |
| `tests/test_teams.py` | 21764 | `f58157991b09a2d53c35f5485d23442edf2f77252e364e5512967cfb6e563556` |
| `tests/test_tool_search.py` | 10528 | `742f3a22fc3e119419d22c479c4add4733b67498a28d16cadf552e407f7a2768` |
| `tests/test_tool_workspace.py` | 6156 | `e8c5b9cb3ceb50834a3b0e7e72db6aa7421c13c9983e3912c6c5b2090508cb45` |
| `tests/test_tui_framework.py` | 5268 | `9fe0dce9cedd4f4a2eaa311f91087ab3371d39b89980cddd4718385a6cc9a49b` |
| `tests/test_tui_session_matrix.py` | 7659 | `b66334b939df27ea9f1ab8265ca4c38680162a24110cc82057f5ccb4e3ad25c9` |
| `tests/test_worker_bootstrap.py` | 11348 | `14be939053cff49049a5062f614d4e56a3fe8c8598d1d4172771478019b09dd8` |
| `tests/test_worker_profiles.py` | 10398 | `7a9fc4e48e2e3f7cf38e4ebd74d1f0cff94d12bffd83370669f8f5e3002cb4be` |
| `tests/test_workspace_sessions.py` | 14970 | `d744b874b005dd0da0616a5988058a614c7a7e74173baf069cb7a7e00fd7a8a0` |
| `tests/test_worktree.py` | 15610 | `a6e7008f06f309fa49ac1837a5b682ff9b43271d6948edb76aaccde377b6d82a` |
| `tests/trace_agent_backend.py` | 6157 | `d4efe2a00ff48ebcb98d4f00b4ddf8c1e7fb7df75195658a2398280d244ab153` |
| `tests/trace_sdk_retries.py` | 6715 | `6ee9748168acf1f78fc9c8ec00181ee5d7d6abd3cede738927e17f803d0799cd` |
| `tests/verify_baseline_behavior.py` | 3054 | `3dab65922d28a6bda55ec0641d40fe13f8a8f7fb511f1b569fbcf533a571cc62` |
| `tests/verify_mcp_cli.py` | 5497 | `53740d53f1672d371e7292f54a61e87cb0792d41ab809afc94b8d3a4f33b3597` |
| `tests/verify_sdk_retry_behavior.py` | 2554 | `363303f0bb5f580fc55902c262c748d2d1cb59ef46e0abd3cc7c179712b33705` |
| `tests/verify_seatbelt.py` | 2348 | `9744706be5fe0c338c4e76f4284c86d39940a60366f3711777f84de931ceeaa0` |
| `tests/verify_skill_install_online.py` | 2494 | `079a2170020b0791541a7bb840c68ba315dfca4476e1cda71be5e4350b1c0e30` |
| `tests/verify_subagent.py` | 19064 | `6f889b1cd7274e3053f493767b8c23f011465494b10e74ad20a4f52299fc21ac` |
| `tests/verify_summary_cli.py` | 5632 | `574858f1900e38bb5a68a40a271d64caf910d20d4965d8935ea4dbb7ea341e6a` |
| `tests/verify_worker_cli.py` | 5906 | `6bb018a12a2ce072e4f1a559783b08c1cb4ccfdbfac72a32cbd1accefb759e83` |
| `tests/verify_workspace_cli.py` | 7524 | `800c5c5b2961ca2b80534d6c7b58e8f0581d2bfbff6f8aef0d0a6a3d4eaedbdd` |
