"""
Motet - Onboarding Documentation Sample Validation

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-21

Description:
    Static validation of the Python samples in docs/developer_onboarding/ against
    the runtime they document.

    This exists because the docs cannot be kept honest by review alone. Command
    data models are declared with ``extra="ignore"``, so a sample that passes a
    keyword the model does not define raises nothing at all — the argument is
    silently dropped and the command runs on defaults. A reader who copies that
    sample gets plausible wrong behavior instead of a traceback, and a reviewer
    reading carefully cannot tell the difference. Only a mechanical check can.

    Every check is static (``ast`` only). Nothing in a doc sample is executed,
    so a snippet that would delete data or call a provider is safe to validate.

    Checks:
      1. Every ``python`` block parses.
      2. Every ``motet`` / ``motet_sdk`` import in a sample resolves.
      3. Every ``XData(...)`` keyword exists on the real Pydantic model.
      4. Every ``motet.<helper>.<method>()`` exists on the real helper class.
      5. Every ``MOTET_*`` env var is a settings field or appears in runtime source.
      6. Every decorated command takes an annotated data model as its first
         parameter — ``def cmd(motet, data)`` reads naturally but never runs.
      7. Every keyword passed to a ``motet.<helper>.<method>()`` is one the
         helper actually reads, rather than one it drops on the floor.
      8. Every method called on an instance of a documented class exists on it.

    Docs legitimately use idioms that are not valid Python — ``...`` meaning "and
    so on" inside an argument list, and dict entries shown without their enclosing
    braces. Those are normalized before parsing rather than being reported. A
    sample that is deliberately invalid (illustrating a mistake, or pseudocode)
    opts out with an HTML comment on the line before the fence:

        <!-- docs-validate: skip -->

Dependencies:
    - pytest: parametrized cases so each doc reports independently
    - motet.core.commands.command_data_classes: the models samples instantiate
    - motet.core.commands.motet_context: the helper classes samples call
    - motet.core.config: the settings backing MOTET_* environment variables

Usage:
    pytest tests/unit/docs/test_onboarding_doc_samples.py -q

Notes:
    - Import resolution only covers first-party modules. Third-party imports in
      samples are ignored, since the docs may reference optional extras.
    - The env var check accepts any name appearing literally in runtime source,
      because plenty are read through os.environ rather than the settings class.
      Pydantic's ``env_prefix="MOTET_"`` means a settings-backed variable never
      appears literally anywhere, so both sources are needed.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import pathlib
import re
import textwrap
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DOCS_DIR = REPO_ROOT / "docs" / "developer_onboarding"

FENCE_RE = re.compile(r"^```(\w+)?[^\n]*\n(.*?)^```", re.S | re.M)
SKIP_MARKER = "<!-- docs-validate: skip -->"

# Docs write `...` to mean "and so on". That is not valid Python inside a call or
# a literal, so remove those elisions before parsing.
_ELISIONS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r",\s*\.\.\.\s*(?=[)\]}])"), ""),
    (re.compile(r"(?<=[(\[{])\s*\.\.\.\s*,\s*"), ""),
    (re.compile(r"(?<=[(\[{])\s*\.\.\.\s*(?=[)\]}])"), ""),
]

_LONE_ELLIPSIS_RE = re.compile(r"^[ \t]*\.\.\.[ \t]*,?[ \t]*(?:#[^\n]*)?$")
_OPENERS, _CLOSERS = "([{", ")]}"


def _drop_bracketed_ellipsis_lines(source: str) -> str:
    """Remove `...` continuation lines that sit inside an open bracket.

    A lone `...` is only a documentation elision when it stands in for more
    arguments or entries. At statement level it is a real function body
    (``def f(): ...``), so depth has to be tracked rather than pattern-matched.
    """
    kept, depth = [], 0
    for line in source.splitlines():
        if depth > 0 and _LONE_ELLIPSIS_RE.match(line):
            continue
        kept.append(line)
        for char in line.split("#", 1)[0]:
            if char in _OPENERS:
                depth += 1
            elif char in _CLOSERS:
                depth = max(0, depth - 1)
    return "\n".join(kept)


# motet.<attr> -> helper class name in motet.core.commands.motet_context
_HELPER_CLASSES: Dict[str, str] = {
    "tools": "MotetToolsHelper",
    "memory": "MotetMemoryHelper",
    "agents": "MotetAgentsHelper",
    "models": "MotetModelsHelper",
    "workflows": "MotetWorkflowsHelper",
    "schedules": "MotetSchedulesHelper",
    "commands": "MotetCommandsHelper",
    "conversations": "MotetConversationsHelper",
}


class Block(NamedTuple):
    """A fenced python sample lifted out of a markdown doc."""

    doc: str
    line: int
    source: str

    def where(self) -> str:
        return f"{self.doc}:{self.line}"


def _strip_elisions(source: str) -> str:
    source = _drop_bracketed_ellipsis_lines(source)
    previous = None
    while previous != source:
        previous = source
        for pattern, replacement in _ELISIONS:
            source = pattern.sub(replacement, source)
    return source


def parse_sample(source: str) -> ast.Module:
    """Parse a doc sample, tolerating documentation-only idioms.

    Raises SyntaxError if the sample is not salvageable, which is a real finding.
    """
    normalized = _strip_elisions(source)
    try:
        return ast.parse(normalized)
    except SyntaxError:
        # Dict entries are often shown without their enclosing braces.
        return ast.parse("{" + normalized + "}")


def iter_blocks() -> Iterator[Block]:
    for path in sorted(DOCS_DIR.glob("[0-9]*.md")):
        text = path.read_text(encoding="utf-8")
        for match in FENCE_RE.finditer(text):
            if (match.group(1) or "").lower() != "python":
                continue
            preceding = text[: match.start()].rstrip().rsplit("\n", 1)[-1]
            if SKIP_MARKER in preceding:
                continue
            line = text[: match.start()].count("\n") + 1
            yield Block(path.name, line, match.group(2))


ALL_BLOCKS: List[Block] = list(iter_blocks())


@functools.lru_cache(maxsize=1)
def _blocks_by_doc() -> Dict[str, List[Block]]:
    grouped: Dict[str, List[Block]] = {}
    for block in ALL_BLOCKS:
        grouped.setdefault(block.doc, []).append(block)
    return grouped


DOC_NAMES = sorted(_blocks_by_doc())


def test_documentation_directory_is_present() -> None:
    """Guard against the suite silently passing because the glob found nothing."""
    assert DOCS_DIR.is_dir(), f"missing docs directory: {DOCS_DIR}"
    assert len(ALL_BLOCKS) > 100, f"expected many python samples, found {len(ALL_BLOCKS)}"


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_python_samples_parse(doc: str) -> None:
    """Every python sample is syntactically valid Python."""
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            parse_sample(block.source)
        except SyntaxError as exc:
            failures.append(f"{block.where()}: {exc.msg}")
    assert not failures, "unparseable python samples:\n  " + "\n  ".join(failures)


# Modules the tutorials instruct the reader to create. They are absent from the
# repo on purpose, so an import of one is correct documentation rather than drift.
_TUTORIAL_MODULES: Set[str] = {
    "motet.core.commands.builtin.text_analysis",
}


def _first_party(module: str) -> bool:
    return module.split(".", 1)[0] in {"motet", "motet_sdk"}


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_first_party_imports_resolve(doc: str) -> None:
    """Every motet / motet_sdk import in a sample points at something real."""
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue  # reported by test_python_samples_parse
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.level or not _first_party(node.module):
                continue
            if node.module in _TUTORIAL_MODULES:
                continue
            try:
                module = importlib.import_module(node.module)
            except Exception as exc:  # noqa: BLE001 - any failure is a doc bug
                failures.append(f"{block.where()}: cannot import {node.module} ({type(exc).__name__})")
                continue
            for alias in node.names:
                if alias.name != "*" and not hasattr(module, alias.name):
                    failures.append(f"{block.where()}: {node.module} has no {alias.name!r}")
    assert not failures, "unresolvable first-party imports:\n  " + "\n  ".join(failures)


@functools.lru_cache(maxsize=1)
def _command_data_models() -> Dict[str, type]:
    module = importlib.import_module("motet.core.commands.command_data_classes")
    models = {}
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            models[name] = obj
    return models


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_command_data_keywords_exist(doc: str) -> None:
    """Keywords passed to XData(...) exist on the model.

    This is the check that matters most: extra="ignore" means a wrong keyword is
    dropped in silence rather than raising, so nothing else catches it.
    """
    models = _command_data_models()
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            model = models.get(node.func.id)
            if model is None:
                continue
            allowed = set(model.model_fields) | {
                alias for f in model.model_fields.values() if (alias := f.alias)
            }
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in allowed:
                    failures.append(
                        f"{block.where()}: {node.func.id}({keyword.arg}=...) "
                        f"is not a field (silently ignored at runtime)"
                    )
    assert not failures, "unknown command-data keywords:\n  " + "\n  ".join(failures)


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_context_helper_methods_exist(doc: str) -> None:
    """motet.<helper>.<method>() names a method that exists on the helper."""
    context = importlib.import_module("motet.core.commands.motet_context")
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
                continue
            inner = node.value
            if not isinstance(inner.value, ast.Name) or inner.value.id != "motet":
                continue
            helper_cls_name = _HELPER_CLASSES.get(inner.attr)
            if helper_cls_name is None:
                continue
            helper_cls = getattr(context, helper_cls_name, None)
            if helper_cls is None:
                continue
            if node.attr.startswith("_") or hasattr(helper_cls, node.attr):
                continue
            failures.append(
                f"{block.where()}: motet.{inner.attr}.{node.attr} does not exist "
                f"on {helper_cls_name}"
            )
    assert not failures, "unknown context helper methods:\n  " + "\n  ".join(failures)


def _is_command_decorator(node: ast.expr) -> bool:
    """True for @motet.command(...), @distributed_command(...) and bare forms."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr == "command" and isinstance(target.value, ast.Name)
    return isinstance(target, ast.Name) and target.id == "distributed_command"


