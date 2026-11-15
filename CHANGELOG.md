# Changelog

All notable changes to minectl are documented in this file.

## [2026.11.001] — 2026-11-15

### Added
- FastAPI application with SQLite backend
- Docker deployment (port 8008, ./data:/data volume)
- Admin login with bcrypt password hashing, HttpOnly + SameSite=Strict cookie
- Fernet-encrypted secrets storage (master key from MINECTL_MASTER_KEY)
- Machine CRUD API (/api/machines) and Machines management screen
- Machine fields: name, host, ssh_port (default 22), ssh_user, auth_type, secret_ref, sudo, os_kind, host_key, usage, vm_host, enabled
- Audit log table and screen (masked secrets)
- GET /api/version endpoint
- Secrets never exposed in API responses (only `has_secret: true`)
- pytest suite: machine CRUD, no-secret leak test, login required, default port 22
