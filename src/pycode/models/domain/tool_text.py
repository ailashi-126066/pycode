"""Bounded model text preserves error/exit flags on transports without status fields."""

from pycode.kernel import MAX_BYTES, MAX_LINES, ToolResult, truncate


def model_tool_text(result: ToolResult) -> str:
    status = []
    if result.is_error:
        status.append("Tool error")
        if result.error_code:
            code = truncate(result.error_code, "tail", max_lines=1, max_bytes=192).text
            status.append(code)
    if result.exit_code is not None:
        status.append(f"exit code {result.exit_code}")
    if not status:
        return result.text
    mode = "tail" if result.name.lower() in {"bash", "command"} else "head"
    footer = "\n[" + "; ".join(status) + "]"
    reserve = max(512, len(footer.encode()) + 192)
    preview = truncate(result.text, mode, max_lines=MAX_LINES - 2, max_bytes=MAX_BYTES - reserve)
    hint = ""
    if preview.truncated:
        path = result.details.get("full_output_path")
        hint = (
            f"\n[Full output: {path}]"
            if path
            else "\n[Model preview limited; original result remains in history.]"
        )
    return preview.text + hint + footer
