"""Read-only scheduled line scanner. Never places bets or publishes picks.

Run with: python scan_worker.py
Requires the same odds-provider environment as the PikBot web service.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import today

SPORTS = {
    'morning': ('NBA','NHL','COLLEGE BASKETBALL','COLLEGE FOOTBALL'),
    'injury': ('NBA','NHL'),
    'pregame': ('NBA','NHL'),
    'football': ('NFL','COLLEGE FOOTBALL'),
}
BOOKS = ('prizepicks','underdog','draftkings','fanduel','betmgm')


def policy(now):
    local=now.astimezone(ZoneInfo('America/New_York'))
    minute=local.hour*60+local.minute
    weekend=local.weekday()>=5
    active=[]
    if 600<=minute<690:active.append('morning')
    if 990<=minute<1110:active.append('injury')
    if 1125<=minute<1155:active.append('pregame')
    if weekend and 690<=minute<765:active.append('football')
    peak=(990<=minute<1155) or (weekend and 690<=minute<780)
    interval=3 if peak else 5 if active else 20
    sports=sorted(set(s for w in active for s in SPORTS[w]))
    return {'time_et':local.isoformat(),'windows':active,'interval_minutes':interval,'sports':sports}


def scan_once(now=None):
    now=now or datetime.now(timezone.utc)
    settings=policy(now)
    rows=[]
    for sport in settings['sports']:
        for bookmaker in BOOKS:
            try:
                offers=today.offers(sport,now,bookmaker=bookmaker)
                for offer in offers:
                    start=today.timestamp(offer.get('starts_at'))
                    updated=today.timestamp(offer.get('line_updated_at'))
                    if not start or start-now<=timedelta(minutes=5):
                        continue
                    if not updated or updated>now+timedelta(minutes=2):
                        continue
                    age=(now-updated).total_seconds()
                    max_age=300 if bookmaker=='prizepicks' else 900
                    if not 0<=age<=max_age:
                        continue
                    rows.append({'sport':sport,'bookmaker':bookmaker,
                                 'player':offer.get('player'),'stat':offer.get('stat'),
                                 'line':offer.get('line'),'updated_at':updated.isoformat(),
                                 'starts_at':start.isoformat(),'verified_fresh':True,
                                 'approved':False,'payout_verified':False})
            except (today.FeedError,ValueError,KeyError) as exc:
                print(json.dumps({'event':'feed_error','sport':sport,'bookmaker':bookmaker,
                                  'message':str(exc)[:160]}),flush=True)
    print(json.dumps({'event':'scan','policy':settings,'fresh_offers':len(rows),
                      'sample':rows[:10],
                      'note':'Research only; no verified payouts, no issued picks, no wagers.'}),flush=True)
    return settings


def main():
    while True:
        started=time.monotonic()
        settings=scan_once()
        delay=max(30,settings['interval_minutes']*60-(time.monotonic()-started))
        if os.getenv('PIKBOT_SCAN_ONCE')=='1':
            break
        time.sleep(delay)


if __name__=='__main__':
    main()
