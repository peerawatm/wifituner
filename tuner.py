__version__ = "0.0.1"
#!/usr/bin/env python3
import os
import sys
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
neighbor_channels = {}

def is_admin():
    try:
        if OS_NAME == "Windows":
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
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
                check=True
            )
            pattern = re.compile(
                r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*([a-zA-Z0-9]+)",
                re.MULTILINE
            )
            match = pattern.search(result.stdout)
            if match:
                return match.group(1)
        except Exception:
            pass
        return "en0"
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
                if os.path.exists(f"/sys/class/net/{d}/wireless") or os.path.exists(f"/sys/class/net/{d}/phy80211"):
                    return d
        except Exception:
            pass
        return "wlan0"
    elif OS_NAME == "Windows":
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                check=True
            )
            for line in result.stdout.splitlines():
                if "Name" in line:
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        return parts[1].strip()
        except Exception:
            pass
        return "Wi-Fi"
    return "wlan0"

def get_macos_service_name(interface):
    try:
        result = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True)
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
        pass
    return "Wi-Fi"

def get_macos_wifi_details():
    details = {}
    try:
        result = subprocess.run(
            ["system_profiler", "SPAirPortDataType"],
            capture_output=True,
            text=True
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
                            details["SNR"] = f"{int(match.group(1)) - int(match.group(2))} dB"
                    elif key == "Transmit Rate":
                        details["Transmit Rate"] = f"{val} Mbps"
    except Exception:
        pass
    return details

def get_windows_wifi_details():
    details = {}
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return details
            
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()
            
            if key == "SSID":
                details["SSID"] = val
            elif key == "Radio type":
                details["PHY Mode"] = val
            elif key == "Channel":
                details["Channel"] = val
            elif key == "Authentication":
                details["Security"] = val
            elif key == "Signal":
                details["RSSI"] = val
            elif key == "Transmit rate (Mbps)":
                details["Transmit Rate"] = f"{val} Mbps"
    except Exception:
        pass
    return details

def get_linux_wifi_details(interface):
    details = {}
    try:
        result = subprocess.run(
            ["iw", "dev", interface, "link"],
            capture_output=True,
            text=True
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
                        if freq >= 5000:
                            details["Channel"] = f"{freq} MHz (5GHz)"
                        elif freq >= 2400:
                            details["Channel"] = f"{freq} MHz (2.4GHz)"
                    except ValueError:
                        details["Channel"] = freq_mhz
                elif stripped.startswith("signal:"):
                    details["RSSI"] = stripped.split("signal:", 1)[1].strip()
                elif stripped.startswith("tx bitrate:"):
                    details["Transmit Rate"] = stripped.split("tx bitrate:", 1)[1].strip()
            if "SSID" in details:
                details["PHY Mode"] = "802.11 (Linux iw)"
                details["Security"] = "Enterprise/Personal"
                return details
                
        result = subprocess.run(
            ["iwconfig", interface],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "ESSID:" in line:
                    match = re.search(r'ESSID:"([^"]+)"', line)
                    if match:
                        details["SSID"] = match.group(1)
                if "Frequency:" in line:
                    match = re.search(r'Frequency:([\d\.]+)\s*GHz', line)
                    if match:
                        details["Channel"] = f"{match.group(1)} GHz"
                if "Bit Rate" in line:
                    match = re.search(r'Bit Rate=([\d\.]+)\s*Mb/s', line)
                    if match:
                        details["Transmit Rate"] = f"{match.group(1)} Mbps"
                if "Signal level" in line:
                    match = re.search(r'Signal level=(-?\d+)\s*dBm', line)
                    if match:
                        details["RSSI"] = f"{match.group(1)} dBm"
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
    return {}

def get_dns_servers(interface):
    if OS_NAME == "Darwin":
        try:
            result = subprocess.run(
                ["networksetup", "-getdnsservers", "Wi-Fi"],
                capture_output=True,
                text=True
            )
            output = result.stdout.strip()
            if result.returncode == 0 and output and "There aren't any DNS" not in output:
                return output.split("\n")
        except Exception:
            pass
        return []
    elif OS_NAME == "Linux":
        dns = []
        try:
            with open("/etc/resolv.conf", "r") as f:
                for line in f:
                    if line.startswith("nameserver"):
                        dns.append(line.split()[1])
        except Exception:
            pass
        return dns
    elif OS_NAME == "Windows":
        dns = []
        try:
            result = subprocess.run(
                ["netsh", "interface", "ipv4", "show", "dns", f"name={interface}"],
                capture_output=True,
                text=True
            )
            ip_pattern = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
            for line in result.stdout.splitlines():
                if "DNS servers" in line or "Statically Configured" in line or ip_pattern.search(line):
                    ips = ip_pattern.findall(line)
                    dns.extend(ips)
        except Exception:
            pass
        return dns
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
            pass
        time.sleep(0.02)
    return compute_stats(latencies, "TCP (Port 443)")

def test_latency(host="1.1.1.1", count=5):
    icmp_supported = False
    try:
        if OS_NAME in ("Darwin", "Linux"):
            probe_cmd = ["ping", "-c", "1", "-t", "1", host] if OS_NAME == "Darwin" else ["ping", "-c", "1", "-W", "1", host]
        elif OS_NAME == "Windows":
            probe_cmd = ["ping", "-n", "1", "-w", "1000", host]
        else:
            probe_cmd = None
            
        if probe_cmd:
            probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
            if probe_result.returncode == 0:
                icmp_supported = True
    except Exception:
        pass

    if icmp_supported:
        try:
            cmd = []
            if OS_NAME in ("Darwin", "Linux"):
                cmd = ["ping", "-c", str(count), host]
            elif OS_NAME == "Windows":
                cmd = ["ping", "-n", str(count), host]
                
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                latencies = []
                for line in result.stdout.splitlines():
                    match = re.search(r"time[=<]\s*([\d\.]+)\s*(ms)?", line, re.IGNORECASE)
                    if match:
                        latencies.append(float(match.group(1)))
                stats = compute_stats(latencies, "ICMP")
                if stats:
                    return stats, True
        except Exception:
            pass

    return test_tcp_latency(host, port=443, count=count), False

def compute_stats(latencies, type_str):
    if not latencies:
        return None
    avg_lat = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    variance = sum((x - avg_lat) ** 2 for x in latencies) / len(latencies)
    stddev = variance ** 0.5
    return {
        "min": round(min_lat, 3),
        "avg": round(avg_lat, 3),
        "max": round(max_lat, 3),
        "stddev": round(stddev, 3),
        "type": type_str
    }

def build_dns_query(domain):
    tx_id = random.randint(0, 65535)
    header = tx_id.to_bytes(2, byteorder='big') + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
    question = b''
    for part in domain.split('.'):
        question += len(part).to_bytes(1, byteorder='big') + part.encode('utf-8')
    question += b'\x00\x00\x01\x00\x01'
    return header + question

def benchmark_single_resolver(name, ip, test_domains):
    latencies = []
    for domain in test_domains:
        try:
            resolver_addr = (ip, 53)
            query = build_dns_query(domain)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.8)
            t_start = time.perf_counter()
            sock.sendto(query, resolver_addr)
            data, _ = sock.recvfrom(512)
            t_end = time.perf_counter()
            latencies.append((t_end - t_start) * 1000.0)
            sock.close()
        except Exception:
            pass
    if latencies:
        return name, {
            "ip": ip,
            "avg": round(sum(latencies) / len(latencies), 2)
        }
    return name, None

def benchmark_dns(interface):
    print("Benchmarking DNS resolver latencies (concurrent queries)...")
    resolvers = {
        "Cloudflare Primary": "1.1.1.1",
        "Cloudflare Secondary": "1.0.0.1",
        "Google Primary": "8.8.8.8",
        "Google Secondary": "8.8.4.4",
        "Quad9 Secure": "9.9.9.9"
    }
    
    system_dns = get_dns_servers(interface)
    for idx, ip in enumerate(system_dns):
        if ip not in resolvers.values():
            resolvers[f"Current System DNS {idx+1}"] = ip
            
    results = {}
    test_domains = ["google.com", "cloudflare.com", "github.com", "wikipedia.org"]
    
    with ThreadPoolExecutor(max_workers=len(resolvers)) as executor:
        futures = [executor.submit(benchmark_single_resolver, name, ip, test_domains) for name, ip in resolvers.items()]
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
                cmd = ["ping", "-D", "-s", str(size), "-c", "1", "-t", "1", host]
            elif OS_NAME == "Linux":
                cmd = ["ping", "-M", "do", "-s", str(size), "-c", "1", "-W", "1", host]
            elif OS_NAME == "Windows":
                cmd = ["ping", "-f", "-l", str(size), "-n", "1", "-w", "1000", host]
            else:
                continue
                
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and "100%" not in result.stdout:
                optimal_payload = size
                break
        except Exception:
            pass
            
    if optimal_payload is not None:
        return optimal_payload + 28
    return None

def scan_neighbor_channels():
    channels = {}
    try:
        if OS_NAME == "Darwin":
            result = subprocess.run(
                ["system_profiler", "SPAirPortDataType"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                matches = re.findall(r"Channel:\s*(\d+)", result.stdout)
                for ch in matches:
                    channels[ch] = channels.get(ch, 0) + 1
        elif OS_NAME == "Windows":
            result = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "Channel" in line:
                        parts = line.split(":", 1)
                        if len(parts) > 1:
                            ch = parts[1].strip()
                            channels[ch] = channels.get(ch, 0) + 1
        elif OS_NAME == "Linux":
            result = subprocess.run(
                ["nmcli", "-f", "CHAN", "dev", "wifi", "list"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines()[1:]:
                    ch = line.strip()
                    if ch.isdigit():
                        channels[ch] = channels.get(ch, 0) + 1
    except Exception:
        pass
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

def get_channel_recommendation(neighbor_channels):
    c_24 = ["1", "6", "11"]
    c_5 = ["36", "40", "44", "48", "149", "153", "157", "161"]
    
    congestion_24 = {ch: neighbor_channels.get(ch, 0) for ch in c_24}
    congestion_5 = {ch: neighbor_channels.get(ch, 0) for ch in c_5}
    
    rec_24 = min(congestion_24, key=congestion_24.get)
    rec_5 = min(congestion_5, key=congestion_5.get)
    
    return rec_24, rec_5

def apply_dns(interface, dns_ip):
    print(f"[*] Applying DNS: Configuration targeting {dns_ip}...")
    if OS_NAME == "Darwin":
        service = get_macos_service_name(interface)
        cmd = ["sudo", "networksetup", "-setdnsservers", service, dns_ip]
    elif OS_NAME == "Linux":
        try:
            subprocess.run(["resolvectl", "--version"], capture_output=True, check=True)
            cmd = ["sudo", "resolvectl", "dns", interface, dns_ip]
        except Exception:
            cmd = ["sudo", "sh", "-c", f"echo 'nameserver {dns_ip}' > /etc/resolv.conf"]
    elif OS_NAME == "Windows":
        cmd = ["netsh", "interface", "ipv4", "set", "dns", f"name={interface}", "static", dns_ip]
        if not is_admin():
            print("Elevation required. Please run this script inside an Administrator terminal.")
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

def apply_mtu(interface, mtu_size):
    print(f"[*] Applying MTU: Configuration targeting {mtu_size} bytes...")
    if OS_NAME == "Darwin":
        cmd = ["sudo", "ifconfig", interface, "mtu", str(mtu_size)]
    elif OS_NAME == "Linux":
        cmd = ["sudo", "ip", "link", "set", "dev", interface, "mtu", str(mtu_size)]
    elif OS_NAME == "Windows":
        cmd = ["netsh", "interface", "ipv4", "set", "subinterface", interface, f"mtu={mtu_size}", "store=persistent"]
        if not is_admin():
            print("Elevation required. Please run this script inside an Administrator terminal.")
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
            pass
    elif OS_NAME == "Linux":
        try:
            subprocess.run(["sudo", "resolvectl", "flush-caches"], check=True)
            print("    DNS cache flushed.")
        except Exception:
            try:
                subprocess.run(["sudo", "systemd-resolve", "--flush-caches"], check=True)
                print("    DNS cache flushed.")
            except Exception:
                pass
    elif OS_NAME == "Windows":
        try:
            subprocess.run(["ipconfig", "/flushdns"], check=True)
            print("    DNS cache flushed.")
        except Exception:
            pass

def apply_sysctl_optimizations():
    print("[*] Tuning TCP/IP kernel stack parameters for lower RTT and zero fragmentation...")
    if OS_NAME == "Darwin":
        optimizations = {
            "net.inet.tcp.delayed_ack": "0",    # Immediate ACKs (resolves gaming/WebRTC jitter)
            "net.inet.tcp.mssdflt": "1460",     # Optimal segment size (avoids fragmentation overhead)
            "net.inet.tcp.win_scale_factor": "8" # Expands receive window size limit
        }
        for key, val in optimizations.items():
            try:
                print(f"    Setting {key} = {val}...")
                subprocess.run(["sudo", "sysctl", "-w", f"{key}={val}"], check=True, capture_output=True)
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")
    elif OS_NAME == "Linux":
        optimizations = {
            "net.ipv4.tcp_slow_start_after_idle": "0",
            "net.ipv4.tcp_notsent_lowat": "16384",
            "net.ipv4.tcp_low_latency": "1"
        }
        for key, val in optimizations.items():
            try:
                print(f"    Setting {key} = {val}...")
                subprocess.run(["sudo", "sysctl", "-w", f"{key}={val}"], check=True, capture_output=True)
            except Exception as e:
                print(f"    Failed to apply {key}: {e}")

def main():
    scan_thread = threading.Thread(target=bg_scan_channels)
    scan_thread.daemon = True
    scan_thread.start()

    print(f"=== Automated Wi-Fi Connection Analyzer & Optimizer ===")
    print(f"Platform: {OS_NAME}")
    
    interface = get_wifi_interface()
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
        
    # 2. Test Baseline Connection Latency
    print("\nMeasuring connection latency baseline...")
    latency, icmp_supported = test_latency()
    print_latency_results(latency)
    
    # 3. Benchmark Public DNS Resolvers
    print("")
    dns_results = benchmark_dns(interface)
    fastest_dns = None
    if dns_results:
        print("\n--- DNS Resolver Speed Profile ---")
        sorted_dns = sorted(dns_results.items(), key=lambda x: x[1]['avg'])
        for name, data in sorted_dns:
            print(f"  {name:<25} ({data['ip']:<15}) : {data['avg']} ms")
        fastest_dns = sorted_dns[0]
        print("----------------------------------")
        
    # 4. Perform Path MTU Discovery
    print("")
    pmtu = discover_pmtu(icmp_supported)
    if not pmtu:
        pmtu = 1500
    
    # 5. Join background channel scan
    scan_thread.join(timeout=3.0)
    rec_24, rec_5 = get_channel_recommendation(neighbor_channels)
    
    # 6. Output Recommendations & Apply Optimizations
    print("\n============================================================")
    print("      AUTOMATED CONNECTION OPTIMIZATION REPORT & ACTION     ")
    print("============================================================\n")
    
    # 6a. Apply DNS
    if fastest_dns:
        dns_ip = fastest_dns[1]['ip']
        print(f"[*] Recommended DNS Resolver: {dns_ip} ({fastest_dns[0]}) at {fastest_dns[1]['avg']} ms")
        apply_dns(interface, dns_ip)
    
    print("")
    # 6b. Apply MTU
    print(f"[*] Recommended MTU Size  : {pmtu} bytes")
    apply_mtu(interface, pmtu)
    
    print("")
    # 6c. Flush cache
    flush_dns_cache()
    
    print("")
    # 6d. Apply kernel TCP stack tweaks
    apply_sysctl_optimizations()
    
    print("")
    # 6e. Channel recommendations
    print(f"[*] Wireless Channel Recommendations (Must adjust manually on your Access Point):")
    print(f"    - Cleanest 2.4 GHz Band : Channel {rec_24} (Occupied by {neighbor_channels.get(rec_24, 0)} neighbors)")
    print(f"    - Cleanest 5.0 GHz Band : Channel {rec_5} (Occupied by {neighbor_channels.get(rec_5, 0)} neighbors)")
    
    if wifi_details:
        sec = wifi_details.get("Security", "")
        if "Enterprise" in sec or "802.1X" in sec:
            print("\n[Note] You are associated with an Enterprise Wi-Fi network.")
            print("       If custom DNS servers block local domain resolution or authentication,")
            print("       you can revert DNS to DHCP using standard OS network settings.")
            
    print("\n============================================================")

if __name__ == "__main__":
    main()
