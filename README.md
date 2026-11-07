# pycode

终端编程 Agent，基于 LangChain `create_agent` 和 LangGraph。正在按 [计划.md](计划.md) 重构，目前完成阶段 1：
内置工具（read、write、edit、bash、grep、find、ls）、写入和命令前的审批、命令行运行一次任务。

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
