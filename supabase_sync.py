"""Mirror arbgrid reward data into the shared Supabase rewards_events table.

SQLite stays the source of truth; this module reads local reward rows and
upserts them into Supabase so multiple engines roll up P&L in one place
(architecture locked 2026-06-13). Deterministic, no LLM.

The Supabase client is injected (anything exposing the supabase-py
``table(name).upsert(rows, on_conflict=...).execute()`` chain) so the mapping
and upsert logic are testable without a live project. ``build_client_from_env``
constructs a real client from SUPABASE_URL + SUPABASE_SERVICE_KEY when needed.
"""

from __future__ import annotations

import datetime
import logging
import os

from url_guard import assert_public_url

logger = logging.getLogger(__name__)

REWARDS_TABLE = 'rewards_events'
OPPORTUNITIES_TABLE = 'paper_opportunities'

# reward_metrics is prediction-market market-making activity; rebates are
# ordinary income for tax purposes (doc 07 tax map).
_DEFAULT_LANE = 'prediction-markets'
_DEFAULT_TAX_BUCKET = 'ordinary'


def _engine_for(platform: str) -> str:
    """Map a reward_metrics platform to a rewards_events engine tag."""
    return f'{platform}-lip' if platform == 'kalshi' else f'{platform}-rewards'


def reward_metric_to_event(row: dict) -> dict:
    """Convert one reward_metrics row into a rewards_events record.

    Placement/cancellation metrics are liquidity activity, not realised cash, so
    reward_usd is left None; the row is an auditable accrual-input keyed for
    idempotent upsert. Realised reward USD is attached downstream when Kalshi
    settles the period.

    Args:
        row: A reward_metrics row (id, platform, market_key, order_id, event,
            size, spread, resting_seconds, timestamp).

    Returns:
        A rewards_events record dict.
    """
    ts = int(row.get('timestamp') or 0)
    event_date = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()
    return {
        'engine': _engine_for(row.get('platform', 'unknown')),
        'lane': _DEFAULT_LANE,
        'tax_bucket': _DEFAULT_TAX_BUCKET,
        'event_date': event_date,
        'asset': None,
        'reward_usd': None,
        'source_key': f"reward_metric:{row.get('id')}",
        'notes': (
            f"{row.get('event')} {row.get('market_key')} "
            f"size={row.get('size')} spread={row.get('spread')} "
            f"resting_s={row.get('resting_seconds')}"
        ),
    }


def build_client_from_env():
    """Build a Supabase client from SUPABASE_URL + SUPABASE_SERVICE_KEY.

    Returns:
        A supabase-py Client when the SDK is installed, otherwise the built-in
        PostgrestClient (same interface subset).

    Raises:
        RuntimeError: If credentials are missing.
    """
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY') or os.getenv('SUPABASE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and SUPABASE_SERVICE_KEY (or SUPABASE_KEY) must be set'
        )
    url = assert_public_url(url, env_name='SUPABASE_URL', allow_http=False)
    try:
        from supabase import create_client
    except ImportError:
        logger.info('supabase-py not installed; using built-in PostgREST client')
        return PostgrestClient(url, key)
    return create_client(url, key)


class _PostgrestResponse:
    """Minimal response wrapper matching supabase-py's ``.data`` attribute."""

    def __init__(self, data):
        self.data = data


class _PostgrestQuery:
    """Chainable query builder for one table, supabase-py-compatible subset."""

    def __init__(self, base_url: str, key: str, table: str, session):
        self._url = f"{base_url}/rest/v1/{table}"
        self._key = key
        self._session = session
        self._mode = None
        self._payload = None
        self._params: dict = {}

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {
            'apikey': self._key,
            'Authorization': f'Bearer {self._key}',
            'Content-Type': 'application/json',
        }
        if extra:
            headers.update(extra)
        return headers

    def upsert(self, rows: list[dict], on_conflict: str = ''):
        self._mode = 'upsert'
        self._payload = rows
        if on_conflict:
            self._params['on_conflict'] = on_conflict
        return self

    def select(self, columns: str = '*'):
        self._mode = 'select'
        self._params['select'] = columns
        return self

    def eq(self, column: str, value):
        self._params[column] = f'eq.{value}'
        return self

    def order(self, column: str, desc: bool = False):
        self._params['order'] = f"{column}.{'desc' if desc else 'asc'}"
        return self

    def limit(self, n: int):
        self._params['limit'] = n
        return self

    def execute(self) -> _PostgrestResponse:
        if self._mode == 'upsert':
            resp = self._session.post(
                self._url,
                params=self._params,
                headers=self._headers({'Prefer': 'resolution=merge-duplicates,return=minimal'}),
                json=self._payload,
                timeout=15,
                allow_redirects=False,
            )
            if resp.status_code >= 300:
                raise RuntimeError(
                    f'PostgREST upsert failed ({resp.status_code}): {resp.text[:200]}')
            return _PostgrestResponse([])
        # Never follow redirects: the service key rides in headers and must not
        # be replayed to a redirect target.
        resp = self._session.get(
            self._url, params=self._params, headers=self._headers(), timeout=15,
            allow_redirects=False)
        if resp.status_code >= 300:
            raise RuntimeError(
                f'PostgREST select failed ({resp.status_code}): {resp.text[:200]}')
        return _PostgrestResponse(resp.json())


class PostgrestClient:
    """Dependency-free stand-in for supabase-py's client.

    Speaks the PostgREST HTTP API directly with ``requests`` (already a runtime
    dependency), covering exactly the chain this module uses:
    ``table().upsert().execute()`` and ``table().select().eq().order().limit()
    .execute()``. Used when supabase-py is not installed — the deployed image
    never shipped it, which silently disabled the opportunity mirror.
    """

    def __init__(self, url: str, key: str, session=None):
        import requests as _requests

        self._url = url.rstrip('/')
        self._key = key
        self._session = session or _requests.Session()

    def table(self, name: str) -> _PostgrestQuery:
        return _PostgrestQuery(self._url, self._key, name, self._session)


