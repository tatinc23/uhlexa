"""Wire schemas for identity: who the caller is, the sign-in screen's user list, and login/reset bodies."""

import uuid
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from app.auth.demo_auth import Principal
from app.models import MembershipRole


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UserOut(_Out):
    id: uuid.UUID
    handle: str
    display_name: str


class HouseholdOut(_Out):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str


class AgentOut(_Out):
    id: uuid.UUID
    name: str


class PrincipalOut(_Out):
    """`GET /api/me`, `POST /demo/login`, and each entry of `GET /demo/users`. Never carries email or secrets."""

    user: UserOut
    role: MembershipRole
    household: HouseholdOut
    agent: AgentOut

    @classmethod
    def from_principal(cls, principal: Principal) -> Self:
        return cls(
            user=UserOut.model_validate(principal.user),
            role=principal.role,
            household=HouseholdOut.model_validate(principal.household),
            agent=AgentOut.model_validate(principal.agent),
        )


class LoginIn(BaseModel):
    handle: str = Field(min_length=1, max_length=64)


class DemoUsersOut(BaseModel):
    users: list[PrincipalOut]


class DemoResetOut(DemoUsersOut):
    status: Literal["reset"] = "reset"
