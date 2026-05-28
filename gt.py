#!/usr/bin/env python3
"""
GuardaTensão v2 — Monitor de Energia com Detecção de Oscilação
Aesthetic: Painel industrial âmbar/laranja — rack elétrico retrô
"""

import curses, psutil, time, os, subprocess, threading, sys, glob
from collections import deque
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURAÇÕES
# ══════════════════════════════════════════════════════════════════════════════
REFRESH_RATE        = 0.8    # segundos
HISTORY_SIZE        = 72     # pontos no gráfico
ALERT_DROP_PCT      = 5      # queda brusca de X% = alerta
ALERT_LOW_PCT       = 20     # bateria baixa
CRITICAL_LOW_PCT    = 10     # bateria crítica
OSCIL_WINDOW_SECS   = 300    # janela para contar eventos (5 min)
OSCIL_EVENT_THRESH  = 3      # N eventos nessa janela = oscilação
OSCIL_RAPID_SECS    = 8      # queda+retorno em X seg = oscilação rápida
CURRENT_VAR_THRESH  = 0.15   # variação de corrente em A para detectar instab.
SAVE_LOG            = True
LOG_FILE            = os.path.expanduser("~/.guardatensao.log")

# ══════════════════════════════════════════════════════════════════════════════
#  PARES DE COR  (índices)
# ══════════════════════════════════════════════════════════════════════════════
# Paleta: âmbar industrial
#  C_AMBER  = texto principal laranja-âmbar
#  C_BRIGHT = âmbar brilhante / destaques
#  C_DIM    = cinza escuro
#  C_GOOD   = verde fosco
#  C_WARN   = amarelo
#  C_CRIT   = vermelho
#  C_OSCIL  = magenta/roxo (oscilação)
#  C_BORDER = âmbar para bordas
#  C_HDR    = fundo âmbar texto preto (cabeçalho)
#  C_INV    = invertido genérico

C_AMBER  = 1
C_BRIGHT = 2
C_DIM    = 3
C_GOOD   = 4
C_WARN   = 5
C_CRIT   = 6
C_OSCIL  = 7
C_BORDER = 8
C_HDR    = 9
C_CYAN   = 10

# ══════════════════════════════════════════════════════════════════════════════
#  ESTADO GLOBAL
# ══════════════════════════════════════════════════════════════════════════════
events          = deque(maxlen=60)
hist_pct        = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
hist_current    = deque([0.0]*HISTORY_SIZE, maxlen=HISTORY_SIZE)
power_events    = deque()          # timestamps de quedas/retornos
alert_flash     = 0
oscil_detected  = False
oscil_score     = 0                # 0-100
beep_enabled    = True
running         = True
last_pct        = None
last_plugged    = None
last_current    = None
last_unplug_ts  = None
stats           = {
    "total_outages": 0,
    "total_oscil":   0,
    "session_start": time.time(),
}

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITÁRIOS
# ══════════════════════════════════════════════════════════════════════════════
def log_event(msg: str, level: str = "INFO"):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}][{level:5s}] {msg}"
    events.appendleft(entry)
    if SAVE_LOG:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{level}] {msg}\n")
        except Exception:
            pass

