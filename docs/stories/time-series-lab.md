---
description: Build an interactive analysis platform for irregular series, timeboxes and calendar-aware comparisons.
keywords: time series analytics DST late data buckets recurrence charts downsampling analysis platform
---

```hero
eyebrow: Story 06 · time is a domain, not a timestamp column
title: Ask better questions of time.
lede: Explore operational series across missing samples, late arrivals, changing granularity and daylight-saving boundaries without quietly changing the question.
signal: calendar-aware buckets
signal: late data
signal: progressive charts
signal: reproducible exports
action: See the awkward question -> #the-awkward-question
action: Browse the data surface -> ../reference/index.md#data-and-analysis
```

## The scene

An analyst investigates energy, latency, manufacturing yield or model evaluations.
They define a timebox, choose measures and dimensions, compare it with another period,
and keep working while a larger query finishes. The result may contain millions of
source points, but the visible chart should remain honest and responsive.

The platform stores events. It does not pretend all events arrived on time or that a
day is always twenty-four hours.

## The awkward question

Compare the current local business week with the previous one across a daylight-saving
transition. One source reported late. Another has gaps. The chart needs a common spine,
appropriate resolution and enough points to preserve the shape without shipping the
entire source series.

The result lines up by the business calendar, identifies incomplete buckets and updates
the affected window when the late observation lands.

> The invariant: aggregation preserves the declared calendar and range semantics; the
> presentation may reduce points, but it may not silently redefine the timebox.

## The system shape

```text
events ──> typed temporal values ──> series plan ──> progressive result
  │                                      │                 │
  └── late arrival facts                 ├── comparison    ├── chart projection
                                         └── dimensions    └── export artifact
```

| Analytical need | Wreath surface | What stays explicit |
|---|---|---|
| instants and local calendars | `wreath.temporal` | zones, buckets, recurrence and duration |
| aggregation and comparison | `wreath.series` | measures, ranges, dimensions and spines |
| live query state | `wreath.streams`, `wreath.progress` | partial results and continuation |
| expensive analysis | `wreath.jobs`, `wreath.workflows` | durable runs and timeboxes |
| large result artifacts | `wreath.objects` | bounded storage and downloads |
| alternative clients | `wreath.graphql`, `wreath.protobuf` | typed query and wire surfaces |

## Build it in four acts

### 1. Make the timebox a value

Parse the requested range and zone once. Refuse an ambiguous local instant. Generate
the output spine before reading events so empty buckets exist rather than disappearing.

### 2. Add useful measures

Count, sum and average over one dimension. Then compare two ranges. Keep the query
declaration apart from its result so the analysis can be recorded and repeated.

### 3. Price the chart

Project the series and downsample only when the output is genuinely large. Stream a
preview, then replace it with the completed result. Expose the raw export beside the
visual rather than making the chart the only evidence.

### 4. Admit imperfect data

Insert late and missing readings. Show which buckets reopen, which remain sealed and
why. Run the same timebox across a daylight-saving transition and a fixed UTC range;
they should differ because the questions differ.

## Build a calendar-aware analysis endpoint

This route accepts an already-aggregated series, creates the requested local-calendar
spine, and reduces only the chart payload. It refuses mismatched data instead of
silently shifting values onto different buckets.

```python title="app.py"
from dataclasses import dataclass
from typing import Annotated

from wreath import Request, Wreath
from wreath.binding import Body
from wreath.exceptions import BadRequest
from wreath.series import lttb
from wreath.temporal import TemporalError, bucket, format_iso, parse, spine, zone


@dataclass
class AnalysisRequest:
    start: str
    end: str
    timezone: str
    resolution: str
    values: list[float]
    max_points: int = 240


def analyse(command: AnalysisRequest) -> dict:
    if command.max_points < 3:
        raise BadRequest("max_points must be at least 3")
    try:
        timezone = zone(command.timezone)
        width = bucket(command.resolution)
        buckets = spine(
            parse(command.start),
            parse(command.end),
            bucket=width,
            in_zone=timezone,
        )
    except TemporalError as error:
        raise BadRequest(f"invalid analysis timebox: {error}") from error
    if len(command.values) != len(buckets):
        raise BadRequest(
            f"received {len(command.values)} values for {len(buckets)} calendar buckets"
        )

    x = tuple(float(index) for index in range(len(buckets)))
    y = tuple(float(value) for value in command.values)
    selected = lttb(x, y, command.max_points)
    return {
        "calendar": command.timezone,
        "resolution": width.name,
        "source_points": len(buckets),
        "points": [
            {"at": format_iso(buckets[index]), "value": y[index]}
            for index in selected
        ],
    }


app = Wreath()


@app.post("/analysis")
async def analysis(
    request: Request,
    command: Annotated[AnalysisRequest, Body()],
) -> dict:
    return analyse(command)
```

