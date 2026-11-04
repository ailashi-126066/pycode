#!/usr/bin/env bash
# 用假模型运行主线场景。
# 用法：PI=/path/to/pi ./run.sh [额外的 pi 参数...]
# 默认参数是 -p "读 README.md，总结成三句话"；传了参数就用传入的参数。
set -euo pipefail

LAB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${PI:?请先设置 PI=pi 仓库的路径}"
WORK="${WORK:-/tmp/pi-lab/work}"
EXT="${EXT:-$LAB_DIR/faux-ext.ts}"

# 隔离配置目录，不影响 ~/.pi
export PI_CODING_AGENT_DIR="${PI_CODING_AGENT_DIR:-/tmp/pi-lab/agent}"
mkdir -p "$WORK" "$PI_CODING_AGENT_DIR"
if [[ ! -f "$WORK/README.md" ]]; then
	printf '# Demo\n\nThis is a demo project.\nIt has one README.\nIt is used to trace pi.\n' > "$WORK/README.md"
fi

if [[ $# -eq 0 ]]; then
	set -- -p "读 README.md，总结成三句话"
fi

cd "$WORK"
exec "$PI/pi-test.sh" --no-env -e "$EXT" --provider faux --model faux-1 "$@"
