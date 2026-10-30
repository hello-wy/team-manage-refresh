"""官方 API Key 独立存储表。"""
from sqlalchemy import Column, DateTime, Integer, Text

from app.database import Base
from app.utils.time_utils import get_now


class OfficialKey(Base):
    __tablename__ = "official_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account = Column(Text, nullable=False)
    api_key_encrypted = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=get_now)


class OfficialKeyExport(Base):
    __tablename__ = "official_key_exports"

    official_key_id = Column(Integer, primary_key=True)
    base_url = Column(Text, nullable=False)
    account_id = Column(Integer, nullable=True)
    operation_id = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    error = Column(Text, nullable=True)
    exported_at = Column(DateTime, nullable=True)


class OfficialKeyExpiration(Base):
    __tablename__ = "official_key_expirations"

    official_key_id = Column(Integer, primary_key=True)
    expires_at = Column(DateTime, nullable=False)
