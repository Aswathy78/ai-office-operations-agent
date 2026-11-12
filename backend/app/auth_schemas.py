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
    # Story 10.1: single-use refresh token, renewed on every refresh.
    refresh_token: str | None = None
    refresh_expires_in: int | None = None
    user: AuthenticatedUserResponse


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=10, max_length=512)


class OAuthExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=10, max_length=512)


class OAuthProviderInfo(BaseModel):
    id: Literal["google", "microsoft"]
    name: str
    enabled: bool


class OAuthProvidersResponse(BaseModel):
    providers: list[OAuthProviderInfo]
    password_enabled: bool = True


class OAuthStartResponse(BaseModel):
    authorization_url: str
