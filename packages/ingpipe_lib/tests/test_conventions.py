"""Source-tree conventions enforced as tests rather than as prose.

MAINTAINING.packages.md describes several house rules as grep invocations,
but the repo has no CI, so nothing runs them -- which is exactly how the
host-dependence defect class this module's first rule closes stayed invisible
until the engine was first exercised on a second platform. The workspace has
three channels that DO run routinely on both development machines: ruff, mypy,
and pytest. A rule that needs to outlive the person who wrote it belongs in
one of them.

Scope is deliberately one rule. MAINTAINING §1's other grep-guarded rules (no
sibling imports between engine packages, no instance references from the
engine) are good candidates for the same treatment, but absorbing them here
would change rules this module's activity does not otherwise touch.

This module needs no database and must never skip.
"""

import io
import tokenize
from pathlib import Path

from ingpipe_lib.testing import find_workspace_root

# The rule: authored and remote-supplied paths are judged with
# ingpipe_lib.paths.is_rooted_path, never Path.is_absolute(), because
# is_absolute() asks the HOST. See is_rooted_path's docstring for the three
# forms that are absolute under neither platform's rules yet still escape.
_BANNED_ATTRIBUTE = "is_absolute"
_REPLACEMENT = "ingpipe_lib.paths.is_rooted_path"

# paths.py is where the one legitimate call lives: is_rooted_path's own
# fallthrough in resolve_config_path, which deliberately honors the host's
# rules for a genuinely host-absolute config path.
_EXEMPT_FILES = {
    Path("packages/ingpipe_lib/src/ingpipe_lib/paths.py"),
}

_SEARCH_ROOTS = ("packages", "instances")


def _attribute_call_lines(source: str) -> list[int]:
    """Return the line numbers where ``.is_absolute`` is accessed as an attribute.

    Tokenizing rather than scanning lines is what lets the rule be EXPLAINED
    in the code it governs: the guards this rule replaced carry comments
    naming ``Path.is_absolute()``, and a line-based grep would flag its own
    documentation. Only real code tokens are considered, so comments,
    docstrings, and string literals are invisible here.

    Args:
        source: The Python source text of one module.

    Returns:
        The 1-based line numbers of each ``.is_absolute`` attribute access,
        in order.

    Raises:
        SyntaxError: If `source` cannot be tokenized, which would mean an
            unparseable module in the tree.
    """
    found: list[int] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    previous_was_dot = False
    for token in tokens:
        if token.type == tokenize.NAME and previous_was_dot and token.string == _BANNED_ATTRIBUTE:
            found.append(token.start[0])
        # Only OP/NAME sequencing matters; whitespace, comments and NL tokens
        # must not break the "dot then name" pair (`path .is_absolute()` and a
        # dot followed by a line continuation are both legal Python).
        if token.type in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT):
            continue
        previous_was_dot = token.type == tokenize.OP and token.string == "."
    return found


def test_no_module_calls_path_is_absolute() -> None:
    """`Path.is_absolute()` is host-dependent and is banned outside paths.py.

    The predicate returns a different answer for the same string on the two
    supported platforms, so every guard built on it validated something
    different on each machine. `is_rooted_path` applies both platforms' rules
    and is the only sanctioned way to judge an authored or remote-supplied
    path.
    """
    workspace_root = find_workspace_root(Path(__file__).resolve())
    assert workspace_root is not None, (
        "No uv workspace root above this test; the conventions sweep has nothing to walk."
    )

    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    scanned = 0
    for search_root in _SEARCH_ROOTS:
        for module in sorted((workspace_root / search_root).rglob("*.py")):
            relative = module.relative_to(workspace_root)
            if Path(*relative.parts) in _EXEMPT_FILES or module.resolve() == this_file:
                continue
            scanned += 1
            for line_number in _attribute_call_lines(module.read_text(encoding="utf-8")):
                offenders.append(f"{relative.as_posix()}:{line_number}")

    assert scanned > 0, "The conventions sweep found no modules to scan; the walk is broken."
    assert not offenders, (
        f"{len(offenders)} call(s) to .{_BANNED_ATTRIBUTE}() outside "
        f"{', '.join(sorted(p.as_posix() for p in _EXEMPT_FILES))}:\n  "
        + "\n  ".join(offenders)
        + f"\n\nPath.{_BANNED_ATTRIBUTE}() follows the rules of the HOST, so it gives a "
        "different answer for the same string on each supported platform: '/data/in' is "
        "absolute on POSIX only, 'C:/data' on Windows only, and 'C:data', 'D:data' and "
        f"'\\\\etc\\\\passwd' on neither -- though all three still escape a root they are "
        f"joined under. Use {_REPLACEMENT}(value) instead, passing path.as_posix() when "
        "you hold a Path, and normalize backslashes before any '..' check."
    )
