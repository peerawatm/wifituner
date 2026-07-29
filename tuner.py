#!/usr/bin/env python3
__version__ = "2.4.0"
import argparse
import asyncio
import contextlib
import functools
import json
import os
import plistlib
import platform
import random
import re
import socket
import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

OS_NAME = platform.system()


def is_bsd() -> bool:
    return OS_NAME.endswith("BSD") or "BSD" in OS_NAME


with contextlib.suppress(ImportError):
    import uvloop  # type: ignore[import-not-found]

    uvloop.install()


def _is_linux_or_bsd() -> bool:
    return OS_NAME == "Linux" or is_bsd()


_IP_RE = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:::[0-9a-fA-F]{1,4}|[0-9a-fA-F]{1,4}:[0-9a-fA-F:]+)"
)

_MACOS_SYSCTL_DEFAULTS = {
    "net.inet.tcp.mssdflt": "512",
    "net.inet.tcp.v6mssdflt": "1024",
    "net.inet.tcp.win_scale_factor": "3",
    "net.inet.tcp.delayed_ack": "3",
    "net.inet.tcp.sendspace": "524288",
    "net.inet.tcp.recvspace": "524288",
    "net.inet.tcp.fastopen": "3",
    "net.inet.tcp.always_keepalive": "0",
    "net.inet.tcp.keepidle": "7200000",
    "net.inet.tcp.keepintvl": "75000",
}

_MACOS_SYSCTL_OPTIMIZATIONS = {
    "net.inet.tcp.mssdflt": "1460",
    "net.inet.tcp.v6mssdflt": "1440",
    "net.inet.tcp.win_scale_factor": "8",
    "net.inet.tcp.sendspace": "1048576",
    "net.inet.tcp.recvspace": "1048576",
    "net.inet.tcp.fastopen": "3",
    "net.inet.tcp.always_keepalive": "1",
    "net.inet.tcp.keepidle": "30000",
    "net.inet.tcp.keepintvl": "5000",
}

_LINUX_SYSCTL_DEFAULTS = {
    "net.ipv4.tcp_slow_start_after_idle": "1",
    "net.ipv4.tcp_notsent_lowat": "4294967295",
}

_LINUX_SYSCTL_OPTIMIZATIONS = {
    "net.ipv4.tcp_slow_start_after_idle": "0",
    "net.ipv4.tcp_notsent_lowat": "16384",
}

_BSD_SYSCTL_DEFAULTS = {
    "net.inet.tcp.mssdflt": "512",
}

_BSD_SYSCTL_OPTIMIZATIONS = {
    "net.inet.tcp.mssdflt": "1460",
}

# --- Daemon / service constants ---

_DAEMON_LABEL = "com.wifituner.sysctl"
_LAUNCHD_PLIST_PATH = Path(f"/Library/LaunchDaemons/{_DAEMON_LABEL}.plist")
_SYSTEMD_SERVICE_NAME = "wifituner-sysctl.service"
_SYSTEMD_SERVICE_PATH = Path(f"/etc/systemd/system/{_SYSTEMD_SERVICE_NAME}")


def _build_sysctl_args(kv: dict[str, str]) -> list[str]:
    """Return ['sysctl', '-w', 'k1=v1', 'k2=v2', ...]."""
    return ["sysctl", "-w"] + [f"{k}={v}" for k, v in kv.items()]


def _build_launchd_plist(
    program_arguments: list[str],
    label: str = _DAEMON_LABEL,
    start_interval: int | None = None,
) -> bytes:
    """Return XML plist bytes for a macOS LaunchDaemon."""
    plist_dict: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
    }
    if start_interval is not None:
        plist_dict["StartInterval"] = start_interval
    return plistlib.dumps(plist_dict, fmt=plistlib.FMT_XML)


def _build_systemd_unit(exec_start: str) -> str:
    """Return a systemd oneshot unit file that runs *exec_start* at boot."""
    return textwrap.dedent(f"""\
        [Unit]
        Description=wifituner sysctl optimizations
        After=network-pre.target

        [Service]
        Type=oneshot
        ExecStart={exec_start}
        RemainAfterExit=yes

        [Install]
        WantedBy=multi-user.target
    """)


def install_daemon(gaming: bool = False, disable_awdl: bool = False) -> bool:
    """Install a boot-time daemon/service that re-applies sysctl optimizations.

    macOS: writes a LaunchDaemon plist to /Library/LaunchDaemons/.
    Linux: writes a systemd oneshot service and enables it.
    """
    if OS_NAME == "Darwin":
        optimizations = dict(_MACOS_SYSCTL_OPTIMIZATIONS)
        if gaming:
            optimizations["net.inet.tcp.delayed_ack"] = "0"
        should_disable_awdl = disable_awdl or gaming
        if should_disable_awdl:
            sysctl_cmd = " ".join(f"{k}={v}" for k, v in optimizations.items())
            cmd_str = (
                f"/usr/sbin/sysctl -w {sysctl_cmd}; "
                "/sbin/ifconfig awdl0 down 2>/dev/null; "
                "/sbin/ifconfig llw0 down 2>/dev/null"
            )
            args = ["/bin/sh", "-c", cmd_str]
            plist_bytes = _build_launchd_plist(args, start_interval=60)
        else:
            args = _build_sysctl_args(optimizations)
            args[0] = "/usr/sbin/sysctl"
            plist_bytes = _build_launchd_plist(args)
        plist_path = str(_LAUNCHD_PLIST_PATH)
        if not _sudo_write_file(plist_path, plist_bytes):
            _warn(f"Failed to write plist to {plist_path}.")
            return False
        # Set ownership and permissions
        _run_ok(["sudo", "chown", "root:wheel", plist_path])
        _run_ok(["sudo", "chmod", "644", plist_path])
        # Load the daemon
        # Unload first in case it is already loaded (ignore errors).
        _run_ok(["sudo", "launchctl", "unload", plist_path])
        if not _run_ok(["sudo", "launchctl", "load", plist_path]):
            _warn(f"Failed to load daemon {_DAEMON_LABEL}.")
            return False
        print(f"Installed LaunchDaemon: {plist_path}")
        print(f"Daemon {_DAEMON_LABEL} is loaded and will run at every boot.")
        return True

    if OS_NAME == "Linux":
        sysctl_bin = "/usr/sbin/sysctl" if os.path.exists("/usr/sbin/sysctl") else "/sbin/sysctl"
        optimizations = dict(_LINUX_SYSCTL_OPTIMIZATIONS)
        args_str = " ".join(f"{k}={v}" for k, v in optimizations.items())
        exec_start = f"{sysctl_bin} -w {args_str}"
        unit_content = _build_systemd_unit(exec_start)
        service_path = str(_SYSTEMD_SERVICE_PATH)
        if not _sudo_write_file(service_path, unit_content):
            _warn(f"Failed to write systemd unit to {service_path}.")
            return False
        _run_ok(["sudo", "systemctl", "daemon-reload"])
        if not _run_ok(["sudo", "systemctl", "enable", _SYSTEMD_SERVICE_NAME]):
            _warn(f"Failed to enable {_SYSTEMD_SERVICE_NAME}.")
            return False
        print(f"Installed systemd service: {service_path}")
        print(f"Service {_SYSTEMD_SERVICE_NAME} is enabled and will run at every boot.")
        return True

    _warn(f"Daemon installation is not supported on {OS_NAME}.")
    return False


