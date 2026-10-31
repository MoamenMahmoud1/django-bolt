"""Cursor pagination across values_list(), aggregates, and nullable orderings."""

from urllib.parse import quote

import pytest
from django.db import connection, models
from django.db.models import Case, Count, F, IntegerField, Value, When
from django.db.models.functions import Cast

from django_bolt import BoltAPI, CursorPagination, paginate
from django_bolt.testing import TestClient

from .test_models import Article, Author, BlogPost


class _JSONOrderingProbe(models.Model):
    payload = models.JSONField(null=True)

    class Meta:
        app_label = "django_bolt"
        managed = False


def _seed():
    Article.objects.bulk_create(
        [
            Article(title=f"cursor-querysets-{i:02d}", content="cursor probe", author="A" if i < 12 else "B")
            for i in range(18)
        ]
    )


def _walk(api, page_size=3):
    items, cursor = [], None
    with TestClient(api) as client:
        for _ in range(25):
            path = "/cursor-querysets"
            if cursor is not None:
                path += "?cursor=" + quote(cursor, safe="")
            response = client.get(path)
            assert response.status_code == 200, response.text
            data = response.json()
            assert len(data["items"]) <= page_size
            items.extend(data["items"])
            if not data["has_next"]:
                assert data.get("next_cursor") is None
                return items
            cursor = data.get("next_cursor")
            assert cursor, "has_next=True without a usable next cursor"
    pytest.fail("Cursor pagination did not terminate")


def _api(ordering, kind, page_size=3):
    api = BoltAPI()

    class Page(CursorPagination):
        pass

    Page.ordering = ordering
    Page.page_size = page_size

    @api.get("/cursor-querysets")
    @paginate(Page)
    async def list_articles(request):
        qs = Article.objects.filter(title__startswith="cursor-querysets-")
        if kind == "tuple":
            return qs.values_list("title", "author")
        if kind == "named":
            return qs.values_list("title", "author", named=True)
        if kind == "flat":
            return qs.values_list("author", flat=True)
        if kind == "flat_distinct":
            return qs.values_list("author", flat=True).distinct()
        if kind == "distinct":
            return qs.values("author").distinct()
        if kind == "grouped":
            return qs.values("author").annotate(total=Count("id"))
        if kind == "all_null":
            return qs.annotate(nullable_sort=Value(None, output_field=IntegerField()))
        if kind == "mixed_null":
            return qs.annotate(
                nullable_sort=Case(
                    When(author="A", then=Value(None)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
        return qs

    return api


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["tuple", "named", "flat"])
def test_values_list_cursor_advances_and_preserves_projection(kind):
    _seed()
    ordering = "author"
    rows = _walk(_api(ordering, kind))
    assert len(rows) == 18
    if kind == "flat":
        assert rows.count("A") == 12
        assert rows.count("B") == 6
    else:
        titles = [row[0] if isinstance(row, list) else row["title"] for row in rows]
        assert set(titles) == {f"cursor-querysets-{i:02d}" for i in range(18)}
        assert all(len(row) == 2 for row in rows)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["distinct", "grouped", "flat_distinct"])
def test_grouped_or_distinct_pages_keep_meaning(kind):
    _seed()
    rows = _walk(_api("author", kind, page_size=1), page_size=1)
    if kind == "flat_distinct":
        assert rows == ["A", "B"]
    elif kind == "distinct":
        assert rows == [{"author": "A"}, {"author": "B"}]
    else:
        assert rows == [{"author": "A", "total": 12}, {"author": "B", "total": 6}]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["nullable_sort", "-nullable_sort"])
