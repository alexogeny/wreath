"""Relational models for cameras, sightings, and survey roles."""

import ormar


class SurveyRole(ormar.Model):
    ormar_config = camera_db.copy(tablename="survey_role")

    id: int = ormar.Integer(primary_key=True)
    name: str = ormar.String(max_length=40, unique=True)
    description: str = ormar.String(max_length=200)


class Camera(ormar.Model):
    ormar_config = camera_db.copy(tablename="camera")

    id: int = ormar.Integer(primary_key=True)
    serial: str = ormar.String(max_length=80, unique=True)
    trail: str = ormar.String(max_length=120)


class Sighting(ormar.Model):
    ormar_config = camera_db.copy(tablename="sighting")

    id: int = ormar.Integer(primary_key=True)
    camera: Camera = ormar.ForeignKey(Camera)
    species: str = ormar.String(max_length=80)


class StationReading(ormar.Model):
    ormar_config = camera_db.copy(tablename="station_reading")

    id: int = ormar.Integer(primary_key=True)
    temperature: float = ormar.Float()
