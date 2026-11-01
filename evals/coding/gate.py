"""Evaluation grants writes only to solutions and commands only to fixed tests."""

import posixpath
import shlex

from pycode.kernel.domain.errors import ToolError


class EvaluationToolGate:
    def __init__(self, solution_files, test_files):
        self.solutions = set(solution_files)
        self.tests = set(test_files)

    async def authorize(self, context, spec, call):
        if spec.category == "read":
            return
        if spec.name in {"write", "edit"}:
            # Pi's write/edit take a workspace-relative or absolute path.
            path = call.arguments["path"]
            path = posixpath.normpath(path if path.startswith("/") else "/workspace/" + path)
            permitted = path.removeprefix("/workspace/") in self.solutions
            if path.startswith("/workspace/") and permitted:
                return
            raise ToolError("eval_write_denied", "Modify only declared evaluation solution files")
        if spec.name == "bash":
            command = call.arguments["command"]
            if any(character in command for character in (";", "|", "&", "`", "$", "\n", ">", "<")):
                raise ToolError("eval_command_denied", "Use a single fixed test invocation")
            tokens = shlex.split(command)
            valid = bool(tokens) and tokens[0] in {"python", "python3", "pytest"}
            remaining = tokens[1:]
            if tokens and tokens[0] in {"python", "python3"}:
                valid = valid and remaining[:2] == ["-m", "pytest"]
                remaining = remaining[2:]
            for token in remaining:
                if token not in self.tests and token not in {"-q", "-v", "-x"}:
                    valid = False
            if valid:
                return
        raise ToolError("eval_action_denied", "Evaluation cannot invoke arbitrary effects")