@pytest.mark.parametrize("kind", ["all_null", "mixed_null"])
def test_nullable_composite_cursor_traverses_all_rows(ordering, kind):
    _seed()
    rows = _walk(_api(ordering, kind))
    ids = [row["id"] for row in rows]
    expected = set(Article.objects.filter(title__startswith="cursor-querysets-").values_list("id", flat=True))
    assert len(ids) == len(expected) == 18
    assert set(ids) == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "fields",
    [("pk", "title"), ()],
    ids=["pk_alias", "all_fields"],
)
def test_values_list_pk_alias_and_default_fields(fields):
    _seed()

    expected_ids = set(Article.objects.filter(title__startswith="cursor-querysets-").values_list("id", flat=True))

    api = BoltAPI()

    class PKCursor(CursorPagination):
        ordering = "-id"
        page_size = 3

    @api.get("/cursor-querysets")
    @paginate(PKCursor)
    async def list_articles(request):
        return Article.objects.filter(title__startswith="cursor-querysets-").values_list(*fields)

    rows = _walk(api, page_size=3)
    returned_ids = [row[0] for row in rows]

    assert len(returned_ids) == len(expected_ids)
    assert len(set(returned_ids)) == len(returned_ids)
    assert set(returned_ids) == expected_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "projection",
    ["distinct_multiple_fields", "grouped_multiple_fields", "missing_ordering_field"],
)
async def test_unsupported_cursor_projections_raise_clear_errors(projection):
    queryset = Article.objects.all()
    if projection == "distinct_multiple_fields":
        queryset = queryset.values("author", "title").distinct()
    elif projection == "grouped_multiple_fields":
        queryset = queryset.values("author", "title").annotate(total=Count("id"))
    else:
        queryset = queryset.values("title")

    class ByAuthor(CursorPagination):
        ordering = "author"

    expected_error = (
        "DISTINCT/GROUP BY" if projection != "missing_ordering_field" else "projection must include its ordering field"
    )
    with pytest.raises(ValueError, match=expected_error):
        await ByAuthor().paginate_queryset(queryset, {"query": {}})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["distinct", "grouped"])
