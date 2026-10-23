from typing import Any

from fastapi import APIRouter, Depends

from app.auth.demo_auth import require_demo_auth
from app.providers import provider_statuses
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api", tags=["providers"])


@router.get("/providers", dependencies=[Depends(require_demo_auth)])
def get_providers(settings: Settings = Depends(get_settings)) -> dict[str, dict[str, Any]]:
    """Provider badges for the simulator and the Family Board: status, flags and a reason, never a secret."""
    return {name: status.as_dict() for name, status in provider_statuses(settings).items()}
