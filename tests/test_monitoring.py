from __future__ import annotations

import subprocess
from datetime import timedelta
from types import SimpleNamespace

import paramiko
from sqlalchemy import select

from app.db.models import Alert
from app.monitoring.alerts import AlertEngine, Thresholds
from app.monitoring.collector import LocalCollector
from app.monitoring.hosts import HostConfig, load_hosts
from app.monitoring.models import HostMetrics
from app.monitoring.notifiers import CompositeNotifier
from app.monitoring.remote import (
    SshCollector,
    build_remote_script,
    parse_cpu,
    parse_df,
    parse_meminfo,
    parse_remote_output,
)


class FakeWinService:
    def __init__(self, name: str, status: str):
        self._name, self._status = name, status

    def name(self) -> str:
        return self._name

    def status(self) -> str:
        return self._status


def fake_psutil(cpu=42.0, mem=63.5, partitions=None, services=None):
    usage = {"/": (100, 50, 50.0), "/var": (200, 190, 95.0), "C:\\": (500, 100, 20.0)}
    return SimpleNamespace(
        cpu_percent=lambda interval=None: cpu,
        virtual_memory=lambda: SimpleNamespace(percent=mem),
        disk_partitions=lambda all=False: partitions
        or [
            SimpleNamespace(mountpoint="/", fstype="ext4", opts="rw"),
            SimpleNamespace(mountpoint="/var", fstype="xfs", opts="rw"),
            SimpleNamespace(mountpoint="/snap/core", fstype="squashfs", opts="ro"),
        ],
        disk_usage=lambda m: SimpleNamespace(
            total=usage[m][0], used=usage[m][1], percent=usage[m][2]
        ),
        win_service_iter=lambda: iter(services or []),
    )


# ---------------------------------------------------------------- collector local


def test_local_collector_linux_metrics_and_systemd():
    def runner(cmd, **kwargs):
        status = "active" if cmd[-1] == "nginx" else "inactive"
        return subprocess.CompletedProcess(
            cmd, 0 if status == "active" else 3, stdout=status + "\n"
        )

    collector = LocalCollector(
        "web-01", services=["nginx", "cron"], ps=fake_psutil(), platform="linux", runner=runner
    )
    m = collector.collect()
    assert (m.cpu_percent, m.memory_percent) == (42.0, 63.5)
    assert [d.mount for d in m.disks] == ["/", "/var"]  # squashfs se ignora
    assert m.disks[1].percent == 95.0
    assert [(s.name, s.running, s.status) for s in m.services] == [
        ("nginx", True, "active"),
        ("cron", False, "inactive"),
    ]


def test_local_collector_windows_services():
    ps = fake_psutil(
        partitions=[SimpleNamespace(mountpoint="C:\\", fstype="NTFS", opts="rw,fixed")],
        services=[FakeWinService("W3SVC", "running"), FakeWinService("MSSQLSERVER", "stopped")],
    )
    collector = LocalCollector(
        "win-01", services=["w3svc", "MSSQLSERVER", "Spooler"], ps=ps, platform="win32"
    )
    m = collector.collect()
    assert m.platform == "windows"
    assert [d.mount for d in m.disks] == ["C:\\"]
    assert [(s.name, s.running, s.status) for s in m.services] == [
        ("w3svc", True, "running"),
        ("MSSQLSERVER", False, "stopped"),
        ("Spooler", False, "not_found"),
    ]


def test_local_collector_explicit_disk_paths():
    collector = LocalCollector("h", disk_paths=["/var"], ps=fake_psutil(), platform="linux")
    assert [d.mount for d in collector.collect().disks] == ["/var"]


# ---------------------------------------------------------------- collector remoto (SSH)

REMOTE_OUTPUT = """cpu  1000 0 500 8000 500 0 0 0 0 0
cpu  1300 0 700 8400 600 0 0 0 0 0
---MEM
MemTotal:       8000000 kB
MemFree:         500000 kB
MemAvailable:   2000000 kB
---DF
Filesystem         1-blocks       Used  Available Capacity Mounted on
/dev/sda1       100000000   80000000   20000000      80% /
/dev/sdb1       200000000   20000000  180000000      10% /data
---SVC
nginx active
postgresql failed
"""


def test_parse_helpers():
    # delta total = 1000, delta idle(+iowait) = 500 -> 50%
    assert parse_cpu(*REMOTE_OUTPUT.splitlines()[:2]) == 50.0
    assert parse_meminfo("MemTotal: 1000 kB\nMemAvailable: 250 kB") == 75.0
    disks = parse_df("Filesystem 1-blocks Used Available Capacity Mounted\n/dev/x 10 9 1 90% /")
    assert disks[0].percent == 90.0


