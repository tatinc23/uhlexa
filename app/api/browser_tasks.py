"""Browser task routes (spec section 14). The screenshot and the takeover link are household/owner scoped."""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.auth.demo_auth import Principal
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.providers.maritime.service import MaritimeService
from app.schemas.browser_tasks import BrowserTaskOut, BrowserTaskStartIn
from app.services import browser_task_service, views
from app.services.providers import get_maritime

router = APIRouter(prefix="/api/browser-tasks", tags=["browser-tasks"])


@router.post("")
def start_browser_task(
    body: BrowserTaskStartIn, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> BrowserTaskOut:
    """Run (or retry) the browser step of an approved task."""
    bt = browser_task_service.start(db, principal, body.task_id)
    out = views.browser_task_out(bt, principal)
    db.commit()
    return out


@router.get("/{browser_task_id}")
def get_browser_task(
    browser_task_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> BrowserTaskOut:
    return views.browser_task_out(browser_task_service.get_for(db, principal, browser_task_id), principal)


@router.get("/{browser_task_id}/screenshot")
def get_screenshot(
    browser_task_id: str, principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> Response:
    data, mime = browser_task_service.screenshot_for(db, principal, browser_task_id)
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, no-store"})


@router.post("/{browser_task_id}/resume")
def resume_browser_task(
    browser_task_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    maritime: MaritimeService = Depends(get_maritime),
) -> BrowserTaskOut:
    bt = browser_task_service.resume_after_takeover(db, principal, browser_task_id, maritime=maritime)
    out = views.browser_task_out(bt, principal)
    db.commit()
    return out
