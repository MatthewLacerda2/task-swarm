"""One sandbox per agent: systemd-run (limits) -> pasta (network) -> bwrap.

The agent sees the system directories read-only, its own folder read-write, and the
run's shared state under /task. /home, /mnt, /media, /run and /tmp are replaced by
empty or private mounts, so the user's personal files and every credential in their
home directory are invisible.

Network: pasta gives the sandbox its own network namespace. Loopback inside it only
reaches the host port forwarded with -T (SearXNG). With block_lan (the default), an
nftables table loaded inside that namespace before bwrap starts rejects private and
link-local destinations (LAN, VPN peers, Docker bridges, the host's own addresses),
except the DNS servers in /etc/resolv.conf.

Commands run in one persistent bash. The orchestrator never reads or writes paths
inside agent folders while handling output; output comes back through the pipe.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import re
import secrets
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

SANDBOX_UID = os.getuid()
SANDBOX_GID = os.getgid()

# Top-level directories that are never bound from the host.
ROOT = "/task"
_NOT_BOUND = {"proc", "dev", "run", "tmp", "home", "mnt", "media", ROOT.strip("/"), "lost+found"}

MAX_CAPTURE = 4 * 1024 * 1024
TAIL_KEEP = 1024 * 1024

_PRIVATE_V4 = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/3",
)
_PRIVATE_V6 = ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8")


class SandboxError(RuntimeError):
    pass


@dataclass
class Bind:
    host: Path
    inside: str
    writable: bool = False


@dataclass
class SandboxSpec:
    name: str                       # agent id
    home_inside: str                # cwd and $HOME inside
    binds: list[Bind]
    env: dict[str, str] = field(default_factory=dict)
    network: bool = True
    memory_gb: int = 3
    cpus: int = 2
    tasks: int = 512
    slice: str = "task-swarm.slice"
    block_lan: bool = False         # nftables egress filter for private addresses
    state_dir: Path | None = None   # host-only dir for the nft rules file


@dataclass
class CommandResult:
    exit_code: int | None
    output: str
    total_bytes: int
    dropped_bytes: int
    timed_out: bool = False
    sandbox_died: bool = False


def resolv_nameservers() -> list[str]:
    servers = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                servers.append(parts[1].split("%")[0])
    except OSError:
        pass
    return servers


def egress_rules() -> str:
    """nftables table loaded inside the sandbox's network namespace."""
    allow = []
    for ns in resolv_nameservers():
        ip = ipaddress.ip_address(ns)
        fam = "ip" if ip.version == 4 else "ip6"
        allow.append(f"    {fam} daddr {ns} meta l4proto {{ udp, tcp }} th dport 53 accept")
    v4 = ", ".join(_PRIVATE_V4)
    v6 = ", ".join(_PRIVATE_V6)
    return "\n".join([
        "table inet taskswarm {",
        "  chain out {",
        "    type filter hook output priority 0; policy accept;",
        '    oifname "lo" accept',
        *allow,
        f"    ip daddr {{ {v4} }} counter reject",
        f"    ip6 daddr {{ {v6} }} counter reject",
        "  }",
        "}",
        "",
    ])


def _unit_safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", name)


def _root_binds() -> list[str]:
    args: list[str] = []
    for entry in sorted(os.listdir("/")):
        if entry in _NOT_BOUND:
            continue
        path = Path("/") / entry
        if path.is_symlink():
            args += ["--symlink", os.readlink(path), str(path)]
        elif path.is_dir():
            args += ["--ro-bind", str(path), str(path)]
    return args


def _mountpoint_dirs(binds: list[Bind]) -> list[str]:
    """--dir for every parent directory under ROOT, so mountpoints exist."""
    dirs: set[str] = set()
    for b in binds:
        parent = Path(b.inside).parent
        while str(parent) not in ("/", ""):
            dirs.add(str(parent))
            parent = parent.parent
    args: list[str] = []
    for d in sorted(dirs, key=lambda p: (p.count("/"), p)):
        if d in ("/home", "/mnt", "/tmp", "/run"):
            continue
        args += ["--dir", d]
    return args


