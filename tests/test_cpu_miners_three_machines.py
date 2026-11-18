"""Non-regression: the three real machines keep their CPU hashrate (004-c4).

The ``ps`` snapshots are real ``ps -eo user:32,pid,pcpu,etimes,args`` captures
(2026-11-18, 00:24Z) with wallets replaced by obvious fakes.  Each test applies
the **production** ``PS_MINERS`` filter (extracted from the whitelisted
``collect_stats`` command, never copied), plans the probes with the production
planner, then feeds the probe output really returned by the machine.

Regression 2026.11.004-c3: the KORSH solo script runs at 0 % CPU inside
``screen``, so the ``ps`` filter dropped it, no log probe was planned and the
rig lost its 6.25 kH/s.  On c3 these tests fail (no ``ps_miners_command`` and no
KORSH hashrate).
"""

from __future__ import annotations

import subprocess

import pytest

from app import ssh_client
from app.ssh_client import cpu_probe_command
from app.cpu_stats import attach_cpu_hashrates, parse_cpu_report, plan_cpu_probes
from app.stats_parser import parse_collect_stats

# ── Real `ps` captures (wallets replaced by fakes) ────────────────────────────

RAW_PS_RIG = """USER                                 PID %CPU ELAPSED COMMAND
root                                 697  0.0  303455 SCREEN -dmS wam bash -c exec /root/wam/wam-miner -o stratum+tcp://pool.wamcoin.org:3333 -u wam1qtest0000000000000000000000000000000000.rig -t 2 >> /root/wam/miner.log 2>&1
root                                 699  191  303455 /root/wam/wam-miner -o stratum+tcp://pool.wamcoin.org:3333 -u wam1qtest0000000000000000000000000000000000.rig -t 2
root                                3035 34.4  303409 ./SRBMiner-MULTI --algorithm pearlhash --pool pearl-eu2.luckypool.io:3360 --wallet prl1qtest0000+TEST --password x --api-enable --api-port 21550
root                                4063 92.8  303402 /root/korsh/bin/korshd -datadir=/root/korsh/data -daemon
root                                4992  0.0  303461 SCREEN -dmS korsh-miner /root/korsh/korsh-miner.sh
root                                4993  0.0  303461 /bin/bash /root/korsh/korsh-miner.sh
root                             3290493  0.0      20 timeout 180 /root/korsh/bin/korsh-cli -datadir=/root/korsh/data generatetoaddress 1 Sbjtest000000000000000000000 200000
root                             3290494  0.0      20 /root/korsh/bin/korsh-cli -datadir=/root/korsh/data generatetoaddress 1 Sbjtest000000000000000000000 200000
"""

RAW_PS_RYZEN = """USER                                 PID %CPU ELAPSED COMMAND
root                              913316  0.0  415342 /root/mining/oms-supryolo/supryolo --url stratum+tcp://stratum.cryptopickaxe.co.uk:6030 --user oms1qtest0000000000000000000000.ryzen-cpu --cpu --cpu-threads 10 --no-gpu --no-tui --log-file /var/log/cpu-miner.log
administrator                    1104500  0.0  348365 bash -c cd ~/bigcoin-mine/bigcoin && nohup ./mine-bigcoin.sh > ~/bigcoin-mine/bigcoin/mine.log 2>&1 & echo "MINER_PID=$!"; sleep 3; ps aux | grep -E "bigcoin|mine" | grep -v grep
administrator                    1104502  0.0  348365 bash ./mine-bigcoin.sh
administrator                    1104510 1097  348365 ./bitcoind -datadir=/home/administrator/bigcoin-mine/bigcoin/data -daemon
administrator                    1106304  0.0  348038 bash ./mine-bigcoin.sh
administrator                    4029099  0.0      29 bash ./mine-bigcoin.sh
administrator                    4029100  0.0      29 ./bigcoin-cli -datadir=/home/administrator/bigcoin-mine/bigcoin/data -rpcclienttimeout=3600 generatetoaddress 1 big1qtest000000000000000000000 20820
"""