class RewardSync:
    """Upsert local reward rows into the shared Supabase rewards_events table."""

    def __init__(self, supabase_client, db=None):
        """Initialise the sync.

        Args:
            supabase_client: Client exposing table().upsert().execute().
            db: Optional TradeDB for reading reward_metrics directly.
        """
        self._client = supabase_client
        self._db = db

    def upsert_events(self, events: list[dict]) -> int:
        """Upsert rewards_events records, deduped on (engine, source_key).

        Args:
            events: Records with at least engine, lane, tax_bucket, event_date,
                source_key.

        Returns:
            Number of records sent.

        Raises:
            ValueError: If any record is missing a required field.
        """
        if not events:
            return 0
        required = ('engine', 'lane', 'tax_bucket', 'event_date', 'source_key')
        for event in events:
            missing = [field for field in required if not event.get(field)]
            if missing:
                raise ValueError(f'rewards_events record missing {missing}: {event}')
        try:
            self._client.table(REWARDS_TABLE).upsert(
                events, on_conflict='engine,source_key'
            ).execute()
        except Exception as exc:
            logger.error('Supabase rewards upsert failed: %s', exc)
            raise
        return len(events)

    def sync_reward_metrics(self, since_ts: int = 0) -> int:
        """Read reward_metrics from the local DB and upsert them to Supabase.

        Args:
            since_ts: Only sync rows newer than this Unix-seconds value.

        Returns:
            Number of records upserted.

        Raises:
            RuntimeError: If no DB was provided.
        """
        if self._db is None:
            raise RuntimeError('RewardSync has no db to read reward_metrics from')
        rows = self._db.get_reward_metrics(since_ts=since_ts)
        events = [reward_metric_to_event(row) for row in rows]
        return self.upsert_events(events)


class OpportunitySync:
    """Mirror trades.db opportunity rows into Supabase paper_opportunities.

    SQLite stays the source of truth; the mirror makes the paper record durable
    off-box and queryable by cloud agents without deploy-environment
    credentials. Idempotent via (engine, source_id) upsert; the caller tracks
    the high-water mark and only advances it on successful delivery.
    """

    ENGINE = 'arbgrid'

    def __init__(self, supabase_client, db=None):
        self._client = supabase_client
        self._db = db

    def sync_opportunities(self, after_id: int = 0, limit: int = 500) -> int:
        """Upsert opportunities with id > after_id; return the new high-water mark.

        Raises on delivery failure so the caller does NOT advance the mark and
        the rows are retried on the next call.
        """
        if self._db is None:
            raise RuntimeError('OpportunitySync has no db to read opportunities from')
        rows = self._db.get_opportunities_after(after_id, limit=limit)
        if not rows:
            return after_id
        records = [
            {
                'engine': self.ENGINE,
                'source_id': row['id'],
                'detected_at': row['timestamp'],
                'opp_type': row['type'],
                'market': row['market'],
                'prices': row['prices'],
                'total_cost': row['total_cost'],
                'net_profit': row['net_profit'],
                'net_roi': row['net_roi'],
                'depth': row['depth'],
                'action': row['action'],
            }
            for row in rows
        ]
        try:
            self._client.table(OPPORTUNITIES_TABLE).upsert(
                records, on_conflict='engine,source_id'
            ).execute()
        except Exception as exc:
            logger.error('Supabase opportunity upsert failed: %s', exc)
            raise
        return max(row['id'] for row in rows)

    def get_remote_high_water_mark(self, window_start_ts: float = 0.0) -> int:
        """Highest source_id already mirrored for this engine.

        Returns 0 for an empty remote table. On an unreachable remote it
        falls back to the LOCAL max opportunity id (when a db is attached, else
        0) — deliberately skipping history so a Supabase outage at startup
        never triggers a full historical backfill; rows detected while the
        remote was down are not retro-mirrored.
        """
        try:
            resp = (
                self._client.table(OPPORTUNITIES_TABLE)
                .select('source_id')
                .eq('engine', self.ENGINE)
                .order('source_id', desc=True)
                .limit(1)
                .execute()
            )
            data = getattr(resp, 'data', None) or []
            remote_max = int(data[0]['source_id']) if data else 0
            seed = 0
            if window_start_ts and self._db is not None:
                # With a configured paper window, never resume below the first
                # in-window row — otherwise a fresh (or partially backfilled)
                # remote spends days mirroring months of pre-window history
                # before current detections ever arrive.
                import datetime as _dt
                since_iso = _dt.datetime.fromtimestamp(
                    window_start_ts, _dt.timezone.utc).isoformat()
                with self._db._lock:
                    row = self._db.conn.execute(
                        'SELECT MIN(id) FROM opportunities WHERE timestamp >= ?',
                        (since_iso,),
                    ).fetchone()
                if row and row[0]:
                    seed = int(row[0]) - 1
                else:
                    # Window configured but no in-window rows yet (fresh start
                    # or quiet period): seed at the local max so pre-window
                    # history is still never replayed.
                    with self._db._lock:
                        row = self._db.conn.execute(
                            'SELECT MAX(id) FROM opportunities').fetchone()
                    seed = int(row[0] or 0)
            return max(remote_max, seed)
        except Exception as exc:
            logger.warning('Could not read remote opportunity high-water mark: %s', exc)
            if self._db is not None:
                with self._db._lock:
                    row = self._db.conn.execute('SELECT MAX(id) FROM opportunities').fetchone()
                return int(row[0] or 0)
            return 0
