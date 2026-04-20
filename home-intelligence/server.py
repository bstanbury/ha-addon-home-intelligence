#!/usr/bin/env python3
"""TARS Home Intelligence v1.0.0
Cross-system correlation engine. Builds context from all add-ons and HA,
then makes coordinated decisions across lighting, music, vacuum, security.

Endpoints:
  GET  /health           — Health + all add-on connectivity
  GET  /context          — Current home context (the brain's view)
  GET  /decide           — What would the brain do right now?
  POST /arrive           — Trigger coordinated arrival sequence
  POST /depart           — Trigger coordinated departure sequence
  POST /mood/<mood>      — Set whole-home mood (chill/energetic/focus/party/sleep/romantic/movie)
  GET  /cooper           — Is Cooper visiting? Schedule check
  POST /cooper/here      — Manual: Cooper arrived
  POST /cooper/gone      — Manual: Cooper left
  GET  /insights         — Cross-system insights and recommendations
  GET  /log              — Recent intelligence decisions
"""
import os,json,time,logging,threading
from datetime import datetime,timedelta
from flask import Flask,jsonify,request
import requests as http

HA_URL=os.environ.get('HA_URL','http://localhost:8123')
HA_TOKEN=os.environ.get('HA_TOKEN','')
API_PORT=int(os.environ.get('API_PORT','8093'))
VACUUM=os.environ.get('VACUUM_URL','http://localhost:8099')
SWITCHBOT=os.environ.get('SWITCHBOT_URL','http://localhost:8098')
SPOTIFY=os.environ.get('SPOTIFY_URL','http://localhost:8097')
HUE=os.environ.get('HUE_URL','http://localhost:8096')
ANALYTICS=os.environ.get('ANALYTICS_URL','http://localhost:8095')
DOORBELL=os.environ.get('DOORBELL_URL','http://localhost:8094')
COOPER_SCHED=os.environ.get('COOPER_SCHEDULE','')
POLL=int(os.environ.get('POLL_INTERVAL','15'))

app=Flask(__name__)
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logger=logging.getLogger('home-intelligence')

decision_log=[]
cooper_override=None  # None=use schedule, True=here, False=gone
DATA='/data/intelligence.json'

def load_data():
    try:
        if os.path.exists(DATA): return json.load(open(DATA))
    except: pass
    return {'decisions':[],'arrivals':[],'departures':[]}

def save_data(data):
    try: json.dump(data,open(DATA,'w'),indent=2)
    except: pass

def get(url,timeout=5):
    try:
        r=http.get(url,timeout=timeout)
        return r.json() if r.status_code==200 else None
    except: return None

def post(url,timeout=5):
    try:
        r=http.post(url,timeout=timeout)
        return r.json() if r.status_code==200 else None
    except: return None

def ha_get(path):
    try:
        r=http.get(f'{HA_URL}/api{path}',headers={'Authorization':f'Bearer {HA_TOKEN}'},timeout=10)
        return r.json() if r.status_code==200 else None
    except: return None

def ha_notify(title,msg):
    try: http.post(f'{HA_URL}/api/services/notify/mobile_app_bks_home_assistant_chatsworth',headers={'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'},json={'data':{'title':title,'message':msg}},timeout=5)
    except: pass