def uninstall_daemon() -> bool:
    """Remove the boot-time daemon/service installed by install_daemon."""
    if OS_NAME == "Darwin":
        plist_path = str(_LAUNCHD_PLIST_PATH)
        if not _LAUNCHD_PLIST_PATH.exists():
            print(f"No daemon found at {plist_path}. Nothing to uninstall.")
            return True
        _run_ok(["sudo", "launchctl", "unload", plist_path])
        if not _run_ok(["sudo", "rm", "-f", plist_path]):
            _warn(f"Failed to remove {plist_path}.")
            return False
        print(f"Uninstalled LaunchDaemon: {plist_path}")
        return True

    if OS_NAME == "Linux":
        service_path = str(_SYSTEMD_SERVICE_PATH)
        if not _SYSTEMD_SERVICE_PATH.exists():
            print(f"No service found at {service_path}. Nothing to uninstall.")
            return True
        _run_ok(["sudo", "systemctl", "disable", _SYSTEMD_SERVICE_NAME])
        if not _run_ok(["sudo", "rm", "-f", service_path]):
            _warn(f"Failed to remove {service_path}.")
            return False
        _run_ok(["sudo", "systemctl", "daemon-reload"])
        print(f"Uninstalled systemd service: {service_path}")
        return True

    _warn(f"Daemon uninstallation is not supported on {OS_NAME}.")
    return False


_WINDOWS_TCP_DEFAULTS = {
    "autotuninglevel": "normal",
    "rss": "enabled",
    "fastopen": "enabled",
    "ecncapability": "enabled",
}


def _warn(msg: str) -> None:
    """Print a non-fatal warning to stdout."""
    print(f"    [warn] {msg}")


