# Backend setup

Install the declared backend runtime and test dependencies with:

```powershell
python -m pip install -r backend/requirements.txt
```

## Recording encryption configuration

Recording encryption requires `RECORDING_ENCRYPTION_KEY`. The value is expected to be a base64-encoded 32-byte key when AES-256-GCM storage is implemented. The application currently has no `.env` loader, so supply the real value directly through the backend process or deployment environment. Do not put a real key in `.env.example`, commit it to Git, or store it in the database. The example file contains only a placeholder.

This configuration preparation does not implement encryption or change recording upload behavior.

## Recording retention

The internal `enforce_recording_retention` service removes expired recording files and their database rows in a retry-safe order. This backend currently has no scheduler, worker, or periodic maintenance mechanism, so automatic invocation is deferred to a future infrastructure integration. The service can be called by a future trusted maintenance job; it is not exposed as a public HTTP endpoint.
