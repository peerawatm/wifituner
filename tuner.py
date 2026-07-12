#!/usr/bin/env python3
__version__ = "1.1.0"
import os
import argparse
import functools
import json
import platform
import subprocess
import time
import re
import socket
import random
import threading
from concurrent.futures import ThreadPoolExecutor

# Check platform
OS_NAME = platform.system()


def is_bsd():
    return OS_NAME.endswith("BSD") or "BSD" in OS_NAME


# Band-keyed neighbor channel counts. Populated by bg_scan_channels() at runtime.
neighbor_channels: dict[str, dict[str, int]] = {"2GHz": {}, "5GHz": {}, "6GHz": {}}


def _warn(msg):
    """Print a non-fatal warning to stdout."""
    print(f"    [warn] {msg}")


def is_admin():
    try:
        if OS_NAME == "Windows":
            import ctypes

            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # type: ignore[attr-defined]
        else:
            return os.geteuid() == 0
    except Exception:
        return False


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
    elif is_bsd():
        try:
            result = subprocess.run(
                ["sysctl", "-n", "net.wlan.devices"], capture_output=True, text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                ifconfig_res = subprocess.run(
                    ["ifconfig", "-l"], capture_output=True, text=True
                )
                if ifconfig_res.returncode == 0:
                    for part in ifconfig_res.stdout.split():
                        if part.startswith("wlan"):
                            return part
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
    elif OS_NAME == "Linux":
        try:
            if os.path.exists("/proc/net/wireless"):
                with open("/proc/net/wireless", "r") as f:
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
    elif OS_NAME == "Windows":
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


@functools.lru_cache(maxsize=None)
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


@functools.lru_cache(maxsize=None)
def _get_airport_json():
    """Invoke system_profiler SPAirPortDataType -json once per process; cache result."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPAirPortDataType", "-json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass  # non-fatal: returns {} → triggers slow-path fallback in caller
    return {}


# Use system_profiler SPAirPortDataType -json (macOS 12+, structured output).
# airport -I was removed in macOS 15 (Sequoia); this is the stable replacement.
def get_macos_wifi_details():
    details = {}
    try:
        data = _get_airport_json()
        if data:
            interfaces = data.get("SPAirPortDataType", [{}])[0].get(
                "spairport_airport_interfaces", []
            )
            for iface in interfaces:
                current = iface.get("spairport_current_network_information", {})
                if not current:
                    continue
                if "_name" in current:
                    details["SSID"] = current["_name"]
                if "spairport_network_channel" in current:
                    details["Channel"] = current["spairport_network_channel"]
                if "spairport_network_phymode" in current:
                    details["PHY Mode"] = current["spairport_network_phymode"]
                if "spairport_signal_noise" in current:
                    details["Signal / Noise"] = current["spairport_signal_noise"]
                    match = re.search(
                        r"(-?\d+)\s*dBm\s*/\s*(-?\d+)\s*dBm",
                        str(current["spairport_signal_noise"]),
                    )
                    if match:
                        details["RSSI"] = f"{match.group(1)} dBm"
                        details["Noise"] = f"{match.group(2)} dBm"
                        details["SNR"] = (
                            f"{int(match.group(1)) - int(match.group(2))} dB"
                        )
                if "spairport_security_mode" in current:
                    details["Security"] = current["spairport_security_mode"]
                if "spairport_network_rate" in current:
                    details["Transmit Rate"] = (
                        f"{current['spairport_network_rate']} Mbps"
                    )
                if details:
                    break
    except Exception:
        pass  # non-fatal: slow path called in return statement below
    return details if details else _get_macos_wifi_details_slow()


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
        return get_macos_wifi_details()
    elif OS_NAME == "Windows":
        return get_windows_wifi_details()
    elif OS_NAME == "Linux":
        return get_linux_wifi_details(interface)
    elif is_bsd():
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
    elif OS_NAME == "Linux" or is_bsd():
        dns = []
        try:
            with open("/etc/resolv.conf", "r") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        parts = line.split()
                        if len(parts) > 1:
                            dns.append(parts[1].strip())
        except Exception as e:
            _warn(f"/etc/resolv.conf read failed: {e}")
        return dns
    elif OS_NAME == "Windows":
        dns = []
        for family_cmd in ["ipv4", "ipv6"]:
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
                ip_pattern = re.compile(
                    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:::[0-9a-fA-F]{1,4}|[0-9a-fA-F]{1,4}:[0-9a-fA-F:]+)"
                )
                for line in result.stdout.splitlines():
                    line_lower = line.lower()
                    if (
                        "dns" in line_lower
                        or "configured" in line_lower
                        or "statically" in line_lower
                        or ip_pattern.search(line)
                    ):
                        ips = ip_pattern.findall(line)
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


def test_latency(host="1.1.1.1", count=5):
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
                cmd = ["ping", "-c", str(count), host]
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
                stats = compute_stats(latencies, "ICMP")
                if stats:
                    return stats, True
        except Exception:
            pass  # non-fatal: ICMP failed mid-run; falls to TCP

    return test_tcp_latency(host, port=443, count=count), False


def compute_stats(latencies, type_str):
    if not latencies:
        return None
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    variance = sum((x - avg_lat) ** 2 for x in latencies) / len(latencies)
    stddev = variance**0.5
    return {
        "min": round(min_lat, 3),
        "avg": round(avg_lat, 3),
        "max": round(max_lat, 3),
        "stddev": round(stddev, 3),
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
# Worst case per-resolver latency: was 4 * 0.8s = 3.2s, now max(0.8s).
def benchmark_single_resolver(name, ip, test_domains, timeout=0.8):
    def query_one(domain):
        try:
            query = build_dns_query(domain)
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            t_start = time.perf_counter()
            sock.sendto(query, (ip, 53))
            data, _ = sock.recvfrom(512)
            t_end = time.perf_counter()
            sock.close()
            if len(data) >= 12 and (data[3] & 0x0F) == 0:
                return (t_end - t_start) * 1000.0
            return None
        except Exception:
            return None  # non-fatal: one of len(test_domains) parallel queries; None filtered

    with ThreadPoolExecutor(max_workers=len(test_domains)) as ex:
        results = list(ex.map(query_one, test_domains))
    latencies = [r for r in results if r is not None]
    if latencies:
        return name, {"ip": ip, "avg": round(sum(latencies) / len(latencies), 2)}
    return name, None


def benchmark_dns(
    interface,
    test_domains=["google.com", "cloudflare.com", "github.com", "wikipedia.org"],
    timeout=0.8,
):
    print("Benchmarking DNS resolver latencies (concurrent queries)...")
    resolvers = {
        "Cloudflare Primary": "1.1.1.1",
        "Cloudflare Secondary": "1.0.0.1",
        "Google Primary": "8.8.8.8",
        "Google Secondary": "8.8.4.4",
        "Quad9 Secure": "9.9.9.9",
        "Cloudflare Primary IPv6": "2606:4700:4700::1111",
        "Cloudflare Secondary IPv6": "2606:4700:4700::1001",
        "Google Primary IPv6": "2001:4860:4860::8888",
        "Google Secondary IPv6": "2001:4860:4860::8844",
        "Quad9 Secure IPv6": "2620:fe::fe",
    }

    system_dns = get_dns_servers(interface)
    for idx, ip in enumerate(system_dns):
        if ip not in resolvers.values():
            resolvers[f"Current System DNS {idx + 1}"] = ip

    results = {}

    with ThreadPoolExecutor(max_workers=len(resolvers)) as executor:
        futures = [
            executor.submit(benchmark_single_resolver, name, ip, test_domains, timeout)
            for name, ip in resolvers.items()
        ]
        for future in futures:
            name, res = future.result()
            if res:
                results[name] = res

    return results


def discover_pmtu(icmp_supported, host="1.1.1.1"):
    if not icmp_supported:
        return None
    print("Analyzing Path MTU (PMTUD)...")
    sizes = [1472, 1464, 1452, 1400, 1300, 1200]
    optimal_payload = None

    for size in sizes:
        try:
            if OS_NAME == "Darwin":
                cmd = ["ping", "-D", "-s", str(size), "-c", "2", "-t", "1", host]
            elif OS_NAME == "Linux":
                cmd = ["ping", "-M", "do", "-s", str(size), "-c", "2", "-W", "1", host]
            elif OS_NAME == "Windows":
                cmd = ["ping", "-f", "-l", str(size), "-n", "2", "-w", "1000", host]
            else:
                continue

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
            if result.returncode == 0 and "100%" not in result.stdout:
                optimal_payload = size
                break
        except Exception:
            pass  # non-fatal: this payload size failed or fragmented; try next size

    if optimal_payload is not None:
        return optimal_payload + 28
    return None


def get_occupied_channels(ch_num, band, width):
    try:
        ch = int(ch_num)
    except (ValueError, TypeError):
        return []

    if band == "2GHz":
        return [str(x) for x in range(max(1, ch - 4), min(15, ch + 5))]

    if width == 40:
        pairs = [
            {36, 40},
            {44, 48},
            {52, 56},
            {60, 64},
            {100, 104},
            {108, 112},
            {116, 120},
            {124, 128},
            {132, 136},
            {140, 144},
            {149, 153},
            {157, 161},
        ]
        for pair in pairs:
            if ch in pair:
                return [str(x) for x in pair]
        return (
            [str(ch), str(ch + 4)]
            if ch % 8 == 4 or ch % 8 == 5
            else [str(ch), str(ch - 4)]
        )

    elif width == 80:
        groups = [
            {36, 40, 44, 48},
            {52, 56, 60, 64},
            {100, 104, 108, 112},
            {116, 120, 124, 128},
            {132, 136, 140, 144},
            {149, 153, 157, 161},
        ]
        for group in groups:
            if ch in group:
                return [str(x) for x in group]
        if band == "6GHz":
            base = 1
            while base <= 233:
                group = {base, base + 4, base + 8, base + 12}
                if ch in group:
                    return [str(x) for x in group]
                base += 16
        return [str(x) for x in range(ch - 6, ch + 7) if x > 0 and (x - ch) % 4 == 0]

    elif width == 160:
        groups = [
            {36, 40, 44, 48, 52, 56, 60, 64},
            {100, 104, 108, 112, 116, 120, 124, 128},
        ]
        for group in groups:
            if ch in group:
                return [str(x) for x in group]
        if band == "6GHz":
            base = 1
            while base <= 233:
                group = {base + 4 * i for i in range(8)}
                if ch in group:
                    return [str(x) for x in group]
                base += 32
        return [str(x) for x in range(ch - 14, ch + 15) if x > 0 and (x - ch) % 4 == 0]

    return [str(ch)]


def _parse_channel_band(ch_str):
    """
    Parse a channel string of the form produced by system_profiler JSON:
      '64 (5GHz, 80MHz)' -> ('64', '5GHz', 80)
      '6 (2GHz, 20MHz)'  -> ('6',  '2GHz', 20)
      '37 (6GHz, 80MHz)' -> ('37', '6GHz', 80)
      '64'               -> ('64', None, 20)
    Returns (None, None, 20) when the string cannot be parsed.
    """
    ch_str = str(ch_str).strip()
    m = re.match(r"(\d+)\s*(?:\((\d+GHz)(?:,\s*(\d+)MHz)?\))?", ch_str)
    if m and m.group(1):
        width = int(m.group(3)) if m.group(3) else 20
        return m.group(1), m.group(2), width
    return None, None, 20


def _classify_band(ch_num_str, band_str):
    """
    Return '2GHz' | '5GHz' | '6GHz' | None.
    Uses explicit band_str when available; falls back to channel-number heuristics.

    Heuristic: channels 1-14 → 2 GHz (ambiguous with 6 GHz, but 2.4 GHz is more
    common for low-numbered channels). Channels ≥ 182 → 6 GHz (exclusive range).
    All others → 5 GHz.
    """
    if band_str in ("2GHz", "5GHz", "6GHz"):
        return band_str
    try:
        n = int(ch_num_str)
        if n <= 14:
            return "2GHz"
        if n >= 182:
            return "6GHz"
        return "5GHz"
    except (ValueError, TypeError):
        return None


# macOS: use system_profiler SPAirPortDataType -json for neighbor channel scan.
# airport -s removed in macOS 15; JSON output avoids space-delimited SSID
# column-index parsing bugs and the deprecated binary.
def scan_neighbor_channels():
    """
    Returns {"2GHz": {ch: count}, "5GHz": {ch: count}, "6GHz": {ch: count}}.

    macOS JSON path (primary): parses spairport_network_channel strings such as
    '6 (2GHz, 20MHz)' via _parse_channel_band — band is explicit, no heuristic needed.
    macOS text path (fallback): used when JSON returns no neighbor data.
    Linux: nmcli -f CHAN,FREQ; frequency used for band classification.
    Windows: netsh wlan; channel-number heuristic for band.
    """
    channels: dict[str, dict[str, int]] = {"2GHz": {}, "5GHz": {}, "6GHz": {}}
    if is_bsd():
        return channels

    def record_neighbor(ch_num, band, width):
        key = _classify_band(ch_num, band)
        if not key:
            return
        occupied = get_occupied_channels(ch_num, key, width)
        for c in occupied:
            channels[key][c] = channels[key].get(c, 0) + 1

    try:
        if OS_NAME == "Darwin":
            data = _get_airport_json()
            if data:
                try:
                    interfaces = data.get("SPAirPortDataType", [{}])[0].get(
                        "spairport_airport_interfaces", []
                    )
                    for iface in interfaces:
                        other_nets = iface.get(
                            "spairport_airport_other_local_wireless_networks", []
                        )
                        for net in other_nets:
                            ch_str = net.get("spairport_network_channel", "")
                            ch_num, band, width = _parse_channel_band(ch_str)
                            if ch_num is None:
                                continue
                            record_neighbor(ch_num, band, width)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass  # non-fatal: falls to text parser fallback below

            if not any(channels.values()):
                # Fallback: system_profiler text output (no -json flag)
                result = subprocess.run(
                    ["system_profiler", "SPAirPortDataType"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    # e.g. "Channel: 64 (5GHz, 80MHz)" or "Channel: 6"
                    matches = re.findall(
                        r"Channel:\s*(\d+)\s*(?:\((\d+GHz)(?:,\s*(\d+)MHz)?\))?",
                        result.stdout,
                    )
                    for ch_num, band, width_str in matches:
                        width = int(width_str) if width_str else 20
                        record_neighbor(ch_num, band, width)

        elif OS_NAME == "Windows":
            result = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line_lower = line.lower()
                    if (
                        "channel" in line_lower
                        or "kanal" in line_lower
                        or "canal" in line_lower
                    ):
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            ch = parts[1].strip()
                            if ch.isdigit():
                                record_neighbor(ch, None, 20)

        elif OS_NAME == "Linux":
            result = subprocess.run(
                ["nmcli", "-f", "CHAN,FREQ", "dev", "wifi", "list"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    ch = parts[0].strip()
                    if not ch.isdigit():
                        continue
                    try:
                        # nmcli may use locale decimal separator and GHz or MHz unit.
                        # Examples: "2,437 GHz", "5.180 GHz", "2412 MHz", "5180"
                        freq_str = parts[1].replace(",", ".")
                        freq_val = float(freq_str)
                        unit = parts[2].upper() if len(parts) > 2 else ""
                        # Treat as GHz if unit says so, or if value is < 100 (bare GHz float)
                        if unit == "GHZ" or freq_val < 100:
                            freq_mhz = freq_val * 1000.0
                        else:
                            freq_mhz = freq_val  # already MHz
                        if freq_mhz >= 5925:
                            key = "6GHz"
                        elif freq_mhz >= 5000:
                            key = "5GHz"
                        else:
                            key = "2GHz"
                    except (ValueError, IndexError):
                        key = _classify_band(ch, None)
                    record_neighbor(ch, key, 20)

    except Exception as e:
        _warn(f"neighbor channel scan failed: {e}")
    return channels


def bg_scan_channels():
    global neighbor_channels
    neighbor_channels = scan_neighbor_channels()


def print_latency_results(latency):
    if latency:
        print(f"  Type      : {latency['type']}")
        print(f"  Avg RTT   : {latency['avg']} ms")
        print(f"  Min RTT   : {latency['min']} ms")
        print(f"  Max RTT   : {latency['max']} ms")
        print(f"  Jitter    : {latency['stddev']} ms")
    else:
        print("  Latency check failed.")


def get_channel_recommendation(ch_by_band):
    """
    Returns (rec_24, rec_5, rec_6).
    rec_6 is None when no 6 GHz neighbors are visible (ch_by_band['6GHz'] is empty).

    ch_by_band must be {"2GHz": {ch: count}, "5GHz": {ch: count}, "6GHz": {ch: count}}.

    c_6 uses Wi-Fi 6E Preferred Scanning Channels (PSC): every 4th channel starting at 5,
    spaced 80 MHz apart across UNII-5 through UNII-8 (5.925–7.125 GHz).
    """
    c_24 = ["1", "6", "11"]
    c_5 = ["36", "40", "44", "48", "149", "153", "157", "161"]
    c_6 = [
        "5",
        "21",
        "37",
        "53",
        "69",
        "85",
        "101",
        "117",
        "133",
        "149",
        "165",
        "181",
        "197",
        "213",
        "229",
    ]

    ch_24 = ch_by_band.get("2GHz", {})
    ch_5 = ch_by_band.get("5GHz", {})
    ch_6 = ch_by_band.get("6GHz", {})

    congestion_24 = {ch: ch_24.get(ch, 0) for ch in c_24}
    congestion_5 = {ch: ch_5.get(ch, 0) for ch in c_5}

    rec_24 = min(congestion_24, key=congestion_24.__getitem__)
    rec_5 = min(congestion_5, key=congestion_5.__getitem__)

    if ch_6:
        congestion_6 = {ch: ch_6.get(ch, 0) for ch in c_6}
        rec_6 = min(congestion_6, key=congestion_6.__getitem__)
    else:
        rec_6 = None

    return rec_24, rec_5, rec_6


def apply_dns(interface, dns_ip):
    print(f"[*] Applying DNS: Configuration targeting {dns_ip}...")
    is_v6 = ":" in dns_ip
    family_cmd = "ipv6" if is_v6 else "ipv4"
    if OS_NAME == "Darwin":
        service = get_macos_service_name(interface)
        cmd = ["sudo", "networksetup", "-setdnsservers", service, dns_ip]
    elif OS_NAME == "Linux" or is_bsd():
        try:
            if OS_NAME == "Linux":
                subprocess.run(
                    ["resolvectl", "--version"], capture_output=True, check=True
                )
                cmd = ["sudo", "resolvectl", "dns", interface, dns_ip]
            else:
                raise Exception("Not Linux")
        except Exception:
            cmd = ["sudo", "sh", "-c", f"echo 'nameserver {dns_ip}' > /etc/resolv.conf"]
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
            print(
                "    Elevation required. Please run this script inside an Administrator terminal."
            )
            return False
    else:
        return False

    try:
        subprocess.run(cmd, check=True)
        print("    DNS configuration applied successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Failed to apply DNS configuration: {e}")
        return False


# Verify the OS resolver is configured to use dns_ip, not just that dns_ip is
# reachable via UDP/53. The previous UDP probe gave false confidence on
# WPA2-Enterprise networks where DHCP DNS overrides our networksetup change.
def verify_dns(interface, dns_ip):
    is_v6 = ":" in dns_ip
    family_cmd = "ipv6" if is_v6 else "ipv4"
    try:
        if OS_NAME == "Darwin":
            service = get_macos_service_name(interface)
            result = subprocess.run(
                ["networksetup", "-getdnsservers", service],
                capture_output=True,
                text=True,
            )
            return dns_ip in [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
        elif OS_NAME == "Linux" or is_bsd():
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
                        ip_pattern = re.compile(
                            r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:::[0-9a-fA-F]{1,4}|[0-9a-fA-F]{1,4}:[0-9a-fA-F:]+)"
                        )
                        ips = ip_pattern.findall(result.stdout)
                        return dns_ip in ips
                else:
                    raise Exception("Not Linux")
            except Exception:
                pass  # non-fatal: resolvectl absent; falls to /etc/resolv.conf
            with open("/etc/resolv.conf", "r") as f:
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
            ip_pattern = re.compile(
                r"\b(?:\d{1,3}\.){3}\d{1,3}\b|(?:::[0-9a-fA-F]{1,4}|[0-9a-fA-F]{1,4}:[0-9a-fA-F:]+)"
            )
            ips = ip_pattern.findall(result.stdout)
            return dns_ip in ips
    except Exception:
        pass  # non-fatal: verify_dns returns False; caller prints warning + revert hint
    return False


def apply_mtu(interface, mtu_size):
    print(f"[*] Applying MTU: Configuration targeting {mtu_size} bytes...")
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
            print(
                "    Elevation required. Please run this script inside an Administrator terminal."
            )
            return False
    else:
        return False

    try:
        subprocess.run(cmd, check=True)
        print("    MTU configuration applied successfully.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"    Failed to apply MTU configuration: {e}")
        return False


def flush_dns_cache():
    print("[*] Flushing system DNS cache...")
    if OS_NAME == "Darwin":
        try:
            subprocess.run(["sudo", "dscacheutil", "-flushcache"], check=True)
            subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=True)
            print("    DNS cache flushed.")
        except Exception:
            _warn("DNS cache flush failed (dscacheutil/mDNSResponder).")
    elif OS_NAME == "Linux":
        try:
            subprocess.run(["sudo", "resolvectl", "flush-caches"], check=True)
            print("    DNS cache flushed.")
        except Exception:
            try:
                subprocess.run(
                    ["sudo", "systemd-resolve", "--flush-caches"], check=True
                )
                print("    DNS cache flushed.")
            except Exception:
                _warn(
                    "DNS cache flush failed (resolvectl and systemd-resolve both unavailable)."
                )
    elif OS_NAME == "Windows":
        try:
            subprocess.run(["ipconfig", "/flushdns"], check=True)
            print("    DNS cache flushed.")
        except Exception:
            _warn("DNS cache flush failed (ipconfig /flushdns).")


# Persist sysctl settings to config file so they survive reboots.
# macOS: /etc/sysctl.conf  Linux: /etc/sysctl.d/99-wifituner.conf
#
# Read and write are done inside a single privileged python3 invocation to
# close the TOCTOU window that existed when the file was read as the current
# user and then written via a separate sudo tee call.
def _persist_sysctl(key, val):
    if OS_NAME == "Darwin" or is_bsd():
        conf_path = "/etc/sysctl.conf"
    elif OS_NAME == "Linux":
        conf_path = "/etc/sysctl.d/99-wifituner.conf"
    else:
        return

    # Build a self-contained script; key/val are repr'd so no shell quoting is
    # needed and no shell-metacharacter injection is possible.
    script = (
        "import os\n"
        f"path = {conf_path!r}\n"
        f"key = {key!r}\n"
        f"val = {val!r}\n"
        "setting = f'{key}={val}\\n'\n"
        "lines = open(path).readlines() if os.path.exists(path) else []\n"
        "updated = False\n"
        "for i, line in enumerate(lines):\n"
        "    s = line.strip()\n"
        "    if s.startswith(f'{key}=') or s.startswith(f'{key} ='):\n"
        "        lines[i] = setting; updated = True; break\n"
        "if not updated: lines.append(setting)\n"
        "open(path, 'w').writelines(lines)\n"
    )
    try:
        result = subprocess.run(
            ["sudo", "python3", "-c", script], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"    Warning: Could not persist {key} to {conf_path}")
    except Exception as e:
        print(f"    Warning: Could not persist {key}: {e}")


# [CHANGE 7] delayed_ack=0 moved to --gaming flag: disabling it hurts bulk
# throughput (large file transfers, streaming) while only helping interactive
# traffic (gaming, VoIP, WebRTC). Default run is now bulk-throughput safe.
# [CHANGE 9] Added missing is_admin() guard for Windows before netsh calls.
def apply_sysctl_optimizations(gaming=False):
    print(
        "[*] Tuning TCP/IP kernel stack parameters for lower RTT and zero fragmentation..."
    )
    if OS_NAME == "Darwin":
        optimizations = {
            "net.inet.tcp.mssdflt": "1460",  # Optimal segment size (avoids fragmentation overhead)
            "net.inet.tcp.win_scale_factor": "8",  # Expands receive window size limit
        }
        if gaming:
            optimizations["net.inet.tcp.delayed_ack"] = (
                "0"  # Immediate ACKs (resolves gaming/WebRTC jitter)
            )
        for key, val in optimizations.items():
            try:
                print(f"    Setting {key} = {val}...")
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, val)
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")
    elif OS_NAME == "Linux":
        optimizations = {
            "net.ipv4.tcp_slow_start_after_idle": "0",
            "net.ipv4.tcp_notsent_lowat": "16384",
        }
        for key, val in optimizations.items():
            try:
                print(f"    Setting {key} = {val}...")
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, val)
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")
    elif is_bsd():
        optimizations = {
            "net.inet.tcp.mssdflt": "1460",
        }
        for key, val in optimizations.items():
            try:
                print(f"    Setting {key} = {val}...")
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, val)
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")
    elif OS_NAME == "Windows":
        if not is_admin():
            print(
                "    Elevation required to tune TCP/IP parameters. Run as Administrator."
            )
            return
        optimizations = {
            "autotuninglevel": "normal",
            "rss": "enabled",
            "fastopen": "enabled",
            "ecncapability": "enabled",
        }
        for key, val in optimizations.items():
            try:
                print(f"    Setting TCP global {key} = {val}...")
                subprocess.run(
                    ["netsh", "int", "tcp", "set", "global", f"{key}={val}"],
                    check=True,
                    capture_output=True,
                )
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")


def apply_power_save_optimization(interface: str) -> bool:
    print("[*] Tuning Wi-Fi adapter power management...")
    if OS_NAME == "Linux":
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
            cmd = [
                "powershell",
                "-Command",
                f"Set-NetAdapterPowerManagement -Name '{interface}' -AllowComputerToTurnOffDevice $false",
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            print("    Wi-Fi adapter power management disabled.")
            return True
        except Exception as e:
            _warn(f"Failed to disable Wi-Fi power management on Windows: {e}")
    return False


def get_windows_roaming_aggressiveness(interface: str) -> str | None:
    try:
        cmd = [
            "powershell",
            "-Command",
            f"(Get-NetAdapterAdvancedProperty -Name '{interface}' -RegistryKeyword 'RoamingSensitivityLevel').RegistryValue",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def set_windows_roaming_aggressiveness(interface: str, value: str) -> bool:
    try:
        cmd = [
            "powershell",
            "-Command",
            f"Set-NetAdapterAdvancedProperty -Name '{interface}' -RegistryKeyword 'RoamingSensitivityLevel' -RegistryValue '{value}'",
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception:
        pass
    return False


BACKUP_PATH = os.path.expanduser("~/.wifituner_backup.json")


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
        backup["sysctl"]["net.inet.tcp.mssdflt"] = get_sysctl_value(
            "net.inet.tcp.mssdflt"
        )
        backup["sysctl"]["net.inet.tcp.win_scale_factor"] = get_sysctl_value(
            "net.inet.tcp.win_scale_factor"
        )
        backup["sysctl"]["net.inet.tcp.delayed_ack"] = get_sysctl_value(
            "net.inet.tcp.delayed_ack"
        )
    elif is_bsd():
        backup["sysctl"]["net.inet.tcp.mssdflt"] = get_sysctl_value(
            "net.inet.tcp.mssdflt"
        )
    elif OS_NAME == "Linux":
        backup["sysctl"]["net.ipv4.tcp_slow_start_after_idle"] = get_sysctl_value(
            "net.ipv4.tcp_slow_start_after_idle"
        )
        backup["sysctl"]["net.ipv4.tcp_notsent_lowat"] = get_sysctl_value(
            "net.ipv4.tcp_notsent_lowat"
        )
    elif OS_NAME == "Windows":
        backup["sysctl"] = get_windows_tcp_settings()
        backup["roaming_aggressiveness"] = get_windows_roaming_aggressiveness(interface)

    try:
        with open(BACKUP_PATH, "w") as f:
            json.dump(backup, f)
    except Exception as e:
        _warn(f"Failed to save backup configurations: {e}")


def revert_optimizations(interface: str) -> None:
    print("=== Reverting wifituner Optimizations to System Defaults ===")
    backup = None
    if os.path.exists(BACKUP_PATH):
        try:
            with open(BACKUP_PATH, "r") as f:
                backup = json.load(f)
            print(f"Loaded backup configurations from {BACKUP_PATH}")
        except Exception as e:
            _warn(
                f"Failed to read backup file: {e}. Falling back to default system heuristics."
            )

    if backup and "dns" in backup:
        dns_servers = backup["dns"]
        if dns_servers:
            print(f"[*] Restoring backup DNS servers: {dns_servers}")
            if OS_NAME == "Darwin":
                service = get_macos_service_name(interface)
                cmd = ["sudo", "networksetup", "-setdnsservers", service] + dns_servers
                subprocess.run(cmd, check=True)
            elif OS_NAME == "Linux" or is_bsd():
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
            print("[*] Reverting DNS to DHCP...")
            if OS_NAME == "Darwin":
                service = get_macos_service_name(interface)
                subprocess.run(
                    ["sudo", "networksetup", "-setdnsservers", service, "empty"],
                    check=True,
                )
            elif OS_NAME == "Linux" or is_bsd():
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
    else:
        print("[*] Reverting DNS to DHCP...")
        if OS_NAME == "Darwin":
            service = get_macos_service_name(interface)
            subprocess.run(
                ["sudo", "networksetup", "-setdnsservers", service, "empty"], check=True
            )
        elif OS_NAME == "Linux" or is_bsd():
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

    mtu = backup["mtu"] if (backup and "mtu" in backup) else 1500
    print(f"[*] Reverting MTU to {mtu}...")
    apply_mtu(interface, mtu)

    flush_dns_cache()

    print("[*] Reverting Wi-Fi adapter power management...")
    if OS_NAME == "Linux" or is_bsd():
        try:
            if OS_NAME == "Linux":
                subprocess.run(
                    ["sudo", "iw", "dev", interface, "set", "power_save", "on"],
                    check=True,
                    capture_output=True,
                )
                print("    Wi-Fi power saving re-enabled.")
            else:
                pass  # Power saving is not optimized on BSD, so no revert action needed
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
        default_sysctl = {
            "net.inet.tcp.mssdflt": "512",
            "net.inet.tcp.win_scale_factor": "3",
            "net.inet.tcp.delayed_ack": "3",
        }
        sysctl_values = (
            backup["sysctl"] if (backup and "sysctl" in backup) else default_sysctl
        )
        for key, val in default_sysctl.items():
            backup_val = sysctl_values.get(key)
            target_val = backup_val if backup_val else val
            try:
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={target_val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, target_val)
            except Exception as e:
                _warn(f"Failed to revert sysctl {key}: {e}")

    elif is_bsd():
        default_sysctl = {
            "net.inet.tcp.mssdflt": "512",
        }
        sysctl_values = (
            backup["sysctl"] if (backup and "sysctl" in backup) else default_sysctl
        )
        for key, val in default_sysctl.items():
            backup_val = sysctl_values.get(key)
            target_val = backup_val if backup_val else val
            try:
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={target_val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, target_val)
            except Exception as e:
                _warn(f"Failed to revert sysctl {key}: {e}")

    elif OS_NAME == "Linux":
        default_sysctl = {
            "net.ipv4.tcp_slow_start_after_idle": "1",
            "net.ipv4.tcp_notsent_lowat": "4294967295",
        }
        sysctl_values = (
            backup["sysctl"] if (backup and "sysctl" in backup) else default_sysctl
        )
        for key, val in default_sysctl.items():
            backup_val = sysctl_values.get(key)
            target_val = backup_val if backup_val else val
            try:
                subprocess.run(
                    ["sudo", "sysctl", "-w", f"{key}={target_val}"],
                    check=True,
                    capture_output=True,
                )
                _persist_sysctl(key, target_val)
            except Exception as e:
                _warn(f"Failed to revert sysctl {key}: {e}")
        try:
            subprocess.run(
                ["sudo", "rm", "-f", "/etc/sysctl.d/99-wifituner.conf"],
                check=True,
                capture_output=True,
            )
        except Exception:
            pass

    elif OS_NAME == "Windows":
        if is_admin():
            default_tcp = {
                "autotuninglevel": "normal",
                "rss": "enabled",
                "fastopen": "enabled",
                "ecncapability": "enabled",
            }
            tcp_values = (
                backup["sysctl"] if (backup and "sysctl" in backup) else default_tcp
            )
            for key, val in default_tcp.items():
                backup_val = tcp_values.get(key)
                target_val = backup_val if backup_val else val
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
    parser = argparse.ArgumentParser(
        description="Automated Wi-Fi Connection Analyzer & Optimizer"
    )
    parser.add_argument(
        "--gaming",
        action="store_true",
        help="Enable gaming/low-latency mode: disables TCP delayed ACK (net.inet.tcp.delayed_ack=0). "
        "Reduces interactive latency but may decrease bulk-transfer throughput.",
    )
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Revert all applied DNS, MTU, and TCP/IP optimizations to system defaults.",
    )
    parser.add_argument(
        "--domains",
        type=str,
        default="google.com,cloudflare.com,github.com,wikipedia.org",
        help="Comma-separated list of domains to use for DNS benchmarking.",
    )
    parser.add_argument(
        "--ping-host",
        type=str,
        default="1.1.1.1",
        help="Target IP host for latency and PMTU tests.",
    )
    parser.add_argument(
        "--dns-timeout",
        type=float,
        default=0.8,
        help="Timeout value in seconds for DNS queries.",
    )
    args = parser.parse_args()

    interface = get_wifi_interface()

    if args.revert:
        revert_optimizations(interface)
        return

    # Warm the airport JSON cache on macOS to prevent parallel system_profiler stampede
    if OS_NAME == "Darwin":
        _get_airport_json()

    # Background: channel scan (independent of everything else)
    scan_thread = threading.Thread(target=bg_scan_channels)
    scan_thread.daemon = True
    scan_thread.start()

    print("=== Automated Wi-Fi Connection Analyzer & Optimizer ===")
    print(f"Platform: {OS_NAME}")
    if args.gaming:
        print(
            "[Gaming Mode] TCP delayed ACK will be disabled (net.inet.tcp.delayed_ack=0)."
        )

    print(f"Interface: {interface}")

    # 1. Retrieve Current Network Details
    wifi_details = get_current_wifi_details(interface)
    if wifi_details:
        print("\n--- Active Wi-Fi Connection Link Parameters ---")
        for k, v in wifi_details.items():
            print(f"  {k:<15}: {v}")
        print("------------------------------------------------")
    else:
        print("\n[Warning] Could not fetch connected Wi-Fi parameters.")

    # 3. Run latency test and DNS benchmark in parallel.
    # PMTU needs icmp_supported from latency, so it runs after latency completes
    # but overlaps with whatever remains of the DNS benchmark.
    print("\nRunning diagnostics in parallel (latency + DNS benchmark)...")
    test_domains_list = [d.strip() for d in args.domains.split(",") if d.strip()]
    if not test_domains_list:
        test_domains_list = [
            "google.com",
            "cloudflare.com",
            "github.com",
            "wikipedia.org",
        ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        lat_future = executor.submit(test_latency, args.ping_host)
        dns_future = executor.submit(
            benchmark_dns, interface, test_domains_list, args.dns_timeout
        )

        latency, icmp_supported = lat_future.result()
        dns_results = dns_future.result()

    print("\nMeasured connection latency:")
    print_latency_results(latency)

    fastest_dns = None
    if dns_results:
        print("\n--- DNS Resolver Speed Profile ---")
        sorted_dns = sorted(dns_results.items(), key=lambda x: x[1]["avg"])
        for name, data in sorted_dns:
            print(f"  {name:<25} ({data['ip']:<15}) : {data['avg']} ms")
        fastest_dns = sorted_dns[0]
        print("----------------------------------")

    # 4. Perform Path MTU Discovery (after icmp_supported is known)
    print("")
    pmtu = discover_pmtu(icmp_supported, args.ping_host)
    if not pmtu:
        pmtu = 1500

    # 5. Join background channel scan
    scan_thread.join(timeout=3.0)
    rec_24, rec_5, rec_6 = get_channel_recommendation(neighbor_channels)

    # 6. Save backup before applying optimizations
    save_backup(interface)

    # 7. Output Recommendations & Apply Optimizations
    print("\n============================================================")
    print("      AUTOMATED CONNECTION OPTIMIZATION REPORT & ACTION     ")
    print("============================================================\n")

    # 7a. Apply DNS
    if fastest_dns:
        dns_ip = fastest_dns[1]["ip"]
        print(
            f"[*] Recommended DNS Resolver: {dns_ip} ({fastest_dns[0]}) at {fastest_dns[1]['avg']} ms"
        )
        if apply_dns(interface, dns_ip):
            if verify_dns(interface, dns_ip):
                print("    DNS verified active.")
            else:
                print("    Warning: DNS verification failed.")
                if OS_NAME == "Darwin":
                    service = get_macos_service_name(interface)
                    print(
                        f"    Revert: sudo networksetup -setdnsservers {service} empty"
                    )
                elif OS_NAME == "Linux":
                    print(
                        "    Revert: sudo resolvectl dns <interface> (or edit /etc/resolv.conf)"
                    )
                elif OS_NAME == "Windows":
                    print(
                        f"    Revert: netsh interface ipv4 set dns name={interface} dhcp"
                    )

    print("")
    # 7b. Apply MTU
    print(f"[*] Recommended MTU Size  : {pmtu} bytes")
    apply_mtu(interface, pmtu)

    print("")
    # 7c. Flush cache
    flush_dns_cache()

    print("")
    # 7d. Apply kernel TCP stack tweaks
    apply_sysctl_optimizations(gaming=args.gaming)

    # 7e. Apply Wi-Fi power-saving and roaming aggressiveness optimizations
    apply_power_save_optimization(interface)
    if OS_NAME == "Windows":
        print("[*] Tuning Roaming Aggressiveness (setting to Medium-Low)...")
        set_windows_roaming_aggressiveness(interface, "2")

    print("")
    # 7f. Channel recommendations
    print(
        "[*] Wireless Channel Recommendations (Must adjust manually on your Access Point):"
    )
    print(
        f"    - Cleanest 2.4 GHz Band : Channel {rec_24} (Occupied by {neighbor_channels['2GHz'].get(rec_24, 0)} neighbors)"
    )
    print(
        f"    - Cleanest 5.0 GHz Band : Channel {rec_5} (Occupied by {neighbor_channels['5GHz'].get(rec_5, 0)} neighbors)"
    )
    if rec_6 is not None:
        print(
            f"    - Cleanest 6.0 GHz Band : Channel {rec_6} (Occupied by {neighbor_channels['6GHz'].get(rec_6, 0)} neighbors)"
        )

    if wifi_details:
        sec = wifi_details.get("Security", "")
        if "Enterprise" in sec or "802.1X" in sec:
            print("\n[Note] You are associated with an Enterprise Wi-Fi network.")
            print(
                "       If custom DNS servers block local domain resolution or authentication,"
            )
            print(
                "       you can revert DNS to DHCP using standard OS network settings."
            )

    print("\n============================================================")


if __name__ == "__main__":
    main()
