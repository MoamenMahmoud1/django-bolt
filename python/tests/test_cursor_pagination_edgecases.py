"""Cursor ordering edge cases: ties, legacy cursors, timestamps, and values()."""

import base64
import json
from urllib.parse import quote

import pytest
from django.utils import timezone

from django_bolt import BoltAPI, CursorPagination, paginate
from django_bolt.testing import TestClient

from .test_models import Article

PREFIX = "cursor-edge-"


def _seed():
    Article.objects.bulk_create(
        [
            Article(
                title=f"{PREFIX}{i:02d}",
                content="cursor correctness probe",
                author="A" if i < 12 else "B",
            )
            for i in range(18)
        ]
    )


def _api(ordering, projection="model", page_size=3):
    api = BoltAPI()

    class ProbePagination(CursorPagination):
        pass

    ProbePagination.ordering = ordering
    ProbePagination.page_size = page_size

    @api.get("/cursor-edge")
    @paginate(ProbePagination)
    async def list_articles(request):
        qs = Article.objects.filter(title__startswith=PREFIX)
        if projection == "values_no_pk":
            return qs.values("title", "author")
        if projection == "values_with_pk":
            return qs.values("id", "title", "author")
        return qs

    return api


def _walk(client, *, key, max_pages=30):
    seen = []
    cursor = None
    for _ in range(max_pages):
        url = "/cursor-edge"
        if cursor is not None:
            url += f"?cursor={quote(cursor, safe='')}"
        response = client.get(url)
        assert response.status_code == 200, response.text
        data = response.json()
        seen.extend(row[key] for row in data["items"])
        if not data["has_next"]:
            assert data.get("next_cursor") is None
            return seen
        cursor = data.get("next_cursor")
        assert cursor, "has_next=True but next_cursor is missing"
    pytest.fail("Pagination did not terminate")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["author", "-author"])
def test_duplicate_ordering_models_complete(ordering):
    _seed()
    expected = set(Article.objects.filter(title__startswith=PREFIX).values_list("id", flat=True))
    with TestClient(_api(ordering)) as client:
        found = _walk(client, key="id")
    assert len(found) == len(expected)
    assert set(found) == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("projection", ["values_no_pk", "values_with_pk"])
def test_values_projection_complete(projection):
    _seed()
    expected = {f"{PREFIX}{i:02d}" for i in range(18)}
    with TestClient(_api("author", projection)) as client:
        found = _walk(client, key="title")
        first = client.get("/cursor-edge").json()["items"]
    assert len(found) == len(expected)
    assert set(found) == expected
    expected_fields = {"title", "author"} if projection == "values_no_pk" else {"id", "title", "author"}
    assert all(set(row) == expected_fields for row in first), "Projection leaked or lost fields"


@pytest.mark.django_db(transaction=True)
def test_default_pk_cursor_wire_format_unchanged():
    _seed()
    with TestClient(_api("-id")) as client:
        response = client.get("/cursor-edge")
    assert response.status_code == 200
    cursor = response.json()["next_cursor"]
    payload = json.loads(base64.b64decode(cursor))
    assert set(payload) == {"v"}
    assert isinstance(payload["v"], int), payload


@pytest.mark.django_db(transaction=True)
def test_legacy_nonunique_cursor_still_accepted():
    _seed()
    legacy = base64.b64encode(json.dumps({"v": "A"}).encode()).decode()
    with TestClient(_api("author")) as client:
        response = client.get("/cursor-edge?cursor=" + quote(legacy, safe=""))
    assert response.status_code == 200
    assert response.json()["items"][0]["author"] == "B"


@pytest.mark.django_db(transaction=True)
def test_datetime_ties_complete():
    _seed()
    Article.objects.filter(title__startswith=PREFIX).update(created_at=timezone.now())
    expected = set(Article.objects.filter(title__startswith=PREFIX).values_list("id", flat=True))
    with TestClient(_api("-created_at")) as client:
        found = _walk(client, key="id")
    assert len(found) == len(expected)
    assert set(found) == expected
