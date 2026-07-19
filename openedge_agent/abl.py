"""ABL-aware parsing for the ingest pipeline.

Deliberately regex/state-machine based, not a grammar: Stage 1 needs
procedure-level structure (units, calls, includes, schema, table touches),
and every extraction here is a documented heuristic. Known, accepted limits:

- ``RUN VALUE(expr)`` is dynamic; recorded as an unresolved edge, never
  guessed at.
- Statement analysis is line-oriented; it is correct for this corpus's
  formatting and typical legacy styles, not for arbitrarily creative ABL.
- Comments (which nest in ABL) and quoted strings are blanked before any
  structural analysis, so code that survives only in comments — e.g. the
  pre-2007 inventory relief in ``oe-post.p`` — is invisible to the call
  graph and table-touch analysis *on purpose*. Raw chunk text keeps the
  comments, so text retrieval still sees them (that trap is eval case W1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# comment / string blanking
# ---------------------------------------------------------------------------


def strip_comments_and_strings(text: str) -> str:
    """Blank ABL comments (nested ``/* */``, ``//``) and quoted strings.

    Every non-newline character inside a comment or string becomes a space,
    so the result has identical length and line structure — line numbers and
    column positions survive.
    """
    out: list[str] = []
    i, n = 0, len(text)
    depth = 0            # comment nesting depth
    quote: str | None = None

    while i < n:
        c = text[i]
        two = text[i : i + 2]

        if depth > 0:  # inside a (possibly nested) comment
            if two == "/*":
                depth += 1
                out.append("  ")
                i += 2
            elif two == "*/":
                depth -= 1
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue

        if quote is not None:  # inside a string
            if c == "\n":  # ABL strings don't span lines; fail safe
                quote = None
                out.append("\n")
                i += 1
            elif c == "~" and i + 1 < n:  # tilde escapes next char
                out.append("  ")
                i += 2
            elif c == quote:
                if text[i + 1 : i + 2] == quote:  # doubled-quote escape
                    out.append("  ")
                    i += 2
                else:
                    quote = None
                    out.append(" ")
                    i += 1
            else:
                out.append(" ")
                i += 1
            continue

        # in plain code
        if two == "/*":
            depth = 1
            out.append("  ")
            i += 2
        elif two == "//":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif c in ('"', "'"):
            quote = c
            out.append(" ")
            i += 1
        else:
            out.append(c)
            i += 1

    return "".join(out)


# ---------------------------------------------------------------------------
# unit chunking (.p / .cls / .i)
# ---------------------------------------------------------------------------

_PROC_RE = re.compile(r"^\s*PROCEDURE\s+([\w-]+)\s*[:.]", re.IGNORECASE)
_FUNC_RE = re.compile(r"^\s*FUNCTION\s+([\w-]+)\s+RETURNS\b", re.IGNORECASE)
_METHOD_RE = re.compile(
    r"^\s*METHOD\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|STATIC|OVERRIDE)\s+)+"
    r"[\w.\[\]]+\s+([\w-]+)\s*\(",
    re.IGNORECASE,
)
_END_RE = re.compile(r"^\s*END\s+(PROCEDURE|FUNCTION|METHOD)\s*[.]", re.IGNORECASE)


@dataclass
class Unit:
    """One retrievable chunk of ABL code."""

    id: str            # e.g. corpus/oe/oe-credit.p#check-credit
    file: str          # repo-relative path
    name: str          # unit name, or "main", or "" for includes
    kind: str          # main | procedure | function | method | include
    segments: list[tuple[int, int]]  # 1-based inclusive line ranges
    text: str          # ORIGINAL text (comments kept — retrieval wants them)


def chunk_source(relpath: str, text: str) -> list[Unit]:
    """Split one source file into units.

    ``.i`` files are a single ``include`` chunk (id = bare path). ``.p`` and
    ``.cls`` files yield one chunk per internal PROCEDURE / FUNCTION /
    METHOD plus a ``#main`` chunk holding everything else (header comment,
    defines, main-block code, class scaffolding).

    A unit ends at its explicit ``END PROCEDURE/FUNCTION/METHOD.`` line;
    defensively, a new unit header also closes the previous unit (with the
    boundary line going to the new unit).
    """
    if relpath.lower().endswith(".i"):
        n = text.count("\n") + (0 if text.endswith("\n") else 1)
        return [Unit(relpath, relpath, "", "include", [(1, max(n, 1))], text)]

    lines = text.splitlines()
    stripped_lines = strip_comments_and_strings(text).splitlines()

    # pass 1: find unit spans on comment/string-blanked lines
    spans: list[tuple[str, str, int, int]] = []  # (kind, name, start, end) 1-based
    current: tuple[str, str, int] | None = None

    for idx, sline in enumerate(stripped_lines, start=1):
        if "FORWARD" in sline.upper():
            continue
        m = _PROC_RE.match(sline) or _FUNC_RE.match(sline) or _METHOD_RE.match(sline)
        if m:
            kind = (
                "procedure"
                if _PROC_RE.match(sline)
                else "function"
                if _FUNC_RE.match(sline)
                else "method"
            )
            if current is not None:  # defensive close of unterminated unit
                spans.append((current[0], current[1], current[2], idx - 1))
            current = (kind, m.group(1), idx)
            continue
        if current is not None and _END_RE.match(sline):
            spans.append((current[0], current[1], current[2], idx))
            current = None
    if current is not None:
        spans.append((current[0], current[1], current[2], len(lines)))

    covered = set()
    for _, _, s, e in spans:
        covered.update(range(s, e + 1))

    units: list[Unit] = []
    for kind, name, s, e in spans:
        units.append(
            Unit(
                id=f"{relpath}#{name}",
                file=relpath,
                name=name,
                kind=kind,
                segments=[(s, e)],
                text="\n".join(lines[s - 1 : e]),
            )
        )

    # main chunk: every line not inside a unit span, as contiguous segments
    main_segments: list[tuple[int, int]] = []
    start = None
    for ln in range(1, len(lines) + 1):
        if ln not in covered:
            if start is None:
                start = ln
        elif start is not None:
            main_segments.append((start, ln - 1))
            start = None
    if start is not None:
        main_segments.append((start, len(lines)))

    main_text = "\n".join("\n".join(lines[s - 1 : e]) for s, e in main_segments)
    if main_text.strip():
        units.insert(
            0,
            Unit(
                id=f"{relpath}#main",
                file=relpath,
                name="main",
                kind="main",
                segments=main_segments,
                text=main_text,
            ),
        )
    return units


# ---------------------------------------------------------------------------
# .df schema parsing
# ---------------------------------------------------------------------------

_ADD_RE = re.compile(
    r'^ADD\s+(SEQUENCE|TABLE|FIELD|INDEX)\s+"([^"]+)"'
    r'(?:\s+O[FN]\s+"([^"]+)")?'
    r'(?:\s+AS\s+(\S+))?'
)
_PROP_RE = re.compile(r"^\s+([A-Z][A-Z-]*)(?:\s+(.*))?$")


def parse_df(text: str) -> dict:
    """Parse a Data Dictionary dump into tables / sequences structures."""
    tables: dict[str, dict] = {}
    sequences: dict[str, dict] = {}
    target: dict | None = None

    for raw in text.splitlines():
        if raw.strip() == ".":  # trailer starts
            break
        m = _ADD_RE.match(raw)
        if m:
            kind, name, owner, astype = m.groups()
            if kind == "SEQUENCE":
                target = sequences.setdefault(name, {"name": name})
            elif kind == "TABLE":
                target = tables.setdefault(
                    name, {"name": name, "fields": [], "indexes": []}
                )
            elif kind == "FIELD":
                target = {"name": name, "type": astype}
                tables[owner]["fields"].append(target)
            elif kind == "INDEX":
                target = {"name": name, "unique": False, "primary": False, "fields": []}
                tables[owner]["indexes"].append(target)
            continue
        if target is None or not raw.strip():
            continue
        p = _PROP_RE.match(raw)
        if not p:
            continue
        key, val = p.group(1), (p.group(2) or "").strip().strip('"')
        if key == "INDEX-FIELD":
            target["fields"].append(val.split()[0].strip('"'))
        elif key in ("UNIQUE", "PRIMARY", "MANDATORY"):
            target[key.lower()] = True
        else:
            target[key.lower().replace("-", "_")] = val

    return {"tables": tables, "sequences": sequences}


def render_table_text(t: dict) -> str:
    """Human/embedding-readable rendering of one table."""
    parts = [f"Table {t['name']}"]
    if t.get("description"):
        parts.append(f"- {t['description']}.")
    fs = []
    for f in t["fields"]:
        d = f" ({f['description']})" if f.get("description") else ""
        ext = f" extent {f['extent']}" if f.get("extent") else ""
        fs.append(f"{f['name']} ({f['type']}{ext}){d}")
    parts.append("Fields: " + "; ".join(fs) + ".")
    ix = []
    for i in t["indexes"]:
        flags = ("unique " if i["unique"] else "") + ("primary " if i["primary"] else "")
        ix.append(f"{i['name']} ({flags}on {', '.join(i['fields'])})")
    if ix:
        parts.append("Indexes: " + "; ".join(ix) + ".")
    return " ".join(parts)


def render_sequence_text(s: dict, used_by: list[str]) -> str:
    txt = (
        f"Sequence {s['name']} (initial {s.get('initial', '?')}, "
        f"increment {s.get('increment', '?')})."
    )
    if used_by:
        txt += " Referenced via NEXT-VALUE by: " + ", ".join(sorted(used_by)) + "."
    return txt


# ---------------------------------------------------------------------------
# call / include / sequence-ref extraction  (on blanked text)
# ---------------------------------------------------------------------------

_RUN_RE = re.compile(r"\bRUN\s+([\w][\w./\\-]*)", re.IGNORECASE)
_IN_HANDLE_RE = re.compile(r"\bIN\s+([\w-]+)", re.IGNORECASE)
_INCLUDE_RE = re.compile(r"\{\s*([\w][\w./\\-]*\.i)\b", re.IGNORECASE)
_NEXTVAL_RE = re.compile(r"\bNEXT-VALUE\s*\(\s*([\w-]+)\s*\)", re.IGNORECASE)
_PERSIST_RE = re.compile(r"^\s*PERSISTENT\b", re.IGNORECASE)


def extract_calls(stripped_unit_text: str, functions_in_file: list[str]) -> list[dict]:
    """Extract call edges from one unit's blanked text.

    Edge dicts: {type, target, persistent?} where type is one of
    run_external | run_internal | run_in_handle | run_dynamic | function_call.
    Targets are raw (e.g. ``oe/oe-credit.p`` or an internal name); the
    ingest layer resolves them to unit ids where possible.
    """
    edges: list[dict] = []
    text = stripped_unit_text

    for m in _RUN_RE.finditer(text):
        target = m.group(1).rstrip(".")  # trailing '.' = statement terminator
        rest = text[m.end() : m.end() + 40]
        if target.upper() == "VALUE":
            edges.append({"type": "run_dynamic", "target": None})
        elif re.search(r"\.(p|w|r)$", target, re.IGNORECASE):
            edges.append(
                {
                    "type": "run_external",
                    "target": target.replace("\\", "/"),
                    "persistent": bool(_PERSIST_RE.match(rest.lstrip())),
                }
            )
        else:
            hm = _IN_HANDLE_RE.match(rest.lstrip())
            if hm:
                edges.append(
                    {"type": "run_in_handle", "target": target, "handle": hm.group(1)}
                )
            else:
                edges.append({"type": "run_internal", "target": target})

    for fn in functions_in_file:
        # a call = the name followed by "(", not on its FUNCTION definition line
        for m in re.finditer(rf"\b{re.escape(fn)}\s*\(", text, re.IGNORECASE):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_end = text.find("\n", m.start())
            line = text[line_start : line_end if line_end != -1 else len(text)]
            if re.match(r"\s*FUNCTION\b", line, re.IGNORECASE):
                continue
            edges.append({"type": "function_call", "target": fn})
            break  # one edge per (unit, function) pair is enough

    return edges


def extract_includes(stripped_text: str) -> list[str]:
    return sorted({m.group(1).replace("\\", "/") for m in _INCLUDE_RE.finditer(stripped_text)})


def extract_sequence_refs(stripped_text: str) -> list[str]:
    return sorted({m.group(1) for m in _NEXTVAL_RE.finditer(stripped_text)})


# ---------------------------------------------------------------------------
# table read / write analysis  (line-heuristic on blanked text)
# ---------------------------------------------------------------------------

_BUFFER_RE = re.compile(
    r"\bDEFINE\s+(?:\{1\}\s+)?(?:NEW\s+)?(?:GLOBAL\s+)?(?:SHARED\s+)?BUFFER\s+"
    r"([\w-]+)\s+FOR\s+([\w-]+)",
    re.IGNORECASE,
)
_CREATE_RE = re.compile(r"^\s*CREATE\s+([\w-]+)\s*\.", re.IGNORECASE)
_ASSIGN_LINE_RE = re.compile(r"^\s*(?:ASSIGN\s+)?([\w-]+)\.([\w-]+)\s*=")


def buffer_aliases(stripped_text: str, known_tables: set[str]) -> dict[str, str]:
    """alias -> table map from DEFINE BUFFER statements."""
    out = {}
    for m in _BUFFER_RE.finditer(stripped_text):
        alias, tbl = m.group(1), m.group(2)
        real = next((t for t in known_tables if t.lower() == tbl.lower()), None)
        if real:
            out[alias.lower()] = real
    return out


def table_touches(
    stripped_unit_text: str, known_tables: set[str], aliases: dict[str, str]
) -> dict:
    """Which tables a unit references, and which it writes.

    Write heuristic (documented, line-oriented): a ``CREATE table.``
    statement, or a line matching ``[ASSIGN] name.field = ...`` at line
    start — in ABL that shape *is* an assignment statement (bare ``x = y.``
    is legal), and it also catches every continuation line of a multi-line
    ASSIGN list. WHERE/AND/OR/IF-prefixed lines never match because the
    pattern is anchored at line start. Known limit: a WHERE clause wrapped
    so that ``table.field = ...`` begins a line would false-positive; that
    formatting does not occur in this corpus.
    """

    def resolve(name: str) -> str | None:
        if name.lower() in aliases:
            return aliases[name.lower()]
        return next((t for t in known_tables if t.lower() == name.lower()), None)

    referenced: set[str] = set()
    created: set[str] = set()
    updated: set[str] = set()

    lowered = stripped_unit_text.lower()
    for t in known_tables:
        if re.search(rf"\b{re.escape(t.lower())}\b", lowered):
            referenced.add(t)
    for alias, t in aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            referenced.add(t)

    for line in stripped_unit_text.splitlines():
        cm = _CREATE_RE.match(line)
        if cm:
            r = resolve(cm.group(1))
            if r:
                created.add(r)
            continue
        am = _ASSIGN_LINE_RE.match(line)
        if am:
            r = resolve(am.group(1))
            if r:
                updated.add(r)

    return {
        "referenced": sorted(referenced),
        "written": sorted(created | updated),
        "created": sorted(created),
        "updated": sorted(updated),
    }
