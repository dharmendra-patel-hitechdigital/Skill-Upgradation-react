"""Dashboard aggregates: contract, real numbers, and per-user scoping."""
from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest
from httpx import AsyncClient

from app.api.v1.endpoints.dashboard import _percent_change, _relative_time
from app.repositories.dashboard import month_buckets
from tests.conftest import (
    OTHER_EMAIL,
    OTHER_PASSWORD,
    auth_header,
    login,
    register,
    upload,
)
from tests.pdf_builder import contract_pdf, invoice_pdf

DASHBOARD_PATHS = ("stats", "revenue", "activity")


# ------------------------------------------------------------------- routing
@pytest.mark.parametrize("panel", DASHBOARD_PATHS)
async def test_panels_exist_and_are_not_404(
    client: AsyncClient, api: str, user_tokens: dict, panel: str
) -> None:
    """The regression this suite exists for: these three routes were missing.

    The SPA called them against the mock backend, so a real deployment answered
    404 on every dashboard load.
    """
    response = await client.get(
        f"{api}/dashboard/{panel}", headers=auth_header(user_tokens["access_token"])
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("panel", DASHBOARD_PATHS)
async def test_panels_require_authentication(
    client: AsyncClient, api: str, panel: str
) -> None:
    response = await client.get(f"{api}/dashboard/{panel}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# --------------------------------------------------------------------- stats
async def test_stats_match_the_client_contract(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """Every key the stat card renders must be present and of the right type."""
    response = await client.get(
        f"{api}/dashboard/stats", headers=auth_header(user_tokens["access_token"])
    )
    body = response.json()

    assert body["window_days"] == 30
    assert [stat["id"] for stat in body["stats"]] == [
        "documents",
        "pages",
        "data",
        "success_rate",
    ]
    for stat in body["stats"]:
        assert isinstance(stat["value"], int | float)
        assert stat["format"] in ("number", "percent", "currency")
        assert stat["trend"] in ("up", "down")
        assert isinstance(stat["delta"], int | float)
        assert stat["label"] and stat["comparison"]


async def test_stats_count_real_uploads(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, filename="one.pdf")

    response = await client.get(f"{api}/dashboard/stats", headers=auth_header(token))
    stats = {stat["id"]: stat for stat in response.json()["stats"]}

    assert stats["documents"]["value"] == 1
    # One document, processed by the offline pipeline, so nothing failed.
    assert stats["success_rate"]["value"] == 100.0
    assert stats["pages"]["value"] >= 1
    # A kilobyte-sized fixture must not be rounded down to "0.0 MB": the unit
    # scales with the volume, and the value stays non-zero.
    assert stats["data"]["unit"] == "KB"
    assert stats["data"]["value"] > 0


async def test_stats_are_zero_not_null_on_an_empty_account(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """An account with no documents must report zeros, not nulls or a 500.

    A dashboard's first-ever load is the empty case, so it is the one that has to
    work: `SUM` over no rows returns NULL, which would otherwise reach the client.
    """
    response = await client.get(
        f"{api}/dashboard/stats", headers=auth_header(user_tokens["access_token"])
    )
    for stat in response.json()["stats"]:
        assert stat["value"] == 0
        assert stat["delta"] == 0.0


async def test_stats_window_is_configurable(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.get(
        f"{api}/dashboard/stats?window_days=7",
        headers=auth_header(user_tokens["access_token"]),
    )
    body = response.json()
    assert body["window_days"] == 7
    assert body["stats"][0]["comparison"] == "vs. previous 7 days"


async def test_stats_reject_an_out_of_range_window(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    response = await client.get(
        f"{api}/dashboard/stats?window_days=0",
        headers=auth_header(user_tokens["access_token"]),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# -------------------------------------------------------------------- series
async def test_series_returns_a_labelled_month_per_point(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    await upload(client, token, filename="series.pdf")

    response = await client.get(f"{api}/dashboard/revenue", headers=auth_header(token))
    body = response.json()

    assert len(body["series"]) == 8
    # Labels key the chart client-side, so duplicates would drop bars.
    labels = [point["label"] for point in body["series"]]
    assert len(set(labels)) == len(labels)
    # This month is the last bucket, and it holds the upload just made.
    assert body["series"][-1]["value"] == 1
    assert body["meta"]["total"] == 1
    assert body["meta"]["year"] == datetime.now(UTC).year
    assert body["meta"]["unit"] == "documents"


async def test_series_months_are_bounded(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """13 months would repeat a label, so the parameter is capped at 12."""
    token = user_tokens["access_token"]
    ok = await client.get(f"{api}/dashboard/revenue?months=12", headers=auth_header(token))
    assert len(ok.json()["series"]) == 12

    rejected = await client.get(
        f"{api}/dashboard/revenue?months=13", headers=auth_header(token)
    )
    assert rejected.status_code == 422


def test_month_buckets_walk_back_across_a_year_boundary() -> None:
    buckets = month_buckets(reference=datetime(2026, 2, 14, tzinfo=UTC), months=4)

    assert [bucket.label for bucket in buckets] == ["Nov", "Dec", "Jan", "Feb"]
    assert [bucket.year for bucket in buckets] == [2025, 2025, 2026, 2026]
    # Half-open and contiguous: no gap, no overlap, so no upload is double-counted.
    for earlier, later in pairwise(buckets):
        assert earlier.end == later.start
    assert buckets[-1].end == datetime(2026, 3, 1, tzinfo=UTC)


# ------------------------------------------------------------------ activity
async def test_activity_reflects_the_real_audit_trail(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    document = await upload(client, token, filename="report.pdf")

    response = await client.get(f"{api}/dashboard/activity", headers=auth_header(token))
    items = response.json()["activity"]

    assert items, "processing a document must leave events behind"
    kinds = {item["type"] for item in items}
    assert {"upload", "completed"} <= kinds

    newest = items[0]
    assert newest["document_id"] == document["id"]
    assert newest["filename"] == "report.pdf"
    # full_name is set on this fixture's user, so the feed shows a name.
    assert newest["user"] == "Test User"
    assert newest["time"] == "just now"
    assert "report.pdf" in newest["action"]
    # Unique keys: the SPA renders this list with key={item.id}.
    assert len({item["id"] for item in items}) == len(items)


async def test_activity_is_newest_first_and_honours_limit(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    token = user_tokens["access_token"]
    # Distinct payloads on purpose: identical bytes are deduplicated against the
    # owner's existing upload, so re-sending the same PDF under a new filename
    # would produce no second document and nothing new to order.
    await upload(client, token, data=invoice_pdf(), filename="first.pdf")
    await upload(client, token, data=contract_pdf(), filename="second.pdf")

    response = await client.get(
        f"{api}/dashboard/activity?limit=2", headers=auth_header(token)
    )
    items = response.json()["activity"]

    assert len(items) == 2
    assert items[0]["timestamp"] >= items[1]["timestamp"]
    assert items[0]["filename"] == "second.pdf"


# ------------------------------------------------------------------- scoping
async def test_a_user_never_sees_another_users_numbers(
    client: AsyncClient, api: str, user_tokens: dict
) -> None:
    """The scope comes from the caller's identity, so this is not bypassable."""
    await upload(client, user_tokens["access_token"], filename="private.pdf")

    await register(client, OTHER_EMAIL, OTHER_PASSWORD)
    other = await login(client, OTHER_EMAIL, OTHER_PASSWORD)
    headers = auth_header(other["access_token"])

    stats = await client.get(f"{api}/dashboard/stats", headers=headers)
    assert {stat["id"]: stat["value"] for stat in stats.json()["stats"]}["documents"] == 0

    activity = await client.get(f"{api}/dashboard/activity", headers=headers)
    assert activity.json()["activity"] == []

    series = await client.get(f"{api}/dashboard/revenue", headers=headers)
    assert series.json()["meta"]["total"] == 0


async def test_an_admin_sees_the_whole_installation(
    client: AsyncClient, api: str, admin_tokens: dict
) -> None:
    user = await login(client)
    await upload(client, user["access_token"], filename="owned-by-user.pdf")

    headers = auth_header(admin_tokens["access_token"])
    stats = await client.get(f"{api}/dashboard/stats", headers=headers)
    documents = {stat["id"]: stat["value"] for stat in stats.json()["stats"]}["documents"]
    assert documents == 1

    activity = await client.get(f"{api}/dashboard/activity", headers=headers)
    assert activity.json()["activity"][0]["filename"] == "owned-by-user.pdf"


# -------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("current", "previous", "expected_delta", "expected_trend"),
    [
        (110, 100, 10.0, "up"),
        (90, 100, -10.0, "down"),
        (100, 100, 0.0, "up"),  # flat renders as a neutral 0%, never a fake drop
        (5, 0, 100.0, "up"),  # growth from nothing has no defined percentage
        (0, 0, 0.0, "up"),  # and "still nothing" must not read as growth
    ],
)
def test_percent_change_handles_the_zero_baseline(
    current: int, previous: int, expected_delta: float, expected_trend: str
) -> None:
    delta, trend = _percent_change(current, previous)
    assert delta == expected_delta
    assert trend.value == expected_trend


def test_relative_time_clamps_a_future_timestamp() -> None:
    """Clock skew between the app server and the database must not print "-3 min"."""
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    assert _relative_time(datetime(2026, 8, 24, 12, 5, tzinfo=UTC), now=now) == "just now"
    assert _relative_time(datetime(2026, 8, 24, 11, 58, tzinfo=UTC), now=now) == "2 min ago"
    assert _relative_time(datetime(2026, 8, 24, 9, 0, tzinfo=UTC), now=now) == "3 hrs ago"
    assert _relative_time(datetime(2026, 8, 22, 12, 0, tzinfo=UTC), now=now) == "2 days ago"
