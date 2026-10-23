import dataclasses
from collections.abc import Callable

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import demo
from app.auth.demo_auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    Principal,
    read_session,
    resolve_principal,
    sign_session,
)
from app.auth.dependencies import get_principal
from app.db.session import get_db
from app.errors import register_error_handlers
from app.models import Agent, HouseholdMembership, MembershipRole, User
from app.settings import get_settings

AsUser = Callable[[str], dict[str, str]]


def _set_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
def probe(db: Session, seeded: dict[str, Principal]) -> TestClient:
    """A tiny app with the demo router and one route behind `get_principal`, independent of the REST routers."""
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(demo.router)

    @app.get("/whoami")
    def whoami(principal: Principal = Depends(get_principal)) -> dict[str, str]:
        return {"handle": principal.user.handle, "role": principal.role, "agent": principal.agent.name}

    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# --- signed session cookie ---------------------------------------------------------------------------------------


def test_signed_session_roundtrip(settings_env: object) -> None:
    token = sign_session("alex")
    assert read_session(token) == "alex"
    assert "alex" not in token.split(".")[2]  # the signature part is not the handle in the clear


def test_session_token_is_cookie_safe(settings_env: object) -> None:
    token = sign_session("alex")
    assert token.isascii()
    assert not set(token) & set(' ;,"\\')


@pytest.mark.parametrize("mangle", ["flip_signature", "swap_handle", "drop_signature", "extend", "extra_part"])
def test_tampered_session_rejected(settings_env: object, mangle: str) -> None:
    b64_handle, issued, signature = sign_session("max").split(".")
    forged_handle = sign_session("alex").split(".")[0]
    tampered = {
        "flip_signature": f"{b64_handle}.{issued}.{('0' if signature[0] != '0' else '1') + signature[1:]}",
        "swap_handle": f"{forged_handle}.{issued}.{signature}",  # promote Max to Alex, keep Max's signature
        "drop_signature": f"{b64_handle}.{issued}",
        "extend": f"{b64_handle}.{int(issued) + 10}.{signature}",  # refresh the issue time
        "extra_part": f"{b64_handle}.{issued}.{signature}.x",
    }[mangle]
    assert read_session(tampered) is None


@pytest.mark.parametrize("garbage", ["", ".", "..", "a.b.c", "alex", "café.1.2", "\x00.\x00.\x00", "x" * 5000])
def test_garbage_session_is_none_not_an_error(settings_env: object, garbage: str) -> None:
    assert read_session(garbage) is None


def test_session_signed_with_another_secret_rejected(settings_env: object, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, APP_SESSION_SECRET="secret-a")
    token = sign_session("alex")
    assert read_session(token) == "alex"
    _set_env(monkeypatch, APP_SESSION_SECRET="secret-b")
    assert read_session(token) is None


def test_session_expires(settings_env: object) -> None:
    token = sign_session("alex", now=1_000_000)
    assert read_session(token, now=1_000_000 + SESSION_MAX_AGE_SECONDS) == "alex"
    assert read_session(token, now=1_000_000 + SESSION_MAX_AGE_SECONDS + 1) is None


# --- resolving principals ----------------------------------------------------------------------------------------


def test_resolve_principal_roles_and_agents(db: Session, seeded: dict[str, Principal]) -> None:
    alex, katie, max_ = (resolve_principal(db, handle) for handle in ("alex", "katie", "max"))
    assert alex and katie and max_
    assert (alex.role, katie.role, max_.role) == (MembershipRole.GUARDIAN, MembershipRole.ADULT, MembershipRole.CHILD)
    assert (alex.is_child, katie.is_child, max_.is_child) == (False, False, True)
    assert (alex.is_guardian_or_adult, katie.is_guardian_or_adult, max_.is_guardian_or_adult) == (True, True, False)
    assert alex.agent.name == "Uhlexa" and alex.agent.owner_user_id == alex.user.id
    assert alex.household.slug == "parker" == max_.household.slug
    assert alex.membership.household_id == alex.household.id and alex.membership.user_id == alex.user.id


def test_resolve_principal_is_case_and_whitespace_insensitive(db: Session, seeded: dict[str, Principal]) -> None:
    principal = resolve_principal(db, "  Katie ")
    assert principal is not None and principal.user.handle == "katie"


def test_resolve_principal_unknown_handle_is_none(db: Session, seeded: dict[str, Principal]) -> None:
    assert resolve_principal(db, "nobody") is None
    assert resolve_principal(db, "") is None


