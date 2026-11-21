"""E2E du coffre : initialisation, ajout masqué, verrouillage, révélation.

Le serveur e2e démarre avec ``FOSSOR_VAULT_REVEAL_SECONDS=2`` : la valeur doit
disparaître toute seule, ce que le test attend réellement (pas seulement la
présence du bouton).
"""

from __future__ import annotations

import hashlib
import os

import pytest
from playwright.async_api import async_playwright

from tests.e2e.conftest import E2E_URL

ADMIN_PASSWORD = "adminpass"
PASSPHRASE = "coffre-e2e-phrase-2026"
SECRET = "FAKE-E2E-SEED-8b1d4c-DO-NOT-LEAK"
PASSWORD = "FAKE-E2E-PASSWORD-77c2-DO-NOT-LEAK"

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= gen[i]
    return chk


def _fake_bech32() -> str:
    acc = 0
    bits = 0
    data = [0]
    for value in bytes([7] * 20):
        acc = (acc << 8) | value
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrp = "bc"
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    mod = _polymod(expanded + data + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_vault_init_add_lock_reveal_and_auto_hide(e2e_server):
    shots = os.environ.get("FOSSOR_E2E_SHOTS", "")
    console_errors: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1400, "height": 1000})
        context.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        context.on("pageerror", lambda err: page_errors.append(str(err)))
        page = await context.new_page()

        await page.goto(E2E_URL + "/", wait_until="domcontentloaded", timeout=15000)
        await page.fill("#username", "admin")
        await page.fill("#password", ADMIN_PASSWORD)
        await page.locator("button[type=submit]").click()
        await page.wait_for_url("**/dashboard", timeout=15000)

        await page.goto(E2E_URL + "/settings", wait_until="domcontentloaded")
        await page.locator("#tabVault").click()
        await page.wait_for_selector("#panel-vault", state="visible", timeout=10000)
        await page.wait_for_selector("#vaultInitBox", state="visible", timeout=10000)

        # 1. Initialisation : avertissement « aucune récupération » visible.
        init_text = await page.inner_text("#vaultInitBox")
        assert "jamais stockée" in init_text and "sans recours" in init_text
        if shots:
            await page.screenshot(path=f"{shots}/vault-1-init.png", full_page=True)
        await page.fill("#vaultInitPass", PASSPHRASE)
        await page.fill("#vaultInitPass2", PASSPHRASE)
        await page.locator("#vaultInitBtn").click()
        await page.wait_for_selector("#vaultUnlockedBox", state="visible", timeout=10000)
        assert "DÉVERROUILLÉ" in (await page.inner_text("#vaultState")).upper()
        assert "verrouillage dans" in (await page.inner_text("#vaultCountdown")).lower()

        # 2. Ajout : la liste montre les métadonnées, jamais le secret.
        address = _fake_bech32()
        await page.fill("#vaultAddCoin", "BTC")
        await page.fill("#vaultAddNetwork", "mainnet")
        await page.fill("#vaultAddAddress", address)
        await page.fill("#vaultAddMachine", "rig-e2e")
        await page.fill("#vaultAddPath", "/tmp/e2e-wallet.dat")
        await page.fill("#vaultAddNote", "wallet e2e")
        await page.fill("#vaultAddSeed", SECRET)
        await page.fill("#vaultAddPass", PASSWORD)
        await page.locator("#vaultAddBtn").click()
        await page.wait_for_selector("#vaultTableBody tr[data-wallet]", timeout=10000)
        row_text = await page.inner_text("#vaultTableBody tr[data-wallet]")
        assert "BTC" in row_text and "seed" in row_text
        assert SECRET not in row_text and PASSWORD not in row_text
        body_text = await page.inner_text("body")
        assert SECRET not in body_text and PASSWORD not in body_text
        if shots:
            await page.screenshot(path=f"{shots}/vault-2-liste-masquee.png", full_page=True)

        # 3. Verrouillage manuel : le formulaire de déverrouillage revient.
        await page.locator("#vaultLockBtn").click()
        await page.wait_for_selector("#vaultLockBox", state="visible", timeout=10000)
        assert "VERROUILLÉ" in (await page.inner_text("#vaultState")).upper()
        assert await page.locator("#vaultUnlockedBox").is_visible() is False

        # 4. Révélation : phrase ressaisie, valeur visible puis effacée seule.
        await page.fill("#vaultUnlockPass", PASSPHRASE)
        await page.locator("#vaultUnlockBtn").click()
        await page.wait_for_selector("#vaultUnlockedBox", state="visible", timeout=10000)
        await page.select_option("#vaultRevealField", "seed")
        await page.fill("#vaultRevealPass", PASSPHRASE)
        await page.locator("#vaultRevealBtn").click()
        await page.wait_for_selector("#vaultRevealOut", state="visible", timeout=10000)
        reveal_text = await page.inner_text("#vaultRevealOut")
        assert SECRET in reveal_text
        assert "effacement dans" in reveal_text
        if shots:
            await page.screenshot(path=f"{shots}/vault-3-revelation.png", full_page=True)

        await page.wait_for_selector("#vaultRevealOut", state="hidden", timeout=8000)
        assert await page.inner_text("#vaultRevealOut") == ""
        assert SECRET not in await page.inner_text("body")

        await context.close()
        await browser.close()

    assert page_errors == []
    assert console_errors == []
