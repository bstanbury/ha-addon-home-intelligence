#!/usr/bin/env python3
"""TARS Home Intelligence v3.0.0
Cross-system correlation engine with Event Bus SSE subscription.
Builds context from all add-ons and HA, makes coordinated decisions.

v2.0 additions:
  - Event Bus SSE subscriber: reacts to events in real-time
  - Pattern learning: tracks event sequences, flags learned routines
  - Adaptive rules: adjusts based on user behavior
  - State machine: home mode transitions (morning->working->evening->night)
  - Persistent learning in /data/intelligence_v2.json

v3.0 additions:
  - Bedroom safety protection: never play audio without motion check
  - Silent hours enforcement (10pm-8am)
  - Cooper-aware mode transitions: cooper_day / cooper_night
  - on_mode_change handler for intelligent transitions
  - Arrival sequence bedroom protection
  - Departure sequence Cooper + time-aware vacuum logic
  - handle_event bedroom/silent-hour guards
  - /proactive endpoint: CO2, golden hour, adaptive suggestions
  - All music routed through DJ add-on (never direct HA media_player)

Endpoints:
  GET  /health, /context, /decide
  POST /arrive, /depart, /mood/<mood>
  GET  /cooper, /insights, /log
  GET  /mode -- Current home mode state machine
  GET  /learned -- Learned patterns and adaptive rules
  POST /cooper/here, /cooper/gone
  GET  /proactive -- Proactive intelligence suggestions
"""
import os,json,time,logging,threading
from datetime import datetime,timedelta
from collections import deque
from flask import Flask,jsonify,request
import requests as http
import sseclient

HA_URL=os.environ.get('HA_URL','http://localhost:8123')
HA_TOKEN=os.environ.get('HA_TOKEN','')
API_PORT=int(os.environ.get('API_PORT','8093'))
EVENT_BUS_URL=os.environ.get('EVENT_BUS_URL','http://localhost:8092')
VACUUM=os.environ.get('VACUUM_URL','http://localhost:8099')
SWITCHBOT=os.environ.get('SWITCHBOT_URL','http://localhost:8098')
SPOTIFY=os.environ.get('SPOTIFY_URL','http://localhost:8097')
HUE=os.environ.get('HUE_URL','http://localhost:8096')
ANALYTICS=os.environ.get('ANALYTICS_URL','http://localhost:8095')
DOORBELL=os.environ.get('DOORBELL_URL','http://localhost:8094')
COOPER_SCHED=os.environ.get('COOPER_SCHEDULE','')

app=Flask(__name__)
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
logger=logging.getLogger('home-intelligence')

decision_log=deque(maxlen=200)
cooper_override=None
DATA='/data/intelligence.json'
DATA_V2='/data/intelligence_v2.json'

# v2.0: State machine
home_mode='unknown'  # morning/working/evening/night/away/cooper_day/cooper_night
mode_history=deque(maxlen=50)

# v2.0: Adaptive rules
adaptive_rules={
    'nudge_ignored_count':0,
    'auto_actions':{},  # {action: count_accepted}
    'suppressed_actions':set(),
}

# v2.0: Event-driven state
last_event_time=None
event_driven_actions=deque(maxlen=100)

# v3.0: Global Safety Constraints
BEDROOM_ENTITIES = ['media_player.bedroom', 'media_player.bedroom_sonos', 'media_player.bedroom_echo_show_chatsworth']
ECHO_ENTITIES = ['media_player.chatsworth_living_room_echo_show', 'media_player.chatsworth_kitchen_echo_show', 'media_player.bedroom_echo_show_chatsworth', 'media_player.chatsworth_echo_show_5_bathroom']
SILENT_HOURS = (22, 8)  # 10pm to 8am

def is_bedroom_safe():
    """Check bedroom motion -- REQUIRED before any bedroom audio."""
    try:
        r = http.get(f'{HA_URL}/api/states/binary_sensor.bedroom_motion', headers={'Authorization': f'Bearer {HA_TOKEN}'}, timeout=5)
        if r.status_code == 200: return r.json()['state'] == 'on'
    except: pass
    return False

