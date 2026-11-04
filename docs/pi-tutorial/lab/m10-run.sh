#!/usr/bin/env bash
# M10 实验：用假模型运行 pi，并接上 m10-server.mjs 这个 stdio MCP 服务器。
# 用法：PI=/path/to/pi WORK=/tmp/pi-lab/m10-work-x PI_CODING_AGENT_DIR=/tmp/pi-lab/m10-agent-x ./m10-run.sh [pi 参数...]
# 环境变量：
#   EXPOSURE      写进 mcp.json 的 exposure，默认 direct（可选 direct / deferred / codemode / hidden）
#   M10_SCENARIO  假模型的脚本，见 m10-faux-ext.ts，默认 direct
#   PI_TRACE=1    打印模型每次收到的消息
#   GATE=1        额外加载 m10-gate.ts（拦截所有 mcp__ 工具调用的权限扩展）
# 它做三件事：
#   1. 在 $PI_CODING_AGENT_DIR/mcp.json 写入名为 demo 的服务器（每次运行都会覆盖这个文件）
#   2. 让服务器把收到的每条消息记到 $PI_CODING_AGENT_DIR/server.log（追加）
#   3. 用 m10-faux-ext.ts 代替默认的假模型，调用 run.sh
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PI_CODING_AGENT_DIR:?请设置 PI_CODING_AGENT_DIR，例如 /tmp/pi-lab/m10-agent-1}"
: "${WORK:?请设置 WORK，例如 /tmp/pi-lab/m10-work-1}"
EXPOSURE="${EXPOSURE:-direct}"

mkdir -p "$PI_CODING_AGENT_DIR"
cat > "$PI_CODING_AGENT_DIR/mcp.json" <<EOF
{
  "mcpServers": {
    "demo": {
      "command": "node",
      "args": ["$LAB_DIR/m10-server.mjs"],
      "exposure": "$EXPOSURE"
    }
  }
}
EOF

export PI_CODING_AGENT_DIR WORK
export M10_SERVER_LOG="$PI_CODING_AGENT_DIR/server.log"
export EXT="$LAB_DIR/m10-faux-ext.ts"
extra=()
if [[ "${GATE:-}" == "1" ]]; then
	extra=(-e "$LAB_DIR/m10-gate.ts")
fi
exec "$LAB_DIR/run.sh" ${extra[@]+"${extra[@]}"} "$@"
