"""Initialize the disposable database supplied by the browser regression runner."""
import asyncio

from app.database import AsyncSessionLocal, init_db
from app.main import app
from app.models import RedemptionCode, RenewalRequest
from app.services.auth import auth_service

CODE_COUNT = 60


async def initialize():
    await init_db()
    async with AsyncSessionLocal() as db:
        await auth_service.initialize_admin_password(db)
        db.add_all([
            RedemptionCode(code=f"WORKSPACE-TEST-{index:03d}", status="unused", pool_type="normal")
            for index in range(CODE_COUNT)
        ])
        await db.flush()
        db.add(RenewalRequest(email="workspace@example.com", code="WORKSPACE-TEST-001"))
        await db.commit()


asyncio.run(initialize())
