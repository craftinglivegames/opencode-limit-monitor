# -*- coding: utf-8 -*-
"""
OpenCode Limit Monitor — монитор лимитов, сессий и сети OpenCode.

Что делает:
  1. В фоне постоянно мониторит лимиты бесплатных моделей (пробы).
  2. Держит окно с реальным временем: список бесплатных моделей (статус,
     контекст, провайдер, когда проверялась), состояние сервера OpenCode,
     состояние WARP, публичный IP.
  3. Показывает открытые сессии OpenCode: модель, outcome, сколько молчит,
     % занятости контекста (как «Использование» в правом верхнем углу OpenCode).
  4. Автовосстановление сессий (с чекбоксами исключения per-session):
     - будит прерванную/упавшую сессию фразой восстановления;
     - сначала на ТОЙ ЖЕ модели, если она освободилась;
     - иначе берёт первую рабочую из порядка маршрутизатора;
     - если свободных нет — ждёт восстановления и будит сразу.
  5. Чекбоксы per-session: автовосстановление, маршрутизация (снять = закрепить
     текущую модель и не переключать), автобекап, «не трогать».
  6. Автобекап сессий: после каждого ответа ИИ выгружает сессию и её сообщения
     в JSON в выбранную папку (для каждой сессии своя подпапка), старый бекап
     заменяется новым. Чекбокс исключения per-session.
  7. Кнопки WARP/oplire: status / connect / disconnect / reset / quick-reset —
     без ввода в терминал. Проверка компонентов, автоскачивание oplire с GitHub.
  8. HTTP-интерфейс для ИИ в OpenCode:
        GET  /state /models /sessions /log
        POST /command  {action: ...}

Запуск:  python opencode_limit_monitor.py     (окно можно свернуть)
Останов: закрыть окно.  Headless:  ... --headless
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    HAS_GUI = True
except Exception:
    HAS_GUI = False

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, 'limit_monitor_config.json')
LOG_PATH = os.path.join(HERE, 'limit_monitor.log')
APP_NAME = 'OpenCode Limit Monitor'
HTTP_PORT = 8799


# ============================ авто-детект путей ============================
def _find_opencode_cli():
    """Найти opencode-cli.exe: glob по AppData, затем PATH."""
    base = os.path.expandvars(r'%APPDATA%\ai.opencode.desktop\cli')
    hits = glob.glob(os.path.join(base, '*', 'opencode-cli.exe'))
    if hits:
        return sorted(hits)[-1]                       # последняя версия
    for name in ('opencode-cli.exe', 'opencode.exe', 'opencode'):
        p = shutil.which(name)
        if p:
            return p
    return None


def _find_warp():
    cands = [
        r'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe',
        r'C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe',
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which('warp-cli') or 'warp-cli'


def _find_oplire():
    pats = [
        os.path.expandvars(r'%USERPROFILE%\Downloads\oplire*.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\oplire*\oplire*.exe'),
        r'C:\oplire*\oplire*.exe',
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\oplire*\oplire*.exe'),
    ]
    for p in pats:
        hits = glob.glob(p)
        if hits:
            return sorted(hits)[-1]
    return None


OPCLI = _find_opencode_cli()
WARP = _find_warp()
OPLIRE = _find_oplire()
OPLIRE_URL = ('https://github.com/theyonecodes/oplire/releases/latest/download/'
              'oplire.exe')


# ============================ конфиг ============================
DEFAULT_CFG = {
    'phrase': 'продолжай',
    'poll_sessions_sec': 45,
    'poll_models_sec': 300,
    'poll_network_sec': 60,
    'poll_usage_sec': 30,
    'poll_backup_sec': 40,
    'prefer': [
        'glm-5.3-flash-free',
        'deepseek-v4.1-flash-free',
        'mimo-v2.6-flash-free',
        'longcat-2.5-preview-free',
        'space-bunny-free',
        'nemotron-3.5-super-vl-free',
        'big-pickle',
        'nemotron-3-ultra-free',
        'nemotron-3.5-lightning-free',
    ],
    'dead': ['beam-free'],
    'auto_recover': True,
    'idle_min_to_wake': 8,
    'backup_enabled': False,
    'backup_root': '',
    'http_port': HTTP_PORT,
    'warp_reset_via_oplire': True,
    # per-session флаги (множества sid):
    'no_touch': [],        # не трогать совсем (не восстанавливать, не бекапить)
    'recover_off': [],     # исключить из автовосстановления
    'no_route': [],        # исключить из маршрутизации (закрепить модель)
    'backup_off': [],      # исключить из автобекапа
    'pinned': {},          # sid -> закреплённая модель
}


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        if os.path.exists(CFG_PATH):
            user = json.load(open(CFG_PATH, encoding='utf-8'))
            cfg.update(user)
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


CFG = load_cfg()


# ============================ OpenCode API клиент ============================
def opcli(method, path, data=None, timeout=180):
    if not OPCLI:
        return {'_err': 'opencode-cli не найден'}
    cmd = [OPCLI, 'api', method, path]
    if data is not None:
        cmd += ['-d', json.dumps(data, ensure_ascii=False)]
    env = dict(os.environ)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {'_err': 'timeout'}
    except Exception as e:
        return {'_err': str(e)[:120]}
    txt = ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', 'replace').strip()
    if not txt:
        return {'data': {}, '_empty': True}
    try:
        return json.loads(txt)
    except Exception:
        return {'_raw': txt[:300]}


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', 'replace')
    except Exception as e:
        return 'ERR: %s' % e


# ============================ WARP / oplire ============================
def warp_status():
    txt = sh([WARP, 'status'], 25)
    m = re.search(r'Status update:\s*(\w+)', txt)
    state = m.group(1) if m else '?'
    m2 = re.search(r'Network:\s*(\w+)', txt)
    net = m2.group(1) if m2 else ''
    return {'state': state, 'network': net, 'raw': txt.strip()[:160]}


def public_ip():
    for url in ('https://api.ipify.org?format=json', 'https://ifconfig.me/ip'):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                txt = r.read().decode('utf-8', 'replace').strip()
            if 'ipify' in url:
                return json.loads(txt).get('ip', txt)
            return txt
        except Exception:
            continue
    return '?'


def warp_reset(log=None):
    def note(t):
        if log:
            log(t)
    if CFG.get('warp_reset_via_oplire') and OPLIRE and os.path.exists(OPLIRE):
        note('oplire reset…')
        out = sh([OPLIRE, 'reset'], 120)
        note('oplire: %s' % out.strip()[:150])
        if 'error' not in out.lower() and 'ERR' not in out:
            time.sleep(12)
            return True
        note('oplire не справился, пробую quick-reset…')
        out = sh([OPLIRE, 'quick-reset'], 90)
        note('oplire quick: %s' % out.strip()[:150])
        time.sleep(10)
        return True
    note('warp-cli: disconnect…')
    sh([WARP, 'disconnect'], 25)
    time.sleep(3)
    note('warp-cli: connect…')
    sh([WARP, 'connect'], 25)
    time.sleep(12)
    st = warp_status()
    note('WARP: %s %s' % (st['state'], st['network']))
    return True


def check_components():
    comps = []
    comps.append(('opencode-cli', bool(OPCLI) and os.path.exists(OPCLI)))
    comps.append(('warp-cli', bool(WARP) and (os.path.exists(WARP) or shutil.which(WARP))))
    comps.append(('oplire (обнулялка)', bool(OPLIRE) and os.path.exists(OPLIRE)))
    comps.append(('python', True))
    node = shutil.which('node')
    comps.append(('node', bool(node)))
    svc = sh(['sc', 'query', 'CloudflareWARP'], 20)
    comps.append(('служба WARP', 'RUNNING' in svc))
    return comps


def download_oplire(log=None):
    if OPLIRE and os.path.exists(OPLIRE):
        if log:
            log('oplire уже установлен: %s' % OPLIRE)
        return True
    if log:
        log('скачиваю oplire с GitHub…')
    try:
        req = urllib.request.Request(OPLIRE_URL,
                                     headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = r.read()
        dst = os.path.expandvars(r'%LOCALAPPDATA%\oplire\oplire.exe')
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'wb') as f:
            f.write(data)
        if log:
            log('oplire скачан: %s (%d КБ)' % (dst, len(data) // 1024))
        return True
    except Exception as e:
        if log:
            log('ошибка скачивания oplire: %s' % e)
        return False


# ============================ ядро ============================
class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.models = {}
        self.sessions = {}
        self.usage = {}          # sid -> {'pct': float, 'tokens': int}
        self.net = {'warp': {}, 'ip': '?'}
        self.server = {'ok': False, 'checked': 0}
        self.comps = []
        self.last_reset = 0
        self.log_lines = []
        self.probe_session = None
        self.running = True
        self.model_ctx = {}      # model_id -> context limit
        self._backup_ts = {}     # sid -> последний запечатанный created
        self._stop = threading.Event()

    # ---------- журнал ----------
    def log(self, msg):
        line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
        with self.lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 400:
                self.log_lines = self.log_lines[-300:]
        print(line, flush=True)
        try:
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    # ---------- флаги сессий ----------
    def flag(self, sid, name):
        if name == 'no_touch':
            return sid in CFG['no_touch']
        if name == 'recover_off':
            return sid in CFG['recover_off']
        if name == 'no_route':
            return sid in CFG['no_route']
        if name == 'backup_off':
            return sid in CFG['backup_off']
        return False

    def set_flag(self, sid, name, value):
        key = {'no_touch': 'no_touch', 'recover_off': 'recover_off',
               'no_route': 'no_route', 'backup_off': 'backup_off'}.get(name)
        if not key:
            return
        lst = CFG.setdefault(key, [])
        if value and sid not in lst:
            lst.append(sid)
        elif not value and sid in lst:
            lst.remove(sid)
        save_cfg(CFG)

    # ---------- каталог и контекст моделей ----------
    def catalog(self):
        data = []
        for attempt in range(3):
            d = opcli('GET', '/api/model', timeout=120)
            data = d.get('data') or []
            if data:
                break
            self.log('каталог пуст (попытка %d), повторяю…' % (attempt + 1))
            time.sleep(4)
        out = []
        ctx = {}
        for m in data:
            if not isinstance(m, dict):
                continue
            cost = m.get('cost') or []
            free = bool(cost) and all((t.get('input', 0) == 0 and
                                       t.get('output', 0) == 0) for t in cost)
            if not free and ((m.get('settings') or {}).get('apiKey') == 'public'):
                free = True
            if not free:
                continue
            caps = m.get('capabilities') or {}
            context = (m.get('limit') or {}).get('context')
            if m.get('id'):
                ctx[m['id']] = context
            out.append({
                'id': m.get('id'),
                'provider': m.get('providerID'),
                'name': m.get('name'),
                'ctx': context,
                'tools': bool(caps.get('tools')),
                'input': caps.get('input') or [],
                'status': 'каталог',
            })
        with self.lock:
            self.model_ctx.update(ctx)
        return out

    # ---------- проба модели ----------
    def _probe_session(self):
        if self.probe_session:
            return self.probe_session
        first = (CFG['prefer'] or ['glm-5.3-flash-free'])[0]
        d = opcli('POST', '/api/session',
                  {'model': {'providerID': 'opencode', 'id': first},
                   'directory': os.path.expandvars(r'%TEMP%\opencode')},
                  180)
        sid = ((d.get('data') or {}).get('id') or '')
        if sid:
            self.probe_session = sid
            self.log('пробная сессия: %s' % sid)
        return sid

    def probe_model(self, mid, provider='opencode'):
        sid = self._probe_session()
        if not sid:
            return 'НЕТ СЕССИИ', ''
        r = opcli('POST', '/api/session/%s/model' % sid,
                  {'model': {'providerID': provider, 'id': mid}}, 120)
        if isinstance(r, dict) and '_err' in r:
            return 'СБОЙ', r['_err']
        since = int(time.time() * 1000)
        opcli('POST', '/api/session/%s/prompt' % sid,
              {'text': 'Ответь одним словом: ОК',
               'model': {'providerID': provider, 'id': mid}}, 120)
        verdict, detail = 'ТИХО', ''
        for _ in range(24):
            time.sleep(5)
            m = opcli('GET', '/api/session/%s/message?limit=8&order=desc' % sid,
                      timeout=90)
            for msg in (m.get('data') or []):
                if msg.get('type') != 'assistant':
                    continue
                created = ((msg.get('time') or {}).get('created')) or 0
                if created and created < since - 2000:
                    continue
                err = msg.get('error')
                if err:
                    low = json.dumps(err, ensure_ascii=False).lower()
                    if 'rate limit' in low or '429' in low or 'quota' in low:
                        verdict, detail = 'ЛИМИТ', '429'
                    elif 'no longer' in low or 'unavailable' in low:
                        verdict, detail = 'МЁРТВ', low[:80]
                    else:
                        verdict, detail = 'ОШИБКА', low[:80]
                    break
                fin = msg.get('finish') or ''
                if fin and fin not in ('tool-calls', 'tool_calls'):
                    texts = [p.get('text', '').strip()
                             for p in (msg.get('content') or [])
                             if isinstance(p, dict) and p.get('type') == 'text'
                             and p.get('text', '').strip()]
                    verdict = 'РАБОТАЕТ'
                    detail = ' '.join(texts)[:60]
                    break
            if verdict != 'ТИХО':
                break
        return verdict, detail

    def probe_all(self):
        self.log('=== проба всех бесплатных моделей ===')
        cat = self.catalog()
        if not cat:
            self.log('каталог моделей пуст')
            return
        with self.lock:
            for m in cat:
                mid = m['id']
                if mid not in self.models:
                    self.models[mid] = {'status': 'ждёт проверки',
                                        'name': m.get('name'),
                                        'ctx': m.get('ctx'),
                                        'provider': m.get('provider'),
                                        'tools': m.get('tools')}
        for m in cat:
            if not self.running:
                break
            mid = m['id']
            if mid in CFG['dead']:
                with self.lock:
                    self.models[mid] = {'status': 'МЁРТВ (в списке)',
                                        'name': m.get('name'),
                                        'ctx': m.get('ctx'),
                                        'checked': time.time()}
                continue
            v, d = self.probe_model(mid, m.get('provider') or 'opencode')
            with self.lock:
                self.models[mid] = {'status': v, 'name': m.get('name'),
                                    'ctx': m.get('ctx'), 'note': d,
                                    'checked': time.time(),
                                    'provider': m.get('provider'),
                                    'tools': m.get('tools')}
            self.log('  %-34s %s %s' % (mid, v, d[:40]))
        with self.lock:
            self.server['ok'] = True
            self.server['checked'] = time.time()

    # ---------- маршрутизатор ----------
    def working_models(self):
        with self.lock:
            return [k for k, v in self.models.items()
                    if v.get('status') == 'РАБОТАЕТ']

    def router_pick(self, wanted=None):
        pool = wanted or CFG['prefer']
        ok = self.working_models()
        for m in pool:
            if m in ok and m not in CFG['dead']:
                return m
        for m in CFG['prefer']:
            if m in ok and m not in CFG['dead']:
                return m
        return None

    # ---------- сессии ----------
    def load_sessions(self):
        d = opcli('GET', '/api/session?limit=150&order=desc', timeout=200)
        data = d.get('data') or []
        now = time.time() * 1000
        out = {}
        for s in data:
            sid = s.get('id')
            if not sid:
                continue
            m = s.get('model') or {}
            upd = ((s.get('time') or {}).get('updated')) or 0
            out[sid] = {
                'title': (s.get('title') or '(без названия)')[:60],
                'model': m.get('id'),
                'provider': m.get('providerID'),
                'outcome': s.get('outcome'),
                'updated': upd,
                'idle_min': round((now - upd) / 60000, 1) if upd else 9999,
                'directory': ((s.get('location') or {}).get('directory') or ''),
            }
        with self.lock:
            self.sessions = out
            self.server['ok'] = True
        return out

    # ---------- % использования контекста ----------
    def _usage_for_session(self, sid, info):
        """% занятости контекста = последний завершённый input+cache+output / лимит."""
        m = opcli('GET', '/api/session/%s/message?limit=6&order=desc' % sid,
                  timeout=60)
        for msg in (m.get('data') or []):
            if msg.get('type') != 'assistant':
                continue
            tok = msg.get('tokens') or {}
            if not tok:
                continue                 # пропускаем недописанное/стриминг
            used = (tok.get('input', 0) + tok.get('output', 0) +
                    (tok.get('cache') or {}).get('read', 0))
            ctxlim = self.model_ctx.get(info.get('model')) or 0
            if ctxlim:
                return {'pct': round(used / ctxlim * 100, 1), 'tokens': used}
            return {'pct': None, 'tokens': used}
        return None

    def loop_usage(self):
        while self.running and not self._stop.is_set():
            try:
                with self.lock:
                    sess = dict(self.sessions)
                for sid, info in sess.items():
                    if self._stop.is_set() or not self.running:
                        break
                    try:
                        u = self._usage_for_session(sid, info)
                    except Exception:
                        u = None
                    if u is not None:
                        with self.lock:
                            self.usage[sid] = u
                    time.sleep(0.4)
            except Exception as e:
                self.log('usage: %s' % e)
            self._stop.wait(CFG['poll_usage_sec'])

    # ---------- автовосстановление ----------
    def needs_recovery(self, sid, info):
        if sid in CFG['no_touch'] or sid == self.probe_session:
            return False, ''
        if sid in CFG['recover_off']:
            return False, ''
        if not CFG.get('auto_recover'):
            return False, ''
        if info.get('idle_min', 9999) < CFG.get('idle_min_to_wake', 8):
            return False, ''
        if info.get('outcome') == 'failed':
            return True, 'упала'
        m = opcli('GET', '/api/session/%s/message?limit=4&order=desc' % sid,
                  timeout=90)
        for msg in (m.get('data') or []):
            if msg.get('type') != 'assistant':
                continue
            fin = msg.get('finish') or ''
            if fin in ('tool-calls', 'tool_calls', 'error', ''):
                return True, 'прервана (%s)' % (fin or 'нет финала')
            return False, ''
        return False, ''

    def recover_session(self, sid, info):
        phrase = CFG['phrase']
        own = info.get('model')
        target = None
        #1) закреплённая модель — только она
        pinned = CFG.get('pinned', {}).get(sid)
        if pinned and pinned not in CFG['dead']:
            with self.lock:
                st = (self.models.get(pinned) or {}).get('status')
            if st == 'РАБОТАЕТ':
                target = pinned
            else:
                self.log('  %s: закреплена %s, но она в лимите — жду'
                         % (info.get('title', '')[:25], pinned))
                return False
        # 2) если маршрутизация отключена — своя модель
        elif sid in CFG['no_route']:
            if own and own not in CFG['dead']:
                with self.lock:
                    st = (self.models.get(own) or {}).get('status')
                if st == 'РАБОТАЕТ':
                    target = own
                else:
                    self.log('  %s: маршрутизация выкл, %s в лимите — жду'
                             % (info.get('title', '')[:25], own))
                    return False
        # 3) своя модель, если свободна
        if not target and own and own not in CFG['dead']:
            with self.lock:
                st = (self.models.get(own) or {}).get('status')
            if st == 'РАБОТАЕТ':
                target = own
        # 4) иначе маршрутизатор
        if not target:
            target = self.router_pick()
        if not target:
            self.log('  %s: свободных моделей нет, жду…'
                     % info.get('title', '')[:30])
            return False
        r = opcli('POST', '/api/session/%s/model' % sid,
                  {'model': {'providerID': 'opencode', 'id': target}}, 150)
        ok = (isinstance(r, dict) and ('data' in r or r.get('_empty')))
        if not ok:
            self.log('  %s: сбой переключения на %s'
                     % (info.get('title', '')[:30], target))
            return False
        p = opcli('POST', '/api/session/%s/prompt' % sid,
                  {'text': phrase, 'resume': True}, 150)
        ok2 = (isinstance(p, dict) and ('data' in p or p.get('_empty')))
        self.log('  %s → %s | «%s» %s' % (info.get('title', '')[:30], target,
                                          phrase, 'отправлено' if ok2 else 'СБОЙ'))
        return ok2

    def switch_model(self, sid, model):
        r = opcli('POST', '/api/session/%s/model' % sid,
                  {'model': {'providerID': 'opencode', 'id': model}}, 150)
        ok = (isinstance(r, dict) and ('data' in r or r.get('_empty')))
        self.log('ручное переключение %s → %s: %s' % (sid[-6:], model,
                                                      'ок' if ok else 'сбой'))
        return ok

    # ---------- автобекап ----------
    def backup_session(self, sid, force=False):
        root = CFG.get('backup_root') or ''
        if not root:
            return False
        if sid in CFG['backup_off'] or sid in CFG['no_touch'] \
                or sid == self.probe_session:
            return False
        info = self.sessions.get(sid) or {}
        # дёшево проверяем, появилось ли новое сообщение
        m = opcli('GET', '/api/session/%s/message?limit=1&order=desc' % sid,
                  timeout=60)
        last_created = 0
        for msg in (m.get('data') or []):
            last_created = ((msg.get('time') or {}).get('created')) or 0
        if not force and self._backup_ts.get(sid) == last_created:
            return False                       # ничего нового
        # полный экспорт (лимит API = 200; храним newest-200 в хронологии)
        meta = opcli('GET', '/api/session/%s' % sid, timeout=90)
        msgs = opcli('GET', '/api/session/%s/message?limit=200&order=desc' % sid,
                     timeout=180)
        mlist = msgs.get('data') or []
        if len(mlist) == 200:
            mlist = list(reversed(mlist))       # в хронологическом порядке
        folder = os.path.join(root, re.sub(r'[^\w\-]', '_', sid))
        try:
            os.makedirs(folder, exist_ok=True)
            json.dump(meta.get('data') or meta,
                      open(os.path.join(folder, 'session.json'), 'w',
                           encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            json.dump({'count': len(mlist),
                       'truncated': len(mlist) == 200,
                       'messages': mlist},
                      open(os.path.join(folder, 'messages.json'), 'w',
                           encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            self._backup_ts[sid] = last_created
            self.log('бекап: %s → %s (%d сообщ.)' % (
                info.get('title', sid)[:24], folder, len(mlist)))
            return True
        except Exception as e:
            self.log('бекап ошибка %s: %s' % (sid[-6:], e))
            return False

    def loop_backup(self):
        while self.running and not self._stop.is_set():
            try:
                if CFG.get('backup_enabled') and CFG.get('backup_root'):
                    with self.lock:
                        sess = dict(self.sessions)
                    for sid in sess:
                        if self._stop.is_set() or not self.running:
                            break
                        try:
                            self.backup_session(sid)
                        except Exception as e:
                            self.log('бекап %s: %s' % (sid[-6:], e))
                        time.sleep(0.3)
            except Exception as e:
                self.log('backup loop: %s' % e)
            self._stop.wait(CFG['poll_backup_sec'])

    # ---------- фоновые циклы ----------
    def loop_models(self):
        while self.running and not self._stop.is_set():
            try:
                self.probe_all()
            except Exception as e:
                self.log('пробы: %s' % e)
            self._stop.wait(CFG['poll_models_sec'])

    def loop_sessions(self):
        while self.running and not self._stop.is_set():
            try:
                sess = self.load_sessions()
                if CFG.get('auto_recover') and self.router_pick():
                    for sid, info in sess.items():
                        need, why = self.needs_recovery(sid, info)
                        if need:
                            self.log('восстановление: %s (%s)'
                                     % (info['title'][:35], why))
                            self.recover_session(sid, info)
            except Exception as e:
                self.log('сессии: %s' % e)
            self._stop.wait(CFG['poll_sessions_sec'])

    def loop_network(self):
        while self.running and not self._stop.is_set():
            try:
                st = warp_status()
                ip = public_ip()
                with self.lock:
                    self.net = {'warp': st, 'ip': ip, 'checked': time.time()}
            except Exception as e:
                self.log('сеть: %s' % e)
            self._stop.wait(CFG['poll_network_sec'])

    def loop_comps(self):
        while self.running and not self._stop.is_set():
            try:
                with self.lock:
                    self.comps = check_components()
            except Exception:
                pass
            self._stop.wait(600)

    # ---------- состояние ----------
    def snapshot(self):
        with self.lock:
            models = json.loads(json.dumps(self.models))
            sessions = json.loads(json.dumps(self.sessions))
            usage = json.loads(json.dumps(self.usage))
        for sid, s in sessions.items():
            u = usage.get(sid) or {}
            s['usage_pct'] = u.get('pct')
            s['usage_tokens'] = u.get('tokens')
            s['flags'] = {
                'no_touch': self.flag(sid, 'no_touch'),
                'recover_off': self.flag(sid, 'recover_off'),
                'no_route': self.flag(sid, 'no_route'),
                'backup_off': self.flag(sid, 'backup_off'),
                'pinned': CFG.get('pinned', {}).get(sid),
            }
        with self.lock:
            net = dict(self.net)
            comps = list(self.comps)
            server = dict(self.server)
            log_lines = list(self.log_lines[-40:])
        return {
            'time': time.strftime('%d.%m.%Y %H:%M:%S'),
            'server_ok': server.get('ok'),
            'server_checked': server.get('checked'),
            'models': models,
            'sessions': sessions,
            'network': net,
            'components': comps,
            'last_reset': self.last_reset,
            'paths': {'opencode_cli': OPCLI, 'warp': WARP, 'oplire': OPLIRE},
            'settings': {
                'phrase': CFG['phrase'],
                'auto_recover': CFG['auto_recover'],
                'backup_enabled': CFG['backup_enabled'],
                'backup_root': CFG['backup_root'],
                'prefer': CFG['prefer'],
                'dead': CFG['dead'],
            },
            'log': log_lines,
        }

    def start(self):
        for fn in (self.loop_models, self.loop_sessions, self.loop_network,
                   self.loop_comps, self.loop_usage, self.loop_backup):
            threading.Thread(target=fn, daemon=True).start()
        self.log('монитор запущен (opencode-cli: %s)' % (OPCLI or 'НЕ НАЙДЕН'))


# ============================ HTTP-интерфейс для ИИ ============================
def make_handler(engine):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            snap = engine.snapshot()
            if self.path.startswith('/models'):
                self._send({'models': snap['models']})
            elif self.path.startswith('/sessions'):
                self._send({'sessions': snap['sessions']})
            elif self.path.startswith('/log'):
                self._send({'log': snap['log']})
            elif self.path.startswith('/state'):
                self._send(snap)
            else:
                self._send({'ok': True,
                            'endpoints': ['/state', '/models', '/sessions',
                                          '/log', '/command']})

        def do_POST(self):
            if not self.path.startswith('/command'):
                self._send({'error': 'use POST /command'}, 400)
                return
            ln = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(ln) or b'{}')
            except Exception:
                body = {}
            act = body.get('action')
            p = body.get('params') or {}
            engine.log('команда ИИ: %s %s' % (act, p))
            if act == 'reset_ip':
                threading.Thread(target=warp_reset, args=(engine.log,),
                                 daemon=True).start()
                self._send({'ok': True, 'note': 'обнуление в фоне'})
            elif act == 'probe_all':
                threading.Thread(target=engine.probe_all, daemon=True).start()
                self._send({'ok': True})
            elif act == 'wake':
                sid = p.get('sid')
                info = engine.sessions.get(sid) or {}
                ok = engine.recover_session(sid, info) if sid else False
                self._send({'ok': ok})
            elif act == 'switch_model':
                ok = engine.switch_model(p.get('sid'), p.get('model'))
                self._send({'ok': ok})
            elif act == 'set_flag':
                engine.set_flag(p.get('sid'), p.get('flag'), bool(p.get('value')))
                self._send({'ok': True})
            elif act == 'pin_model':
                CFG.setdefault('pinned', {})[p.get('sid')] = p.get('model')
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'unpin_model':
                CFG.get('pinned', {}).pop(p.get('sid'), None)
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'backup_now':
                sid = p.get('sid')
                if sid:
                    ok = engine.backup_session(sid, force=True)
                    self._send({'ok': ok})
                else:
                    with engine.lock:
                        sess = list(engine.sessions)
                    n = sum(1 for s in sess if engine.backup_session(s, force=True))
                    self._send({'ok': True, 'backed_up': n})
            elif act == 'set_backup_root':
                CFG['backup_root'] = p.get('path', '')
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'set_backup_enabled':
                CFG['backup_enabled'] = bool(p.get('value'))
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'set_phrase':
                CFG['phrase'] = p.get('phrase', 'продолжай')
                save_cfg(CFG)
                self._send({'ok': True, 'phrase': CFG['phrase']})
            elif act == 'set_auto_recover':
                CFG['auto_recover'] = bool(p.get('value'))
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'set_prefer':
                CFG['prefer'] = p.get('prefer', CFG['prefer'])
                save_cfg(CFG)
                self._send({'ok': True})
            elif act == 'download_oplire':
                threading.Thread(target=download_oplire, args=(engine.log,),
                                 daemon=True).start()
                self._send({'ok': True})
            else:
                self._send({'error': 'unknown action: %s' % act}, 400)

    return H


# ============================ GUI ============================
class App:
    def __init__(self, engine):
        self.e = engine
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry('1040x720')
        self._sel_sid = None
        self._build()
        self._refresh()

    def _build(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill='both', expand=True)
        self.nb = nb

        # --- Модели ---
        f1 = ttk.Frame(nb)
        nb.add(f1, text='  Модели  ')
        cols = ('статус', 'контекст', 'провайдер', 'проверена', 'детали')
        self.tree_m = ttk.Treeview(f1, columns=cols, show='tree headings',
                                   height=18)
        self.tree_m.heading('#0', text='модель')
        self.tree_m.column('#0', width=260)
        for c in cols:
            self.tree_m.heading(c, text=c)
            self.tree_m.column(c, width=120)
        self.tree_m.pack(side='top', fill='both', expand=True, padx=4, pady=4)
        b1 = ttk.Frame(f1)
        b1.pack(fill='x', padx=4, pady=2)
        ttk.Button(b1, text='Проверить все модели',
                   command=self._probe).pack(side='left', padx=2)
        ttk.Button(b1, text='Обнулить лимиты (WARP/oplire)',
                   command=self._reset).pack(side='left', padx=2)
        self.lbl_probe = ttk.Label(b1, text='')
        self.lbl_probe.pack(side='left', padx=10)

        # --- Сессии ---
        f2 = ttk.Frame(nb)
        nb.add(f2, text='  Сессии  ')
        cols2 = ('модель', 'outcome', 'молчит, мин', 'контекст %', 'авто',
                 'маршр.', 'бекап', 'закрепл.')
        self.tree_s = ttk.Treeview(f2, columns=cols2, show='tree headings',
                                   height=16)
        self.tree_s.heading('#0', text='сессия')
        self.tree_s.column('#0', width=300)
        widths = {'модель': 150, 'outcome': 80, 'молчит, мин': 90,
                  'контекст %': 85, 'авто': 60, 'маршр.': 60, 'бекап': 60,
                  'закрепл.': 130}
        for c in cols2:
            self.tree_s.heading(c, text=c)
            self.tree_s.column(c, width=widths.get(c, 90))
        self.tree_s.pack(side='top', fill='both', expand=True, padx=4, pady=4)
        self.tree_s.bind('<<TreeviewSelect>>', self._on_select)

        # панель выбранной сессии
        frm = ttk.LabelFrame(f2, text='Выбранная сессия')
        frm.pack(fill='x', padx=4, pady=4)
        self.lbl_sel = ttk.Label(frm, text='— выберите сессию —', anchor='w')
        self.lbl_sel.pack(fill='x', padx=6, pady=(4, 2))
        row = ttk.Frame(frm)
        row.pack(fill='x', padx=6, pady=2)
        self.v_touch = tk.BooleanVar()
        self.v_recoff = tk.BooleanVar()
        self.v_noroute = tk.BooleanVar()
        self.v_backupoff = tk.BooleanVar()
        self.cb_touch = ttk.Checkbutton(row, text='Не трогать',
                                        variable=self.v_touch,
                                        command=lambda: self._toggle('no_touch'))
        self.cb_recoff = ttk.Checkbutton(row, text='Исключить из автовосст.',
                                         variable=self.v_recoff,
                                         command=lambda: self._toggle('recover_off'))
        self.cb_noroute = ttk.Checkbutton(row, text='Без маршрутизации (своя модель)',
                                          variable=self.v_noroute,
                                          command=lambda: self._toggle('no_route'))
        self.cb_backupoff = ttk.Checkbutton(row, text='Исключить из бекапа',
                                            variable=self.v_backupoff,
                                            command=lambda: self._toggle('backup_off'))
        for cb in (self.cb_touch, self.cb_recoff, self.cb_noroute,
                   self.cb_backupoff):
            cb.pack(side='left', padx=6)
        row2 = ttk.Frame(frm)
        row2.pack(fill='x', padx=6, pady=(0, 4))
        ttk.Button(row2, text='Открыть сессию',
                   command=self._open_session).pack(side='left', padx=2)
        ttk.Button(row2, text='Закрепить текущую модель',
                   command=self._pin_current).pack(side='left', padx=2)
        ttk.Button(row2, text='Снять закрепление',
                   command=self._unpin).pack(side='left', padx=2)
        ttk.Button(row2, text='Восстановить сейчас',
                   command=self._wake_sel).pack(side='left', padx=2)
        ttk.Button(row2, text='Бекап сейчас',
                   command=self._backup_sel).pack(side='left', padx=2)
        self.lbl_pin = ttk.Label(row2, text='')
        self.lbl_pin.pack(side='left', padx=10)

        # --- Сеть / WARP / oplire ---
        f3 = ttk.Frame(nb)
        nb.add(f3, text='  Сеть и WARP  ')
        self.lbl_net = ttk.Label(f3, text='загрузка…', justify='left')
        self.lbl_net.pack(anchor='w', padx=8, pady=6)
        b3 = ttk.Frame(f3)
        b3.pack(fill='x', padx=8, pady=4)
        for txt, cmd in [('Статус', self._n_status),
                         ('Connect', self._n_connect),
                         ('Disconnect', self._n_disconnect),
                         ('Reset (новый IP)', self._n_reset),
                         ('Quick-reset', self._n_qreset),
                         ('Скачать oplire', self._n_dl)]:
            ttk.Button(b3, text=txt, command=cmd).pack(side='left', padx=2)
        self.lbl_nlog = ttk.Label(f3, text='', justify='left')
        self.lbl_nlog.pack(anchor='w', padx=8, pady=6)
        ttk.Label(f3, text='Компоненты:').pack(anchor='w', padx=8)
        self.lbl_comps = ttk.Label(f3, text='', justify='left')
        self.lbl_comps.pack(anchor='w', padx=8)

        # --- Настройки ---
        f4 = ttk.Frame(nb)
        nb.add(f4, text='  Настройки  ')
        ttk.Label(f4, text='Фраза восстановления:').pack(anchor='w', padx=8,
                                                          pady=(8, 0))
        self.var_phrase = tk.StringVar(value=CFG['phrase'])
        ttk.Entry(f4, textvariable=self.var_phrase, width=40).pack(anchor='w',
                                                                  padx=8)
        ttk.Button(f4, text='Сохранить фразу',
                   command=self._save_phrase).pack(anchor='w', padx=8, pady=4)
        self.var_auto = tk.BooleanVar(value=CFG['auto_recover'])
        ttk.Checkbutton(f4, text='Автовосстановление сессий (глобально)',
                        variable=self.var_auto,
                        command=self._save_auto).pack(anchor='w', padx=8, pady=2)
        self.var_backup = tk.BooleanVar(value=CFG['backup_enabled'])
        ttk.Checkbutton(f4, text='Автобекап сессий (в JSON-папку)',
                        variable=self.var_backup,
                        command=self._save_backup_enabled).pack(anchor='w',
                                                               padx=8, pady=2)
        ttk.Label(f4, text='Папка автобекапа:').pack(anchor='w', padx=8,
                                                     pady=(8, 0))
        brow = ttk.Frame(f4)
        brow.pack(fill='x', padx=8)
        self.var_backup_root = tk.StringVar(value=CFG['backup_root'])
        ttk.Entry(brow, textvariable=self.var_backup_root,
                  width=70).pack(side='left', padx=(0, 4))
        ttk.Button(brow, text='Обзор…',
                   command=self._browse_backup).pack(side='left')
        ttk.Label(f4, text='Порядок моделей (маршрутизатор), через запятую:').pack(
            anchor='w', padx=8, pady=(10, 0))
        self.var_prefer = tk.StringVar(value=', '.join(CFG['prefer']))
        ttk.Entry(f4, textvariable=self.var_prefer, width=90).pack(anchor='w',
                                                                   padx=8)
        ttk.Button(f4, text='Сохранить порядок',
                   command=self._save_prefer).pack(anchor='w', padx=8, pady=4)
        ttk.Label(f4, text='HTTP-интерфейс для ИИ: http://127.0.0.1:%d/state'
                           % CFG['http_port']).pack(anchor='w', padx=8, pady=8)
        ttk.Label(f4, text='GET /state /models /sessions /log | '
                           'POST /command {action:reset_ip|wake|switch_model|'
                           'set_flag|pin_model|backup_now|...}',
                  foreground='#666', wraplength=900,
                  justify='left').pack(anchor='w', padx=8)

        # --- Журнал ---
        f5 = ttk.Frame(nb)
        nb.add(f5, text='  Журнал  ')
        self.txt_log = tk.Text(f5, height=24, state='disabled')
        self.txt_log.pack(fill='both', expand=True, padx=4, pady=4)

    # ---------- выбор/действия сессии ----------
    def _on_select(self, _evt=None):
        sel = self.tree_s.selection()
        self._sel_sid = sel[0] if sel else None
        self._update_panel()

    def _update_panel(self):
        sid = self._sel_sid
        if not sid:
            self.lbl_sel.config(text='— выберите сессию —')
            self.lbl_pin.config(text='')
            return
        info = self.e.sessions.get(sid) or {}
        self.lbl_sel.config(text='%s   [%s]' % (info.get('title', sid), sid))
        # флаги читаем напрямую из конфига (в engine.sessions их нет)
        self.v_touch.set(self.e.flag(sid, 'no_touch'))
        self.v_recoff.set(self.e.flag(sid, 'recover_off'))
        self.v_noroute.set(self.e.flag(sid, 'no_route'))
        self.v_backupoff.set(self.e.flag(sid, 'backup_off'))
        pin = CFG.get('pinned', {}).get(sid)
        self.lbl_pin.config(text=('закреплена: %s' % pin) if pin else '')

    def _toggle(self, flag):
        sid = self._sel_sid
        if not sid:
            return
        var = {'no_touch': self.v_touch, 'recover_off': self.v_recoff,
               'no_route': self.v_noroute, 'backup_off': self.v_backupoff}[flag]
        self.e.set_flag(sid, flag, bool(var.get()))

    def _pin_current(self):
        sid = self._sel_sid
        if not sid:
            return
        model = (self.e.sessions.get(sid) or {}).get('model')
        if not model:
            messagebox.showinfo('Нет модели', 'У сессии не выбрана модель')
            return
        CFG.setdefault('pinned', {})[sid] = model
        save_cfg(CFG)
        self.e.log('закреплена модель %s для %s' % (model, sid[-6:]))
        self._update_panel()

    def _unpin(self):
        sid = self._sel_sid
        if not sid:
            return
        CFG.get('pinned', {}).pop(sid, None)
        save_cfg(CFG)
        self.e.log('снято закрепление %s' % sid[-6:])
        self._update_panel()

    def _wake_sel(self):
        sid = self._sel_sid
        if sid:
            info = self.e.sessions.get(sid) or {}
            threading.Thread(target=self.e.recover_session,
                             args=(sid, info), daemon=True).start()

    def _backup_sel(self):
        sid = self._sel_sid
        if not sid:
            return
        if not CFG.get('backup_root'):
            messagebox.showinfo('Папка бекапа', 'Сначала выберите папку в Настройках')
            return
        threading.Thread(target=self.e.backup_session, args=(sid, True),
                         daemon=True).start()

    def _open_session(self):
        sid = self._sel_sid
        if not sid:
            messagebox.showinfo('Сессия', 'Сначала выберите сессию в списке')
            return
        if not OPCLI or not os.path.isfile(OPCLI):
            messagebox.showinfo('OpenCode', 'opencode-cli не найден')
            return
        try:
            subprocess.Popen(
                [OPCLI, '-s', sid],
                creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.e.log('открыта сессия %s в терминале' % sid[-6:])
        except Exception as ex:
            messagebox.showinfo('Ошибка', 'Не удалось открыть сессию: %s' % ex)

    # ---------- модели/сеть ----------
    def _probe(self):
        threading.Thread(target=self.e.probe_all, daemon=True).start()

    def _reset(self):
        threading.Thread(target=warp_reset, args=(self.e.log,),
                         daemon=True).start()

    def _n_status(self):
        self._netlog(sh([WARP, 'status'], 25))

    def _n_connect(self):
        self._netlog(sh([WARP, 'connect'], 30))

    def _n_disconnect(self):
        self._netlog(sh([WARP, 'disconnect'], 30))

    def _n_reset(self):
        threading.Thread(target=warp_reset, args=(self._netlog,),
                         daemon=True).start()

    def _n_qreset(self):
        if OPLIRE and os.path.exists(OPLIRE):
            self._netlog(sh([OPLIRE, 'quick-reset'], 90))
        else:
            self._netlog('oplire не установлен — используй Reset')

    def _n_dl(self):
        threading.Thread(target=download_oplire, args=(self._netlog,),
                         daemon=True).start()

    def _netlog(self, txt):
        self.lbl_nlog.config(text=str(txt).strip()[:400])

    # ---------- настройки ----------
    def _save_phrase(self):
        CFG['phrase'] = self.var_phrase.get().strip() or 'продолжай'
        save_cfg(CFG)
        self.e.log('фраза восстановления: %s' % CFG['phrase'])

    def _save_auto(self):
        CFG['auto_recover'] = bool(self.var_auto.get())
        save_cfg(CFG)
        self.e.log('автовосстановление: %s' % CFG['auto_recover'])

    def _save_backup_enabled(self):
        CFG['backup_enabled'] = bool(self.var_backup.get())
        save_cfg(CFG)
        self.e.log('автобекап: %s' % CFG['backup_enabled'])

    def _browse_backup(self):
        d = filedialog.askdirectory(title='Папка автобекапа сессий')
        if d:
            self.var_backup_root.set(d)
            CFG['backup_root'] = d
            save_cfg(CFG)
            self.e.log('папка бекапа: %s' % d)

    def _save_prefer(self):
        CFG['prefer'] = [x.strip() for x in self.var_prefer.get().split(',')
                         if x.strip()]
        save_cfg(CFG)
        self.e.log('порядок моделей: %s' % ', '.join(CFG['prefer']))

    # ---------- обновление ----------
    def _refresh(self):
        try:
            snap = self.e.snapshot()
            self._draw(snap)
        except Exception as ex:
            print('refresh err:', ex, flush=True)
        self.root.after(4000, self._refresh)

    def _draw(self, snap):
        # модели
        self.tree_m.delete(*self.tree_m.get_children())
        for mid, v in snap['models'].items():
            ck = time.strftime('%H:%M:%S', time.localtime(v['checked'])) \
                if v.get('checked') else '—'
            self.tree_m.insert('', 'end', iid=mid, text=mid,
                               values=(v.get('status'), v.get('ctx') or '—',
                                       v.get('provider') or '—', ck,
                                       (v.get('note') or '')[:40]))
        # сессии
        sel = self.tree_s.selection()
        self.tree_s.delete(*self.tree_s.get_children())
        for sid, v in snap['sessions'].items():
            fl = v.get('flags') or {}
            auto = '—' if fl.get('no_touch') else ('выкл' if fl.get('recover_off')
                                                   else 'вкл')
            route = 'своя' if (fl.get('no_route') or fl.get('pinned')) else 'вкл'
            bak = 'выкл' if (fl.get('backup_off') or fl.get('no_touch')) else \
                ('вкл' if CFG['backup_enabled'] else '—')
            pct = v.get('usage_pct')
            pct_s = ('%s%%' % pct) if pct is not None else '—'
            self.tree_s.insert('', 'end', iid=sid,
                               text='%s  [%s]' % (v['title'], sid[-6:]),
                               values=(v.get('model') or '—',
                                       v.get('outcome') or '—',
                                       v.get('idle_min'), pct_s,
                                       auto, route, bak,
                                       fl.get('pinned') or '—'))
        if sel:
            try:
                self.tree_s.selection_set(sel[0])
            except Exception:
                pass
        self._update_panel()
        # сеть
        n = snap['network']
        w = n.get('warp') or {}
        self.lbl_net.config(text=(
            'WARP: %s %s\nIP: %s\nПоследний сброс: %s'
            % (w.get('state', '?'), w.get('network', ''), n.get('ip', '?'),
               time.strftime('%H:%M:%S', time.localtime(snap['last_reset']))
               if snap['last_reset'] else '—')))
        self.lbl_comps.config(text='\n'.join(
            '%s: %s' % (k, 'OK' if v else 'НЕТ') for k, v in snap['components']))
        self.txt_log.config(state='normal')
        self.txt_log.delete('1.0', 'end')
        self.txt_log.insert('end', '\n'.join(snap['log']))
        self.txt_log.config(state='disabled')
        self.lbl_probe.config(text='моделей проверено: %d' % len(snap['models']))


# ============================ запуск ============================
def main():
    headless = '--headless' in sys.argv
    engine = Engine()
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', CFG['http_port']),
                                  make_handler(engine))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print('HTTP-интерфейс: http://127.0.0.1:%d/state' % CFG['http_port'],
              flush=True)
    except Exception as e:
        print('HTTP не поднялся: %s' % e, flush=True)

    engine.start()

    if HAS_GUI and not headless:
        try:
            app = App(engine)
            app.root.mainloop()
        except Exception as e:
            engine.log('GUI упал (%s) → headless' % e)
            headless = True

    if headless or not HAS_GUI:
        print('headless-режим. Ctrl+C — остановка.', flush=True)
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            pass

    engine.running = False
    engine._stop.set()
    engine.log('монитор остановлен')


if __name__ == '__main__':
    main()
