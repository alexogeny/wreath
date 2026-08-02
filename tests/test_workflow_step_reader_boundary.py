"""The workflow-step reader distinguishes an empty clean file from a tear."""

from __future__ import annotations

import pytest

from wreath._flight_schema import SCHEMA_VERSION, MetadataImage, SchemaError
from wreath._recording_format import WFR1Writer, read_step_recording


def test_clean_recording_without_a_workflow_step_is_refused_by_kind(tmp_path) -> None:
    path = tmp_path / "no-step.wfr1"
    image = MetadataImage(SCHEMA_VERSION, *([()] * 11))
    with path.open("wb") as handle:
        WFR1Writer(handle, image).close()

    with pytest.raises(SchemaError) as excinfo:
        read_step_recording(path.read_bytes())

    assert str(excinfo.value) == "this recording holds no workflow-step recording"
