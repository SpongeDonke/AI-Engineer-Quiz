"""Search and discovery.

Three complementary modes over one catalog:

  * keyword  - FTS5 full-text across title, description, tags, metadata
  * filter   - faceted structured filters (media type, tags, collection, status)
  * semantic - embedding cosine ranking ("natural language" / more-like-this)

Facet counts are returned alongside results so the UI can render a live,
drill-down sidebar.
"""
from __future__ import annotations

import math
from typing import Optional

from . import ai
from .db import get_conn

DEFAULT_LIMIT = 12
MAX_LIMIT = 100

# Every order ends with the primary key, so assets created in the same second
# still sort deterministically and consecutive pages never overlap.
SORTS = {
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id ASC",
}

EXPORT_COLUMNS = ("uuid", "title", "description", "filename", "media_type", "tags", "created_at")
_EXPORT_FIELDS = ("uuid", "title", "description", "filename", "media_type", "created_at")


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def _tag_names(tags) -> list[str]:
    """Normalise tags the way the rest of the app stores them: lower case,
    trimmed, no duplicates, blank entries dropped."""
    out: list[str] = []
    for raw in tags or ():
        name = (raw or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


def _where(
    *,
    q: str,
    media_type: Optional[str],
    include_tags: list[str],
    exclude_tags: list[str],
    collection_id: Optional[int],
    status: str,
) -> tuple[str, list]:
    where = ["a.status = ?"]
    params: list = [status]

    match = _fts_query(q) if q and q.strip() else ""
    if match:
        where.append("a.id IN (SELECT rowid FROM asset_fts WHERE asset_fts MATCH ?)")
        params.append(match)
    if media_type:
        where.append("a.media_type = ?")
        params.append(media_type)
    if collection_id:
        where.append("EXISTS (SELECT 1 FROM collection_assets ca "
                     "WHERE ca.asset_id = a.id AND ca.collection_id = ?)")
        params.append(collection_id)

    # EXISTS rather than a join: an asset with several matching tags stays one
    # row, which keeps COUNT(*) and LIMIT/OFFSET honest.
    for name in include_tags:
        where.append("EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                     "WHERE at.asset_id = a.id AND t.name = ?)")
        params.append(name)
    if exclude_tags:
        marks = ",".join("?" for _ in exclude_tags)
        where.append("NOT EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                     f"WHERE at.asset_id = a.id AND t.name IN ({marks}))")
        params.extend(exclude_tags)

    return " AND ".join(where), params


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tag: Optional[str] = None,
    include_tags=(),
    exclude_tags=(),
    collection_id: Optional[int] = None,
    status: str = "active",
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict:
    include = _tag_names(list(include_tags) + ([tag] if tag else []))
    exclude = _tag_names(exclude_tags)

    # Semantic mode ranks a candidate pool by embedding similarity. It reuses
    # the structured filters but not the keyword condition, which it replaces.
    if mode == "semantic" and q.strip():
        where_sql, params = _where(q="", media_type=media_type, include_tags=include,
                                   exclude_tags=exclude, collection_id=collection_id,
                                   status=status)
        return _semantic_search(q, where_sql, params, status, limit, offset)

    where_sql, params = _where(q=q, media_type=media_type, include_tags=include,
                               exclude_tags=exclude, collection_id=collection_id,
                               status=status)
    order = SORTS.get(sort, SORTS["newest"])
    conn = get_conn()

    total = conn.execute(
        f"SELECT COUNT(*) AS c FROM assets a WHERE {where_sql}", params
    ).fetchone()["c"]

    rows = conn.execute(
        f"SELECT a.* FROM assets a WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(status),
        "mode": mode,
        "limit": limit,
        "offset": offset,
        "page": offset // limit + 1,
        "pages": max(1, math.ceil(total / limit)),
    }


def export_rows(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    include_tags=(),
    exclude_tags=(),
    collection_id: Optional[int] = None,
    sort: str = "newest",
) -> list[dict]:
    """Every matching active asset in list order, plus its tag names.

    Tags are collected in the same statement (a left join keeps the asset order
    the list uses) and folded into one row per asset here.
    """
    where_sql, params = _where(q=q, media_type=media_type,
                               include_tags=_tag_names(include_tags),
                               exclude_tags=_tag_names(exclude_tags),
                               collection_id=collection_id, status="active")
    order = SORTS.get(sort, SORTS["newest"])

    rows = get_conn().execute(
        f"""SELECT a.uuid, a.title, a.description, a.filename, a.media_type,
                   a.created_at, t.name AS tag
            FROM assets a
            LEFT JOIN asset_tags at ON at.asset_id = a.id
            LEFT JOIN tags t ON t.id = at.tag_id
            WHERE {where_sql}
            ORDER BY {order}, t.name""",
        params,
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        if not out or out[-1]["uuid"] != row["uuid"]:
            item = {field: row[field] for field in _EXPORT_FIELDS}
            item["tags"] = []
            out.append(item)
        if row["tag"]:
            out[-1]["tags"].append(row["tag"])
    return out


def _semantic_search(q, where_sql, params, status, limit, offset) -> dict:
    conn = get_conn()
    qvec = ai.embed(q)
    rows = conn.execute(
        f"SELECT a.* FROM assets a WHERE {where_sql} AND a.embedding IS NOT NULL", params
    ).fetchall()
    scored = []
    for r in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(r["embedding"]))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    page = scored[offset:offset + limit]
    items = []
    for score, r in page:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        items.append(card)
    total = len(scored)
    return {"total": total, "items": items, "facets": _facets(status), "mode": "semantic",
            "limit": limit, "offset": offset, "page": offset // limit + 1,
            "pages": max(1, math.ceil(total / limit))}


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets(status: str) -> dict:
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = ? GROUP BY media_type ORDER BY c DESC",
        (status,),
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = ?
           GROUP BY t.name ORDER BY c DESC LIMIT 25""",
        (status,),
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
