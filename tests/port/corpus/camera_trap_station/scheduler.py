"""A durable one-shot survey scheduled through an external service."""

import boto3


scheduler = boto3.client("scheduler")


def schedule_survey(name, when, payload):
    return scheduler.create_schedule(
        Name=name,
        ScheduleExpression=f"at({when})",
        Target={"Input": payload},
    )
