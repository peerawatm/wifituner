#!/usr/bin/env python3
__version__ = "1.1.0"
import argparse
import asyncio
import contextlib
import functools
import json
import os
import platform
import random
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Check platform
OS_NAME = platform.system()
_AIRPORT_LOCK = threading.Lock()


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

_WINDOWS_TCP_DEFAULTS = {
    "autotuninglevel": "normal",
    "rss": "enabled",
    "fastopen": "enabled",
    "ecncapability": "enabled",
}


def _warn(msg: str) -> None:
    """Print a non-fatal warning to stdout."""
    print(f"    [warn] {msg}")


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
        try:
            result = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True,
                text=True,
                check=True,
            )
            pattern = re.compile(
                r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*([a-zA-Z0-9]+)", re.MULTILINE
            )
            match = pattern.search(result.stdout)
            if match:
                return match.group(1)
        except Exception:
            pass  # non-fatal: returns "en0" as default
        return "en0"
    if is_bsd():
        try:
            ifconfig_res = subprocess.run(
                ["ifconfig", "-l"], capture_output=True, text=True
            )
            if ifconfig_res.returncode == 0:
                parts = ifconfig_res.stdout.split()
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
        except Exception:
            pass
        return "wlan0"
    if OS_NAME == "Linux":
        try:
            if os.path.exists("/proc/net/wireless"):
                with open("/proc/net/wireless") as f:
                    lines = f.readlines()
                    for line in lines[2:]:
                        parts = line.split()
                        if parts:
                            return parts[0].strip(":")
            for d in os.listdir("/sys/class/net"):
                if os.path.exists(f"/sys/class/net/{d}/wireless") or os.path.exists(
                    f"/sys/class/net/{d}/phy80211"
                ):
                    return d
        except Exception:
            pass  # non-fatal: returns "wlan0" as default
        return "wlan0"
    if OS_NAME == "Windows":
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                if "Name" in line:
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        return parts[1].strip()
        except Exception:
            pass  # non-fatal: returns "Wi-Fi" as default
        return "Wi-Fi"
    return "wlan0"


@functools.cache
def get_macos_service_name(interface):
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"], capture_output=True, text=True
        )
        lines = result.stdout.splitlines()
        current_service = None
        for line in lines:
            if "Hardware Port:" in line:
                current_service = line.split("Hardware Port:", 1)[1].strip()
            if "Device:" in line:
                dev = line.split("Device:", 1)[1].strip()
                if dev == interface:
                    return current_service
    except Exception:
        pass  # non-fatal: returns "Wi-Fi" as fallback
    return "Wi-Fi"


