"""Load YAML strictly: reject duplicate keys and remember the line of every key."""

from __future__ import annotations

from typing import Any, Dict, Tuple

import yaml

from .findings import Path, format_path


class LoadError(Exception):
    def __init__(self, message: str, line: int | None = None, path: Path = ()):
        super().__init__(message)
        self.message = message
        self.line = line
        self.path = path


def load(text: str) -> Tuple[Any, Dict[Path, int]]:
    """Return (document, {path: line}). Raises LoadError on bad YAML or duplicate keys."""
    loader = yaml.SafeLoader(text)
    lines: Dict[Path, int] = {}
    try:
        try:
            node = loader.get_single_node()
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            raise LoadError(f"not valid YAML: {exc}", mark.line + 1 if mark else None) from None
        if node is None:
            raise LoadError("the file is empty")

        def walk(n: yaml.Node, path: Path) -> Any:
            if isinstance(n, yaml.MappingNode):
                out: Dict[Any, Any] = {}
                for key_node, value_node in n.value:
                    key = loader.construct_object(key_node, deep=True)
                    key_path = path + (key,)
                    if key in out:
                        raise LoadError(
                            f"duplicate key '{key}' in {format_path(path)}; YAML keeps only the last one",
                            key_node.start_mark.line + 1,
                            key_path,
                        )
                    lines[key_path] = key_node.start_mark.line + 1
                    out[key] = walk(value_node, key_path)
                return out
            if isinstance(n, yaml.SequenceNode):
                items = []
                for i, value_node in enumerate(n.value):
                    lines[path + (i,)] = value_node.start_mark.line + 1
                    items.append(walk(value_node, path + (i,)))
                return items
            return loader.construct_object(n, deep=True)

        return walk(node, ()), lines
    finally:
        loader.dispose()
