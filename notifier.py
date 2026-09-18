"""Webhook notifier for arbitrage opportunities.

Supports Telegram, Slack, Discord, CallMeBot WhatsApp, and generic webhooks.
"""

import json
import logging
import os
import re
import threading
from urllib.parse import quote as urlquote

import requests

from url_guard import assert_public_url

logger = logging.getLogger(__name__)

# requests exceptions (ConnectionError, Timeout, ...) — and some proxy/WAF
# error pages — commonly embed the full request URL in their text. For
# Telegram and CallMeBot that URL carries a live bot token / API key / phone
# number in the path or query string, so any code that logs str(exc) or
# resp.text verbatim on failure can leak a credential into build/CI logs.
# Every log call on these paths goes through _redact_secrets first.
_TELEGRAM_TOKEN_RE = re.compile(r"(/bot)[^/\s]+(/)")
_QUERY_SECRET_RE = re.compile(r"((?:apikey|phone)=)[^&\s'\"]+", re.IGNORECASE)


def _redact_secrets(text: str | None) -> str | None:
    """Strip Telegram bot tokens / CallMeBot apikey+phone from log-bound text."""
    if not text:
        return text
    text = _TELEGRAM_TOKEN_RE.sub(r"\1***REDACTED***\2", text)
    text = _QUERY_SECRET_RE.sub(r"\1***REDACTED***", text)
    return text


