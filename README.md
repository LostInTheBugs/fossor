# Fossor

Mining management application — machines, monitoring, and pilotage.

**Version:** [2026.11.001](https://github.com/LostInTheBugs/fossor/releases/tag/2026.11.001)

## Description

Fossor (ex-MineCtl) replaces the ad-hoc `mining.html` dashboard with a proper application for managing mining machines, monitoring hardware, and piloting mining campaigns.

## Installation

### Docker (recommended)

```bash
# Required environment variables:
# FOSSOR_ADMIN_PASSWORD — admin password (hashed on first startup)
# FOSSOR_MASTER_KEY   — Fernet key for secret encryption (32 url-safe base64 bytes)
# FOSSOR_PORT         — host port (default 8008)

cp .env.example .env
# Edit .env with your secrets
docker compose up -d
```

### Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8008
```

## Configuration

| Variable              | Description                              | Default     |
|-----------------------|------------------------------------------|-------------|
| `FOSSOR_ADMIN_PASSWORD` | Admin password (hashed on startup)  | *(required)*|
| `FOSSOR_MASTER_KEY`     | Fernet encryption key             | *(required)*|
| `FOSSOR_PORT`           | Host port mapping              | `8008`      |
| `DATABASE_URL`          | SQLite connection string       | `sqlite:////data/fossor.db` |

## API Endpoints

| Method | Path                          | Description              |
|--------|-------------------------------|--------------------------|
| GET    | `/api/version`                | Returns VERSION file     |
| POST   | `/api/login`                  | Admin login              |
| GET    | `/api/machines`               | List machines            |
| POST   | `/api/machines`               | Create machine           |
| GET    | `/api/machines/{id}`          | Get machine              |
| PATCH  | `/api/machines/{id}`          | Update machine           |
| DELETE | `/api/machines/{id}`          | Delete machine           |
| GET    | `/api/audit-log`              | Audit log entries        |

## Project Structure

```
sources/
├── app/
│   ├── main.py          # FastAPI entry point
│   ├── models.py         # SQLAlchemy models
│   ├── database.py       # DB setup
│   ├── auth.py           # Authentication
│   ├── secrets.py        # Fernet encryption
│   ├── dependencies.py   # FastAPI deps
│   └── routers/
│       ├── auth.py       # Login/logout routes
│       └── machines.py   # Machine CRUD
├── static/               # Frontend HTML/CSS/JS
├── tests/                # pytest suite
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── requirements.txt
├── VERSION
├── CHANGELOG.md
└── README.md
```

## License

MIT — See [LICENSE](LICENSE) for details.
