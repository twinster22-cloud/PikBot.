"""Private prospective record. Scores and decision evidence are immutable after issue."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from flask import Blueprint, jsonify
from psycopg.types.json import Jsonb
from membership import database, owner_required, check_csrf
import pick_results
from prediction_team import MODEL_VERSION, accuracy_summary

audit = Blueprint('prediction_audit', __name__)


def init_audit(app):
    with database() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_prediction_audit (
            prediction_id TEXT PRIMARY KEY, model_version TEXT NOT NULL,
            issued_at TIMESTAMPTZ NOT NULL, starts_at TIMESTAMPTZ NOT NULL,
            payload JSONB NOT NULL, result TEXT NOT NULL DEFAULT 'PENDING',
            actual DOUBLE PRECISION, checked_at TIMESTAMPTZ,
            CHECK (result IN ('PENDING','WIN','LOSS','PUSH','VOID'))
        )''')
        conn.execute('''CREATE INDEX IF NOT EXISTS pikbot_prediction_audit_due
            ON pikbot_prediction_audit (starts_at, checked_at) WHERE result='PENDING' ''')
    app.register_blueprint(audit)


def archive(picks, now):
    fields = {'sport','event_id','player','stat','line','side','matchup','starts_at',
              'bookmaker','p_raw','p_push','projection','games','team_review','line_updated_at',
              'stats_source','history_digest','playing_time','market_review'}
    with database() as conn:
        for pick in picks:
            start = pick_results.timestamp(pick.get('starts_at'))
            if not start or start <= now:
                raise ValueError('Pregame predictions cannot be issued after the scheduled start.')
            identity = [MODEL_VERSION, pick['sport'],pick['event_id'],pick.get('bookmaker'),
                        pick['player'],pick['stat'],pick['line'],pick['side']]
            key = sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
            payload = {k:v for k,v in pick.items() if k in fields}
            payload['model_version'] = MODEL_VERSION
            conn.execute('''INSERT INTO pikbot_prediction_audit
                (prediction_id,model_version,issued_at,starts_at,payload) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (prediction_id) DO NOTHING''', (key, MODEL_VERSION, now, start, Jsonb(payload)))


@audit.get('/api/owner/accuracy')
@owner_required
def accuracy():
    with database() as conn:
        rows = conn.execute('''SELECT payload || jsonb_build_object('result',result,
            'actual',actual,'model_version',model_version) AS record FROM pikbot_prediction_audit''').fetchall()
        first = conn.execute('SELECT MIN(issued_at) AS first FROM pikbot_prediction_audit').fetchone()['first']
    return jsonify(**accuracy_summary([r['record'] for r in rows]),
                   tracking_started=first.isoformat() if first else None)


@audit.post('/api/owner/accuracy/settle')
@owner_required
def settle():
    check_csrf()
    now = datetime.now(timezone.utc)
    # A lease prevents simultaneous clicks/processes grading the same batch.
    with database() as conn:
        rows = conn.execute('''SELECT prediction_id,payload FROM pikbot_prediction_audit
            WHERE result='PENDING' AND starts_at < %s
              AND (checked_at IS NULL OR checked_at < %s)
            ORDER BY checked_at NULLS FIRST,starts_at LIMIT %s FOR UPDATE SKIP LOCKED''',
            (now - timedelta(hours=3), now - timedelta(minutes=30), pick_results.PAGE_SIZE)).fetchall()
        for row in rows:
            conn.execute('UPDATE pikbot_prediction_audit SET checked_at=%s WHERE prediction_id=%s',
                         (now, row['prediction_id']))
    picks = pick_results.grade([dict(row['payload']) for row in rows], now)
    with database() as conn:
        for row, pick in zip(rows, picks):
            conn.execute('''UPDATE pikbot_prediction_audit SET result=%s,actual=%s
                WHERE prediction_id=%s AND result='PENDING' ''',
                (pick['result'],pick.get('actual'),row['prediction_id']))
    return jsonify(checked=len(picks),settled=sum(p['result'] != 'PENDING' for p in picks),
                   message='Only verified completed box scores are graded. Unresolved records stay pending; retry after 30 minutes.')
