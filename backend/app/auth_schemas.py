from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AuthRole = Literal["employee", "manager", "hr_operations", "admin"]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


class AuthenticatedUserResponse(BaseModel):
    id: str
    account_id: str
    name: str
    email: str
    role: AuthRole
    team: str | None = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: AuthenticatedUserResponse