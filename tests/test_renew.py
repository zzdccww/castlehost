import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

import renew


class UCNavigationTests(unittest.TestCase):
    def setUp(self):
        self.sb = Mock()
        type(self.sb.driver).window_handles = PropertyMock(return_value=["window"])

    def test_healthy_navigation_does_not_reconnect_again(self):
        renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.sb.uc_open_with_reconnect.assert_called_once()
        self.sb.driver.reconnect.assert_not_called()

    def test_navigation_connection_failure_recovers_once(self):
        self.sb.uc_open_with_reconnect.side_effect = [ConnectionError("refused"), None]
        renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.assertEqual(self.sb.uc_open_with_reconnect.call_count, 2)
        self.sb.driver.reconnect.assert_called_once_with(0.5)

    def test_silently_failed_reconnect_is_detected(self):
        type(self.sb.driver).window_handles = PropertyMock(
            side_effect=[ConnectionError("refused"), ["window"]]
        )
        renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.assertEqual(self.sb.uc_open_with_reconnect.call_count, 2)

    def test_persistent_failure_stops_after_one_retry(self):
        self.sb.uc_open_with_reconnect.side_effect = ConnectionError("refused")
        with self.assertRaises(renew.BrowserConnectionError):
            renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.assertEqual(self.sb.uc_open_with_reconnect.call_count, 2)

    def test_browser_without_windows_fails(self):
        type(self.sb.driver).window_handles = PropertyMock(return_value=[])
        with self.assertRaises(renew.BrowserConnectionError):
            renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.assertEqual(self.sb.uc_open_with_reconnect.call_count, 2)

    def test_driver_reconnect_failure_is_reported(self):
        self.sb.uc_open_with_reconnect.side_effect = ConnectionError("refused")
        self.sb.driver.reconnect.side_effect = RuntimeError("service exited")
        with self.assertRaisesRegex(renew.BrowserConnectionError, "service exited"):
            renew.open_uc_page(self.sb, renew.BASE + "/servers", renew.RECONNECT)
        self.sb.uc_open_with_reconnect.assert_called_once()

    def test_connection_failure_does_not_become_an_empty_server_list(self):
        client = renew.CastleClient(self.sb, 0)
        with patch.object(client, "goto_servers", side_effect=renew.BrowserConnectionError("refused")):
            with self.assertRaises(renew.BrowserConnectionError):
                client.get_server_ids()

    def test_expiry_connection_failure_is_not_hidden_by_toast_success(self):
        client = renew.CastleClient(self.sb, 0)
        with patch.object(renew, "open_uc_page", side_effect=renew.BrowserConnectionError("refused")):
            with self.assertRaises(renew.BrowserConnectionError):
                client._reload_expiry(renew.BASE + "/servers/pay/index/12345")

    def test_disconnected_driver_skips_screenshot(self):
        type(self.sb.driver).window_handles = PropertyMock(side_effect=ConnectionError("refused"))
        client = renew.CastleClient(self.sb, 0)
        self.assertEqual(client.take_screenshot("error", "exception"), "")
        self.sb.save_screenshot.assert_not_called()


class MainStatusTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            cookies_list=["CH_SESSION=old; uid=user"],
            tg_token=None, tg_chat_id=None, repo_token=None,
            repository=None, proxy=None,
        )
        for target in ("logger", "ensure_output_dir", "Notifier", "GitHubManager"):
            patcher = patch.object(renew, target)
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        config_patcher = patch.object(renew.Config, "from_env", return_value=self.config)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

    def result(self, status=renew.RenewalStatus.SUCCESS, start=renew.StartStatus.RUNNING):
        return renew.ServerResult("12345", status, "", start=start)

    def test_missing_credentials_fail(self):
        self.config.cookies_list = []
        with patch.object(renew, "process_account") as account:
            self.assertEqual(renew.main(), 1)
            account.assert_not_called()

    def test_driver_start_failure_fails(self):
        with patch.object(renew, "process_account", side_effect=RuntimeError("driver exited")):
            self.assertEqual(renew.main(), 1)

    def test_no_servers_or_expired_cookie_fails(self):
        with patch.object(renew, "process_account", return_value=(None, [])):
            self.assertEqual(renew.main(), 1)

    def test_empty_results_with_a_cookie_still_fail(self):
        with patch.object(renew, "process_account", return_value=(self.config.cookies_list[0], [])):
            self.assertEqual(renew.main(), 1)

    def test_failed_business_results_fail(self):
        cases = [
            self.result(status=renew.RenewalStatus.FAILED),
            self.result(status=renew.RenewalStatus.CAPTCHA_REQUIRED),
            self.result(start=renew.StartStatus.FAILED),
            self.result(start=renew.StartStatus.CAPTCHA),
            self.result(start=renew.StartStatus.UNKNOWN),
            self.result(start=renew.StartStatus.STOPPED),
        ]
        for result in cases:
            with self.subTest(status=result.status, start=result.start):
                with patch.object(renew, "process_account", return_value=(self.config.cookies_list[0], [result])):
                    self.assertEqual(renew.main(), 1)

    def test_success_and_daily_renewal_limit_succeed(self):
        for status in (renew.RenewalStatus.SUCCESS, renew.RenewalStatus.RATE_LIMITED):
            with self.subTest(status=status):
                with patch.object(renew, "process_account", return_value=(self.config.cookies_list[0], [self.result(status=status)])):
                    self.assertEqual(renew.main(), 0)

    def test_failure_preserves_its_cookie_and_does_not_stop_other_accounts(self):
        failed_cookie = self.config.cookies_list[0]
        self.config.cookies_list.append("CH_SESSION=second; uid=second")
        responses = [(None, []), ("CH_SESSION=rotated; uid=second", [self.result()])]
        with patch.object(renew, "time"), patch.object(renew, "process_account", side_effect=responses) as account:
            self.assertEqual(renew.main(), 1)
            self.assertEqual(account.call_count, 2)
        self.GitHubManager.return_value.update_secret.assert_called_once_with(
            "CASTLE_COOKIES", failed_cookie + ",CH_SESSION=rotated; uid=second"
        )


if __name__ == "__main__":
    unittest.main()
