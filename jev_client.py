"""Client for TypeSafe's Jev System One decision model via OpenRouter.

Provides low-latency, typed evaluations over structured states using
calibrated probabilities (noul), discrete selections (choice), and
rubric scoring (score).

Conventions:
- Target Python 3.10+
- "AI-powered software, not autonomous agents"
- Deterministic code maintains 100% control over workflow and safety.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_JEV_MODEL = "typesafe/jev-1.13"
DEFAULT_TIMEOUT_SEC = 15


# ---------------------------------------------------------------------------
# Jev Decision Client
# ---------------------------------------------------------------------------


class JevError(Exception):
    """Base exception for Jev decision errors."""


class JevRateLimitError(JevError):
    """Raised when Jev API returns HTTP 429."""


class JevClient:
    """Client for querying TypeSafe's Jev-1.13 decision model."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = OPENROUTER_DECISIONS_URL,
        timeout: int = DEFAULT_TIMEOUT_SEC,
    ):
        """Initialize Jev client.

        Args:
            api_key: OpenRouter API key. If None, reads from OPENROUTER_API_KEY env.
            model: Model identifier. Defaults to typesafe/jev-1.13.
            base_url: Decisions API endpoint URL.
            timeout: Request timeout in seconds.
        """
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        self.model = model or os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL)
        self.base_url = base_url
        self.timeout = timeout

    def is_available(self) -> bool:
        """Check if client has an API key configured."""
        return bool(self.api_key)

    def query_decisions(
        self,
        state: dict[str, Any] | str,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
    ) -> dict[str, Any]:
        """Send a structured decision request to the Jev model.

        Args:
            state: The context or data being evaluated (dict or string).
            questions: Dict of question_id -> question definition dict.
            model: Optional model override.

        Returns:
            Raw response dict containing answers, usage, and model metadata.

        Raises:
            JevError: On API or network failures.
        """
        if not self.api_key:
            raise JevError("OPENROUTER_API_KEY is not configured")

        selected_model = model or self.model
        payload = {
            "model": selected_model,
            "state": state,
            "questions": questions,
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/johnsnow92/polymarket-arb-scanner",
                "X-OpenRouter-Title": "Polymarket-Arb-Scanner / Jev",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error("Jev API HTTP %d error: %s", e.code, err_body)
            if e.code == 429:
                raise JevRateLimitError(f"Rate limited by Jev API: {err_body}") from e
            raise JevError(f"Jev API HTTP {e.code}: {err_body}") from e
        except Exception as e:
            logger.error("Jev network or parsing error: %s", e)
            raise JevError(f"Jev request failed: {e}") from e

    def evaluate_noul(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: dict[str, str] | None = None,
    ) -> float:
        """Evaluate a single yes/no question to a calibrated probability.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Optional clarifying criteria for 'true' and 'false'.

        Returns:
            Calibrated probability float in range [0.0, 1.0].
        """
        question_def: dict[str, Any] = {
            "type": "noul",
            "instructions": instructions,
        }
        if criteria:
            question_def["criteria"] = criteria

        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        return float(q_ans.get("noul", 0.5))

    def evaluate_choice(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: dict[str, str],
    ) -> tuple[str, float, dict[str, float]]:
        """Evaluate a discrete choice question from given options.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Mapping of option_key -> descriptive criteria.

        Returns:
            Tuple of (selected_choice, confidence, probabilities_dict).
        """
        question_def = {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        }
        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        choice = str(q_ans.get("choice", ""))
        conf = float(q_ans.get("confidence", 0.0))
        probs = {str(k): float(v) for k, v in q_ans.get("probabilities", {}).items()}
        return choice, conf, probs

    def evaluate_score(
        self,
        state: dict[str, Any] | str,
        instructions: str,
        criteria: list[str],
    ) -> tuple[float, float, dict[str, float]]:
        """Evaluate an ordered rubric score along descriptive levels.

        Args:
            state: State context to evaluate.
            instructions: Question prompt to evaluate.
            criteria: Ordered list of level descriptions (0, 1, 2...).

        Returns:
            Tuple of (score_float, confidence, probabilities_dict).
        """
        question_def = {
            "type": "score",
            "instructions": instructions,
            "criteria": criteria,
        }
        resp = self.query_decisions(state, {"q": question_def})
        answers = resp.get("answers", {})
        q_ans = answers.get("q", {})
        score = float(q_ans.get("score", 0.0))
        conf = float(q_ans.get("confidence", 0.0))
        probs = {str(k): float(v) for k, v in q_ans.get("probabilities", {}).items()}
        return score, conf, probs


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_client: JevClient | None = None


def get_jev_client() -> JevClient:
    """Get or create the module-level JevClient singleton."""
    global _default_client
    if _default_client is None:
        _default_client = JevClient()
    return _default_client
