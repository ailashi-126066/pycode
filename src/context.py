"""What every tool can reach through `runtime.context` during one run."""

from dataclasses import dataclass

from .tools.files import Files


@dataclass
class Context:
    files: Files
    sandboxed: bool = False  # bash commands run under Seatbelt (see sandbox.py)