def get_macos_wifi_details(interface="en0"):
    details = {}
    try:
        res = subprocess.run(
            ["ipconfig", "getsummary", interface],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                line_str = line.strip()
                if line_str.startswith("SSID :"):
                    details["SSID"] = line_str.split(":", 1)[1].strip()
                elif line_str.startswith("Security :"):
                    details["Security"] = line_str.split(":", 1)[1].strip()
                elif line_str.startswith("Router :"):
                    details["Gateway"] = line_str.split(":", 1)[1].strip()
    except Exception:
        pass

    try:
        ip_res = subprocess.run(
            ["ipconfig", "getifaddr", interface],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        if ip_res.returncode == 0 and ip_res.stdout.strip():
            details["IP Address"] = ip_res.stdout.strip()
    except Exception:
        pass

    if not details.get("SSID"):
        try:
            res = subprocess.run(
                ["networksetup", "-getairportnetwork", interface],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            if res.returncode == 0 and "Current Wi-Fi Network:" in res.stdout:
                details["SSID"] = res.stdout.split("Current Wi-Fi Network:", 1)[
                    1
                ].strip()
        except Exception:
            pass

    return details


def _get_macos_wifi_details_slow():
    details: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["system_profiler", "SPAirPortDataType"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return details

        lines = result.stdout.splitlines()
        in_current_net = False
        ssid = None
        indent_level = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if "Current Network Information:" in line:
                in_current_net = True
                indent_level = len(line) - len(line.lstrip())
                continue

            if in_current_net:
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= indent_level and stripped != "":
                    in_current_net = False
                    continue

                if ssid is None and current_indent > indent_level:
                    ssid = stripped.rstrip(":")
                    details["SSID"] = ssid
                    continue

                if ":" in stripped:
                    key, val = stripped.split(":", 1)
                    key = key.strip()
                    val = val.strip()
                    if key == "PHY Mode":
                        details["PHY Mode"] = val
                    elif key == "Channel":
                        details["Channel"] = val
                    elif key == "Security":
                        details["Security"] = val
                    elif key == "Signal / Noise":
                        details["Signal / Noise"] = val
                        match = re.search(r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm", val)
                        if match:
                            details["RSSI"] = f"{match.group(1)} dBm"
                            details["Noise"] = f"{match.group(2)} dBm"
                            details["SNR"] = (
                                f"{int(match.group(1)) - int(match.group(2))} dB"
                            )
                    elif key == "Transmit Rate":
                        details["Transmit Rate"] = f"{val} Mbps"
    except Exception as e:
        _warn(f"system_profiler text parse failed: {e}")
    return details


def get_windows_wifi_details():
    details: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return details

        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, val = stripped.split(":", 1)
            key = key.strip().lower()
            val = val.strip()

            if key == "ssid":
                details["SSID"] = val
            elif "radio" in key or "funktyp" in key:
                details["PHY Mode"] = val
            elif "channel" in key or "kanal" in key or "canal" in key:
                details["Channel"] = val
            elif "auth" in key or "sec" in key:
                details["Security"] = val
            elif "signal" in key or "señal" in key or "segnale" in key:
                details["RSSI"] = val
            elif (
                "transmit" in key
                or "transmission" in key
                or "übertrag" in key
                or "velocidad de trans" in key
                or "velocità di trans" in key
            ):
                details["Transmit Rate"] = (
                    f"{val} Mbps" if "mbps" not in val.lower() else val
                )
    except Exception as e:
        _warn(f"netsh wlan show interfaces failed: {e}")
    return details


def get_linux_wifi_details(interface):
    details = {}
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "link"], capture_output=True, text=True
        )
        if result.returncode == 0 and "Not connected" not in result.stdout:
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("SSID:"):
                    details["SSID"] = stripped.split("SSID:", 1)[1].strip()
                elif stripped.startswith("freq:"):
                    freq_mhz = stripped.split("freq:", 1)[1].strip()
                    try:
                        freq = int(freq_mhz)
                        if freq >= 5925:
                            details["Channel"] = f"{freq} MHz (6GHz)"
                        elif freq >= 5000:
                            details["Channel"] = f"{freq} MHz (5GHz)"
                        elif freq >= 2400:
                            details["Channel"] = f"{freq} MHz (2.4GHz)"
                    except ValueError:
                        details["Channel"] = (
                            freq_mhz  # non-fatal: raw freq string used as-is
                        )
                elif stripped.startswith("signal:"):
                    details["RSSI"] = stripped.split("signal:", 1)[1].strip()
                elif stripped.startswith("tx bitrate:"):
                    details["Transmit Rate"] = stripped.split("tx bitrate:", 1)[
                        1
                    ].strip()
            if "SSID" in details:
                details["PHY Mode"] = "802.11 (Linux iw)"
                details["Security"] = "Enterprise/Personal"
                return details

        result = subprocess.run(["iwconfig", interface], capture_output=True, text=True)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "ESSID:" in line:
                    match = re.search(r'ESSID:"([^"]+)"', line)
                    if match:
                        details["SSID"] = match.group(1)
                if "Frequency:" in line:
                    match = re.search(r"Frequency:([\d\.]+)\s*GHz", line)
                    if match:
                        details["Channel"] = f"{match.group(1)} GHz"
                if "Bit Rate" in line:
                    match = re.search(r"Bit Rate=([\d\.]+)\s*Mb/s", line)
                    if match:
                        details["Transmit Rate"] = f"{match.group(1)} Mbps"
                if "Signal level" in line:
                    match = re.search(r"Signal level=(-?\d+)\s*dBm", line)
                    if match:
                        details["RSSI"] = f"{match.group(1)} dBm"
    except Exception as e:
        _warn(f"iw/iwconfig query failed: {e}")
    return details


def get_bsd_wifi_details(interface: str) -> dict[str, str]:
    details: dict[str, str] = {}
    try:
        result = subprocess.run(["ifconfig", interface], capture_output=True, text=True)
        if result.returncode == 0:
            match_ssid = re.search(r"\bssid\s+([^\s]+)", result.stdout)
            if match_ssid:
                details["SSID"] = match_ssid.group(1)
            match_chan = re.search(r"\bchannel\s+(\d+)", result.stdout)
            if match_chan:
                details["Channel"] = match_chan.group(1)
            match_bssid = re.search(r"\bbssid\s+([0-9a-fA-F:]+)", result.stdout)
            if match_bssid:
                details["BSSID"] = match_bssid.group(1)
    except Exception:
        pass
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
        seen = set()
        deduped = []
        for ip in dns:
            if ip not in seen:
                seen.add(ip)
                deduped.append(ip)
        return deduped
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
    icmp_supported = False
    try:
        if OS_NAME in ("Darwin", "Linux"):
            probe_cmd = (
                ["ping", "-c", "1", "-t", "1", host]
                if OS_NAME == "Darwin"
                else ["ping", "-c", "1", "-W", "1", host]
            )
        elif OS_NAME == "Windows":
            probe_cmd = ["ping", "-n", "1", "-w", "1000", host]
        else:
            probe_cmd = None

        if probe_cmd:
            probe_result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=2.0
            )
            if probe_result.returncode == 0:
                icmp_supported = True
    except Exception:
        pass  # non-fatal: ICMP unavailable; falls to TCP

    if icmp_supported:
        try:
            cmd = []
            if OS_NAME in ("Darwin", "Linux"):
                cmd = ["ping", "-c", str(count), "-i", "0.1", host]
            elif OS_NAME == "Windows":
                cmd = ["ping", "-n", str(count), host]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10.0)
            if result.returncode == 0:
                latencies = []
                for line in result.stdout.splitlines():
                    match = re.search(
                        r"(\b\w+|[^\x00-\x7F]+)[=<]\s*([\d\.]+)\s*(ms)?",
                        line,
                        re.IGNORECASE,
                    )
                    if match:
                        prefix = match.group(1).lower()
                        if prefix != "ttl":
                            latencies.append(float(match.group(2)))
                stats = compute_stats(latencies, "ICMP", total_count=count)
                if stats:
                    return stats, True
        except Exception:
            pass  # non-fatal: ICMP failed mid-run; falls to TCP

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


def benchmark_single_resolver(name, ip, test_domains, timeout=0.8):
    try:
        return asyncio.run(_async_benchmark_resolver(name, ip, test_domains, timeout))
    except Exception:
        return name, None


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
    sizes = [1472, 1464, 1452, 1400, 1300, 1200]
    optimal_payload = None

    for size in sizes:
        try:
            if OS_NAME == "Darwin":
                cmd = ["ping", "-D", "-s", str(size), "-c", "1", "-t", "1", host]
            elif OS_NAME == "Linux":
                cmd = ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "1", host]
            elif OS_NAME == "Windows":
                cmd = ["ping", "-f", "-l", str(size), "-n", "1", "-w", "1000", host]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
            if result.returncode == 0 and "100%" not in result.stdout:
                optimal_payload = size
                break
        except Exception:
            pass  # non-fatal: this payload size failed or fragmented; try next size

    if optimal_payload is not None:
        return optimal_payload + 28
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
        if not is_admin():
            _warn("Elevation required to set MTU on Windows.")
            return False
    else:
        return False

    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        _warn(f"Failed to apply MTU configuration: {e}")
        return False


