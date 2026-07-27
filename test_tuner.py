import unittest
from unittest.mock import MagicMock, mock_open, patch

import tuner


class TestTunerMultiPlatform(unittest.TestCase):
    def setUp(self):
        tuner.get_macos_service_name.cache_clear()

    # --- platform.system() and admin status ---

    @patch("platform.system", return_value="Windows")
    def test_is_admin_windows(self, mock_system):
        tuner.OS_NAME = "Windows"
        mock_ctypes = MagicMock()
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            for val, expected in [(1, True), (0, False)]:
                with self.subTest(val=val):
                    mock_ctypes.windll.shell32.IsUserAnAdmin.return_value = val
                    self.assertEqual(tuner.is_admin(), expected)

    @patch("platform.system", return_value="Linux")
    def test_is_admin_linux(self, mock_system):
        tuner.OS_NAME = "Linux"
        for euid, expected in [(0, True), (1000, False)]:
            with self.subTest(euid=euid), patch("os.geteuid", return_value=euid):
                self.assertEqual(tuner.is_admin(), expected)

    @patch("platform.system", return_value="Darwin")
    @patch("tuner.is_admin", return_value=False)
    @patch("subprocess.run")
    def test_ensure_admin_prompts_sudo(self, mock_run, mock_is_admin, mock_system):
        tuner.OS_NAME = "Darwin"
        tuner.ensure_admin()
        mock_run.assert_called_once_with(["sudo", "-v"], check=True)

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
            ["ping", "-M", "do", "-s", "1472", "-c", "1", "-W", "1", "1.1.1.1"],
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
            ["ping", "-f", "-l", "1472", "-n", "1", "-w", "1000", "1.1.1.1"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )

    # --- apply configurations ---

    @patch("platform.system", return_value="Linux")
    @patch("tuner.verify_dns", return_value=False)
    @patch("subprocess.run")
    def test_apply_dns_linux_resolvectl(self, mock_run, mock_verify, mock_system):
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
    @patch("subprocess.run")
    def test_apply_sysctl_optimizations_windows(self, mock_run, mock_system):
        tuner.OS_NAME = "Windows"
        for is_adm in (True, False):
            with (
                self.subTest(is_adm=is_adm),
                patch("tuner.is_admin", return_value=is_adm),
            ):
                mock_run.reset_mock()
                mock_run.return_value = MagicMock(returncode=0)
                tuner.apply_sysctl_optimizations()
                if is_adm:
                    mock_run.assert_any_call(
                        [
                            "netsh",
                            "int",
                            "tcp",
                            "set",
                            "global",
                            "autotuninglevel=normal",
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    mock_run.assert_not_called()

    # --- verify_dns ---

    @patch("subprocess.run")
    def test_verify_dns_macos(self, mock_run):
        tuner.OS_NAME = "Darwin"
        for stdout, expected in [("1.1.1.1\n", True), ("8.8.8.8\n", False)]:
            with (
                self.subTest(expected=expected),
                patch("tuner.get_macos_service_name", return_value="Wi-Fi"),
            ):
                mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
                self.assertEqual(tuner.verify_dns("en0", "1.1.1.1"), expected)

    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_verify_dns_linux_resolv(self, mock_run):
        tuner.OS_NAME = "Linux"
        for content, expected in [
            ("nameserver 1.1.1.1\nnameserver 8.8.8.8\n", True),
            ("nameserver 8.8.8.8\n", False),
        ]:
            with (
                self.subTest(expected=expected),
                patch("builtins.open", new_callable=mock_open, read_data=content),
            ):
                self.assertEqual(tuner.verify_dns("wlan0", "1.1.1.1"), expected)

    @patch("subprocess.run")
    def test_verify_dns_linux_resolvectl(self, mock_run):
        tuner.OS_NAME = "Linux"
        for stdout, expected in [
            ("  DNS Servers: 1.1.1.1\n", True),
            ("  DNS Servers: 8.8.8.8\n", False),
        ]:
            with self.subTest(expected=expected):
                mock_run.return_value = MagicMock(returncode=0, stdout=stdout)
                self.assertEqual(tuner.verify_dns("wlan0", "1.1.1.1"), expected)

    @patch("subprocess.run", side_effect=FileNotFoundError)
    @patch("builtins.open", new_callable=mock_open, read_data="nameserver 1.1.1.1\n")
    def test_verify_dns_linux_fallback_resolv(self, mock_file, mock_run):
        """Fallback to /etc/resolv.conf when resolvectl not installed."""
        tuner.OS_NAME = "Linux"
        self.assertTrue(tuner.verify_dns("wlan0", "1.1.1.1"))

    # --- get_macos_wifi_details JSON path ---

    @patch("subprocess.run")
    def test_get_macos_wifi_details(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="SSID : TestNet\nSecurity : WPA2\n", returncode=0
        )
        details = tuner.get_macos_wifi_details("en0")
        self.assertEqual(details["SSID"], "TestNet")
        self.assertEqual(details["Security"], "WPA2")

    # --- _persist_sysctl_dict ---

    @patch("subprocess.run")
    def test_persist_sysctl_dict(self, mock_run):
        tuner.OS_NAME = "Darwin"
        for retcode in (0, 1):
            with self.subTest(retcode=retcode):
                mock_run.return_value = MagicMock(
                    returncode=retcode, stderr="permission denied"
                )
                tuner._persist_sysctl_dict({"net.inet.tcp.mssdflt": "1460"})

    # --- apply_sysctl_optimizations gaming flag ---

    @patch("subprocess.run")
    def test_apply_sysctl_gaming_darwin(self, mock_run):
        tuner.OS_NAME = "Darwin"
        for gaming in (True, False):
            with self.subTest(gaming=gaming):
                mock_run.reset_mock()
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                tuner.apply_sysctl_optimizations(gaming=gaming)
                sysctl_calls = [
                    c
                    for c in mock_run.call_args_list
                    if c[0][0][:2] == ["sudo", "sysctl"]
                ]
                args_str = " ".join(str(c) for c in sysctl_calls)
                self.assertEqual("delayed_ack=0" in args_str, gaming)

    # --- get_current_mtu ---

    @patch("subprocess.run")
    def test_get_current_mtu_platforms(self, mock_run):
        cases = [
            ("Darwin", "en0: flags=8863 mtu 1500\n", "en0", 1500),
            ("Linux", "3: wlan0: mtu 1492 qdisc noqueue\n", "wlan0", 1492),
            ("Windows", "  1450                1  Wi-Fi\n", "Wi-Fi", 1450),
        ]
        for os_name, stdout, iface, expected in cases:
            with (
                self.subTest(os_name=os_name),
                patch("platform.system", return_value=os_name),
            ):
                tuner.OS_NAME = os_name
                mock_run.return_value = MagicMock(stdout=stdout, returncode=0)
                self.assertEqual(tuner.get_current_mtu(iface), expected)

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
    @patch("tuner.verify_dns", return_value=False)
    @patch("subprocess.run")
    def test_apply_dns_bsd(self, mock_run, mock_verify, mock_system):
        tuner.OS_NAME = "FreeBSD"
        tuner.IS_BSD = True
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(tuner.apply_dns("wlan0", "1.1.1.1"))
        mock_run.assert_called_with(
            ["sudo", "sh", "-c", "printf 'nameserver 1.1.1.1\n' > /etc/resolv.conf"],
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
    @patch("tuner._persist_sysctl_dict")
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