def _cmd(args, timeout: float | None = None, check: bool = False) -> str | None:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True}
    if check:
        kwargs["check"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        res = subprocess.run(args, **kwargs)
        return res.stdout if res.returncode == 0 else None
    except Exception:
        return None


def _run_ok(
    args,
    timeout: float | None = None,
    capture_output: bool = True,
    input: bytes | str | None = None,
) -> bool:
    kwargs: dict[str, Any] = {"check": True}
    if capture_output:
        kwargs["capture_output"] = True
    if timeout is not None:
        kwargs["timeout"] = timeout
    if input is not None:
        kwargs["input"] = input
    try:
        res = subprocess.run(args, **kwargs)
        return res.returncode == 0
    except Exception:
        return False


def _sudo_write_file(path: str, data: str | bytes) -> bool:
    """Write data to path with root privileges via sudo python3."""
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    script = f"import sys; open({path!r}, 'wb').write(sys.stdin.buffer.read())"
    return _run_ok(["sudo", "python3", "-c", script], input=raw)


def is_admin():
    try:
        if OS_NAME == "Windows":
            import ctypes

            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
        return os.geteuid() == 0
    except Exception:
        return False


def ensure_admin() -> None:
    """Prompt for admin credentials at startup so mid-run sudo calls do not block on password."""
    if not is_admin():
        if OS_NAME == "Windows":
            _warn(
                "Elevation required for full network tuning. Run in Administrator terminal."
            )
        else:
            try:
                print("Authenticating administrator privileges...")
                subprocess.run(["sudo", "-v"], check=True)
            except Exception:
                _warn("Failed to obtain root privileges via sudo.")


def get_wifi_interface():
    if OS_NAME == "Darwin":
        res = _cmd(["networksetup", "-listallhardwareports"], check=True)
        if res:
            match = re.search(
                r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*([a-zA-Z0-9]+)",
                res,
                re.MULTILINE,
            )
            if match:
                return match.group(1)
        return "en0"
    if is_bsd():
        res = _cmd(["ifconfig", "-l"])
        if res:
            parts = res.split()
            for part in parts:
                if part.startswith("wlan"):
                    return part
            wifi_prefixes = (
                "iwn",
                "ath",
                "wpi",
                "run",
                "ral",
                "rsu",
                "rtwn",
                "malo",
                "otus",
                "urtwn",
                "pgt",
                "bwn",
            )
            for part in parts:
                if any(part.startswith(p) for p in wifi_prefixes):
                    return part
        return "wlan0"
    if OS_NAME == "Linux":
        try:
            if os.path.exists("/proc/net/wireless"):
                with open("/proc/net/wireless") as f:
                    for line in f.readlines()[2:]:
                        parts = line.split()
                        if parts:
                            return parts[0].strip(":")
            for d in os.listdir("/sys/class/net"):
                if os.path.exists(f"/sys/class/net/{d}/wireless") or os.path.exists(
                    f"/sys/class/net/{d}/phy80211"
                ):
                    return d
            for d in sorted(os.listdir("/sys/class/net")):
                if d != "lo" and not d.startswith(
                    ("docker", "veth", "br-", "virbr", "tun", "tap")
                ):
                    return d
        except Exception:
            pass
        return "wlan0"
    if OS_NAME == "Windows":
        res = _cmd(["netsh", "wlan", "show", "interfaces"], check=True)
        if res:
            for line in res.splitlines():
                if "Name" in line and ":" in line:
                    return line.split(":", 1)[1].strip()
        return "Wi-Fi"
    return "wlan0"


@functools.cache
def get_macos_service_name(interface):
    res = _cmd(["networksetup", "-listallhardwareports"])
    if res:
        current_service = None
        for line in res.splitlines():
            if "Hardware Port:" in line:
                current_service = line.split("Hardware Port:", 1)[1].strip()
            if "Device:" in line and line.split("Device:", 1)[1].strip() == interface:
                return current_service
    return "Wi-Fi"


def get_macos_awdl_status() -> dict[str, bool]:
    """Check whether awdl0 and llw0 interfaces are active/UP on macOS."""
    status = {"awdl0": False, "llw0": False}
    if OS_NAME != "Darwin":
        return status
    for iface in ("awdl0", "llw0"):
        res = _cmd(["ifconfig", iface], timeout=0.5)
        if res:
            lines = res.splitlines()
            first_line = lines[0] if lines else ""
            if "<" in first_line and ">" in first_line:
                flags = first_line.split("<", 1)[1].split(">", 1)[0].split(",")
                if "UP" in flags:
                    status[iface] = True
            elif "status: active" in res:
                status[iface] = True
    return status


def set_macos_awdl(enable: bool) -> bool:
    """Enable or disable awdl0 and llw0 interfaces on macOS."""
    if OS_NAME != "Darwin":
        return False
    state_str = "up" if enable else "down"
    action_str = "Enabling" if enable else "Disabling"
    print(f"[*] {action_str} Apple Wireless Direct Link (AWDL / AirDrop)...")
    success = True
    for iface in ("awdl0", "llw0"):
        res = _cmd(["ifconfig", iface], timeout=0.5)
        if res:  # interface exists
            ok = _run_ok(["sudo", "ifconfig", iface, state_str])
            if not ok:
                _warn(f"Failed to set {iface} to {state_str}.")
                success = False
    return success


def get_macos_wifi_details(interface="en0"):
    details = {}
    res = _cmd(["ipconfig", "getsummary", interface], timeout=1.0)
    if res:
        for line in res.splitlines():
            s = line.strip()
            if s.startswith("SSID :"):
                details["SSID"] = s.split(":", 1)[1].strip()
            elif s.startswith("Security :"):
                details["Security"] = s.split(":", 1)[1].strip()
            elif s.startswith("Router :"):
                details["Gateway"] = s.split(":", 1)[1].strip()

    ip = _cmd(["ipconfig", "getifaddr", interface], timeout=0.5)
    if ip and ip.strip():
        details["IP Address"] = ip.strip()

    if not details.get("SSID"):
        res = _cmd(["networksetup", "-getairportnetwork", interface], timeout=1.0)
        if res and "Current Wi-Fi Network:" in res:
            details["SSID"] = res.split("Current Wi-Fi Network:", 1)[1].strip()

    awdl_st = get_macos_awdl_status()
    active_awdl = [k for k, v in awdl_st.items() if v]
    details["AWDL (AirDrop)"] = (
        f"Active ({', '.join(active_awdl)})" if active_awdl else "Inactive / Disabled"
    )

    return details


def get_windows_wifi_details():
    details: dict[str, str] = {}
    res = _cmd(["netsh", "wlan", "show", "interfaces"])
    if not res:
        return details
    for line in res.splitlines():
        s = line.strip()
        if not s or ":" not in s:
            continue
        key, val = s.split(":", 1)
        k, v = key.strip().lower(), val.strip()
        if k == "ssid":
            details["SSID"] = v
        elif any(x in k for x in ("radio", "funktyp")):
            details["PHY Mode"] = v
        elif any(x in k for x in ("channel", "kanal", "canal")):
            details["Channel"] = v
        elif any(x in k for x in ("auth", "sec")):
            details["Security"] = v
        elif any(x in k for x in ("signal", "señal", "segnale")):
            details["RSSI"] = v
        elif any(
            x in k
            for x in (
                "transmit",
                "transmission",
                "übertrag",
                "velocidad de trans",
                "velocità di trans",
            )
        ):
            details["Transmit Rate"] = f"{v} Mbps" if "mbps" not in v.lower() else v
    return details


def get_linux_wifi_details(interface):
    details = {}
    res = _cmd(["iw", "dev", interface, "link"])
    if res and "Not connected" not in res:
        for line in res.splitlines():
            s = line.strip()
            if s.startswith("SSID:"):
                details["SSID"] = s.split("SSID:", 1)[1].strip()
            elif s.startswith("freq:"):
                freq_mhz = s.split("freq:", 1)[1].strip()
                try:
                    freq = int(freq_mhz)
                    band = (
                        "6GHz"
                        if freq >= 5925
                        else "5GHz"
                        if freq >= 5000
                        else "2.4GHz"
                        if freq >= 2400
                        else None
                    )
                    details["Channel"] = f"{freq} MHz ({band})" if band else freq_mhz
                except ValueError:
                    details["Channel"] = freq_mhz
            elif s.startswith("signal:"):
                details["RSSI"] = s.split("signal:", 1)[1].strip()
            elif s.startswith("tx bitrate:"):
                details["Transmit Rate"] = s.split("tx bitrate:", 1)[1].strip()
        if "SSID" in details:
            details["PHY Mode"] = "802.11 (Linux iw)"
            details["Security"] = "Enterprise/Personal"
            return details

    res2 = _cmd(["iwconfig", interface])
    if res2:
        for line in res2.splitlines():
            if "ESSID:" in line:
                m = re.search(r'ESSID:"([^"]+)"', line)
                if m:
                    details["SSID"] = m.group(1)
            if "Frequency:" in line:
                m = re.search(r"Frequency:([\d\.]+)\s*GHz", line)
                if m:
                    details["Channel"] = f"{m.group(1)} GHz"
            if "Bit Rate" in line:
                m = re.search(r"Bit Rate=([\d\.]+)\s*Mb/s", line)
                if m:
                    details["Transmit Rate"] = f"{m.group(1)} Mbps"
            if "Signal level" in line:
                m = re.search(r"Signal level=(-?\d+)\s*dBm", line)
                if m:
                    details["RSSI"] = f"{m.group(1)} dBm"
    return details


def get_bsd_wifi_details(interface: str) -> dict[str, str]:
    details: dict[str, str] = {}
    res = _cmd(["ifconfig", interface])
    if res:
        for pat, key in [
            (r"\bssid\s+([^\s]+)", "SSID"),
            (r"\bchannel\s+(\d+)", "Channel"),
            (r"\bbssid\s+([0-9a-fA-F:]+)", "BSSID"),
        ]:
            m = re.search(pat, res)
            if m:
                details[key] = m.group(1)
    return details


def get_current_wifi_details(interface):
    if OS_NAME == "Darwin":
        return get_macos_wifi_details(interface)
    if OS_NAME == "Windows":
        return get_windows_wifi_details()
    if OS_NAME == "Linux":
        return get_linux_wifi_details(interface)
    if is_bsd():
        return get_bsd_wifi_details(interface)
    return {}


# [CHANGE 5] Fix: was hardcoded "Wi-Fi" — breaks on non-English locales or
# renamed interfaces. Now resolves the correct service name via the interface.
def get_dns_servers(interface):
    if OS_NAME == "Darwin":
        try:
            service = get_macos_service_name(interface)
            result = subprocess.run(
                ["networksetup", "-getdnsservers", service],
                capture_output=True,
                text=True,
            )
            output = result.stdout.strip()
            if (
                result.returncode == 0
                and output
                and "There aren't any DNS" not in output
            ):
                return [line.strip() for line in output.splitlines() if line.strip()]
        except Exception as e:
            _warn(f"networksetup -getdnsservers failed: {e}")
        return []
    if _is_linux_or_bsd():
        dns = []
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        parts = line.split()
                        if len(parts) > 1:
                            dns.append(parts[1].strip())
        except Exception as e:
            _warn(f"/etc/resolv.conf read failed: {e}")
        return dns
    if OS_NAME == "Windows":
        dns = []
        for family_cmd in ("ipv4", "ipv6"):
            try:
                result = subprocess.run(
                    [
                        "netsh",
                        "interface",
                        family_cmd,
                        "show",
                        "dns",
                        f"name={interface}",
                    ],
                    capture_output=True,
                    text=True,
                )
                for line in result.stdout.splitlines():
                    line_lower = line.lower()
                    if (
                        "dns" in line_lower
                        or "configured" in line_lower
                        or "statically" in line_lower
                        or _IP_RE.search(line)
                    ):
                        ips = _IP_RE.findall(line)
                        dns.extend(ips)
            except Exception as e:
                _warn(f"netsh interface {family_cmd} show dns failed: {e}")
        return list(dict.fromkeys(dns))
    return []


def test_tcp_latency(host="1.1.1.1", port=443, count=5):
    latencies = []
    for _ in range(count):
        t0 = time.perf_counter()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.8)
            s.connect((host, port))
            s.close()
            t1 = time.perf_counter()
            latencies.append((t1 - t0) * 1000.0)
        except Exception:
            pass  # non-fatal: single attempt of {count}; None filtered by compute_stats
        time.sleep(0.02)
    return compute_stats(latencies, "TCP (Port 443)")