def test_user_without_household_or_agent_is_not_a_principal(db: Session, seeded: dict[str, Principal]) -> None:
    db.add(User(handle="loner", display_name="Loner", is_demo=True))
    drifter = User(handle="drifter", display_name="Drifter", is_demo=True)
    db.add(drifter)
    db.flush()
    db.add(HouseholdMembership(household_id=seeded["alex"].household.id, user_id=drifter.id, role=MembershipRole.ADULT))
    db.flush()
    assert resolve_principal(db, "loner") is None  # no membership
    assert resolve_principal(db, "drifter") is None  # member, but no default agent
    db.add(Agent(household_id=seeded["alex"].household.id, owner_user_id=drifter.id, name="D", is_default=True))
    db.flush()
    assert resolve_principal(db, "drifter") is not None


def test_principal_is_frozen(seeded: dict[str, Principal]) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        seeded["alex"].user = seeded["max"].user  # type: ignore[misc]


# --- get_principal: cookie, then header, then default user ------------------------------------------------------


def test_unknown_handle_401(probe: TestClient) -> None:
    response = probe.get("/whoami", headers={"X-Uhlexa-User": "nobody"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    login = probe.post("/demo/login", json={"handle": "nobody"})
    assert login.status_code == 401
    assert login.json()["code"] == "unauthorized"
    assert SESSION_COOKIE not in login.cookies


def test_demo_auth_disabled_401(probe: TestClient, as_user: AsUser, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, ENABLE_DEMO_AUTH="false")
    assert probe.get("/whoami", headers=as_user("alex")).status_code == 401
    assert probe.get("/whoami").status_code == 401
    assert probe.post("/demo/login", json={"handle": "alex"}).status_code == 401
    # the user list stays public so the sign-in screen can still render
    assert probe.get("/demo/users").status_code == 200


def test_header_identifies_the_user(probe: TestClient, as_user: AsUser) -> None:
    response = probe.get("/whoami", headers=as_user("max"))
    assert response.status_code == 200
    assert response.json() == {"handle": "max", "role": "child", "agent": "Max's Uhlexa"}


def test_header_handle_is_normalised(probe: TestClient) -> None:
    assert probe.get("/whoami", headers={"X-Uhlexa-User": " Katie "}).json()["handle"] == "katie"


def test_default_user_when_no_credentials(probe: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert probe.get("/whoami").json()["handle"] == "alex"
    _set_env(monkeypatch, DEMO_DEFAULT_USER="katie")
    assert probe.get("/whoami").json()["handle"] == "katie"
    _set_env(monkeypatch, DEMO_DEFAULT_USER="ghost")
    assert probe.get("/whoami").status_code == 401


def test_blank_header_counts_as_absent(probe: TestClient) -> None:
    assert probe.get("/whoami", headers={"X-Uhlexa-User": "  "}).json()["handle"] == "alex"


def test_login_cookie_works_and_beats_the_header(probe: TestClient, as_user: AsUser) -> None:
    login = probe.post("/demo/login", json={"handle": "katie"})
    assert login.status_code == 200
    assert read_session(login.cookies[SESSION_COOKIE]) == "katie"
    assert probe.get("/whoami").json()["handle"] == "katie"
    # the cookie is signed, the header is not: the signed identity wins
    assert probe.get("/whoami", headers=as_user("max")).json()["handle"] == "katie"


def test_invalid_cookie_is_never_replaced_by_the_default_user(probe: TestClient, as_user: AsUser) -> None:
    probe.cookies.set(SESSION_COOKIE, "forged.1.deadbeef")
    stale = probe.get("/whoami")
    assert stale.status_code == 401
    assert stale.json()["code"] == "unauthorized"
    # an explicit header still identifies the caller (the simulator sends it on every request)
    assert probe.get("/whoami", headers=as_user("max")).json()["handle"] == "max"


def test_expired_cookie_is_rejected(probe: TestClient) -> None:
    probe.cookies.set(SESSION_COOKIE, sign_session("alex", now=1_000))
    assert probe.get("/whoami").status_code == 401


def test_login_sets_an_httponly_signed_cookie(probe: TestClient) -> None:
    response = probe.post("/demo/login", json={"handle": "max"})
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    assert "Secure" not in cookie  # plain http in dev
    assert f"Max-Age={SESSION_MAX_AGE_SECONDS}" in cookie
    body = response.json()
    assert body["user"]["handle"] == "max" and body["role"] == "child"
    assert "session" not in str(body).lower().replace("uhlexa_session", "")  # no token echoed in the body


def test_login_cookie_is_secure_over_https(db: Session, seeded: dict[str, Principal]) -> None:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(demo.router)
    app.dependency_overrides[get_db] = lambda: db
    response = TestClient(app, base_url="https://testserver").post("/demo/login", json={"handle": "alex"})
    assert "Secure" in response.headers["set-cookie"]


def test_login_validates_the_body(probe: TestClient) -> None:
    assert probe.post("/demo/login", json={}).status_code == 422
    assert probe.post("/demo/login", json={"handle": ""}).status_code == 422
    assert probe.post("/demo/login", json={"handle": "x" * 65}).status_code == 422
