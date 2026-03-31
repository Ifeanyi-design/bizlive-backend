BizLive backend is now wired for `Flask-Migrate`.

Recommended first-time setup:

```powershell
cd backend
.venv\Scripts\activate
$env:FLASK_APP="run.py"
flask db init
flask db migrate -m "Initial schema"
flask db upgrade
```

For later schema changes:

```powershell
flask db migrate -m "Describe your change"
flask db upgrade
```

Notes:

- `AUTO_CREATE_TABLES=1` is fine for early local testing.
- For production Postgres, prefer migrations and set `AUTO_CREATE_TABLES=0`.