def test_parse_remote_output():
    m = parse_remote_output("db-01", REMOTE_OUTPUT)
    assert m.cpu_percent == 50.0
    assert m.memory_percent == 75.0
    assert [(d.mount, d.percent) for d in m.disks] == [("/", 80.0), ("/data", 10.0)]
    assert [(s.name, s.running) for s in m.services] == [("nginx", True), ("postgresql", False)]


def test_remote_script_quotes_arguments():
    script = build_remote_script(["/", "/mnt/my disk"], ["nginx"])
    assert "'/mnt/my disk'" in script
    assert "systemctl is-active nginx" in script


class FakeSSHClient:
    def __init__(self, output: str = REMOTE_OUTPUT, fail: Exception | None = None):
        self.output = output
        self.fail = fail
        self.connected_with: dict = {}
        self.closed = False

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, hostname, **kwargs):
        if self.fail:
            raise self.fail
        self.connected_with = {"hostname": hostname, **kwargs}

    def exec_command(self, cmd, timeout=None):
        self.cmd = cmd
        return None, SimpleNamespace(read=lambda: self.output.encode()), None

    def close(self):
        self.closed = True


def test_ssh_collector_success():
    fake = FakeSSHClient()
    collector = SshCollector(
        "db-01",
        "10.0.0.5",
        "monitor",
        services=["nginx", "postgresql"],
        client_factory=lambda: fake,
    )
    m = collector.collect()
    assert m.reachable and m.cpu_percent == 50.0
    assert fake.connected_with["hostname"] == "10.0.0.5"
    assert fake.connected_with["username"] == "monitor"
    assert isinstance(fake.policy, paramiko.RejectPolicy)
    assert fake.closed


def test_ssh_collector_unreachable():
    fake = FakeSSHClient(fail=TimeoutError("timed out"))
    m = SshCollector("db-01", "10.0.0.5", "monitor", client_factory=lambda: fake).collect()
    assert not m.reachable
    assert "timed out" in m.error
    assert fake.closed


# ---------------------------------------------------------------- hosts YAML


def test_load_hosts_yaml(tmp_path):
    path = tmp_path / "hosts.yaml"
    path.write_text(
        """
hosts:
  - name: local
    type: local
    services: [nginx]
  - name: db-01
    type: ssh
    address: 10.0.0.5
    username: monitor
    thresholds:
      disk: {warning: 70, critical: 85}
""",
        encoding="utf-8",
    )
    hosts = load_hosts(path)
    assert [h.name for h in hosts] == ["local", "db-01"]
    assert hosts[1].thresholds["disk"].critical == 85


def test_load_hosts_missing_file_defaults_to_local(tmp_path):
    hosts = load_hosts(tmp_path / "nope.yaml")
    assert len(hosts) == 1 and hosts[0].type == "local"


# ---------------------------------------------------------------- servicio + endpoints


class StaticCollector:
    def __init__(self, name: str, metrics: HostMetrics):
        self.name = name
        self.metrics = metrics

    def collect(self) -> HostMetrics:
        return self.metrics


def test_monitoring_endpoints_persist_alerts(client, app, auth_headers):
    from app.monitoring.service import MonitoringService

    hot = HostMetrics(host="web-01", cpu_percent=99.0, memory_percent=20.0)
    hosts = [HostConfig(name="web-01")]
    app.state.monitoring = MonitoringService(
        hosts=hosts,
        engine=AlertEngine(Thresholds(), timedelta(minutes=15)),
        notifier=CompositeNotifier([]),
        session_factory=app.state.session_factory,
        collector_factory=lambda cfg: StaticCollector(cfg.name, hot),
    )

    resp = client.post("/monitoring/check", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["cpu_percent"] == 99.0

    # Segundo chequeo: dentro del cooldown no se duplica la alerta
    client.post("/monitoring/check", headers=auth_headers)

    alerts = client.get("/monitoring/alerts", headers=auth_headers).json()
    assert alerts["total"] == 1
    assert alerts["items"][0]["severity"] == "critical"

    hosts_resp = client.get("/monitoring/hosts", headers=auth_headers).json()
    assert hosts_resp[0]["host"] == "web-01"
    assert client.get("/monitoring/hosts/web-01/metrics", headers=auth_headers).status_code == 200
    assert client.get("/monitoring/hosts/zzz/metrics", headers=auth_headers).status_code == 404

    with app.state.session_factory() as db:
        assert db.scalar(select(Alert.metric)) == "cpu"


def test_health_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json()["database"] == "ok"
