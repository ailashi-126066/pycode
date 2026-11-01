# Source: MewCode local permissions/dangerous.py; SHA-256:
# 6de120e749f1aaa3ea12fe657891c0b2bf944d26e5de33bdefecbd0bf6443d95
# Adaptation: pure source deny patterns; no claimed general shell sandbox.
"""Source command deny rules remain ahead of sandbox automatic approval.

The deny patterns are defence in depth only: a shell can always spell a command in
a way no regular expression recognises. Automatic approval therefore relies on the
separate allow list in ``auto_approvable``, never on the absence of a deny match.
"""

import re

_SHELL = r"(?:sudo\s+)?(?:ba|da|k|z)?sh\b"
_TARGET_END = r"(?:\s|$|[;&|])"
PATTERNS = (
    (
        # Any rm (any flag order, e.g. -rf, -r -f, --no-preserve-root) of /, /*, ~ or $HOME.
        r"\brm\s+(?:-\S*\s+)*(?:/\*?|~/?\*?|\$HOME/?\*?|\$\{HOME\}/?\*?)" + _TARGET_END,
        "Recursive forced deletion of root",
    ),
    (r"mkfs\.", "Disk formatting"),
    (r"dd\s+if=.*of=/dev/", "Direct disk device write"),
    (r"chmod\s+-R\s+777\s+/", "Recursive root permission change"),
    (r":\(\)\{\s*:\|:&\s*\};:", "Fork bomb"),
    (r"(?:curl|wget)\b[^\n]*\|\s*" + _SHELL, "Remote script execution through a pipe"),
    (
        r"(?:\b(?:ba|da|k|z)?sh|\bsource|(?:^|[;&|]\s*)\.)\s+<\(\s*(?:curl|wget)\b",
        "Remote script execution through process substitution",
    ),
    (r"\b(?:ba|da|k|z)?sh\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b", "Remote script execution"),
    (r">\s*/dev/sd", "Disk device overwrite"),
)


def denied_command(command):
    for pattern, reason in PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


# Conservative read-only commands for on-auto approval. Anything else, including
# sudo, interpreters and editors, falls back to the ordinary approval interrupt.
READ_ONLY_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "dirname",
        "echo",
        "find",
        "git",
        "grep",
        "head",
        "ls",
        "pwd",
        "realpath",
        "rg",
        "stat",
        "tail",
        "true",
        "wc",
        "which",
    }
)
# find actions that execute commands, delete files or write output files.
FIND_UNSAFE = frozenset(
    {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
)
GIT_READ_ONLY = frozenset({"status", "diff", "log", "show", "branch", "rev-parse", "ls-files"})
GIT_BRANCH_LISTING = frozenset(
    {"-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "--show-current", "--verbose"}
)
# Shell syntax that substitutes commands, expands variables or redirects data.
UNSAFE_CHARACTERS = frozenset("`$<>\\\n\r")
SEPARATORS = frozenset({";", "&&", "||", "|"})
OPERATOR_CHARACTERS = frozenset(";&|")
# Unquoted pathname and brace expansion happens after this check, so a word such as
# "-de*" or "{-delete,}" can become an option chosen by file names in the workspace.
EXPANSION_CHARACTERS = frozenset("*?[{")
# Commands whose options can execute programs, delete files or write output.
OPTION_SENSITIVE = frozenset({"find", "rg", "git"})


class _Word(str):
    """A shell word after quote removal, remembering unquoted expansion characters."""

    expands = False


def _segments(command):
    # $, backslashes and newlines are rejected beforehand, so quotes are the only
    # escaping mechanism left and the shell's word splitting reduces to this scan.
    segments, text, expands, operator, quote = [[]], None, False, "", None

    def flush_word():
        nonlocal text, expands
        if text is not None:
            word = _Word(text)
            word.expands = expands
            segments[-1].append(word)
            text, expands = None, False

    def flush_operator():
        nonlocal operator
        if operator:
            if operator not in SEPARATORS:
                # Background jobs (&) and unusual operators (|&, ;;) are not read-only lists.
                raise ValueError(operator)
            segments.append([])
            operator = ""

    for character in command:
        if quote is not None:
            if character == quote:
                quote = None
            else:
                text += character
            continue
        if character in OPERATOR_CHARACTERS:
            flush_word()
            operator += character
            continue
        flush_operator()
        if character in " \t":
            flush_word()
            continue
        text = text or ""
        if character in "'\"":
            quote = character
        else:
            text += character
            expands = expands or character in EXPANSION_CHARACTERS
    if quote is not None:
        raise ValueError("unterminated quote")
    flush_word()
    flush_operator()
    return segments


def _read_only(words):
    name, arguments = words[0], words[1:]
    if name not in READ_ONLY_COMMANDS or words[0].expands:
        return False
    if name in OPTION_SENSITIVE and any(argument.expands for argument in arguments):
        return False
    if name == "find":
        return not FIND_UNSAFE.intersection(arguments)
    if name == "rg":
        # --pre runs an arbitrary preprocessor command for every searched file.
        return not any(argument.startswith("--pre") for argument in arguments)
    if name == "git":
        # Global options such as -c can run configured programs; none are accepted.
        if not arguments or arguments[0] not in GIT_READ_ONLY:
            return False
        if any(argument.startswith("--output") for argument in arguments):
            return False
        if arguments[0] == "branch":
            return all(argument in GIT_BRANCH_LISTING for argument in arguments[1:])
    return True


def auto_approvable(command):
    """Return whether every pipeline/list segment is a known read-only command."""
    if not isinstance(command, str) or UNSAFE_CHARACTERS.intersection(command):
        return False
    try:
        segments = _segments(command)
    except ValueError:
        return False
    return all(segments) and all(_read_only(words) for words in segments)
