"""Four tables: an animal, the device it wears, a place, and a position.

The camera-trap example has nine tables because a camera network has nine
things. This one has four, and each is here because the tracking story needs
it rather than because a framework feature wanted a demonstration.

**``Collar`` is not ``Animal``.** A collar is hardware: it is fitted, it fails,
it is recovered from a carcass and refurbished onto a different animal two
seasons later. Collapsing the two would make a device's battery history and an
animal's movement history the same row, and then a collar's second life would
appear as a teleport in the first animal's track. It is the same argument the
camera trap makes for a station and a camera, one level up.

**``Fix.recorded_at`` is not ``Fix.received_at``.** A collar takes a position on
a schedule and uploads it when it next sees a satellite. Those are hours apart
routinely and *days* apart under canopy, which is the whole of the late-data
story. Every question about "yesterday" has to decide which of the two it means.

**``Fix.leg_m`` is a stored derivation, and that is a choice.** How far an
animal travelled is the sum of the legs between consecutive fixes, and summing
them in the database means one number per day instead of a thousand
coordinates crossing the wire to be reduced in Python. The price is that the
column has to be maintained by the ingest path -- including for the fix that
*follows* a late arrival, whose leg was measured across a gap that has since
been filled in. ``tracking.ingest`` pays that price in one place and
``tests/tracking/test_ingest.py`` holds it to the answer
:class:`wreath.geospatial.Trajectory` computes from the raw fixes.

**Coordinates are ``Float8`` here and ``Numeric`` in the camera trap.** Both are
right. A station's position is surveyed once and written down, so exactness is
free and the camera trap keeps it. A collar's position is a measurement with
tens of metres of error attached; storing it as an exact decimal claims a
precision the GPS never had, and doubles the width of the busiest table in the
schema for the claim. Six decimal places of degrees is about a tenth of a
metre, and a ``float8`` carries fifteen.
"""

from __future__ import annotations

from wreath.orm import Mapped, Model, column, index, is_null, one_of, relationship
from wreath.orm.types import Float8, Int16, Int32, Int64, Text, TimestampTz

from .config import SCHEMA

#: The three protection tiers an animal may carry, weakest first. They mean the
#: same thing as the camera trap's `Species.protection`, deliberately: the two
#: examples describe one conservancy, and a species that is restricted on a
#: camera is not open on a collar.
PROTECTIONS = ("open", "sensitive", "restricted")


class Animal(Model, table="animals", schema=SCHEMA):
    """One collared individual.

    ``protection`` is the column every authorization decision in this example
    reads. It is on the *animal* rather than on the species because collaring is
    individual work: this conservancy's two rhinos are named, known, and
    followed, and one of the zebras carries a collar for a gait study that
    nobody is trying to keep quiet.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, unique=True)
    taxon: Mapped[str] = column(Text)
    protection: Mapped[str] = column(Text)

    #: Partial: the withheld animals are the minority and every precision
    #: decision asks for exactly them. `one_of` rather than two indexes because
    #: the question is one question -- "is this animal's position withheld from
    #: anybody?" The camera trap indexes `Species.protection` the same way, for
    #: the same reason.
    _withheld = index("protection", "id", where=one_of("protection", ["sensitive", "restricted"]))


class Collar(Model, table="collars", schema=SCHEMA):
    """A device, on one animal at a time.

    ``removed_at`` is null while the collar is in service, which makes "which
    collars are live" a partial index rather than a scan -- and unlike a camera,
    a collar is usually removed rather than replaced, so the live set stays a
    small fraction of the rows as the programme runs.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    animal_id: Mapped[int] = column(Int64, references=Animal.id, index=True)
    serial: Mapped[str] = column(Text, unique=True)
    fitted_at: Mapped[object] = column(TimestampTz)
    removed_at: Mapped[object] = column(TimestampTz, nullable=True)

    animal = relationship(Animal, foreign_key=animal_id, load="raise")

    _live = index("animal_id", "fitted_at", where=is_null("removed_at"))


