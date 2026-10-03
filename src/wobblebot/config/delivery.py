"""Independent outbound delivery owns only operator state and its own log."""

from pydantic import BaseModel, Field


class DeliveryConfig(BaseModel):
    """Cadences belong in schedules; the channel comes from operator configuration."""

    operator_db: str = Field(default="data/wobblebot-operator.db", min_length=1)
    log_file_path: str | None = "data/logs/delivery/delivery.log"