class WebhookNotifier:
    """Send opportunity alerts via Telegram, Slack, Discord, CallMeBot, or generic webhook."""

    def __init__(self, url: str, min_profit: float = 0.01):
        """
        Args:
            url: Webhook URL to POST JSON payloads to.  Special sentinel values:
                - "telegram" or "telegram://": uses TELEGRAM_BOT_TOKEN and
                  TELEGRAM_CHAT_ID env vars.
                - "callmebot" or "callmebot://": uses CALLMEBOT_PHONE and
                  CALLMEBOT_APIKEY env vars.
            min_profit: Only notify for opportunities with net_profit >= this value.
        """
        self.url = url
        self.min_profit = min_profit
        self._session = requests.Session()
        self._session.headers["Content-Type"] = "application/json"

        # Telegram support
        self._telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self._telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self._is_telegram = url.startswith("telegram")
        if self._is_telegram and (not self._telegram_token or not self._telegram_chat_id):
            logger.warning("WEBHOOK_URL set to telegram but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")

        # CallMeBot WhatsApp support
        self._callmebot_phone = os.getenv("CALLMEBOT_PHONE")
        self._callmebot_apikey = os.getenv("CALLMEBOT_APIKEY")
        self._is_callmebot = url.startswith("callmebot")
        if self._is_callmebot and (not self._callmebot_phone or not self._callmebot_apikey):
            logger.warning("WEBHOOK_URL set to callmebot but CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set.")

        # SSRF guard: a generic webhook URL (WEBHOOK_URL env) is POSTed the full
        # opportunity payload — refuse hosts that resolve to internal addresses.
        # Validate any non-sentinel URL so a mixed-case scheme (HTTP://…) or a
        # padded value can't skip the check; assert_public_url enforces the
        # scheme and strips whitespace.
        if self.url and not self._is_telegram and not self._is_callmebot:
            self.url = assert_public_url(self.url, env_name="WEBHOOK_URL")

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def notify(self, opportunities: list[dict]):
        """Send notification for qualifying opportunities (async, non-blocking)."""
        qualifying = [o for o in opportunities if o.get("net_profit", 0) >= self.min_profit]
        if not qualifying:
            return

        thread = threading.Thread(target=self._send, args=(qualifying,), daemon=True)
        thread.start()

    def notify_promo_warning(self, platform: str, days_remaining: int,
                             expiry_iso: str = ""):
        """Send a fee-promo expiration warning (Strategy #9 calendar tracking).

        Fired daily by ``continuous.py`` when ``config.get_promo_expiry``
        returns a date within ``PROMO_WARNING_DAYS``.

        Args:
            platform: Platform whose promo is expiring (matchbook, gemini, polymarket).
            days_remaining: Whole days until the promo ends. Negative if already past.
            expiry_iso: ISO date string for the expiry, included in the message.
        """
        if not self.url:
            return
        msg = (
            f"PROMO EXPIRY: {platform} fee promo ends in {days_remaining} day(s)"
            + (f" ({expiry_iso})" if expiry_iso else "")
            + " -- update fee env vars or rotate strategy mix."
        )
        if self._is_telegram:
            thread = threading.Thread(
                target=self._send_telegram, args=(f"⚠️ {msg}",), daemon=True)
            thread.start()
        elif self._is_callmebot:
            thread = threading.Thread(
                target=self._send_callmebot, args=(msg,), daemon=True)
            thread.start()
        else:
            payload: dict = {
                "event": "promo_warning",
                "platform": platform,
                "days_remaining": days_remaining,
                "expiry": expiry_iso,
                "message": msg,
            }
            if "hooks.slack.com" in self.url:
                payload = {"text": f":warning: {msg}"}
            elif "discord.com/api/webhooks" in self.url:
                payload = {"content": f"⚠️ {msg}"}
            thread = threading.Thread(target=self._send_raw, args=(payload,), daemon=True)
            thread.start()

    def notify_partial_fill(self, trade_id: int, platform: str, market: str,
                            fill_price: float, status: str):
        """Send urgent notification about a partial fill event."""
        if not self.url:
            return
        msg = f"PARTIAL FILL #{trade_id} on {platform}: {market} @ ${fill_price:.3f} -- {status}"
        if self._is_telegram:
            thread = threading.Thread(
                target=self._send_telegram, args=(f"\u26a0\ufe0f {msg}",), daemon=True)
            thread.start()
        elif self._is_callmebot:
            thread = threading.Thread(
                target=self._send_callmebot, args=(msg,), daemon=True)
            thread.start()
        else:
            payload = self._build_partial_fill_payload(trade_id, platform, market,
                                                       fill_price, status)
            thread = threading.Thread(target=self._send_raw, args=(payload,), daemon=True)
            thread.start()

    def notify_text(self, message: str) -> bool:
        """Send a pre-formatted alert string, synchronously.

        Unlike notify()/notify_promo_warning()/notify_partial_fill(), this blocks
        until the send completes. Detection-core watchers (EDGAR, etc.) render
        their own ticket text via the core's formatter and run as short-lived
        crons — a daemon thread might not flush before the process exits.

        Returns True only when a configured channel accepted the message —
        callers that must retry (e.g. the paper-window completion alert) gate
        on this instead of assuming fire-and-forget succeeded.
        """
        if not message:
            return False
        if self._is_telegram:
            return self._send_telegram(message)
        if self._is_callmebot:
            return self._send_callmebot(message)
        if self.url:
            if "hooks.slack.com" in self.url:
                return self._send_raw({"text": message})
            if "discord.com/api/webhooks" in self.url:
                return self._send_raw({"content": message})
            return self._send_raw({"message": message})
        return False

    # ---------------------------------------------------------------------------
    # Internal dispatch
    # ---------------------------------------------------------------------------

    def _send(self, opportunities: list[dict]):
        """POST opportunity data to the configured destination."""
        try:
            if self._is_telegram:
                self._send_telegram(self._format_text(opportunities))
                return
            if self._is_callmebot:
                self._send_callmebot(self._format_text(opportunities))
                return
            payload = self._build_payload(opportunities)
            resp = self._session.post(self.url, json=payload, timeout=10)
            if resp.status_code < 300:
                logger.debug("Webhook sent: %d opportunities", len(opportunities))
            else:
                logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            logger.warning("Webhook request failed: %s", e)

    def _send_raw(self, payload: dict) -> bool:
        """POST a raw payload to the webhook URL. Returns delivery success."""
        try:
            if self._is_telegram:
                msg = payload.get("text") or payload.get("content") or json.dumps(payload)
                return self._send_telegram(msg)
            if self._is_callmebot:
                msg = payload.get("text") or payload.get("content") or json.dumps(payload)
                return self._send_callmebot(msg)
            resp = self._session.post(self.url, json=payload, timeout=10)
            if resp.status_code >= 300:
                logger.warning("Webhook returned %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except requests.RequestException as e:
            logger.warning("Webhook request failed: %s", e)
            return False

    # ---------------------------------------------------------------------------
    # Telegram Bot API
    # ---------------------------------------------------------------------------

    def _send_telegram(self, text: str) -> bool:
        """Send a message via the Telegram Bot API. Returns delivery success."""
        if not self._telegram_token or not self._telegram_chat_id:
            logger.warning("Telegram not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
            return False
        url = f"https://api.telegram.org/bot{self._telegram_token}/sendMessage"
        payload = {
            "chat_id": self._telegram_chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            if resp.status_code < 300:
                logger.debug("Telegram sent: %s", text[:60])
                return True
            logger.warning("Telegram returned %d: %s", resp.status_code, _redact_secrets(resp.text)[:200])
            return False
        except requests.RequestException as e:
            logger.warning("Telegram request failed: %s", _redact_secrets(str(e)))
            return False

    # ---------------------------------------------------------------------------
    # CallMeBot WhatsApp
    # ---------------------------------------------------------------------------

    def _send_callmebot(self, text: str) -> bool:
        """Send via CallMeBot WhatsApp API. Returns delivery success."""
        if not self._callmebot_phone or not self._callmebot_apikey:
            logger.warning("CallMeBot not configured: missing CALLMEBOT_PHONE or CALLMEBOT_APIKEY")
            return False
        url = (
            f"https://api.callmebot.com/whatsapp.php"
            f"?phone={urlquote(self._callmebot_phone)}"
            f"&text={urlquote(text)}"
            f"&apikey={urlquote(self._callmebot_apikey)}"
        )
        try:
            resp = self._session.get(url, timeout=15)
            if resp.status_code < 300:
                logger.debug("CallMeBot WhatsApp sent: %s", text[:60])
                return True
            logger.warning("CallMeBot returned %d: %s", resp.status_code, _redact_secrets(resp.text[:200]))
            return False
        except requests.RequestException as e:
            logger.warning("CallMeBot request failed: %s", _redact_secrets(str(e)))
            return False

    # ---------------------------------------------------------------------------
    # Formatting helpers
    # ---------------------------------------------------------------------------

    def _format_text(self, opportunities: list[dict]) -> str:
        """Format opportunities as plain text for Telegram / WhatsApp / text notifications."""
        lines = [f"*Arb Scanner*: {len(opportunities)} opportunity(s)"]
        for opp in opportunities:
            profit = opp.get("net_profit", 0)
            roi = opp.get("net_roi", "")
            market = opp.get("market", "")
            # Truncate long market names for readability
            if len(market) > 50:
                market = market[:47] + "..."
            lines.append(
                f"  `{opp.get('type', '')}` {market}\n"
                f"  Profit: ${profit:.4f} | ROI: {roi} | Depth: ${opp.get('_clob_depth', 0):.0f}"
            )
        return "\n".join(lines)

    def _build_payload(self, opportunities: list[dict]) -> dict:
        """Build the webhook payload, auto-detecting Slack/Discord format."""
        items = []
        for opp in opportunities:
            items.append({
                "type": opp.get("type", ""),
                "market": opp.get("market", ""),
                "net_profit": f"${opp.get('net_profit', 0):.4f}",
                "net_roi": opp.get("net_roi", ""),
                "prices": opp.get("prices", ""),
                "depth": opp.get("_clob_depth", 0),
            })

        # Detect Slack or Discord by URL pattern
        if "hooks.slack.com" in self.url:
            lines = [f"*{o['type']}* {o['market']}: {o['net_profit']} ({o['net_roi']})" for o in items]
            return {"text": f"Arb Scanner: {len(items)} opportunities\n" + "\n".join(lines)}
        elif "discord.com/api/webhooks" in self.url:
            lines = [f"**{o['type']}** {o['market']}: {o['net_profit']} ({o['net_roi']})" for o in items]
            return {"content": f"Arb Scanner: {len(items)} opportunities\n" + "\n".join(lines)}
        else:
            return {"opportunities": items, "count": len(items)}

    def _build_partial_fill_payload(self, trade_id: int, platform: str, market: str,
                                    fill_price: float, status: str) -> dict:
        """Build partial fill alert payload for Slack/Discord/generic."""
        msg = f"PARTIAL FILL #{trade_id} on {platform}: {market} @ ${fill_price:.3f} -- {status}"
        if "hooks.slack.com" in self.url:
            return {"text": f":warning: {msg}"}
        elif "discord.com/api/webhooks" in self.url:
            return {"content": f"\u26a0\ufe0f {msg}"}
        else:
            return {"event": "partial_fill", "trade_id": trade_id, "platform": platform,
                    "market": market, "fill_price": fill_price, "status": status}