class Landmark(Model, table="landmarks", schema=SCHEMA):
    """A named place on the conservancy: a waterhole, a gate, a ranger post.

    Small and static -- twelve rows -- which is why the "which landmark is this
    animal nearest" question is answered in Python over the whole table rather
    than by a query. Reaching for an index on twelve rows would be a
    demonstration, not a design.
    """

    id: Mapped[int] = column(Int64, primary_key=True)
    name: Mapped[str] = column(Text, unique=True)
    kind: Mapped[str] = column(Text)
    latitude: Mapped[float] = column(Float8)
    longitude: Mapped[float] = column(Float8)


class Fix(Model, table="fixes", schema=SCHEMA):
    """One position, from one collar, at one moment. The fact table.

    ``animal_id`` is carried here as well as on the collar, and it is not
    denormalisation: a collar's second life is on a different animal, so which
    animal a *fix* belongs to is a fact about the fix rather than a lookup
    through the device. It is also what keeps every authorization filter one
    join deep instead of two.

    ``relay`` is the field station that forwarded the batch. A collar does not
    talk to this application; it talks to a satellite, and a station on the
    ground relays what comes down. Recording which one is what makes a station
    with a broken clock findable later.

    **The primary key is ``(collar_id, recorded_at)``, and there is no ``id``
    column.** A collar takes at most one position per instant, so the pair *is*
    the identity -- the same argument the camera trap makes for
    ``Assignment``. It is worth more here than tidiness: a field station whose
    upload times out retries the whole batch, and with a synthetic key that
    retry is a second copy of every position in it. With this key the retry is
    an ``ON CONFLICT DO NOTHING`` that lands nothing, so ingest is idempotent
    because of the schema rather than because of a flag somebody remembered to
    check.
    """

    collar_id: Mapped[int] = column(Int64, references=Collar.id, primary_key=True)
    recorded_at: Mapped[object] = column(TimestampTz, primary_key=True)
    animal_id: Mapped[int] = column(Int64, references=Animal.id)
    received_at: Mapped[object] = column(TimestampTz)
    latitude: Mapped[float] = column(Float8)
    longitude: Mapped[float] = column(Float8)
    accuracy_m: Mapped[object] = column(Float8, nullable=True)
    battery_pct: Mapped[int] = column(Int16)
    #: Metres from the previous fix of this animal. Null for the first fix of an
    #: animal, because there is no leg before the first one -- and null rather
    #: than zero, because zero is a real answer meaning "did not move".
    leg_m: Mapped[object] = column(Float8, nullable=True)
    relay: Mapped[str] = column(Text)
    #: How many satellites the collar had. Kept because a two-satellite fix in a
    #: gorge is the one that lands in the Indian Ocean, and a reader wants to be
    #: able to see that in the data rather than be told about it.
    satellites: Mapped[int] = column(Int32)

    animal = relationship(Animal, foreign_key=animal_id, load="raise")
    collar = relationship(Collar, foreign_key=collar_id, load="raise")

    #: The track query: one animal's fixes in a window, in order. Also the index
    #: the daily-distance series reads.
    _track = index("animal_id", "recorded_at")

    #: The proximity query. A degree-aligned rectangle is what a btree can
    #: answer, and `tracking.place.within` builds exactly that from
    #: `wreath.geospatial.bounding_boxes` before the exact great-circle test
    #: runs over what comes back. Latitude leads because a metre of latitude is
    #: a fixed number of degrees everywhere, so the leading column's selectivity
    #: does not depend on where on Earth the query is asked.
    _position = index("latitude", "longitude")

    #: "What has arrived since I last looked", which is a different question
    #: from `_track` and needs a different index: arrival order and recording
    #: order diverge by days for a collar that lost the sky, so neither index
    #: answers the other's query. Not partial -- there is no predicate that
    #: separates late rows from prompt ones, because lateness is a *relation*
    #: between two columns rather than a property of one.
    _late = index("animal_id", "received_at")


#: Declaration order matters for schema creation: a table must exist before
#: another references it.
MODELS = (Animal, Collar, Landmark, Fix)
