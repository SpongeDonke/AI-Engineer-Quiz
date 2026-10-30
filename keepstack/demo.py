"""A fixed demo catalog for the campaign filtering scenario.

``python -m keepstack demo`` appends a small, deterministic set of assets to an
existing repository so the sample task can be reproduced by hand: inside one
collection, search for "harbor", keep what is tagged `approved` *and*
`campaign`, and drop anything tagged `internal`.

Two of the matches share a created_at on purpose - that is what makes stable
paging observable across pages. Nothing here touches the seeded 24 assets.
"""
from __future__ import annotations

import io
import uuid as uuidlib

from PIL import Image, ImageDraw

from . import ingest
from .db import get_conn, init_db, transaction

COLLECTION = "Harbor Campaign"

# title, tags
_ASSETS = [
    ("Harbor at dawn", ("approved", "campaign")),
    ("Harbor crane in the mist", ("approved", "campaign")),
    ("Harbor pilot boat", ("approved", "campaign")),
    ("Harbor warehouse rooftops", ("approved", "campaign")),
    ("Harbor lighthouse, north pier", ("approved", "campaign")),
    ("Harbor dredging survey", ("approved", "campaign")),
    ("Harbor market stalls", ("approved", "campaign")),
    ("Harbor ferry queue", ("approved", "campaign")),
    ("Harbor container stacks", ("approved", "campaign")),
    ("Harbor breakwater repair", ("approved", "campaign")),
    ("Harbor tugboat manoeuvre", ("approved", "campaign")),
    ("Harbor map, 1890 survey", ("approved", "campaign")),
    ("Harbor night signal lights", ("approved", "campaign")),
    ("Harbor customs office", ("approved", "campaign")),
    ("Harbor security briefing", ("approved", "campaign", "internal")),
    ("Harbor staff badge", ("approved", "campaign", "internal")),
    ("Harbor ticket counter", ("approved",)),
    ("Quay office window", ("campaign",)),
]


def _created_at(index: int) -> str:
    """Ascending minutes, except the first two, which tie."""
    minute = 0 if index < 2 else index
    return f"2026-03-01T09:{minute:02d}:00Z"


def _description(index: int) -> str:
    if index == 0:
        # commas, quotes and a newline, so the exported CSV has to quote it
        return 'Harbor at first light, with a crane hook "in frame"\nand a barge passing behind.'
    return f"Demo asset {index + 1} for the harbor campaign walkthrough."


def _image(index: int) -> bytes:
    """Distinct bytes per asset: identical ones would be deduplicated on ingest."""
    img = Image.new("RGB", (480, 360), (30 + index * 11 % 200, 40 + index * 23 % 200, 80 + index * 7 % 160))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 240, 440, 300], fill=(20, 24, 32))
    draw.ellipse([80 + index * 13 % 200, 40, 220 + index * 13 % 200, 180], fill=(230, 235, 245))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def run_demo() -> None:
    init_db()
    conn = get_conn()
    admin = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()

    row = conn.execute("SELECT id FROM collections WHERE name = ?", (COLLECTION,)).fetchone()
    if row:
        collection_id = row["id"]
    else:
        with transaction() as tx:
            collection_id = tx.execute(
                "INSERT INTO collections (uuid, name, description, kind, created_by, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (str(uuidlib.uuid4()), COLLECTION,
                 "Demo data for the filter and export walkthrough", "folder",
                 admin["id"] if admin else None, _created_at(0)),
            ).lastrowid

    added = 0
    for index, (title, tags) in enumerate(_ASSETS):
        created_at = _created_at(index)
        asset = ingest.ingest_stream(
            io.BytesIO(_image(index)), f"harbor_{index + 1:02d}.jpg",
            user=dict(admin) if admin else None, run_ai=False, title=title,
        )
        if asset.get("duplicate"):
            continue  # a previous `demo` run already added this asset
        with transaction() as tx:
            tx.execute(
                "UPDATE assets SET created_at = ?, updated_at = ?, description = ? WHERE id = ?",
                (created_at, created_at, _description(index), asset["id"]),
            )
            for name in tags:
                tx.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                tag_id = tx.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
                tx.execute(
                    "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?, 'manual')",
                    (asset["id"], tag_id),
                )
            tx.execute(
                "INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) VALUES (?,?,?)",
                (collection_id, asset["id"], created_at),
            )
        ingest.reindex_fts(asset["id"])
        added += 1

    print(f"Demo catalog ready: {added} new assets in collection '{COLLECTION}'.")
    print("Library -> collection 'Harbor Campaign', search 'harbor', "
          "include approved + campaign, exclude internal.")