def test_latency(host="1.1.1.1", count=2):
    probe_cmd = (
        ["ping", "-c", "1", "-t", "1", host]
        if OS_NAME == "Darwin"
        else ["ping", "-c", "1", "-W", "1", host]
        if OS_NAME == "Linux"
        else ["ping", "-n", "1", "-w", "1000", host]
        if OS_NAME == "Windows"
        else None
    )
    if probe_cmd and _cmd(probe_cmd, timeout=2.0) is not None:
        cmd = (
            ["ping", "-c", str(count), "-i", "0.1", host]
            if OS_NAME in ("Darwin", "Linux")
            else ["ping", "-n", str(count), host]
            if OS_NAME == "Windows"
            else []
        )
        res = _cmd(cmd, timeout=10.0)
        if res:
            latencies = []
            for line in res.splitlines():
                match = re.search(
                    r"(\b\w+|[^\x00-\x7F]+)[=<]\s*([\d\.]+)\s*(ms)?",
                    line,
                    re.IGNORECASE,
                )
                if match and match.group(1).lower() != "ttl":
                    latencies.append(float(match.group(2)))
            stats = compute_stats(latencies, "ICMP", total_count=count)
            if stats:
                return stats, True

    return test_tcp_latency(host, port=443, count=count), False


def compute_stats(latencies, type_str, total_count=None):
    if not latencies:
        return None
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    variance = sum((x - avg_lat) ** 2 for x in latencies) / len(latencies)
    stddev = variance**0.5
    count = (
        total_count if total_count and total_count >= len(latencies) else len(latencies)
    )
    loss = round(((count - len(latencies)) / count) * 100, 1)
    return {
        "min": round(min_lat, 3),
        "avg": round(avg_lat, 3),
        "max": round(max_lat, 3),
        "stddev": round(stddev, 3),
        "loss": loss,
        "type": type_str,
    }


