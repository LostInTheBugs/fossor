"""Unit tests: external miner detection, password masking, wallet truncation."""

from __future__ import annotations

from app.miners import detect_external_miners, mask_command, parse_miner_cmd, truncate_wallet

SRB_CMD = (
    "srbminer-multi --algorithm pearlhash --pool pearl-eu2.luckypool.io:3360 "
    "--wallet prl1testwallet0000000000000000000000000000000000000000000000 "
    "--password x --api-port 21528 --disable-gpu --cpu-threads 0"
)
LOL_CMD = (
    "./lolMiner --algo AUTOLYKOS2 --pool stratum+tcp://akl.luckypool.io:1444 "
    "--user 0x1234567890abcdef1234567890abcdef12345678 --pass hunter2 -p 4444"
)
XMRIG_CMD = (
    "/opt/xmrig -o pool.supportxmr.com:3333 -u "
    "48edfHu7V9Z84YzzMa6fUueoELZ9ZRXq9VetWZgDMtu8GxVJ1V7rSsm4jHkXk1ZQ2i9zqZC9Z8abcdeFGH -p x"
)


class TestParseMinerCmd:
    def test_srbminer(self):
        parsed = parse_miner_cmd(SRB_CMD)
        assert parsed["name"] == "SRBMiner-MULTI"
        assert parsed["algorithm"] == "pearlhash"
        assert parsed["pool"] == "pearl-eu2.luckypool.io:3360"
        assert parsed["version"] == ""

    def test_lolminer(self):
        parsed = parse_miner_cmd(LOL_CMD)
        assert parsed["name"] == "lolMiner"
        assert parsed["algorithm"] == "AUTOLYKOS2"
        assert parsed["pool"].startswith("stratum+tcp://akl.luckypool.io:1444")

    def test_xmrig_short_flags(self):
        parsed = parse_miner_cmd(XMRIG_CMD)
        assert parsed["name"] == "XMRig"
        assert parsed["pool"] == "pool.supportxmr.com:3333"

    def test_unknown_process_is_not_a_miner(self):
        assert parse_miner_cmd("/usr/bin/python3 train.py --pool x")["name"] == ""

    def test_bzminer_and_peakminer_extensible(self):
        assert parse_miner_cmd("bzminer --algo pearlhash -o pool:1234")["name"] == "BzMiner"
        assert parse_miner_cmd("./peakminer --pool x:1")["name"] == "PeakMiner"


class TestMasking:
    def test_password_values_masked(self):
        masked = mask_command(SRB_CMD)
        assert "--password •••" in masked
        assert "hunter2" not in mask_command(LOL_CMD)
        assert " --pass •••" in mask_command(LOL_CMD)
        assert "-p •••" in mask_command(XMRIG_CMD)

    def test_wallet_truncated_everywhere(self):
        for cmd in (SRB_CMD, LOL_CMD, XMRIG_CMD):
            masked = mask_command(cmd)
            assert "prl1testwallet0000" not in masked
            assert "1234567890abcdef1234567890abcdef12345678" not in masked
            assert "48edfHu7V9Z84Yzz" not in masked
            assert "…" in masked

    def test_truncate_wallet_short_value_kept(self):
        assert truncate_wallet("abc") == "abc"
        long = "prl1testwallet" + "x" * 50
        assert truncate_wallet(long).endswith(long[-4:])
        assert len(truncate_wallet(long)) < len(long)

    def test_pool_credentials_masked(self):
        masked = mask_command("lolMiner -o stratum+tcp://wallet.rig:pass@pool.example:4444")
        assert "wallet.rig:pass" not in masked
        assert "pool.example:4444" in masked


class TestComputeApps:
    def test_4_columns_header_and_gpu(self):
        compute = (
            "pid, process_name, gpu_id, used_memory\n"
            "4242, /usr/bin/srbminer-multi, 0, 512 MiB\n"
        )
        miners = detect_external_miners(compute, SRB_CMD)
        assert len(miners) == 1
        miner = miners[0]
        assert miner["gpu_id"] == 0
        assert miner["pid"] == 4242
        assert miner["miner_name"] == "SRBMiner-MULTI"
        assert miner["algorithm"] == "pearlhash"
        assert miner["pool"] == "pearl-eu2.luckypool.io:3360"
        assert "prl1testwallet0000" not in miner["cmd"]

    def test_3_columns_legacy(self):
        compute = "777, xmrig, 128\n"
        miners = detect_external_miners(compute, XMRIG_CMD)
        assert miners and miners[0]["gpu_id"] is None
        assert miners[0]["miner_name"] == "XMRig"

    def test_non_miner_compute_app_ignored(self):
        compute = "12, /usr/bin/python3, 0, 9000 MiB\n"
        assert detect_external_miners(compute, "/usr/bin/python3 train.py") == []

    def test_fallback_on_ps_only(self):
        miners = detect_external_miners("", LOL_CMD + "\n" + XMRIG_CMD)
        names = [m["miner_name"] for m in miners]
        assert names == ["lolMiner", "XMRig"]
