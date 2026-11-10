"""Synthetic correctness fixtures. These tests do not measure real prediction accuracy."""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import engine
from prediction_team import review, accuracy_summary
NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def fixture_pick(**changes):
    return {'sport':'NBA','event_id':'game-1','player':'Test Player','stat':'PTS','line':12.5,
            'side':'MORE','p_raw':.95,'games':30,'projection':20,
            'starts_at':(NOW+timedelta(hours=2)).isoformat(),'matchup':'Away at Home',
            'recent_games':[{'date':'2026-11-01','value':20} for _ in range(10)],
            'playing_time':{'projected':30,'stable':True}, **changes}


def test_high_score_never_invents_lineup_or_validation_approval():
    p=fixture_pick()
    result=review(p, {'starts_at':p['starts_at'],'line_updated_at':NOW.isoformat()}, NOW)
    assert result['screened'] and not result['approved']
    assert len(result['checks'])==6
    assert not result['checks'][-1]['passed'] and not result['checks'][-2]['passed']


@pytest.mark.parametrize('changes',[
    {'line_updated_at':(NOW-timedelta(minutes=16)).isoformat()},
    {'starts_at':(NOW-timedelta(seconds=1)).isoformat()},
    {'line_updated_at':(NOW+timedelta(seconds=1)).isoformat()},
])
def test_stale_future_or_started_offer_fails_data_reviewer(changes):
    p=fixture_pick();offer={'starts_at':p['starts_at'],'line_updated_at':NOW.isoformat(),**changes}
    assert not review(p,offer,NOW)['checks'][0]['passed']


def test_missing_playing_time_is_not_filled_in():
    p=fixture_pick(playing_time=None)
    assert not review(p,{},NOW)['checks'][3]['passed']


def test_minutes_shift_is_detected():
    minutes=pd.DataFrame({'minutes':[10]*17+[30]*3})
    r=engine.nba_playing_time(minutes)
    assert r['adjusted'] and not r['stable']


def test_same_day_outcome_cannot_leak_into_pregame_history():
    dates=pd.date_range('2026-09-01',periods=20,tz='UTC')
    h=pd.DataFrame({'date':dates,'sport':'NBA','player':'Test Player','stat':'PTS','value':10})
    h=pd.concat([h,pd.DataFrame([{'date':NOW.replace(hour=0),'sport':'NBA','player':'Test Player','stat':'PTS','value':1000}])])
    o=pd.DataFrame([{'as_of':NOW,'sport':'NBA','player':'Test Player','stat':'PTS','line':12.5}])
    p=engine.predict(h,o,max_age_days=100)
    assert len(p)==2 and p.iloc[0].projection==10 and p.iloc[0].games==20


def test_conflicting_game_values_are_rejected():
    h=pd.DataFrame({'date':pd.date_range('2026-10-01',periods=20,tz='UTC'),'sport':'NBA','player':'Test Player','stat':'PTS','value':10})
    h=pd.concat([h,h.iloc[:1].assign(value=99)])
    o=pd.DataFrame([{'as_of':NOW,'sport':'NBA','player':'Test Player','stat':'PTS','line':12.5}])
    assert engine.predict(h,o).empty


def test_reserve_nba_excludes_all_star_games_from_points_history(monkeypatch):
    import player_history

    player = 'Test Player'
    games = pd.DataFrame([
        {'game_id': str(index), 'game_date': date, 'athlete_id': '7',
         'athlete_display_name': player, 'athlete_headshot_href': None,
         'team_id': 18, 'opponent_team_id': 15, 'minutes': 30,
         'points': 25, 'rebounds': 4, 'assists': 5, 'did_not_play': False}
        for index, date in enumerate(pd.date_range('2026-10-20', periods=15))
    ])
    exhibitions = games.iloc[[0, 0]].assign(game_id=['ex1', 'ex2'],
        team_id=132375, opponent_team_id=132374, minutes=[7, 8], points=[2, 8])
    monkeypatch.setattr(player_history, 'reserve_nba_season', lambda season, day:
        games if season == 2027 else exhibitions)
    offers = pd.DataFrame([{'as_of': NOW, 'sport': 'NBA', 'player': player,
                            'stat': stat, 'line': line}
                           for stat, line in [('PTS', 24.5), ('PA', 29.5)]])

    history = player_history.reserve_nba_history(offers, NOW)
    picks = engine.predict(history, offers, include_history=True)
    assert set(picks['stat']) == {'PTS', 'PA'}
    assert picks['games'].eq(15).all()
    assert set(picks.loc[picks['stat'] == 'PTS', 'projection']) == {25.0}


def test_backtest_counts_one_side_and_grades_selected_less():
    rows=[]
    for i in range(2):
        for side,p in [('MORE',.1),('LESS',.9)]:
            rows.append({'as_of':NOW,'sport':'NBA','player':f'P{i}','stat':'PTS','line':10.5,
                         'offer_id':f'o{i}','event_id':f'e{i}','side':side,'p_raw':p,'p_push':0,'actual':1})
    with patch.object(engine,'predict',return_value=pd.DataFrame(rows)):
        picks,entries=engine.backtest(pd.DataFrame(),pd.DataFrame({'actual':[1]}))
    assert len(picks)==2 and set(picks.side)=={'LESS'}
    assert len(entries)==1 and entries.iloc[0].entry_win==1


