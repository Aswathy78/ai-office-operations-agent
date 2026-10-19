# Backend setup

Install the declared backend runtime and test dependencies with:

```powershell
python -m pip install -r backend/requirements.txt
```

## Database migrations

Apply database migrations from the `backend` directory with:

```powershell
python -m alembic upgrade head
```

The application still calls `Base.metadata.create_all` at startup for newly
introduced tables. Alembic migrations are required to apply schema changes to
databases that already exist.

## Recording encryption configuration

Recording encryption requires `RECORDING_ENCRYPTION_KEY`. The value is expected to be a base64-encoded 32-byte key when AES-256-GCM storage is implemented. The application currently has no `.env` loader, so supply the real value directly through the backend process or deployment environment. Do not put a real key in `.env.example`, commit it to Git, or store it in the database. The example file contains only a placeholder.

This configuration preparation does not implement encryption or change recording upload behavior.

## Meeting cancellation approval (Story 5.4)

Cancellation requests for meetings above 20 unique attendees (participants plus
the organizer) remain pending until approved. The approver policy reuses Story
5.3: only the meeting organizer or an authorized delegate can approve or reject
the request. The repository does not define a separate High-risk role or
manager/admin approval policy. Set
`MEETING_CANCELLATION_APPROVAL_ATTENDEE_THRESHOLD` to change the default
threshold; values below 1 or non-integer values fall back to the default.

authentication layer populates `request.state.authenticated_principal.user_id`.
authentication middleware that sets the principal from verified credentials;
do not copy an identity header directly into request state.
Story 5.4 cancellation, approval, participant, roster, and audit endpoints
require a verified Bearer access token. The backend validates its signature,
issuer, audience, expiry, and revocation record, then maps its account subject
to an active `Employee` before setting
`request.state.authenticated_principal`. It does not accept `X-User-Id` or
`organizer_id` as proof of identity. The frontend signs in through the local
authentication endpoints, keeps the access token in memory, and sends it in
the authorization header for protected API requests. Native browser WebSocket
connections do not send this token; query parameters are not trusted for
identity, so organizer-only WebSocket controls remain unavailable to
unauthenticated connections.

### Provisioning Local Accounts

Accounts are provisioned only for existing active employees; public
self-registration is not enabled. From the `backend` directory, an operator
with shell access can run:

```powershell
python -m alembic upgrade head
python -m app.auth_provision --employee-email employee@example.com
```

The command prompts for a password without echoing it and defaults the role to
`employee`. Elevated roles require an explicit role option and confirmation.
Configure `AUTH_JWT_SECRET` with at least 32 bytes of cryptographically random
material through the backend environment or its local `.env`; never commit the
secret. `AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, and
`AUTH_ACCESS_TOKEN_TTL_SECONDS` are configurable. Tokens default to 900 seconds.
The API provides `POST /api/v1/auth/login`, `GET /api/v1/auth/me`, and
`POST /api/v1/auth/logout`.

## Recording retention

The internal `enforce_recording_retention` service removes expired recording files and their database rows in a retry-safe order. This backend currently has no scheduler, worker, or periodic maintenance mechanism, so automatic invocation is deferred to a future infrastructure integration. The service can be called by a future trusted maintenance job; it is not exposed as a public HTTP endpoint.
