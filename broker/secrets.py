"""Secret rotation plumbing: values move process→process, never through logs.

The secret is read from a getter command (e.g. `infisical secrets get … --plain`)
and piped directly into a setter command's stdin (e.g. `gh secret set NAME`).
The value is held only as opaque bytes and is never decoded, logged, returned,
or interpolated into a shell string — so it can never enter LLM context.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)


class SecretRotationError(RuntimeError):
    """Rotation failed or could not be verified. Caller marks IN_DOUBT."""


def rotate_secret_via_stdin(
    get_cmd: list[str],
    set_cmd: list[str],
    timeout: float = 60.0,
) -> bool:
    """Fetch a secret via get_cmd and pipe it into set_cmd's stdin.

    Returns True only when the setter exits 0 (verified). Raises
    SecretRotationError when the getter OR setter fails — a rotation must never
    silently "succeed" or return a falsey no-op that a caller could misread;
    error messages never include the secret value.
    """
    if not get_cmd or not set_cmd:
        raise SecretRotationError("get_cmd and set_cmd are both required")

    try:
        get_proc = subprocess.run(get_cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # .stdout may hold partial secret bytes — never surface it (from None).
        raise SecretRotationError(f"secret getter {get_cmd[0]!r} timed out") from None
    except OSError as exc:
        raise SecretRotationError(
            f"secret getter {get_cmd[0]!r} could not run: {exc}"
        ) from exc
    if get_proc.returncode != 0:
        raise SecretRotationError(
            f"secret getter {get_cmd[0]!r} exited {get_proc.returncode}"
        )
    # CLI getters terminate output with a newline that is not part of the
    # secret; storing it would corrupt the rotated credential.
    value = get_proc.stdout.rstrip(b"\r\n")
    if not value.strip():
        raise SecretRotationError(f"secret getter {get_cmd[0]!r} returned empty output")

    try:
        set_proc = subprocess.run(set_cmd, input=value, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SecretRotationError(f"secret setter {set_cmd[0]!r} timed out") from None
    except OSError as exc:
        raise SecretRotationError(
            f"secret setter {set_cmd[0]!r} could not run: {exc}"
        ) from exc
    if set_proc.returncode != 0:
        # Fail-closed: the caller marks the rotation IN_DOUBT and escalates,
        # never retries. Returning False here would let a caller misread a
        # failed rotation as a benign no-op.
        raise SecretRotationError(
            f"secret setter {set_cmd[0]!r} exited {set_proc.returncode}"
        )
    logger.info("Secret rotated via %r -> %r (value not logged)", get_cmd[0], set_cmd[0])
    return True
