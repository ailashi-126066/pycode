# pycode

终端编程 Agent，基于 LangChain `create_agent` 和 LangGraph。正在按 [计划.md](计划.md) 重构，目前完成阶段 2：
内置工具（read、write、edit、bash、grep、find、ls）、写入和命令前的审批、会话保存、回退和分支。

## 安装

需要 Python 3.11+，以及 PATH 中的 `bash`、`rg`（ripgrep）、`git`。只支持 macOS 和 Linux。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## 运行

```bash
cp providers.example.json providers.json   # 按自己的服务修改模型和地址
export DEEPSEEK_API_KEY='你的密钥'           # 也可以写在工作区的 .env 里
pycode --workspace /path/to/project run '看看 src 目录里有什么'
```

`providers.json` 里的 `api_key_env` 是环境变量名，文件本身不保存密钥。write、edit、bash 执行前会询问 `[y/N]`。

## 会话和分支

会话保存在 `~/.pycode/sessions.sqlite`（可用 `PYCODE_HOME` 改位置）。

| 命令 | 作用 |
|---|---|
| `pycode run '任务' --session ID` | 在已有会话里继续 |
| `pycode session list` | 列出当前工作区的会话 |
| `pycode session open ID` | 显示对话；有待审批的操作时询问并继续 |
| `pycode revert ID N` | 回到第 N 轮结束时，之后的对话保留为另一个分支 |
| `pycode session branches ID` / `session switch ID N` | 查看分支 / 切换到分支 N |
| `pycode resume ID --answers JSON` | 没有终端时回答审批（运行会以退出码 2 停下并打印待审批内容） |
| `pycode session delete ID` | 删除会话 |