def notify(title, body, urgency="normal"):
    try:
        subprocess.Popen(["notify-send", "-u", urgency, "-t", "6000", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        pass

def beep(n=1):
    if beep_enabled:
        for _ in range(n):
            sys.stdout.write('\a'); sys.stdout.flush()
            if n > 1: time.sleep(0.15)

def secs_hms(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, _ = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}min"

def uptime_str():
    return secs_hms(time.time() - stats["session_start"])

# ══════════════════════════════════════════════════════════════════════════════
#  LEITURA DE ENERGIA
# ══════════════════════════════════════════════════════════════════════════════
def get_power():
    info = dict(battery_pct=None, plugged=None, time_left=None,
                status="—", voltage=None, current=None, ac_online=None,
                charge_status="unknown")
    bat = psutil.sensors_battery()
    if bat:
        info["battery_pct"] = bat.percent
        info["plugged"]     = bat.power_plugged
        info["time_left"]   = bat.secsleft if bat.secsleft not in (-1,-2) else None
        info["status"]      = "Carregando" if bat.power_plugged else "Descarregando"
    try:
        for sp in glob.glob("/sys/class/power_supply/*"):
            tf = os.path.join(sp, "type")
            if not os.path.exists(tf): continue
            st = open(tf).read().strip()
            if st == "Mains":
                of = os.path.join(sp, "online")
                if os.path.exists(of):
                    info["ac_online"] = open(of).read().strip() == "1"
            elif st == "Battery":
                for k, fn in [("voltage","voltage_now"),("current","current_now")]:
                    fp = os.path.join(sp, fn)
                    if os.path.exists(fp):
                        try: info[k] = int(open(fp).read().strip()) / 1_000_000
                        except: pass
                sf = os.path.join(sp, "status")
                if os.path.exists(sf):
                    raw = open(sf).read().strip()
                    info["charge_status"] = raw
                    m = {"Charging":"Carregando ⚡","Discharging":"Descarregando",
                         "Full":"Completa ✓","Not charging":"Sem carga","Unknown":"—"}
                    info["status"] = m.get(raw, raw)
    except: pass
    return info

# ══════════════════════════════════════════════════════════════════════════════
#  THREAD DE MONITORAMENTO
# ══════════════════════════════════════════════════════════════════════════════
def monitor_loop():
    global last_pct, last_plugged, last_current, last_unplug_ts
    global alert_flash, oscil_detected, oscil_score

    log_event("GuardaTensão v2 iniciado", "START")

    while running:
        info = get_power()
        now  = time.time()
        pct  = info["battery_pct"]
        cur  = info["current"]

        # — histórico —
        hist_pct.append(pct or 0.0)
        hist_current.append(abs(cur) if cur else 0.0)

        # — eventos de plug/unplug —
        if last_plugged is not None and info["plugged"] != last_plugged:
            power_events.append(now)
            # limpa eventos velhos
            while power_events and power_events[0] < now - OSCIL_WINDOW_SECS:
                power_events.popleft()

            if info["plugged"]:
                # RETORNOU
                delta = now - last_unplug_ts if last_unplug_ts else 999
                msg = f"Energia RETORNOU (fora por {delta:.0f}s) — bateria {pct:.0f}%"
                log_event(msg, "POWER")
                notify("⚡ Energia Voltou", f"Bateria: {pct:.0f}%  |  fora por {delta:.0f}s")
                beep(1); alert_flash = 2
                # oscilação rápida?
                if delta <= OSCIL_RAPID_SECS:
                    stats["total_oscil"] += 1
                    log_event(f"OSCILAÇÃO RÁPIDA detectada! (ciclo de {delta:.1f}s)", "OSCIL")
                    notify("⚠ Oscilação Rápida!", f"Energia voltou em {delta:.1f}s — rede instável", "critical")
                    beep(3); alert_flash = 8
            else:
                # QUEDA
                last_unplug_ts = now
                stats["total_outages"] += 1
                msg = f"QUEDA DE ENERGIA — bateria {pct:.0f}%" if pct else "QUEDA DE ENERGIA"
                log_event(msg, "OUTAGE")
                notify("⚠ Queda de Energia!", f"Bateria: {pct:.0f}%  — salve seu trabalho!", "critical")
                beep(2); alert_flash = 6

        # — score de oscilação —
        recent = sum(1 for t in power_events if t > now - OSCIL_WINDOW_SECS)
        oscil_score = min(100, int(recent / OSCIL_EVENT_THRESH * 100))
        if recent >= OSCIL_EVENT_THRESH and not oscil_detected:
            oscil_detected = True
            stats["total_oscil"] += 1
            log_event(f"REDE INSTÁVEL: {recent} eventos em {OSCIL_WINDOW_SECS//60}min", "OSCIL")
            notify("⚡ Rede Elétrica Instável!",
                   f"{recent} oscilações em {OSCIL_WINDOW_SECS//60} minutos!", "critical")
            beep(3); alert_flash = 10
        elif recent < OSCIL_EVENT_THRESH:
            oscil_detected = False

        # — variação de corrente (instabilidade de carga) —
        if cur is not None and last_current is not None and info["plugged"]:
            delta_i = abs(abs(cur) - abs(last_current))
            if delta_i >= CURRENT_VAR_THRESH:
                log_event(f"Corrente instável: Δ{delta_i:.2f}A ({last_current:.2f}→{cur:.2f}A)", "OSCIL")
                if delta_i >= CURRENT_VAR_THRESH * 2:
                    notify("⚡ Carga Instável", f"Variação de corrente: {delta_i:.2f}A", "normal")
        last_current = cur

        # — bateria baixa —
        if pct is not None and not info["plugged"]:
            if pct <= CRITICAL_LOW_PCT and (last_pct is None or last_pct > CRITICAL_LOW_PCT):
                log_event(f"CRÍTICO: bateria {pct:.0f}%!", "CRIT")
                notify("🔴 Bateria Crítica!", f"{pct:.0f}% — conecte imediatamente!", "critical")
                beep(3); alert_flash = 8
            elif pct <= ALERT_LOW_PCT and (last_pct is None or last_pct > ALERT_LOW_PCT):
                log_event(f"Bateria baixa: {pct:.0f}%", "WARN")
                notify("⚠ Bateria Baixa", f"{pct:.0f}% restantes")
                beep(1)
            # queda brusca
            if last_pct is not None and (last_pct - pct) >= ALERT_DROP_PCT:
                log_event(f"Queda brusca: {last_pct:.0f}%→{pct:.0f}%", "WARN")
                beep(1)

        last_pct     = pct
        last_plugged = info["plugged"]
        time.sleep(REFRESH_RATE)

# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS DE DESENHO
# ══════════════════════════════════════════════════════════════════════════════
def W(win): return win.getmaxyx()[1]
def H(win): return win.getmaxyx()[0]

def safe(win, y, x, txt, attr=0):
    try:
        h,w = win.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            win.addstr(y, x, txt[:w-x-1], attr)
    except curses.error: pass

def hline(win, y, x, ch, n, attr=0):
    try:
        h,w = win.getmaxyx()
        if 0 <= y < h:
            n = min(n, w-x-1)
            win.addstr(y, x, ch*n, attr)
    except curses.error: pass

def box(win, y, x, h, w, title="", color=C_BORDER):
    a = curses.color_pair(color) | curses.A_BOLD
    try:
        win.addstr(y,   x,   "╔" + "═"*(w-2) + "╗", a)
        for i in range(1,h-1):
            win.addstr(y+i, x,   "║", a)
            win.addstr(y+i, x+w-1, "║", a)
        win.addstr(y+h-1, x, "╚" + "═"*(w-2) + "╝", a)
        if title:
            t = f"┤ {title} ├"
            win.addstr(y, x+2, t, curses.color_pair(C_HDR) | curses.A_BOLD)
    except curses.error: pass

def hbar(win, y, x, w, pct, color):
    filled = max(0, min(w, int(w * pct / 100)))
    a_on   = curses.color_pair(color) | curses.A_BOLD
    a_off  = curses.color_pair(C_DIM)
    try:
        win.addstr(y, x,        "▰"*filled,    a_on)
        win.addstr(y, x+filled, "▱"*(w-filled), a_off)
    except curses.error: pass

BLOCKS = " ▁▂▃▄▅▆▇█"

def sparkline(win, y, x, w, data, h=5, color_high=C_AMBER, color_low=C_DIM):
    vals = list(data)[-w:]
    while len(vals) < w: vals.insert(0, 0)
    mx = max(vals) if max(vals) > 0 else 100
    try:
        for col, v in enumerate(vals):
            frac = v/mx
            for row in range(h):
                row_threshold = row / h
                next_threshold = (row+1) / h
                if frac >= next_threshold:
                    ch = "█"
                    c  = color_high if row >= h//2 else color_low
                elif frac > row_threshold:
                    sub = int((frac - row_threshold) / (1/h) * 8)
                    ch  = BLOCKS[max(1, sub)]
                    c   = color_high
                else:
                    ch = "·" if row == 0 else " "
                    c  = C_DIM
                attr = curses.color_pair(c)
                win.addstr(y + (h-1-row), x+col, ch, attr)
    except curses.error: pass

def oscil_meter(win, y, x, w, score):
    """Barra de instabilidade — de verde a vermelho."""
    filled = max(0, min(w, int(w * score / 100)))
    for i in range(filled):
        frac = i / w
        if frac < 0.4:   c = C_GOOD
        elif frac < 0.7: c = C_WARN
        else:            c = C_CRIT
        try: win.addstr(y, x+i, "█", curses.color_pair(c) | curses.A_BOLD)
        except curses.error: pass
    try: win.addstr(y, x+filled, "░"*(w-filled), curses.color_pair(C_DIM))
    except curses.error: pass

# ══════════════════════════════════════════════════════════════════════════════
#  LAYOUT PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
LOGO = [
    "  ▄▄  ▄  ▄  ▄▄  ▄▄  ▄▄  ▄▄      ▄▄  ▄▄  ▄▄  ▄▄ ▄  ▄  ▄▄  ▄▄ ",
    " █    █  █ █  █ █   █  █ █  █    █   █   █  █ █ █  █ █  █ █   ",
    " █ ▄▄ █  █ █▄▄█ █▄▄ █  █ █▄▄█    █▄▄ █▄▄ █  █ █  ▀▀  ██▄█ █▄▄ ",
    " █  █ █  █ █  █ █   █  █ █  █    █   █   █  █ █   █  █  █ █   ",
    "  ▀▀   ▀▀  █  █ ▀▀   ▀▀  █  █    ▀▀  ▀▀   ▀▀  ▀   █  █  █ ▀▀  ",
]

def draw(stdscr, info, tick):
    global alert_flash
    SH, SW = stdscr.getmaxyx()
    stdscr.erase()

    # flash de alerta
    if alert_flash > 0:
        if tick % 2 == 0:
            stdscr.bkgd(' ', curses.color_pair(C_CRIT) | curses.A_REVERSE)
        else:
            stdscr.bkgd(' ', 0)
        alert_flash -= 1
    else:
        stdscr.bkgd(' ', 0)

    pct      = info["battery_pct"]
    plugged  = info["plugged"]
    now_str  = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    recent   = sum(1 for t in power_events if t > time.time() - OSCIL_WINDOW_SECS)

    # ── LOGO ──────────────────────────────────────────────────────────────────
    logo_x = max(0, (SW - len(LOGO[0])) // 2)
    for i, line in enumerate(LOGO):
        if i < SH:
            safe(stdscr, i, logo_x, line,
                 curses.color_pair(C_AMBER if tick%4<2 else C_BRIGHT) | curses.A_BOLD)

    row = len(LOGO)

    # ── LINHA DE STATUS RÁPIDO ────────────────────────────────────────────────
    if plugged is None:
        plug_sym = "  ━━  SEM SENSOR  ━━  "
        plug_col = C_DIM
    elif plugged:
        plug_sym = "  ⚡  NA TOMADA  ⚡  "
        plug_col = C_GOOD
    else:
        plug_sym = "  ⚡  SEM ENERGIA  ⚡  "
        plug_col = C_CRIT if (pct or 100) <= ALERT_LOW_PCT else C_WARN

    oscil_sym = ""
    if oscil_score >= 60:
        oscil_sym = "  ◈ REDE INSTÁVEL ◈  "
    elif oscil_score >= 30:
        oscil_sym = "  ◇ OSCILANDO ◇  "

    status_line = plug_sym + oscil_sym + f"   {now_str}   uptime {uptime_str()}"
    sl_x = max(0, (SW - len(status_line)) // 2)
    safe(stdscr, row, sl_x, status_line,
         curses.color_pair(plug_col) | curses.A_REVERSE | curses.A_BOLD)
    row += 1

    hline(stdscr, row, 0, "─", SW, curses.color_pair(C_DIM))
    row += 1

    # ── PAINEL CENTRAL ────────────────────────────────────────────────────────
    PW = min(SW - 2, 78)
    PX = (SW - PW) // 2

    # ┌ Bloco bateria ┐
    bh = 8
    box(stdscr, row, PX, bh, PW, "BATERIA & CARGA")
    ir = row + 1

    if pct is not None:
        pct_col = C_GOOD if pct > 60 else (C_WARN if pct > ALERT_LOW_PCT else C_CRIT)

        # número grande em ASCII
        pct_str = f"{pct:5.1f}%"
        safe(stdscr, ir, PX+2, "CARGA:", curses.color_pair(C_DIM))
        safe(stdscr, ir, PX+9, pct_str,
             curses.color_pair(pct_col) | curses.A_BOLD)

        # barra
        bar_w = PW - 26
        safe(stdscr, ir, PX+17, "▕", curses.color_pair(C_DIM))
        hbar(stdscr, ir, PX+18, bar_w, pct, pct_col)
        safe(stdscr, ir, PX+18+bar_w, "▏", curses.color_pair(C_DIM))

        ir += 1
        # detalhes linha 2
        d = []
        if info["voltage"]:   d.append(f"TENSÃO {info['voltage']:.2f}V")
        if info["current"]:   d.append(f"CORRENTE {abs(info['current']):.2f}A")
        if info["time_left"]: d.append(f"RESTANTE {secs_hms(info['time_left'])}")
        if info["ac_online"] is not None:
            d.append(f"AC {'ONLINE' if info['ac_online'] else 'OFFLINE'}")
        safe(stdscr, ir, PX+2, "  ".join(d) if d else "dados de sensor não disponíveis",
             curses.color_pair(C_CYAN))
        ir += 1

        # status textual
        safe(stdscr, ir, PX+2, f"STATUS  {info['status']}",
             curses.color_pair(pct_col))
    else:
        safe(stdscr, ir, PX+2,
             "Notebook sem bateria detectada — modo AC direto ou sensor ausente",
             curses.color_pair(C_DIM))
        ir += 2

    ir += 1
    # CPU/RAM
    cpu  = psutil.cpu_percent(interval=None)
    mem  = psutil.virtual_memory()
    freq = psutil.cpu_freq()
    fmhz = f"{freq.current:.0f}MHz" if freq else "?"
    sys_str = (f"CPU {cpu:4.1f}%  {fmhz}   "
               f"RAM {mem.percent:4.1f}%  ({mem.used//1024//1024}MB/{mem.total//1024//1024}MB)   "
               f"QUEDAS {stats['total_outages']}  OSCIL {stats['total_oscil']}")
    safe(stdscr, ir, PX+2, sys_str, curses.color_pair(C_DIM))
    row += bh

    # ┌ Bloco oscilação ┐
    oh = 6
    if SH > row + oh + 2:
        box(stdscr, row, PX, oh, PW, "DETECTOR DE OSCILAÇÃO")
        ir = row + 1

        # barra de score
        safe(stdscr, ir, PX+2, "INSTABILIDADE  ", curses.color_pair(C_DIM))
        meter_w = PW - 22
        oscil_meter(stdscr, ir, PX+17, meter_w, oscil_score)
        score_col = C_CRIT if oscil_score >= 60 else (C_WARN if oscil_score >= 30 else C_GOOD)
        safe(stdscr, ir, PX+17+meter_w+1, f"{oscil_score:3d}%",
             curses.color_pair(score_col) | curses.A_BOLD)
        ir += 1

        # indicadores
        ind1_col = C_CRIT if oscil_detected else C_DIM
        ind2_col = C_WARN if (last_unplug_ts and time.time()-last_unplug_ts < OSCIL_RAPID_SECS*3) else C_DIM
        ind3_col = C_WARN if (info["current"] and last_current and
                              abs(abs(info["current"])-abs(last_current)) >= CURRENT_VAR_THRESH*0.5) else C_DIM

        safe(stdscr, ir, PX+2,
             f"[EVENTOS/{OSCIL_WINDOW_SECS//60}min: {recent:2d}]",
             curses.color_pair(ind1_col) | curses.A_BOLD)
        safe(stdscr, ir, PX+24,
             f"[CICLO RÁPIDO: {'SIM ◈' if ind2_col==C_WARN else 'NÃO  '}]",
             curses.color_pair(ind2_col) | curses.A_BOLD)
        safe(stdscr, ir, PX+48,
             f"[CORRENTE: {'INSTÁVEL ◈' if ind3_col==C_WARN else 'ESTÁVEL  '}]",
             curses.color_pair(ind3_col) | curses.A_BOLD)
        ir += 1

        # mensagem de estado
        if oscil_score >= 60:
            msg = "◈◈◈  REDE ELÉTRICA MUITO INSTÁVEL — considere um nobreak (UPS)!  ◈◈◈"
            mc  = C_CRIT
        elif oscil_score >= 30:
            msg = "◇  Oscilações detectadas na rede — salve seus arquivos com frequência  ◇"
            mc  = C_WARN
        else:
            msg = "✓  Rede elétrica estável"
            mc  = C_GOOD
        mx = max(0, (PW - len(msg)) // 2)
        safe(stdscr, ir, PX + mx + 1, msg, curses.color_pair(mc) | curses.A_BOLD)
        row += oh

    # ┌ Bloco gráficos ┐
    gh = 7
    graph_row = row
    half = (PW - 3) // 2
    if SH > row + gh + 2:
        # gráfico bateria
        box(stdscr, row, PX, gh, half, "% BATERIA (60s)")
        sparkline(stdscr, row+1, PX+1, half-2, hist_pct, h=gh-2,
                  color_high=C_AMBER, color_low=C_DIM)
        safe(stdscr, row+gh-1, PX+1, "◂ 60s",     curses.color_pair(C_DIM))
        safe(stdscr, row+gh-1, PX+half-5, "agora▸", curses.color_pair(C_DIM))
        # gráfico corrente
        cx = PX + half + 1
        cw = PW - half - 1
        box(stdscr, row, cx, gh, cw, "CORRENTE A (60s)")
        sparkline(stdscr, row+1, cx+1, cw-2, hist_current, h=gh-2,
                  color_high=C_CYAN, color_low=C_DIM)
        safe(stdscr, row+gh-1, cx+1, "◂ 60s",   curses.color_pair(C_DIM))
        safe(stdscr, row+gh-1, cx+cw-6, "agora▸", curses.color_pair(C_DIM))
        row += gh

    # ┌ Bloco log ┐
    log_h = SH - row - 3
    if log_h >= 3 and SH > row + 3:
        box(stdscr, row, PX, log_h+2, PW, "LOG DE EVENTOS")
        ev_list = list(events)[:log_h]
        for i, ev in enumerate(ev_list):
            if "OUTAGE" in ev or "QUEDA" in ev:    ec = C_CRIT
            elif "OSCIL" in ev or "instável" in ev.lower(): ec = C_WARN
            elif "POWER" in ev or "RETORNOU" in ev: ec = C_GOOD
            elif "CRIT" in ev:                      ec = C_CRIT
            elif "WARN" in ev:                      ec = C_WARN
            elif "START" in ev:                     ec = C_CYAN
            else:                                   ec = C_DIM
            safe(stdscr, row+1+i, PX+2, ev[:PW-4], curses.color_pair(ec))
        row += log_h + 2

    # ── RODAPÉ ────────────────────────────────────────────────────────────────
    hline(stdscr, SH-2, 0, "─", SW, curses.color_pair(C_DIM))
    beep_lbl = f"B:beep={'ON' if beep_enabled else 'OFF'}"
    footer = f"  Q:sair  {beep_lbl}  L:limpar-log  R:reset-stats   log→{LOG_FILE}  "
    safe(stdscr, SH-1, 0, footer, curses.color_pair(C_DIM))

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN CURSES
# ══════════════════════════════════════════════════════════════════════════════
def main(stdscr):
    global alert_flash, beep_enabled, running

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(700)
    curses.start_color()
    curses.use_default_colors()

    # Paleta âmbar industrial
    try:
        curses.init_color(20, 850, 450, 0)    # âmbar escuro
        curses.init_color(21, 1000, 650, 100) # âmbar brilhante
        curses.init_color(22, 250, 250, 250)  # cinza escuro
        AMB  = 20; ABRI = 21; DGRY = 22
    except Exception:
        AMB  = curses.COLOR_YELLOW
        ABRI = curses.COLOR_WHITE
        DGRY = 8

    curses.init_pair(C_AMBER,  AMB,               -1)
    curses.init_pair(C_BRIGHT, ABRI,              -1)
    curses.init_pair(C_DIM,    DGRY,              -1)
    curses.init_pair(C_GOOD,   curses.COLOR_GREEN,  -1)
    curses.init_pair(C_WARN,   curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CRIT,   curses.COLOR_RED,    -1)
    curses.init_pair(C_OSCIL,  curses.COLOR_MAGENTA,-1)
    curses.init_pair(C_BORDER, AMB,               -1)
    curses.init_pair(C_HDR,    curses.COLOR_BLACK, AMB)
    curses.init_pair(C_CYAN,   curses.COLOR_CYAN,  -1)

    tick = 0
    while True:
        k = stdscr.getch()
        if k in (ord('q'), ord('Q'), 27): break
        if k in (ord('b'), ord('B')):
            beep_enabled = not beep_enabled
            log_event(f"Beep {'ativado' if beep_enabled else 'desativado'}")
        if k in (ord('l'), ord('L')):
            events.clear()
            log_event("Log limpo")
        if k in (ord('r'), ord('R')):
            stats["total_outages"] = 0
            stats["total_oscil"]   = 0
            stats["session_start"] = time.time()
            log_event("Estatísticas resetadas", "INFO")

        info = get_power()
        draw(stdscr, info, tick)
        stdscr.refresh()
        tick += 1

    running = False

# ══════════════════════════════════════════════════════════════════════════════
#  ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════
def run():
    global running
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        running = False
        log_event("GuardaTensão encerrado", "STOP")
        print(f"\n✓ Encerrado. Log: {LOG_FILE}")

if __name__ == "__main__":
    try: import psutil
    except ImportError:
        print("Instale psutil:  sudo dnf install python3-psutil"); sys.exit(1)
    run()
