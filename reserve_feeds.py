"""Credit-free reserve history. Primary providers remain authoritative until limited."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache
from io import BytesIO
from threading import Lock
import unicodedata
import pandas as pd
import requests
from today import FeedError
COLUMNS=['sport','player','stat','date','value']
BASE='https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
LOCK=Lock()
SPECS={
 'WNBA':('espn_wnba_player_boxscores','player_box'),
 'COLLEGE BASKETBALL':('espn_mens_college_basketball_player_boxscores','player_box'),
 'COLLEGE FOOTBALL':('espn_cfb_player_box','player_box'),
 'NHL':('nhl_skater_boxscores','skater_box'),
}
def normalized(value):
 return ''.join(c for c in unicodedata.normalize('NFKD',str(value)).casefold() if c.isalnum())
def get(url,**kwargs):
 try:
  r=requests.get(url,timeout=20,**kwargs);r.raise_for_status();return r
 except requests.RequestException:raise FeedError('The free reserve history is temporarily unavailable.') from None
@lru_cache(maxsize=4)
def season_frame(sport,season,day):
 tag,prefix=SPECS[sport]
 names={'game_id','game_date','athlete_id','athlete_display_name','athlete_name','did_not_play','minutes','points','rebounds','assists','athlete_headshot_href','passingYards','rushingYards','player_id','player_name','shots_on_goal','toi'}
 data=get(BASE+tag+'/'+prefix+'_'+str(season)+'.csv').content
 frame=pd.read_csv(BytesIO(data),usecols=lambda c:c in names,dtype={'game_id':str,'athlete_id':str,'player_id':str})
 if sport=='COLLEGE FOOTBALL':
  schedule=pd.read_csv(BytesIO(get(BASE+'espn_cfb_schedules/cfb_schedule_'+str(season)+'.csv').content),dtype={'game_id':str},usecols=['game_id','game_date','status'])
  frame=frame.merge(schedule[schedule['status']=='STATUS_FINAL'][['game_id','game_date']],on='game_id',how='inner')
 return frame
@lru_cache(maxsize=1024)
def hockey_name(player_id):
 p=get('https://api-web.nhle.com/v1/player/'+str(player_id)+'/landing').json()
 return (p.get('firstName',{}).get('default','')+' '+p.get('lastName',{}).get('default','')).strip()
@lru_cache(maxsize=2)
def nhl_identities(day):
 # Full names from the reserve roster, never fuzzy matches on initials.
 frame=season_frame('NHL',int(day[:4])+int(int(day[5:7])>=9),day)
 ids=frame['player_id'].dropna().unique()
 return ids

def reserve_history(sport,offers,now):
 if sport=='MLB':return mlb_history(offers,now)
 season=now.year+int(sport in {'NHL','COLLEGE BASKETBALL'} and now.month>=9)
 day=now.date().isoformat()
 with LOCK:
  frames=[season_frame(sport,season,day),season_frame(sport,season-1,day)]
 frame=pd.concat(frames,ignore_index=True)
 frame['date']=pd.to_datetime(frame['game_date'],utc=True,errors='coerce')
 frame=frame[(frame['date']<now.replace(hour=0,minute=0,second=0,microsecond=0))&(frame['date']>=now-timedelta(days=365))]
 if 'minutes' in frame:frame=frame[pd.to_numeric(frame['minutes'],errors='coerce')>0]
 if 'did_not_play' in frame:frame=frame[frame['did_not_play'].astype(str).str.lower()=='false']
 name_col='athlete_name' if sport=='COLLEGE FOOTBALL' else 'athlete_display_name'
 fields={'passingYards':'PASS_YDS','rushingYards':'RUSH_YDS'} if sport=='COLLEGE FOOTBALL' else {'points':'PTS','rebounds':'REB','assists':'AST'}
 if sport=='NHL':name_col='player_name';fields={'shots_on_goal':'SOG','points':'PTS'}
 rows=[];photos={}
 for name in offers['player'].drop_duplicates():
  if sport=='NHL':
   parts=name.split();short=parts[0][0]+'. '+' '.join(parts[1:])
   candidates=frame[frame['player_name'].str.casefold()==short.casefold()]['player_id'].dropna().unique()
   ids=[i for i in candidates if normalized(hockey_name(i))==normalized(name)]
   matches=frame[frame['player_id'].isin(ids)] if len(ids)==1 else frame.iloc[0:0]
   idcol='player_id'
  else:
   matches=frame[frame[name_col].map(normalized)==normalized(name)];idcol='athlete_id'
  if matches[idcol].nunique()!=1:continue
  if 'athlete_headshot_href' in matches:
   urls=matches['athlete_headshot_href'].dropna()
   if len(urls):photos[name]=str(urls.iloc[-1])
  if sport=='NHL' and len(matches):photos[name]='https://assets.nhle.com/mugs/nhl/latest/'+str(matches.iloc[0]['player_id'])+'.png'
  for field,stat in fields.items():
   if field not in matches:continue
   games=matches[['game_id','date',field]].copy();games[field]=pd.to_numeric(games[field],errors='coerce')
   conflicts=games.groupby('game_id')[field].nunique();games=games[~games['game_id'].isin(conflicts[conflicts>1].index)]
   for _,g in games.dropna(subset=[field]).drop_duplicates('game_id').iterrows():
    if 0<=g[field]<float('inf'):rows.append((sport,name,stat,g['date'].isoformat(),float(g[field])))
 result=pd.DataFrame(rows,columns=COLUMNS);result.attrs.update(source='SportsDataverse (reserve)',photos=photos)
 return result
@lru_cache(maxsize=512)
def mlb_id(name):
 rows=get('https://statsapi.mlb.com/api/v1/people/search',params={'names':name}).json().get('people',[])
 ids={p['id'] for p in rows if p.get('fullName','').casefold()==name.casefold()}
 return next(iter(ids)) if len(ids)==1 else None
@lru_cache(maxsize=1024)
def mlb_games(player,season,group,day):
 data=get('https://statsapi.mlb.com/api/v1/people/'+str(player)+'/stats',params={'stats':'gameLog','group':group,'season':season}).json()
 return [g for block in data.get('stats',[]) for g in block.get('splits',[])]
def mlb_history(offers,now):
 rows=[]
 for name in offers['player'].drop_duplicates():
  player=mlb_id(name)
  if not player:continue
  for stat,group,field in [('H','hitting','hits'),('SO','pitching','strikeOuts')]:
   if stat not in set(offers.loc[offers['player']==name,'stat']):continue
   for season in (now.year,now.year-1):
    for g in mlb_games(player,season,group,now.date().isoformat()):
     value=g.get('stat',{}).get(field);date=g.get('date','')
     if date and date<now.date().isoformat() and isinstance(value,(int,float)) and value>=0:rows.append(('MLB',name,stat,date,value))
 result=pd.DataFrame(rows,columns=COLUMNS).drop_duplicates();result.attrs.update(source='MLB Stats API (reserve)',photos={})
 return result

