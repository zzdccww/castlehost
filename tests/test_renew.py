import unittest
import sys
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


class StartResponseTests(unittest.TestCase):
    def setUp(self):
        self.sb = Mock()
        self.sb.get_current_url.return_value = renew.BASE + "/servers"
        self.client = renew.CastleClient(self.sb, 0)
        for name in ("goto_servers", "wait_csrf_ready", "_observe_start", "_dispatch_action",
                     "_wait_start_response", "check_server_stopped", "toast_text", "captcha_gate_active"):
            patcher = patch.object(self.client, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.wait_csrf_ready.return_value = True
        self._observe_start.return_value = True
        self._dispatch_action.return_value = True
        self.toast_text.return_value = ""
        self.captcha_gate_active.return_value = False
        self.check_server_stopped.side_effect = [True, False]
        self._wait_start_response.return_value = {
            "http_status": 200, "status": "success", "message": "accepted",
        }
        sleep = patch.object(renew.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_site_rejection_is_reported_even_when_toast_is_empty(self):
        original = "Ошибка при запуске сервера. Пожалуйста, попробуйте позже!"
        self._wait_start_response.return_value = {
            "http_status": 200, "status": "error", "message": original,
        }
        status, message = self.client.ensure_running("12345")
        self.assertIs(status, renew.StartStatus.FAILED)
        self.assertIn(original, message)
        self.assertIn("站点启动接口", message)
        self.assertNotIn("CSRF", message)
        self.goto_servers.assert_not_called()

    def test_response_captcha_is_detected_without_modal(self):
        self._wait_start_response.return_value = {
            "http_status": 200, "status": "captcha_required", "captcha_required": True,
        }
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.CAPTCHA)

    def test_non_json_challenge_is_detected(self):
        self._wait_start_response.return_value = {"http_status": 403, "json": False, "challenge": True}
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.CAPTCHA)

    def test_accepted_request_waits_for_running_state_without_resending(self):
        self.check_server_stopped.side_effect = [True, True, True, False]
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.STARTED)
        self._dispatch_action.assert_called_once_with("12345", "start")
        self.assertEqual(self.goto_servers.call_count, 3)

    def test_success_response_does_not_hide_stopped_server(self):
        self.check_server_stopped.side_effect = None
        self.check_server_stopped.return_value = True
        status, message = self.client.ensure_running("12345")
        self.assertIs(status, renew.StartStatus.STOPPED)
        self.assertIn("已接受", message)
        self._dispatch_action.assert_called_once()
        self.assertEqual(self.goto_servers.call_count, renew.START_CONFIRM_ROUNDS)

    def test_missing_controls_does_not_confirm_running(self):
        self.check_server_stopped.side_effect = [True] + [None] * renew.START_CONFIRM_ROUNDS
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.UNKNOWN)

    def test_response_timeout_is_unconfirmed_and_only_checks_state_once(self):
        self._wait_start_response.return_value = None
        self.check_server_stopped.side_effect = [True, True]
        status, message = self.client.ensure_running("12345")
        self.assertIs(status, renew.StartStatus.STOPPED)
        self.assertIn("结果未确认", message)
        self.goto_servers.assert_called_once()

    def test_transport_error_is_not_treated_as_an_accepted_request(self):
        self._wait_start_response.return_value = {"http_status": 502, "status": "", "json": False}
        self.check_server_stopped.side_effect = [True, True]
        status, message = self.client.ensure_running("12345")
        self.assertIs(status, renew.StartStatus.STOPPED)
        self.assertIn("502", message)
        self.goto_servers.assert_called_once()

    def test_no_observer_means_no_start_request(self):
        self._observe_start.return_value = False
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.FAILED)
        self._dispatch_action.assert_not_called()

    def test_exception_after_dispatch_is_unconfirmed(self):
        self._dispatch_action.side_effect = ConnectionError("driver disconnected")
        self.assertIs(self.client.ensure_running("12345")[0], renew.StartStatus.STOPPED)
        self.sb.click.assert_not_called()

    def test_read_only_check_never_dispatches(self):
        self.assertIs(self.client.ensure_running("12345", allow_start=False)[0], renew.StartStatus.STOPPED)
        self._dispatch_action.assert_not_called()
        self._observe_start.assert_not_called()


class AccountStartVerificationTests(unittest.TestCase):
    def test_post_renewal_does_not_repeat_uncertain_start_and_preserves_start_record(self):
        cases = [
            (renew.StartStatus.STOPPED, renew.StartStatus.STOPPED, renew.StartStatus.STOPPED),
            (renew.StartStatus.STOPPED, renew.StartStatus.RUNNING, renew.StartStatus.STARTED),
            (renew.StartStatus.STARTED, renew.StartStatus.RUNNING, renew.StartStatus.STARTED),
            (renew.StartStatus.UNKNOWN, renew.StartStatus.UNKNOWN, renew.StartStatus.UNKNOWN),
        ]
        for pre, final, expected in cases:
            with self.subTest(pre=pre, final=final):
                fake_sb = Mock()
                fake_sb.return_value.__enter__ = Mock(return_value=Mock())
                fake_sb.return_value.__exit__ = Mock(return_value=False)
                with patch.dict(sys.modules, {"seleniumbase": SimpleNamespace(SB=fake_sb)}), \
                        patch.object(renew, "open_uc_page"), patch.object(renew, "CastleClient") as factory:
                    client = factory.return_value
                    client.get_server_ids.return_value = ["12345"]
                    client.ensure_running.side_effect = [(pre, "initial request result"), (final, "final")]
                    client.renew.return_value = (renew.RenewalStatus.SUCCESS, "renewed", "", "25.11.2026", 6)
                    client.extract_cookies.return_value = None
                    _, results = renew.process_account("CH_SESSION=fake; uid=fake", 0, Mock())
                    self.assertEqual(client.ensure_running.call_args_list[1].kwargs, {"allow_start": False})
                    self.assertIs(results[0].start, expected)
                    if expected is renew.StartStatus.STOPPED:
                        self.assertEqual(results[0].start_msg, "initial request result")


if __name__ == "__main__":
    unittest.main()