`spine` is half-open: `start` is included and `end` is not. A local day crossing
daylight saving is still one calendar bucket even when the elapsed time is 23 or 25
hours. `lttb` keeps the endpoints and the largest excursions; it never changes the
aggregation.

### Test the awkward week

```python title="test_app.py"
from wreath.testing import TestClient
from wreath.temporal import Duration, hours, parse

from app import app


async def test_auckland_days_keep_the_dst_fold() -> None:
    async with TestClient(app) as client:
        response = await client.post(
            "/analysis",
            json={
                "start": "2026-04-03T00:00:00+13:00",
                "end": "2026-04-08T00:00:00+12:00",
                "timezone": "Pacific/Auckland",
                "resolution": "day",
                "values": [12, 16, 9, 18, 14],
                "max_points": 20,
            },
        )

    assert response.status == 200
    body = response.json()
    assert body["source_points"] == 5
    instants = [parse(point["at"]) for point in body["points"]]
    elapsed = [
        Duration.of(right.to("UTC") - left.to("UTC"))
        for left, right in zip(instants, instants[1:], strict=False)
    ]
    assert hours(25) in elapsed


async def test_chart_reduction_keeps_the_first_and_last_points() -> None:
    values = [0, 1, 2, 3, 40, 5, 6, 7, 8]
    async with TestClient(app) as client:
        response = await client.post(
            "/analysis",
            json={
                "start": "2026-06-01T00:00:00+00:00",
                "end": "2026-06-10T00:00:00+00:00",
                "timezone": "UTC",
                "resolution": "day",
                "values": values,
                "max_points": 4,
            },
        )

    points = response.json()["points"]
    assert len(points) == 4
    assert points[0]["value"] == 0
    assert points[-1]["value"] == 8
    assert any(point["value"] == 40 for point in points)
```

```bash
uv run wreath test -k analysis
uv run wreath dev app:app
```

## Move aggregation into PostgreSQL

When the source is no longer a small array, declare the time axis and measures once.
Wreath generates the calendar spine in the query so missing buckets remain present.

```python title="readings.py"
from wreath.orm import Mapped, Model, column
from wreath.orm.types import Float64, Int64, Text, TimestampTz
from wreath.series import Range, Series, count, sum_
from wreath.temporal import Hour


class Reading(Model, table="readings"):
    id: Mapped[int] = column(Int64, primary_key=True)
    meter_id: Mapped[str] = column(Text)
    observed_at: Mapped[object] = column(TimestampTz)
    kilowatts: Mapped[float] = column(Float64)


hourly_energy = (
    Series(Reading, at=Reading.observed_at, bucket=Hour)
    .measure(samples=count(), kilowatts=sum_(Reading.kilowatts))
    .by(Reading.meter_id, top=20)
)


async def run_hourly(session, start, end):
    return await hourly_energy.run(
        session,
        range=Range(start, end),
        zone="Pacific/Auckland",
    )
```

Use a durable job for analyses that outlive a request, store the full result as an
object, and send the small chart projection through progress or a stream. The saved
artifact and the visible chart then share the same range, zone, bucket and query
declaration.

## The larger idea

Most analytics defects are not arithmetic defects. They are unstated calendar,
completeness or comparison decisions. Wreath gives those decisions names so the query,
job, chart and export can share them.

Next: [put the same honesty under flash traffic](noon-drop.md), or
[start with one Wreath route](../start/index.md).