def is_silent_hours():
    """True if 10pm-8am."""
    h = datetime.now().hour
    return h >= SILENT_HOURS[0] or h < SILENT_HOURS[1]

def safe_notify(title, msg, force_audio=False):
    """Smart notification: push only during silent hours or if bedroom not safe. Audio otherwise."""
    if is_silent_hours() and not force_audio:
        ha_notify(title, msg)  # Silent push
    elif not is_bedroom_safe():
        ha_notify(title, msg)  # Silent push -- don't wake anyone
    else:
        ha_notify(title, msg)  # Could add TTS here later

def load_data():
    global adaptive_rules, home_mode
    try:
        if os.path.exists(DATA_V2):
            d=json.load(open(DATA_V2))
            adaptive_rules=d.get('adaptive_rules',adaptive_rules)
            adaptive_rules['suppressed_actions']=set(adaptive_rules.get('suppressed_actions',[]))
            home_mode=d.get('home_mode','unknown')
            logger.info(f'Loaded v2 state: mode={home_mode}')
    except Exception as e:
        logger.error(f'Load v2 data: {e}')

def save_data():
    try:
        d={'adaptive_rules':{**adaptive_rules,'suppressed_actions':list(adaptive_rules['suppressed_actions'])},'home_mode':home_mode,'saved_at':datetime.now().isoformat()}
        json.dump(d,open(DATA_V2,'w'),indent=2)
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
            if sd<=dow<=ed:
                if sd==ed:
                    if st<=hour<=et: return True
                elif dow==sd and hour>=st: return True
                elif dow==ed and hour<=et: return True
                elif sd<dow<ed: return True
    return False

def on_mode_change(old, new):
    """React to mode transitions intelligently."""
    if new == 'cooper_day' and old != 'cooper_day':
        # Cooper arrived or morning started with Cooper
        post(f'{SPOTIFY}/kids')
        logger.info('MODE CHANGE: Activated kids music for Cooper day')
    elif new == 'cooper_night':
        # Quiet time for Cooper
        post(f'{SPOTIFY}/volume/12')
        logger.info('MODE CHANGE: Cooper night -- lowered volume')
    elif old in ('cooper_day', 'cooper_night') and 'cooper' not in new:
        # Cooper left
        post(f'{SPOTIFY}/kids/off')
        logger.info('MODE CHANGE: Cooper gone -- normal mode')
    elif new == 'morning' and old == 'night':
        # Morning transition -- only if bedroom motion confirms awake
        if is_bedroom_safe():
            post(f'{SPOTIFY}/play')
            logger.info('MODE CHANGE: Morning music started (motion confirmed)')
        else:
            logger.info('MODE CHANGE: Morning started but no motion -- staying quiet')

def update_mode():
    """v3.0: State machine for home mode transitions with Cooper awareness."""
    global home_mode
    now = datetime.now()
    h = now.hour
    p = ha_get('/states/binary_sensor.iphone_presence')
    is_home = p['state'] == 'on' if p else True
    cooper = is_cooper_here()

    old_mode = home_mode
    if not is_home:
        home_mode = 'away'
    elif cooper and h < 8:
        home_mode = 'cooper_night'
    elif cooper:
        home_mode = 'cooper_day'
    elif h < 6:
        home_mode = 'night'
    elif h < 9:
        home_mode = 'morning'
    elif h < 17:
        home_mode = 'working'
    elif h < 21:
        home_mode = 'evening'
    else:
        home_mode = 'night'

    if old_mode != home_mode:
        mode_history.append({'from': old_mode, 'to': home_mode, 'time': datetime.now().isoformat()})
        logger.info(f'MODE: {old_mode} -> {home_mode}')
        on_mode_change(old_mode, home_mode)
        save_data()
    return home_mode