def bwrap_args(spec: SandboxSpec) -> list[str]:
    args = [
        "bwrap",
        "--unshare-user", "--uid", str(SANDBOX_UID), "--gid", str(SANDBOX_GID),
        "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try",
        "--hostname", _unit_safe(spec.name),
        "--die-with-parent", "--new-session",
        "--cap-drop", "ALL",
    ]
    if not spec.network:
        args.append("--unshare-net")
    args += _root_binds()
    args += [
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/run",
        "--tmpfs", "/tmp",
        "--dir", "/home",
        "--dir", "/mnt",
        "--dir", "/media",
        "--tmpfs", ROOT,
    ]
    args += _mountpoint_dirs(spec.binds)
    # Read-only binds first, then writable ones (which may be nested inside them).
    for b in sorted(spec.binds, key=lambda b: (b.writable, b.inside.count("/"))):
        if not b.host.exists():
            raise SandboxError(f"bind source missing: {b.host}")
        args += ["--bind" if b.writable else "--ro-bind", str(b.host), b.inside]
    args += ["--remount-ro", ROOT, "--remount-ro", "/"]
    args += ["--clearenv"]
    for k, v in spec.env.items():
        args += ["--setenv", k, v]
    args += ["--chdir", spec.home_inside, "--", "/bin/bash", "--noprofile", "--norc"]
    return args


def full_command(spec: SandboxSpec, unit: str) -> list[str]:
    scope = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--unit={unit}", f"--slice={spec.slice}",
        "-p", f"MemoryMax={spec.memory_gb}G",
        "-p", "MemorySwapMax=0",
        "-p", f"TasksMax={spec.tasks}",
        "-p", f"CPUQuota={spec.cpus * 100}%",
        "--",
    ]
    inner = bwrap_args(spec)
    if not spec.network:
        return scope + inner
    pasta = [
        "pasta", "--quiet", "--foreground", "--config-net", "--no-map-gw",
        "-t", "none", "-u", "none", "-U", "none",
        "-T", str(config.SEARXNG_PORT),
        "--",
    ]
    if spec.block_lan:
        if spec.state_dir is None:
            raise SandboxError("state_dir is required for the egress rules")
        spec.state_dir.mkdir(parents=True, exist_ok=True)
        rules = spec.state_dir / "egress.nft"
        rules.write_text(egress_rules())
        # Inside pasta's namespace we are root: load the egress filter, then drop
        # into bwrap, which takes every capability away.
        pasta += ["/bin/sh", "-c", 'nft -f "$0" && exec "$@"', str(rules)]
    return scope + pasta + inner


