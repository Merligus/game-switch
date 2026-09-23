"""Minimal read-only parser for Valve's text VDF / ACF format.

Read-only on purpose: appmanifest files are moved byte-for-byte and never
rewritten, so the app can never corrupt one.
"""
from __future__ import annotations

from pathlib import Path

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "r": "\r"}
_WORD_STOP = set(' \t\r\n{}"')


def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c in "{}":
            yield ("brace", c)
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], "\\" + text[i + 1]))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            yield ("str", "".join(buf))
            continue
        j = i
        while j < n and text[j] not in _WORD_STOP:
            j += 1
        yield ("str", text[i:j])
        i = j


def loads(text: str) -> dict:
    toks = list(_tokenize(text))
    pos = 0

    def parse_obj() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(toks):
            kind, val = toks[pos]
            if kind == "brace":
                pos += 1
                if val == "}":
                    return obj
                continue
            key = val
            pos += 1
            if pos >= len(toks):
                break
            k2, v2 = toks[pos]
            if k2 == "brace" and v2 == "{":
                pos += 1
                obj[key] = parse_obj()
            else:
                obj[key] = v2
                pos += 1
        return obj

    return parse_obj()


def load(path: Path) -> dict:
    return loads(path.read_text(encoding="utf-8", errors="replace"))


def get_ci(d: dict, key: str, default=None):
    """VDF key casing is inconsistent across Steam versions."""
    if not isinstance(d, dict):
        return default
    if key in d:
        return d[key]
    low = key.lower()
    for k, v in d.items():
        if k.lower() == low:
            return v
    return default
