# Source: https://github.com/earendil-works/pi/blob/2c8d1fe/packages/coding-agent/src/core/compaction/utils.ts
"""Files read and modified by the read/write/edit tools, carried across compactions."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from pycode.kernel import Message


@dataclass
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


# Pi: extractFileOpsFromMessage
def extract_file_ops_from_message(message: Message, file_ops: FileOperations) -> None:
    for call in message.tool_calls:
        path = call.arguments.get("path")
        if not isinstance(path, str) or not path:
            continue
        if call.name == "read":
            file_ops.read.add(path)
        elif call.name == "write":
            file_ops.written.add(path)
        elif call.name == "edit":
            file_ops.edited.add(path)


# Pi: computeFileLists
def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    modified = file_ops.edited | file_ops.written
    return sorted(file_ops.read - modified), sorted(modified)


# Pi: formatFileOperations
def format_file_operations(read_files: Sequence[str], modified_files: Sequence[str]) -> str:
    sections = []
    if read_files:
        sections.append("<read-files>\n" + "\n".join(read_files) + "\n</read-files>")
    if modified_files:
        sections.append("<modified-files>\n" + "\n".join(modified_files) + "\n</modified-files>")
    if not sections:
        return ""
    return "\n\n" + "\n\n".join(sections)


def format_loaded_skills(read_files: Sequence[str]) -> str:
    """11.1 section 33.1 #8: one line naming the skills read so far (/skills/<source>/<name>/)."""
    skills = [
        f"{path.split('/')[-2]} ({path})"
        for path in read_files
        if path.startswith("/skills/") and path.endswith("/SKILL.md")
    ]
    return "\n\nLoaded skills: " + ", ".join(skills) if skills else ""