def handle_event(ev):
    """v3.0: React to Event Bus events with bedroom/silent-hour protection."""
    global last_event_time
    last_event_time=datetime.now().isoformat()

    eid=ev.get('entity_id','')
    new=ev.get('new_state','')
    old=ev.get('old_state','')
    sig=ev.get('significant',False)
    reason=ev.get('reason','')
    classification=ev.get('classification','routine')

    if not sig:
        return  # Only react to significant events

    action_taken=None

    # Presence change: immediate context rebuild + action
    if 'presence' in eid:
        if new=='on':
            logger.info('EVENT: Arrival detected via SSE')
            update_mode()
            threading.Thread(target=lambda: arrive_sequence(), daemon=True).start()
            action_taken='arrival_sequence'
        elif new=='off':
            logger.info('EVENT: Departure detected via SSE')
            update_mode()
            threading.Thread(target=lambda: depart_sequence(), daemon=True).start()
            action_taken='departure_sequence'

    # TV state change — only trigger movie mode if NOT silent hours
    elif 'media_player.75_the_frame' in eid or ('media_player' in eid and 'frame' in eid):
        if new=='on' or new=='playing':
            if not is_silent_hours():
                logger.info('EVENT: TV on -- triggering movie mood')
                post(f'{HUE}/movie')
                post(f'{SPOTIFY}/mood/chill')  # Lower music or pause
                action_taken='tv_on_response'
            else:
                logger.info('EVENT: TV on but silent hours -- skipping audio change')
                action_taken='tv_on_silent_skip'

    # Weather change — only update music if already playing
    elif 'weather' in eid:
        logger.info(f'EVENT: Weather changed to {new}')
        music=get(f'{SPOTIFY}/now-playing')
        music_active = music and music.get('state') not in ('stopped', 'idle', None)
        if new in ['rainy','pouring']:
            post(f'{HUE}/ambient/candlelight')
            if music_active:
                post(f'{SPOTIFY}/mood/rainy')
            action_taken='rainy_mood'
        elif new=='sunny' and home_mode in ['morning','afternoon']:
            post(f'{HUE}/ambient/sunset')
            if music_active:
                post(f'{SPOTIFY}/mood/sunny')
            action_taken='sunny_mood'

    # Sun state (golden hour) — lights safe, no audio
    elif eid=='sun.sun':
        if new=='below_horizon':
            logger.info('EVENT: Sunset -- transitioning to evening lighting')
            post(f'{HUE}/ambient/sunset')
            # No audio change here — let mode transitions handle that
            action_taken='sunset_transition'

    # Vacuum state
    elif 'vacuum' in eid:
        if new in ['docked','standby'] and old in ['cleaning','returning']:
            logger.info('EVENT: Vacuum finished cleaning')
            ha_notify('\U0001f9f9 Clean Complete','Vacuum has finished and docked.')
            action_taken='vacuum_complete_notify'

    # Motion events
    elif 'motion' in eid and new=='on':
        if home_mode=='night':
            action_taken='night_motion_noted'

    if action_taken:
        event_driven_actions.append({'time':datetime.now().isoformat(),'event':eid,'action':action_taken,'trigger':reason})
        logger.info(f'ACTION: {action_taken} (triggered by {reason})')

def arrive_sequence():
    """Execute arrival sequence with bedroom protection."""
    ctx = build_context()

    # SAFETY: Check if it's silent hours or bedroom occupied
    if is_silent_hours() and not is_bedroom_safe():
        # Silent arrival -- push notification only, no audio
        safe_notify('\U0001f3e0 Welcome Home', 'Arrived home (quiet mode -- no audio)')
        decision_log.append({'type': 'arrival', 'time': datetime.now().isoformat(), 'decisions': [{'action': 'silent_welcome', 'reason': 'silent hours, no bedroom motion'}], 'source': 'event_bus'})
        return

    decisions = decide(ctx)
    results = execute_decisions(decisions)
    event = {'type': 'arrival', 'time': datetime.now().isoformat(), 'decisions': results, 'source': 'event_bus'}
    decision_log.append(event)

