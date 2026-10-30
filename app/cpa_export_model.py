"""CPA background jobs are separate from Sub2API export records."""
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base
from app.utils.time_utils import get_now


class CpaExportJob(Base):
    __tablename__ = "account_pool_cpa_export_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_pool_id = Column(Integer, ForeignKey("account_pool_entries.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    workspace_id = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    error = Column(Text)
    filename = Column(String(255))
    created_at = Column(DateTime, default=get_now, nullable=False)
    finished_at = Column(DateTime)