@pytest.mark.parametrize("ordering", ["nullable_sort", "-nullable_sort"])
def test_grouped_distinct_nullable_cursor_advances(kind, ordering):
    _seed()
    api = BoltAPI()

    class NullableCursor(CursorPagination):
        page_size = 1

    NullableCursor.ordering = ordering

    @api.get("/cursor-querysets")
    @paginate(NullableCursor)
    async def list_articles(request):
        queryset = (
            Article.objects.filter(title__startswith="cursor-querysets-")
            .annotate(
                nullable_sort=Case(
                    When(author="A", then=Value(None)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .values("nullable_sort")
        )
        if kind == "distinct":
            return queryset.distinct()
        return queryset.annotate(total=Count("id"))

    rows = _walk(api, page_size=1)

    assert len(rows) == 2
    assert {row["nullable_sort"] for row in rows} == {None, 1}

    if kind == "grouped":
        counts = {row["nullable_sort"]: row["total"] for row in rows}
        assert counts == {None: 12, 1: 6}


@pytest.mark.django_db(transaction=True)
def test_values_list_all_fields_with_annotation_cursor_advances():
    _seed()
    api = BoltAPI()

    class AnnotatedCursor(CursorPagination):
        ordering = "sort_rank"
        page_size = 3

    @api.get("/cursor-querysets")
    @paginate(AnnotatedCursor)
    async def list_articles(request):
        return (
            Article.objects.filter(title__startswith="cursor-querysets-")
            .annotate(
                sort_rank=Case(
                    When(author="A", then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            )
            .values_list()
        )

    rows = _walk(api, page_size=3)
    returned_ids = [row[0] for row in rows]

    expected_ids = set(Article.objects.filter(title__startswith="cursor-querysets-").values_list("id", flat=True))

    assert len(returned_ids) == len(expected_ids) == 18
    assert set(returned_ids) == expected_ids


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ordering", ["author", "-author", "author_id", "-author_id"])
@pytest.mark.parametrize("projection", ["model", "tuple", "values", "explicit_tuple"])
def test_cursor_foreign_key_ordering_roundtrip(ordering, projection):
    authors = [
        Author.objects.create(
            name=name,
            email=f"cursor-fk-{name}@test.invalid",
        )
        for name in ("A", "B")
    ]

    BlogPost.objects.bulk_create(
        [
            BlogPost(
                title=f"cursor-fk-{i}",
                content="cursor",
                author=authors[i // 6],
            )
            for i in range(12)
        ]
    )

    api = BoltAPI()

    class Page(CursorPagination):
        page_size = 2

    Page.ordering = ordering

    @api.get("/cursor-querysets")
    @paginate(Page)
    async def posts(request):
        qs = BlogPost.objects.filter(title__startswith="cursor-fk-")
        if projection == "tuple":
            return qs.values_list()
        if projection == "values":
            return qs.values("author", "id", "title")
        if projection == "explicit_tuple":
            return qs.values_list("author", "id", "title")
        return qs

    rows = _walk(api, page_size=2)

    ids = (
        [row["id"] if isinstance(row, dict) else row[0] if projection == "tuple" else row[1] for row in rows]
        if projection != "model"
        else [row["id"] for row in rows]
    )

    expected = set(BlogPost.objects.filter(title__startswith="cursor-fk-").values_list("id", flat=True))

    assert len(ids) == len(set(ids)) == 12
    assert set(ids) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering", ["author", "-author"])
async def test_distinct_extra_annotation_rejected_instead_of_skipping_rows(
    ordering,
):
    qs = Article.objects.values("author").annotate(extra=F("id")).distinct()

    class Page(CursorPagination):
        page_size = 2

    Page.ordering = ordering

    with pytest.raises(ValueError, match="DISTINCT.*extra selected"):
        await Page().paginate_queryset(qs, {"query": {}})


@pytest.mark.asyncio
async def test_nested_cursor_ordering_is_rejected_before_evaluation():
    class Nested(CursorPagination):
        ordering = "author__name"

    with pytest.raises(ValueError, match="concrete field or annotation"):
        await Nested().paginate_queryset(BlogPost.objects.all(), {"query": {}})


@pytest.mark.asyncio
async def test_grouped_aggregate_sort_alias_rejected_without_unique_group_key():
    queryset = Article.objects.values("author").annotate(sort=Count("id")).values("sort")

    class AggregateSort(CursorPagination):
        ordering = "sort"
        page_size = 1

    with pytest.raises(ValueError, match="GROUP BY ordering must match"):
        await AggregateSort().paginate_queryset(queryset, {"query": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["model", "values", "values_list", "distinct", "grouped"])
@pytest.mark.parametrize("source", ["field", "annotation"])
async def test_jsonfield_ordering_rejected_before_cursor_can_skip_rows(kind, source):
    queryset = _JSONOrderingProbe.objects.all()
    ordering = "payload"
    if source == "annotation":
        queryset = queryset.annotate(json_sort=F("payload"))
        ordering = "json_sort"
    if kind == "values":
        queryset = queryset.values(ordering)
    elif kind == "values_list":
        queryset = queryset.values_list(ordering, flat=True)
    elif kind == "distinct":
        queryset = queryset.values(ordering).distinct()
    elif kind == "grouped":
        queryset = queryset.values(ordering).annotate(total=Count("id"))

    class ByJSON(CursorPagination):
        pass

    ByJSON.ordering = ordering

    with pytest.raises(ValueError, match="JSONField ordering"):
        await ByJSON().paginate_queryset(queryset, {"query": {}})


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("descending", [False, True])
def test_typed_json_sort_allows_sql_null_json_null_and_ties(descending):
    # A JSONField cannot distinguish these NULLs once loaded as Python None.
    # Casting to text gives distinct, stable cursor values across page breaks.
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(_JSONOrderingProbe)

    try:
        for value in [None, Value(None, output_field=models.JSONField()), {"x": 1}, {"x": 1}, "hello"]:
            _JSONOrderingProbe.objects.create(payload=value)

        ordering = "-sort_key" if descending else "sort_key"
        queryset = _JSONOrderingProbe.objects.annotate(sort_key=Cast("payload", output_field=models.TextField()))
        pk_order = "-id" if descending else "id"
        expected_ids = list(queryset.order_by(ordering, pk_order).values_list("id", flat=True))

        api = BoltAPI()

        class Page(CursorPagination):
            page_size = 1

        Page.ordering = ordering

        @api.get("/cursor-json-typed")
        @paginate(Page)
        async def list_json(request):
            queryset = _JSONOrderingProbe.objects.annotate(sort_key=Cast("payload", output_field=models.TextField()))
            return queryset.values("id", "sort_key")

        found_ids = []
        cursor = None
        with TestClient(api) as client:
            for _ in range(len(expected_ids) + 1):
                path = "/cursor-json-typed"
                if cursor is not None:
                    path += "?cursor=" + quote(cursor, safe="")
                response = client.get(path)
                assert response.status_code == 200, response.text
                result = response.json()
                found_ids.extend(row["id"] for row in result["items"])
                cursor = result.get("next_cursor")
                if not result["has_next"]:
                    break
            else:
                pytest.fail("Typed JSON cursor did not terminate")

        assert found_ids == expected_ids
        assert len(found_ids) == len(set(found_ids))
    finally:
        with connection.schema_editor() as schema_editor:
            schema_editor.delete_model(_JSONOrderingProbe)