RAW_PS_ATHLON = """USER                                 PID %CPU ELAPSED COMMAND
root                                1270  198  199747 /home/administrator/cpuminer-korsh --algo yespower --param-n 256 --param-r 8 --url stratum+tcp://pool.korsh.org:3333 --user Sbjtest000000000000000000000.athlon-korsh --threads 2
administrator                       1187  6.3  199751 ./srbminer/srbminer_compute --algorithm pearlhash --wallet prl1qtest0000+TEST --worker athlon-3000g --pool pearl-eu2.luckypool.io:3360 --api-enable --api-port 21550
"""

# ── Probe output really returned by each machine (same sections) ─────────────

#: ``/root/korsh/mining.log``, real lines (scan = 200000 nonces, ~32 s apart).
KORSH_LOG = """2026-11-18T00:23:07Z no block (scan 200k)
2026-11-18T00:23:39Z no block (scan 200k)
2026-11-18T00:24:11Z no block (scan 200k)
2026-11-18T00:24:42Z no block (scan 200k)
2026-11-18T00:25:14Z no block (scan 200k)
2026-11-18T00:25:46Z no block (scan 200k)
2026-11-18T00:26:17Z no block (scan 200k)
2026-11-18T00:26:49Z no block (scan 200k)
2026-11-18T00:27:21Z no block (scan 200k)
2026-11-18T00:27:53Z no block (scan 200k)"""

WAM_LOG = """01:14:27 accept   share accepted (21189 total)
01:14:50 accept   share accepted (21190 total)
01:14:51 stats    1.19 kH/s   accepted 21190  rejected 88  blocks 1  total 358306564 hashes
01:15:20 job      job 23ea  height 43871  diff 4e-06  (new block)
01:15:21 stats    1.15 kH/s   accepted 21191  rejected 88  blocks 1  total 358341679 hashes"""

#: ``~/bigcoin-mine/bigcoin/mine.log``, real format (total already per machine).
BIG_LOG = """[23:50:37] mining ... height=20363 difficulty=0.003286003385533663 algo=yespower 614 hashes/s/worker (~6754 total over 11 workers)
[23:52:04] mining ... height=20363 difficulty=0.003286003385533663 algo=yespower 678 hashes/s/worker (~7458 total over 11 workers)
[23:53:38] mining ... height=20364 difficulty=0.003276917778190243 algo=yespower 622 hashes/s/worker (~6842 total over 11 workers)
[23:54:39] mining ... height=20365 difficulty=0.003282253682975613 algo=yespower 601 hashes/s/worker (~6611 total over 11 workers)
[23:56:08] mining ... height=20365 difficulty=0.003282253682975613 algo=yespower 621 hashes/s/worker (~6831 total over 11 workers)
[23:57:09] mining ... height=20366 difficulty=0.003288198190821163 algo=yespower 600 hashes/s/worker (~6600 total over 11 workers)"""

#: ``/var/log/athlon-korsh-miner.log`` (cpuminer), real lines.
KSH_CPUMINER_LOG = """[2026-11-18 00:23:10] yespower: stratum+tcp://pool.korsh.org:3333
                      Periodic Report     5m04s        2d07h
                      Hash rate          0.00h/s      0.00h/s   (235.66Mh/s)"""

PROBE_RIG = """### JLOG 697
-- No entries --
### LOGI 4993 /root/korsh/mining.log
1794961673
{korsh}
### JLOG 4993
-- No entries --
### LOGI 699 /root/wam/miner.log
1794961532
{wam}
### JLOG 699
-- No entries --
### NODE korsh
{{
  "blocks": 78497,
  "difficulty": 0.044140765497258,
  "networkhashps": 2765036.432421875
}}
### CHAIN korsh
{{
  "chain": "main",
  "blocks": 78497,
  "headers": 78497
}}
### NETPS korsh
2765036.432421875
""".format(korsh=KORSH_LOG, wam=WAM_LOG)

PROBE_RYZEN = """### LOGI 1104502 /home/administrator/bigcoin-mine/bigcoin/mine.log
1794961483
{big}
### NODE bigcoin
{{
  "blocks": 20373,
  "difficulty": 0.003281328108977646,
  "networkhashps": 28314.78960865774
}}
### NETPS bigcoin
28314.78960865774
""".format(big=BIG_LOG)