def test_report_keeps_pending_pushes_and_bands_separate():
    rows=[{'sport':'NBA','model_version':'v1','p_raw':.75,'result':r}
          for r in ['WIN','WIN','LOSS','PUSH','PENDING']]
    r=accuracy_summary(rows)
    assert r['overall']['win_rate']==66.67
    assert r['overall']['pending']==1 and r['overall']['pushes']==1
    assert r['score_bands'][2]['logged']==5
    assert not r['claim_supported']


class FakeDB:
    def __init__(self):self.queries=[];self.rows=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,params=()):
        from pglast import parse_sql
        parse_sql(sql.replace('%s','NULL'))
        self.queries.append((sql,params));return self
    def fetchall(self):return self.rows
    def fetchone(self):return {'first':None}


@pytest.fixture
def server_env(monkeypatch):
    import membership
    db=FakeDB()
    monkeypatch.setenv('PIKBOT_SESSION_SECRET','synthetic-test-only-session-secret')
    monkeypatch.setattr(membership,'database',lambda:db)
    import server
    import prediction_audit
    import owner_dashboard
    for module in [server,prediction_audit,owner_dashboard]:monkeypatch.setattr(module,'database',lambda:db)
    monkeypatch.setattr(membership,'account',lambda:None)
    return server, membership, prediction_audit, db


def test_audit_endpoints_require_owner_and_csrf(server_env,monkeypatch):
    s,m,a,db=server_env;client=s.app.test_client()
    assert client.get('/api/owner/accuracy').status_code==401
    monkeypatch.setattr(m,'account',lambda:{'email':'member@example.test'})
    monkeypatch.setattr(m,'private_paid_test',lambda r:False)
    assert client.get('/api/owner/accuracy').status_code==403
    monkeypatch.setattr(m,'private_paid_test',lambda r:True)
    assert client.post('/api/owner/accuracy/settle').status_code==400
    assert client.get('/api/owner/accuracy').json['overall']['logged']==0


def test_live_performance_is_paid_and_does_not_make_picks(server_env,monkeypatch):
    s,m,a,db=server_env;client=s.app.test_client()
    monkeypatch.setattr(m,'account',lambda:{'email':'member@example.test'})
    monkeypatch.setattr(m,'access',lambda r:'trial')
    assert client.get('/api/today/performance?sport=NBA').status_code==403
    monkeypatch.setattr(m,'access',lambda r:'expired')
    assert client.get('/api/today/performance?sport=NBA').status_code==402
    monkeypatch.setattr(m,'access',lambda r:'subscriber')
    monkeypatch.setattr(s.today,'today_events',lambda *a:[{'id':'game-1','away_team':'Away','home_team':'Home','commence_time':NOW.isoformat()}])
    monkeypatch.setattr(s.pick_results,'live_performance',lambda *a:{'players':[],'note':'Observed stats only'})
    r=client.get('/api/today/performance?sport=NBA&event_id=game-1')
    assert r.status_code==200 and 'picks' not in r.json


def test_archival_is_immutable_and_cannot_issue_after_start(server_env):
    s,m,a,db=server_env
    p=fixture_pick(bookmaker='PrizePicks',team_review={})
    a.archive([p],NOW)
    sql,params=db.queries[-1]
    assert 'DO NOTHING' in sql and params[2]==NOW
    with pytest.raises(ValueError):a.archive([p],NOW+timedelta(hours=3))


def test_settler_leases_work_and_preserves_pending(server_env,monkeypatch):
    s,m,a,db=server_env;client=s.app.test_client()
    monkeypatch.setattr(m,'account',lambda:{'email':'owner@example.test'})
    monkeypatch.setattr(m,'private_paid_test',lambda r:True)
    db.rows=[{'prediction_id':'one','payload':fixture_pick()}]
    monkeypatch.setattr(a.pick_results,'grade',lambda picks,now:[{**p,'actual':None,'result':'PENDING'} for p in picks])
    with client.session_transaction() as sess:sess['csrf']='test-token'
    r=client.post('/api/owner/accuracy/settle',data={'csrf':'test-token'})
    assert r.json['checked']==1 and r.json['settled']==0
    assert any('SKIP LOCKED' in sql for sql,_ in db.queries)
    assert db.queries[-1][1][0]=='PENDING'


def test_devig_requires_both_prices_and_matches_worked_example():
    from market_math import devig, decimal_odds
    assert devig(-110,-110,'american')['MORE']==pytest.approx(.5)
    assert devig(-142,112,'american')['MORE']==pytest.approx(.5543606364)
    with pytest.raises(ValueError):devig(None,1.9)
    with pytest.raises(ValueError):decimal_odds(True)