def build_dns_query(domain):
    tx_id = random.randint(0, 65535)
    header = (
        tx_id.to_bytes(2, byteorder="big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    )
    question = b""
    for part in domain.split("."):
        question += len(part).to_bytes(1, byteorder="big") + part.encode("utf-8")
    question += b"\x00\x00\x01\x00\x01"
    return header + question


# [CHANGE 4] Parallelize domain queries within each resolver. Previously they
# were sequential: one 0.8s timeout would block the next domain query.
async def _async_query_dns(ip: str, domain: str, timeout: float = 0.8) -> float | None:
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    eff_timeout = (
        min(timeout, 0.15) if family == socket.AF_INET6 else min(timeout, 0.25)
    )

    class DNSDatagramProtocol(asyncio.DatagramProtocol):
        def __init__(self):
            self.future = loop.create_future()

        def datagram_received(self, data, addr):
            if not self.future.done():
                self.future.set_result(data)

        def error_received(self, exc):
            if not self.future.done():
                self.future.set_exception(exc)

    try:
        transport, protocol = await loop.create_datagram_endpoint(
            DNSDatagramProtocol,
            remote_addr=(ip, 53),
            family=family,
        )
        query = build_dns_query(domain)
        t_start = time.perf_counter()
        transport.sendto(query)
        try:
            data = await asyncio.wait_for(protocol.future, timeout=eff_timeout)
            t_end = time.perf_counter()
            if len(data) >= 12 and (data[3] & 0x0F) == 0:
                return (t_end - t_start) * 1000.0
            return None
        finally:
            transport.close()
    except Exception:
        return None


async def _async_benchmark_resolver(
    name: str, ip: str, test_domains: list[str], timeout: float = 0.8
):
    tasks = [_async_query_dns(ip, domain, timeout) for domain in test_domains]
    results = await asyncio.gather(*tasks)
    latencies = [r for r in results if r is not None]
    if latencies:
        return name, {"ip": ip, "avg": round(sum(latencies) / len(latencies), 2)}
    return name, None


async def _async_benchmark_dns(
    resolvers: dict[str, str], test_domains: list[str], timeout: float = 0.8
):
    tasks = [
        _async_benchmark_resolver(name, ip, test_domains, timeout)
        for name, ip in resolvers.items()
    ]
    res_list = await asyncio.gather(*tasks)
    return {name: data for name, data in res_list if data is not None}


def benchmark_dns(
    interface,
    test_domains=None,
    timeout=0.8,
):
    if test_domains is None:
        test_domains = ["google.com", "cloudflare.com", "github.com", "wikipedia.org"]
    resolvers = {
        "Cloudflare Primary": "1.1.1.1",
        "Cloudflare Secondary": "1.0.0.1",
        "Google Primary": "8.8.8.8",
        "Google Secondary": "8.8.4.4",
        "Quad9 Secure": "9.9.9.9",
        "OpenDNS Primary": "208.67.222.222",
        "OpenDNS Secondary": "208.67.220.220",
        "AdGuard DNS": "94.140.14.14",
        "Control D": "76.76.2.0",
        "Cloudflare Primary IPv6": "2606:4700:4700::1111",
        "Cloudflare Secondary IPv6": "2606:4700:4700::1001",
        "Google Primary IPv6": "2001:4860:4860::8888",
        "Google Secondary IPv6": "2001:4860:4860::8844",
        "Quad9 Secure IPv6": "2620:fe::fe",
        "OpenDNS Primary IPv6": "2620:119:35::35",
        "AdGuard DNS IPv6": "2a10:50c0::ad1:ff",
    }

    system_dns = get_dns_servers(interface)
    for idx, ip in enumerate(system_dns):
        if ip not in resolvers.values():
            resolvers[f"Current System DNS {idx + 1}"] = ip

    try:
        return asyncio.run(_async_benchmark_dns(resolvers, test_domains, timeout))
    except Exception:
        return {}


def discover_pmtu(icmp_supported, host="1.1.1.1"):
    if not icmp_supported:
        return None
    for size in (1472, 1464, 1452, 1400, 1300, 1200):
        cmd = (
            ["ping", "-D", "-s", str(size), "-c", "1", "-t", "1", host]
            if OS_NAME == "Darwin"
            else ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "1", host]
            if OS_NAME == "Linux"
            else ["ping", "-f", "-l", str(size), "-n", "1", "-w", "1000", host]
            if OS_NAME == "Windows"
            else None
        )
        if not cmd:
            return None
        res = _cmd(cmd, timeout=2.0)
        if res and "100%" not in res:
            return size + 28

    return None


def print_latency_results(latency):
    if latency:
        print("Latency:")
        loss_str = f", loss {latency['loss']}%" if "loss" in latency else ""
        print(
            f"  {latency['type']} avg {latency['avg']} ms "
            f"(min {latency['min']} ms, max {latency['max']} ms, jitter {latency['stddev']} ms{loss_str})"
        )
        if latency.get("loss", 0.0) > 0.0:
            print(
                f"  warning: {latency['loss']}% packet loss detected — check Wi-Fi distance or radio interference."
            )
    else:
        print("Latency: check failed.")


def apply_dns(interface, dns_ip, fallback_dns=None):
    dns_servers = [dns_ip]
    if fallback_dns and fallback_dns != dns_ip:
        dns_servers.append(fallback_dns)

    if verify_dns(interface, dns_ip):
        print("              DNS configuration already active.")
        return True

    is_v6 = ":" in dns_ip
    family_cmd = "ipv6" if is_v6 else "ipv4"
    if OS_NAME == "Darwin":
        service = get_macos_service_name(interface)
        cmd = ["sudo", "networksetup", "-setdnsservers", service] + dns_servers
    elif _is_linux_or_bsd():
        try:
            if OS_NAME == "Linux":
                subprocess.run(
                    ["resolvectl", "--version"], capture_output=True, check=True
                )
                cmd = ["sudo", "resolvectl", "dns", interface] + dns_servers
            else:
                raise Exception("Not Linux")
        except Exception:
            resolv_content = "".join(f"nameserver {ip}\n" for ip in dns_servers)
            cmd = ["sudo", "sh", "-c", f"printf '{resolv_content}' > /etc/resolv.conf"]
    elif OS_NAME == "Windows":
        cmd = [
            "netsh",
            "interface",
            family_cmd,
            "set",
            "dns",
            f"name={interface}",
            "static",
            dns_ip,
        ]
        if not is_admin():
            _warn("Elevation required to set DNS on Windows.")
            return False
    else:
        return False

    try:
        subprocess.run(cmd, check=True)
        if OS_NAME == "Windows" and len(dns_servers) > 1:
            with contextlib.suppress(Exception):
                subprocess.run(
                    [
                        "netsh",
                        "interface",
                        "ipv6" if ":" in dns_servers[1] else "ipv4",
                        "add",
                        "dns",
                        f"name={interface}",
                        dns_servers[1],
                        "index=2",
                    ],
                    check=True,
                    capture_output=True,
                )
        return True
    except subprocess.CalledProcessError as e:
        _warn(f"Failed to apply DNS configuration: {e}")
        return False


# Verify the OS resolver is configured to use dns_ip, not just that dns_ip is
# reachable via UDP/53. The previous UDP probe gave false confidence on
# WPA2-Enterprise networks where DHCP DNS overrides our networksetup change.
def verify_dns(interface, dns_ip):
    is_v6 = ":" in dns_ip
    family_cmd = "ipv6" if is_v6 else "ipv4"
    try:
        if OS_NAME == "Darwin":
            result = subprocess.run(
                ["scutil", "--dns"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return dns_ip in result.stdout
            service = get_macos_service_name(interface)
            result = subprocess.run(
                ["networksetup", "-getdnsservers", service],
                capture_output=True,
                text=True,
            )
            return dns_ip in [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
        if _is_linux_or_bsd():
            # resolvectl-managed systems do not write to /etc/resolv.conf;
            # check resolvectl status first, fall back for non-systemd systems.
            try:
                if OS_NAME == "Linux":
                    result = subprocess.run(
                        ["resolvectl", "status", interface],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        ips = _IP_RE.findall(result.stdout)
                        return dns_ip in ips
                else:
                    raise Exception("Not Linux")
            except Exception:
                pass  # non-fatal: resolvectl absent; falls to /etc/resolv.conf
            with open("/etc/resolv.conf") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        parts = line.split()
                        if len(parts) > 1 and parts[1].strip() == dns_ip:
                            return True
                return False
        elif OS_NAME == "Windows":
            result = subprocess.run(
                ["netsh", "interface", family_cmd, "show", "dns", f"name={interface}"],
                capture_output=True,
                text=True,
            )
            ips = _IP_RE.findall(result.stdout)
            return dns_ip in ips
    except Exception:
        pass  # non-fatal: verify_dns returns False; caller prints warning + revert hint
    return False


def apply_mtu(interface, mtu_size):
    if OS_NAME == "Darwin" or is_bsd():
        cmd = ["sudo", "ifconfig", interface, "mtu", str(mtu_size)]
    elif OS_NAME == "Linux":
        cmd = ["sudo", "ip", "link", "set", "dev", interface, "mtu", str(mtu_size)]
    elif OS_NAME == "Windows":
        if not is_admin():
            _warn("Elevation required to set MTU on Windows.")
            return False
        cmd = [
            "netsh",
            "interface",
            "ipv4",
            "set",
            "subinterface",
            interface,
            f"mtu={mtu_size}",
            "store=persistent",
        ]
    else:
        return False

    if _run_ok(cmd, capture_output=False):
        return True
    _warn("Failed to apply MTU configuration.")
    return False


def flush_dns_cache():
    if OS_NAME == "Darwin":
        if not (
            _run_ok(["sudo", "dscacheutil", "-flushcache"], capture_output=False)
            and _run_ok(
                ["sudo", "killall", "-HUP", "mDNSResponder"], capture_output=False
            )
        ):
            _warn("DNS cache flush failed (dscacheutil/mDNSResponder).")
    elif OS_NAME == "Linux":
        if not (
            _run_ok(["sudo", "resolvectl", "flush-caches"], capture_output=False)
            or _run_ok(
                ["sudo", "systemd-resolve", "--flush-caches"], capture_output=False
            )
        ):
            _warn(
                "DNS cache flush failed (resolvectl and systemd-resolve both unavailable)."
            )
    elif OS_NAME == "Windows" and not _run_ok(
        ["ipconfig", "/flushdns"], capture_output=False
    ):
        _warn("DNS cache flush failed (ipconfig /flushdns).")


# Persist sysctl settings to config file so they survive reboots.
# macOS: /etc/sysctl.conf  Linux: /etc/sysctl.d/99-wifituner.conf
#
# Read and write are done inside a single privileged python3 invocation to
# close the TOCTOU window that existed when the file was read as the current
# user and then written via a separate sudo tee call.
def _persist_sysctl_dict(kv_dict: dict[str, str]) -> None:
    if OS_NAME == "Darwin" or is_bsd():
        conf_path = "/etc/sysctl.conf"
    elif OS_NAME == "Linux":
        conf_path = "/etc/sysctl.d/99-wifituner.conf"
    else:
        return

    script = (
        "import os\n"
        f"path = {conf_path!r}\n"
        f"kv = {kv_dict!r}\n"
        "lines = open(path).readlines() if os.path.exists(path) else []\n"
        "kv_rem = dict(kv)\n"
        "for i, line in enumerate(lines):\n"
        "    s = line.strip()\n"
        "    for k, v in list(kv_rem.items()):\n"
        "        if s.startswith(f'{k}=') or s.startswith(f'{k} ='):\n"
        "            lines[i] = f'{k}={v}\\n'\n"
        "            del kv_rem[k]\n"
        "            break\n"
        "for k, v in kv_rem.items():\n"
        "    lines.append(f'{k}={v}\\n')\n"
        "open(path, 'w').writelines(lines)\n"
    )
    if not _run_ok(["sudo", "python3", "-c", script]):
        print(f"    Warning: Could not persist sysctl to {conf_path}")


def _apply_sysctl_dict(optimizations: dict[str, str]) -> None:
    _persist_sysctl_dict(optimizations)
    if OS_NAME in ("Darwin", "Linux") or is_bsd():
        cmd = ["sudo", "sysctl", "-w"] + [f"{k}={v}" for k, v in optimizations.items()]
        if not _run_ok(cmd):
            _warn("Failed to apply sysctl batch.")


def _revert_sysctl(backup: dict | None, default_sysctl: dict[str, str]) -> None:
    sysctl_values = (
        backup["sysctl"] if (backup and "sysctl" in backup) else default_sysctl
    )
    for key, val in default_sysctl.items():
        backup_val = sysctl_values.get(key)
        target_val = backup_val or val
        if _run_ok(["sudo", "sysctl", "-w", f"{key}={target_val}"]):
            _persist_sysctl_dict({key: target_val})
        else:
            _warn(f"Failed to revert sysctl {key}.")


def apply_sysctl_optimizations(gaming=False):
    if OS_NAME == "Darwin":
        optimizations = dict(_MACOS_SYSCTL_OPTIMIZATIONS)
        if gaming:
            optimizations["net.inet.tcp.delayed_ack"] = "0"
        _apply_sysctl_dict(optimizations)
    elif OS_NAME == "Linux":
        _apply_sysctl_dict(_LINUX_SYSCTL_OPTIMIZATIONS)
    elif is_bsd():
        _apply_sysctl_dict(_BSD_SYSCTL_OPTIMIZATIONS)
    elif OS_NAME == "Windows":
        if not is_admin():
            _warn("Elevation required to tune TCP/IP parameters on Windows.")
            return
        for key, val in _WINDOWS_TCP_DEFAULTS.items():
            if not _run_ok(["netsh", "int", "tcp", "set", "global", f"{key}={val}"]):
                _warn(f"Failed to apply {key}.")


def apply_power_save_optimization(interface: str, gaming: bool = False) -> bool:
    if OS_NAME == "Darwin":
        return bool(gaming and _run_ok(["sudo", "pmset", "-a", "sleep", "0"]))
    if OS_NAME == "Linux":
        if _run_ok(["sudo", "iw", "dev", interface, "set", "power_save", "off"]):
            print("    Wi-Fi power saving disabled.")
            return True
        _warn("Failed to disable Wi-Fi power saving on Linux.")
    elif OS_NAME == "Windows":
        if not is_admin():
            _warn("Elevation required to disable Wi-Fi power management on Windows.")
            return False
        ps_iface = interface.replace("'", "''")
        cmd = [
            "powershell",
            "-Command",
            f"Set-NetAdapterPowerManagement -Name '{ps_iface}' -AllowComputerToTurnOffDevice $false",
        ]
        if _run_ok(cmd):
            print("    Wi-Fi adapter power management disabled.")
            return True
        _warn("Failed to disable Wi-Fi power management on Windows.")
    return False


def get_windows_roaming_aggressiveness(interface: str) -> str | None:
    ps_iface = interface.replace("'", "''")
    cmd = [
        "powershell",
        "-Command",
        f"(Get-NetAdapterAdvancedProperty -Name '{ps_iface}' -RegistryKeyword 'RoamingSensitivityLevel').RegistryValue",
    ]
    res = _cmd(cmd)
    return res.strip() if res and res.strip() else None


def set_windows_roaming_aggressiveness(interface: str, value: str) -> bool:
    ps_iface = interface.replace("'", "''")
    ps_val = value.replace("'", "''")
    cmd = [
        "powershell",
        "-Command",
        f"Set-NetAdapterAdvancedProperty -Name '{ps_iface}' -RegistryKeyword 'RoamingSensitivityLevel' -RegistryValue '{ps_val}'",
    ]
    return _run_ok(cmd)


def _get_backup_path() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        repo_root = result.stdout.strip()
        return os.path.join(repo_root, ".wifituner_backup.json")
    except Exception:
        # Fallback to the directory containing tuner.py
        script_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(script_dir, ".wifituner_backup.json")


BACKUP_PATH = _get_backup_path()


def get_current_mtu(interface):
    if OS_NAME == "Darwin" or is_bsd():
        out = _cmd(["ifconfig", interface])
    elif OS_NAME == "Linux":
        out = _cmd(["ip", "link", "show", interface])
    elif OS_NAME == "Windows":
        out = _cmd(["netsh", "interface", "ipv4", "show", "subinterfaces"])
        if out:
            for line in out.splitlines():
                if interface in line:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        return int(parts[0])
        return 1500
    else:
        return 1500

    if out:
        match = re.search(r"mtu\s+(\d+)", out)
        if match:
            return int(match.group(1))
    return 1500


def get_sysctl_value(key):
    res = _cmd(["sysctl", "-n", key])
    return res.strip() if res else None


def get_windows_tcp_settings():
    settings = {}
    res = _cmd(["netsh", "interface", "tcp", "show", "global"])
    if res:
        for line in res.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                k = key.strip().lower()
                v = val.strip().lower()
                if "receive-side scaling" in k or "rss" in k:
                    settings["rss"] = v
                elif "auto-tuning level" in k:
                    settings["autotuninglevel"] = v
                elif "fast open" in k and "fallback" not in k:
                    settings["fastopen"] = v
                elif "ecn capability" in k:
                    settings["ecncapability"] = v
    return settings


def save_backup(interface: str) -> None:
    if os.path.exists(BACKUP_PATH):
        return
    backup = {
        "dns": get_dns_servers(interface),
        "mtu": get_current_mtu(interface),
        "sysctl": {},
        "power_save": True,
    }
    if OS_NAME == "Darwin":
        backup["awdl_status"] = get_macos_awdl_status()
        for key in _MACOS_SYSCTL_DEFAULTS:
            backup["sysctl"][key] = get_sysctl_value(key)
    elif is_bsd():
        for key in _BSD_SYSCTL_DEFAULTS:
            backup["sysctl"][key] = get_sysctl_value(key)
    elif OS_NAME == "Linux":
        for key in _LINUX_SYSCTL_DEFAULTS:
            backup["sysctl"][key] = get_sysctl_value(key)
    elif OS_NAME == "Windows":
        backup["sysctl"] = get_windows_tcp_settings()
        backup["roaming_aggressiveness"] = get_windows_roaming_aggressiveness(interface)
    if OS_NAME == "Darwin":
        try:
            result = subprocess.run(["pmset", "-g"], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith("sleep"):
                    parts = stripped.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        backup["pmset_sleep"] = parts[1]
                        break
        except Exception:
            pass

    try:
        with open(BACKUP_PATH, "w") as f:
            json.dump(backup, f)
    except Exception as e:
        _warn(f"Failed to save backup configurations: {e}")


def _revert_dns_to_dhcp(interface: str) -> None:
    print("[*] Reverting DNS to DHCP...")
    if OS_NAME == "Darwin":
        service = get_macos_service_name(interface)
        subprocess.run(
            ["sudo", "networksetup", "-setdnsservers", service, "empty"], check=True
        )
    elif _is_linux_or_bsd():
        try:
            if OS_NAME == "Linux":
                subprocess.run(["resolvectl", "revert", interface], check=True)
            else:
                raise Exception("Not Linux")
        except Exception:
            pass
    elif OS_NAME == "Windows":
        if is_admin():
            for family in ["ipv4", "ipv6"]:
                subprocess.run(
                    [
                        "netsh",
                        "interface",
                        family,
                        "set",
                        "dns",
                        f"name={interface}",
                        "dhcp",
                    ],
                    check=True,
                )
        else:
            _warn("Elevation required to revert DNS on Windows.")


def revert_optimizations(interface: str) -> None:
    print("=== Reverting wifituner Optimizations to System Defaults ===")
    backup = None
    if os.path.exists(BACKUP_PATH):
        try:
            with open(BACKUP_PATH) as f:
                backup = json.load(f)
            print(f"Loaded backup configurations from {BACKUP_PATH}")
        except Exception as e:
            _warn(
                f"Failed to read backup file: {e}. Falling back to default system heuristics."
            )

    if backup and backup.get("dns"):
        dns_servers = backup["dns"]
        print(f"[*] Restoring backup DNS servers: {dns_servers}")
        if OS_NAME == "Darwin":
            service = get_macos_service_name(interface)
            cmd = ["sudo", "networksetup", "-setdnsservers", service] + dns_servers
            subprocess.run(cmd, check=True)
        elif _is_linux_or_bsd():
            try:
                if OS_NAME == "Linux":
                    subprocess.run(
                        ["resolvectl", "--version"], capture_output=True, check=True
                    )
                    cmd = ["sudo", "resolvectl", "dns", interface] + dns_servers
                    subprocess.run(cmd, check=True)
                else:
                    raise Exception("Not Linux")
            except Exception:
                lines = [f"nameserver {ip}\n" for ip in dns_servers]
                script = f"open('/etc/resolv.conf', 'w').writelines({lines!r})"
                subprocess.run(["sudo", "python3", "-c", script], check=True)
        elif OS_NAME == "Windows":
            if is_admin():
                first_dns = dns_servers[0]
                is_v6 = ":" in first_dns
                family_cmd = "ipv6" if is_v6 else "ipv4"
                subprocess.run(
                    [
                        "netsh",
                        "interface",
                        family_cmd,
                        "set",
                        "dns",
                        f"name={interface}",
                        "static",
                        first_dns,
                    ],
                    check=True,
                )
                for idx, dns_ip in enumerate(dns_servers[1:], start=2):
                    is_ip_v6 = ":" in dns_ip
                    fam_cmd = "ipv6" if is_ip_v6 else "ipv4"
                    subprocess.run(
                        [
                            "netsh",
                            "interface",
                            fam_cmd,
                            "add",
                            "dns",
                            f"name={interface}",
                            dns_ip,
                            f"index={idx}",
                        ],
                        check=True,
                    )
            else:
                _warn("Elevation required to revert DNS on Windows.")
    else:
        _revert_dns_to_dhcp(interface)

    mtu = backup["mtu"] if (backup and "mtu" in backup) else 1500
    print(f"[*] Reverting MTU to {mtu}...")
    apply_mtu(interface, mtu)

    flush_dns_cache()

    print("[*] Reverting Wi-Fi adapter power management...")
    if OS_NAME == "Darwin":
        sleep_val = backup.get("pmset_sleep", "1") if backup else "1"
        try:
            subprocess.run(
                ["sudo", "pmset", "-a", "sleep", sleep_val],
                check=True,
                capture_output=True,
            )
            print(f"    System sleep restored to {sleep_val}.")
        except Exception as e:
            _warn(f"Failed to restore system sleep on macOS: {e}")
        if backup and "awdl_status" in backup:
            awdl_status = backup.get("awdl_status", {})
            if any(awdl_status.values()):
                print("[*] Restoring AWDL (AirDrop/Sidecar) interfaces...")
                for iface, was_up in awdl_status.items():
                    if was_up:
                        _run_ok(["sudo", "ifconfig", iface, "up"])
    elif _is_linux_or_bsd():
        try:
            if OS_NAME == "Linux":
                subprocess.run(
                    ["sudo", "iw", "dev", interface, "set", "power_save", "on"],
                    check=True,
                    capture_output=True,
                )
                print("    Wi-Fi power saving re-enabled.")
        except Exception as e:
            _warn(f"Failed to re-enable Wi-Fi power saving: {e}")
    elif OS_NAME == "Windows":
        if is_admin():
            try:
                cmd = [
                    "powershell",
                    "-Command",
                    f"Set-NetAdapterPowerManagement -Name '{interface}' -AllowComputerToTurnOffDevice $true",
                ]
                subprocess.run(cmd, check=True, capture_output=True)
                print("    Wi-Fi adapter power management re-enabled.")
            except Exception as e:
                _warn(f"Failed to re-enable Wi-Fi power management on Windows: {e}")
        else:
            _warn("Elevation required to re-enable Wi-Fi power management on Windows.")

    if OS_NAME == "Windows":
        if is_admin():
            roam_val = backup.get("roaming_aggressiveness") if backup else None
            if roam_val:
                print(f"[*] Restoring backup roaming aggressiveness: {roam_val}")
                set_windows_roaming_aggressiveness(interface, roam_val)
            else:
                print("[*] Reverting roaming aggressiveness to default (Medium)...")
                set_windows_roaming_aggressiveness(interface, "3")
        else:
            _warn("Elevation required to revert roaming aggressiveness on Windows.")

    print("[*] Reverting TCP/IP kernel stack optimizations...")
    if OS_NAME == "Darwin":
        _revert_sysctl(backup, _MACOS_SYSCTL_DEFAULTS)
    elif is_bsd():
        _revert_sysctl(backup, _BSD_SYSCTL_DEFAULTS)
    elif OS_NAME == "Linux":
        _revert_sysctl(backup, _LINUX_SYSCTL_DEFAULTS)
        with contextlib.suppress(Exception):
            subprocess.run(
                ["sudo", "rm", "-f", "/etc/sysctl.d/99-wifituner.conf"],
                check=True,
                capture_output=True,
            )
    elif OS_NAME == "Windows":
        if is_admin():
            tcp_values = (
                backup["sysctl"]
                if (backup and "sysctl" in backup)
                else _WINDOWS_TCP_DEFAULTS
            )
            for key, val in _WINDOWS_TCP_DEFAULTS.items():
                backup_val = tcp_values.get(key)
                target_val = backup_val or val
                try:
                    subprocess.run(
                        ["netsh", "int", "tcp", "set", "global", f"{key}={target_val}"],
                        check=True,
                        capture_output=True,
                    )
                except Exception as e:
                    _warn(f"Failed to revert TCP global {key}: {e}")
        else:
            _warn("Elevation required to revert TCP optimizations on Windows.")

    if os.path.exists(BACKUP_PATH):
        try:
            os.remove(BACKUP_PATH)
            print(f"Removed backup file {BACKUP_PATH}")
        except Exception as e:
            _warn(f"Failed to delete backup file: {e}")

    print("=== Reversion Complete ===")


def main():
    parser = argparse.ArgumentParser(description="Wi-Fi analyzer and optimizer.")
    parser.add_argument(
        "--gaming",
        action="store_true",
        help="Disable TCP delayed ACK and AWDL (macOS) for lower latency.",
    )
    parser.add_argument(
        "--disable-awdl",
        action="store_true",
        help="Disable Apple Wireless Direct Link (awdl0/llw0) on macOS to prevent Wi-Fi latency spikes.",
    )
    parser.add_argument(
        "--enable-awdl",
        action="store_true",
        help="Re-enable Apple Wireless Direct Link (awdl0/llw0) on macOS.",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Undo all applied changes and restore system defaults.",
    )
    parser.add_argument(
        "--install-daemon",
        action="store_true",
        help="Install a LaunchDaemon (macOS) or systemd service (Linux) to persist sysctl optimizations across reboots.",
    )
    parser.add_argument(
        "--uninstall-daemon",
        action="store_true",
        help="Remove the boot-time daemon/service created by --install-daemon.",
    )
    parser.add_argument(
        "--domains",
        type=str,
        default="google.com,cloudflare.com,github.com,wikipedia.org",
        help="Domains for DNS benchmarking (comma-separated).",
    )
    parser.add_argument(
        "--ping-host",
        type=str,
        default="1.1.1.1",
        help="Host for latency and MTU tests.",
    )
    parser.add_argument(
        "--dns-timeout",
        type=float,
        default=0.8,
        help="DNS query timeout in seconds.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON metrics.",
    )
    args = parser.parse_args()

    interface = get_wifi_interface()

    if args.enable_awdl:
        ensure_admin()
        ok = set_macos_awdl(True)
        sys.exit(0 if ok else 1)

    if args.revert:
        ensure_admin()
        revert_optimizations(interface)
        return

    if args.install_daemon:
        ensure_admin()
        ok = install_daemon(gaming=args.gaming, disable_awdl=args.disable_awdl)
        sys.exit(0 if ok else 1)

    if args.uninstall_daemon:
        ensure_admin()
        ok = uninstall_daemon()
        sys.exit(0 if ok else 1)

    # Start non-privileged background diagnostics BEFORE sudo prompt so they execute
    # in parallel while the user types their password.
    test_domains_list = [
        d.strip() for d in args.domains.split(",") if d.strip()
    ] or None

    diag_executor = ThreadPoolExecutor(max_workers=4)
    wifi_future = diag_executor.submit(get_current_wifi_details, interface)
    lat_future = diag_executor.submit(test_latency, args.ping_host)
    dns_future = diag_executor.submit(
        benchmark_dns, interface, test_domains_list, args.dns_timeout
    )
    pmtu_future = diag_executor.submit(discover_pmtu, True, args.ping_host)

    ensure_admin()

    # 1. Retrieve Current Network Details
    wifi_details = wifi_future.result()

    # 2. Collect diagnostic results (already completing/completed in background)
    latency, icmp_supported = lat_future.result()
    dns_results = dns_future.result()
    raw_pmtu = pmtu_future.result()
    diag_executor.shutdown(wait=False)

    fastest_dns = None
    fallback_dns = None
    if dns_results:
        valid_dns = [
            item
            for item in dns_results.items()
            if not (
                item[1]["ip"].startswith("127.") or item[1]["ip"] in ("::1", "0.0.0.0")
            )
        ]
        sorted_dns = sorted(valid_dns, key=lambda x: x[1]["avg"])
        if sorted_dns:
            fastest_dns = sorted_dns[0]
            if len(sorted_dns) > 1:
                fallback_dns = sorted_dns[1]

    # 3. Path MTU (pre-computed in background)
    pmtu = raw_pmtu if icmp_supported else 1500
    if not pmtu:
        pmtu = 1500

    # 4. Save backup before applying optimizations
    save_backup(interface)

    should_disable_awdl = OS_NAME == "Darwin" and (args.disable_awdl or args.gaming)

    if args.json:
        dns_ips = [fastest_dns[1]["ip"]] if fastest_dns else []
        if fallback_dns:
            dns_ips.append(fallback_dns[1]["ip"])
        if dns_ips:
            apply_dns(interface, dns_ips[0], dns_ips[1] if len(dns_ips) > 1 else None)
        apply_mtu(interface, pmtu)
        flush_dns_cache()
        apply_sysctl_optimizations(gaming=args.gaming)
        apply_power_save_optimization(interface, gaming=args.gaming)
        if should_disable_awdl:
            set_macos_awdl(False)
        if OS_NAME == "Windows":
            set_windows_roaming_aggressiveness(interface, "2")
        out = {
            "platform": OS_NAME,
            "interface": interface,
            "wifi_details": wifi_details,
            "latency": latency,
            "dns_resolvers": dns_results,
            "recommended_dns": dns_ips,
            "recommended_mtu": pmtu,
            "optimizations_applied": {
                "dns": dns_ips,
                "mtu": pmtu,
                "dns_cache_flushed": True,
                "sysctl_tuned": True,
                "power_save_configured": True,
                "awdl_disabled": should_disable_awdl,
            },
        }
        print(json.dumps(out, indent=2))
        return

    print(f"wifituner: Analyzing {interface} ({OS_NAME})")
    if args.gaming:
        print("note: Gaming mode active (disabled TCP delayed ACK, adapter sleep, and AWDL).")

    if wifi_details:
        print("\nActive Wi-Fi connection:")
        for k, v in wifi_details.items():
            print(f"  {k:<15}: {v}")
    else:
        print("\nwarning: Could not fetch active Wi-Fi link parameters.")

    print()
    print_latency_results(latency)

    if dns_results:
        print("\nDNS resolvers:")
        for name, data in sorted_dns:
            print(f"  {data['ip']:<15} {name:<25} {data['avg']} ms")

    # 5. Output Recommendations & Apply Optimizations
    print("\nApplying optimizations:")

    # 5a. Apply DNS
    if fastest_dns:
        primary_ip = fastest_dns[1]["ip"]
        fallback_ip = fallback_dns[1]["ip"] if fallback_dns else None
        desc = f"  DNS         Set to {primary_ip} ({fastest_dns[0]}, {fastest_dns[1]['avg']} ms)"
        if fallback_dns and fallback_ip:
            desc += f" + {fallback_ip} ({fallback_dns[0]})"
        print(desc)
        if apply_dns(interface, primary_ip, fallback_ip):
            if verify_dns(interface, primary_ip):
                print("              DNS verified active.")
            else:
                print("              warning: DNS verification failed.")

    # 5b. Apply MTU
    print(f"  MTU         Set to {pmtu} bytes")
    apply_mtu(interface, pmtu)

    # 5c. Flush cache
    flush_dns_cache()
    print("  Cache       Flushed system DNS cache")

    # 5d. Apply kernel TCP stack tweaks
    apply_sysctl_optimizations(gaming=args.gaming)
    print("  Kernel      Tuned TCP/IP stack parameters")

    # 5e. Apply Wi-Fi power-saving and roaming aggressiveness optimizations
    apply_power_save_optimization(interface, gaming=args.gaming)
    print("  Power       Configured Wi-Fi power management")
    if OS_NAME == "Windows":
        set_windows_roaming_aggressiveness(interface, "2")
        print("  Roaming     Set roaming aggressiveness to Medium-Low")

    # 5f. Apply AWDL optimization on macOS
    if should_disable_awdl:
        set_macos_awdl(False)
        print("  AWDL        Disabled awdl0/llw0 interfaces (prevents ping spikes)")

    if wifi_details:
        sec = wifi_details.get("Security", "")
        if "Enterprise" in sec or "802.1X" in sec:
            print(
                "\nnote: Associated with Enterprise Wi-Fi network. "
                "Revert using 'python3 tuner.py --revert' if local auth is affected."
            )


if __name__ == "__main__":
    main()
