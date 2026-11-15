# Changelog

> MineCtl a été renommé **Fossor**. Les fonctionnalités et l'API restent identiques.

All notable changes to fossor are documented in this file.

## [2026.11.001-c1] — 2026-11-15

### Security fixes
- Sessions expire correctly: `URLSafeTimedSerializer` with `max_age` enforcement in `get_current_user()`; removed hand-rolled `exp` claim
- Forged cookies rejected (wrong signing key → 401)
- Test: expired token → 401, forged cookie → 401

### Bug fixes
- Admin account persisted in database on startup (via `FOSSOR_ADMIN_PASSWORD`)
- Test isolation: all tests use `tmp_path` for DB and signing key — pass on fresh `git clone`
- Test teardown drops tables between test classes

### Tests
- 25 passing tests: CRUD, auth-required, no-secret-leak, forged-cookie, expired-token

## [2026.11.001] — 2026-11-15

### Added
- FastAPI application with SQLite backend
- Docker deployment (port 8008, ./data:/data volume)
- Admin login with bcrypt password hashing, HttpOnly + SameSite=Strict cookie
- Fernet-encrypted secrets storage (master key from FOSSOR_MASTER_KEY)
- Machine CRUD API (/api/machines) and Machines management screen
- Machine fields: name, host, ssh_port (default 22), ssh_user, auth_type, secret_ref, sudo, os_kind, host_key, usage, vm_host, enabled
- Audit log table and screen (masked secrets)
- GET /api/version endpoint
- Secrets never exposed in API responses (only `has_secret: true`)
- pytest suite: machine CRUD, no-secret leak test, login required, default port 22
