import json
import unittest
from unittest.mock import patch, MagicMock, mock_open
import tuner


class TestTunerMultiPlatform(unittest.TestCase):
    def setUp(self):
        tuner.get_macos_service_name.cache_clear()
        tuner._get_airport_json.cache_clear()

    # --- platform.system() and admin status ---

    @patch("platform.system", return_value="Windows")
    def test_is_admin_windows_admin(self, mock_system):
        tuner.OS_NAME = "Windows"
        mock_ctypes = MagicMock()
        mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = 1
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            self.assertTrue(tuner.is_admin())

    @patch("platform.system", return_value="Windows")
    def test_is_admin_windows_user(self, mock_system):
        tuner.OS_NAME = "Windows"
        mock_ctypes = MagicMock()
        mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = 0
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            self.assertFalse(tuner.is_admin())

    @patch("platform.system", return_value="Linux")
    @patch("os.geteuid", return_value=0)
    def test_is_admin_linux_root(self, mock_geteuid, mock_system):
        tuner.OS_NAME = "Linux"
        self.assertTrue(tuner.is_admin())

    @patch("platform.system", return_value="Linux")
    @patch("os.geteuid", return_value=1000)
    def test_is_admin_linux_user(self, mock_geteuid, mock_system):
        tuner.OS_NAME = "Linux"
        self.assertFalse(tuner.is_admin())

    # --- get_wifi_interface ---

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_get_wifi_interface_windows(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "Name : Wi-Fi 2\nDescription : Intel(R) Wi-Fi"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        tuner.OS_NAME = "Windows"
        interface = tuner.get_wifi_interface()
        self.assertEqual(interface, "Wi-Fi 2")
        mock_run.assert_called_with(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True,
            text=True,
            check=True,
        )

    @patch("platform.system", return_value="Linux")
    @patch("os.path.exists")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="Inter-| sta-|   Quality        | Discarded packets\n face | tus | link level noise |  nwid  crypt   frag  retry   misc\n wlan1: 0000   45.  -65.  -256.        0      0      0      0      0",
    )
    def test_get_wifi_interface_linux_proc(self, mock_file, mock_exists, mock_system):
        mock_exists.side_effect = lambda path: path == "/proc/net/wireless"
        tuner.OS_NAME = "Linux"
        interface = tuner.get_wifi_interface()
        self.assertEqual(interface, "wlan1")

    # --- get_wifi_details ---

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_get_windows_wifi_details(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "SSID : HomeNet\nRadio type : 802.11ax\nChannel : 36\nAuthentication : WPA3-Personal\nSignal : 99%\nTransmit rate (Mbps) : 1201"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        tuner.OS_NAME = "Windows"
        details = tuner.get_windows_wifi_details()
        self.assertEqual(details["SSID"], "HomeNet")
        self.assertEqual(details["Channel"], "36")
        self.assertEqual(details["RSSI"], "99%")

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_get_linux_wifi_details_iw(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "Connected to 00:11:22:33:44:55 (on wlan0)\nSSID: TestLinux\nfreq: 5240\nsignal: -50 dBm\ntx bitrate: 866.7 MBit/s"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        tuner.OS_NAME = "Linux"
        details = tuner.get_linux_wifi_details("wlan0")
        self.assertEqual(details["SSID"], "TestLinux")
        self.assertEqual(details["Channel"], "5240 MHz (5GHz)")
        self.assertEqual(details["RSSI"], "-50 dBm")

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_get_linux_wifi_details_6ghz(self, mock_run, mock_system):
        """6 GHz frequency (>= 5925 MHz) is classified as 6GHz."""
        mock_result = MagicMock()
        mock_result.stdout = "Connected to 00:11:22:33:44:55 (on wlan0)\nSSID: TestLinux6E\nfreq: 5955\nsignal: -45 dBm\ntx bitrate: 2401 MBit/s"
        mock_result.returncode = 0
        mock_run.return_value = mock_result

        tuner.OS_NAME = "Linux"
        details = tuner.get_linux_wifi_details("wlan0")
        self.assertEqual(details["Channel"], "5955 MHz (6GHz)")

    # --- get_dns_servers ---

    @patch("platform.system", return_value="Linux")
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="nameserver 1.1.1.1\nnameserver 8.8.8.8",
    )
    def test_get_dns_servers_linux(self, mock_file, mock_system):
        tuner.OS_NAME = "Linux"
        dns = tuner.get_dns_servers("wlan0")
        self.assertEqual(dns, ["1.1.1.1", "8.8.8.8"])

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_get_dns_servers_windows(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "DNS servers: 1.1.1.1\n             8.8.8.8"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Windows"
        dns = tuner.get_dns_servers("Wi-Fi")
        self.assertEqual(dns, ["1.1.1.1", "8.8.8.8"])

    # --- discover_pmtu ---

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_discover_pmtu_linux(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "1 packets transmitted, 1 received, 0% packet loss"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Linux"
        pmtu = tuner.discover_pmtu(icmp_supported=True, host="1.1.1.1")
        self.assertEqual(pmtu, 1500)
        mock_run.assert_any_call(
            ["ping", "-M", "do", "-s", "1472", "-c", "2", "-W", "1", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_discover_pmtu_windows(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "Reply from 1.1.1.1: bytes=1472 time=10ms"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Windows"
        pmtu = tuner.discover_pmtu(icmp_supported=True, host="1.1.1.1")
        self.assertEqual(pmtu, 1500)
        mock_run.assert_any_call(
            ["ping", "-f", "-l", "1472", "-n", "2", "-w", "1000", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )

    # --- scan_neighbor_channels ---

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run")
    def test_scan_neighbor_channels_macos_json(self, mock_run, mock_system):
        """macOS JSON path: band parsed from channel string '6 (2GHz, 20MHz)'."""
        tuner.OS_NAME = "Darwin"
        payload = {
            "SPAirPortDataType": [
                {
                    "spairport_airport_interfaces": [
                        {
                            "_name": "en0",
                            "spairport_airport_other_local_wireless_networks": [
                                {
                                    "_name": "Net1",
                                    "spairport_network_channel": "6 (2GHz, 20MHz)",
                                },
                                {
                                    "_name": "Net2",
                                    "spairport_network_channel": "1 (2GHz, 20MHz)",
                                },
                                {
                                    "_name": "Net3",
                                    "spairport_network_channel": "6 (2GHz, 20MHz)",
                                },
                                {
                                    "_name": "Net4",
                                    "spairport_network_channel": "36 (5GHz, 80MHz)",
                                },
                                {
                                    "_name": "Net5",
                                    "spairport_network_channel": "37 (6GHz, 80MHz)",
                                },
                            ],
                        }
                    ]
                }
            ]
        }
        mock_run.return_value = MagicMock(stdout=json.dumps(payload), returncode=0)
        tuner._get_airport_json.cache_clear()
        channels = tuner.scan_neighbor_channels()
        self.assertEqual(channels["2GHz"].get("6"), 2)
        self.assertEqual(channels["2GHz"].get("1"), 1)
        self.assertEqual(channels["5GHz"].get("36"), 1)
        self.assertEqual(channels["6GHz"].get("37"), 1)

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_scan_neighbor_channels_linux(self, mock_run, mock_system):
        """Linux nmcli path: uses CHAN,FREQ columns; GHz unit triggers × 1000 conversion."""
        mock_result = MagicMock()
        # nmcli -f CHAN,FREQ format (locale decimal: comma, unit: GHz)
        mock_result.stdout = (
            "CHAN  FREQ\n"
            "1     2,412 GHz\n"
            "6     2,437 GHz\n"
            "1     2,412 GHz\n"
            "36    5,180 GHz\n"
        )
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Linux"
        channels = tuner.scan_neighbor_channels()
        mock_run.assert_called_once_with(
            ["nmcli", "-f", "CHAN,FREQ", "dev", "wifi", "list"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(channels["2GHz"].get("1"), 2)
        self.assertEqual(channels["2GHz"].get("6"), 1)
        self.assertEqual(channels["5GHz"].get("36"), 1)
        self.assertEqual(channels["6GHz"], {})

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_scan_neighbor_channels_windows(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = (
            "BSSID 1\n  Channel: 6\nBSSID 2\n  Channel: 11\nBSSID 3\n  Channel: 6"
        )
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Windows"
        channels = tuner.scan_neighbor_channels()
        self.assertEqual(channels["2GHz"].get("6"), 2)
        self.assertEqual(channels["2GHz"].get("11"), 1)
        self.assertEqual(channels["5GHz"], {})
        self.assertEqual(channels["6GHz"], {})

    # --- get_channel_recommendation ---

    def test_get_channel_recommendation_no_6ghz(self):
        """rec_6 is None when 6GHz band is empty."""
        ch_by_band = {
            "2GHz": {"1": 3, "6": 1, "11": 0},
            "5GHz": {
                "36": 2,
                "40": 0,
                "44": 3,
                "48": 3,
                "149": 3,
                "153": 3,
                "157": 3,
                "161": 3,
            },
            "6GHz": {},
        }
        rec_24, rec_5, rec_6 = tuner.get_channel_recommendation(ch_by_band)
        self.assertEqual(rec_24, "11")
        self.assertEqual(rec_5, "40")
        self.assertIsNone(rec_6)

    def test_get_channel_recommendation_with_6ghz(self):
        """rec_6 selects the PSC channel with fewest neighbors."""
        c_6_psc = [
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
        # All PSC channels congested (2 each) except 181 (0)
        ch_6 = {ch: (0 if ch == "181" else 2) for ch in c_6_psc}
        ch_by_band = {"2GHz": {}, "5GHz": {}, "6GHz": ch_6}
        _, _, rec_6 = tuner.get_channel_recommendation(ch_by_band)
        self.assertEqual(rec_6, "181")

    # --- apply configurations ---

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_apply_dns_linux_resolvectl(self, mock_run, mock_system):
        tuner.OS_NAME = "Linux"
        # First call checks version of resolvectl, second applies DNS
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        res = tuner.apply_dns("wlan0", "1.1.1.1")
        self.assertTrue(res)
        mock_run.assert_any_call(
            ["sudo", "resolvectl", "dns", "wlan0", "1.1.1.1"], check=True
        )

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=True)
    @patch("subprocess.run")
    def test_apply_mtu_windows(self, mock_run, mock_is_admin, mock_system):
        tuner.OS_NAME = "Windows"
        mock_run.return_value = MagicMock(returncode=0)
        res = tuner.apply_mtu("Wi-Fi", 1500)
        self.assertTrue(res)
        mock_run.assert_called_with(
            [
                "netsh",
                "interface",
                "ipv4",
                "set",
                "subinterface",
                "Wi-Fi",
                "mtu=1500",
                "store=persistent",
            ],
            check=True,
        )

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_flush_dns_cache_linux(self, mock_run, mock_system):
        tuner.OS_NAME = "Linux"
        mock_run.return_value = MagicMock(returncode=0)
        tuner.flush_dns_cache()
        mock_run.assert_any_call(["sudo", "resolvectl", "flush-caches"], check=True)

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run")
    def test_flush_dns_cache_darwin_failure_warns(self, mock_run, mock_system):
        """Failed macOS cache flush prints a [warn] line instead of silently passing."""
        tuner.OS_NAME = "Darwin"
        mock_run.side_effect = Exception("permission denied")
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            tuner.flush_dns_cache()
        self.assertIn("[warn]", buf.getvalue())

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=True)
    @patch("subprocess.run")
    def test_apply_sysctl_optimizations_windows_admin(
        self, mock_run, mock_is_admin, mock_system
    ):
        tuner.OS_NAME = "Windows"
        mock_run.return_value = MagicMock(returncode=0)
        tuner.apply_sysctl_optimizations()
        mock_run.assert_any_call(
            ["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"],
            check=True,
            capture_output=True,
        )
        mock_run.assert_any_call(
            ["netsh", "int", "tcp", "set", "global", "rss=enabled"],
            check=True,
            capture_output=True,
        )

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=False)
    @patch("subprocess.run")
    def test_apply_sysctl_optimizations_windows_non_admin(
        self, mock_run, mock_is_admin, mock_system
    ):
        tuner.OS_NAME = "Windows"
        tuner.apply_sysctl_optimizations()
        mock_run.assert_not_called()

    # --- verify_dns ---

    @patch("subprocess.run")
    def test_verify_dns_macos_present(self, mock_run):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(stdout="1.1.1.1\n", returncode=0)
        with patch("tuner.get_macos_service_name", return_value="Wi-Fi"):
            self.assertTrue(tuner.verify_dns("en0", "1.1.1.1"))

    @patch("subprocess.run")
    def test_verify_dns_macos_absent(self, mock_run):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(stdout="8.8.8.8\n", returncode=0)
        with patch("tuner.get_macos_service_name", return_value="Wi-Fi"):
            self.assertFalse(tuner.verify_dns("en0", "1.1.1.1"))

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data="nameserver 1.1.1.1\nnameserver 8.8.8.8\n",
    )
    def test_verify_dns_linux_present(self, mock_file, mock_run):
        tuner.OS_NAME = "Linux"
        self.assertTrue(tuner.verify_dns("wlan0", "1.1.1.1"))

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("builtins.open", new_callable=mock_open, read_data="nameserver 8.8.8.8\n")
    def test_verify_dns_linux_absent(self, mock_file, mock_run):
        tuner.OS_NAME = "Linux"
        self.assertFalse(tuner.verify_dns("wlan0", "1.1.1.1"))

    @patch("subprocess.run")
    def test_verify_dns_linux_resolvectl_present(self, mock_run):
        """resolvectl path: DNS IP found in resolvectl status output."""
        tuner.OS_NAME = "Linux"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  DNS Servers: 1.1.1.1\n"
        )
        self.assertTrue(tuner.verify_dns("wlan0", "1.1.1.1"))
        mock_run.assert_called_with(
            ["resolvectl", "status", "wlan0"], capture_output=True, text=True
        )

    @patch("subprocess.run")
    def test_verify_dns_linux_resolvectl_absent(self, mock_run):
        """resolvectl path: DNS IP not in resolvectl status output."""
        tuner.OS_NAME = "Linux"
        mock_run.return_value = MagicMock(
            returncode=0, stdout="  DNS Servers: 8.8.8.8\n"
        )
        self.assertFalse(tuner.verify_dns("wlan0", "1.1.1.1"))

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("builtins.open", new_callable=mock_open, read_data="nameserver 1.1.1.1\n")
    def test_verify_dns_linux_fallback_resolv(self, mock_file, mock_run):
        """Fallback to /etc/resolv.conf when resolvectl not installed."""
        tuner.OS_NAME = "Linux"
        self.assertTrue(tuner.verify_dns("wlan0", "1.1.1.1"))

    # --- get_macos_wifi_details JSON path ---

    @patch("subprocess.run")
    def test_get_macos_wifi_details_json(self, mock_run):
        tuner.OS_NAME = "Darwin"
        # Payload uses the correct key names as returned by macOS system_profiler.
        payload = {
            "SPAirPortDataType": [
                {
                    "spairport_airport_interfaces": [
                        {
                            "_name": "en0",
                            "spairport_current_network_information": {
                                "_name": "TestNet",
                                "spairport_network_channel": "6 (2GHz, 20MHz)",
                                "spairport_network_phymode": "802.11ax",
                                "spairport_signal_noise": "-55 dBm / -92 dBm",
                                "spairport_security_mode": "WPA2 Personal",
                                "spairport_network_rate": 300,
                            },
                        }
                    ]
                }
            ]
        }
        mock_run.return_value = MagicMock(stdout=json.dumps(payload), returncode=0)
        details = tuner.get_macos_wifi_details()
        self.assertEqual(details["SSID"], "TestNet")
        self.assertEqual(details["Channel"], "6 (2GHz, 20MHz)")
        self.assertEqual(details["PHY Mode"], "802.11ax")
        self.assertEqual(details["RSSI"], "-55 dBm")
        self.assertEqual(details["Noise"], "-92 dBm")
        self.assertEqual(details["SNR"], "37 dB")
        self.assertEqual(details["Transmit Rate"], "300 Mbps")

    # --- _parse_channel_band ---

    def test_parse_channel_band_5ghz(self):
        ch, band, width = tuner._parse_channel_band("64 (5GHz, 80MHz)")
        self.assertEqual(ch, "64")
        self.assertEqual(band, "5GHz")
        self.assertEqual(width, 80)

    def test_parse_channel_band_2ghz(self):
        ch, band, width = tuner._parse_channel_band("6 (2GHz, 20MHz)")
        self.assertEqual(ch, "6")
        self.assertEqual(band, "2GHz")
        self.assertEqual(width, 20)

    def test_parse_channel_band_6ghz(self):
        ch, band, width = tuner._parse_channel_band("37 (6GHz, 80MHz)")
        self.assertEqual(ch, "37")
        self.assertEqual(band, "6GHz")
        self.assertEqual(width, 80)

    def test_parse_channel_band_plain_int(self):
        ch, band, width = tuner._parse_channel_band("64")
        self.assertEqual(ch, "64")
        self.assertIsNone(band)
        self.assertEqual(width, 20)

    def test_parse_channel_band_empty(self):
        ch, band, width = tuner._parse_channel_band("")
        self.assertIsNone(ch)
        self.assertIsNone(band)
        self.assertEqual(width, 20)

    # --- _persist_sysctl ---

    @patch("subprocess.run")
    def test_persist_sysctl_new_key(self, mock_run):
        """New key appended when conf does not exist."""
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(returncode=0)
        tuner._persist_sysctl("net.inet.tcp.mssdflt", "1460")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertEqual(cmd[:3], ["sudo", "python3", "-c"])
        script = cmd[3]
        self.assertIn("net.inet.tcp.mssdflt", script)
        self.assertIn("1460", script)

    @patch("subprocess.run")
    def test_persist_sysctl_failure_warns(self, mock_run):
        """Non-zero return code from sudo python3 prints a warning (no exception)."""
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(returncode=1, stderr="permission denied")
        # Should not raise
        tuner._persist_sysctl("net.inet.tcp.mssdflt", "1460")

    # --- apply_sysctl_optimizations gaming flag ---

    @patch("subprocess.run")
    def test_apply_sysctl_gaming_true_darwin(self, mock_run):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        tuner.apply_sysctl_optimizations(gaming=True)
        sysctl_calls = [
            c for c in mock_run.call_args_list if c[0][0][:2] == ["sudo", "sysctl"]
        ]
        args_str = " ".join(str(c) for c in sysctl_calls)
        self.assertIn("delayed_ack=0", args_str)

    @patch("subprocess.run")
    def test_apply_sysctl_gaming_false_darwin(self, mock_run):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        tuner.apply_sysctl_optimizations(gaming=False)
        sysctl_calls = [
            c for c in mock_run.call_args_list if c[0][0][:2] == ["sudo", "sysctl"]
        ]
        args_str = " ".join(str(c) for c in sysctl_calls)
        self.assertNotIn("delayed_ack", args_str)

    # --- get_current_mtu ---

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run")
    def test_get_current_mtu_darwin(self, mock_run, mock_system):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(
            stdout="en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500\n",
            returncode=0,
        )
        mtu = tuner.get_current_mtu("en0")
        self.assertEqual(mtu, 1500)

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_get_current_mtu_linux(self, mock_run, mock_system):
        tuner.OS_NAME = "Linux"
        mock_run.return_value = MagicMock(
            stdout="3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1492 qdisc noqueue state UP\n",
            returncode=0,
        )
        mtu = tuner.get_current_mtu("wlan0")
        self.assertEqual(mtu, 1492)

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_get_current_mtu_windows(self, mock_run, mock_system):
        tuner.OS_NAME = "Windows"
        mock_run.return_value = MagicMock(
            stdout="   MTU  MediaSenseState   Bytes In  Bytes Out  Interface\n------  ---------------  ---------  ---------  -------------\n  1450                1    1000200     500100  Wi-Fi\n",
            returncode=0,
        )
        mtu = tuner.get_current_mtu("Wi-Fi")
        self.assertEqual(mtu, 1450)

    # --- save_backup & revert_optimizations ---

    @patch("tuner.BACKUP_PATH", "/tmp/wifituner_test_backup.json")
    @patch("tuner.get_dns_servers", return_value=["1.1.1.1"])
    @patch("tuner.get_current_mtu", return_value=1500)
    @patch("tuner.get_sysctl_value", return_value="3")
    @patch("builtins.open", new_callable=mock_open)
    @patch("os.path.exists", return_value=False)
    @patch("json.dump")
    def test_save_backup(
        self, mock_json_dump, mock_exists, mock_open, mock_sysctl, mock_mtu, mock_dns
    ):
        tuner.OS_NAME = "Darwin"
        tuner.save_backup("en0")
        mock_json_dump.assert_called_once()
        backup_data = mock_json_dump.call_args[0][0]
        self.assertEqual(backup_data["dns"], ["1.1.1.1"])
        self.assertEqual(backup_data["mtu"], 1500)
        self.assertEqual(backup_data["sysctl"]["net.inet.tcp.win_scale_factor"], "3")

    @patch("tuner.BACKUP_PATH", "/tmp/wifituner_test_backup.json")
    @patch("os.path.exists", return_value=True)
    @patch(
        "builtins.open",
        new_callable=mock_open,
        read_data='{"dns": ["1.1.1.1"], "mtu": 1500, "sysctl": {"net.inet.tcp.mssdflt": "512"}}',
    )
    @patch("tuner.get_macos_service_name", return_value="Wi-Fi")
    @patch("subprocess.run")
    @patch("os.remove")
    def test_revert_optimizations_darwin(
        self, mock_remove, mock_run, mock_service, mock_file, mock_exists
    ):
        tuner.OS_NAME = "Darwin"
        tuner.revert_optimizations("en0")
        mock_run.assert_any_call(
            ["sudo", "networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1"], check=True
        )
        mock_run.assert_any_call(["sudo", "ifconfig", "en0", "mtu", "1500"], check=True)
        mock_remove.assert_called_once_with("/tmp/wifituner_test_backup.json")

    @patch("platform.system", return_value="Darwin")
    @patch("subprocess.run")
    def test_get_wifi_interface_darwin(self, mock_run, mock_system):
        tuner.OS_NAME = "Darwin"
        mock_result = MagicMock()
        mock_result.stdout = "Hardware Port: Wi-Fi\nDevice: en0\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        self.assertEqual(tuner.get_wifi_interface(), "en0")

    @patch("platform.system", return_value="FreeBSD")
    @patch("subprocess.run")
    def test_get_wifi_interface_bsd(self, mock_run, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        mock_res1 = MagicMock()
        mock_res1.stdout = "wlan0\n"
        mock_res1.returncode = 0
        mock_res2 = MagicMock()
        mock_res2.stdout = "lo0 em0 wlan0\n"
        mock_res2.returncode = 0
        mock_run.side_effect = [mock_res1, mock_res2]
        self.assertEqual(tuner.get_wifi_interface(), "wlan0")

    @patch("platform.system", return_value="Darwin")
    @patch("tuner.get_macos_service_name", return_value="Wi-Fi")
    @patch("subprocess.run")
    def test_get_dns_servers_darwin(self, mock_run, mock_service, mock_system):
        tuner.OS_NAME = "Darwin"
        mock_result = MagicMock()
        mock_result.stdout = "1.1.1.1\n8.8.8.8\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        self.assertEqual(tuner.get_dns_servers("en0"), ["1.1.1.1", "8.8.8.8"])

    @patch("platform.system", return_value="Darwin")
    @patch("tuner.get_macos_service_name", return_value="Wi-Fi")
    @patch("subprocess.run")
    def test_apply_dns_darwin(self, mock_run, mock_service, mock_system):
        tuner.OS_NAME = "Darwin"
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(tuner.apply_dns("en0", "1.1.1.1"))
        mock_run.assert_any_call(
            ["sudo", "networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1"], check=True
        )

    @patch("platform.system", return_value="FreeBSD")
    @patch("subprocess.run")
    def test_apply_dns_bsd(self, mock_run, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(tuner.apply_dns("wlan0", "1.1.1.1"))
        mock_run.assert_called_with(
            ["sudo", "sh", "-c", "echo 'nameserver 1.1.1.1' > /etc/resolv.conf"],
            check=True,
        )

    @patch("platform.system", return_value="FreeBSD")
    @patch("subprocess.run")
    def test_apply_mtu_bsd(self, mock_run, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        mock_run.return_value = MagicMock(returncode=0)
        tuner.apply_mtu("wlan0", 1492)
        mock_run.assert_called_with(
            ["sudo", "ifconfig", "wlan0", "mtu", "1492"], check=True
        )

    @patch("subprocess.run")
    def test_get_bsd_wifi_details(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = "wlan0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST>\n\tether 00:11:22:33:44:55\n\tssid CUHomeWiFi channel 128 (5180 MHz 11a) bssid 00:11:22:33:44:55\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        details = tuner.get_bsd_wifi_details("wlan0")
        self.assertEqual(details["SSID"], "CUHomeWiFi")
        self.assertEqual(details["Channel"], "128")
        self.assertEqual(details["BSSID"], "00:11:22:33:44:55")

    @patch("platform.system", return_value="FreeBSD")
    @patch("subprocess.run")
    @patch("tuner._persist_sysctl")
    def test_apply_sysctl_optimizations_bsd(self, mock_persist, mock_run, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        mock_run.return_value = MagicMock(returncode=0)
        tuner.apply_sysctl_optimizations()
        mock_run.assert_any_call(
            ["sudo", "sysctl", "-w", "net.inet.tcp.mssdflt=1460"],
            check=True,
            capture_output=True,
        )

    @patch("platform.system", return_value="FreeBSD")
    @patch("builtins.open", new_callable=mock_open, read_data="nameserver 1.1.1.1\n")
    def test_verify_dns_bsd_present(self, mock_file, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        self.assertTrue(tuner.verify_dns("wlan0", "1.1.1.1"))

    def test_build_dns_query(self):
        query = tuner.build_dns_query("example.com")
        self.assertEqual(query[-5:], b"\x00\x00\x01\x00\x01")
        self.assertIn(b"example", query)
        self.assertIn(b"com", query)


if __name__ == "__main__":
    unittest.main()