PROBE_ATHLON = """### API 4048
### LOGI 1270 /var/log/athlon-korsh-miner.log
1794961529
{ksh}
""".format(ksh=KSH_CPUMINER_LOG)


def _filtered(raw_ps: str) -> str:
    """Run the production ``PS_MINERS`` filter on a raw ``ps`` capture.

    The ``ps`` of the command is replaced by ``cat`` of the capture: the awk
    program itself is the one from the whitelisted command, never a copy.
    """
    # c4 exposes the filter through a helper; on an older tree (c3) the same
    # line is read straight from the whitelisted command, so the test still
    # exercises the real filter instead of failing on an import.
    helper = getattr(ssh_client, "ps_miners_command", None)
    if helper is not None:
        command = helper()
    else:
        command = next(
            line.strip()
            for line in ssh_client.WHITELISTED_COMMANDS["collect_stats"].splitlines()
            if line.strip().startswith("ps -eo user:32")
        )
    awk = command.split("| awk", 1)[1]
    result = subprocess.run(
        ["bash", "-c", "cat | awk" + awk],
        input=raw_ps, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _miners(raw_ps: str, probe_stdout: str, now: float) -> list[dict]:
    ps = _filtered(raw_ps)
    stats = parse_collect_stats("### PS_MINERS\n" + ps + "\n### UPTIME\n100.0 200.0\n")
    miners = [m for m in stats["miners"] if str(m.get("kind") or "") in ("cpu", "solo")]
    probes = plan_cpu_probes(ps)
    attach_cpu_hashrates(miners, probes, parse_cpu_report(probe_stdout), now=now)
    return miners


def _hashrate(miners: list[dict], coin: str) -> tuple[float | None, str]:
    for miner in miners:
        if str(miner.get("coin") or "").upper() == coin and miner.get("hashrate_hs"):
            return float(miner["hashrate_hs"]), str(miner.get("hashrate_source") or "")
    return None, ""


@pytest.fixture(autouse=True)
def _no_ssh(monkeypatch):
    """The test never contacts a machine: any SSH attempt is a failure."""

    async def boom(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("SSH interdit dans ce test")

    monkeypatch.setattr(ssh_client, "ssh_collect_cpu", boom)
    monkeypatch.setattr(ssh_client, "ssh_collect_stats", boom)


def test_rig_keeps_wam_and_korsh_solo_hashrate():
    ps = _filtered(RAW_PS_RIG)
    # The script runs at 0 % CPU inside screen: it must survive the filter.
    assert "/bin/bash /root/korsh/korsh-miner.sh" in ps
    assert "SCREEN -dmS korsh-miner" in ps

    probes = plan_cpu_probes(ps)
    korsh = next((n for n in probes["nodes"] if n["key"] == "korsh"), None)
    assert korsh is not None, probes
    assert korsh["solo_pids"]["script"] == [4993]
    # The probe must read the log the script itself declares (fd 255).
    command = cpu_probe_command(probes)
    assert "fd/255" in command and "4993" in command

    miners = _miners(RAW_PS_RIG, PROBE_RIG, now=1794961673 + 60)
    wam, wam_source = _hashrate(miners, "WAM")
    ksh, ksh_source = _hashrate(miners, "KSH")
    assert wam and wam > 900, miners
    assert ksh and abs(ksh - 6250) / 6250 < 0.05, (ksh, ksh_source, miners)


def test_ryzen_keeps_big_solo_hashrate():
    miners = _miners(RAW_PS_RYZEN, PROBE_RYZEN, now=1794961483 + 60)
    big, source = _hashrate(miners, "BIG")
    assert big and abs(big - 6600) / 6600 < 0.20, (big, source, miners)


def test_athlon_keeps_cpuminer_hashrate():
    miners = _miners(RAW_PS_ATHLON, PROBE_ATHLON, now=1794961529 + 60)
    ksh, source = _hashrate(miners, "KSH")
    assert ksh and ksh > 1e6, (ksh, source, miners)