class Sandbox:
    """A persistent bash inside the sandbox."""

    def __init__(self, spec: SandboxSpec):
        self.spec = spec
        self.unit = f"taskswarm-{_unit_safe(spec.name)}-{secrets.token_hex(4)}"
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_lines: list[str] = []
        self._buf = bytearray()
        self._stderr_task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, timeout: float = 30) -> None:
        cmd = full_command(self.spec, self.unit)
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        res = await self.run("echo ready", timeout=timeout)
        if res.exit_code != 0 or res.output.strip() != "ready":
            err = "\n".join(self.stderr_lines[-20:])
            await self.close()
            raise SandboxError(f"sandbox failed to start: {res.output!r} {err}")

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self.stderr_lines.append(line.decode(errors="replace").rstrip())
            del self.stderr_lines[:-200]

    async def run(self, command: str, timeout: float) -> CommandResult:
        if not self.alive:
            return CommandResult(None, "", 0, 0, sandbox_died=True)
        assert self.proc and self.proc.stdin and self.proc.stdout
        nonce = secrets.token_hex(8)
        b64 = base64.b64encode(command.encode()).decode()
        # eval keeps cd/variables/functions in the persistent shell; stdin is
        # /dev/null so commands can't eat the protocol stream.
        line = (
            f"{{ eval \"$(printf '%s' '{b64}' | base64 -d)\"; }} </dev/null 2>&1; "
            f"printf '\\n__TASK_END_{nonce} %d\\n' \"$?\"\n"
        )
        marker = re.compile(rb"\n__TASK_END_" + nonce.encode() + rb" (\d+)\n")
        try:
            self.proc.stdin.write(line.encode())
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return CommandResult(None, "", 0, 0, sandbox_died=True)

        head = bytearray()
        tail = bytearray()
        total = 0
        dropped = 0
        pending = self._buf
        self._buf = bytearray()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            m = marker.search(pending)
            if m:
                body = pending[: m.start()]
                self._buf = bytearray(pending[m.end():])
                head, tail, total, dropped = _absorb(head, tail, total, dropped, body)
                out = bytes(head) + bytes(tail)
                return CommandResult(
                    int(m.group(1)), out.decode(errors="replace"), total, dropped
                )
            # Keep enough unconsumed bytes to match a marker split across reads.
            keep = 64
            if len(pending) > keep:
                head, tail, total, dropped = _absorb(head, tail, total, dropped, pending[:-keep])
                pending = pending[-keep:]
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self.kill()
                out = bytes(head) + bytes(tail) + bytes(pending)
                return CommandResult(None, out.decode(errors="replace"), total, dropped, timed_out=True)
            try:
                chunk = await asyncio.wait_for(self.proc.stdout.read(65536), remaining)
            except TimeoutError:
                continue
            if not chunk:
                await self.kill()
                out = bytes(head) + bytes(tail) + bytes(pending)
                return CommandResult(None, out.decode(errors="replace"), total, dropped, sandbox_died=True)
            pending += chunk

    async def kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.proc.wait()
        subprocess.run(
            ["systemctl", "--user", "kill", "--signal=SIGKILL", f"{self.unit}.scope"],
            capture_output=True,
        )
        if self._stderr_task:
            self._stderr_task.cancel()

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None and self.proc.stdin:
            try:
                self.proc.stdin.write(b"exit 0\n")
                await self.proc.stdin.drain()
                self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 5)
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
        await self.kill()


def _absorb(head: bytearray, tail: bytearray, total: int, dropped: int, data: bytes):
    """Keep the first MAX_CAPTURE - TAIL_KEEP bytes and a rolling tail."""
    total += len(data)
    room = (MAX_CAPTURE - TAIL_KEEP) - len(head)
    if room > 0:
        head += data[:room]
        data = data[room:]
    if data:
        tail += data
        if len(tail) > TAIL_KEEP:
            cut = len(tail) - TAIL_KEEP
            dropped += cut
            del tail[:cut]
    return head, tail, total, dropped


# ---------------------------------------------------------------------------
# Specs used by the orchestrator


def agent_env(agent_id: str) -> dict[str, str]:
    return {
        "HOME": f"{ROOT}/work/{agent_id}",
        "USER": agent_id,
        "LOGNAME": agent_id,
        "TASK_AGENT": agent_id,
        "PATH": f"{ROOT}/bin:{ROOT}/tools:/usr/local/bin:/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": "/tmp",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "PYTHONUNBUFFERED": "1",
    }


def agent_spec(cfg: config.RunConfig, agent_id: str) -> SandboxSpec:
    """Everyone's folders, the store, the deliverable and saved outputs read-only; its own folder read-write."""
    run_dir = cfg.run_dir
    binds = [
        Bind(run_dir / "work", f"{ROOT}/work"),
        Bind(run_dir / "store", f"{ROOT}/store"),
        Bind(run_dir / "deliverable", f"{ROOT}/deliverable"),
        Bind(run_dir / "outputs", f"{ROOT}/outputs"),
        Bind(config.AGENT_BIN, f"{ROOT}/bin"),
        Bind(run_dir / "work" / agent_id, f"{ROOT}/work/{agent_id}", writable=True),
    ]
    if config.UV_BINARY.exists():
        binds.append(Bind(config.UV_BINARY, f"{ROOT}/tools/uv"))
    if cfg.data_dir:
        binds.append(Bind(Path(cfg.data_dir), f"{ROOT}/data"))
    return SandboxSpec(
        name=agent_id,
        home_inside=f"{ROOT}/work/{agent_id}",
        binds=binds,
        env=agent_env(agent_id),
        network=True,
        memory_gb=cfg.sandbox_memory_gb,
        cpus=cfg.sandbox_cpus,
        tasks=cfg.sandbox_tasks,
        block_lan=cfg.block_lan,
        state_dir=run_dir / "sys",
    )
