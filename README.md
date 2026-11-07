# pycode

终端编程 Agent，基于 LangChain `create_agent` 和 LangGraph。正在按 [计划.md](计划.md) 重构，目前完成阶段 4：
内置工具（read、write、edit、bash、grep、find、ls）、写入和命令前的审批、会话保存、回退和分支、
上下文后台压缩（窗口用到 80% 时在后台生成摘要，90% 时用摘要替换旧消息）、Textual 界面。

摘要提示词来自 [Pi](https://github.com/earendil-works/pi)（MIT，见 `licenses/pi-MIT.txt`）。

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
pycode --workspace /path/to/project         # 打开界面；--session ID 直接打开某个会话
```

`providers.json` 里的 `api_key_env` 是环境变量名，文件本身不保存密钥。会话保存在 `~/.pycode/sessions.sqlite`
（可用 `PYCODE_HOME` 改位置）。

## 界面里的操作

| 按键 | 作用 |
|---|---|
| Enter / Shift+Enter 或 Ctrl+J | 发送 / 换行 |
| Tab | 补全斜杠命令 |
| y / n（或方向键加 Enter） | 审批时允许 / 拒绝 write、edit、bash |
| Ctrl+C | 停止当前运行；在审批时按下等于拒绝 |
| F6 | 切换深色 / 浅色主题 |
| Ctrl+Q | 退出 |

| 命令 | 作用 |
|---|---|
| `/new` | 新建会话 |
| `/sessions` | 列出当前工作区的会话 |
| `/open ID` | 打开会话，显示完整历史（包括压缩前的原文） |
| `/revert N` | 回到第 N 轮结束时，之后的对话留在另一个分支 |
| `/branches`、`/switch N` | 查看分支、切换分支 |
| `/delete ID` | 删除会话 |
| `/compact` | 立即压缩上下文 |
| `/model [名字]` | 列出或切换 `providers.json` 里的模型 |
| `/theme`、`/help`、`/quit` | 切换主题、列出命令、退出 |
