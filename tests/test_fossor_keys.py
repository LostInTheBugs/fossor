"""§4.1 bis — Fossor's own SSH identity: generation, persistence, export."""

from __future__ import annotations


class TestFossorKey:
    def test_generated_with_instance_comment(self, setup_test_env):
        from app.fossor_keys import ensure_key, public_key_text

        info = ensure_key()
        assert info["created"] is True
        line = public_key_text()
        assert line.startswith("ssh-ed25519 ")
        assert line.split()[-1].startswith("fossor@")

    def test_stable_across_restart_and_usable_for_ssh(self, setup_test_env):
        from app import fossor_keys
        from app.ssh_client import _auth_kwargs

        first = fossor_keys.public_key_text()
        # No in-memory cache: a second call re-reads the database, exactly like
        # a process restart would.
        assert fossor_keys.public_key_text() == first

        kwargs, tmp_path = _auth_kwargs("fossor_key", "")
        assert tmp_path is None
        key = kwargs["client_keys"][0]
        assert key.export_public_key().decode().strip() == first

    def test_private_key_encrypted_at_rest(self, setup_test_env):
        from app.fossor_keys import PRIVATE_KEY_SETTING, ensure_key
        from app.database import SessionLocal
        from app.models import AppSetting

        ensure_key()
        db = SessionLocal()
        try:
            row = db.get(AppSetting, PRIVATE_KEY_SETTING)
            assert row is not None
            assert "PRIVATE KEY" not in row.value
            assert "ssh-ed25519" not in row.value
        finally:
            db.close()

    def test_export_with_secrets_carries_the_key_and_import_restores_it(
        self, setup_test_env
    ):
        import json

        from app.config_io import apply_import, build_export
        from app.database import SessionLocal
        from app.fossor_keys import PRIVATE_KEY_SETTING, PUBLIC_KEY_SETTING, ensure_key, public_key_text
        from app.models import AppSetting

        ensure_key()
        original = public_key_text()
        db = SessionLocal()
        try:
            doc = build_export(db, include_secrets=True, passphrase="phrase-test")
            assert "ssh_key" in doc["secrets"]
            assert "PRIVATE KEY" not in json.dumps(build_export(db, include_secrets=False))
            # Simulate a fresh instance: drop the identity, then import the file.
            for key in (PRIVATE_KEY_SETTING, PUBLIC_KEY_SETTING):
                row = db.get(AppSetting, key)
                if row is not None:
                    db.delete(row)
            db.commit()
            apply_import(db, doc, mode="merge", passphrase="phrase-test")
        finally:
            db.close()

        assert public_key_text() == original
