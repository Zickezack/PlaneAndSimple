from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from flighttracker.models.base import Base


class PlatformSetting(Base):
    """Admin-managed override of a `Settings` field (see `services/settings.py`).

    Only the keys listed in `services.settings.SETTING_FIELDS` are ever written; everything
    else still comes from the environment / `.env`.
    """

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
