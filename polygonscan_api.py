"""Polygonscan REST API client for monitoring whale wallet activity on Polygon."""

import logging
import requests
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger(__name__)


class _RateLimitError(requests.RequestException):
    """Raised for Polygonscan HTTP 429 responses."""


class PolygonscanClient:
    """Polygonscan REST API client for wallet transaction monitoring.

    Fetches transaction lists for specified wallet addresses on Polygon chain.
    Used for whale copy trading strategy to detect large trades on Polymarket CLOB.

    Supports both free tier (5 req/sec limit, 100K calls/day) and Pro tier APIs.
    """

    def __init__(self, api_key: str = ""):
        """Initialize Polygonscan client.

        Args:
            api_key: Polygonscan API key (free or Pro tier).
                     If empty, falls back to free tier endpoint with "YourApiKeyToken".
        """
        self.api_key = api_key
        self.base_url = "https://api.polygonscan.com/api"

        # Session reuse for HTTP connection pooling
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10))

        self._request_timeout = 10.0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
            _RateLimitError,
        )),
        reraise=True,
    )
    def get_latest_transactions(
        self,
        address: str,
        start_block: int = 0,
        sort: str = "desc",
    ) -> list[dict]:
        """Fetch latest transactions for a wallet address on Polygon.

        Polls Polygonscan API for all transactions sent from the given address.
        Results are sorted by block number (newest first by default).

        Args:
            address: Polygon wallet address (0x...).
            start_block: Start block number for filtering (0 = from genesis).
            sort: "asc" (oldest first) or "desc" (newest first). Newest first is typical for
                  real-time detection of whale activity.

        Returns:
            List of transaction dicts with keys:
            - hash: Transaction hash (0x...)
            - from: Sender address
            - to: Recipient address (target contract)
            - value: Amount transferred (in wei)
            - blockNumber: Block number
            - timeStamp: Unix timestamp of block
            - gas: Gas used
            - gasPrice: Gas price in wei
            - input: Transaction calldata (encoded function call + params)
            - isError: "0" if success, "1" if reverted
            - type: "call", "create", etc.

        Returns:
            Empty list if no transactions found or API error.

        Raises:
            requests.Timeout: On network error (will retry with exponential backoff).
        """
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": start_block,
            "endblock": 99999999,
            "sort": sort,
            "apikey": self.api_key or "YourApiKeyToken",
        }

        try:
            resp = self._session.get(
                self.base_url,
                params=params,
                timeout=self._request_timeout,
            )

            if resp.status_code == 429:
                logger.warning("Polygonscan rate limited (429), will retry")
                raise _RateLimitError("Rate limit exceeded")

            resp.raise_for_status()

            data = resp.json()

            # Polygonscan returns status="0" for no data or error
            if data.get("status") == "0":
                message = data.get("message", "Unknown error")
                logger.debug("No transactions for %s: %s", address, message)
                return []

            transactions = data.get("result", [])
            logger.info("Fetched %d transactions for %s", len(transactions), address)

            return transactions

        except (requests.Timeout, requests.HTTPError, _RateLimitError):
            logger.warning("Polygonscan request failed for %s, will retry", address)
            raise

        except Exception as e:
            logger.debug("Polygonscan fetch error for %s: %s", address, str(e))
            return []

    def get_contract_events(self, address: str, topic: str) -> list[dict]:
        """Fetch contract events (logs) for a contract address and topic.

        NOTE: This is a stub for Phase 10 (detailed event parsing).
        Full implementation requires Polygon logs API and ABI decoding.

        Args:
            address: Contract address to query.
            topic: Event topic hash (e.g., OrderFilled(address,uint256,...)).

        Returns:
            Empty list (stub implementation).
        """
        # TODO: Phase 10 — Polygon logs API for OrderFilled event decoding
        # Will require:
        # - topic parameter (keccak256(OrderFilled(address,uint256,...)))
        # - Parsing of indexed + data fields
        # - ABI decoding via py-clob-client
        return []