def flush_dns_cache():
    if OS_NAME == "Darwin":
        try:
            subprocess.run(["sudo", "dscacheutil", "-flushcache"], check=True)
            subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=True)
        except Exception:
            _warn("DNS cache flush failed (dscacheutil/mDNSResponder).")
    elif OS_NAME == "Linux":
        try:
            subprocess.run(["sudo", "resolvectl", "flush-caches"], check=True)
        except Exception:
            try:
                subprocess.run(
                    ["sudo", "systemd-resolve", "--flush-caches"], check=True
                )
            except Exception:
                _warn(
                    "DNS cache flush failed (resolvectl and systemd-resolve both unavailable)."
                )
    elif OS_NAME == "Windows":
        try:
            subprocess.run(["ipconfig", "/flushdns"], check=True)
        except Exception:
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
    try:
        result = subprocess.run(
            ["sudo", "python3", "-c", script], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"    Warning: Could not persist sysctl to {conf_path}")
    except Exception as e:
        print(f"    Warning: Could not persist sysctl: {e}")


def _persist_sysctl(key, val):
    _persist_sysctl_dict({key: val})


def _apply_sysctl_dict(optimizations: dict[str, str]) -> None:
    _persist_sysctl_dict(optimizations)
    if OS_NAME in ("Darwin", "Linux") or is_bsd():
        try:
            cmd = ["sudo", "sysctl", "-w"] + [
                f"{k}={v}" for k, v in optimizations.items()
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        except Exception as e:
            _warn(f"Failed to apply sysctl batch: {e}")


def _revert_sysctl(backup: dict | None, default_sysctl: dict[str, str]) -> None:
    sysctl_values = (
        backup["sysctl"] if (backup and "sysctl" in backup) else default_sysctl
    )
    for key, val in default_sysctl.items():
        backup_val = sysctl_values.get(key)
        target_val = backup_val or val
        try:
            subprocess.run(
                ["sudo", "sysctl", "-w", f"{key}={target_val}"],
                check=True,
                capture_output=True,
            )
            _persist_sysctl(key, target_val)
        except Exception as e:
            _warn(f"Failed to revert sysctl {key}: {e}")


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
            try:
                subprocess.run(
                    ["netsh", "int", "tcp", "set", "global", f"{key}={val}"],
                    check=True,
                    capture_output=True,
                )
            except Exception as e:
                _warn(f"Failed to apply {key}: {e}")


def apply_power_save_optimization(interface: str, gaming: bool = False) -> bool:
    if OS_NAME == "Darwin":
        if not gaming:
            return False
        try:
            subprocess.run(
                ["sudo", "pmset", "-a", "sleep", "0"],
                check=True,
                capture_output=True,
            )
            return True
        except Exception as e:
            _warn(f"Failed to disable system sleep on macOS: {e}")
            return False
    elif OS_NAME == "Linux":
        try:
            subprocess.run(
                ["sudo", "iw", "dev", interface, "set", "power_save", "off"],
                check=True,
                capture_output=True,
            )
            print("    Wi-Fi power saving disabled.")
            return True
        except Exception as e:
            _warn(f"Failed to disable Wi-Fi power saving on Linux: {e}")
    elif OS_NAME == "Windows":
        if not is_admin():
            _warn("Elevation required to disable Wi-Fi power management on Windows.")
            return False
        try:
            ps_iface = interface.replace("'", "''")
            cmd = [
                "powershell",
                "-Command",
                f"Set-NetAdapterPowerManagement -Name '{ps_iface}' -AllowComputerToTurnOffDevice $false",
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            print("    Wi-Fi adapter power management disabled.")
            return True
        except Exception as e:
            _warn(f"Failed to disable Wi-Fi power management on Windows: {e}")
    return False


def get_windows_roaming_aggressiveness(interface: str) -> str | None:
    try:
        ps_iface = interface.replace("'", "''")
        cmd = [
            "powershell",
            "-Command",
            f"(Get-NetAdapterAdvancedProperty -Name '{ps_iface}' -RegistryKeyword 'RoamingSensitivityLevel').RegistryValue",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def set_windows_roaming_aggressiveness(interface: str, value: str) -> bool:
    try:
        ps_iface = interface.replace("'", "''")
        ps_val = value.replace("'", "''")
        cmd = [
            "powershell",
            "-Command",
            f"Set-NetAdapterAdvancedProperty -Name '{ps_iface}' -RegistryKeyword 'RoamingSensitivityLevel' -RegistryValue '{ps_val}'",
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception:
        pass
    return False


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
        try:
            result = subprocess.run(
                ["ifconfig", interface], capture_output=True, text=True
            )
            match = re.search(r"mtu\s+(\d+)", result.stdout)
            if match:
                return int(match.group(1))
        except Exception:
            pass
    elif OS_NAME == "Linux":
        try:
            result = subprocess.run(
                ["ip", "link", "show", interface], capture_output=True, text=True
            )
            match = re.search(r"mtu\s+(\d+)", result.stdout)
            if match:
                return int(match.group(1))
        except Exception:
            pass
    elif OS_NAME == "Windows":
        try:
            result = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "subinterfaces"],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.splitlines():
                if interface in line:
                    parts = line.split()
                    if parts and parts[0].isdigit():
                        return int(parts[0])
        except Exception:
            pass
    return 1500


def get_sysctl_value(key):
    try:
        result = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_windows_tcp_settings():
    settings = {}
    try:
        result = subprocess.run(
            ["netsh", "interface", "tcp", "show", "global"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip().lower()
                val = val.strip().lower()
                if "receive-side scaling" in key or "rss" in key:
                    settings["rss"] = val
                elif "auto-tuning level" in key:
                    settings["autotuninglevel"] = val
                elif "fast open" in key and "fallback" not in key:
                    settings["fastopen"] = val
                elif "ecn capability" in key:
                    settings["ecncapability"] = val
    except Exception:
        pass
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
        help="Disable TCP delayed ACK for lower latency (trades bulk throughput).",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Undo all applied changes and restore system defaults.",
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

    if args.revert:
        ensure_admin()
        revert_optimizations(interface)
        return

    # Start non-privileged background diagnostics BEFORE sudo prompt so they execute
    # in parallel while the user types their password.
    test_domains_list = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not test_domains_list:
        test_domains_list = [
            "google.com",
            "cloudflare.com",
            "github.com",
            "wikipedia.org",
        ]

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
        sorted_dns = sorted(dns_results.items(), key=lambda x: x[1]["avg"])
        fastest_dns = sorted_dns[0]
        if len(sorted_dns) > 1:
            fallback_dns = sorted_dns[1]

    # 3. Path MTU (pre-computed in background)
    pmtu = raw_pmtu if icmp_supported else 1500
    if not pmtu:
        pmtu = 1500

    # 4. Save backup before applying optimizations
    save_backup(interface)

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
            },
        }
        print(json.dumps(out, indent=2))
        return

    print(f"wifituner: Analyzing {interface} ({OS_NAME})")
    if args.gaming:
        print("note: Gaming mode active (disabled TCP delayed ACK and adapter sleep).")

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

    if wifi_details:
        sec = wifi_details.get("Security", "")
        if "Enterprise" in sec or "802.1X" in sec:
            print(
                "\nnote: Associated with Enterprise Wi-Fi network. "
                "Revert using 'python3 tuner.py --revert' if local auth is affected."
            )


if __name__ == "__main__":
    main()
