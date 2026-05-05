from fastapi import APIRouter, Header, HTTPException
from app.settings import settings
from app.worker.loop import kick

router = APIRouter()


@router.post("/internal/kick")
async def internal_kick(x_internal_token: str = Header(...)) -> dict:
    if x_internal_token != settings.internal_shared_secret:
        raise HTTPException(status_code=401, detail="Unauthorized")
    kick()
    return {"kicked": True}
