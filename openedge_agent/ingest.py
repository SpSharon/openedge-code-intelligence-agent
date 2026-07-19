"""Ingest: synthetic ABL corpus -> index/ artifacts.

Outputs (all JSON except the embedding matrix):
    index/chunks.json     retrievable units (code chunks, includes) with text
    index/schema.json     structured tables/sequences + their unit texts
    index/callgraph.json  resolved/unresolved call edges, includes, sequence
                          refs, per-unit table touches
    index/embeddings.npy  unit embedding matrix (with --embed, the default)
    index/embed_ids.json  row order for embeddings.npy

Corpus policy: ``corpus/README.md`` is meta-documentation about the corpus
being synthetic and is deliberately NOT indexed; the eval answer key lives in
``evals/`` which the indexer never reads.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import abl

CODE_SUFFIXES = {".p", ".cls", ".i"}


def find_corpus_files(corpus_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in corpus_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in CODE_SUFFIXES
    )


def build_index(repo_root: Path, corpus: str = "corpus", index: str = "index",
                embed: bool = True) -> dict:
    """Run the full ingest. Returns a summary dict (also printed by CLI)."""
    corpus_dir = repo_root / corpus
    index_dir = repo_root / index
    index_dir.mkdir(exist_ok=True)

    # ---- schema ----
    df_files = sorted(corpus_dir.rglob("*.df"))
    schema = {"tables": {}, "sequences": {}}
    for df in df_files:
        parsed = abl.parse_df(df.read_text())
        schema["tables"].update(parsed["tables"])
        schema["sequences"].update(parsed["sequences"])
    known_tables = set(schema["tables"])

    # ---- chunk code files ----
    files: dict[str, dict] = {}  # relpath -> {text, stripped, units, ...}
    all_units: dict[str, abl.Unit] = {}
    for path in find_corpus_files(corpus_dir):
        rel = path.relative_to(repo_root).as_posix()
        text = path.read_text()
        stripped = abl.strip_comments_and_strings(text)
        units = abl.chunk_source(rel, text)
        files[rel] = {
            "text": text,
            "stripped": stripped,
            "stripped_lines": stripped.splitlines(),
            "units": units,
            "functions": [u.name for u in units if u.kind == "function"],
            "includes": abl.extract_includes(stripped),
        }
        for u in units:
            all_units[u.id] = u

    # include-aware buffer aliases per file
    for rel, f in files.items():
        aliases = abl.buffer_aliases(f["stripped"], known_tables)
        for inc in f["includes"]:
            inc_rel = f"{corpus}/{inc}"
            if inc_rel in files:
                aliases.update(
                    abl.buffer_aliases(files[inc_rel]["stripped"], known_tables)
                )
        f["aliases"] = aliases

    def stripped_unit_text(rel: str, unit: abl.Unit) -> str:
        lines = files[rel]["stripped_lines"]
        return "\n".join(
            "\n".join(lines[s - 1 : e]) for s, e in unit.segments
        )

    # ---- edges, sequence refs, table touches ----
    edges: list[dict] = []
    seq_refs: dict[str, list[str]] = {}   # unit id -> [sequence names]
    touches: dict[str, dict] = {}         # unit id -> {referenced, written}
    proc_units_by_name: dict[str, list[str]] = {}
    for uid, u in all_units.items():
        if u.kind == "procedure":
            proc_units_by_name.setdefault(u.name.lower(), []).append(uid)

    for rel, f in files.items():
        for u in f["units"]:
            su_text = stripped_unit_text(rel, u)
            if u.kind != "include":
                touches[u.id] = abl.table_touches(su_text, known_tables, f["aliases"])
            sq = abl.extract_sequence_refs(su_text)
            if sq:
                seq_refs[u.id] = sq
            for raw in abl.extract_calls(su_text, f["functions"]):
                edge = {"from": u.id, "type": raw["type"], "resolved": None}
                if raw["type"] == "run_external":
                    cand = f"{corpus}/{raw['target']}"
                    edge["target_raw"] = raw["target"]
                    edge["persistent"] = raw.get("persistent", False)
                    if cand in files:
                        edge["resolved"] = cand
                elif raw["type"] == "run_internal":
                    edge["target_raw"] = raw["target"]
                    cand = f"{rel}#{raw['target']}"
                    if cand in all_units:
                        edge["resolved"] = cand
                elif raw["type"] == "run_in_handle":
                    edge["target_raw"] = raw["target"]
                    edge["handle"] = raw.get("handle")
                    matches = proc_units_by_name.get(raw["target"].lower(), [])
                    if len(matches) == 1:
                        edge["resolved"] = matches[0]
                    edge["candidates"] = matches
                elif raw["type"] == "function_call":
                    edge["target_raw"] = raw["target"]
                    edge["resolved"] = f"{rel}#{raw['target']}"
                elif raw["type"] == "run_dynamic":
                    edge["target_raw"] = None  # RUN VALUE(...) - never guessed
                edges.append(edge)
            # include refs as edges too
            for inc in abl.extract_includes(su_text):
                inc_rel = f"{corpus}/{inc}"
                edges.append(
                    {
                        "from": u.id,
                        "type": "include",
                        "target_raw": inc,
                        "resolved": inc_rel if inc_rel in files else None,
                    }
                )

    # ---- cross-reference maps (generated metadata; never mixed into the
    #      original source text — stored in a separate "xref" field) ----
    def short(uid: str) -> str:
        return uid.replace(f"{corpus}/", "")

    calls_out: dict[str, set[str]] = {}
    called_by: dict[str, set[str]] = {}
    included_by: dict[str, set[str]] = {}
    for e in edges:
        if not e.get("resolved"):
            continue
        if e["type"] == "include":
            included_by.setdefault(e["resolved"], set()).add(e["from"])
        elif e["type"] in ("run_external", "run_internal", "run_in_handle",
                           "function_call"):
            calls_out.setdefault(e["from"], set()).add(e["resolved"])
            target_unit = e["resolved"]
            if e["type"] == "run_external":  # external RUN enters the main block
                cand = f"{e['resolved']}#main"
                target_unit = cand if cand in all_units else e["resolved"]
            called_by.setdefault(target_unit, set()).add(e["from"])

    def xref_text(uid: str) -> str:
        parts = []
        if uid in calls_out:
            parts.append("calls: " + ", ".join(sorted(short(x) for x in calls_out[uid])))
        if uid in called_by:
            parts.append(
                "called by: " + ", ".join(sorted(short(x) for x in called_by[uid]))
            )
        if uid in included_by:
            parts.append(
                "included by: " + ", ".join(sorted(short(x) for x in included_by[uid]))
            )
        t = touches.get(uid)
        if t:
            if t["created"]:
                parts.append("creates records in: " + ", ".join(t["created"]))
            if t["updated"]:
                parts.append("updates: " + ", ".join(t["updated"]))
        return "[x-ref] " + "; ".join(parts) + "." if parts else ""

    # ---- schema unit texts (with code cross-references) ----
    schema_units: list[dict] = []
    for tname, t in schema["tables"].items():
        creators = sorted(short(uid) for uid, tt in touches.items()
                          if tname in tt["created"])
        updaters = sorted(short(uid) for uid, tt in touches.items()
                          if tname in tt["updated"])
        readers = sorted({short(all_units[uid].file) for uid, tt in touches.items()
                          if tname in tt["referenced"]})
        text = abl.render_table_text(t)
        if creators:
            text += " Records created by: " + ", ".join(creators) + "."
        if updaters:
            text += " Updated by: " + ", ".join(updaters) + "."
        if readers:
            text += " Referenced in: " + ", ".join(readers) + "."
        schema_units.append({"id": f"schema:{tname}", "kind": "table", "text": text})
    for sname, s in schema["sequences"].items():
        used_by = sorted(uid for uid, names in seq_refs.items() if sname in names)
        text = abl.render_sequence_text(s, [short(u) for u in used_by])
        # quote the actual referencing source lines - gives the sequence unit
        # the vocabulary of its call sites (e.g. "shared order number")
        for uid in used_by:
            u = all_units[uid]
            for line in u.text.splitlines():
                if re.search(rf"NEXT-VALUE\s*\(\s*{re.escape(sname)}\s*\)", line,
                             re.IGNORECASE):
                    text += f' Usage in {short(uid)}: "{line.strip()}"'
        # .df field descriptions that mention the sequence by name
        for tname, t in schema["tables"].items():
            for fld in t["fields"]:
                desc = fld.get("description", "")
                if sname in desc:
                    text += f' Field {tname}.{fld["name"]}: "{desc}".'
        schema_units.append(
            {"id": f"schema:{sname}", "kind": "sequence", "text": text}
        )

    # ---- write artifacts ----
    chunk_records = []
    for uid, u in sorted(all_units.items()):
        rec = {
            "id": uid,
            "file": u.file,
            "name": u.name,
            "kind": u.kind,
            "segments": u.segments,
            "text": u.text,
            "xref": xref_text(uid),
        }
        chunk_records.append(rec)

    (index_dir / "chunks.json").write_text(json.dumps(chunk_records, indent=1))
    (index_dir / "schema.json").write_text(
        json.dumps({"structured": schema, "units": schema_units}, indent=1)
    )
    (index_dir / "callgraph.json").write_text(
        json.dumps(
            {
                "edges": edges,
                "sequence_refs": seq_refs,
                "table_touches": touches,
                "notes": "run_dynamic edges (RUN VALUE) are never resolved; "
                "commented-out code is invisible here by design.",
            },
            indent=1,
        )
    )

    summary = {
        "files": len(files),
        "units": len(all_units),
        "schema_units": len(schema_units),
        "tables": len(schema["tables"]),
        "sequences": len(schema["sequences"]),
        "edges": len(edges),
        "unresolved_edges": sum(
            1 for e in edges if e["resolved"] is None and e["type"] != "run_dynamic"
        ),
        "dynamic_edges": sum(1 for e in edges if e["type"] == "run_dynamic"),
    }

    if embed:
        from .retrieve import build_embeddings, compose_embed_text

        ids, texts = [], []
        for rec in chunk_records:
            ids.append(rec["id"])
            texts.append(compose_embed_text(rec))
        for su in schema_units:
            ids.append(su["id"])
            texts.append(su["text"])
        meta = build_embeddings(ids, texts, index_dir)
        summary["embedded_units"] = meta["units"]
        summary["embedder"] = meta["backend"]

    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--no-embed", action="store_true",
                    help="skip the embedding matrix (parsing artifacts only)")
    args = ap.parse_args()
    summary = build_index(Path(args.root).resolve(), embed=not args.no_embed)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