def test_ev_is_total_entry_return_not_probability_point_difference():
    from market_math import entry_ev
    assert entry_ev([.5,.5],{2:3})==pytest.approx(-.25)
    assert entry_ev([.75,.75],{2:3})==pytest.approx(.6875)
    assert entry_ev([.5425253777362726]*5,{5:10,4:2,3:.4})==pytest.approx(0,abs=1e-10)
    assert entry_ev([.542059699707008]*6,{6:25,5:2,4:.4})==pytest.approx(0,abs=1e-10)
    with pytest.raises(ValueError):entry_ev([float('nan')],{1:2})


def test_dfs_does_not_get_fake_single_leg_ev():
    from market_math import comparison
    p=fixture_pick(bookmaker='PrizePicks')
    result=comparison(p,{'line_updated_at':NOW.isoformat(),'prices':{'MORE':1.91}},
                      {'last_update':NOW.isoformat(),'MORE':1.7,'LESS':2.1},NOW)
    assert result['available'] and result['net_ev_per_unit'] is None
    assert not comparison(p,{}, {'last_update':NOW.isoformat(),'MORE':1.7,'LESS':2.1},NOW)['available']


def test_exact_line_benchmark_rejects_ambiguous_rows(monkeypatch):
    import today
    payload={'id':'game-1','bookmakers':[{'key':'pinnacle','markets':[
        {'key':'player_points','last_update':NOW.isoformat(),'outcomes':[
            {'description':'Test Player','point':12.5,'name':'Over','price':1.8},
            {'description':'Test Player','point':12.5,'name':'Under','price':2.0},
            {'description':'Test Player','point':12.5,'name':'Over','price':1.9}]}]}]}
    monkeypatch.setattr(today,'event_offers',lambda *a:payload)
    assert today.benchmark_lines('NBA','game-1',NOW)=={}


def test_live_boxscore_shows_actual_stats_not_forecasts(monkeypatch):
    import pick_results as pr
    event={'id':'espn-1','date':NOW.isoformat(),'competitions':[
        {'competitors':[{'homeAway':'away','team':{'displayName':'Away'}},
                        {'homeAway':'home','team':{'displayName':'Home'}}]}]}
    summary={'header':{'competitions':[{'status':{'type':{'state':'in','detail':'3rd quarter'},'period':3,'displayClock':'5:00'},
                'competitors':[{'team':{'displayName':'Away'},'score':'70'},{'team':{'displayName':'Home'},'score':'65'}]}]},
             'boxscore':{'players':[{'statistics':[{'keys':['points','rebounds','assists'],
                'athletes':[{'athlete':{'id':'player-1','displayName':'Test Player'},'stats':['12','4','3']}]}]}]}}
    monkeypatch.setattr(pr,'scoreboard',lambda *a:[event])
    monkeypatch.setattr(pr,'fetch',lambda *a,**k:summary)
    pr.live_performance.cache_clear()
    data=pr.live_performance('NBA','Away at Home',NOW.isoformat(),1)
    assert data['state']=='in' and data['players'][0]['stats']['PTS']==12
    assert 'picks' not in data and 'probability' not in data


@pytest.mark.parametrize('level,archive_fails,expected',[('subscriber',False,200),('trial',False,404),('subscriber',True,503)])
def test_full_prediction_flow_records_before_display_and_withholds_unapproved_trial(server_env,monkeypatch,level,archive_fails,expected):
    s,m,a,db=server_env
    import psycopg
    now=datetime.now(timezone.utc)
    start=(now+timedelta(hours=2)).isoformat()
    offer={'sport':'NBA','event_id':'g1','player':'Test Player','stat':'PTS','line':12.5,
           'as_of':now,'offer_id':'o1','starts_at':start,'line_updated_at':now.isoformat()}
    history=pd.DataFrame({'date':pd.date_range(now-timedelta(days=40),periods=30,tz='UTC'),
                          'sport':'NBA','player':'Test Player','stat':'PTS','value':20})
    monkeypatch.setattr(m,'account',lambda:{'email':'member@example.test'})
    monkeypatch.setattr(m,'access',lambda r:level)
    monkeypatch.setattr(s,'trial_daily_board',lambda *a:None)
    monkeypatch.setattr(s.today,'offers',lambda *args,**kw:[offer])
    monkeypatch.setattr(s.today,'benchmark_lines',lambda *args,**kw:{})
    monkeypatch.setattr(s.today,'today_events',lambda *args:[{'id':'g1','away_team':'Away','home_team':'Home','commence_time':start}])
    monkeypatch.setattr(s.history_loader,'load',lambda *args:history)
    monkeypatch.setattr(s,'attach_photos',lambda *a:None)
    saved=[]
    def archive(picks,now):
        saved.extend(picks)
        if archive_fails:raise psycopg.OperationalError('synthetic archive failure')
    monkeypatch.setattr(a,'archive',archive)
    monkeypatch.setattr(s,'save_member_pick_board',lambda picks,*args:saved.append('display-board'))
    response=s.app.test_client().get('/api/today/predictions?sport=NBA&event_id=g1')
    assert response.status_code==expected
    assert saved and saved[0]['team_review']['approved'] is False
    if expected==200:
        assert response.json['approved_count']==0 and response.json['entries']==[]
        assert saved[-1]=='display-board'
    else:assert 'picks' not in response.json
