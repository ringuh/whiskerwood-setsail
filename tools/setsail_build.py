"""Generates the Blueprint paste text (T3D) for the SetSail mod into tools/out/.
Usage: python tools/setsail_build.py   (needs the modkit's jmap, see t3d.py)

What the mod does: fishing, trade, guano, exploration and naval ships leave their dock by themselves when they are ready.

How (game 0.7.207, UE 5.8):
  - Fishing / trade docks are GridActors navaldock_fishing_C / navaldock_trade_C (building table rows
    navalfishingdock / navaltradeDock, ModAPI table "GridActors"). Classes come from the table's GridActor column.
  - The dock window class UI_WorkDockView has CalcHudState() -> WorkDock_UIData (workDockPhase,
    allowDeparture, ...). One invisible UI_WorkDockView is pointed at a dock with SetObjectPropertyByName
    (Context isn't Blueprint-writable). Phase 5 = WORKSHIP_READY_TO_DEPLOY.
  - The window's Send to Sea button sends HudAction "dispatchShipFromDock" (no params); we send the same
    through ReceiveHudAction on the invisible view.

Build v1 = PROBE: the game has no Blueprint event for "ship ready". This build binds every candidate event
(each dock's ActivityTracker.uponStateChange, NauticalOcean.OnNoticeChange / OnNoticeClear, ModAPI onDayStart)
and on each one checks the known docks (only the two dock classes, never all buildings): logs their state and
sends a ready ship to sea. The debug log shows which event fires when a ship becomes ready.

Assets (/Game/Mods/SetSail/): BP_Startup (Actor), BP_MapLoad (Actor), PAL_SetSail (chunk 26).
Debug log needs Saved\\mods\\SetSailConfig\\debug.txt (any text).
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from t3d import *

OUT = os.path.join(os.path.dirname(__file__), 'out')
os.makedirs(OUT, exist_ok=True)
MOD = 'SetSail'
VERSION = '1.1'
BUILD = 'v21'
TRACE = False   # TEMP: ungated log lines at BeginPlay / OnLoaded to find out what runs. Remove before release.
M = '/Game/Mods/%s/' % MOD
STARTUP, MAPLOAD = M + 'BP_Startup', M + 'BP_MapLoad'
KSL = '/Script/Engine.KismetSystemLibrary'
KML = '/Script/Engine.KismetMathLibrary'
KSTR = '/Script/Engine.KismetStringLibrary'
KAL = '/Script/Engine.KismetArrayLibrary'
GS = '/Script/Engine.GameplayStatics'
API = '/Script/SystemCore.ModAPI'
PA = '/Script/ProjectArco.'
AWB, VIEW = PA + 'ArcoWidgetBase', PA + 'UI_WorkDockView'
UIDATA = PA + 'WorkDock_UIData'
TRACKER = '/Script/SystemCore.ActivityTracker'
OCEAN = '/Script/NauticalKit.NauticalOcean'
NOTICE = '/Script/NauticalKit.NauticalNotice'
TABLE = 'GridActors'
OPT_FISH, OPT_TRADE, OPT_GUANO, OPT_SCOUT, OPT_NAVAL = 'SetSail_Fishing', 'SetSail_Trade', 'SetSail_Guano', 'SetSail_Scout', 'SetSail_Naval'
CHECKS_PER_DAY = 6        # day start + 5 more, spread over the daytime part of the day (whiskers sleep at night)
EVENING_MARGIN = '10.0'   # game seconds: the last check runs this long before evening (WorldTime.TimeUntilNextPhase)
AUTO_TAG = '[auto]'       # only ships whose name contains this (any case) are sent
OLD_TAG = '[hold]'        # v9-v12 tag, removed when the toggle renames a ship
WORLDTIME = PA + 'WorldTime'
TOGGLE, WAITER = M + 'WBP_SailToggle', M + 'WBP_SailWaiter'
TOGGLE_CLS = "/Script/UMG.WidgetBlueprintGeneratedClass'%s.%s_C'" % (TOGGLE, TOGGLE.split('/')[-1])
WAITER_CLS = "/Script/UMG.WidgetBlueprintGeneratedClass'%s.%s_C'" % (WAITER, WAITER.split('/')[-1])
MAPLOAD_CLS = "/Script/Engine.BlueprintGeneratedClass'%s.%s_C'" % (M + 'BP_MapLoad', 'BP_MapLoad')
TOGGLE_T, WAITER_T, MAPLOAD_T = T('object', obj=TOGGLE_CLS), T('object', obj=WAITER_CLS), T('object', obj=MAPLOAD_CLS)
UWT, WIDGET_T, PANEL = OBJ('/Script/UMG.UserWidget'), OBJ('/Script/UMG.Widget'), '/Script/UMG.PanelWidget'
LABEL_ON, LABEL_OFF = 'Auto-sail: On', 'Auto-sail: Off'
COLOR_ON = '(R=0.815000,G=0.672000,B=0.381000,A=1.000000)'    # E9D7A6 (sRGB) in linear
COLOR_OFF = '(R=0.262000,G=0.205000,B=0.125000,A=1.000000)'   # 8C7D63 (sRGB) in linear
ANCHOR_NAME = 'ButtonEditName'   # the dock window's rename (pencil) button; the toggle goes into the same panel
DIAG = []  # was: [('navalhuntingdock', 'hunting'), ('navalwarDock', 'war'), ('navalStorageDock', 'storage')]  # TEMP: only counted + logged
READY_PHASE = '5'   # EWorkdockPhase::WORKSHIP_READY_TO_DEPLOY
DOCKS = [('navalfishingdock', 'fishing', OPT_FISH), ('navaltradeDock', 'trade', OPT_TRADE), ('navalGuanoDock', 'guano', OPT_GUANO),
         ('navalScoutDock', 'exploration', OPT_SCOUT),   # 0.2: Exploration Dock (navaldock_scout_C), goal 'Leave port and await orders'
         ('navalwarDock', 'naval', OPT_NAVAL)]           # 0.2: Naval Dock (navaldock_war_C), warships  # table row, label, option id (stored in DockOpt)

ACTOR = OBJ('/Script/Engine.Actor')
ACLS = CLS('/Script/Engine.Actor')
VIEW_T = OBJ(VIEW)

# ---- PinSubCategoryMemberReference support (delegate pins)
_orig_fmt = Pin.fmt
def _fmt(self):
    s = _orig_fmt(self)
    mr = getattr(self, 'memref', None)
    if mr: s = s.replace('PinType.PinSubCategoryMemberReference=()', 'PinType.PinSubCategoryMemberReference=(%s)' % mr, 1)
    return s
Pin.fmt = _fmt


def modapi(g, x, y, name='Api'):
    return g.call(API + ':GetModAPI', name, x, y)


def api_call(g, fn, x, y, name=None, **kw):
    a = modapi(g, x - 250, y + 150, name=(name or fn) + 'Api')
    n = g.call(API + ':' + fn, name or fn, x, y, **kw)
    link(a['ReturnValue'], n['self'])
    return n


class _Gate(Node):
    def __init__(self, entry, exit_):
        self._e, self._x = entry, exit_; self.name = entry.node.name
    def __getitem__(self, k):
        return {'execute': self._e, 'then': self._x}[k]


def log(g, x, y, msg_pin=None, msg=None, name='Log'):
    """Debug-only log line (Debug = SetSailConfig\\debug.ini not empty), joined back by a reroute knot."""
    dg = g.get('Debug', BOOL, x - 200, y + 120, name=name + 'DebugGet')
    br = g.branch(x - 150, y, name + 'IfDebug'); link(dg['Debug'], br['Condition'])
    n = api_call(g, 'LogMessage', x, y, name=name, doPrependDate='true')
    if msg_pin: link(msg_pin, n['Msg'])
    elif msg: n.set('Msg', msg)
    ex(br, n)
    k = g.add(BG + 'K2Node_Knot', name + 'Join', [], x + 250, y - 40)
    k.pin('InputPin', EXEC); k.pin('OutputPin', EXEC, out=True)
    link(n['then'], k['InputPin']); link(br['else'], k['InputPin'])
    return _Gate(br['execute'], k['OutputPin'])


def concat(g, x, y, *parts):
    cur = None
    for i, p in enumerate(parts):
        if cur is None:
            if isinstance(p, str):
                c = g.call(KSTR + ':Concat_StrStr', 'Cat', x, y); c.set('A', p); cur = c['ReturnValue']; continue
            cur = p; continue
        c = g.call(KSTR + ':Concat_StrStr', 'Cat', x + 40 * i, y + 30 * i)
        link(cur, c['A'])
        if isinstance(p, str): c.set('B', p)
        else: link(p, c['B'])
        cur = c['ReturnValue']
    return cur


def istr(g, pin, x, y, name):
    n = g.call(KSTR + ':Conv_IntToString', name, x, y); link(pin, n['inInt']); return n['ReturnValue']


def bstr(g, pin, x, y, name):
    n = g.call(KSTR + ':Conv_BoolToString', name, x, y); link(pin, n['InBool']); return n['ReturnValue']


def nstr(g, pin, x, y, name):
    n = g.call(KSTR + ':Conv_NameToString', name, x, y); link(pin, n['InName']); return n['ReturnValue']


def is_valid(g, pin, x, y, name='Valid'):
    n = g.call(KSL + ':IsValid', name, x, y); link(pin, n['Object']); return n['ReturnValue']


def struct_node(g, kind, spath, show, x, y, name):
    props = J()[spath]['properties']
    n = g.add(BG + 'K2Node_%sStruct' % kind, name,
              ['StructType="/Script/CoreUObject.ScriptStruct\'%s\'"' % spath, 'bMadeAfterOverridePinRemoval=True'], x, y)
    for i, p in enumerate(props):
        n.header.append('ShowPinForProperties(%d)=(PropertyName="%s",bShowPin=%s,bCanToggleVisibility=True)' % (i, p['name'], p['name'] in show))
    sname = spath.split('.')[-1]
    if kind == 'Break':
        n.pin(sname, STRUCT(spath))
    for p in props:
        if p['name'] in show:
            d = None
            if kind == 'Make':
                d = {'IntProperty': '0', 'StrProperty': '', 'BoolProperty': 'false'}.get(p['type'])
            n.pin(p['name'], prop_type(p), out=(kind == 'Break'), default=d)
    if kind == 'Make':
        n.pin(sname, STRUCT(spath), out=True)
    return n


def bind(g, target_pin, owner, delegate, sig_pkg, sig, event_node, x, y, name):
    n = g.add(BG + 'K2Node_AddDelegate', name,
              ['DelegateReference=(MemberParent="%s",MemberName="%s")' % (cls_ref(owner), delegate)], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True)
    n.pin('self', OBJ(owner), friendly='NSLOCTEXT("K2Node", "Target", "Target")')
    d = n.pin('Delegate', T('delegate'))
    d.memref = 'MemberParent="/Script/CoreUObject.Package\'%s\'",MemberName="%s"' % (sig_pkg, sig)
    link(target_pin, n['self'])
    ev = event_node['OutputDelegate']
    ev.memref = 'MemberParent="/Script/Engine.BlueprintGeneratedClass\'%s.%s_C\'",MemberName="%s"' % (MAPLOAD, MAPLOAD.split('/')[-1], event_node.name)
    link(ev, d)
    return n


def arr_fn(g, fn, elem_t, x, y, name):
    n = g.add(BG + 'K2Node_CallArrayFunction', name,
              ['FunctionReference=(MemberParent="%s",MemberName="%s")' % (cls_ref(KAL), fn)], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True)
    n.pin('self', OBJ(KAL), defobj='/Script/Engine.Default__KismetArrayLibrary', hidden=True, friendly='NSLOCTEXT("K2Node", "Target", "Target")')
    at = ARR(elem_t); at['ref'] = True
    n.pin('TargetArray', at)
    it = dict(elem_t); it['ref'] = True; it['const'] = True
    if fn == 'Array_Add':
        n.pin('NewItem', it); n.pin('ReturnValue', INT, out=True)
    return n


def array_item(g, arr_pin, elem_t, idx_pin, x, y, name):
    n = g.add(BG + 'K2Node_GetArrayItem', name, ['bReturnByRefDesired=False'], x, y)
    at = ARR(elem_t); at['ref'] = True; at['const'] = True
    n.pin('Array', at); n.pin('Dimension 1', INT, default='0'); n.pin('Output', elem_t, out=True)
    link(arr_pin, n['Array']); link(idx_pin, n['Dimension 1'])
    return n['Output']


def class_cast(g, x, y, name):
    n = g.add(BG + 'K2Node_ClassDynamicCast', name, ['TargetType="%s"' % cls_ref('/Script/Engine.Actor')], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True); n.pin('CastFailed', EXEC, out=True)
    n.pin('Class', CLS('/Script/CoreUObject.Object'))
    n.pin('AsActor', ACLS, out=True)
    n.pin('bSuccess', BOOL, out=True, hidden=True)
    return n


def knot(g, x, y, name):
    k = g.add(BG + 'K2Node_Knot', name, [], x, y)
    k.pin('InputPin', EXEC); k.pin('OutputPin', EXEC, out=True)
    return k


# =========================================================================== BP_Startup
g = Graph(STARTUP)
bp = g.event('/Script/Engine.Actor', 'ReceiveBeginPlay', [], 'Begin', 0, 0)
prev = bp
for i, (oid, disp, desc) in enumerate([
        (OPT_FISH, 'Docks - Fishing ships leave when ready',
         'A fishing ship that is repaired, crewed and supplied leaves its dock by itself, the same as clicking Send to Sea.'),
        (OPT_TRADE, 'Docks - Trade ships leave when ready',
         'A trade ship that is repaired, crewed, loaded and allowed to depart leaves its dock by itself, the same as clicking Send to Sea.'),
        (OPT_GUANO, 'Docks - Guano ships leave when ready',
         'A guano ship that is repaired, crewed and supplied leaves its dock by itself, the same as clicking Send to Sea.')]):
    vals = g.add(BG + 'K2Node_MakeArray', 'Vals%d' % i, ['NumInputs=2'], 300 + 700 * i, 250)
    vals.pin('Array', ARR(STR), out=True)
    vals.pin('[0]', STR, default='On'); vals.pin('[1]', STR, default='Off')
    reg = api_call(g, 'RegisterModOptions', 600 + 700 * i, 0, name='Reg%d' % i,
                   optionId=oid, optionDisplayName=disp, DefaultValue='On', optionDescription=desc)
    link(vals['Array'], reg['Values'])
    ex(prev, reg); prev = reg
open(OUT + '/BP_Startup.txt', 'w', encoding='utf-8').write(g.text())

# =========================================================================== BP_MapLoad
# Variables: Ready (Boolean), SetupTries (Integer), Rescan (Boolean) [1.1], Debug (Boolean), View (UI_WorkDockView object ref), Docks (Actor object ref ARRAY), DockOpt (String ARRAY), Tries (Integer), RetryPending (Boolean), DockClasses (Actor class ARRAY), DockKinds (String ARRAY), Target (Panel Widget ref), Toggle (WBP_SailToggle ref), Waiter (WBP_SailWaiter ref), Win (User Widget ref), Injected (User Widget ref), Anchor (Widget ref)
g = Graph(MAPLOAD)

# ---- BeginPlay: bind ModAPI events
bp = g.event('/Script/Engine.Actor', 'ReceiveBeginPlay', [], 'Begin', 0, -3000)
api = modapi(g, 200, -2750, name='BindApi')
evL = g.custom_event('OnLoaded', [], 0, -2400)
evD = g.custom_event('OnDayStart', [('Day', INT)], 0, 1400)
evA = g.custom_event('OnActivity', [], 0, 1800)
evN = g.custom_event('OnNotice', [('noticeData', STRUCT(NOTICE))], 0, 2200)
evC = g.custom_event('OnNoticeCleared', [('clearedNotice', INT)], 0, 2800)
b0 = bind(g, api['ReturnValue'], API, 'onLoadingFinished', '/Script/SystemCore', 'ModAPI_OnEvent__DelegateSignature', evL, 400, -3000, 'BindLoaded')
if TRACE:
    t0 = api_call(g, 'LogMessage', 200, -3300, name='TraceBeginPlay', doPrependDate='true', Msg='SetSail TRACE %s: BP_MapLoad BeginPlay' % BUILD)
    ex(bp, t0); ex(t0, b0)
else:
    ex(bp, b0)
b1 = bind(g, api['ReturnValue'], API, 'onDayStart', '/Script/SystemCore', 'ModAPI_OnDayStart__DelegateSignature', evD, 700, -3000, 'BindDay'); ex(b0, b1)

# ---- Setup guard (1.1): a NEW game doesn't deliver onLoadingFinished to BP_MapLoad, so setup also runs from
#      BeginPlay. BeginPlay (after the binds) and OnLoaded enter the same guard: Ready -> nothing; player controller
#      and the GridActors table there -> Ready = true -> setup once; else one-shot 1 s timer back into OnLoaded
#      (max 10 tries, SetupTries; 'Tries' is already the checks-left counter).
gr = g.branch(-300, -2000, 'BrReady'); link(g.get('Ready', BOOL, -450, -1850, name='ReadyGet')['Ready'], gr['Condition'])
ex(b1, gr); ex(evL, gr)
pcG = g.call(GS + ':GetPlayerController', 'PCGuard', -300, -1700)
pcv = g.call(KSL + ':IsValid', 'PCGuardValid', -150, -1700); link(pcG['ReturnValue'], pcv['Object'])
htb = api_call(g, 'HasDataTable', -150, -1550, name='GuardHasTable', datatableName=TABLE)
gok = g.call(KML + ':BooleanAND', 'GuardOk', 50, -1650); link(pcv['ReturnValue'], gok['A']); link(htb['ReturnValue'], gok['B'])
gp = g.branch(-50, -2000, 'BrCanSetup'); link(gok['ReturnValue'], gp['Condition']); ex(gr, htb, 'else'); ex(htb, gp)   # HasDataTable is impure
srd = g.setv('Ready', BOOL, 150, -2000, value='true', name='SetReady'); ex(gp, srd)
stg0 = g.get('SetupTries', INT, 0, -1350, name='SetupTriesGet')
stl = g.call(KML + ':Less_IntInt', 'SetupTriesLeft', 150, -1350, B='10'); link(stg0['SetupTries'], stl['A'])
bst = g.branch(150, -1550, 'BrSetupRetry'); link(stl['ReturnValue'], bst['Condition']); ex(gp, bst, 'else')
sta = g.call(KML + ':Add_IntInt', 'SetupTriesPlus', 300, -1400, B='1'); link(stg0['SetupTries'], sta['A'])
sst = g.setv('SetupTries', INT, 400, -1550, name='SetSetupTries'); link(sta['ReturnValue'], sst['SetupTries']); ex(bst, sst)
stm = g.call(KSL + ':K2_SetTimer', 'RetrySetup', 650, -1550, FunctionName='OnLoaded', Time='1.000000', bLooping='false')
stn = g.add(BG + 'K2Node_Self', 'MeSetupTimer', [], 500, -1400); stn.pin('self', T('object', sub='self'), out=True); link(stn['self'], stm['Object'])
ex(sst, stm)

# ---- Setup (once): debug switch, find docks, bind candidate events
READS = [(MOD + 'Config', 'debug')]   # ModAPI.ReadModTextFile appends '.txt': reads Saved\\mods\\SetSailConfig\\debug.txt
prevx = srd; parts = []
for i, (mn, fn) in enumerate(READS):
    r = api_call(g, 'ReadModTextFile', 300 + 300 * i, -2400, name='ReadDebugFile%d' % i, modName=mn, Filename=fn)
    ex(prevx, r); prevx = r; parts.append(r['ReturnValue'])
allt = concat(g, 1300, -2250, *parts)
dfe = g.call(KSTR + ':IsEmpty', 'DebugFileEmpty', 1550, -2250); link(allt, dfe['InString'])
dfn = g.call(KML + ':Not_PreBool', 'DebugFileThere', 1750, -2250); link(dfe['ReturnValue'], dfn['A'])
sdb = g.setv('Debug', BOOL, 1600, -2400, name='SetDebug'); link(dfn['ReturnValue'], sdb['Debug'])
if TRACE:
    segs = ['SetSail TRACE %s: OnLoaded fired, debug.txt length' % BUILD]
    for i, (mn, fn) in enumerate(READS):
        tl = g.call(KSTR + ':Len', 'DebugLen%d' % i, 1200, -2900 + 80 * i); link(parts[i], tl['S'])
        segs += [' (%s, %s)=' % (mn, fn), istr(g, tl['ReturnValue'], 1300, -2900 + 80 * i, 'DebugLenStr%d' % i)]
    tm = concat(g, 1400, -2900, *segs)
    t1 = api_call(g, 'LogMessage', 1300, -2600, name='TraceLoaded', doPrependDate='true'); link(tm, t1['Msg'])
    ex(prevx, t1); ex(t1, sdb)
else:
    ex(prevx, sdb)
lr = log(g, 2000, -2400, msg='SetSail %s (%s): game ready, looking for docks' % (VERSION, BUILD), name='LogReady'); ex(sdb, lr)
# ---- v21: dock scan, also re-run at onLoadingFinished. At a save load BeginPlay runs before the save's docks exist
#      (v20 logged "docks found: 0" at every load), so the guard's Ready branch (= OnLoaded after setup) sets
#      Rescan = true and runs the scan again; the scan clears the dock lists first, and after a rescan it only
#      checks the docks (no second ocean bind / view / toggle / waiter).
sr0 = g.setv('Rescan', BOOL, 2250, -2550, value='false', name='FirstScan'); ex(lr, sr0)
sr1 = g.setv('Rescan', BOOL, -50, -2550, value='true', name='MarkRescan'); ex(gr, sr1)
scanin = knot(g, 2450, -2600, 'ScanStart'); link(sr0['then'], scanin['InputPin']); link(sr1['then'], scanin['InputPin'])
prev = (scanin, 'OutputPin')
for ci, (cvar, ct) in enumerate([('Docks', ACTOR), ('DockOpt', STR), ('DockClasses', ACLS), ('DockKinds', STR)]):
    cl = arr_fn(g, 'Array_Clear', ct, 2550 + 200 * ci, -2750, 'Clear' + cvar)
    link(g.get(cvar, ARR(ct), 2550 + 200 * ci, -2900, name=cvar + 'Clr')[cvar], cl['TargetArray'])
    ex(prev[0], cl, prev[1]); prev = (cl, 'then')
X = 1400
for i, (row, label, optid) in enumerate(DOCKS):
    Y = -2400 + 0 * i
    x0 = X + 2600 * i
    rc = api_call(g, 'ReadDataTableValue', x0, Y, name='ReadClass_' + label, datatableName=TABLE, rowId=row, ColumnName='GridActor')
    ex(prev[0], rc, prev[1])
    scp = g.call(KSL + ':MakeSoftClassPath', 'ClassPath_' + label, x0 + 250, Y + 200); link(rc['ReturnValue'], scp['PathString'])
    scr = g.call(KSL + ':Conv_SoftClassPathToSoftClassRef', 'ClassRef_' + label, x0 + 450, Y + 200); link(scp['ReturnValue'], scr['SoftClassPath'])
    lca = g.call(KSL + ':LoadClassAsset_Blocking', 'LoadClass_' + label, x0 + 300, Y); link(scr['ReturnValue'], lca['AssetClass']); ex(rc, lca)
    cc = class_cast(g, x0 + 550, Y, 'AsActorClass_' + label); link(lca['ReturnValue'], cc['Class']); ex(lca, cc)
    gaa = g.call(GS + ':GetAllActorsOfClass', 'Docks_' + label, x0 + 800, Y)
    gaa['OutActors'].t = ARR(ACTOR)
    link(cc['AsActor'], gaa['ActorClass'])
    ac1 = arr_fn(g, 'Array_Add', ACLS, x0 + 650, Y - 250, 'AddClass_' + label)
    link(g.get('DockClasses', ARR(ACLS), x0 + 650, Y - 400, name='ClassesA_' + label)['DockClasses'], ac1['TargetArray']); link(cc['AsActor'], ac1['NewItem']); ex(cc, ac1)
    ac2 = arr_fn(g, 'Array_Add', STR, x0 + 850, Y - 250, 'AddClassKind_' + label)
    link(g.get('DockKinds', ARR(STR), x0 + 850, Y - 400, name='KindsA_' + label)['DockKinds'], ac2['TargetArray']); ac2.set('NewItem', label); ex(ac1, ac2)
    ex(ac2, gaa)
    lp = g.macro('ForEachLoop', ACTOR, x0 + 1050, Y, name='DockLoop_' + label); link(gaa['OutActors'], lp['Array']); ex(gaa, lp, 'then', 'Exec')
    el = lp['Array Element']
    a1 = arr_fn(g, 'Array_Add', ACTOR, x0 + 1300, Y + 300, 'AddDock_' + label)
    link(g.get('Docks', ARR(ACTOR), x0 + 1300, Y + 500, name='DocksA_' + label)['Docks'], a1['TargetArray']); link(el, a1['NewItem']); ex(lp, a1, 'LoopBody')
    a2 = arr_fn(g, 'Array_Add', STR, x0 + 1550, Y + 300, 'AddKind_' + label)
    link(g.get('DockOpt', ARR(STR), x0 + 1550, Y + 500, name='KindA_' + label)['DockOpt'], a2['TargetArray']); a2.set('NewItem', label); ex(a1, a2)
    tc = g.call('/Script/Engine.Actor:GetComponentByClass', 'Tracker_' + label, x0 + 1550, Y + 650, ComponentClass=TRACKER); link(el, tc['self'])
    tc['ReturnValue'].t = OBJ(TRACKER)
    bt = g.branch(x0 + 1800, Y + 300, 'BrTracker_' + label); link(is_valid(g, tc['ReturnValue'], x0 + 1800, Y + 650, 'TrackerValid_' + label), bt['Condition']); ex(a2, bt)
    bnd = bind(g, tc['ReturnValue'], TRACKER, 'uponStateChange', '/Script/SystemCore', 'UponActivityStateChange__DelegateSignature', evA, x0 + 2050, Y + 300, 'BindActivity_' + label)
    ex(bt, bnd)
    dn = g.call(KSL + ':GetDisplayName', 'DockName_' + label, x0 + 2050, Y + 800); link(el, dn['Object'])
    m1 = concat(g, x0 + 2300, Y + 800, 'SetSail:   %s dock ' % label, dn['ReturnValue'], ' has an ActivityTracker, bound')
    l1 = log(g, x0 + 2300, Y + 300, msg_pin=m1, name='LogTracker_' + label); ex(bnd, l1)
    m2 = concat(g, x0 + 2300, Y + 1100, 'SetSail:   %s dock ' % label, dn['ReturnValue'], ' has NO ActivityTracker')
    l2 = log(g, x0 + 2050, Y + 550, msg_pin=m2, name='LogNoTracker_' + label); ex(bt, l2, 'else')
    cnt = g.arr('Array_Length', ACTOR, x0 + 1300, Y - 300, name='Count_' + label, pure=True)
    link(gaa['OutActors'], cnt['TargetArray'])
    mc = concat(g, x0 + 1500, Y - 300, 'SetSail: %s docks found: ' % label, istr(g, cnt['ReturnValue'], x0 + 1300, Y - 150, 'CountStr_' + label))
    lc = log(g, x0 + 1300, Y - 500, msg_pin=mc, name='LogCount_' + label); ex(lp, lc, 'Completed')
    mf = concat(g, x0 + 800, Y - 700, 'SetSail: no class for %s (table %s row %s): ' % (label, TABLE, row), rc['ReturnValue'])
    lf = log(g, x0 + 800, Y - 500, msg_pin=mf, name='LogNoClass_' + label); ex(cc, lf, 'CastFailed')
    j = knot(g, x0 + 2400, Y - 600, 'NextDock_' + label)
    link(lc['then'], j['InputPin']); link(lf['then'], j['InputPin'])
    prev = (j, 'OutputPin')
# TEMP diagnostics: how many docks of other WorkDock kinds exist (not handled)
for j, (row, label) in enumerate(DIAG):
    x0 = X + 2600 * len(DOCKS) + 1400 * j; Y = -3400
    rc = api_call(g, 'ReadDataTableValue', x0, Y, name='DiagClass_' + label, datatableName=TABLE, rowId=row, ColumnName='GridActor')
    ex(prev[0], rc, prev[1])
    scp = g.call(KSL + ':MakeSoftClassPath', 'DiagPath_' + label, x0 + 250, Y + 200); link(rc['ReturnValue'], scp['PathString'])
    scr = g.call(KSL + ':Conv_SoftClassPathToSoftClassRef', 'DiagRef_' + label, x0 + 450, Y + 200); link(scp['ReturnValue'], scr['SoftClassPath'])
    lca = g.call(KSL + ':LoadClassAsset_Blocking', 'DiagLoad_' + label, x0 + 300, Y); link(scr['ReturnValue'], lca['AssetClass']); ex(rc, lca)
    cc = class_cast(g, x0 + 550, Y, 'DiagCast_' + label); link(lca['ReturnValue'], cc['Class']); ex(lca, cc)
    gaa = g.call(GS + ':GetAllActorsOfClass', 'DiagDocks_' + label, x0 + 800, Y); gaa['OutActors'].t = ARR(ACTOR)
    link(cc['AsActor'], gaa['ActorClass']); ex(cc, gaa)
    cnt = g.arr('Array_Length', ACTOR, x0 + 800, Y + 300, name='DiagCount_' + label, pure=True); link(gaa['OutActors'], cnt['TargetArray'])
    mc = concat(g, x0 + 1000, Y + 300, 'SetSail: (not handled) %s docks (%s, class ' % (label, row), rc['ReturnValue'], '): ', istr(g, cnt['ReturnValue'], x0 + 800, Y + 400, 'DiagCountStr_' + label))
    lc = log(g, x0 + 1100, Y, msg_pin=mc, name='LogDiag_' + label); ex(gaa, lc)
    j2 = knot(g, x0 + 1300, Y - 100, 'DiagNext_' + label); link(lc['then'], j2['InputPin']); link(cc['CastFailed'], j2['InputPin'])
    prev = (j2, 'OutputPin')
# ocean notices
XO = X + 2600 * len(DOCKS) + 1400 * len(DIAG)
brs = g.branch(XO - 300, -2400, 'BrRescan'); link(g.get('Rescan', BOOL, XO - 450, -2250, name='RescanGet')['Rescan'], brs['Condition']); ex(prev[0], brs, prev[1])
lrs = log(g, XO - 300, -2700, msg='SetSail: docks read again after loading', name='LogRescan'); ex(brs, lrs)
goc = g.call(GS + ':GetActorOfClass', 'FindOcean', XO, -2400, ActorClass=OCEAN); ex(brs, goc, 'else')
goc['ReturnValue'].t = OBJ(OCEAN)
bo = g.branch(XO + 250, -2400, 'BrOcean'); link(is_valid(g, goc['ReturnValue'], XO + 250, -2200, 'OceanValid'), bo['Condition']); ex(goc, bo)
bn1 = bind(g, goc['ReturnValue'], OCEAN, 'OnNoticeChange', '/Script/NauticalKit', 'OnNoticeChange__DelegateSignature', evN, XO + 500, -2400, 'BindNotice'); ex(bo, bn1)
bn2 = bind(g, goc['ReturnValue'], OCEAN, 'OnNoticeClear', '/Script/NauticalKit', 'OnNoticeClear__DelegateSignature', evC, XO + 750, -2400, 'BindNoticeClear'); ex(bn1, bn2)
lo = log(g, XO + 1100, -2400, msg='SetSail: ocean notices bound', name='LogOcean'); ex(bn2, lo)
lno = log(g, XO + 500, -2150, msg='SetSail: no NauticalOcean actor found', name='LogNoOcean'); ex(bo, lno, 'else')
jo = knot(g, XO + 1400, -2500, 'AfterOcean'); link(lo['then'], jo['InputPin']); link(lno['then'], jo['InputPin'])
# invisible dock window (created once per map)
pc = g.call(GS + ':GetPlayerController', 'PC', XO + 1500, -2200)
cw = g.add('/Script/UMGEditor.K2Node_CreateWidget', 'CreateView', [], XO + 1700, -2400)
cw.pin('execute', EXEC); cw.pin('then', EXEC, out=True)
cw.pin('Class', T('class', obj=cls_ref('/Script/UMG.UserWidget')), defobj=VIEW)
cw.pin('OwningPlayer', OBJ('/Script/Engine.PlayerController'))
cw.pin('ReturnValue', VIEW_T, out=True)
link(pc['ReturnValue'], cw['OwningPlayer']); link(jo['OutputPin'], cw['execute'])
sv = g.setv('View', VIEW_T, XO + 2000, -2400, name='SetView'); link(cw['ReturnValue'], sv['View']); ex(cw, sv)

def bp_get(cls, var, t, target_pin, x, y, name):
    n = g.add(BG + 'K2Node_VariableGet', name, ['VariableReference=(MemberParent="%s",MemberName="%s")' % (cls, var)], x, y)
    n.pin(var, t, out=True); n.pin('self', T('object', obj=cls), friendly='NSLOCTEXT("K2Node", "Target", "Target")')
    link(target_pin, n['self']); return n
def label_state(on_pin, toggle_pin, x, y, tag):
    """Toggle.Label: text 'Auto-sail: On/Off' and colour cream/dim. Returns (first node, last node)."""
    lb = bp_get(TOGGLE_CLS, 'Label', OBJ('/Script/UMG.TextBlock'), toggle_pin, x, y + 250, 'LabelGet' + tag)
    ts = g.call(KML + ':SelectString', 'LabelStr' + tag, x, y + 400, A=LABEL_ON, B=LABEL_OFF); link(on_pin, ts['bPickA'])
    tt = g.call('/Script/Engine.KismetTextLibrary:Conv_StringToText', 'LabelTxt' + tag, x + 200, y + 400); link(ts['ReturnValue'], tt['InString'])
    st = g.call('/Script/UMG.TextBlock:SetText', 'SetLabel' + tag, x + 250, y); link(lb['Label'], st['self']); link(tt['ReturnValue'], st['InText'])
    sc = g.call(KML + ':SelectColor', 'LabelCol' + tag, x + 400, y + 400, A=COLOR_ON, B=COLOR_OFF); link(on_pin, sc['bPickA'])
    mk_ = struct_node(g, 'Make', '/Script/SlateCore.SlateColor', ['SpecifiedColor', 'ColorUseRule'], x + 600, y + 350, 'LabelSlate' + tag)
    mk_.set('ColorUseRule', 'UseColor_Specified'); link(sc['ReturnValue'], mk_['SpecifiedColor'])
    sco = g.call('/Script/UMG.TextBlock:SetColorAndOpacity', 'SetLabelColor' + tag, x + 500, y); link(lb['Label'], sco['self']); link(mk_['SlateColor'], sco['InColorAndOpacity'])
    ex(st, sco)
    return st, sco
def selfn(x, y, name):
    n = g.add(BG + 'K2Node_Self', name, [], x, y); n.pin('self', T('object', sub='self'), out=True); return n
def gname(pin, x, y, name):
    n = g.call(KSL + ':GetDisplayName', name, x, y); link(pin, n['Object']); return n['ReturnValue']

# ---- toggle UI setup (after the invisible view exists)
UX, UY = XO + 2300, -2400
pcU = g.call(GS + ':GetPlayerController', 'PCUi', UX, UY + 250)
ei = g.call('/Script/Engine.Actor:EnableInput', 'EnableClicks', UX + 200, UY); link(selfn(UX + 50, UY + 150, 'MeInput')['self'], ei['self']); link(pcU['ReturnValue'], ei['PlayerController']); ex(sv, ei)
ct = g.create_widget(TOGGLE, UX + 450, UY, name='CreateToggle'); ct['ReturnValue'].t = TOGGLE_T; link(pcU['ReturnValue'], ct['OwningPlayer']); ex(ei, ct)
stg = g.setv('Toggle', TOGGLE_T, UX + 700, UY, name='SetToggle'); link(ct['ReturnValue'], stg['Toggle']); ex(ct, stg)
chk = bp_get(TOGGLE_CLS, 'Check', OBJ('/Script/UMG.CheckBox'), ct['ReturnValue'], UX + 700, UY + 200, 'CheckGet')
evTg = g.custom_event('OnToggle', [('bIsChecked', BOOL)], 0, 5000)
btg = bind(g, chk['Check'], '/Script/UMG.CheckBox', 'OnCheckStateChanged', '/Script/UMG', 'OnCheckBoxComponentStateChanged__DelegateSignature', evTg, UX + 950, UY, 'BindToggle'); ex(stg, btg)
cwt = g.create_widget(WAITER, UX + 1200, UY, name='CreateWaiter'); cwt['ReturnValue'].t = WAITER_T; link(pcU['ReturnValue'], cwt['OwningPlayer']); ex(btg, cwt)
swt = g.setv('Waiter', WAITER_T, UX + 1450, UY, name='SetWaiter'); link(cwt['ReturnValue'], swt['Waiter']); ex(cwt, swt)
# the waiter calls its own event dispatcher "Done"; we bind it (no Owner variable -> no ambiguous BP_MapLoad type)
evCC = g.custom_event('OnClickCheck', [], 0, 4500)
sow = g.add(BG + 'K2Node_AddDelegate', 'BindWaiterDone', ['DelegateReference=(MemberParent="%s",MemberName="Done")' % WAITER_CLS], UX + 1700, UY)
sow.pin('execute', EXEC); sow.pin('then', EXEC, out=True)
sow.pin('self', WAITER_T, friendly='NSLOCTEXT("K2Node", "Target", "Target")')
dpin = sow.pin('Delegate', T('delegate'))
dpin.memref = 'MemberParent="%s",MemberName="Done__DelegateSignature"' % WAITER_CLS
link(cwt['ReturnValue'], sow['self'])
evo = evCC['OutputDelegate']
evo.memref = 'MemberParent="/Script/Engine.BlueprintGeneratedClass\'%s.%s_C\'",MemberName="%s"' % (MAPLOAD, MAPLOAD.split('/')[-1], 'OnClickCheck')
link(evo, dpin); ex(swt, sow)
bv = sow

# ---- left click (works while paused) -> show the waiter; its Tick calls OnClickCheck after ~0.1 s and hides itself
kL = g.add(BG + 'K2Node_InputKey', 'KeyClick', ['InputKey=LeftMouseButton', 'bConsumeInput=False', 'bExecuteWhenPaused=True'], 0, 4000)
kL.pin('Pressed', EXEC, out=True); kL.pin('Released', EXEC, out=True); kL.pin('Key', STRUCT('/Script/InputCore.Key'), out=True)
wtg = g.get('Waiter', WAITER_T, 100, 4200, name='WaiterGetK')
inv = g.call('/Script/UMG.Widget:IsInViewport', 'WaiterShown', 300, 4200); link(wtg['Waiter'], inv['self'])
lck = log(g, 150, 3800, msg='SetSail: CLICK', name='LogClick'); ex(kL, lck, 'Released')
evWB = g.custom_event('OnWindowButton', [], 0, 3600)
lwb = log(g, 150, 3600, msg='SetSail: WINDOW BUTTON', name='LogWindowButton'); ex(evWB, lwb)
link(lwb['then'], lck['execute'])
bk = g.branch(300, 4000, 'BrWaiterIdle'); link(inv['ReturnValue'], bk['Condition']); ex(lck, bk)
bwv = g.branch(500, 4000, 'BrWaiterValid'); link(is_valid(g, wtg['Waiter'], 450, 4300, 'WaiterValid'), bwv['Condition']); ex(bk, bwv, 'else')
lnw = log(g, 700, 4250, msg='SetSail:   no waiter widget!', name='LogNoWaiter'); ex(bwv, lnw, 'else')
atv = g.call('/Script/UMG.UserWidget:AddToViewport', 'ShowWaiter', 750, 4000); link(wtg['Waiter'], atv['self']); ex(bwv, atv)

# ---- OnClickCheck: the visible dock window (not our invisible View)
lcc = log(g, 150, 4350, msg='SetSail: CLICK CHECK (waiter called back)', name='LogClickCheck'); ex(evCC, lcc)
sw0 = g.setv('Win', UWT, 250, 4500, name='ClearWin'); ex(lcc, sw0)
gw = g.call('/Script/UMG.WidgetBlueprintLibrary:GetAllWidgetsOfClass', 'DockWindows', 500, 4500, WidgetClass=VIEW, TopLevelOnly='false')
gw['FoundWidgets'].t = ARR(UWT); link(selfn(350, 4700, 'MeWC')['self'], gw['WorldContextObject']); ex(sw0, gw)
lpW = g.macro('ForEachLoop', UWT, 800, 4500, name='WindowLoop'); link(gw['FoundWidgets'], lpW['Array']); ex(gw, lpW, 'then', 'Exec')
W = lpW['Array Element']
notV = g.call(KML + ':NotEqual_ObjectObject', 'NotOurView', 1100, 4700); link(W, notV['A']); link(g.get('View', VIEW_T, 900, 4800, name='ViewGetW')['View'], notV['B'])
vis = g.call('/Script/UMG.Widget:IsVisible', 'WinVisible', 1100, 4850); link(W, vis['self'])
okW = g.call(KML + ':BooleanAND', 'RealWindow', 1300, 4750); link(notV['ReturnValue'], okW['A']); link(vis['ReturnValue'], okW['B'])
lwn = log(g, 950, 4300, msg_pin=concat(g, 950, 4400, 'SetSail:   window ', gname(W, 850, 4450, 'WName'), ' visible ', bstr(g, vis['ReturnValue'], 850, 4500, 'WVisStr'), ' ours ', bstr(g, g.call(KML + ':Not_PreBool', 'IsOurView', 850, 4550)['ReturnValue'], 850, 4600, 'WOursStr')), name='LogWindow'); ex(lpW, lwn, 'LoopBody')
for n_ in g.nodes:
    if n_.name == 'IsOurView': link(notV['ReturnValue'], n_['A'])
bW = g.branch(1100, 4500, 'BrIsWindow'); link(okW['ReturnValue'], bW['Condition']); ex(lwn, bW)
swW = g.setv('Win', UWT, 1350, 4500, name='SetWin'); link(W, swW['Win']); ex(bW, swW)
wgc = g.get('Win', UWT, 1500, 4900, name='WinGetC')
bWv = g.branch(1600, 4500, 'BrHaveWindow'); link(is_valid(g, wgc['Win'], 1550, 5000, 'WinValid'), bWv['Condition']); ex(lpW, bWv, 'Completed')
lnwin = log(g, 1800, 5300, msg='SetSail:   no visible dock window', name='LogNoWindow'); ex(bWv, lnwin, 'else')
neq = g.call(KML + ':NotEqual_ObjectObject', 'NewWindow', 1800, 4800); link(wgc['Win'], neq['A']); link(g.get('Injected', UWT, 1650, 5050, name='InjectedGetC')['Injected'], neq['B'])
bNew = g.branch(1850, 4500, 'BrNewWindow'); link(neq['ReturnValue'], bNew['Condition']); ex(bWv, bNew)
REFRESH = knot(g, 4800, 4400, 'RefreshToggle')
link(bNew['else'], REFRESH['InputPin'])
# new window: find the rename button, put the toggle into its panel
san = g.setv('Anchor', WIDGET_T, 2100, 4500, name='ClearAnchor'); ex(bNew, san)
fd = g.call('/Script/SystemCore.NaviUi:FindDecendentsOfClasses', 'WindowWidgets', 2350, 4500, ignoreHidden='false'); link(wgc['Win'], fd['searchRoot']); ex(san, fd)
elemC = dict(fd['candidateClasses'].t); elemC['cont'] = 'None'; elemC['ref'] = False; elemC['const'] = False
mkc = g.add(BG + 'K2Node_MakeArray', 'WidgetClasses', ['NumInputs=1'], 2200, 4750); mkc.pin('Array', ARR(elemC), out=True); mkc.pin('[0]', elemC, defobj='/Script/UMG.Button')
link(mkc['Array'], fd['candidateClasses']); fd['ReturnValue'].t = ARR(WIDGET_T)
lpA = g.macro('ForEachLoop', WIDGET_T, 2650, 4500, name='AnchorLoop'); link(fd['ReturnValue'], lpA['Array']); ex(fd, lpA, 'then', 'Exec')
isA = g.call(KSTR + ':EqualEqual_StrStr', 'IsRenameButton', 2900, 4750, B=ANCHOR_NAME); link(gname(lpA['Array Element'], 2700, 4750, 'BtnName'), isA['A'])
bA = g.branch(2900, 4500, 'BrIsAnchor'); link(isA['ReturnValue'], bA['Condition']); ex(lpA, bA, 'LoopBody')
sa = g.setv('Anchor', WIDGET_T, 3150, 4500, name='SetAnchor'); link(lpA['Array Element'], sa['Anchor']); ex(bA, sa)
ag = g.get('Anchor', WIDGET_T, 2900, 5000, name='AnchorGet')
bAv = g.branch(3150, 4800, 'BrHaveAnchor'); link(is_valid(g, ag['Anchor'], 3000, 5100, 'AnchorValid'), bAv['Condition']); ex(lpA, bAv, 'Completed')
lNA = log(g, 3400, 5200, msg='SetSail: dock window has no ButtonEditName - toggle not added', name='LogNoAnchor'); ex(bAv, lNA, 'else')
# walk up from the rename button: log each panel, take the first VerticalBox as Target (the toggle gets its own row)
stc = g.setv('Target', OBJ(PANEL), 3300, 4800, name='ClearTarget'); ex(bAv, stc)
cur = g.get('Anchor', WIDGET_T, 3300, 5000, name='AnchorWalk')['Anchor']
prev_ex = stc['then']; WALK_END = knot(g, 3300 + 600 * 9, 4700, 'WalkEnd')
for lv in range(1, 9):
    xx = 3300 + 600 * lv
    par = g.call('/Script/UMG.Widget:GetParent', 'Up%d' % lv, xx, 5000); link(cur, par['self'])
    bv_ = g.branch(xx, 4800, 'BrUp%d' % lv); link(is_valid(g, par['ReturnValue'], xx, 5100, 'UpValid%d' % lv), bv_['Condition']); link(prev_ex, bv_['execute'])
    link(bv_['else'], WALK_END['InputPin'])
    pcl = g.call('/Script/Engine.GameplayStatics:GetObjectClass', 'UpCls%d' % lv, xx + 100, 5200); link(par['ReturnValue'], pcl['Object'])
    pcn = g.call(KSL + ':GetClassDisplayName', 'UpClsName%d' % lv, xx + 250, 5200); link(pcl['ReturnValue'], pcn['Class'])
    lu = log(g, xx + 200, 4800, msg_pin=concat(g, xx + 200, 5300, 'SetSail:   up %d: ' % lv, gname(par['ReturnValue'], xx + 100, 5350, 'UpName%d' % lv), ' (', pcn['ReturnValue'], ')'), name='LogUp%d' % lv); ex(bv_, lu)
    isvb = g.call(KSTR + ':EqualEqual_StrStr', 'UpIsVBox%d' % lv, xx + 350, 5250, B='VerticalBox'); link(pcn['ReturnValue'], isvb['A'])
    noT = g.call(KML + ':Not_PreBool', 'NoTarget%d' % lv, xx + 350, 5400); link(is_valid(g, g.get('Target', OBJ(PANEL), xx + 200, 5450, name='TargetGet%d' % lv)['Target'], xx + 300, 5450, 'TargetValid%d' % lv), noT['A'])
    takeit = g.call(KML + ':BooleanAND', 'TakeVBox%d' % lv, xx + 450, 5300); link(isvb['ReturnValue'], takeit['A']); link(noT['ReturnValue'], takeit['B'])
    bt_ = g.branch(xx + 400, 4800, 'BrTake%d' % lv); link(takeit['ReturnValue'], bt_['Condition']); ex(lu, bt_)
    stt2 = g.setv('Target', OBJ(PANEL), xx + 500, 4700, name='SetTarget%d' % lv); link(par['ReturnValue'], stt2['Target']); ex(bt_, stt2)
    jn = knot(g, xx + 550, 4800, 'UpNext%d' % lv); link(stt2['then'], jn['InputPin']); link(bt_['else'], jn['InputPin'])
    prev_ex = jn['OutputPin']; cur = par['ReturnValue']
link(prev_ex, WALK_END['InputPin'])
XA = 3300 + 600 * 10
tgt = g.get('Target', OBJ(PANEL), XA, 5000, name='TargetGetA')
btv = g.branch(XA, 4700, 'BrHaveTarget'); link(is_valid(g, tgt['Target'], XA, 5100, 'TargetValidA'), btv['Condition']); link(WALK_END['OutputPin'], btv['execute'])
lnt = log(g, XA + 300, 5200, msg='SetSail: no VerticalBox above the rename button - toggle not added', name='LogNoTarget'); ex(btv, lnt, 'else')
tgA = g.get('Toggle', TOGGLE_T, XA + 200, 5000, name='ToggleGetA')
ac = g.call(PANEL + ':AddChild', 'AddToggle', XA + 250, 4700); link(tgt['Target'], ac['self']); link(tgA['Toggle'], ac['Content']); ex(btv, ac)
sinj = g.setv('Injected', UWT, XA + 500, 4700, name='MarkInjected'); link(wgc['Win'], sinj['Injected']); ex(ac, sinj)
lAd = log(g, XA + 750, 4700, msg_pin=concat(g, XA + 750, 4900, 'SetSail: toggle added to ', gname(tgt['Target'], XA + 600, 5000, 'TargetName')), name='LogAdded'); ex(sinj, lAd)
cvs = g.cast('/Script/UMG.VerticalBoxSlot', False, XA + 1000, 4700, name='AsVBoxSlot'); link(ac['ReturnValue'], cvs['Object']); ex(lAd, cvs)
sha = g.call('/Script/UMG.VerticalBoxSlot:SetHorizontalAlignment', 'ToggleLeft', XA + 1250, 4700, InHorizontalAlignment='HAlign_Left'); link(cvs['AsVerticalBoxSlot'], sha['self']); ex(cvs, sha)
spd = g.call('/Script/UMG.VerticalBoxSlot:SetPadding', 'TogglePad', XA + 1500, 4700, InPadding='(Left=8.000000,Top=6.000000,Right=0.000000,Bottom=2.000000)'); link(cvs['AsVerticalBoxSlot'], spd['self']); ex(sha, spd)
BB = knot(g, XA + 1700, 4600, 'BindWindowButtons'); link(spd['then'], BB['InputPin']); link(cvs['CastFailed'], BB['InputPin'])
fdb = g.call('/Script/SystemCore.NaviUi:FindDecendentsOfClasses', 'WindowButtons', XA + 1850, 4600, ignoreHidden='false')
link(g.get('Win', UWT, XA + 1700, 4800, name='WinGetB')['Win'], fdb['searchRoot']); link(BB['OutputPin'], fdb['execute'])
mkb = g.add(BG + 'K2Node_MakeArray', 'ButtonClasses', ['NumInputs=1'], XA + 1700, 4900); mkb.pin('Array', ARR(elemC), out=True); mkb.pin('[0]', elemC, defobj='/Script/UMG.Button')
link(mkb['Array'], fdb['candidateClasses']); fdb['ReturnValue'].t = ARR(WIDGET_T)
lpb = g.macro('ForEachLoop', WIDGET_T, XA + 2150, 4600, name='ButtonLoop'); link(fdb['ReturnValue'], lpb['Array']); ex(fdb, lpb, 'then', 'Exec')
cbt = g.cast('/Script/UMG.Button', False, XA + 2400, 4600, name='AsWindowButton'); link(lpb['Array Element'], cbt['Object']); ex(lpb, cbt, 'LoopBody')
bwb = bind(g, cbt['AsButton'], '/Script/UMG.Button', 'OnClicked', '/Script/UMG', 'OnButtonClickedEvent__DelegateSignature', evWB, XA + 2650, 4600, 'BindWindowButton'); ex(cbt, bwb)
nb = g.arr('Array_Length', WIDGET_T, XA + 2150, 4800, name='ButtonCount', pure=True); link(fdb['ReturnValue'], nb['TargetArray'])
lbb = log(g, XA + 2400, 4400, msg_pin=concat(g, XA + 2400, 4300, 'SetSail: listening to ', istr(g, nb['ReturnValue'], XA + 2300, 4250, 'ButtonCountStr'), ' buttons of the dock window'), name='LogButtons'); ex(lpb, lbb, 'Completed')
link(lbb['then'], REFRESH['InputPin'])

# ---- refresh: hide for docks we don't handle, show the ship's state
igr = g.get('Injected', UWT, 4800, 4700, name='InjectedGetR')
car = g.cast(AWB, False, 4950, 4400, name='WinAsArco'); link(igr['Injected'], car['Object']); link(REFRESH['OutputPin'], car['execute'])
ctx = g.get('Context', ACTOR, 5100, 4700, owner=AWB, name='WinContext'); link(car['AsArcoWidgetBase'], ctx['self'])
ccl = g.call('/Script/Engine.GameplayStatics:GetObjectClass', 'WinDockClass', 5100, 4900); link(ctx['Context'], ccl['Object'])
fcl = g.arr('Array_Find', ACLS, 5200, 4850, name='ClassIndex', pure=True)
link(g.get('DockClasses', ARR(ACLS), 5000, 4950, name='ClassesR')['DockClasses'], fcl['TargetArray'])
itc = dict(ACLS); itc['ref'] = True; itc['const'] = True; fcl.pin('ItemToFind', itc); fcl.pin('ReturnValue', INT, out=True)
link(ccl['ReturnValue'], fcl['ItemToFind'])
known = g.call(KML + ':GreaterEqual_IntInt', 'KnownDock', 5400, 4850, B='0'); link(fcl['ReturnValue'], known['A'])
fnd = g.arr('Array_Find', ACTOR, 5200, 5050, name='DockIndex', pure=True)
link(g.get('Docks', ARR(ACTOR), 5000, 5100, name='DocksR')['Docks'], fnd['TargetArray'])
it_ = dict(ACTOR); it_['ref'] = True; it_['const'] = True; fnd.pin('ItemToFind', it_); fnd.pin('ReturnValue', INT, out=True)
link(ctx['Context'], fnd['ItemToFind'])
inList = g.call(KML + ':GreaterEqual_IntInt', 'DockInList', 5400, 5050, B='0'); link(fnd['ReturnValue'], inList['A'])
tgr = g.get('Toggle', TOGGLE_T, 5300, 4600, name='ToggleGetR')
bK = g.branch(5250, 4400, 'BrKnownDock'); link(known['ReturnValue'], bK['Condition']); ex(car, bK)
hideT = g.call('/Script/UMG.Widget:SetVisibility', 'HideToggle', 5500, 4600, InVisibility='Collapsed'); link(tgr['Toggle'], hideT['self']); ex(bK, hideT, 'else')
showT = g.call('/Script/UMG.Widget:SetVisibility', 'ShowToggle', 5500, 4400, InVisibility='Visible'); link(tgr['Toggle'], showT['self']); ex(bK, showT)
bIL = g.branch(5650, 4300, 'BrDockInList'); link(inList['ReturnValue'], bIL['Condition']); ex(showT, bIL)
adD = arr_fn(g, 'Array_Add', ACTOR, 5800, 4150, 'AddNewDock'); link(g.get('Docks', ARR(ACTOR), 5800, 4050, name='DocksN')['Docks'], adD['TargetArray']); link(ctx['Context'], adD['NewItem']); ex(bIL, adD, 'else')
kindN = array_item(g, g.get('DockKinds', ARR(STR), 5800, 3950, name='KindsN')['DockKinds'], STR, fcl['ReturnValue'], 5950, 3950, 'NewDockKind')
adK = arr_fn(g, 'Array_Add', STR, 6000, 4150, 'AddNewDockKind'); link(g.get('DockOpt', ARR(STR), 6000, 4050, name='DockOptN')['DockOpt'], adK['TargetArray']); link(kindN, adK['NewItem']); ex(adD, adK)
lnd = log(g, 6200, 4150, msg_pin=concat(g, 6200, 4000, 'SetSail: new ', kindN, ' dock added: ', gname(ctx['Context'], 6100, 3900, 'NewDockName')), name='LogNewDock'); ex(adK, lnd)
jIL = knot(g, 6450, 4350, 'AfterDockList'); link(bIL['then'], jIL['InputPin']); link(lnd['then'], jIL['InputPin'])
vgr = g.get('View', VIEW_T, 5600, 4700, name='ViewGetR')
scr = g.call(KSL + ':SetObjectPropertyByName', 'ViewToWinDock', 5750, 4400, PropertyName='Context'); link(vgr['View'], scr['Object']); link(ctx['Context'], scr['Value']); link(jIL['OutputPin'], scr['execute'])
chr_ = g.call(VIEW + ':CalcHudState', 'WinDockState', 6000, 4400); link(vgr['View'], chr_['self']); ex(scr, chr_)
str_ = struct_node(g, 'Break', UIDATA, ['assignedShipNauticalState', 'workDockPhase'], 6000, 4650, 'BreakWinDock'); link(chr_['ReturnValue'], str_['WorkDock_UIData'])
ssr = struct_node(g, 'Break', '/Script/NauticalKit.NauticalShipState', ['shipName'], 6250, 4750, 'BreakWinShip'); link(str_['assignedShipNauticalState'], ssr['NauticalShipState'])
hr = g.call(KSTR + ':Contains', 'WinAuto', 6450, 4750, Substring=AUTO_TAG, bUseCase='false', bSearchFromEnd='false'); link(ssr['shipName'], hr['SearchIn'])
nh = {'ReturnValue': hr['ReturnValue']}
chk2 = bp_get(TOGGLE_CLS, 'Check', OBJ('/Script/UMG.CheckBox'), tgr['Toggle'], 6400, 4600, 'CheckGetR')
sic = g.call('/Script/UMG.CheckBox:SetIsChecked', 'ShowState', 6300, 4400); link(chk2['Check'], sic['self']); link(nh['ReturnValue'], sic['InIsChecked']); ex(chr_, sic)
phr = g.call(KSTR + ':Conv_ByteToString', 'WinPhaseStr', 6450, 4900); link(str_['workDockPhase'], phr['InByte'])
hasShip = g.call(KSTR + ':NotEqual_StrStr', 'WinHasShip', 6650, 4900, B='0'); link(phr['ReturnValue'], hasShip['A'])
sen = g.call('/Script/UMG.Widget:SetIsEnabled', 'EnableToggle', 6550, 4400); link(tgr['Toggle'], sen['self']); link(hasShip['ReturnValue'], sen['bInIsEnabled']); lsA, lsB = label_state(hr['ReturnValue'], tgr['Toggle'], 6300, 4100, 'R'); ex(sic, lsA); ex(lsB, sen)
lR = log(g, 6800, 4400, msg_pin=concat(g, 6800, 4600, 'SetSail: dock window ', gname(ctx['Context'], 6700, 4700, 'WinDockName'), ' ship ', ssr['shipName']), name='LogWinRefresh'); ex(sen, lR)

# ---- OnToggle: rename the ship (add / remove the hold tag) with the game's own rename command
igt = g.get('Injected', UWT, 200, 5200, name='InjectedGetT')
ltA, ltB = label_state(evTg['bIsChecked'], g.get('Toggle', TOGGLE_T, 100, 5700, name='ToggleGetT')['Toggle'], 100, 5800, 'T'); ex(evTg, ltA)
cat_ = g.cast(AWB, False, 300, 5000, name='WinAsArcoT'); link(igt['Injected'], cat_['Object']); ex(ltB, cat_)
ctxT = g.get('Context', ACTOR, 450, 5250, owner=AWB, name='WinContextT'); link(cat_['AsArcoWidgetBase'], ctxT['self'])
vgt = g.get('View', VIEW_T, 550, 5300, name='ViewGetT')
sct = g.call(KSL + ':SetObjectPropertyByName', 'ViewToToggleDock', 600, 5000, PropertyName='Context'); link(vgt['View'], sct['Object']); link(ctxT['Context'], sct['Value']); ex(cat_, sct)
cht = g.call(VIEW + ':CalcHudState', 'ToggleDockState', 850, 5000); link(vgt['View'], cht['self']); ex(sct, cht)
stt_ = struct_node(g, 'Break', UIDATA, ['assignedShipNauticalState', 'assignedShipInfo'], 850, 5250, 'BreakToggleDock'); link(cht['ReturnValue'], stt_['WorkDock_UIData'])
sst_ = struct_node(g, 'Break', '/Script/NauticalKit.NauticalShipState', ['shipName'], 1100, 5350, 'BreakToggleShip'); link(stt_['assignedShipNauticalState'], sst_['NauticalShipState'])
sai_ = struct_node(g, 'Break', PA + 'ArcoWorkShip', ['nameId'], 1100, 5500, 'BreakToggleWorkShip'); link(stt_['assignedShipInfo'], sai_['ArcoWorkShip'])
cur_ = sst_['shipName']
for i_, frm in enumerate([' ' + AUTO_TAG, AUTO_TAG, ' ' + OLD_TAG, OLD_TAG]):
    rr = g.call(KSTR + ':Replace', 'DropTag%d' % i_, 1300 + 150 * i_, 5300, From=frm, To='', SearchCase='IgnoreCase'); link(cur_, rr['SourceString']); cur_ = rr['ReturnValue']
clean = cur_
tagged = g.call(KSTR + ':Concat_StrStr', 'AddTag', 1700, 5450, B=' ' + AUTO_TAG); link(clean, tagged['A'])
newn = g.call(KML + ':SelectString', 'NewShipName', 1900, 5350); link(tagged['ReturnValue'], newn['A']); link(clean, newn['B']); link(evTg['bIsChecked'], newn['bPickA'])
mkr = struct_node(g, 'Make', PA + 'HudAction', ['action', 'paramString', 'paramName'], 2100, 5300, 'MakeRename')
mkr.set('action', 'setNauticalShipName'); link(newn['ReturnValue'], mkr['paramString']); link(sai_['nameId'], mkr['paramName'])
pcr = g.call(GS + ':GetPlayerController', 'PCRename', 2100, 5600)
cpr = g.cast(PA + 'PlayerController_Play', False, 1150, 5000, name='AsPlayPCRename'); link(pcr['ReturnValue'], cpr['Object']); ex(cht, cpr)
hr2 = g.call(PA + 'PlayerController_Play:HandleHudAction', 'RenameShip', 2350, 5000); link(cpr['AsPlayerController_Play'], hr2['self']); link(mkr['HudAction'], hr2['HudAction']); ex(cpr, hr2)
lT = log(g, 2600, 5000, msg_pin=concat(g, 2600, 5200, 'SetSail: toggle -> ship ', nstr(g, sai_['nameId'], 2400, 5300, 'ShipIdStr'), ' renamed to "', newn['ReturnValue'], '"'), name='LogRenamed'); ex(hr2, lT)
PENDING_EVT_LINK = lT

# ---- candidate events: log, then check the docks
SCAN = knot(g, 1300, 1600, 'CheckDocks')
EVT = knot(g, 1000, 1500, 'NewEvent')   # notices: just check
link(PENDING_EVT_LINK['then'], EVT['InputPin'])   # toggle switched: check right away
link(EVT['OutputPin'], SCAN['InputPin'])
link(lrs['then'], EVT['InputPin'])   # v21: after the load rescan, check the docks right away
DAYCHAIN = knot(g, 900, 1300, 'StartDayChecks')   # load + day start: check now and schedule the rest of today's checks

def schedule(x, y, name):
    """Next check: (time until evening - margin) / checks left. Only while it is day (phase MID_DAY = 0).
    Returns (entry node, exit knot)."""
    gwt = g.call(GS + ':GetActorOfClass', name + 'FindTime', x, y, ActorClass=WORLDTIME); gwt['ReturnValue'].t = OBJ(WORLDTIME)
    wt = gwt['ReturnValue']
    ph = g.call(WORLDTIME + ':GetDayPhase', name + 'Phase', x + 200, y + 300); link(wt, ph['self'])
    phs = g.call(KSTR + ':Conv_ByteToString', name + 'PhaseStr', x + 400, y + 300); link(ph['ReturnValue'], phs['InByte'])
    isday = g.call(KSTR + ':EqualEqual_StrStr', name + 'IsDay', x + 600, y + 300, B='0'); link(phs['ReturnValue'], isday['A'])
    tnp = g.call(WORLDTIME + ':TimeUntilNextPhase', name + 'UntilNight', x + 200, y + 450); link(wt, tnp['self'])
    dtr = g.call(WORLDTIME + ':GetDayTimeRemaining', name + 'DayLeft', x + 200, y + 550); link(wt, dtr['self'])
    sub = g.call(KML + ':Subtract_DoubleDouble', name + 'MinusMargin', x + 400, y + 450, B=EVENING_MARGIN); link(tnp['ReturnValue'], sub['A'])
    tg2 = g.get('Tries', INT, x + 400, y + 600, name=name + 'TriesGet')
    tdb = g.call(KML + ':Conv_IntToDouble', name + 'TriesD', x + 550, y + 600); link(tg2['Tries'], tdb['inInt'])
    itv = g.call(KML + ':Divide_DoubleDouble', name + 'Interval', x + 700, y + 450); link(sub['ReturnValue'], itv['A']); link(tdb['ReturnValue'], itv['B'])
    pos = g.call(KML + ':Greater_DoubleDouble', name + 'IntervalOk', x + 850, y + 450, B='1.0'); link(itv['ReturnValue'], pos['A'])
    vok_ = g.call(KSL + ':IsValid', name + 'TimeValid', x + 200, y + 200); link(wt, vok_['Object'])
    a1_ = g.call(KML + ':BooleanAND', name + 'Ok1', x + 800, y + 250); link(vok_['ReturnValue'], a1_['A']); link(isday['ReturnValue'], a1_['B'])
    a2_ = g.call(KML + ':BooleanAND', name + 'Ok', x + 1000, y + 300); link(a1_['ReturnValue'], a2_['A']); link(pos['ReturnValue'], a2_['B'])
    br_ = g.branch(x + 250, y, name + 'BrSchedule'); link(a2_['ReturnValue'], br_['Condition']); ex(gwt, br_)
    srp = g.setv('RetryPending', BOOL, x + 500, y, value='true', name=name + 'Pending'); ex(br_, srp)
    tmr = g.call(KSL + ':K2_SetTimer', name + 'Timer', x + 750, y, FunctionName='OnRetry', bLooping='false'); link(itv['ReturnValue'], tmr['Time'])
    sn = g.add(BG + 'K2Node_Self', name + 'Self', [], x + 600, y + 150); sn.pin('self', T('object', sub='self'), out=True); link(sn['self'], tmr['Object'])
    ex(srp, tmr)
    d2s = lambda pin, nm, xx, yy: (lambda n_: (link(pin, n_['InDouble']), n_['ReturnValue'])[1])(g.call(KSTR + ':Conv_DoubleToString', nm, xx, yy))
    m = concat(g, x + 1100, y + 400, 'SetSail:   next check in ', d2s(itv['ReturnValue'], name + 'ItvStr', x + 1000, y + 500),
               ' s, checks left today ', istr(g, tg2['Tries'], x + 1000, y + 550, name + 'TriesStr'),
               ' (until night ', d2s(tnp['ReturnValue'], name + 'TnpStr', x + 1000, y + 600),
               ', day time left ', d2s(dtr['ReturnValue'], name + 'DtrStr', x + 1000, y + 650),
               ', phase ', phs['ReturnValue'], ')')
    lg = log(g, x + 1000, y, msg_pin=m, name=name + 'Log'); ex(tmr, lg)
    m2 = concat(g, x + 600, y + 750, 'SetSail:   no more checks today (phase ', phs['ReturnValue'], ', until night ', d2s(tnp['ReturnValue'], name + 'TnpStr2', x + 400, y + 800), ')')
    lg2 = log(g, x + 500, y - 250, msg_pin=m2, name=name + 'LogNone'); ex(br_, lg2, 'else')
    j_ = knot(g, x + 1300, y - 100, name + 'Done'); link(lg['then'], j_['InputPin']); link(lg2['then'], j_['InputPin'])
    return gwt, j_

# day chain: Tries = checks-1 ; if no timer pending -> schedule ; -> check now
stt = g.setv('Tries', INT, 1000, 1100, value=str(CHECKS_PER_DAY - 1), name='ResetTries'); link(DAYCHAIN['OutputPin'], stt['execute'])
rpg0 = g.get('RetryPending', BOOL, 1000, 1000, name='PendingGet0')
bpd = g.branch(1250, 1100, 'BrAlreadyPending'); link(rpg0['RetryPending'], bpd['Condition']); ex(stt, bpd)
s0, l0 = schedule(1500, 900, 'Sched0'); ex(bpd, s0, 'else'); l0 = {'then': l0['OutputPin']}
link(bpd['then'], SCAN['InputPin']); link(l0['then'], SCAN['InputPin'])
link(bv['then'], DAYCHAIN['InputPin'])
# OnRetry: RetryPending = false ; Tries-1 ; > 0 -> schedule next ; -> check
evR = g.custom_event('OnRetry', [], 0, 3300)
srp0 = g.setv('RetryPending', BOOL, 200, 3300, value='false', name='RetryDone'); ex(evR, srp0)
tgr = g.get('Tries', INT, 200, 3550, name='TriesR')
decr = g.call(KML + ':Subtract_IntInt', 'TriesMinus1', 350, 3550, B='1'); link(tgr['Tries'], decr['A'])
std = g.setv('Tries', INT, 450, 3300, name='UseCheck'); link(decr['ReturnValue'], std['Tries']); ex(srp0, std)
lrt = log(g, 700, 3300, msg='SetSail: DAY CHECK', name='LogRetry'); ex(std, lrt)
more = g.call(KML + ':Greater_IntInt', 'MoreChecks', 900, 3550, B='0'); link(std['Output_Get'], more['A'])
bmo = g.branch(950, 3300, 'BrMoreChecks'); link(more['ReturnValue'], bmo['Condition']); ex(lrt, bmo)
s1, l1 = schedule(1200, 3300, 'Sched1'); ex(bmo, s1); l1 = {'then': l1['OutputPin']}
link(bmo['else'], SCAN['InputPin']); link(l1['then'], SCAN['InputPin'])
md = concat(g, 400, 1600, 'SetSail: EVENT day start ', istr(g, evD['Day'], 200, 1600, 'DayStr'))
ld = log(g, 400, 1400, msg_pin=md, name='LogDay'); ex(evD, ld)
link(ld['then'], DAYCHAIN['InputPin'])
la = log(g, 400, 1800, msg='SetSail: EVENT dock activity changed', name='LogActivity'); ex(evA, la); link(la['then'], EVT['InputPin'])
nb = struct_node(g, 'Break', NOTICE, ['noticeId', 'noticeType', 'noticeTitleId', 'noticeDescId', 'ship'], 200, 2400, 'BreakNotice')
link(evN['noticeData'], nb['NauticalNotice'])
shn = g.call(KSL + ':GetDisplayName', 'NoticeShip', 450, 2600); link(nb['ship'], shn['Object'])
mn = concat(g, 600, 2400, 'SetSail: EVENT notice ', istr(g, nb['noticeId'], 400, 2450, 'NoticeIdStr'),
            ' type ', nstr(g, nb['noticeType'], 400, 2500, 'NoticeTypeStr'),
            ' title ', nstr(g, nb['noticeTitleId'], 400, 2550, 'NoticeTitleStr'),
            ' desc ', nstr(g, nb['noticeDescId'], 400, 2650, 'NoticeDescStr'),
            ' ship ', shn['ReturnValue'])
ln = log(g, 400, 2200, msg_pin=mn, name='LogNotice'); ex(evN, ln); link(ln['then'], EVT['InputPin'])
mcl = concat(g, 400, 3000, 'SetSail: EVENT notice cleared ', istr(g, evC['clearedNotice'], 200, 3000, 'ClearedStr'))
lcl = log(g, 400, 2800, msg_pin=mcl, name='LogNoticeCleared'); ex(evC, lcl); link(lcl['then'], EVT['InputPin'])

# ---- check every known dock: log its state, send a ready ship to sea
X1, Y1 = 1500, 1600
vok = g.branch(X1, Y1, 'BrHaveView'); link(is_valid(g, g.get('View', VIEW_T, X1, Y1 + 200, name='ViewChk')['View'], X1 + 50, Y1 + 300, 'ViewValid'), vok['Condition'])
link(SCAN['OutputPin'], vok['execute'])
lp = g.macro('ForEachLoop', ACTOR, X1 + 250, Y1, name='CheckLoop')
link(g.get('Docks', ARR(ACTOR), X1 + 50, Y1 + 450, name='DocksL')['Docks'], lp['Array']); ex(vok, lp, 'then', 'Exec')
dock, idx = lp['Array Element'], lp['Array Index']
bdv = g.branch(X1 + 550, Y1, 'BrDockValid'); link(is_valid(g, dock, X1 + 550, Y1 + 300, 'DockValid'), bdv['Condition']); ex(lp, bdv, 'LoopBody')
vg = g.get('View', VIEW_T, X1 + 700, Y1 + 450, name='ViewCtx')
sctx = g.call(KSL + ':SetObjectPropertyByName', 'SetContext', X1 + 800, Y1, PropertyName='Context')
link(vg['View'], sctx['Object']); link(dock, sctx['Value']); ex(bdv, sctx)
chs = g.call(VIEW + ':CalcHudState', 'DockState', X1 + 1100, Y1); link(vg['View'], chs['self']); ex(sctx, chs)
st = struct_node(g, 'Break', UIDATA, ['workDockPhase', 'allowDeparture', 'shipIsDocked', 'shipIsReturning', 'nCrewPresent',
                                       'maxCrewPresent', 'defaultDepartureAction', 'shipStatusKey', 'canReachOpenOcean',
                                       'canAffordApproval', 'departureApprovalCost', 'validTradePartner', 'hasIncompatibleRequests',
                                       'assignedShipNauticalState'], X1 + 1100, Y1 + 300, 'BreakState')
link(chs['ReturnValue'], st['WorkDock_UIData'])
sst = struct_node(g, 'Break', '/Script/NauticalKit.NauticalShipState', ['shipName'], X1 + 1300, Y1 + 1200, 'BreakShip')
link(st['assignedShipNauticalState'], sst['NauticalShipState'])
shipname = sst['shipName']
auto = g.call(KSTR + ':Contains', 'HasAutoTag', X1 + 1500, Y1 + 1200, Substring=AUTO_TAG, bUseCase='false', bSearchFromEnd='false'); link(shipname, auto['SearchIn'])
ph = g.call(KSTR + ':Conv_ByteToString', 'PhaseStr', X1 + 1400, Y1 + 300); link(st['workDockPhase'], ph['InByte'])
optid_pin = array_item(g, g.get('DockOpt', ARR(STR), X1 + 1100, Y1 + 900, name='KindG')['DockOpt'], STR, idx, X1 + 1300, Y1 + 900, 'ThisOption')
kind = {'ReturnValue': optid_pin}
dn = g.call(KSL + ':GetDisplayName', 'DockName', X1 + 1400, Y1 + 1050); link(dock, dn['Object'])
ms = concat(g, X1 + 1700, Y1 + 400, 'SetSail:   ', kind['ReturnValue'], ' dock ', dn['ReturnValue'],
            ' phase ', ph['ReturnValue'],
            ' allow ', bstr(g, st['allowDeparture'], X1 + 1400, Y1 + 400, 'AllowStr'),
            ' docked ', bstr(g, st['shipIsDocked'], X1 + 1400, Y1 + 450, 'DockedStr'),
            ' returning ', bstr(g, st['shipIsReturning'], X1 + 1400, Y1 + 500, 'ReturningStr'),
            ' ocean ', bstr(g, st['canReachOpenOcean'], X1 + 1400, Y1 + 550, 'OceanStr'),
            ' crew ', istr(g, st['nCrewPresent'], X1 + 1400, Y1 + 600, 'CrewStr'),
            '/', istr(g, st['maxCrewPresent'], X1 + 1400, Y1 + 650, 'CrewMaxStr'),
            ' status ', nstr(g, st['shipStatusKey'], X1 + 1400, Y1 + 700, 'StatusStr'),
            ' approval ', istr(g, st['departureApprovalCost'], X1 + 1400, Y1 + 750, 'ApprCostStr'),
            ' affordable ', bstr(g, st['canAffordApproval'], X1 + 1400, Y1 + 800, 'AffordStr'),
            ' partner ', bstr(g, st['validTradePartner'], X1 + 1400, Y1 + 850, 'PartnerStr'),
            ' incompatible ', bstr(g, st['hasIncompatibleRequests'], X1 + 1400, Y1 + 900, 'IncompStr'),
            ' goal ', st['defaultDepartureAction'], ' ship ', shipname)
lst = log(g, X1 + 1400, Y1, msg_pin=ms, name='LogDock'); ex(chs, lst)
# ready?  phase 5 AND allowDeparture AND option on for this dock kind
isR = g.call(KSTR + ':EqualEqual_StrStr', 'PhaseReady', X1 + 1700, Y1 + 200, B=READY_PHASE); link(ph['ReturnValue'], isR['A'])
br = g.branch(X1 + 1800, Y1, 'BrReady'); link(isR['ReturnValue'], br['Condition']); ex(lst, br)
oid = {'ReturnValue': optid_pin}
bho = g.branch(X1 + 2100, Y1 + 100, 'BrAutoTag'); link(auto['ReturnValue'], bho['Condition']); ex(br, bho)
lho = log(g, X1 + 2600, Y1 + 900, msg='SetSail:   ready, but auto-sail is off (no %s in the ship name)' % AUTO_TAG, name='LogNotAuto'); ex(bho, lho, 'else')
bo2 = g.branch(X1 + 2450, Y1, 'BrAllowed'); link(st['allowDeparture'], bo2['Condition']); ex(bho, bo2)
lwt = log(g, X1 + 2700, Y1 + 700, msg='SetSail:   ready but departure not allowed yet (crew?) - next check', name='LogWaiting'); ex(bo2, lwt, 'else')
# PlayerController_Play.HandleHudAction("dispatchShipFromDock") finds the dock by paramGrid (exe 0.7.207:
# GetGridActorAt(paramGrid) -> WorkDock -> dispatch). The window's own action carries no cell, so we fill in
# the dock's root cell and call the handler directly.
dga = g.cast(PA + 'GridActor', False, X1 + 2600, Y1, name='DockAsGridActor'); link(dock, dga['Object']); ex(bo2, dga)
dfp = g.get('liveFootprint', STRUCT(PA + 'GridFootprint'), X1 + 2600, Y1 + 300, owner=PA + 'GridActor', name='DockFootprint'); link(dga['AsGridActor'], dfp['self'])
dfb = struct_node(g, 'Break', PA + 'GridFootprint', ['rootPosition'], X1 + 2850, Y1 + 300, 'BreakDockFootprint'); link(dfp['liveFootprint'], dfb['GridFootprint'])
mk = struct_node(g, 'Make', PA + 'HudAction', ['action', 'paramGrid'], X1 + 3100, Y1 + 250, 'MakeDispatch')
mk.set('action', 'dispatchShipFromDock'); link(dfb['rootPosition'], mk['paramGrid'])
pcd = g.call(GS + ':GetPlayerController', 'PCDispatch', X1 + 2850, Y1 + 600)
cpd = g.cast(PA + 'PlayerController_Play', False, X1 + 2850, Y1, name='AsPlayPC'); link(pcd['ReturnValue'], cpd['Object']); ex(dga, cpd)
ra = g.call(PA + 'PlayerController_Play:HandleHudAction', 'SendToSea', X1 + 3350, Y1); link(cpd['AsPlayerController_Play'], ra['self']); link(mk['HudAction'], ra['HudAction']); ex(cpd, ra)
cell = g.call(KSTR + ':Conv_IntVectorToString', 'DockCellStr', X1 + 3350, Y1 + 450); link(dfb['rootPosition'], cell['InIntVec'])
lnp = log(g, X1 + 3100, Y1 - 300, msg='SetSail: no PlayerController_Play - cannot send', name='LogNoPC'); ex(cpd, lnp, 'CastFailed')
lng = log(g, X1 + 2850, Y1 - 450, msg='SetSail: dock is not a GridActor - cannot send', name='LogNoGrid'); ex(dga, lng, 'CastFailed')
jd = knot(g, X1 + 3550, Y1 - 50, 'AfterDispatch'); link(ra['then'], jd['InputPin'])
msent = concat(g, X1 + 3700, Y1 + 300, 'SetSail: sent ', kind['ReturnValue'], ' ship of ', dn['ReturnValue'], ' to sea (cell ', cell['ReturnValue'], ')')
lsent = log(g, X1 + 4200, Y1, msg_pin=msent, name='LogSent'); link(jd['OutputPin'], lsent['execute'])




open(OUT + '/BP_MapLoad.txt', 'w', encoding='utf-8').write(g.text())

# =========================================================================== WBP_SailWaiter (Widget BP, parent UserWidget, empty designer)
# Variables: Waited (Float), FirstSent (Boolean). Event Dispatcher: Done (no inputs)
g = Graph(WAITER)
evT = g.event('/Script/UMG.UserWidget', 'Tick', [('MyGeometry', STRUCT('/Script/SlateCore.Geometry')), ('InDeltaTime', FLT)], 'Tick', 0, 0)
wg_ = g.get('Waited', DBL, 0, 250, name='WaitedGet')
add = g.call(KML + ':Add_DoubleDouble', 'AddDelta', 200, 250); link(wg_['Waited'], add['A']); link(evT['InDeltaTime'], add['B'])
sw = g.setv('Waited', DBL, 300, 0, name='SetWaited'); link(add['ReturnValue'], sw['Waited']); ex(evT, sw)
# first tick: call Done right away (window usually exists by then); at 0.15 s: call it again and hide
fs = g.get('FirstSent', BOOL, 400, 400, name='FirstSentGet')
bf = g.branch(500, 0, 'BrFirstSent'); link(fs['FirstSent'], bf['Condition']); ex(sw, bf)
sf1 = g.setv('FirstSent', BOOL, 700, -250, value='true', name='MarkFirstSent'); ex(bf, sf1, 'else')
cd1 = g.add(BG + 'K2Node_CallDelegate', 'CallDoneFirst', ['DelegateReference=(MemberName="Done",bSelfContext=True)'], 950, -250)
cd1.pin('execute', EXEC); cd1.pin('then', EXEC, out=True); cd1.pin('self', T('object', obj=WAITER_CLS), friendly='NSLOCTEXT("K2Node", "Target", "Target")')
ex(sf1, cd1)
ge = g.call(KML + ':GreaterEqual_DoubleDouble', 'LongEnough', 700, 250, B='0.15'); link(sw['Output_Get'], ge['A'])
bw = g.branch(750, 0, 'BrLongEnough'); link(ge['ReturnValue'], bw['Condition']); ex(bf, bw)
s0 = g.setv('Waited', DBL, 1000, 0, value='0.0', name='ResetWaited'); ex(bw, s0)
sf0 = g.setv('FirstSent', BOOL, 1050, 150, value='false', name='ResetFirstSent'); ex(s0, sf0)
s0 = sf0
rp = g.call('/Script/UMG.Widget:RemoveFromParent', 'Hide', 1300, 0); ex(s0, rp)
cc_ = g.add(BG + 'K2Node_CallDelegate', 'CallDone', ['DelegateReference=(MemberName="Done",bSelfContext=True)'], 1350, 0)
cc_.pin('execute', EXEC); cc_.pin('then', EXEC, out=True)
cc_.pin('self', T('object', obj=WAITER_CLS), friendly='NSLOCTEXT("K2Node", "Target", "Target")', hidden=False)
ex(rp, cc_)
open(OUT + '/WBP_SailWaiter.txt', 'w', encoding='utf-8').write(g.text())
print('wrote waiter')
if False:
    open(OUT + '/BP_MapLoad.txt', 'w', encoding='utf-8').write(g.text())
print('wrote', OUT)
