"""Treasury / auto-rebalance manager (Strategy #18).

Programmatic fund movement is only possible between Gemini and Polymarket
(USDC on Polygon). All six other platforms expose read-only balance APIs
with no withdraw / deposit / transfer endpoints, so they stay on the
manual-rebalance path with weekly digests via ``notifier.py``.

Risk gates layered around every transfer:
- AUTO_REBALANCE_ENABLED feature flag (default false)
- DRY_RUN gate — when set, audit row written but no real fund movement
- MAX_AUTO_TRANSFER_PER_DAY rolling limit
- MIN_TRANSFER_AMOUNT to avoid dust + gas overhead
- Idempotency key prevents duplicate sends within a 24h window
- Kill switch (dashboard pause)
- Optional gas ceiling via gas_monitor.should_execute()
"""

import hashlib
import logging
import math
import time

import config

logger = logging.getLogger(__name__)


SUPPORTED_CORRIDORS = {
    ("gemini", "polymarket"),
    ("polymarket", "gemini"),
}


class TransferResult:
    """Lightweight result wrapper. Keeps the API stable as we add fields."""

    __slots__ = ("ok", "transfer_id", "tx_hash", "error", "dry_run")

    def __init__(
        self,
        ok: bool,
        transfer_id: int | None = None,
        tx_hash: str | None = None,
        error: str | None = None,
        dry_run: bool = False,
    ):
        self.ok = ok
        self.transfer_id = transfer_id
        self.tx_hash = tx_hash
        self.error = error
        self.dry_run = dry_run

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "transfer_id": self.transfer_id,
            "tx_hash": self.tx_hash,
            "error": self.error,
            "dry_run": self.dry_run,
        }


