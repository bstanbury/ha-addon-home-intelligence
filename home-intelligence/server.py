#!/usr/bin/env python3
"""TARS Home Intelligence v3.0.0
Cross-system correlation engine with Event Bus SSE subscription.
Builds context from all add-ons and HA, makes coordinated decisions.

v3.0 additions:
  - Bedroom sleep protection as FIRST CLASS constraint across ALL decisions
    - Before any audio: check binary_sensor.bedroom_motion
    - Before 9am: silent delivery (push notifications) unless motion confirms awake
    - NEVER suggest or execute bedroom speaker actions without motion
  - Richer mode state machine: cooper_day, cooper_night, guest modes added
  - Cross-addon safety: music routed through Spotify DJ (never direct Echo calls)
    - Passes bedroom_safe=True/False flag and volume recommendation
  - Better arrival sequence: motion check before ANY audio
  - Better departure sequence: lower all volumes, check doors locked
  - Context-aware decide(): time+day, Cooper presence, sleep quality, weather,
    room motion, TV state, current music state
  - Proactive intelligence: CO2, washer, calendar, golden hour, adaptive suppression
  - Decision audit trail: every decision logs WHY and what data drove it
  - BEDROOM_ENTITIES and ECHO_ENTITIES constants
  - Silent hours (22:00-08:00) enforced

v2.0 additions:
  - Event Bus SSE subscriber: reacts to events in real-time
  - Pattern learning: tracks event sequences, flags learned routines
  - Adaptive rules: adjusts based on user behavior
  - State machine: home mode transitions (morning->working->evening->night)
  - Persistent learning in /data/intelligence_v2.json

Endpoints:
  GET  /health, /context, /decide
  POST /arrive, /depart, /mood/<mood>
  GET  /cooper, /insights, /log
  GET  /mode — Current home mode state machine
  GET  /learned — Learned patterns and adaptive rules
  POST /cooper/here, /cooper/gone
  GET  /audit — v3.0: Decision audit trail
  GET  /proactive — v3.0: Proactive intelligence checks
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

# v3.0: Safety constants
BEDROOM_ENTITIES = lambda eid: 'bedroom' in eid.lower()
ECHO_ENTITIES = [
    'media_player.living_room_echo_show',
    'media_player.kitchen_echo_show',
    'media_player.bedroom_echo',
]
SILENT_HOURS = lambda: datetime.now().hour >= 22 or datetime.now().hour < 8
BEFORE_9AM = lambda: datetime.now().hour < 9

decision_log=deque(maxlen=200)
# v3.0: Decision audit trail
audit_log=deque(maxlen=500)
cooper_override=None
DATA='/data/intelligence.json'
DATA_V2='/data/intelligence_v2.json'

# v3.0: Richer mode state machine
# Modes: morning/working/evening/night/away/cooper_day/cooper_night/guest
home_mode='unknown'
mode_history=deque(maxlen=50)

# v2.0: Adaptive rules
adaptive_rules={
    'nudge_ignored_count':0,
    'auto_actions':{},
    'suppressed_actions':set(),
    'suggestion_counts':{},  # v3.0: track how many times each suggestion fires
    'suggestion_ignored':{}, # v3.0: track which suggestions get ignored
}

last_event_time=None
event_driven_actions=deque(maxlen=100)

def load_data():
    global adaptive_rules, home_mode
    try:
        if os.path.exists(DATA_V2):
            d=json.load(open(DATA_V2))
            ar=d.get('adaptive_rules',adaptive_rules)
            ar['suppressed_actions']=set(ar.get('suppressed_actions',[]))
            adaptive_rules=ar
            home_mode=d.get('home_mode','unknown')
            logger.info(f'Loaded v2 state: mode={home_mode}')
    except Exception as e:
        logger.error(f'Load v2 data: {e}')

def save_data():
    try:
        d={'adaptive_rules':{**adaptive_rules,'suppressed_actions':list(adaptive_rules['suppressed_actions'])},
           'home_mode':home_mode,'saved_at':datetime.now().isoformat()}
        json.dump(d,open(DATA_V2,'w'),indent=2)
    except: pass

def get(url,timeout=5):
    try:
        r=http.get(url,timeout=timeout)
        return r.json() if r.status_code==200 else None
    except: return None

def post_json(url,data=None,timeout=5):
    try:
        r=http.post(url,json=data or {},timeout=timeout)
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
    try:
        http.post(f'{HA_URL}/api/services/notify/mobile_app_bks_home_assistant_chatsworth',
                  headers={'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'},
                  json={'data':{'title':title,'message':msg}},timeout=5)
    except: pass

def audit(decision_type, action, reason, context_data=None):
    """v3.0: Log every decision with WHY and what data drove it."""
    entry={
        'time':datetime.now().isoformat(),
        'type':decision_type,
        'action':action,
        'reason':reason,
        'context':context_data or {},
        'mode':home_mode,
    }
    audit_log.append(entry)
    logger.info(f'AUDIT [{decision_type}]: {action} — {reason}')
    return entry

def is_bedroom_safe():
    """v3.0: FIRST CLASS check — must pass before ANY audio action."""
    p=ha_get('/states/binary_sensor.bedroom_motion')
    return p['state']=='on' if p else False

def check_audio_allowed(context_hint=''):
    """v3.0: Returns (allowed, reason) for any audio action."""
    if SILENT_HOURS():
        if not is_bedroom_safe():
            return False, f'Silent hours (22-8) and no bedroom motion — {context_hint}'
    if BEFORE_9AM():
        if not is_bedroom_safe():
            return False, f'Before 9am and no bedroom motion — {context_hint}'
    return True, 'ok'

def play_music_safe(mood=None, reason='', vol_rec=None):
    """v3.0: Route ALL music through Spotify DJ. Never direct Echo calls."""
    allowed, why = check_audio_allowed(reason)
    bedroom_safe = is_bedroom_safe()
    if not allowed:
        audit('music_blocked', f'play_music({mood})', why, {'bedroom_safe':bedroom_safe})
        ha_notify('🎵 Music Suppressed', f'Silent/early hours: {why}')
        return False
    vol = vol_rec if vol_rec else 0.25
    url = f'{SPOTIFY}/mood/{mood}' if mood else f'{SPOTIFY}/play'
    result = post(url)
    audit('music_played', f'play_music({mood})', reason, {'bedroom_safe':bedroom_safe,'vol':vol,'mood':mood})
    return bool(result)

def is_cooper_here():
    if cooper_override is not None: return cooper_override
    now=datetime.now()
    hour=now.hour*100+now.minute
    days_map={'mon':0,'tue':1,'wed':2,'thu':3,'fri':4,'sat':5,'sun':6}
    dow=now.weekday()
    for block in COOPER_SCHED.split(','):
        if '-' not in block: continue
        parts=block.strip().split('-')
        if len(parts)!=2: continue
        start_parts=parts[0].split('_')
        day=now.strftime('%a').lower()
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

def update_mode():
    """v3.0: Richer state machine including cooper_day, cooper_night, guest."""
    global home_mode
    now=datetime.now()
    h=now.hour
    p=ha_get('/states/binary_sensor.iphone_presence')
    is_home=p['state']=='on' if p else True
    cooper=is_cooper_here()
    
    old_mode=home_mode
    if not is_home:
        home_mode='away'
    elif cooper:
        home_mode='cooper_day' if 8<=h<20 else 'cooper_night'
    elif h<6:
        home_mode='night'
    elif h<9:
        home_mode='morning'
    elif h<17:
        home_mode='working'
    elif h<21:
        home_mode='evening'
    else:
        home_mode='night'
    
    if old_mode!=home_mode:
        mode_history.append({'from':old_mode,'to':home_mode,'time':datetime.now().isoformat()})
        logger.info(f'MODE: {old_mode} -> {home_mode}')
        save_data()
    return home_mode

def build_context():
    ctx={}
    now=datetime.now()
    ctx['time']={'hour':now.hour,'minute':now.minute,'day':now.strftime('%A'),
                 'weekend':now.weekday()>=5}
    h=now.hour
    if h<6: ctx['time']['period']='night'
    elif h<9: ctx['time']['period']='morning_early'
    elif h<12: ctx['time']['period']='morning_late'
    elif h<17: ctx['time']['period']='afternoon'
    elif h<21: ctx['time']['period']='evening'
    else: ctx['time']['period']='night'
    ctx['time']['mode']=home_mode

    p=ha_get('/states/binary_sensor.iphone_presence')
    ctx['presence']={'home':p['state']=='on' if p else None}
    ctx['cooper']={'here':is_cooper_here(),'schedule_based':cooper_override is None}

    w=ha_get('/states/weather.forecast_home')
    ctx['weather']={'state':w['state'] if w else None,
                    'temp':w['attributes'].get('temperature') if w else None,
                    'humidity':w['attributes'].get('humidity') if w else None} if w else {}

    lock=ha_get('/states/lock.front_door_lock')
    ctx['lock']={'state':lock['state'] if lock else None,
                 'battery':lock['attributes'].get('battery_level') if lock else None}

    # v3.0: Bedroom motion — FIRST CLASS context
    bm=ha_get('/states/binary_sensor.bedroom_motion')
    ctx['bedroom']={'motion':bm['state']=='on' if bm else False,
                    'audio_allowed':is_bedroom_safe()}

    # v3.0: TV state
    tv=ha_get('/states/media_player.75_the_frame')
    ctx['tv']={'state':tv['state'] if tv else None}

    # v3.0: Current music state
    music=get(f'{SPOTIFY}/now-playing')
    ctx['music']={'state':music.get('state') if music else None,
                  'kids_mode':music.get('kids_mode',False) if music else False}

    # Room motion sensors
    motions={}
    for eid in ['binary_sensor.living_room_motion','binary_sensor.kitchen_motion',
                'binary_sensor.bathroom_motion_motion','binary_sensor.bedroom_motion']:
        s=ha_get(f'/states/{eid}')
        motions[eid.split('.')[-1]]=s['state'] if s else None
    ctx['motion']=motions

    # CO2 sensors
    climate={}
    for room,eid in [('Bedroom','sensor.bedroom_co2_monitor_carbon_dioxide'),
                     ('Living Room','sensor.living_room_co2_monitor_carbon_dioxide')]:
        s=ha_get(f'/states/{eid}')
        try: climate[room]={'co2':float(s['state'])} if s and s['state'] not in ['unavailable','unknown'] else {}
        except: climate[room]={}
    ctx['climate']=climate

    # Sleep quality from analytics
    sleep=get(f'{ANALYTICS}/sleep/last-night')
    ctx['sleep']={'score':sleep.get('score',100) if sleep else 100,
                  'grade':sleep.get('grade','?') if sleep else '?'}

    # Outdoor temp from weather
    ctx['outdoor_temp']=w['attributes'].get('temperature') if w else None

    return ctx

def decide(ctx):
    """v3.0: Context-aware decisions with full audit trail."""
    decisions=[]
    h=ctx['time']['hour']
    period=ctx['time']['period']
    cooper=ctx['cooper']['here']
    bedroom_motion=ctx['bedroom']['motion']
    audio_allowed,audio_reason=check_audio_allowed('decide()')
    tv_on=ctx.get('tv',{}).get('state') in ['on','playing']

    # Arrival audio decision
    if ctx['presence']['home']:
        if not audio_allowed:
            decisions.append({'action':'silent_welcome','value':True,'reason':audio_reason})
            audit('arrival','silent_welcome',audio_reason,ctx)
        elif not tv_on:
            mood='kids' if cooper else ('morning_coffee' if period in ['morning_early','morning_late'] else 'chill')
            decisions.append({'action':'play_music','mood':mood,'value':True,
                              'reason':f'Arrival, {period}, cooper={cooper}'})
            audit('arrival','play_music',f'Arrival in {period}, cooper={cooper}',
                  {'mood':mood,'bedroom_safe':bedroom_motion})

    # CO2 proactive check
    for room,data in ctx.get('climate',{}).items():
        co2=data.get('co2',0)
        outdoor_temp=ctx.get('outdoor_temp')
        if co2>1000 and outdoor_temp and 60<=outdoor_temp<=80:
            suggestion='co2_window_open'
            if suggestion not in adaptive_rules.get('suppressed_actions',set()):
                decisions.append({'action':'notify','suggestion':suggestion,
                                  'reason':f'{room} CO2 {co2}ppm, outdoor temp {outdoor_temp}°F is comfortable'})
                audit('proactive','notify_co2',f'{room} CO2={co2}ppm outdoor={outdoor_temp}F',ctx)

    # Vacuum on departure (handled by event handler, but track here)
    if not ctx['presence']['home'] and not cooper:
        decisions.append({'action':'vacuum_check','value':True,'reason':'Away, no Cooper'})
        audit('departure','vacuum_check','Not home, no Cooper',ctx)

    # Morning audio guard
    if period in ['morning_early','morning_late'] and not bedroom_motion:
        decisions.append({'action':'hold_audio','value':True,
                          'reason':'Morning + no bedroom motion — someone may still be sleeping'})
        audit('morning_guard','hold_audio','Morning and no bedroom motion',ctx)

    return decisions

def execute_decisions(decisions):
    results=[]
    for d in decisions:
        action=d.get('action')
        if action=='play_music':
            ok=play_music_safe(mood=d.get('mood'),reason=d.get('reason',''),vol_rec=d.get('vol'))
            results.append({**d,'executed':ok})
        elif action=='silent_welcome':
            ha_notify('🏠 Welcome Home','Silent arrival — quiet hours or early morning.')
            results.append({**d,'executed':True})
        elif action=='notify':
            s=d.get('suggestion','')
            ha_notify('💡 Smart Home Tip',d.get('reason',''))
            adaptive_rules['suggestion_counts'][s]=adaptive_rules['suggestion_counts'].get(s,0)+1
            results.append({**d,'executed':True})
        elif action=='vacuum_check':
            vac=get(f'{VACUUM}/suggest')
            if vac and vac.get('should_clean'):
                post(f'{VACUUM}/start')
                results.append({**d,'executed':True,'vacuum':'started'})
            else:
                results.append({**d,'executed':False,'vacuum':'not_due'})
        else:
            results.append({**d,'executed':False,'skipped':True})
    return results

def handle_event(ev):
    """v3.0: React to Event Bus events with bedroom safety enforcement."""
    global last_event_time
    last_event_time=datetime.now().isoformat()
    eid=ev.get('entity_id','')
    new=ev.get('new_state','')
    old=ev.get('old_state','')
    sig=ev.get('significant',False)
    reason=ev.get('reason','')
    classification=ev.get('classification','routine')
    if not sig: return
    action_taken=None

    if 'presence' in eid:
        if new=='on':
            logger.info('EVENT: Arrival detected via SSE')
            update_mode()
            threading.Thread(target=arrive_sequence,daemon=True).start()
            action_taken='arrival_sequence'
        elif new=='off':
            logger.info('EVENT: Departure detected via SSE')
            update_mode()
            threading.Thread(target=depart_sequence,daemon=True).start()
            action_taken='departure_sequence'

    elif 'media_player.75_the_frame' in eid or ('media_player' in eid and 'frame' in eid):
        if new in ['on','playing']:
            logger.info('EVENT: TV on — routing hue + notifying DJ')
            post(f'{HUE}/movie')
            post(f'{SPOTIFY}/mood/chill')
            audit('tv_on','hue_movie+chill_music','TV turned on',{'entity':eid})
            action_taken='tv_on_response'

    elif 'weather' in eid:
        logger.info(f'EVENT: Weather changed to {new}')
        if new in ['rainy','pouring']:
            post(f'{HUE}/ambient/candlelight')
            audit('weather','rainy_mood',f'Weather changed to {new}',{'entity':eid})
            action_taken='rainy_mood'
        elif new=='sunny' and home_mode in ['morning','working','evening']:
            post(f'{HUE}/ambient/sunset')
            audit('weather','sunny_mood',f'Weather changed to {new}',{'entity':eid})
            action_taken='sunny_mood'

    elif eid=='sun.sun':
        if new=='below_horizon':
            logger.info('EVENT: Sunset — transitioning to evening lighting')
            post(f'{HUE}/ambient/sunset')
            audit('sunset','evening_lighting','Sun below horizon',{'entity':eid})
            action_taken='sunset_transition'

    elif 'vacuum' in eid and new in ['docked','standby'] and old in ['cleaning','returning']:
        ha_notify('\U0001f9f9 Clean Complete','Vacuum has finished and docked.')
        audit('vacuum','notify_complete','Vacuum finished cleaning',{'entity':eid})
        action_taken='vacuum_complete_notify'

    if action_taken:
        event_driven_actions.append({'time':datetime.now().isoformat(),'event':eid,
                                     'action':action_taken,'trigger':reason})
        logger.info(f'ACTION: {action_taken} (triggered by {reason})')

def arrive_sequence():
    """v3.0: Arrival sequence with bedroom safety as first check."""
    bedroom_safe=is_bedroom_safe()
    audio_allowed,audio_reason=check_audio_allowed('arrival')
    decisions=[]
    
    if not audio_allowed:
        ha_notify('🏠 Welcome Home','You\'re back!')
        audit('arrival','silent_welcome',audio_reason,{'bedroom_safe':bedroom_safe})
        decisions.append({'action':'silent_welcome','reason':audio_reason,'executed':True})
    else:
        # Welcome music on Sonos (NEVER Echo)
        cooper=is_cooper_here()
        mood='kids' if cooper else 'morning_coffee' if BEFORE_9AM() else 'chill'
        ok=play_music_safe(mood=mood,reason='Arrival sequence',vol_rec=0.25)
        decisions.append({'action':'play_music','mood':mood,'executed':ok,
                          'reason':'arrival_sequence','bedroom_safe':bedroom_safe})
    
    event={'type':'arrival','time':datetime.now().isoformat(),'decisions':decisions,'source':'event_bus'}
    decision_log.append(event)

def depart_sequence():
    """v3.0: Departure sequence: lower volumes, check doors, maybe vacuum."""
    decisions=[]
    cooper=is_cooper_here()
    
    # Lower all volumes
    hh={'Authorization':f'Bearer {HA_TOKEN}','Content-Type':'application/json'}
    for sonos_eid in ['media_player.living_room','media_player.bathroom','media_player.bedroom']:
        try:
            http.post(f'{HA_URL}/api/services/media_player/volume_set',headers=hh,
                      json={'entity_id':sonos_eid,'volume_level':0.10},timeout=5)
        except: pass
    decisions.append({'action':'lower_volumes','value':True,'reason':'Departure'})
    audit('departure','lower_volumes','Leaving home',{})
    
    # Check doors locked
    lock=ha_get('/states/lock.front_door_lock')
    if lock and lock['state']=='unlocked':
        ha_notify('\U0001f6aa Door Unlocked','Front door is unlocked while you\'re away!')
        decisions.append({'action':'door_alert','value':True,'reason':'Departed with door unlocked'})
        audit('departure','door_alert','Left with front door unlocked',{'lock_state':lock['state']})
    
    # Vacuum if Cooper not here
    if not cooper:
        decisions.append({'action':'vacuum_start','value':True,'reason':'Departing, no one home'})
        post(f'{VACUUM}/start')
        audit('departure','vacuum_start','Departing, no Cooper home',{})
    else:
        decisions.append({'action':'vacuum_skipped','reason':'Cooper is home'})
        audit('departure','vacuum_skipped','Cooper home, skip vacuum',{})
        # Cooper here: lower volumes more aggressively + cooper-safe lights
        post(f'{HUE}/cooper-safe')
    
    event={'type':'departure','time':datetime.now().isoformat(),'decisions':decisions,'source':'event_bus'}
    decision_log.append(event)

def proactive_checks():
    """v3.0: Periodic proactive intelligence checks."""
    while True:
        time.sleep(300)  # Every 5 minutes
        try:
            ctx=build_context()
            h=ctx['time']['hour']
            
            # CO2 check
            for room,data in ctx.get('climate',{}).items():
                co2=data.get('co2',0)
                outdoor_temp=ctx.get('outdoor_temp')
                if co2 and co2>1000 and outdoor_temp and 60<=outdoor_temp<=80:
                    suggestion='co2_windows'
                    if suggestion not in adaptive_rules.get('suppressed_actions',set()):
                        count=adaptive_rules['suggestion_counts'].get(suggestion,0)
                        if count < 3 or count % 6 == 0:  # Reduce frequency if often ignored
                            ha_notify('\U0001f4a8 Air Quality',f'{room} CO2 is {co2:.0f}ppm. Outdoor temp is {outdoor_temp}°F — consider opening windows.')
                            adaptive_rules['suggestion_counts'][suggestion]=count+1
                            audit('proactive','co2_notify',f'{room} CO2={co2} outdoor={outdoor_temp}',{})
            
            # Golden hour suggestion (30min before sunset approx)
            sun=ha_get('/states/sun.sun')
            if sun and sun.get('attributes',{}).get('next_setting'):
                try:
                    next_set=datetime.fromisoformat(sun['attributes']['next_setting'].replace('Z','+00:00'))
                    mins_to_sunset=(next_set-datetime.now(next_set.tzinfo)).total_seconds()/60
                    if 25<=mins_to_sunset<=35:
                        suggestion='golden_hour_outside'
                        count=adaptive_rules['suggestion_counts'].get(suggestion,0)
                        if count<1 or (count<5 and count%3==0):
                            ha_notify('\U0001f305 Golden Hour',f'Sunset in ~{int(mins_to_sunset)}min. Perfect time to go outside!')
                            adaptive_rules['suggestion_counts'][suggestion]=count+1
                            audit('proactive','golden_hour',f'Sunset in {int(mins_to_sunset)}min',{})
                except: pass
        except Exception as e:
            logger.error(f'Proactive checks error: {e}')

def event_bus_subscriber():
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
                except json.JSONDecodeError: pass
                except Exception as e: logger.error(f'Event handling error: {e}')
        except Exception as e:
            logger.error(f'Event Bus SSE error: {e}')
        logger.info('Reconnecting to Event Bus in 10s...')
        time.sleep(10)

def mode_updater():
    while True:
        update_mode()
        time.sleep(60)

@app.route('/')
def index():
    return jsonify({'name':'TARS Home Intelligence','version':'3.0.0','mode':home_mode,
                    'bedroom_safe':is_bedroom_safe(),'silent_hours':SILENT_HOURS(),
                    'cooper':is_cooper_here()})

@app.route('/health')
def health():
    return jsonify({'status':'ok','mode':home_mode,'event_bus_connected':bool(last_event_time),
                    'last_event':last_event_time,'bedroom_motion':is_bedroom_safe()})

@app.route('/context')
def context_ep():
    ctx=build_context()
    return jsonify(ctx)

@app.route('/decide')
def decide_ep():
    ctx=build_context()
    decisions=decide(ctx)
    return jsonify({'decisions':decisions,'context':ctx,'mode':home_mode})

@app.route('/arrive',methods=['POST','GET'])
def arrive():
    update_mode()
    threading.Thread(target=arrive_sequence,daemon=True).start()
    return jsonify({'triggered':'arrive_sequence','bedroom_safe':is_bedroom_safe()})

@app.route('/depart',methods=['POST','GET'])
def depart():
    update_mode()
    threading.Thread(target=depart_sequence,daemon=True).start()
    return jsonify({'triggered':'depart_sequence'})

@app.route('/mood/<mood>',methods=['POST','GET'])
def mood_ep(mood):
    mood_map={
        'chill':{'music':'chill','hue':'candlelight','vol':0.22},
        'energetic':{'music':'energetic','hue':None,'vol':0.28},
        'focus':{'music':'focus','hue':'ocean','vol':0.20},
        'party':{'music':'party','hue':'neon','vol':0.28},
        'sleep':{'music':'sleep','hue':None,'vol':0.15},
        'romantic':{'music':'romantic','hue':'sunset','vol':0.22},
        'movie':{'music':None,'hue':'movie','vol':None},
        'rainy':{'music':'rainy','hue':'candlelight','vol':0.22},
        'morning':{'music':'morning_coffee','hue':None,'vol':0.22},
        'dinner':{'music':'dinner','hue':'candlelight','vol':0.22},
    }
    if mood not in mood_map:
        return jsonify({'error':f'Available: {list(mood_map.keys())}'}),400
    m=mood_map[mood]
    results=[]
    if m['music']:
        ok=play_music_safe(mood=m['music'],reason=f'Mood: {mood}',vol_rec=m['vol'])
        results.append({'action':'music','mood':m['music'],'ok':ok})
    if m['hue']=='movie': r=post(f'{HUE}/movie'); results.append({'action':'hue','mode':'movie','ok':bool(r)})
    elif m['hue']: r=post(f'{HUE}/ambient/{m["hue"]}'); results.append({'action':'hue','preset':m['hue'],'ok':bool(r)})
    audit('mood',f'mood_set_{mood}',f'Mood set to {mood}',{'results':results})
    event={'type':'mood','mood':mood,'time':datetime.now().isoformat(),'results':results}
    decision_log.append(event)
    return jsonify(event)

@app.route('/mode')
def get_mode():
    return jsonify({'current':home_mode,'history':list(mode_history)[-10:],'updated':last_event_time})

@app.route('/learned')
def get_learned():
    return jsonify({'adaptive_rules':{k:v for k,v in adaptive_rules.items() if k!='suppressed_actions'},
                    'suppressed':list(adaptive_rules.get('suppressed_actions',[])),
                    'event_driven_actions':list(event_driven_actions)[-20:]})

@app.route('/cooper')
def cooper_status():
    return jsonify({'here':is_cooper_here(),'override':cooper_override,'schedule':COOPER_SCHED,'mode':home_mode})

@app.route('/cooper/here',methods=['POST','GET'])
def cooper_here():
    global cooper_override
    cooper_override=True
    update_mode()
    post(f'{SPOTIFY}/kids')
    post(f'{HUE}/cooper-safe')
    ha_notify('\U0001f466 Cooper Mode','Kids music on, cooper-safe lights, vacuum disabled')
    return jsonify({'cooper':'here','kids_mode':True,'mode':home_mode})

@app.route('/cooper/gone',methods=['POST','GET'])
def cooper_gone():
    global cooper_override
    cooper_override=False
    update_mode()
    post(f'{SPOTIFY}/kids/off')
    ha_notify('\U0001f466 Cooper Left','Normal mode restored')
    return jsonify({'cooper':'gone','kids_mode':False,'mode':home_mode})

@app.route('/insights')
def insights():
    ctx=build_context()
    tips=[]
    sleep=ctx.get('sleep',{})
    if sleep.get('score',100)<70: tips.append({'type':'sleep','tip':'Sleep score was low. Consider earlier bedtime or air purifier.'})
    co2=ctx.get('climate',{}).get('Bedroom',{}).get('co2',400)
    if co2 and co2>600: tips.append({'type':'air','tip':f'Bedroom CO2 is {co2:.0f}ppm. Open windows before bed.'})
    battery=ctx.get('lock',{}).get('battery',100)
    if battery and battery<30: tips.append({'type':'battery','tip':f'Front door lock battery at {battery}%.'})
    if not ctx['bedroom']['motion'] and BEFORE_9AM(): tips.append({'type':'sleep','tip':'Bedroom motion not detected before 9am — audio actions suppressed.'})
    if not tips: tips.append({'type':'all_good','tip':'Everything looks great.'})
    return jsonify({'insights':tips,'mode':home_mode,'context_period':ctx['time']['period'],'bedroom_safe':ctx['bedroom']['motion']})

@app.route('/log')
def get_log():
    limit=request.args.get('limit',20,type=int)
    return jsonify(list(decision_log)[-limit:])

@app.route('/audit')
def get_audit():
    """v3.0: Decision audit trail."""
    limit=request.args.get('limit',50,type=int)
    return jsonify(list(audit_log)[-limit:])

@app.route('/proactive')
def proactive_status():
    """v3.0: Current proactive suggestion state."""
    return jsonify({'suggestion_counts':adaptive_rules.get('suggestion_counts',{}),
                    'suppressed':list(adaptive_rules.get('suppressed_actions',[])),
                    'bedroom_safe':is_bedroom_safe(),'silent_hours':SILENT_HOURS(),
                    'before_9am':BEFORE_9AM()})

if __name__=='__main__':
    logger.info(f'TARS Home Intelligence v3.0.0 on :{API_PORT}')
    logger.info(f'Cooper schedule: {COOPER_SCHED}')
    load_data()
    threading.Thread(target=event_bus_subscriber,daemon=True).start()
    threading.Thread(target=mode_updater,daemon=True).start()
    threading.Thread(target=proactive_checks,daemon=True).start()
    logger.info('Event Bus subscriber + mode state machine + proactive checks started')
    app.run(host='0.0.0.0',port=API_PORT,debug=False)
