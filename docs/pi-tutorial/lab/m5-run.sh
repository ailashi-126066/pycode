#!/usr/bin/env bash
# M5 实验：用小上下文窗口的假模型（m5-faux-ext.ts）运行 pi，强制触发压缩。
# 用法：PI=/path/to/pi WORK=... PI_CODING_AGENT_DIR=... ./m5-run.sh [pi 参数...]
# 环境变量：
#   M5_CTX      模型的上下文窗口，默认 3000（token）
#   M5_RESERVE  settings.json 的 compaction.reserveTokens，默认 500
#   M5_KEEP     settings.json 的 compaction.keepRecentTokens，默认 600
#   M5_TRACE=1  打印假模型每次收到的请求
# 脚本会在 WORK 里生成 a.txt … e.txt（每个约 2000 字符，第一行是 [文件名]），
# 并把压缩设置写进 $PI_CODING_AGENT_DIR/settings.json。WORK 和 PI_CODING_AGENT_DIR 必须设置，
# 不要指向 ~/.pi。
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PI:?请先设置 PI=pi 仓库的路径}"
: "${WORK:?请设置 WORK=实验工作目录，例如 /tmp/pi-lab/m5-work}"
: "${PI_CODING_AGENT_DIR:?请设置 PI_CODING_AGENT_DIR=配置目录，例如 /tmp/pi-lab/m5-agent}"
export WORK PI_CODING_AGENT_DIR
mkdir -p "$WORK" "$PI_CODING_AGENT_DIR"

for name in a b c d e; do
	file="$WORK/$name.txt"
	if [[ ! -f "$file" ]]; then
		{
			printf '[%s.txt]\n' "$name"
			for i in $(seq 1 40); do printf 'line %02d of %s.txt: lorem ipsum dolor sit amet.\n' "$i" "$name"; done
		} > "$file"
	fi
done

printf '{"compaction":{"reserveTokens":%s,"keepRecentTokens":%s}}\n' "${M5_RESERVE:-500}" "${M5_KEEP:-600}" \
	> "$PI_CODING_AGENT_DIR/settings.json"

EXT="$LAB_DIR/m5-faux-ext.ts" exec "$LAB_DIR/run.sh" "$@"