def is_cooper_here():
    if cooper_override is not None: return cooper_override
    # Parse schedule: fri_1600-mon_1100,tue_0800-1100,thu_1630-1930
    now=datetime.now()
    day=now.strftime('%a').lower()
    hour=now.hour*100+now.minute
    days_map={'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6}
    dow=now.weekday()
    for block in COOPER_SCHED.split(','):
        if '-' not in block: continue
        parts=block.strip().split('-')
        if len(parts)!=2: continue
        start_parts=parts[0].split('_')
        end_parts=parts[1].split('_') if '_' in parts[1] else [day,parts[1]]
        if len(start_parts)==2 and len(end_parts)==2:
            sd=days_map.get(start_parts[0],99)
            st=int(start_parts[1])
            ed=days_map.get(end_parts[0],99)
            et=int(end_parts[1])
            # Check if now falls in this range
            if sd<=dow<=ed:
                if sd==ed: # Same day
                    if st<=hour<=et: return True
                elif dow==sd and hour>=st: return True
                elif dow==ed and hour<=et: return True
                elif sd<dow<ed: return True
    return False

def build_context():
    """Build the complete home context from all systems."""
    ctx={}
    # Time
    now=datetime.now()
    ctx['time']={'hour':now.hour,'minute':now.minute,'day':now.strftime('%A'),'weekend':now.weekday()>=5}
    if now.hour<6: ctx['time']['period']='night'
    elif now.hour<9: ctx['time']['period']='morning_early'
    elif now.hour<12: ctx['time']['period']='morning_late'
    elif now.hour<17: ctx['time']['period']='afternoon'
    elif now.hour<21: ctx['time']['period']='evening'
    else: ctx['time']['period']='night'
    
    # Presence
    p=ha_get('/states/binary_sensor.iphone_presence')
    ctx['presence']={'home':p['state']=='on' if p else None}
    ctx['cooper']={'here':is_cooper_here(),'schedule_based':cooper_override is None}
    
    # Weather
    w=ha_get('/states/weather.forecast_home')
    ctx['weather']={'state':w['state'] if w else 'unknown','temp':w['attributes'].get('temperature') if w else None}
    
    # Climate (from analytics)
    climate=get(f'{ANALYTICS}/climate/now')
    ctx['climate']=climate or {}
    
    # Sleep quality (from analytics)
    sleep=get(f'{ANALYTICS}/sleep/last-night')
    ctx['sleep']={'grade':sleep.get('grade','?'),'score':sleep.get('score',0)} if sleep else {}
    
    # Vacuum
    vac=get(f'{VACUUM}/status')
    ctx['vacuum']={'state':vac.get('state','?'),'battery':vac.get('battery',0)} if vac else {}
    
    # Lock/Door (from switchbot)
    lock=get(f'{SWITCHBOT}/status/lock')
    ctx['lock']={'state':lock.get('lock','?'),'door':lock.get('door','?'),'battery':lock.get('battery',0)} if lock else {}
    
    # Music
    music=get(f'{SPOTIFY}/now-playing')
    ctx['music']={'state':music.get('state','?'),'title':music.get('title'),'source':music.get('source')} if music else {}
    
    # Security (from doorbell)
    door=get(f'{DOORBELL}/status')
    ctx['security']={'last_visitor':door.get('last_visitor',{}).get('type','none') if door else 'unknown'}
    
    return ctx

def decide(ctx):
    """Make coordinated decisions based on full context."""
    decisions=[]
    period=ctx.get('time',{}).get('period','unknown')
    home=ctx.get('presence',{}).get('home',False)
    cooper=ctx.get('cooper',{}).get('here',False)
    weather=ctx.get('weather',{}).get('state','unknown')
    sleep_score=ctx.get('sleep',{}).get('score',100)
    
    # Arrival intelligence
    if home and period=='evening' and sleep_score<70:
        decisions.append({'action':'music_mood','value':'chill','reason':f'Bad sleep last night (score {sleep_score}) + evening = calming vibes'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Warm lighting for recovery evening'})
    
    if home and weather in ['rainy','pouring']:
        decisions.append({'action':'music_mood','value':'rainy','reason':'Rain detected — lo-fi/ambient fits the vibe'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Cozy rainy day lighting'})
    
    if cooper:
        decisions.append({'action':'spotify_kids','value':True,'reason':'Cooper is visiting per schedule'})
        decisions.append({'action':'skip_vacuum','value':True,'reason':'No vacuum while Cooper is here'})
        decisions.append({'action':'hue_ambient','value':'sunset','reason':'Warm, kid-friendly lighting'})
    
    # CO2 + nice weather
    bedroom_co2=ctx.get('climate',{}).get('Bedroom',{}).get('co2',400)
    outdoor_temp=ctx.get('climate',{}).get('Outdoor',{}).get('temp',70)
    if bedroom_co2 and bedroom_co2>800 and outdoor_temp and 60<=outdoor_temp<=80:
        decisions.append({'action':'open_windows','value':True,'reason':f'CO2 {bedroom_co2}ppm + outdoor {outdoor_temp}°F = perfect for fresh air'})
    
    # Sleep prep
    if period=='night' and home and not cooper:
        decisions.append({'action':'music_mood','value':'sleep','reason':'Late night — transition to sleep music'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Wind-down lighting'})
    
    # Weekend morning
    if period=='morning_early' and ctx.get('time',{}).get('weekend'):
        decisions.append({'action':'skip_alarm','value':True,'reason':'Weekend — let sleep in'})
        decisions.append({'action':'music_mood','value':'morning_coffee','reason':'Lazy weekend morning'})
    
    return decisions

def execute_decisions(decisions):
    """Execute coordinated decisions across add-ons."""
    results=[]
    for d in decisions:
        action=d['action']
        value=d['value']
        try:
            if action=='music_mood': r=post(f'{SPOTIFY}/mood/{value}'); results.append({**d,'executed':bool(r)})
            elif action=='hue_ambient': r=post(f'{HUE}/ambient/{value}'); results.append({**d,'executed':bool(r)})
            elif action=='spotify_kids': r=post(f'{SPOTIFY}/kids'); results.append({**d,'executed':bool(r)})
            elif action=='open_windows':
                ha_notify('\U0001f32c\ufe0f Fresh Air',d['reason'])
                results.append({**d,'executed':True,'note':'Notification sent'})
            else: results.append({**d,'executed':False,'note':'No executor'})
        except: results.append({**d,'executed':False,'note':'Error'})
    return results

@app.route('/')
def index():
    return jsonify({'name':'TARS Home Intelligence','version':'1.0.0','cooper_here':is_cooper_here(),'decisions_today':len([d for d in decision_log if d.get('time','')[:10]==datetime.now().strftime('%Y-%m-%d')])})

@app.route('/health')
def health():
    addons={}
    for name,url in [('vacuum',VACUUM),('switchbot',SWITCHBOT),('spotify',SPOTIFY),('hue',HUE),('analytics',ANALYTICS),('doorbell',DOORBELL)]:
        r=get(f'{url}/health')
        addons[name]='ok' if r and r.get('status')=='ok' else 'unreachable'
    return jsonify({'status':'ok','addons':addons,'cooper_here':is_cooper_here()})

@app.route('/context')
def context():
    return jsonify(build_context())

@app.route('/decide')
def decide_endpoint():
    ctx=build_context()
    decisions=decide(ctx)
    return jsonify({'context_summary':{'period':ctx['time']['period'],'home':ctx['presence']['home'],'cooper':ctx['cooper']['here'],'weather':ctx['weather']['state'],'sleep':ctx['sleep']},'decisions':decisions})

@app.route('/arrive',methods=['POST','GET'])
def arrive():
    ctx=build_context()
    decisions=decide(ctx)
    results=execute_decisions(decisions)
    event={'type':'arrival','time':datetime.now().isoformat(),'decisions':results}
    decision_log.append(event)
    return jsonify(event)

@app.route('/depart',methods=['POST','GET'])
def depart():
    decisions=[]
    if not is_cooper_here():
        decisions.append({'action':'vacuum_start','value':True,'reason':'Departing, no one home'})
        r=post(f'{VACUUM}/start')
    decisions.append({'action':'lights_off','value':True,'reason':'Departure'})
    event={'type':'departure','time':datetime.now().isoformat(),'decisions':decisions}
    decision_log.append(event)
    return jsonify(event)

@app.route('/mood/<mood>',methods=['POST','GET'])
def set_mood(mood):
    mood_map={
        'chill':    {'music':'chill','hue':'candlelight','vol':12},
        'energetic':{'music':'energetic','hue':'neon','vol':20},
        'focus':    {'music':'focus','hue':'ocean','vol':8},
        'party':    {'music':'party','hue':'neon','vol':25},
        'sleep':    {'music':'sleep','hue':'candlelight','vol':8},
        'romantic': {'music':'romantic','hue':'sunset','vol':10},
        'movie':    {'music':None,'hue':'movie','vol':None},
        'rainy':    {'music':'rainy','hue':'candlelight','vol':10},
        'morning':  {'music':'morning_coffee','hue':None,'vol':12},
    }
    if mood not in mood_map:
        return jsonify({'error':f'Available: {list(mood_map.keys())}'}),400
    m=mood_map[mood]
    results=[]
    if m['music']: r=post(f'{SPOTIFY}/mood/{m["music"]}'); results.append({'action':'music','mood':m['music'],'ok':bool(r)})
    if m['hue']=='movie': r=post(f'{HUE}/movie'); results.append({'action':'hue','mode':'movie','ok':bool(r)})
    elif m['hue']: r=post(f'{HUE}/ambient/{m["hue"]}'); results.append({'action':'hue','preset':m['hue'],'ok':bool(r)})
    if m['vol']: r=post(f'{SPOTIFY}/volume/{m["vol"]}'); results.append({'action':'volume','level':m['vol'],'ok':bool(r)})
    event={'type':'mood','mood':mood,'time':datetime.now().isoformat(),'results':results}
    decision_log.append(event)
    return jsonify(event)

@app.route('/cooper')
def cooper_status():
    return jsonify({'here':is_cooper_here(),'override':cooper_override,'schedule':COOPER_SCHED})

@app.route('/cooper/here',methods=['POST','GET'])
def cooper_here():
    global cooper_override
    cooper_override=True
    post(f'{SPOTIFY}/kids')
    ha_notify('\U0001f466 Cooper Mode','Kids music on, vacuum disabled')
    return jsonify({'cooper':'here','kids_mode':True})

@app.route('/cooper/gone',methods=['POST','GET'])
def cooper_gone():
    global cooper_override
    cooper_override=False
    post(f'{SPOTIFY}/kids/off')
    ha_notify('\U0001f466 Cooper Left','Normal mode restored')
    return jsonify({'cooper':'gone','kids_mode':False})

@app.route('/insights')
def insights():
    ctx=build_context()
    tips=[]
    sleep=ctx.get('sleep',{})
    if sleep.get('score',100)<70: tips.append({'type':'sleep','tip':'Sleep score was low. Consider earlier bedtime, lower bedroom temp, or running air purifier.'})
    co2=ctx.get('climate',{}).get('Bedroom',{}).get('co2',400)
    if co2 and co2>600: tips.append({'type':'air','tip':f'Bedroom CO2 is {co2}ppm. Open windows or run air purifier before bed tonight.'})
    battery=ctx.get('lock',{}).get('battery',100)
    if battery and battery<30: tips.append({'type':'battery','tip':f'Front door lock battery at {battery}%. Replace soon.'})
    if not tips: tips.append({'type':'all_good','tip':'Everything looks great. No recommendations right now.'})
    return jsonify({'insights':tips,'context_period':ctx['time']['period'],'sleep_grade':sleep.get('grade','?')})

@app.route('/log')
def get_log():
    limit=request.args.get('limit',20,type=int)
    return jsonify(decision_log[-limit:])

if __name__=='__main__':
    logger.info(f'TARS Home Intelligence v1.0.0 on :{API_PORT}')
    logger.info(f'Cooper schedule: {COOPER_SCHED}')
    logger.info(f'Cooper currently here: {is_cooper_here()}')
    app.run(host='0.0.0.0',port=API_PORT,debug=False)
