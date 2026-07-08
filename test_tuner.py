import unittest
from unittest.mock import patch, MagicMock, mock_open
import sys
import tuner

class TestTunerMultiPlatform(unittest.TestCase):

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
        mock_run.assert_called_with(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, check=True)

    @patch("platform.system", return_value="Linux")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open, read_data="Inter-| sta-|   Quality        | Discarded packets\n face | tus | link level noise |  nwid  crypt   frag  retry   misc\n wlan1: 0000   45.  -65.  -256.        0      0      0      0      0")
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

    # --- get_dns_servers ---

    @patch("platform.system", return_value="Linux")
    @patch("builtins.open", new_callable=mock_open, read_data="nameserver 1.1.1.1\nnameserver 8.8.8.8")
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
        mock_run.assert_any_call(["ping", "-M", "do", "-s", "1472", "-c", "1", "-W", "1", "1.1.1.1"], capture_output=True, text=True, timeout=2.0)

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
        mock_run.assert_any_call(["ping", "-f", "-l", "1472", "-n", "1", "-w", "1000", "1.1.1.1"], capture_output=True, text=True, timeout=2.0)

    # --- scan_neighbor_channels ---

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_scan_neighbor_channels_linux(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "CHAN\n1\n6\n1\n36\n"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Linux"
        channels = tuner.scan_neighbor_channels()
        self.assertEqual(channels, {"1": 2, "6": 1, "36": 1})

    @patch("platform.system", return_value="Windows")
    @patch("subprocess.run")
    def test_scan_neighbor_channels_windows(self, mock_run, mock_system):
        mock_result = MagicMock()
        mock_result.stdout = "BSSID 1\n  Channel: 6\nBSSID 2\n  Channel: 11\nBSSID 3\n  Channel: 6"
        mock_result.returncode = 0
        mock_run.return_value = mock_result
        tuner.OS_NAME = "Windows"
        channels = tuner.scan_neighbor_channels()
        self.assertEqual(channels, {"6": 2, "11": 1})

    # --- apply configurations ---

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_apply_dns_linux_resolvectl(self, mock_run, mock_system):
        tuner.OS_NAME = "Linux"
        # First call checks version of resolvectl, second applies DNS
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        res = tuner.apply_dns("wlan0", "1.1.1.1")
        self.assertTrue(res)
        mock_run.assert_any_call(["sudo", "resolvectl", "dns", "wlan0", "1.1.1.1"], check=True)

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=True)
    @patch("subprocess.run")
    def test_apply_mtu_windows(self, mock_run, mock_is_admin, mock_system):
        tuner.OS_NAME = "Windows"
        mock_run.return_value = MagicMock(returncode=0)
        res = tuner.apply_mtu("Wi-Fi", 1500)
        self.assertTrue(res)
        mock_run.assert_called_with(["netsh", "interface", "ipv4", "set", "subinterface", "Wi-Fi", "mtu=1500", "store=persistent"], check=True)

    @patch("platform.system", return_value="Linux")
    @patch("subprocess.run")
    def test_flush_dns_cache_linux(self, mock_run, mock_system):
        tuner.OS_NAME = "Linux"
        mock_run.return_value = MagicMock(returncode=0)
        tuner.flush_dns_cache()
        mock_run.assert_any_call(["sudo", "resolvectl", "flush-caches"], check=True)

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=True)
    @patch("subprocess.run")
    def test_apply_sysctl_optimizations_windows_admin(self, mock_run, mock_is_admin, mock_system):
        tuner.OS_NAME = "Windows"
        mock_run.return_value = MagicMock(returncode=0)
        tuner.apply_sysctl_optimizations()
        mock_run.assert_any_call(["netsh", "int", "tcp", "set", "global", "autotuninglevel=normal"], check=True, capture_output=True)
        mock_run.assert_any_call(["netsh", "int", "tcp", "set", "global", "rss=enabled"], check=True, capture_output=True)

    @patch("platform.system", return_value="Windows")
    @patch("tuner.is_admin", return_value=False)
    @patch("subprocess.run")
    def test_apply_sysctl_optimizations_windows_non_admin(self, mock_run, mock_is_admin, mock_system):
        tuner.OS_NAME = "Windows"
        tuner.apply_sysctl_optimizations()
        mock_run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
