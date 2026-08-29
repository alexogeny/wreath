"""The nine tables a camera-trap network needs.

Every model here exists because the domain has the thing, not because a
framework feature wanted a demonstration. Two shapes are worth reading before
the rest, because they are the ones a smaller design would have collapsed:

**``Station`` is not ``Camera``.** A station is a place — a tree, a waterhole
crossing, a fence gap. A camera is a device that hangs there until it is
stolen, eaten, or replaced. Collapsing them would mean a station's activity
history restarts every time the hardware changes, which is exactly the question
an ecologist is asking. Keeping them apart is what lets a series follow the
*place* across three devices.

**``Deployment`` is the SD card, not the sighting.** Images are captured over
weeks and collected in one trip, so ``captured_at`` and the collection date are
genuinely different facts. That gap is not an edge case in this domain — a card
collected on the 20th routinely carries images from the 1st — and it is why the
analysis layer has to think about late data at all.

``Sighting.review_state`` is deliberately free text in this version. It is the
flaw the second chapter fixes.
"""

from __future__ import annotations

import os

from wreath.orm import (
    Mapped,
    Model,
    column,
    eq,
    index,
    is_null,
    one_of,
    relationship,
    unique,
)
from wreath.orm.types import (
    Bool,
    Int16,
    Int32,
    Int64,
    Jsonb,
    Numeric,
    Text,
    TimestampTz,
)

#: The default namespace name. `\dt camera_trap.*` in psql shows the domain and
#: nothing else. The framework's own tables live in "wreath"; an application's
#: belong somewhere it chose.
DEFAULT_SCHEMA = "camera_trap"

#: One PostgreSQL namespace for the whole example.
#:
#: Read from the environment because a schema name is deployment configuration,
#: not a property of the domain: one database can carry a staging copy beside
#: production, and a test run can give each parallel worker its own namespace
#: instead of six of them fighting over one. Resolved at import because
#: `schema=` is fixed when the model class is built -- so a process serves
#: exactly one schema, and changing it means a new process.
SCHEMA = os.environ.get("CAMERA_TRAP_SCHEMA", DEFAULT_SCHEMA)


class Reserve(Model, table="reserves", schema=SCHEMA):
    """A protected area. Owns the timezone every timestamp under it is read in.

    The timezone is on the reserve rather than on the application because the
    question "how much moved last night" is asked per reserve, and *night* is a
    local idea. Two reserves comparing activity are comparing different wall
    clocks, which is the whole reason this column is not a constant.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text)
    slug: Mapped[str] = column(Text, unique=True)
    timezone: Mapped[str] = column(Text)
    area_hectares: Mapped[int] = column(Int32)
    created_at: Mapped[object] = column(TimestampTz)


class Station(Model, table="stations", schema=SCHEMA):
    """A fixed location where a camera hangs.

    ``sensitive`` marks a place whose coordinates are withheld from volunteers:
    a rhino midden or a raptor nest. It is a property of the *place*, not of
    what happened to walk past it, which is why it lives here and not on
    ``Sighting``.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    reserve_id: Mapped[int] = column(Int64, references=Reserve.id, index=True)
    name: Mapped[str] = column(Text)
    latitude: Mapped[object] = column(Numeric)
    longitude: Mapped[object] = column(Numeric)
    habitat: Mapped[str] = column(Text)
    sensitive: Mapped[bool] = column(Bool, default=False)

    reserve = relationship(Reserve, foreign_key=reserve_id, load="raise")

    #: Every device that has hung here, including the retired ones -- the
    #: station's hardware history, which is the thing a series has to survive.
    #: ``load="raise"`` like everything else: a handler that wants the cameras
    #: says so with ``.include(Station.cameras.selectin())``, and one that
    #: forgets gets an exception rather than a query per station.
    cameras = relationship("Camera", foreign_key="station_id", load="raise")

    #: Partial: the sensitive stations are a small minority, and every
    #: authorization check asks for exactly them. Indexing all 48 rows to find 6
    #: is the shape a partial index exists to avoid.
    _sensitive = index("reserve_id", "id", where=eq("sensitive", True))


class Camera(Model, table="cameras", schema=SCHEMA):
    """A physical device, at one station at a time.

    ``retired_at`` is null while the camera is in service, which makes the
    "cameras currently deployed" question a partial index rather than a scan.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
    serial: Mapped[str] = column(Text, unique=True)
    model: Mapped[str] = column(Text)
    deployed_at: Mapped[object] = column(TimestampTz)
    retired_at: Mapped[object] = column(TimestampTz, nullable=True)
    battery_pct: Mapped[int] = column(Int16)
    firmware: Mapped[str] = column(Text)

    station = relationship(Station, foreign_key=station_id, load="raise")

    #: Partial: "which camera is live at this station" is exactly
    #: ``retired_at IS NULL``, and retired rows outnumber live ones as a network
    #: ages -- 13 of 61 devices are already replacements here, and that ratio
    #: only moves one way.
    _live = index("station_id", "deployed_at", where=is_null("retired_at"))


class Species(Model, table="species", schema=SCHEMA):
    """The controlled vocabulary, and the protection status behind access control.

    ``protection`` drives row-level authorization: ``open`` is public,
    ``sensitive`` is withheld from volunteers, ``restricted`` is withheld from
    everyone but rangers and logged when read. Publishing a rhino's location
    assists poachers — this column is why conservation databases have access
    control at all.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    code: Mapped[str] = column(Text, unique=True)
    common_name: Mapped[str] = column(Text)
    scientific_name: Mapped[str] = column(Text)
    protection: Mapped[str] = column(Text)
    nocturnal: Mapped[bool] = column(Bool, default=False)

    #: Partial: three protection levels, and only two of them ever need a
    #: lookup. `one_of` rather than two indexes because the authorization check
    #: asks one question -- "is this species withheld from anybody?"
    _withheld = index("protection", "id", where=one_of("protection", ["sensitive", "restricted"]))


