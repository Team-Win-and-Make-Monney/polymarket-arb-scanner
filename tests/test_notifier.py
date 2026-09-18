"""Tests for notifier.py — webhook notification system."""

import logging

import pytest
import requests
from unittest.mock import patch, MagicMock
from notifier import WebhookNotifier


class TestWebhookNotifier:
    def test_init_sets_url_and_min_profit(self):
        n = WebhookNotifier("https://example.com/hook", min_profit=0.05)
        assert n.url == "https://example.com/hook"
        assert n.min_profit == 0.05

    def test_default_min_profit(self):
        n = WebhookNotifier("https://example.com/hook")
        assert n.min_profit == 0.01

    def test_rejects_internal_url_even_with_uppercase_scheme(self):
        # SSRF guard must not be bypassed by a mixed-case scheme (audit follow-up).
        with pytest.raises(ValueError):
            WebhookNotifier("HTTP://127.0.0.1/hook")

    def test_notify_skips_when_no_qualifying_opportunities(self):
        n = WebhookNotifier("https://example.com/hook", min_profit=1.0)
        with patch.object(n, "_send") as mock_send:
            n.notify([{"net_profit": 0.01}])
            # _send should not be called via thread
            mock_send.assert_not_called()

    def test_notify_spawns_thread_for_qualifying_opportunities(self):
        n = WebhookNotifier("https://example.com/hook", min_profit=0.01)
        with patch("notifier.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            n.notify([{"net_profit": 0.05}])
            mock_thread.assert_called_once()
            mock_instance.start.assert_called_once()

    def test_notify_filters_below_min_profit(self):
        n = WebhookNotifier("https://example.com/hook", min_profit=0.03)
        with patch("notifier.threading.Thread") as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            n.notify([
                {"net_profit": 0.01},  # below threshold
                {"net_profit": 0.05},  # above threshold
            ])
            # Thread should be spawned (at least one qualifying)
            mock_thread.assert_called_once()

    def test_notify_empty_list_does_nothing(self):
        n = WebhookNotifier("https://example.com/hook")
        with patch("notifier.threading.Thread") as mock_thread:
            n.notify([])
            mock_thread.assert_not_called()


class TestBuildPayload:
    def test_slack_format(self):
        n = WebhookNotifier("https://hooks.slack.com/services/T00/B00/X00")
        opps = [{"type": "Binary", "market": "Test Market", "net_profit": 0.05, "net_roi": "5%", "prices": "Y=0.45 N=0.50", "_clob_depth": 100}]
        payload = n._build_payload(opps)
        assert "text" in payload
        assert "Binary" in payload["text"]
        assert "$0.0500" in payload["text"]

    def test_discord_format(self):
        n = WebhookNotifier("https://discord.com/api/webhooks/123/abc")
        opps = [{"type": "Cross", "market": "Test", "net_profit": 0.02, "net_roi": "2%", "prices": "PM_Y=0.40"}]
        payload = n._build_payload(opps)
        assert "content" in payload
        assert "Cross" in payload["content"]

    def test_generic_format(self):
        n = WebhookNotifier("https://example.com/hook")
        opps = [{"type": "Binary", "market": "Test", "net_profit": 0.03, "net_roi": "3%", "prices": "Y=0.45"}]
        payload = n._build_payload(opps)
        assert "opportunities" in payload
        assert "count" in payload
        assert payload["count"] == 1

    def test_multiple_opportunities(self):
        n = WebhookNotifier("https://example.com/hook")
        opps = [
            {"type": "Binary", "market": "Test1", "net_profit": 0.05, "net_roi": "5%", "prices": "Y=0.45"},
            {"type": "Cross", "market": "Test2", "net_profit": 0.03, "net_roi": "3%", "prices": "PM_Y=0.40"},
        ]
        payload = n._build_payload(opps)
        assert payload["count"] == 2
        assert len(payload["opportunities"]) == 2


class TestSend:
    def test_send_success(self):
        n = WebhookNotifier("https://example.com/hook")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        n._session = MagicMock()
        n._session.post.return_value = mock_resp
        n._send([{"type": "Binary", "market": "T", "net_profit": 0.05, "net_roi": "5%", "prices": "Y=0.45"}])
        n._session.post.assert_called_once()

    def test_send_handles_http_error(self):
        n = WebhookNotifier("https://example.com/hook")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        n._session = MagicMock()
        n._session.post.return_value = mock_resp
        # Should not raise
        n._send([{"type": "Binary", "market": "T", "net_profit": 0.05, "net_roi": "5%", "prices": "Y=0.45"}])

    def test_send_handles_request_exception(self):
        import requests
        n = WebhookNotifier("https://example.com/hook")
        n._session = MagicMock()
        n._session.post.side_effect = requests.RequestException("timeout")
        # Should not raise
        n._send([{"type": "Binary", "market": "T", "net_profit": 0.05, "net_roi": "5%", "prices": "Y=0.45"}])


class TestNotifyText:
    """notify_text() is the synchronous alert channel the EDGAR cron uses."""

    def test_telegram_routes_synchronously(self):
        n = WebhookNotifier("telegram")
        with patch.object(n, "_send_telegram") as send, \
                patch("notifier.threading.Thread") as thread:
            n.notify_text("hello world")
        send.assert_called_once_with("hello world")
        thread.assert_not_called()  # synchronous — must flush before a cron exits

    def test_empty_message_is_noop(self):
        n = WebhookNotifier("telegram")
        with patch.object(n, "_send_telegram") as send:
            n.notify_text("")
        send.assert_not_called()

    def test_generic_webhook_wraps_message(self):
        n = WebhookNotifier("https://example.com/hook")
        with patch.object(n, "_send_raw") as raw:
            n.notify_text("alert text")
        raw.assert_called_once_with({"message": "alert text"})

    def test_slack_payload(self):
        n = WebhookNotifier("https://hooks.slack.com/services/T/B/X")
        with patch.object(n, "_send_raw") as raw:
            n.notify_text("alert text")
        raw.assert_called_once_with({"text": "alert text"})

    def test_discord_payload(self):
        n = WebhookNotifier("https://discord.com/api/webhooks/1/abc")
        with patch.object(n, "_send_raw") as raw:
            n.notify_text("alert text")
        raw.assert_called_once_with({"content": "alert text"})


class TestSecretRedaction:
    """A transient network failure must never leak the Telegram bot token or
    CallMeBot API key / phone number into logs. requests exceptions
    (ConnectionError, Timeout, ...) commonly embed the full request URL —
    which embeds these secrets in the path/query string — in their str()."""

    def test_telegram_connection_error_does_not_leak_token(self, caplog):
        n = WebhookNotifier("telegram")
        n._telegram_token = "123456:XXXXPLACEHOLDERXXXXNOTAREALXXXXTOKEN"
        n._telegram_chat_id = "999"
        n._session = MagicMock()
        n._session.post.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries "
            "exceeded with url: /bot123456:XXXXPLACEHOLDERXXXXNOTAREALXXXXTOKEN/"
            "sendMessage (Caused by NewConnectionError('...'))"
        )
        with caplog.at_level(logging.WARNING):
            n._send_telegram("test message")
        assert "XXXXPLACEHOLDERXXXXNOTAREALXXXXTOKEN" not in caplog.text

    def test_telegram_non_200_response_does_not_leak_token_in_echoed_url(self, caplog):
        n = WebhookNotifier("telegram")
        n._telegram_token = "123456:PLACEHOLDERNOTREALXXTOKEN"
        n._telegram_chat_id = "999"
        n._session = MagicMock()
        resp = MagicMock()
        resp.status_code = 404
        # Some proxies/WAFs echo the full attempted URL in a generic error page.
        resp.text = "Not Found: /bot123456:PLACEHOLDERNOTREALXXTOKEN/sendMessage"
        n._session.post.return_value = resp
        with caplog.at_level(logging.WARNING):
            n._send_telegram("test message")
        assert "PLACEHOLDERNOTREALXXTOKEN" not in caplog.text

    def test_telegram_redacts_before_response_truncation(self, caplog):
        n = WebhookNotifier("telegram")
        token = "123456:BOUNDARYPLACEHOLDERNOTREALTOKEN"
        n._telegram_token = token
        n._telegram_chat_id = "999"
        n._session = MagicMock()
        resp = MagicMock(status_code=404)
        resp.text = "x" * 180 + f"/bot{token}/sendMessage"
        n._session.post.return_value = resp
        with caplog.at_level(logging.WARNING):
            n._send_telegram("test message")
        assert "BOUNDARYPLACEHOLDER" not in caplog.text

    def test_callmebot_connection_error_does_not_leak_apikey_or_phone(self, caplog):
        n = WebhookNotifier("callmebot")
        n._callmebot_phone = "15551234567"
        n._callmebot_apikey = "placeholdernotrealapikeyxx"
        n._session = MagicMock()
        n._session.get.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='api.callmebot.com', port=443): Max retries "
            "exceeded with url: /whatsapp.php?phone=15551234567&text=hi&apikey="
            "placeholdernotrealapikeyxx (Caused by NewConnectionError('...'))"
        )
        with caplog.at_level(logging.WARNING):
            n._send_callmebot("hi")
        assert "placeholdernotrealapikeyxx" not in caplog.text
        assert "15551234567" not in caplog.text

    def test_redaction_preserves_unrelated_text(self):
        from notifier import _redact_secrets
        msg = _redact_secrets("plain error with no secrets in it")
        assert msg == "plain error with no secrets in it"

    def test_redaction_handles_empty_and_none(self):
        from notifier import _redact_secrets
        assert _redact_secrets("") == ""
        assert _redact_secrets(None) is None