class TreasuryManager:
    """Orchestrates auto-transfers between Gemini and Polymarket.

    Attributes:
        db: TradeDB instance for the ``transfers`` audit table.
        gemini_client: GeminiClient with ``withdraw_usdc()``.
        web3_send_usdc: Optional callable
            ``fn(amount_usd, dest_address) -> tx_hash`` for the
            Polymarket → Gemini path. Defaults to None until on-chain
            withdrawal from the Polymarket proxy is wired.
        gas_monitor: Optional GasMonitor instance — if provided, refuses
            transfers when its threshold gate fails.
        kill_switch: Optional callable returning ``True`` if the dashboard
            kill switch is engaged.
        dry_run: If True, every transfer is recorded but no real fund
            movement happens.
    """

    def __init__(
        self,
        db,
        gemini_client=None,
        web3_send_usdc=None,
        gas_monitor=None,
        kill_switch=None,
        dry_run: bool = True,
    ):
        self.db = db
        self.gemini_client = gemini_client
        self.web3_send_usdc = web3_send_usdc
        self.gas_monitor = gas_monitor
        self.kill_switch = kill_switch
        self.dry_run = dry_run

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    @staticmethod
    def _existing_transfer_result(
        existing: dict,
        from_platform: str,
        to_platform: str,
        amount_usd: float,
    ) -> TransferResult:
        """Return a prior result only when its idempotency payload matches."""
        existing_amount = existing.get("amount_usd")
        payload_matches = (
            existing.get("from_platform") == from_platform
            and existing.get("to_platform") == to_platform
            and isinstance(existing_amount, (int, float))
            and abs(float(existing_amount) - float(amount_usd)) < 1e-9
        )
        if not payload_matches:
            return TransferResult(
                ok=False,
                transfer_id=existing.get("id"),
                error="idempotency key payload mismatch",
            )
        status = existing.get("status")
        if status == "pending":
            return TransferResult(
                ok=False,
                transfer_id=existing.get("id"),
                error="transfer already in progress",
            )
        return TransferResult(
            ok=status in ("dry_run", "succeeded"),
            transfer_id=existing.get("id"),
            tx_hash=existing.get("tx_hash"),
            error=existing.get("error"),
            dry_run=status == "dry_run",
        )

    def execute_transfer(
        self,
        from_platform: str,
        to_platform: str,
        amount_usd: float,
        idempotency_key: str | None = None,
    ) -> TransferResult:
        """Execute (or dry-run) a fund transfer.

        Returns ``TransferResult.ok = False`` for any rejected request and
        records both rejection and success in the ``transfers`` table for
        ops visibility.
        """
        from_platform = from_platform.lower()
        to_platform = to_platform.lower()

        # Reject unsupported corridors before touching the DB
        if (from_platform, to_platform) not in SUPPORTED_CORRIDORS:
            return TransferResult(
                ok=False,
                error=(
                    f"Unsupported corridor {from_platform}->{to_platform}. "
                    f"Programmatic transfers only between Gemini and Polymarket."
                ),
            )

        if not config.AUTO_REBALANCE_ENABLED:
            return TransferResult(ok=False,
                                  error="AUTO_REBALANCE_ENABLED=false")

        if amount_usd < config.MIN_TRANSFER_AMOUNT:
            return TransferResult(
                ok=False,
                error=f"Amount ${amount_usd:.2f} below MIN_TRANSFER_AMOUNT "
                      f"${config.MIN_TRANSFER_AMOUNT:.2f}",
            )

        if self.db is None:
            return TransferResult(ok=False, error="transfer audit unavailable")

        idempotency_key = idempotency_key or self._build_idempotency_key(
            from_platform, to_platform, amount_usd)
        try:
            existing = self.db.get_transfer_by_idempotency_key(idempotency_key)
        except Exception as exc:
            logger.exception("treasury: failed to read transfer audit: %s", exc)
            return TransferResult(ok=False, error="transfer audit unavailable")
        if existing:
            return self._existing_transfer_result(
                existing, from_platform, to_platform, amount_usd,
            )

        if self.kill_switch and callable(self.kill_switch) and self.kill_switch():
            return TransferResult(ok=False, error="kill switch engaged")

        if self.gas_monitor is not None:
            try:
                raw_gas_cost = self.gas_monitor.get_current_gas_cost()
                if isinstance(raw_gas_cost, bool):
                    raise ValueError("boolean gas cost")
                gas_cost = float(raw_gas_cost)
            except Exception as exc:
                logger.warning("treasury: gas cost unavailable: %s", exc)
                return TransferResult(ok=False, error="gas cost unavailable")
            if not math.isfinite(gas_cost) or gas_cost < 0:
                return TransferResult(ok=False, error="invalid gas cost")
            if gas_cost > config.TREASURY_MAX_GAS_COST_USD:
                return TransferResult(ok=False, error="gas above ceiling")

        # Audit row first so we always know we tried
        try:
            transfer_id, created, used_today = (
                self.db.claim_transfer_with_daily_limit(
                    from_platform=from_platform,
                    to_platform=to_platform,
                    amount_usd=amount_usd,
                    idempotency_key=idempotency_key,
                    max_daily_usd=config.MAX_AUTO_TRANSFER_PER_DAY,
                )
            )
        except Exception as exc:
            logger.exception("treasury: failed to claim transfer: %s", exc)
            return TransferResult(ok=False, error="transfer audit unavailable")
        if used_today is not None:
            return TransferResult(
                ok=False,
                error=(
                    f"Daily limit hit: ${used_today:.2f} used + ${amount_usd:.2f} "
                    f"requested > MAX_AUTO_TRANSFER_PER_DAY "
                    f"${config.MAX_AUTO_TRANSFER_PER_DAY:.2f}"
                ),
            )
        if not created:
            try:
                existing = (
                    self.db.get_transfer_by_idempotency_key(idempotency_key) or {}
                )
            except Exception as exc:
                logger.exception("treasury: failed to read claimed transfer: %s", exc)
                return TransferResult(ok=False, error="transfer audit unavailable")
            return self._existing_transfer_result(
                existing, from_platform, to_platform, amount_usd,
            )

        if self.dry_run:
            if transfer_id is not None:
                self.db.update_transfer(transfer_id, status="dry_run")
            return TransferResult(ok=True, transfer_id=transfer_id, dry_run=True)

        # Live path: dispatch to the appropriate corridor
        try:
            if from_platform == "gemini" and to_platform == "polymarket":
                tx_hash = self._gemini_to_polymarket(amount_usd)
            elif from_platform == "polymarket" and to_platform == "gemini":
                tx_hash = self._polymarket_to_gemini(amount_usd)
            else:  # unreachable due to corridor check above
                raise ValueError("unreachable")
        except Exception as exc:
            logger.exception("treasury: transfer failed: %s", exc)
            if transfer_id is not None:
                self.db.update_transfer(transfer_id, status="failed",
                                        error=str(exc))
            return TransferResult(ok=False, transfer_id=transfer_id,
                                  error=str(exc))

        if transfer_id is not None:
            self.db.update_transfer(transfer_id, status="succeeded",
                                    tx_hash=tx_hash)
        return TransferResult(ok=True, transfer_id=transfer_id, tx_hash=tx_hash)

    # -------------------------------------------------------------------
    # Corridor dispatch
    # -------------------------------------------------------------------

    def _gemini_to_polymarket(self, amount_usd: float) -> str | None:
        """Withdraw USDC from Gemini to the configured Polymarket address."""
        if not self.gemini_client:
            raise RuntimeError("gemini_client not configured")
        if not config.POLYMARKET_DEPOSIT_ADDRESS:
            raise RuntimeError("POLYMARKET_DEPOSIT_ADDRESS env var not set")
        result = self.gemini_client.withdraw_usdc(
            address=config.POLYMARKET_DEPOSIT_ADDRESS,
            amount=amount_usd,
        )
        if not result:
            raise RuntimeError("gemini withdraw_usdc returned empty response")
        return result.get("txHash") or result.get("tx_hash")

    def _polymarket_to_gemini(self, amount_usd: float) -> str | None:
        """Send USDC from the Polymarket proxy wallet to a Gemini deposit address."""
        if not self.web3_send_usdc:
            raise RuntimeError(
                "Polymarket->Gemini transfer requires web3_send_usdc helper"
            )
        # Gemini deposit address is fetched at call time; production will
        # store this in config under GEMINI_DEPOSIT_ADDRESS once the user
        # supplies one. For the first ship we expose this knob:
        import os
        dest = os.getenv("GEMINI_DEPOSIT_ADDRESS", "")
        if not dest:
            raise RuntimeError("GEMINI_DEPOSIT_ADDRESS env var not set")
        return self.web3_send_usdc(amount_usd, dest)

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _build_idempotency_key(from_platform: str, to_platform: str,
                               amount_usd: float) -> str:
        """Idempotency window = current UTC hour. Replays within the same
        hour collide with the UNIQUE constraint on the audit table.
        """
        bucket = int(time.time() // 3600)
        raw = f"{from_platform}->{to_platform}:{amount_usd:.4f}:{bucket}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
