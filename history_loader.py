"""Bound slow provider work so HTTP workers remain available on the free plan."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Lock
from time import monotonic
import json

import pandas as pd

from today import FeedError

POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix='pikbot-history')
LOCK = Lock()
JOBS = {}
MAX_JOBS = 24


def cache_key(sport, offers, now):
    players = sorted(str(p) for p in offers['player'].drop_duplicates())
    identity = [sport, now.date().isoformat(), players, 'team-minutes-v1']
    if sport == 'NBA':
        identity.append('nba-franchise-box-v3')
    return sha256(json.dumps(identity).encode()).hexdigest()


def load(sport, offers, now, provider, read_cache=None, write_cache=None):
    key = cache_key(sport, offers, now)
    with LOCK:
        for old, (future, expiry) in list(JOBS.items()):
            if future.done() and expiry < monotonic():
                del JOBS[old]
        job = JOBS.get(key)
        if job is None:
            saved = read_cache(key) if read_cache else None
            if saved is not None:
                return saved
            if len(JOBS) >= MAX_JOBS:
                raise FeedError('Player history is refreshing. Please try again shortly.')

            def work():
                frame = provider(sport, offers, now)
                # Retain only the requested athletes, never a whole league per matchup.
                attrs = dict(frame.attrs)
                frame = frame[frame['player'].isin(offers['player'])].copy()
                frame.attrs = attrs
                if write_cache and not frame.empty:
                    write_cache(key, frame)
                return frame

            future = POOL.submit(work)
            JOBS[key] = (future, monotonic() + 21600)
        else:
            future, _ = job
        if not future.done():
            # Allow a brief wait for fast cache/provider responses without tying up workers indefinitely.
            raise FeedError('Player history is loading. Picks will appear when the provider finishes.')
        try:
            return future.result()
        except Exception as exc:
            _, expiry = JOBS[key]
            # A failed future must not poison the same player's subsequent requests.
            JOBS.pop(key, None)
            # Errors are bounded; do not expose provider URLs or secret query parameters.
            if isinstance(exc, FeedError):
                raise exc
            raise FeedError('Player history is temporarily unavailable. Please try again shortly.') from None


def encode(frame):
    return json.dumps({'rows': json.loads(frame.to_json(orient='records', date_format='iso')),
                       'attrs': frame.attrs}, allow_nan=False)


def decode(payload):
    frame = pd.DataFrame(payload['rows'])
    frame.attrs.update(payload.get('attrs', {}))
    return frame
