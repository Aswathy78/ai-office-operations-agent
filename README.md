# AI Office Operations Agent — User Story 1.1 UI Skeleton

This package implements the **shared frontend skeleton** for User Story 1.1 / FR-1. It intentionally does not implement the business logic owned by the other team members.

## Included

- Responsive application shell with sidebar + header
- Centralized role-based navigation
- Four UI roles: Employee, Manager, HR / Operations, Admin
- Home page with today's meetings
- Join buttons using mock meeting routes
- Create Meeting integration point
- AI Assistant / chat integration point
- Centralized protected-route checks
- Access Denied page for unauthorized direct navigation
- Accessible semantic structure and keyboard focus states
- Mobile navigation drawer behavior
- Mock user and mock meeting data

## Deliberately not included

- OAuth/OIDC/JWT backend authentication
- Server-side RBAC enforcement
- WebRTC
- Meeting recording
- Transcription / AI meeting intelligence
- AI chat execution
- Tasks backend
- Email/calendar integrations
- Dashboards / agent monitoring logic

Those are integration points for the other stories.

## Run

```bash
npm install
npm run dev
```

Then open the Vite URL shown in the terminal.

## Testing different roles

Change the initial role in `src/mocks/mockUser.ts`:

```ts
role: 'employee'
```

Supported values:

```text
employee
manager
hr_operations
admin
```

The same `UserRole` type and navigation configuration are shared throughout the UI.

## Where teammates plug in

- **Meeting implementation:** `/meetings`, `/meetings/create`, `/meetings/:meetingId`
- **AI assistant:** `/chat`
- **Tasks:** `/tasks`
- **Calendar:** `/calendar`
- **Manager / operations dashboard:** `/dashboard`
- **Knowledge / RAG:** `/documents`
- **Approvals:** `/approvals`
- **Admin:** `/admin/*`

Replace the placeholder page inside the route without changing the `AppShell`, `Sidebar`, `Header`, or centralized role configuration unless the feature actually needs a new navigation item.

## Shared files intended for this story

```text
src/config/navigation.ts
src/contexts/AuthContext.tsx
src/components/layout/*
src/components/common/AccessDenied.tsx
src/components/meetings/*
src/pages/HomePage.tsx
src/types/*
src/mocks/*
```

## Security note

Frontend route protection is only a UI layer. The project BRD requires role and department scope to be enforced server-side for every API/tool call. Replace `mockUser` with the real authenticated user once the authentication/RBAC implementation is available.