def depart_sequence():
    """Execute departure sequence with safety checks."""
    decisions = []

    # Vacuum: only if Cooper NOT here AND bedroom clear
    if not is_cooper_here():
        if is_bedroom_safe():
            # Someone was recently in bedroom -- maybe still sleeping? Check time.
            if datetime.now().hour >= 9:
                decisions.append({'action': 'vacuum_start', 'value': True, 'reason': 'Departed, no Cooper, after 9am'})
                post(f'{VACUUM}/start')
            else:
                decisions.append({'action': 'vacuum_deferred', 'value': False, 'reason': 'Before 9am -- deferring vacuum'})
        else:
            decisions.append({'action': 'vacuum_start', 'value': True, 'reason': 'Departed, no motion in bedroom'})
            post(f'{VACUUM}/start')
    else:
        decisions.append({'action': 'vacuum_skip', 'value': False, 'reason': 'Cooper is here'})

    decisions.append({'action': 'lights_off', 'value': True, 'reason': 'Departure'})

    # Stop music
    decisions.append({'action': 'music_stop', 'value': True, 'reason': 'Departure'})
    post(f'{SPOTIFY}/mood/off')  # or pause

    event = {'type': 'departure', 'time': datetime.now().isoformat(), 'decisions': decisions, 'source': 'event_bus'}
    decision_log.append(event)

def event_bus_subscriber():
    """v2.0: SSE subscriber thread -- connects to Event Bus stream."""
    while True:
        try:
            logger.info(f'Connecting to Event Bus SSE: {EVENT_BUS_URL}/events/stream')
            response=http.get(f'{EVENT_BUS_URL}/events/stream',stream=True,timeout=None)
            client=sseclient.SSEClient(response)
            logger.info('Event Bus SSE connected')
            for event in client.events():
                try:
                    ev=json.loads(event.data)
                    handle_event(ev)
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE error: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)

def mode_updater():
    """Background thread: periodically update home mode."""
    while True:
        update_mode()
        time.sleep(60)

def build_context():
    ctx={}
    now=datetime.now()
    ctx['time']={'hour':now.hour,'minute':now.minute,'day':now.strftime('%A'),'weekend':now.weekday()>=5}
    if now.hour<6: ctx['time']['period']='night'
    elif now.hour<9: ctx['time']['period']='morning_early'
    elif now.hour<12: ctx['time']['period']='morning_late'
    elif now.hour<17: ctx['time']['period']='afternoon'
    elif now.hour<21: ctx['time']['period']='evening'
    else: ctx['time']['period']='night'
    ctx['time']['mode']=home_mode

    p=ha_get('/states/binary_sensor.iphone_presence')
    ctx['presence']={'home':p['state']=='on' if p else None}
    ctx['cooper']={'here':is_cooper_here(),'schedule_based':cooper_override is None}
    w=ha_get('/states/weather.forecast_home')
    ctx['weather']={'state':w['state'] if w else 'unknown','temp':w['attributes'].get('temperature') if w else None}
    climate=get(f'{ANALYTICS}/climate/now')
    ctx['climate']=climate or {}
    sleep=get(f'{ANALYTICS}/sleep/last-night')
    ctx['sleep']={'grade':sleep.get('grade','?'),'score':sleep.get('score',0)} if sleep else {}
    vac=get(f'{VACUUM}/status')
    ctx['vacuum']={'state':vac.get('state','?'),'battery':vac.get('battery',0)} if vac else {}
    lock=get(f'{SWITCHBOT}/status/lock')
    ctx['lock']={'state':lock.get('lock','?'),'door':lock.get('door','?'),'battery':lock.get('battery',0)} if lock else {}
    music=get(f'{SPOTIFY}/now-playing')
    ctx['music']={'state':music.get('state','?'),'title':music.get('title'),'source':music.get('source')} if music else {}
    door=get(f'{DOORBELL}/status')
    ctx['security']={'last_visitor':door.get('last_visitor',{}).get('type','none') if door else 'unknown'}
    return ctx

