# 手册构建与核对

正文写在 `../content/*.md.in`，分组的 `chains-*.json` 保存真实调用链，`inline-*.json` 保存独立数据定义解释。
网页阅读不需要安装依赖、API Key、业务服务或网络；构建时需要 Python 3.11+ 与 Node.js 20+。

## 构建

在仓库根目录执行：

```bash
.venv/bin/python docs/interview/tools/build.py
.venv/bin/python docs/interview/tools/build.py --check
.venv/bin/python docs/interview/tools/check_index.py
```

渲染需要 `marked` 与 `@viz-js/viz`。当前开发机可直接使用已有依赖；其他环境可执行：

```bash
npm install --prefix docs/interview/tools
```

也可用 `CODEX_DOC_NODE_MODULES` 指向包含这两个库的 node_modules，`NODE_BINARY` 指定 Node 可执行文件。
依赖版本固定在本目录 package.json。构建只解析已有源码，不 import 产品、不启动 Agent、不连接模型/MCP。

## 源文件与生成物

- source_index.py：用 Python AST 定位类、函数、方法和嵌套函数；保留装饰器、完整源码与行号。
- build.py：核对每个节点真实存在、解释不为空、章节和源码链接有效、模块章节齐全、无未使用链；生成Markdown。
- render.cjs：将同一份Markdown渲染为内嵌数据的index.html、Agent独立run.html、source.html。
- book.css / chain.css：沿用参考手册的页面布局、表格、节点卡片、原地展开、嵌套辅助函数与移动端样式。
- coverage.json：记录源码固定版本、全部已索引文件SHA、生成器输入与生成产物摘要、章节/链/节点数量。

`--check` 不写文件，会核对生成Markdown与当前模板一致，源码/生成器输入与产物摘要一致。
源码基线使用最后一次改动 src、evals、scripts 的提交，纯文档提交不会让源码链接漂移。
`src/__init__.py` 对应逻辑模块 `pycode`，`src/agent/runtime.py` 对应 `pycode.agent.runtime`；源码文件链接使用实际 `src/` 路径。
索引会拒绝源码文件或其祖先目录的符号链接及越界路径。类型重载导航到实际实现；条件同名定义按源行区分，不静态猜测分支。`check_index.py` 用不执行的临时源码验证遮蔽、闭包、类作用域、重载和符号链接边界。

## 内容约定

章节编号统一由文件名（00–19）生成，正文标题和侧栏使用同一个编号；模板标题只写文字，避免某些章节有编号、某些没有。

每个模块章先接上前一章的具体任务：修复 parser.py、运行测试，再按需要审查、查外部服务或回退。先把这件事从输入走到结果，之后才回看设计、框架和组装。不要先放一张术语表要求读者背定义。

每一步都要回答：上一步还缺什么，所以为什么调用这里；传入什么；做完把什么交给谁；谁接着处理。说明应该连成自然段，不能把函数名加一句职责当作完整解释。相似概念确实会混淆时，先用具体动作区分，再给代码名字供查找。

链顶层字段是 title/input_label/request/nodes/result；节点是 file/name/purpose/guide/notes/helpers，可递归嵌套。guide 是读者不展开代码时就能看到的连贯说明，purpose 是卡片上先显示的中文动作；函数名和路径作为定位信息随后显示。notes 在展开后解释代码里的关键判断。每个节点都必须写 guide，不能批量套一句空话。

edge 要明确普通调用、框架稍后调用、预先保存的方法以及协作顺序的区别，groups 表示并列入口。创建运行对象、建立执行图、启动执行图必须分清；不能仅因它们相邻显示，就暗示前者直接调用后者。

```text
{{chain:模块:动作}}
{{inline:src/路径.py:Class.method}}
```

完整源码由AST位置提取，不手抄、不省略关键步骤。工厂依赖图和框架回调不能伪装成直接调用。
源码浏览器只保守解析导入/词法/self目标；动态注入要由人工链和装配表解释，不通过同名搜索猜实现。

本索引包括产品Python及维护的evals/scripts，不包括tests、archive、运行产物、.env、用户记忆或日志。
人工审查仍须核对顺序、权限与默认值；“符号存在”不等于解释正确。浏览器交互也应实际检查筛选、导航、嵌套展开、完整文件和移动端布局。