class Deployment(Model, table="deployments", schema=SCHEMA):
    """An SD-card collection trip.

    This table is what makes late data a row rather than a paragraph. The gap
    between a sighting's ``captured_at`` and its deployment's ``collected_at``
    is inspectable in psql, and the seed puts eleven deliberately long gaps in
    the data so the query returns something.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
    collected_at: Mapped[object] = column(TimestampTz)
    card_serial: Mapped[str] = column(Text)
    image_count: Mapped[int] = column(Int32)
    ingested_at: Mapped[object] = column(TimestampTz, nullable=True)

    station = relationship(Station, foreign_key=station_id, load="raise")

    #: Partial: an ingest worker asks only for cards it has not processed, which
    #: in steady state is an empty set -- the ideal shape for one, because the
    #: index stays small no matter how long the network runs.
    _pending = index("station_id", "collected_at", where=is_null("ingested_at"))


class Observer(Model, table="observers", schema=SCHEMA):
    """A person: volunteer, researcher, or ranger.

    ``reserve_id`` is nullable because researchers work across reserves while
    volunteers are scoped to one. That nullability is the domain's, not a
    convenience.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    email: Mapped[str] = column(Text, unique=True)
    display_name: Mapped[str] = column(Text)
    role: Mapped[str] = column(Text)
    reserve_id: Mapped[object] = column(Int64, references=Reserve.id, nullable=True)


class Sighting(Model, table="sightings", schema=SCHEMA):
    """One identified animal in one image. The fact table.

    ``captured_at`` is when the animal walked past. ``uploaded_at`` is when the
    card reached a laptop. They are weeks apart often enough that treating them
    as one field would be a bug.

    ``review_state`` is **free text on purpose in this version**. Eighteen
    months of a review console posting whatever it liked leaves "confirmed",
    "Confirmed", "ok", "needs-review", "needs review" and "?" in one column, and
    nobody can count how many sightings are confirmed. The second chapter
    recodes it to a controlled vocabulary and shows what the transitional scan
    refuses along the way.

    ``confidence`` is an integer percentage for the same reason: it is the
    honest v1 choice, and a later chapter can retype it.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    station_id: Mapped[int] = column(Int64, references=Station.id, index=True)
    camera_id: Mapped[int] = column(Int64, references=Camera.id, index=True)
    species_id: Mapped[int] = column(Int64, references=Species.id, index=True)
    #: The card this image came off. Without it, "how late was this row" is not
    #: a question the database can answer -- a sighting could belong to any card
    #: collected at its station, and the whole late-data story would be prose.
    deployment_id: Mapped[object] = column(
        Int64, references=Deployment.id, nullable=True, index=True
    )
    captured_at: Mapped[object] = column(TimestampTz, index=True)
    uploaded_at: Mapped[object] = column(TimestampTz)
    confidence: Mapped[int] = column(Int16)
    image_key: Mapped[str] = column(Text)
    thumbnail_key: Mapped[object] = column(Text, nullable=True)
    identified_by: Mapped[object] = column(Int64, references=Observer.id, nullable=True)
    review_state: Mapped[str] = column(Text)
    tags: Mapped[object] = column(Jsonb, default=dict)
    notes: Mapped[object] = column(Text, nullable=True)

    station = relationship(Station, foreign_key=station_id, load="raise")
    camera = relationship(Camera, foreign_key=camera_id, load="raise")
    species = relationship(Species, foreign_key=species_id, load="raise")

    #: The chart query: one station's activity over a date range.
    _activity = index("station_id", "captured_at")

    #: Partial: the review console only ever asks for what a human has not
    #: settled. In a healthy network that is a few hundred rows out of 140,000,
    #: which is the ratio that makes a partial index worth declaring.
    _unreviewed = index("station_id", "captured_at", where=eq("review_state", "needs-review"))


class Assignment(Model, table="assignments", schema=SCHEMA):
    """Which observer may see which reserve, and at what level.

    A composite primary key, because the pair *is* the identity — there is no
    such thing as two assignments of one observer to one reserve.
    """

    observer_id: Mapped[int] = column(Int64, references=Observer.id, primary_key=True)
    reserve_id: Mapped[int] = column(Int64, references=Reserve.id, primary_key=True)
    level: Mapped[str] = column(Text)

    _by_reserve = index("reserve_id", "level")


class AuditEntry(Model, table="audit_entries", schema=SCHEMA):
    """Who read a restricted location, and when.

    Required by permit conditions in real conservation work, not by the
    framework. It is the one table here that records an access rather than an
    observation, which is why it reads oddly next to the others.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    observer_id: Mapped[int] = column(Int64, references=Observer.id, index=True)
    sighting_id: Mapped[int] = column(Int64, references=Sighting.id)
    action: Mapped[str] = column(Text)
    at: Mapped[object] = column(TimestampTz)

    _who = unique("observer_id", "sighting_id", "at")


#: Declared order matters for schema creation: a table must exist before
#: another references it.
MODELS = (
    Reserve,
    Station,
    Camera,
    Species,
    Deployment,
    Observer,
    Sighting,
    Assignment,
    AuditEntry,
)
