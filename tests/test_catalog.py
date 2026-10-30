"""Filtering, paging and CSV export, exercised through the HTTP API.

The dataset is built here instead of shipping one, so the run is independent of
whatever `seed` produced: one collection, fixed created_at values, and a pair of
assets stamped with the same second.
"""
import csv
import io
import os
import tempfile
import uuid
import zlib

import pytest

os.environ.setdefault("KEEPSTACK_DATA_DIR", tempfile.mkdtemp(prefix="keepstack-catalog-test-"))
os.environ["KEEPSTACK_AI_ENABLED"] = "false"
os.environ["KEEPSTACK_OCR_ENABLED"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from keepstack import ingest  # noqa: E402
from keepstack.app import app  # noqa: E402
from keepstack.db import get_conn, init_db, transaction  # noqa: E402

HEADER = ["uuid", "title", "description", "filename", "media_type", "tags", "created_at"]


def _jpeg(seed_text: str) -> bytes:
    digest = zlib.crc32(seed_text.encode())
    colour = (digest % 255, (digest >> 8) % 255, (digest >> 16) % 255)
    buf = io.BytesIO()
    Image.new("RGB", (48, 48), colour).save(buf, format="JPEG")
    return buf.getvalue()


def _make_asset(collection_id, title, tags=(), created_at="2026-01-01T00:00:00Z",
                description=None, filename=None):
    filename = filename or title.replace(" ", "_") + ".jpg"
    asset = ingest.ingest_stream(io.BytesIO(_jpeg(filename)), filename,
                                 run_ai=False, title=title)
    with transaction() as conn:
        conn.execute("UPDATE assets SET created_at = ?, updated_at = ?, description = ? WHERE id = ?",
                     (created_at, created_at, description, asset["id"]))
        for name in tags:
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
            conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) "
                         "VALUES (?,?, 'manual')", (asset["id"], tag_id))
        conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id) VALUES (?,?)",
                     (collection_id, asset["id"]))
    ingest.reindex_fts(asset["id"])
    return asset


def _new_collection(client) -> int:
    resp = client.post("/api/collections", json={"name": "Catalog " + uuid.uuid4().hex[:8]})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        resp = c.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert resp.status_code == 200, resp.text
        c.headers.update({"Authorization": "Bearer " + resp.json()["token"]})
        yield c


@pytest.fixture(scope="module")
def catalog(client):
    init_db()
    collection_id = _new_collection(client)
    assets = {
        # two assets on the same second: the tie that stable paging has to survive
        "alpha": _make_asset(collection_id, "Harbor alpha", ("approved", "campaign"), "2026-01-01T00:00:00Z"),
        "beta": _make_asset(collection_id, "Harbor beta", ("approved", "campaign"), "2026-01-01T00:00:00Z"),
        "gamma": _make_asset(collection_id, "Harbor gamma", ("approved", "campaign"), "2026-01-02T00:00:00Z"),
        "internal": _make_asset(collection_id, "Harbor delta", ("approved", "campaign", "internal"),
                                "2026-01-03T00:00:00Z"),
        "approved_only": _make_asset(collection_id, "Harbor epsilon", ("approved",), "2026-01-04T00:00:00Z"),
        "campaign_only": _make_asset(collection_id, "Harbor zeta", ("campaign",), "2026-01-05T00:00:00Z"),
        "no_keyword": _make_asset(collection_id, "Quay note", ("approved", "campaign"), "2026-01-06T00:00:00Z"),
    }
    # a non-image asset, so the media type filter has something to separate
    notes = ingest.ingest_stream(io.BytesIO(b"dock survey notes\n"), "dock_notes.txt",
                                 run_ai=False, title="Dock notes")
    with transaction() as conn:
        conn.execute("UPDATE assets SET created_at = ? WHERE id = ?", ("2026-01-07T00:00:00Z", notes["id"]))
        conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id) VALUES (?,?)",
                     (collection_id, notes["id"]))
    assets["notes"] = notes
    return {"collection": collection_id, "assets": assets}