@functools.lru_cache(maxsize=None)
def _helper_accepted_kwargs(helper_cls_name: str, method: str) -> Optional[frozenset]:
    """Keyword names a helper method actually reads, or None if it takes no **kwargs.

    Helpers such as ``memory.recall`` declare a few explicit parameters and then
    pull named extras out of ``**kwargs`` with ``kwargs.get("mode")``. Anything
    not read that way is dropped on the floor — no TypeError, no validation
    error — so ``recall(include_vector=True)`` silently returns keyword results
    and the caller never learns the flag did nothing.
    """
    context = importlib.import_module("motet.core.commands.motet_context")
    helper_cls = getattr(context, helper_cls_name, None)
    func = getattr(helper_cls, method, None)
    if func is None:
        return None
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    except (OSError, TypeError, SyntaxError):
        return None

    fn = tree.body[0]
    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.args.kwarg is None:
        return None

    kwarg_name = fn.args.kwarg.arg
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs} - {"self"}

    named: Set[str] = set()
    splat_targets: List[Optional[str]] = []
    for node in ast.walk(fn):
        # kwargs.get("name") / kwargs.pop("name")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            if (
                isinstance(owner, ast.Name)
                and owner.id == kwarg_name
                and node.func.attr in {"get", "pop"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                named.add(node.args[0].value)
        # kwargs["name"]
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == kwarg_name and isinstance(node.slice, ast.Constant):
                named.add(node.slice.value)
        # f(**kwargs)
        if isinstance(node, ast.Call) and any(
            k.arg is None and isinstance(k.value, ast.Name) and k.value.id == kwarg_name
            for k in node.keywords
        ):
            target = node.func
            splat_targets.append(
                target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
            )

    # Reading specific names is the deliberate contract: the helper picks what it
    # forwards to the command and drops the rest. Trust that over any splat,
    # which in these helpers is the local fallback path rather than the delegated
    # one a reader will actually hit in a worker.
    if named:
        return frozenset(params | named)

    # Otherwise the bag goes somewhere wholesale. Union the model's fields when
    # that target is a data model; stay quiet when it cannot be resolved.
    models = _command_data_models()
    accepted = set(params)
    for name in splat_targets:
        model = models.get(name) if name else None
        if model is None:
            return None
        accepted |= set(model.model_fields)
    return frozenset(accepted)


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_context_helper_kwargs_are_read(doc: str) -> None:
    """Keywords passed to motet.<helper>.<method>() are names the helper reads."""
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            inner = node.func.value
            if not isinstance(inner, ast.Attribute) or not isinstance(inner.value, ast.Name):
                continue
            if inner.value.id != "motet":
                continue
            helper_cls_name = _HELPER_CLASSES.get(inner.attr)
            if helper_cls_name is None:
                continue
            accepted = _helper_accepted_kwargs(helper_cls_name, node.func.attr)
            if accepted is None:
                continue
            for kw in node.keywords:
                if kw.arg is None or kw.arg in accepted:
                    continue
                failures.append(
                    f"{block.where()}: motet.{inner.attr}.{node.func.attr}({kw.arg}=...) "
                    f"is silently ignored; the helper never reads it"
                )
    assert not failures, "ignored helper keywords:\n  " + "\n  ".join(failures)


# Pages teach by contrast, so a deliberately broken signature sits under a
# "wrong" marker. Flagging those would punish the docs for making the point.
_COUNTEREXAMPLE_RE = re.compile(r"❌|\bWRONG\b|\bDon't\b|\bDo not\b", re.IGNORECASE)


def _is_counterexample(lines: List[str], node: ast.AST) -> bool:
    """True when a marker just above the definition labels it as the bad way."""
    first = min(
        [node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]
    )
    start = max(0, first - 4)
    return any(_COUNTEREXAMPLE_RE.search(line) for line in lines[start:first])


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_command_signatures_are_valid(doc: str) -> None:
    """A decorated command's first parameter is the data model, and is annotated.

    The decorator binds params[0] as the data parameter and raises
    ValueError("Data parameter '<name>' must have a type hint") without an
    annotation, so `def cmd(motet, data)` fails at import even though it reads
    naturally. Samples written that way look correct and never run.
    """
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue
        lines = block.source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not any(_is_command_decorator(d) for d in node.decorator_list):
                continue
            if _is_counterexample(lines, node):
                continue
            params = node.args.args
            if not params:
                failures.append(f"{block.where()}: {node.name}() takes no data parameter")
                continue
            first = params[0]
            if first.arg == "motet":
                failures.append(
                    f"{block.where()}: {node.name}(motet, ...) has the parameters "
                    f"reversed; the data model must come first"
                )
            elif first.annotation is None:
                failures.append(
                    f"{block.where()}: {node.name}() data parameter "
                    f"'{first.arg}' needs a type hint"
                )
    assert not failures, "invalid command signatures:\n  " + "\n  ".join(failures)


def _imported_symbols(tree: ast.AST) -> Dict[str, Any]:
    """Map local name -> first-party object, for `from motet... import X` in a block."""
    found: Dict[str, Any] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if not _first_party(node.module):
            continue
        try:
            module = importlib.import_module(node.module)
        except Exception:
            continue
        for alias in node.names:
            obj = getattr(module, alias.name, None)
            if obj is not None:
                found[alias.asname or alias.name] = obj
    return found


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_methods_on_documented_classes_exist(doc: str) -> None:
    """`m = SomeClass()` then `m.method()` names a method the class really has.

    Catches the case where the class is real and imported correctly but the
    method was invented — which reads as authoritative precisely because the
    surrounding code checks out.
    """
    failures = []
    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue
        symbols = _imported_symbols(tree)
        if not symbols:
            continue

        # Locals bound directly to a constructor call of an imported class.
        instances: Dict[str, Any] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if not isinstance(func, ast.Name) or func.id not in symbols:
                continue
            cls = symbols[func.id]
            if not inspect.isclass(cls):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    instances[target.id] = cls

        lines = block.source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or owner.id not in instances:
                continue
            cls = instances[owner.id]
            method = node.func.attr
            if method.startswith("_") or hasattr(cls, method):
                continue
            if _is_counterexample(lines, node):
                continue
            failures.append(
                f"{block.where()}: {owner.id}.{method}() does not exist on "
                f"{cls.__name__}"
            )
    assert not failures, "unknown methods:\n  " + "\n  ".join(failures)


_ENV_RE = re.compile(r"\bMOTET_[A-Z0-9_]+\b")

# `$MOTET_API` in a curl sample is a shell variable the reader exports for
# convenience, not a setting the runtime reads.
_SHELL_ENV_RE = re.compile(r"\$\{?MOTET_[A-Z0-9_]+")

# Families whose full names are assembled at runtime from a user-chosen suffix,
# so no complete literal ever appears in source (e.g. MOTET_IMAGE_STACK_<NAME>).
_DYNAMIC_ENV_PREFIXES = ("MOTET_IMAGE_STACK_",)

_ENV_SOURCE_DIRS = ("motet", "motet-sdk", "tests", "scripts", "hosting")
_ENV_SOURCE_FILES = ("docker-compose.distributed.yml", "docker-compose.test.yml")


@functools.lru_cache(maxsize=1)
def _known_env_vars() -> Set[str]:
    """Names settable as MOTET_*, from settings fields plus literals in source.

    Both sources are required. Pydantic's ``env_prefix="MOTET_"`` means a
    settings-backed variable never appears literally anywhere in the tree, while
    plenty of others are read straight from os.environ and have no field.
    """
    known: Set[str] = set()

    config = importlib.import_module("motet.core.config")
    for attr in dir(config):
        obj = getattr(config, attr)
        fields = getattr(obj, "model_fields", None)
        if isinstance(obj, type) and fields:
            known |= {f"MOTET_{name.upper()}" for name in fields}

    for directory in _ENV_SOURCE_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            known |= set(_ENV_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    for name in _ENV_SOURCE_FILES:
        candidate = REPO_ROOT / name
        if candidate.exists():
            known |= set(_ENV_RE.findall(candidate.read_text(encoding="utf-8", errors="ignore")))
    return known


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_env_vars_are_real(doc: str) -> None:
    """Every MOTET_* variable named in a doc is settable somewhere in the runtime."""
    known = _known_env_vars()
    text = (DOCS_DIR / doc).read_text(encoding="utf-8")
    shell_vars = {m.lstrip("${") for m in _SHELL_ENV_RE.findall(text)}

    unknown = sorted(
        {
            name
            for name in _ENV_RE.findall(text)
            if name not in known
            and name not in shell_vars
            and not name.startswith(_DYNAMIC_ENV_PREFIXES)
        }
    )
    assert not unknown, (
        f"{doc} documents MOTET_* variables that no runtime code reads:\n  "
        + "\n  ".join(unknown)
    )


_BUILTIN_TOOL_DIR = REPO_ROOT / "motet" / "core" / "tools" / "builtin"
_TOOL_SCHEMA_RE = re.compile(
    r"class \w*(?:Params|Schema)\w*\(BaseModel\):\n(.*?)(?=\n(?:class |def |@))", re.S
)
_TOOL_FIELD_RE = re.compile(r"^\s{4}(\w+)\s*:", re.M)


@functools.lru_cache(maxsize=None)
def _builtin_tool_params(tool: str) -> Optional[frozenset]:
    """Parameter names on a builtin tool's Pydantic schema, or None if it has none."""
    path = _BUILTIN_TOOL_DIR / f"{tool}.py"
    if not path.exists():
        return None
    match = _TOOL_SCHEMA_RE.search(path.read_text(encoding="utf-8"))
    if not match:
        return None
    return frozenset(_TOOL_FIELD_RE.findall(match.group(1)))


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_builtin_tool_parameters_exist(doc: str) -> None:
    """Parameters passed to a `core.*` builtin tool are real fields on its schema.

    A wrong key here fails at call time rather than import time, so nothing else
    in this suite would notice it. Only the top-level keys of the parameter dict
    are checked — a nested dict belongs to the target tool, as with the inner
    ``parameters`` of a ``core.tool_call``.
    """
    problems: List[str] = []

    for block in _blocks_by_doc()[doc]:
        try:
            tree = parse_sample(block.source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and len(node.args) >= 2):
                continue
            target, params = node.args[0], node.args[1]
            if not (isinstance(target, ast.Constant) and isinstance(target.value, str)):
                continue
            if not target.value.startswith("core."):
                continue
            if not isinstance(params, ast.Dict):
                continue

            accepted = _builtin_tool_params(target.value.removeprefix("core."))
            if not accepted:
                continue
            for key in params.keys:
                if isinstance(key, ast.Constant) and key.value not in accepted:
                    problems.append(
                        f"{block.where()}: {target.value}({key.value}=...) "
                        f"— accepts {sorted(accepted)}"
                    )

    assert not problems, (
        f"{doc} passes parameters no builtin tool schema declares:\n  "
        + "\n  ".join(sorted(set(problems)))
    )


_ROUTE_ROW_RE = re.compile(r"\|\s*`?(GET|POST|PUT|PATCH|DELETE)`?\s*\|\s*`([^`]+)`")
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


@functools.lru_cache(maxsize=1)
def _declared_routes() -> Set[Tuple[str, str]]:
    """Every (method, path) declared by an @router.<verb> decorator under interfaces/api."""
    routes: Set[Tuple[str, str]] = set()
    api_dir = REPO_ROOT / "motet" / "interfaces" / "api"
    for path in api_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        prefix = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "APIRouter":
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = str(kw.value.value)

        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []):
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                method = dec.func.attr.upper()
                if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    continue
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    routes.add((method, prefix + str(dec.args[0].value)))
    return routes


def _normalize_route(path: str) -> str:
    """Collapse path parameter names so {id} and {schedule_id} compare equal."""
    return _PATH_PARAM_RE.sub("{}", path.rstrip("/"))


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_documented_endpoints_exist(doc: str) -> None:
    """Every `| METHOD | /api/... |` table row corresponds to a declared route.

    Endpoint tables are the part of the docs a reader is most likely to copy
    verbatim into a client, so a path that drifted is an integration failure
    rather than a cosmetic error.
    """
    declared = {(m, _normalize_route(p)) for m, p in _declared_routes()}
    text = (DOCS_DIR / doc).read_text(encoding="utf-8")

    missing = sorted(
        f"{method} {path}"
        for method, path in _ROUTE_ROW_RE.findall(text)
        if path.startswith("/api/") and (method, _normalize_route(path)) not in declared
    )
    assert not missing, (
        f"{doc} documents endpoints with no matching route under "
        f"motet/interfaces/api:\n  " + "\n  ".join(missing)
    )


_BASH_BLOCK_RE = re.compile(r"```(?:bash|shell|sh)\n(.*?)^```", re.S | re.M)
_CONTINUATION_RE = re.compile(r"\\\n\s*")
_URL_RE = re.compile(r"https?://[^\s'\"\\]+")
_METHOD_FLAG_RE = re.compile(r"-X\s+(GET|POST|PUT|PATCH|DELETE)")
_BODY_FLAG_RE = re.compile(
    r"(?:^|\s)(?:-d|--data(?:-raw|-binary)?|-F|--form)\s"
)


def _route_matchers() -> List[Tuple[str, "re.Pattern[str]"]]:
    """Declared routes as regexes, {param} widened to match any value."""
    matchers = []
    for method, path in _declared_routes():
        parts = [
            re.escape(p) for p in _PATH_PARAM_RE.split(path.rstrip("/"))
        ]
        pattern = "^" + "[^/]+".join(parts) + "/?$"
        matchers.append((method, re.compile(pattern)))
    return matchers


@pytest.mark.parametrize("doc", DOC_NAMES)
def test_curl_endpoints_exist(doc: str) -> None:
    """Every curl against /api/... in a shell block hits a declared route.

    The endpoint tables were already checked, but the copy-paste examples were
    not, and those are the ones a reader actually runs. This is the check that
    catches a path invented for the prose rather than read off the router.
    """
    matchers = _route_matchers()
    text = (DOCS_DIR / doc).read_text(encoding="utf-8")

    missing = []
    for block in _BASH_BLOCK_RE.findall(text):
        for line in _CONTINUATION_RE.sub(" ", block).splitlines():
            if "curl" not in line:
                continue
            url = _URL_RE.search(line)
            if not url:
                continue
            path = urlsplit(url.group(0)).path.rstrip("/")
            if not path.startswith("/api/"):
                continue
            method_flag = _METHOD_FLAG_RE.search(line)
            if method_flag:
                method = method_flag.group(1)
            elif _BODY_FLAG_RE.search(line):
                method = "POST"  # curl's default once it is given a body
            else:
                # No method to infer. Illustrative examples (TLS, auth
                # headers) omit it, so only check the path exists.
                method = None

            matched = any(
                (method is None or m == method) and p.match(path)
                for m, p in matchers
            )
            if not matched:
                missing.append(f"{method or 'ANY'} {path}")

    assert not missing, (
        f"{doc} has curl examples with no matching route under "
        f"motet/interfaces/api:\n  " + "\n  ".join(sorted(set(missing)))
    )