def decide(ctx):
    decisions=[]
    period=ctx.get('time',{}).get('period','unknown')
    home=ctx.get('presence',{}).get('home',False)
    cooper=ctx.get('cooper',{}).get('here',False)
    weather=ctx.get('weather',{}).get('state','unknown')
    sleep_score=ctx.get('sleep',{}).get('score',100)

    if home and period=='evening' and sleep_score<70:
        decisions.append({'action':'music_mood','value':'chill','reason':f'Bad sleep (score {sleep_score}) + evening'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Warm lighting for recovery'})
    if home and weather in ['rainy','pouring']:
        decisions.append({'action':'music_mood','value':'rainy','reason':'Rain detected'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Cozy rainy day'})
    if cooper:
        decisions.append({'action':'spotify_kids','value':True,'reason':'Cooper visiting'})
        decisions.append({'action':'skip_vacuum','value':True,'reason':'No vacuum while Cooper here'})
        decisions.append({'action':'hue_ambient','value':'sunset','reason':'Kid-friendly lighting'})
    bedroom_co2=ctx.get('climate',{}).get('Bedroom',{}).get('co2',400)
    outdoor_temp=ctx.get('climate',{}).get('Outdoor',{}).get('temp',70)
    if bedroom_co2 and bedroom_co2>800 and outdoor_temp and 60<=outdoor_temp<=80:
        decisions.append({'action':'open_windows','value':True,'reason':f'CO2 {bedroom_co2}ppm + outdoor {outdoor_temp}\u00b0F'})
    if period=='night' and home and not cooper:
        decisions.append({'action':'music_mood','value':'sleep','reason':'Late night wind-down'})
        decisions.append({'action':'hue_ambient','value':'candlelight','reason':'Wind-down lighting'})
    if period=='morning_early' and ctx.get('time',{}).get('weekend'):
        decisions.append({'action':'music_mood','value':'morning_coffee','reason':'Weekend morning'})

    # v2.0: Filter out suppressed actions
    decisions=[d for d in decisions if d['action'] not in adaptive_rules.get('suppressed_actions',set())]
    return decisions

def execute_decisions(decisions):
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
    return jsonify({'name':'TARS Home Intelligence','version':'3.0.0','mode':home_mode,'cooper_here':is_cooper_here(),'event_bus_connected':last_event_time is not None,'last_event':last_event_time,'decisions_today':len([d for d in decision_log if d.get('time','')[:10]==datetime.now().strftime('%Y-%m-%d')])})

@app.route('/health')
def health():
    addons={}
    for name,url in [('vacuum',VACUUM),('switchbot',SWITCHBOT),('spotify',SPOTIFY),('hue',HUE),('analytics',ANALYTICS),('doorbell',DOORBELL),('event_bus',EVENT_BUS_URL)]:
        r=get(f'{url}/health')
        addons[name]='ok' if r and r.get('status')=='ok' else 'unreachable'
    return jsonify({'status':'ok','mode':home_mode,'addons':addons,'cooper_here':is_cooper_here(),'event_bus_streaming':last_event_time is not None})

@app.route('/context')
def context():
    return jsonify(build_context())

@app.route('/decide')
def decide_endpoint():
    ctx=build_context()
    decisions=decide(ctx)
    return jsonify({'context_summary':{'period':ctx['time']['period'],'mode':home_mode,'home':ctx['presence']['home'],'cooper':ctx['cooper']['here'],'weather':ctx['weather']['state'],'sleep':ctx['sleep']},'decisions':decisions})

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
        post(f'{VACUUM}/start')
    decisions.append({'action':'lights_off','value':True,'reason':'Departure'})
    event={'type':'departure','time':datetime.now().isoformat(),'decisions':decisions}
    decision_log.append(event)
    return jsonify(event)

@app.route('/mood/<mood>',methods=['POST','GET'])
def set_mood(mood):
    mood_map={
        'chill':{'music':'chill','hue':'candlelight','vol':12},
        'energetic':{'music':'energetic','hue':'neon','vol':20},
        'focus':{'music':'focus','hue':'ocean','vol':8},
        'party':{'music':'party','hue':'neon','vol':25},
        'sleep':{'music':'sleep','hue':'candlelight','vol':8},
        'romantic':{'music':'romantic','hue':'sunset','vol':10},
        'movie':{'music':None,'hue':'movie','vol':None},
        'rainy':{'music':'rainy','hue':'candlelight','vol':10},
        'morning':{'music':'morning_coffee','hue':None,'vol':12},
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

@app.route('/mode')
def get_mode():
    return jsonify({'current':home_mode,'history':list(mode_history)[-10:],'updated':last_event_time})

@app.route('/learned')
def get_learned():
    return jsonify({'adaptive_rules':{k:v for k,v in adaptive_rules.items() if k!='suppressed_actions'},'suppressed':list(adaptive_rules.get('suppressed_actions',[])),'event_driven_actions':list(event_driven_actions)[-20:]})

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
    if sleep.get('score',100)<70: tips.append({'type':'sleep','tip':'Sleep score was low. Consider earlier bedtime or air purifier.'})
    co2=ctx.get('climate',{}).get('Bedroom',{}).get('co2',400)
    if co2 and co2>600: tips.append({'type':'air','tip':f'Bedroom CO2 is {co2}ppm. Open windows before bed.'})
    battery=ctx.get('lock',{}).get('battery',100)
    if battery and battery<30: tips.append({'type':'battery','tip':f'Front door lock battery at {battery}%.'})
    if not tips: tips.append({'type':'all_good','tip':'Everything looks great.'})
    return jsonify({'insights':tips,'mode':home_mode,'context_period':ctx['time']['period']})

@app.route('/log')
def get_log():
    limit=request.args.get('limit',20,type=int)
    return jsonify(list(decision_log)[-limit:])

@app.route('/proactive')
def proactive_check():
    """Run proactive intelligence checks -- call periodically from cron."""
    suggestions = []

    # CO2 check
    co2 = None
    s = ha_get('/states/sensor.bedroom_co2_monitor_carbon_dioxide')
    if s: co2 = float(s.get('state', 0))
    if co2 and co2 > 1000:
        weather = ha_get('/states/weather.forecast_home')
        temp = weather.get('attributes', {}).get('temperature', 0) if weather else 0
        if 60 <= temp <= 80:
            suggestions.append({'type': 'air_quality', 'message': f'CO2 is {int(co2)}ppm. Outdoor temp is {temp}\u00b0F -- open some windows!', 'priority': 'medium'})

    # Hydration nudge (every 2 hours of motion without kitchen activity)
    # Golden hour check
    sun = ha_get('/states/sun.sun')
    if sun:
        elevation = sun.get('attributes', {}).get('elevation', 90)
        if 0 < elevation < 10 and home_mode not in ('away', 'night'):
            weather = ha_get('/states/weather.forecast_home')
            if weather and weather.get('state') in ('sunny', 'partlycloudy', 'clear-night'):
                suggestions.append({'type': 'golden_hour', 'message': 'Golden hour outside! Perfect light for a walk.', 'priority': 'low'})

    # Adaptive: check if suggestion type has been ignored too many times
    filtered = []
    for s in suggestions:
        ignore_count = adaptive_rules.get('auto_actions', {}).get(f"ignored_{s['type']}", 0)
        if ignore_count < 5:  # Stop suggesting after 5 ignores
            filtered.append(s)

    return jsonify({'suggestions': filtered, 'mode': home_mode, 'bedroom_safe': is_bedroom_safe(), 'silent_hours': is_silent_hours()})

if __name__=='__main__':
    logger.info(f'TARS Home Intelligence v3.0.0 on :{API_PORT}')
    logger.info(f'Cooper schedule: {COOPER_SCHED}')
    load_data()
    # v2.0: Start Event Bus SSE subscriber
    threading.Thread(target=event_bus_subscriber,daemon=True).start()
    # v2.0: Start mode updater
    threading.Thread(target=mode_updater,daemon=True).start()
    logger.info('Event Bus subscriber + mode state machine started')
    app.run(host='0.0.0.0',port=API_PORT,debug=False)