def _scenario(catalog, **extra):
    params = {"q": "harbor", "collection_id": catalog["collection"],
              "include_tags": ["approved", "campaign"], "exclude_tags": ["internal"]}
    params.update(extra)
    return params


def _search(client, **params):
    resp = client.get("/api/assets", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _export(client, **params):
    resp = client.get("/api/export/assets.csv", params=params)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    return rows[0], rows[1:]


def test_combined_filters_keep_only_full_matches(client, catalog):
    data = _search(client, **_scenario(catalog))
    assert data["total"] == 3
    assert {i["title"] for i in data["items"]} == {"Harbor alpha", "Harbor beta", "Harbor gamma"}


def test_each_condition_narrows_the_result(client, catalog):
    base = {"q": "harbor", "collection_id": catalog["collection"]}
    assert _search(client, **base)["total"] == 6
    assert _search(client, **base, include_tags=["approved"])["total"] == 5
    assert _search(client, **base, include_tags=["approved", "campaign"])["total"] == 4
    assert _search(client, **base, include_tags=["approved", "campaign"],
                   exclude_tags=["internal"])["total"] == 3
    # the keyword alone does not cross the collection boundary
    assert _search(client, q="quay", collection_id=catalog["collection"])["total"] == 1


def test_same_tag_included_and_excluded_returns_nothing(client, catalog):
    data = _search(client, collection_id=catalog["collection"],
                   include_tags=["approved"], exclude_tags=["approved"])
    assert data["total"] == 0
    assert data["items"] == []


def test_repeated_tag_filters_do_not_change_the_result(client, catalog):
    base = _search(client, **_scenario(catalog))["total"]
    noisy = _search(client, **_scenario(
        catalog,
        include_tags=["approved", "campaign", "approved", "Campaign"],
        exclude_tags=["internal", "internal"],
    ))["total"]
    assert noisy == base == 3


def test_sort_applies_when_a_keyword_is_present(client, catalog):
    newest = _search(client, **_scenario(catalog, sort="newest", limit=10))["items"]
    oldest = _search(client, **_scenario(catalog, sort="oldest", limit=10))["items"]
    assert [i["title"] for i in newest] == ["Harbor gamma", "Harbor beta", "Harbor alpha"]
    assert newest == list(reversed(oldest))


def test_paging_covers_every_match_once(client, catalog):
    first = _search(client, **_scenario(catalog, limit=2, offset=0))
    assert first["total"] == 3 and first["pages"] == 2
    second = _search(client, **_scenario(catalog, limit=2, offset=2))

    walked = [i["uuid"] for i in first["items"]] + [i["uuid"] for i in second["items"]]
    whole = [i["uuid"] for i in _search(client, **_scenario(catalog, limit=10))["items"]]

    assert walked == whole
    assert len(set(walked)) == 3


def test_assets_with_the_same_created_at_order_stably(client, catalog):
    uuids = [catalog["assets"]["alpha"]["uuid"], catalog["assets"]["beta"]["uuid"]]
    stamps = {row["created_at"] for row in get_conn().execute(
        "SELECT created_at FROM assets WHERE uuid IN (?,?)", uuids)}
    assert len(stamps) == 1, "the fixture is supposed to contain a tie"

    def walk():
        order = []
        for offset in range(3):
            page = _search(client, **_scenario(catalog, limit=1, offset=offset))
            order += [i["uuid"] for i in page["items"]]
        return order

    assert walk() == walk()
    assert len(set(walk())) == 3


def test_past_the_last_page_is_empty_not_an_error(client, catalog):
    data = _search(client, **_scenario(catalog, limit=2, offset=8))
    assert data["total"] == 3
    assert data["items"] == []


def test_invalid_paging_is_rejected(client):
    assert client.get("/api/assets", params={"limit": 0}).status_code == 400
    assert client.get("/api/assets", params={"limit": 101}).status_code == 400
    assert client.get("/api/assets", params={"offset": -1}).status_code == 400


def test_media_type_filter_matches_the_list_and_the_export(client, catalog):
    documents = _search(client, collection_id=catalog["collection"], media_type="document")
    assert documents["total"] == 1
    assert documents["items"][0]["media_type"] == "document"

    assert _search(client, **_scenario(catalog, media_type="document"))["total"] == 0
    _, rows = _export(client, **_scenario(catalog, media_type="document"))
    assert rows == []


def test_export_returns_the_whole_result_in_list_order(client, catalog):
    header, rows = _export(client, **_scenario(catalog))
    page = _search(client, **_scenario(catalog, limit=2))
    whole = _search(client, **_scenario(catalog, limit=10))["items"]

    assert header == HEADER
    assert len(rows) == page["total"] == 3  # the 2-item page does not cap the file
    assert [r[0] for r in rows] == [i["uuid"] for i in whole]
    assert [r[1] for r in rows] == [i["title"] for i in whole]
    assert rows[0][5] == "approved;campaign"


def test_export_without_matches_still_writes_the_header(client, catalog):
    header, rows = _export(client, collection_id=catalog["collection"],
                           include_tags=["approved"], exclude_tags=["approved"])
    assert rows == []
    assert header == HEADER


def test_export_quotes_commas_quotes_and_newlines(client):
    collection_id = _new_collection(client)
    title = '港口, "quoted"'
    description = "第一行\n第二行, 带逗号"
    asset = _make_asset(collection_id, title, ("harbor",), "2026-02-01T00:00:00Z",
                        description=description, filename="port,gateway.jpg")

    header, rows = _export(client, collection_id=collection_id)
    assert header == HEADER
    assert len(rows) == 1
    assert rows[0][0] == asset["uuid"]
    assert rows[0][1] == title
    assert rows[0][2] == description
    assert rows[0][3] == "port,gateway.jpg"


def test_export_requires_a_login(client):
    resp = client.get("/api/export/assets.csv", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


def test_edits_show_up_in_the_next_query_and_export(client):
    collection_id = _new_collection(client)
    # the filename stays generic: the FTS index covers it too, and this test is
    # about the title being replaced
    asset = _make_asset(collection_id, "Draft harbor card", ("approved",), "2026-02-02T00:00:00Z",
                        filename="card.jpg")
    asset_uuid = asset["uuid"]

    resp = client.patch(f"/api/assets/{asset_uuid}",
                        json={"title": "Final dredging card", "description": "revised caption"})
    assert resp.status_code == 200
    assert _search(client, q="dredging", collection_id=collection_id)["total"] == 1
    assert _search(client, q="draft", collection_id=collection_id)["total"] == 0
    _, rows = _export(client, collection_id=collection_id)
    assert rows[0][1] == "Final dredging card"
    assert rows[0][2] == "revised caption"

    client.post(f"/api/assets/{asset_uuid}/tags", json={"name": "campaign"})
    assert _search(client, collection_id=collection_id,
                   include_tags=["approved", "campaign"])["total"] == 1
    _, rows = _export(client, collection_id=collection_id, include_tags=["approved", "campaign"])
    assert [r[0] for r in rows] == [asset_uuid]

    client.delete(f"/api/assets/{asset_uuid}/tags/campaign")
    assert _search(client, collection_id=collection_id,
                   include_tags=["approved", "campaign"])["total"] == 0
    _, rows = _export(client, collection_id=collection_id, include_tags=["approved", "campaign"])
    assert rows == []


def test_keyword_with_special_characters_is_handled(client, catalog):
    for q in ('harbor "alpha', "harbor OR NEAR(x", "harbor*", "***", "  "):
        resp = client.get("/api/assets", params={"q": q, "collection_id": catalog["collection"]})
        assert resp.status_code == 200, q

    # punctuation only means "no keyword", like an empty box
    assert _search(client, q="***", collection_id=catalog["collection"])["total"] == \
        _search(client, collection_id=catalog["collection"])["total"]


def test_no_matches_is_an_empty_page(client):
    data = _search(client, q="nothing-matches-this-token")
    assert data["total"] == 0
    assert data["items"] == []
    assert data["pages"] == 1
