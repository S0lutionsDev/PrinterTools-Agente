"""
PrinterAgent.py — Agente de monitoreo autónomo de impresoras
SolutionsDev · Printer Tools PRO v3

Corre como tarea programada de Windows (o manualmente).
Lee estado via SNMP, detecta condiciones de alerta y envía emails.

SMTP: toma prioridad lo que el cliente configuró en la UI (config.json).
      Si no configuró nada, usa los valores por defecto de SMTP_DEFAULT (este archivo).
El técnico configura los umbrales y schedule desde el tab Agente de la app.

Archivos que usa (todos en ~/.printer_repair/):
  agent_config.json     — umbrales, schedule y SMTP si el cliente los configuró
  alerts_state.json     — estado persistente de alertas (cooldowns, contadores)
  agent_log.txt         — log de actividad del agente
  counters_history.json — historial de contadores para calcular delta mensual
"""

import calendar
import json
import logging
import os
import smtplib
import socket
import struct
import subprocess
import sys
import threading
import time
import tempfile
import webbrowser
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import alerts_history_db
except Exception:
    alerts_history_db = None

try:
    import notifications
except Exception:
    notifications = None



try:
    import usb_monitor
except Exception:
    usb_monitor = None

try:
    import crypto_utils
except Exception:
    crypto_utils = None

# ============================================================================
# SMTP — VALORES POR DEFECTO (fallback final si nadie configuró nada)
# Prioridad de SMTP:
#   1. agent_config.json  (si el técnico configuró SMTP en el tab Agente)
#   2. config.json        (si el cliente configuró SMTP desde la UI de la app)
#   3. SMTP_DEFAULT       (credenciales de SolutionsDev, siempre funcionan)
# ============================================================================
import base64
_DEF_SMTP_PWD = base64.b64decode(b'Q3NYN3ZfNDlGNWJTZQ==').decode('ascii')

SMTP_DEFAULT = {
    'smtp_server':   '23.111.150.66',
    'smtp_port':     465,
    'smtp_user':     'printertools@solutionsdev.com.ar',
    'smtp_password': _DEF_SMTP_PWD,
    'smtp_ssl':      True,
    'email_from':    'printertools@solutionsdev.com.ar',
    'email_to':      '',
    'email_cc':      '',
}

# ============================================================================
# VERSIÓN Y AUTO-ACTUALIZACIÓN
# ============================================================================
AGENT_VERSION = '3.19.2'
AGENT_GITHUB_REPO   = "S0lutionsDev/PrinterTools-Agente"

# ============================================================================
# RUTAS
# ============================================================================
BASE_DIR        = Path.home() / '.printer_repair'
BASE_DIR.mkdir(exist_ok=True)

AGENT_CONFIG    = BASE_DIR / 'agent_config.json'
APP_CONFIG      = BASE_DIR / 'config.json'       # config de la app principal
ALERTS_STATE    = BASE_DIR / 'alerts_state.json'
AGENT_LOG       = BASE_DIR / 'agent_log.txt'
COUNTERS_FILE   = BASE_DIR / 'counters_history.json'
OFFLINE_QUEUE_FILE = BASE_DIR / 'offline_telemetry_queue.json'
OFFLINE_RETENTION_DAYS = 45
MULTI_AGENT_STATE = BASE_DIR / 'multi_agent_state.json'


def load_multi_agent_state() -> dict:
    """Carga el estado de coordinación multi-agente emitido por el Servidor NOC."""
    try:
        if MULTI_AGENT_STATE.exists():
            with open(MULTI_AGENT_STATE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_multi_agent_state(state: dict):
    """Persiste localmente el estado multi-agente (líder LAN asignado, total de agentes)."""
    try:
        with open(MULTI_AGENT_STATE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.debug(f"Aviso guardando multi_agent_state: {e}")


def parse_datetime_flexible(val):
    """Parsea fechas en diversos formatos flexibles (int/float timestamp, ISO, str comunes)."""
    if val is None or val == '':
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(val)
        except Exception:
            return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    if not s or s.lower() in ('none', 'n/d', 'desconocida', 'unknown', '-', '—'):
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        pass
    for fmt in [
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M',
        '%Y-%m-%d',
        '%d/%m/%Y %H:%M:%S',
        '%d/%m/%Y %H:%M',
        '%d/%m/%Y',
        '%Y/%m/%d %H:%M:%S',
        '%Y/%m/%d %H:%M',
        '%Y/%m/%d',
        '%Y-%m'
    ]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def is_offline_expired(val_or_dict, max_days: int = OFFLINE_RETENTION_DAYS) -> bool:
    """
    Determina si un registro de impresora o fecha de sincronización supera el límite de días sin conexión.
    Si supera 'max_days' (45 días por defecto), retorna True para indicar que debe ser purgado.

    POLÍTICA DE RETENCIÓN: Si no se puede determinar la fecha de última conexión,
    retorna False (NO purgar) para preservar el registro. Solo purgar cuando se puede
    confirmar que han pasado más de max_days sin conexión.
    """
    # Sin dato → conservar (no podemos confirmar que superó max_days)
    if val_or_dict is None or val_or_dict == '':
        return False

    dt = None
    if isinstance(val_or_dict, dict):
        # 1. Intentar obtener last_seen
        ls = val_or_dict.get('last_seen')
        dt = parse_datetime_flexible(ls)

        # 2. Fallback a end_date del último mes en el historial de contadores
        if dt is None and val_or_dict.get('months'):
            months = val_or_dict['months']
            if isinstance(months, dict) and months:
                last_m = sorted(months.keys())[-1]
                m_info = months.get(last_m, {})
                if isinstance(m_info, dict):
                    dt = parse_datetime_flexible(m_info.get('end_date'))
                    if dt is None:
                        dt = parse_datetime_flexible(last_m)

        # 3. Fallback a updated_at numérico (timestamp Unix)
        if dt is None and val_or_dict.get('updated_at'):
            dt = parse_datetime_flexible(val_or_dict.get('updated_at'))
    else:
        dt = parse_datetime_flexible(val_or_dict)

    # Fecha no determinable → conservar el registro (política: no purgar ante la duda)
    if dt is None:
        return False

    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)

    delta = datetime.now() - dt
    if delta.total_seconds() < 0:
        return False

    return delta.total_seconds() > (max_days * 86400)



# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [AGENTE] %(levelname)s — %(message)s',
    handlers=[
        logging.FileHandler(AGENT_LOG, encoding='utf-8'),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger('PrinterAgent')
log.setLevel(logging.INFO)
if not any(isinstance(h, logging.FileHandler) and getattr(h, 'baseFilename', '') == str(AGENT_LOG) for h in log.handlers):
    _fh = logging.FileHandler(AGENT_LOG, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s [AGENTE] %(levelname)s — %(message)s'))
    log.addHandler(_fh)


def get_windows_short_path(path_str: str) -> str:
    """Obtiene la ruta 8.3 de Windows para evitar fallos con espacios en schtasks."""
    if sys.platform != 'win32' or not path_str:
        return str(path_str)
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path_str), buf, 1024) > 0:
            return buf.value or str(path_str)
    except Exception:
        pass
    return str(path_str)


# ============================================================================
# CONFIGURACIÓN POR DEFECTO
# ============================================================================
DEFAULT_CONFIG = {
    # Datos de cliente / identificación
    'client_name':            '',

    # Reporte programado mensual
    'monthly_report_enabled': True,
    'monthly_report_day':     1,   # día del mes (1-28)
    'monthly_report_hour':    8,   # hora (0-23)

    # Intervalo de check de alertas (minutos)
    'check_interval_minutes': 15,

    # Umbrales de alertas (todos configurables desde la UI)
    'alert_toner_low_pct':           5,   # % tóner bajo
    'alert_toner_empty_minutes':    60,   # minutos sin tóner antes de alertar
    'alert_jam_count_per_day':       3,   # reincidencias de atasco antes de alertar
    'alert_offline_minutes':        30,   # minutos offline antes de alertar
    'alert_queue_stuck_minutes':    30,   # minutos con mismo trabajo en cola
    'alert_monthly_pages_limit':  5000,   # páginas/mes para alerta de volumen

    # Cooldowns para no spamear (horas)
    'cooldown_toner_low_hours':      24,
    'cooldown_toner_empty_hours':    24,
    'cooldown_jam_hours':            24,
    'cooldown_offline_hours':        24,
    'cooldown_queue_stuck_hours':    8,

    # Modo de Operación del Agente en la Sede:
    # 'full': Red (SNMP) + USB local (Modo por defecto: escanea toda la red)
    # 'usb_only': Solo USB local
    'agent_mode':             'full',
    'network_scan_enabled':   True,

    # Red a escanear ('auto' detecta la subred local de la PC, o acepta '192.168.1.0/24', '192.168.1.1-254', etc.)
    'network_range':          'auto',
    'snmp_community':         'public',
    'snmp_read_community':    'public',
    'snmp_write_community':   'private',
    'snmp_timeout':           0.8,
    'snmp_port':              161,

    # Sincronización Multi-Sede (Panel Central NOC)
    'multisite_enabled':    False,
    'multisite_server_url': 'http://127.0.0.1:8088',
    'multisite_token':      '',
    'multisite_site_name':  '',
    'multisite_client_id':  '',

    # Micro-servicio REST local (diagnóstico LAN directo)
    'local_api_enabled':    False,
    'local_api_port':       9200,
}

# ============================================================================
# RESOLUCIÓN, PARSEO Y CLIENTE SNMP (Importado de snmp_utils)
# ============================================================================
from snmp_utils import (
    get_local_subnet, parse_target_ips,
    OID_PRINTER_STATUS, OID_PAGE_COUNT, OID_KYOCERA_COUNT_1, OID_KYOCERA_COUNT_2,
    OID_SERIAL_1, OID_SERIAL_0, OID_DEVICE_MODEL, OID_KYOCERA_MODEL, OID_SYS_DESCR,
    OID_TONER_MAX_BASE, OID_TONER_CUR_BASE, OID_JAM_COUNT, TONER_SLOTS,
    SNMPClient, scan_network_printers,
    SNMPWriter, ECOPRINT_OIDS, detect_brand
)


# ============================================================================
# ESTADO PERSISTENTE DE ALERTAS
# ============================================================================
class AlertsState:
    """Guarda cooldowns y contadores en disco para persistir entre ejecuciones."""

    def __init__(self, auto_save=True):
        self.data = {}
        self.auto_save = auto_save
        self._dirty = False
        self.load()

    def load(self):
        try:
            if ALERTS_STATE.exists():
                with open(ALERTS_STATE, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
        except Exception as e:
            log.error(f"Error al cargar estado de alertas: {e}")
            self.data = {}

    def save(self):
        try:
            tmp = ALERTS_STATE.with_suffix('.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ALERTS_STATE)
            self._dirty = False
        except Exception as e:
            log.error(f"Error al guardar estado de alertas: {e}")

    def _maybe_save(self):
        self._dirty = True
        if self.auto_save:
            self.save()

    def flush(self):
        """Fuerza la persistencia a disco si hubo cambios pendientes."""
        if self._dirty:
            self.save()

    def _key(self, ip, alert):
        return f"{ip}:{alert}"

    def get_last_sent(self, ip, alert):
        """Retorna datetime de último envío o None."""
        val = self.data.get(self._key(ip, alert), {}).get('last_sent')
        if val:
            try: return datetime.fromisoformat(val)
            except: pass
        return None

    def set_last_sent(self, ip, alert):
        k = self._key(ip, alert)
        if k not in self.data: self.data[k] = {}
        self.data[k]['last_sent'] = datetime.now().isoformat()
        self._maybe_save()

    def can_send(self, ip, alert, cooldown_hours):
        last = self.get_last_sent(ip, alert)
        if last is None: return True
        return datetime.now() - last > timedelta(hours=cooldown_hours)

    # Contadores específicos por tipo
    def get_first_seen(self, ip, condition):
        """Retorna datetime en que se detectó por primera vez una condición continua."""
        val = self.data.get(self._key(ip, condition), {}).get('first_seen')
        if val:
            try: return datetime.fromisoformat(val)
            except: pass
        return None

    def set_first_seen(self, ip, condition, dt=None):
        k = self._key(ip, condition)
        if k not in self.data: self.data[k] = {}
        self.data[k]['first_seen'] = (dt or datetime.now()).isoformat()
        self._maybe_save()

    def clear_first_seen(self, ip, condition):
        k = self._key(ip, condition)
        if k in self.data and 'first_seen' in self.data[k]:
            del self.data[k]['first_seen']
            self._maybe_save()

    def get_jam_count(self, ip, date_str):
        return self.data.get(self._key(ip, f'jams:{date_str}'), {}).get('count', 0)

    def increment_jam_count(self, ip, date_str):
        k = self._key(ip, f'jams:{date_str}')
        if k not in self.data: self.data[k] = {'count': 0}
        self.data[k]['count'] += 1
        self._maybe_save()

    def get_last_page_count(self, ip):
        return self.data.get(self._key(ip, 'page_count'), {}).get('value')

    def set_last_page_count(self, ip, count):
        k = self._key(ip, 'page_count')
        if k not in self.data: self.data[k] = {}
        self.data[k]['value'] = count
        self._maybe_save()

    def get_queue_job(self, ip):
        return self.data.get(self._key(ip, 'queue_job'), {})

    def set_queue_job(self, ip, job_id, since=None):
        k = self._key(ip, 'queue_job')
        self.data[k] = {'job_id': job_id, 'since': (since or datetime.now()).isoformat()}
        self._maybe_save()

    def clear_queue_job(self, ip):
        k = self._key(ip, 'queue_job')
        if k in self.data:
            del self.data[k]
            self._maybe_save()


# ============================================================================
# HISTORIAL DE CONTADORES
# ============================================================================
class CountersHistory:
    def __init__(self, auto_save=True):
        self.data = {}
        self.auto_save = auto_save
        self._dirty = False
        self.load()

    def load(self):
        try:
            if COUNTERS_FILE.exists():
                with open(COUNTERS_FILE, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                self._consolidate_serials()
                self.prune_expired()
                return
        except Exception as e:
            log.warning(f"Error al leer {COUNTERS_FILE}: {e}")

        # Fallback a copia de seguridad si el principal se corrompió
        bak = COUNTERS_FILE.with_suffix('.json.bak')
        if bak.exists():
            try:
                with open(bak, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
                log.info(f"✅ Historial restaurado desde copia de respaldo: {bak}")
                self._consolidate_serials()
                self.prune_expired()
                return
            except Exception as e:
                log.error(f"Error al leer backup {bak}: {e}")
        self.data = {}

    def _consolidate_serials(self):
        """Fusiona entradas que pertenezcan a la misma máquina física (mismo Nº de serie, MAC o Hostname con distinta IP)."""
        key_map = {}
        merged = {}
        changed = False
        for ip, info in list(self.data.items()):
            s = str(info.get('serial', '')).strip()
            mac = str(info.get('mac', '')).strip().lower()
            host = str(info.get('hostname', '')).strip().lower()
            ident = s if (s and s not in ('', 'N/D', '—', '0')) else (mac if mac else (host if host and host not in ('', 'none', 'unknown') else ''))

            if ident:
                if ident in key_map:
                    prev_ip = key_map[ident]
                    prev_info = merged[prev_ip]
                    for m_k, m_v in info.get('months', {}).items():
                        if m_k not in prev_info.setdefault('months', {}):
                            prev_info['months'][m_k] = m_v
                        else:
                            pm = prev_info['months'][m_k]
                            pm['start'] = min(pm.get('start', 0), m_v.get('start', 0))
                            pm['end'] = max(pm.get('end', 0), m_v.get('end', 0))
                            if m_v.get('end_date', '') > pm.get('end_date', ''):
                                pm['end_date'] = m_v.get('end_date')
                                pm['end'] = m_v.get('end')
                    changed = True
                    continue
                else:
                    key_map[ident] = ip
            merged[ip] = info
        if changed:
            self.data = merged
            self._maybe_save()

    def prune_expired(self, max_days: int = OFFLINE_RETENTION_DAYS) -> int:
        """
        Elimina registros de impresoras que superen max_days (45 días) sin conexión.
        Garantiza que al cambiar un equipo por otro en un cliente, el registro del equipo anterior
        se depure automáticamente tras 45 días sin reportes.
        """
        expired_ips = [ip for ip, info in list(self.data.items()) if is_offline_expired(info, max_days=max_days)]
        for ip in expired_ips:
            del self.data[ip]
        if expired_ips:
            self._maybe_save()
            log.info(f"🗑️ Purga de contadores agente: {len(expired_ips)} equipo(s) inactivo(s) > {max_days} días eliminados: {expired_ips}")
        return len(expired_ips)

    def save(self):
        try:
            tmp_path = COUNTERS_FILE.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, COUNTERS_FILE)

            # Generar o actualizar copia de seguridad .bak
            try:
                import shutil
                bak_path = COUNTERS_FILE.with_suffix('.json.bak')
                shutil.copy2(COUNTERS_FILE, bak_path)
            except Exception:
                pass

            self._dirty = False
        except Exception as e:
            log.error(f"Error al guardar contadores: {e}")

    def _maybe_save(self):
        self._dirty = True
        if self.auto_save:
            self.save()

    def flush(self):
        """Fuerza la persistencia a disco si hubo cambios pendientes."""
        if self._dirty:
            self.save()

    def record(self, ip, model, page_count, serial='', hostname='', mac='', toners=None, tech='laser', status='OK', error_detail='', is_online=True):
        now = datetime.now()
        month = now.strftime('%Y-%m')
        now_str = now.strftime('%Y-%m-%d %H:%M')

        clean_serial = str(serial).strip() if serial else ''
        clean_host = str(hostname).strip() if hostname else ''
        clean_mac = str(mac).strip().lower() if mac else ''

        # Si cambió de IP por DHCP pero coincide Serial, MAC o Hostname, migrar registro histórico
        for old_ip, old_info in list(self.data.items()):
            if old_ip == ip:
                continue
            is_usb = str(ip).startswith('USB:') or str(old_ip).startswith('USB:')
            match_s = clean_serial and clean_serial not in ('', 'N/D', '—', '0') and old_info.get('serial') == clean_serial
            match_m = not is_usb and clean_mac and old_info.get('mac') == clean_mac
            match_h = not is_usb and clean_host and clean_host.lower() not in ('', 'none', 'unknown') and old_info.get('hostname', '').lower() == clean_host.lower()
            match_usb_synthetic = (
                is_usb and str(old_ip).startswith('USB:USB-') and
                clean_serial and not clean_serial.startswith('USB-') and
                (
                    (model and old_info.get('model') and model.strip().lower() == str(old_info.get('model')).strip().lower())
                    or (clean_serial.upper() in str(old_ip).upper())
                )
            )

            if match_s or match_m or match_h or match_usb_synthetic:
                rec = self.data.pop(old_ip)
                if ip not in self.data:
                    self.data[ip] = rec
                else:
                    for m_k, m_v in rec.get('months', {}).items():
                        if m_k not in self.data[ip].setdefault('months', {}):
                            self.data[ip]['months'][m_k] = m_v
                log.info(f"🔄 Migración/Deduplicación automática: {old_ip} -> {ip} ({model or clean_host or clean_serial})")
                break

        if ip not in self.data:
            self.data[ip] = {'model': model, 'serial': clean_serial, 'hostname': clean_host, 'mac': clean_mac, 'tech': tech, 'months': {}}
        if model: self.data[ip]['model'] = model
        if clean_serial: self.data[ip]['serial'] = clean_serial
        if clean_host: self.data[ip]['hostname'] = clean_host
        if clean_mac: self.data[ip]['mac'] = clean_mac
        if tech: self.data[ip]['tech'] = tech
        if toners is not None: self.data[ip]['toners'] = toners
        if status: self.data[ip]['status'] = status
        if error_detail: self.data[ip]['error_detail'] = error_detail
        if is_online:
            self.data[ip]['last_seen'] = now_str
            self.data[ip]['is_online'] = True
        if page_count:
            self.data[ip]['last_page_count'] = page_count

        months = self.data[ip].setdefault('months', {})
        if month not in months:
            months[month] = {
                'start': page_count,
                'end': page_count,
                'start_date': now_str,
                'end_date': now_str
            }
        else:
            months[month]['end'] = page_count
            months[month]['end_date'] = now_str
            if 'start_date' not in months[month]:
                months[month]['start_date'] = f"{month}-01 00:00"
        self._maybe_save()

    def get_monthly_delta(self, ip):
        """Retorna páginas impresas en el mes actual."""
        month = datetime.now().strftime('%Y-%m')
        try:
            m = self.data[ip]['months'][month]
            return m['end'] - m['start']
        except: return 0

    def get_all_deltas(self):
        """Retorna dict ip → {model, serial, delta, total} para el mes actual."""
        result = {}
        month = datetime.now().strftime('%Y-%m')
        for ip, info in self.data.items():
            try:
                m = info['months'][month]
                delta = m['end'] - m['start']
                result[ip] = {'model': info.get('model', ''), 'serial': info.get('serial', ''),
                              'delta': delta, 'total': m['end'], 'start': m['start']}
            except: pass
        return result


# ============================================================================
# MAILER
# ============================================================================
def send_email(config, subject, html_body):
    """Envía email HTML usando la config SMTP resuelta."""
    try:
        smtp_server = config.get('smtp_server', '')
        smtp_port   = int(config.get('smtp_port', 465) or 465)
        smtp_user   = config.get('smtp_user', '')
        smtp_pass   = config.get('smtp_password', '')
        use_ssl     = config.get('smtp_ssl', True)

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = config.get('email_from') or smtp_user
        msg['To']      = config.get('email_to', '')
        if config.get('email_cc'):
            msg['Cc'] = config['email_cc']

        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        recipients = [config['email_to']]
        if config.get('email_cc'): recipients.append(config['email_cc'])

        if use_ssl and smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
            if use_ssl or smtp_port == 587:
                server.starttls()

        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.sendmail(msg['From'], recipients, msg.as_string())
        server.quit()
        to_display = ", ".join(recipients)
        log.info(f"✅ Email enviado exitosamente a [{to_display}]: {subject}")
        return True
    except Exception as e:
        log.error(f"❌ Error al enviar email ({subject}): {e}")
        return False


# ============================================================================
# ============================================================================
# TEMPLATES HTML DE EMAILS
# ============================================================================
FONT_STACK = "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif"

STYLE = f"""
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body, table, td, th, p, h1, h2, h3, div, span, b, a {{
    font-family: {FONT_STACK} !important;
  }}
  body {{ background: #f8fafc; padding: 24px 12px; color: #1e293b; margin: 0; font-family: {FONT_STACK}; }}
  .wrap {{ max-width: 720px; margin: 0 auto; background: #ffffff;
          border-radius: 8px; overflow: hidden;
          border: 1px solid #e2e8f0;
          box-shadow: 0 1px 4px rgba(0,0,0,0.06); font-family: {FONT_STACK}; }}
  .header {{ background: #ffffff; border-bottom: 2px solid #f1f5f9; padding: 20px 24px; font-family: {FONT_STACK}; }}
  .header h1 {{ font-size: 18px; font-weight: 700; color: #0f172a; margin-bottom: 4px; font-family: {FONT_STACK}; }}
  .header p  {{ font-size: 13px; color: #64748b; font-family: {FONT_STACK}; }}
  .body {{ padding: 20px 24px; font-family: {FONT_STACK}; }}
  .alert-box {{ border-left: 4px solid #ef4444; background: #fef2f2;
               border-radius: 6px; padding: 12px 16px; margin-bottom: 18px; font-family: {FONT_STACK}; }}
  .alert-box.warn {{ border-color: #f59e0b; background: #fffbeb; }}
  .alert-box.info {{ border-color: #3b82f6; background: #eff6ff; }}
  .alert-box h3 {{ font-size: 15px; color: #0f172a; margin-bottom: 3px; font-weight: 600; font-family: {FONT_STACK}; }}
  .alert-box p  {{ font-size: 13px; color: #475569; font-family: {FONT_STACK}; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; margin-bottom: 16px;
          font-size: 13px; font-family: {FONT_STACK}; }}
  th {{ background: #f8fafc; color: #334155; text-align: left;
       padding: 9px 12px; border-top: 1px solid #e2e8f0; border-bottom: 2px solid #cbd5e1; font-weight: 600; font-family: {FONT_STACK}; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #e2e8f0; color: #334155; font-family: {FONT_STACK}; }}
  tr:nth-child(even) td {{ background: #fbfcfe; }}
  code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-family: Consolas, 'Courier New', monospace; font-size: 12px; color: #0f172a; }}
  .toner-bar {{ height: 8px; border-radius: 4px; background: #e2e8f0;
               overflow: hidden; margin-top: 4px; }}
  .toner-fill {{ height: 100%; border-radius: 4px; }}
  .ok   {{ color: #16a34a; font-weight: 600; }}
  .warn {{ color: #d97706; font-weight: 600; }}
  .err  {{ color: #dc2626; font-weight: 600; }}
  .section-title {{ font-size: 15px; color: #0f172a; font-weight: 600; margin: 16px 0 8px; font-family: {FONT_STACK}; }}
  .footer {{ background: #f8fafc; border-top: 1px solid #e2e8f0; padding: 12px 24px; font-size: 12px;
            color: #94a3b8; text-align: center; font-family: {FONT_STACK}; }}
</style>
"""

def toner_color(pct):
    if pct > 30: return '#16a34a'
    if pct > 10: return '#d97706'
    return '#dc2626'

def toner_row(name, pct, is_discrete=False):
    if is_discrete:
        return (f"<tr style='font-family:{FONT_STACK};'><td>{name}</td><td>"
                f"<div class='toner-bar'><div class='toner-fill' "
                f"style='width:100%;background:#16a34a'></div></div>"
                f"<span style='font-size:12px;color:#16a34a;font-weight:600;font-family:{FONT_STACK};'>OK</span>"
                f"</td></tr>")
    color = toner_color(pct)
    return (f"<tr style='font-family:{FONT_STACK};'><td>{name}</td><td>"
            f"<div class='toner-bar'><div class='toner-fill' "
            f"style='width:{pct}%;background:{color}'></div></div>"
            f"<span style='font-size:12px;color:{color};font-weight:600;font-family:{FONT_STACK};'>{pct}%</span>"
            f"</td></tr>")

def build_alert_email(alert_type, ip, model, details, timestamp=None, serial='—', page_count=0, client_name=None):
    ts = timestamp or datetime.now().strftime('%d/%m/%Y %H:%M')
    client = client_name or os.getenv('COMPUTERNAME', 'Cliente')

    icons = {
        'toner_low':    ('⚠️ Tóner bajo',        'warn', 'Nivel de consumible bajo detectado'),
        'toner_empty':  ('🔴 Sin tóner',          'err',  'El consumible lleva más de 1 hora agotado'),
        'jam':          ('📄 Atascos recurrentes', 'err',  'La impresora presenta atascos recurrentes o persistentes'),
        'offline':      ('📡 Impresora offline',  'err',  'La impresora no responde en la red o USB'),
        'queue_stuck':  ('⏳ Cola atascada',      'warn', 'Un trabajo lleva demasiado tiempo en cola de impresión'),
        'volume':       ('📊 Volumen alto',       'info', 'El volumen mensual superó el límite configurado'),
        'head_open':    ('🚨 Cabezal Abierto',    'err',  'El cabezal térmico está destrabado o abierto'),
        'paper_out':    ('❌ Sin Papel / Rollo',   'err',  'Bandeja de papel o rollo de etiquetas agotado'),
        'ribbon_out':   ('⚠️ Ribbon Agotado',     'warn', 'Cinta de transferencia Ribbon agotada'),
        'door_open':    ('⚠️ Tapa / Puerta Abierta', 'warn', 'La tapa de la impresora se encuentra abierta'),
    }
    title, css_class, desc = icons.get(alert_type, ('Alerta', 'warn', ''))

    extra_rows = "".join(
        f"<tr style='font-family:{FONT_STACK};'><td style='font-weight:600;width:35%;font-family:{FONT_STACK};'>{k}</td><td style='font-family:{FONT_STACK};'>{v}</td></tr>"
        for k, v in details.items()
    )

    pc_count_str = f"{page_count:,} págs" if page_count else "—"

    return f"""<!DOCTYPE html><html><head>{STYLE}</head><body style="margin:0;padding:24px 12px;background-color:#f8fafc;font-family:{FONT_STACK};color:#1e293b;">
<div class='wrap' style="font-family:{FONT_STACK};">
  <div class='header' style="font-family:{FONT_STACK};">
    <h1 style="font-family:{FONT_STACK};">🖨 Printer Tools — Alerta de Impresora</h1>
    <p style="font-family:{FONT_STACK};">SolutionsDev · {ts} · <b>Cliente:</b> {client}</p>
  </div>
  <div class='body' style="font-family:{FONT_STACK};">
    <div class='alert-box {css_class}' style="font-family:{FONT_STACK};">
      <h3 style="font-family:{FONT_STACK};">{title}</h3>
      <p style="font-family:{FONT_STACK};">{desc}</p>
    </div>
    <table style="font-family:{FONT_STACK};">
      <tr><th colspan='2' style="font-family:{FONT_STACK};">Información del Dispositivo</th></tr>
      <tr><td style='font-weight:600;width:35%;font-family:{FONT_STACK};'>Equipo</td><td style="font-family:{FONT_STACK};"><b>{model}</b></td></tr>
      <tr><td style='font-weight:600;font-family:{FONT_STACK};'>Serie</td><td style="font-family:{FONT_STACK};"><b>{serial or '—'}</b></td></tr>
      <tr><td style='font-weight:600;font-family:{FONT_STACK};'>Contador</td><td style="font-family:{FONT_STACK};">{pc_count_str}</td></tr>
      <tr><td style='font-weight:600;font-family:{FONT_STACK};'>IP</td><td><code>{ip}</code></td></tr>
      {extra_rows}
    </table>
  </div>
  <div class='footer' style="font-family:{FONT_STACK};">Printer Tools PRO · SolutionsDev · solutionsdev.com.ar</div>
</div></body></html>"""


def build_monthly_report(printers_data, counters, client_name=None):
    ts  = datetime.now().strftime('%d/%m/%Y %H:%M')
    meses = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
             'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
    now = datetime.now()
    month = f"{meses[now.month - 1]} {now.year}"
    client = client_name or os.getenv('COMPUTERNAME', 'Cliente')

    rows_printers = ""
    for p in printers_data:
        toner_summary = ", ".join(
            f"{n}: OK" if (isinstance(v, dict) and (v.get('is_discrete') or str(v.get('display_text') or '').strip().lower() in ('ok', 'normal', 'presente') or (v.get('cur') == 254 and v.get('max') == 254)))
            else f"{n}: {v.get('pct', v) if isinstance(v, dict) else v}%"
            for n, v in p.get('toners', {}).items()
        ) or "—"
        if not p.get('is_ok', True) and p.get('error_detail'):
            status_str = f"⚠️ {p.get('error_detail')}"
        elif p.get('is_jammed'):
            status_str = "⚠️ Atasco de papel"
        else:
            raw_st = p.get('status', 0)
            if raw_st in (0, 3):
                status_str = '✅ Lista'
            elif raw_st == 4:
                status_str = '🖨️ Imprimiendo'
            elif raw_st == 5:
                status_str = '⏳ Calentando'
            elif raw_st == -1:
                status_str = '📡 Offline'
            else:
                status_str = '✅ Lista'
        cnt_info = counters.get(p['ip'], {})
        delta = cnt_info.get('delta', 0)
        serial = p.get('serial') or cnt_info.get('serial') or '—'
        model = p.get('model', 'Impresora')
        total_pages = p.get('page_count', 0)
        pc_str = f"{total_pages:,} págs" if total_pages else "—"

        rows_printers += (
            f"<tr style='font-family:{FONT_STACK};'>"
            f"<td><b>{model}</b></td>"
            f"<td><b>{serial}</b></td>"
            f"<td>{pc_str}</td>"
            f"<td><code>{p['ip']}</code></td>"
            f"<td><b>+{delta:,}</b></td>"
            f"<td>{status_str}</td>"
            f"<td>{toner_summary}</td>"
            f"</tr>"
        )

    toner_rows = ""
    for p in printers_data:
        m = p.get('model', p['ip'])
        for name, vals in p.get('toners', {}).items():
            is_disc = isinstance(vals, dict) and (vals.get('is_discrete') or str(vals.get('display_text') or '').strip().lower() in ('ok', 'normal', 'presente') or (vals.get('cur') == 254 and vals.get('max') == 254))
            p_val = vals.get('pct', 0) if isinstance(vals, dict) else (vals if isinstance(vals, (int, float)) else 0)
            toner_rows += toner_row(f"{m} ({p['ip']}) — {name}", p_val, is_discrete=is_disc)

    return f"""<!DOCTYPE html><html><head>{STYLE}</head><body style="margin:0;padding:24px 12px;background-color:#f8fafc;font-family:{FONT_STACK};color:#1e293b;">
<div class='wrap' style="font-family:{FONT_STACK};">
  <div class='header' style="font-family:{FONT_STACK};">
    <h1 style="font-family:{FONT_STACK};">🖨 Reporte Mensual de Impresoras — {month}</h1>
    <p style="font-family:{FONT_STACK};">SolutionsDev · {ts} · <b>Cliente:</b> {client}</p>
  </div>
  <div class='body' style="font-family:{FONT_STACK};">
    <div class='section-title' style="font-family:{FONT_STACK};">Resumen de Flota ({len(printers_data)} dispositivos auditados)</div>
    <table style="font-family:{FONT_STACK};">
      <tr>
        <th style="font-family:{FONT_STACK};">Equipo</th>
        <th style="font-family:{FONT_STACK};">Serie</th>
        <th style="font-family:{FONT_STACK};">Contador Total</th>
        <th style="font-family:{FONT_STACK};">IP</th>
        <th style="font-family:{FONT_STACK};">Páginas del Mes</th>
        <th style="font-family:{FONT_STACK};">Estado</th>
        <th style="font-family:{FONT_STACK};">Tóner</th>
      </tr>
      {rows_printers or "<tr><td colspan='7'>Sin impresoras detectadas</td></tr>"}
    </table>

    <div class='section-title' style="font-family:{FONT_STACK};">Niveles de Consumibles</div>
    <table style="font-family:{FONT_STACK};">
      <tr><th style="font-family:{FONT_STACK};">Dispositivo — Cartucho</th><th style="font-family:{FONT_STACK};">Nivel</th></tr>
      {toner_rows or "<tr><td colspan='2'>Sin datos de consumibles</td></tr>"}
    </table>
  </div>
  <div class='footer' style="font-family:{FONT_STACK};">Printer Tools PRO · SolutionsDev · solutionsdev.com.ar</div>
</div></body></html>"""


# ============================================================================
# MOTOR DE ALERTAS
# ============================================================================
class AlertEngine:
    def __init__(self, config, state: AlertsState):
        self.cfg   = config
        self.state = state

    def check_all(self, printers):
        """Evalúa todas las condiciones de alerta para la lista de impresoras."""
        alerts_sent = []
        client = self.cfg.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')

        for p in printers:
            ip    = p['ip']
            model = p.get('model', ip)
            serial = p.get('serial', '—')
            page_count = p.get('page_count', 0)
            toners = p.get('toners', {})
            status = p.get('status', 0)

            # ---- 1. TELEMETRÍA Y TÓNER BAJO (Fase 3.2 & 3.4) ----------------
            if alerts_history_db and toners:
                for slot_name, vals in toners.items():
                    pct = vals.get('pct', 100) if isinstance(vals, dict) else (vals if isinstance(vals, (int, float)) else 100)
                    cur_v = vals.get('cur') if isinstance(vals, dict) else None
                    max_v = vals.get('max') if isinstance(vals, dict) else None
                    try:
                        alerts_history_db.record_toner_level(
                            ip=ip, color=slot_name, percent=pct,
                            model=model, serial=serial, cur_val=cur_v, max_val=max_v
                        )
                    except Exception:
                        pass

            for slot_name, vals in toners.items():
                pct = vals.get('pct', 100) if isinstance(vals, dict) else (vals if isinstance(vals, (int, float)) else 100)
                threshold = self.cfg.get('alert_toner_low_pct', 5)
                if pct < threshold:
                    alert_key = f'toner_low_{slot_name}'
                    cooldown  = self.cfg.get('cooldown_toner_low_hours', 24)
                    if self.state.can_send(ip, alert_key, cooldown):
                        subject = f"⚠️ Tóner {slot_name} bajo — {model} ({ip})"
                        body    = build_alert_email('toner_low', ip, model, {
                            'Cartucho': slot_name,
                            'Nivel actual': f"{pct}%",
                            'Umbral configurado': f"{threshold}%",
                        }, serial=serial, page_count=page_count, client_name=client)
                        if send_email(self.cfg, subject, body):
                            self.state.set_last_sent(ip, alert_key)
                            alerts_sent.append(f"toner_low:{ip}:{slot_name}")
                            if alerts_history_db:
                                try:
                                    alerts_history_db.log_alert(
                                        ip=ip, alert_type='toner_low', model=model, serial=serial,
                                        detail=f"Tóner {slot_name} bajo ({pct}%)", email_sent=True
                                    )
                                except Exception: pass

                        # Notificación nativa de Windows (Toast)
                        if notifications and pct <= 10:
                            try:
                                notifications.notify_low_toner(f"{model} ({ip})", slot_name, pct)
                            except Exception:
                                pass

            # ---- 2. SIN TÓNER > X minutos ---------------------------------
            for slot_name, vals in toners.items():
                pct = vals.get('pct', 100)
                cond_key = f'toner_empty_{slot_name}'
                minutes  = self.cfg.get('alert_toner_empty_minutes', 60)

                if pct == 0:
                    if self.state.get_first_seen(ip, cond_key) is None:
                        self.state.set_first_seen(ip, cond_key)
                    else:
                        first = self.state.get_first_seen(ip, cond_key)
                        elapsed = (datetime.now() - first).total_seconds() / 60
                        if elapsed >= minutes:
                            cooldown = self.cfg.get('cooldown_toner_empty_hours', 24)
                            if self.state.can_send(ip, f'alert_{cond_key}', cooldown):
                                subject = f"🔴 Sin tóner {slot_name} — {model} ({ip})"
                                body    = build_alert_email('toner_empty', ip, model, {
                                    'Cartucho': slot_name,
                                    'Sin tóner desde': first.strftime('%H:%M del %d/%m'),
                                    'Tiempo sin tóner': f"{int(elapsed)} minutos",
                                }, serial=serial, page_count=page_count, client_name=client)
                                if send_email(self.cfg, subject, body):
                                    self.state.set_last_sent(ip, f'alert_{cond_key}')
                                    alerts_sent.append(f"toner_empty:{ip}:{slot_name}")
                                    if alerts_history_db:
                                        try:
                                            alerts_history_db.log_alert(
                                                ip=ip, alert_type='toner_empty', model=model, serial=serial,
                                                detail=f"Sin tóner {slot_name} ({int(elapsed)} min)", email_sent=True
                                            )
                                        except Exception: pass
                else:
                    self.state.clear_first_seen(ip, cond_key)

            # ---- 3. OFFLINE -----------------------------------------------
            offline_key = 'offline'
            offline_min = self.cfg.get('alert_offline_minutes', 30)
            if status == -1:  # sin respuesta SNMP
                if self.state.get_first_seen(ip, offline_key) is None:
                    self.state.set_first_seen(ip, offline_key)
                else:
                    first   = self.state.get_first_seen(ip, offline_key)
                    elapsed = (datetime.now() - first).total_seconds() / 60
                    if elapsed >= offline_min:
                        cooldown = self.cfg.get('cooldown_offline_hours', 24)
                        if self.state.can_send(ip, f'alert_{offline_key}', cooldown):
                            subject = f"📡 Impresora offline — {model} ({ip})"
                            body    = build_alert_email('offline', ip, model, {
                                'Offline desde': first.strftime('%H:%M del %d/%m'),
                                'Minutos sin respuesta': f"{int(elapsed)}",
                            }, serial=serial, page_count=page_count, client_name=client)
                            if send_email(self.cfg, subject, body):
                                self.state.set_last_sent(ip, f'alert_{offline_key}')
                                alerts_sent.append(f"offline:{ip}")
                                if alerts_history_db:
                                    try:
                                        alerts_history_db.log_alert(
                                            ip=ip, alert_type='offline', model=model, serial=serial,
                                            detail=f"Sin respuesta de red ({int(elapsed)} min)", email_sent=True
                                        )
                                    except Exception: pass
            else:
                self.state.clear_first_seen(ip, offline_key)

            # ---- 4. ATASCO DE PAPEL (Persistente o Frecuente) --------------
            is_jammed = p.get('is_jammed', False)
            jam_detail = p.get('error_detail') or 'Atasco de papel'
            today_str = datetime.now().strftime('%Y-%m-%d')
            jam_key = 'jam_active'

            if is_jammed:
                first_seen = self.state.get_first_seen(ip, jam_key)
                if first_seen is None:
                    self.state.set_first_seen(ip, jam_key)
                    self.state.increment_jam_count(ip, today_str)
                    first_seen = datetime.now()
                    # Registro del atasco individual en la base de datos de auditoría
                    if alerts_history_db:
                        try:
                            alerts_history_db.log_alert(
                                ip=ip, alert_type='jam', model=model, serial=serial,
                                detail=f"{jam_detail} (Detectado en escaneo)", email_sent=False
                            )
                        except Exception: pass

                    # Notificación push nativa de Windows (Toast)
                    if notifications:
                        try:
                            notifications.notify_paper_jam(ip, model=model)
                        except Exception: pass

                elapsed_min = (datetime.now() - first_seen).total_seconds() / 60
                jams_today = self.state.get_jam_count(ip, today_str)
                max_jams = self.cfg.get('alert_jam_count_per_day', 3)
                cooldown = self.cfg.get('cooldown_jam_hours', 24)

                should_alert = (jams_today >= max_jams) or (elapsed_min >= 15) or (max_jams <= 1)
                can_send = self.state.can_send(ip, 'jam_alert', cooldown)

                if should_alert and can_send:
                    if jams_today >= max_jams:
                        reason = f"Atascos recurrentes ({jams_today} reincidencias detectadas hoy)"
                    elif elapsed_min >= 15:
                        reason = f"Impresora detenida por atasco persistente ({int(elapsed_min)} min sin despejar)"
                    else:
                        reason = "Atasco de papel activo detectado"

                    subject = f"📄 Atasco recurrente / persistente — {model} ({ip})"
                    body = build_alert_email('jam', ip, model, {
                        'Detalle del equipo': jam_detail,
                        'Causa de alerta': reason,
                        'Reincidencias hoy': f"{jams_today} evento(s)",
                        'Atascada desde': first_seen.strftime('%H:%M del %d/%m'),
                    }, serial=serial, page_count=page_count, client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip, 'jam_alert')
                        alerts_sent.append(f"jam:{ip}:{jams_today}")
                        log.info(f"✅ Alerta de atasco enviada para {ip} ({reason})")
                        if alerts_history_db:
                            try:
                                alerts_history_db.log_alert(
                                    ip=ip, alert_type='jam', model=model, serial=serial,
                                    detail=f"{jam_detail} ({reason})", email_sent=True
                                )
                            except Exception: pass
                else:
                    if not can_send:
                        log.info(f"  ℹ️ {ip}: Atasco activo ({jam_detail}) pero silenciado por cooldown ({cooldown}h)")
                    else:
                        log.info(f"  ℹ️ {ip}: Atasco activo ({jam_detail}) — Reincidencias hoy: {jams_today}/{max_jams}, tiempo trabada: {int(elapsed_min)} min (alerta al llegar a {max_jams} o 15 min)")
            else:
                self.state.clear_first_seen(ip, jam_key)

            # ---- 5. TÉRMICAS Y ETIQUETAS (Zebra / TSPL / ESC-POS) ----------
            if p.get('is_thermal') or p.get('tech') == 'thermal':
                cooldown_th = self.cfg.get('cooldown_thermal_hours', 12)
                if p.get('head_open'):
                    alert_k = 'thermal_head_open'
                    if self.state.can_send(ip, alert_k, cooldown_th):
                        subject = f"🚨 Cabezal Abierto — Impresora Térmica {model} ({ip})"
                        body = build_alert_email('head_open', ip, model, {
                            'Estado': 'Cabezal térmico abierto o destrabado',
                            'Acción': 'Cerrar la palanca/tapa del cabezal térmico firmemente.',
                        }, serial=serial, page_count=page_count, client_name=client)
                        if send_email(self.cfg, subject, body):
                            self.state.set_last_sent(ip, alert_k)
                            alerts_sent.append(f"head_open:{ip}")
                            if alerts_history_db:
                                try:
                                    alerts_history_db.log_alert(ip=ip, alert_type='head_open', model=model, serial=serial, detail='Cabezal térmico abierto', email_sent=True)
                                except Exception: pass

                if p.get('paper_out'):
                    alert_k = 'thermal_paper_out'
                    if self.state.can_send(ip, alert_k, cooldown_th):
                        subject = f"❌ Sin Papel / Fin de Rollo — {model} ({ip})"
                        body = build_alert_email('paper_out', ip, model, {
                            'Estado': 'Rollo de etiquetas o papel agotado',
                            'Acción': 'Reponer rollo de etiquetas en el equipo.',
                        }, serial=serial, page_count=page_count, client_name=client)
                        if send_email(self.cfg, subject, body):
                            self.state.set_last_sent(ip, alert_k)
                            alerts_sent.append(f"paper_out:{ip}")
                            if alerts_history_db:
                                try:
                                    alerts_history_db.log_alert(ip=ip, alert_type='paper_out', model=model, serial=serial, detail='Sin papel / Rollo agotado', email_sent=True)
                                except Exception: pass

                if p.get('ribbon_out'):
                    alert_k = 'thermal_ribbon_out'
                    if self.state.can_send(ip, alert_k, cooldown_th):
                        subject = f"⚠️ Cinta Ribbon Agotada — {model} ({ip})"
                        body = build_alert_email('ribbon_out', ip, model, {
                            'Estado': 'Cinta Ribbon (Transferencia Térmica) agotada o rota',
                            'Acción': 'Reemplazar rollo de Ribbon de transferencia.',
                        }, serial=serial, page_count=page_count, client_name=client)
                        if send_email(self.cfg, subject, body):
                            self.state.set_last_sent(ip, alert_k)
                            alerts_sent.append(f"ribbon_out:{ip}")
                            if alerts_history_db:
                                try:
                                    alerts_history_db.log_alert(ip=ip, alert_type='ribbon_out', model=model, serial=serial, detail='Cinta Ribbon agotada', email_sent=True)
                                except Exception:
                                    pass

            # ---- 6. ADVERTENCIAS DE HARDWARE Y TÓNER NO ORIGINAL -----------
            is_ok = p.get('is_ok', True)
            err_det = str(p.get('error_detail') or '').strip()
            
            # Lista estricta de términos normales / benignos que NUNCA deben emitir alerta de hardware
            benign_screen_terms = (
                'preparad', 'list', 'ready', 'en line', 'online', 'sleep', 'repos',
                'ahorro', 'bajo consumo', 'modo de reposo', 'energy saver', 'powersave',
                'imprim', 'print', 'proces', 'copi', 'standby', 'espera', 'ok',
                'calent', 'warming', 'auto', 'cassette', 'bandeja', 'operativ'
            )
            is_benign = any(b in err_det.lower() for b in benign_screen_terms)

            if not is_ok and err_det and not is_jammed and not is_benign:
                hw_dev_key = serial if (serial and serial not in ('—', 'N/D', '0')) else ip
                hw_k = f'hw_warning_{hw_dev_key}'
                cooldown_hw = int(self.cfg.get('cooldown_hardware_hours', 24))
                if self.state.can_send(hw_dev_key, hw_k, cooldown_hw):
                    is_non_gen = ('no original' in err_det.lower()) or ('non-genuine' in err_det.lower())
                    alert_type = 'toner_non_genuine' if is_non_gen else 'warning'
                    subj = f"⚠️ Tóner no original — {model} ({ip})" if is_non_gen else f"⚠️ Advertencia en pantalla — {model} ({ip}): {err_det}"
                    body = build_alert_email('warning', ip, model, {
                        'Mensaje en pantalla': err_det,
                        'Estado': 'Equipo operativo con advertencia de insumo/hardware',
                    }, serial=serial, page_count=page_count, client_name=client)
                    if send_email(self.cfg, subj, body):
                        self.state.set_last_sent(hw_dev_key, hw_k)
                        self.state.save()
                        alerts_sent.append(f"{alert_type}:{ip}")
                    if alerts_history_db:
                        try:
                            alerts_history_db.log_alert(
                                ip=ip, alert_type=alert_type, model=model, serial=serial,
                                detail=err_det, email_sent=True
                            )
                        except Exception:
                            pass
                elif alerts_history_db:
                    # Registrar en historial de auditoría aunque esté bajo cooldown de correo
                    try:
                        is_non_gen = ('no original' in err_det.lower()) or ('non-genuine' in err_det.lower())
                        alert_type = 'toner_non_genuine' if is_non_gen else 'warning'
                        alerts_history_db.log_alert(
                            ip=ip, alert_type=alert_type, model=model, serial=serial,
                            detail=err_det, email_sent=False
                        )
                    except Exception:
                        pass

            # ---- 7. VOLUMEN MENSUAL ---------------------------------------
            # (Se evalúa desde CountersHistory, no desde SNMP directo)

        return alerts_sent

    def check_usb_alerts(self, usb_printers):
        """Evalúa alertas en impresoras USB locales detectadas vía Spooler / WMI."""
        alerts_sent = []
        client = self.cfg.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')
        cooldown_usb = self.cfg.get('cooldown_usb_hours', 12)

        for up in usb_printers:
            name = up.get('name') or up.get('model', 'USB Printer')
            port = up.get('port', 'USB')
            ip_key = f"USB_{name}"

            if up.get('is_offline'):
                alert_k = 'usb_offline'
                if self.state.can_send(ip_key, alert_k, cooldown_usb):
                    subject = f"⚫ Impresora USB Desconectada — {name} ({port})"
                    body = build_alert_email('offline', port, name, {
                        'Estado': 'Cable USB desenchufado o equipo apagado',
                        'Puerto': port,
                        'Trabajos pendientes': str(up.get('jobs', 0)),
                    }, client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip_key, alert_k)
                        alerts_sent.append(f"usb_offline:{name}")
                        if alerts_history_db:
                            try:
                                alerts_history_db.log_alert(ip=port, alert_type='usb_offline', model=name, detail='Impresora USB desconectada', email_sent=True)
                            except Exception: pass

            if up.get('is_jammed'):
                alert_k = 'usb_jam'
                if self.state.can_send(ip_key, alert_k, cooldown_usb):
                    subject = f"🚨 Atasco de Papel en Impresora USB — {name} ({port})"
                    body = build_alert_email('jam', port, name, {
                        'Estado': 'Atasco de papel reportado por Windows Spooler',
                        'Puerto': port,
                        'Acción': 'Retirar papel atascado de los rodillos del equipo.',
                    }, client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip_key, alert_k)
                        alerts_sent.append(f"usb_jam:{name}")
                        if alerts_history_db:
                            try:
                                alerts_history_db.log_alert(ip=port, alert_type='jam', model=name, detail='Atasco en impresora USB', email_sent=True)
                            except Exception: pass

            if up.get('is_door_open'):
                alert_k = 'usb_door'
                if self.state.can_send(ip_key, alert_k, cooldown_usb):
                    subject = f"⚠️ Puerta / Tapa Abierta en Impresora USB — {name} ({port})"
                    body = build_alert_email('door_open', port, name, {
                        'Estado': 'Tapa o compuerta frontal/superior abierta',
                        'Puerto': port,
                        'Acción': 'Cerrar la tapa del equipo para continuar la impresión.',
                    }, client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip_key, alert_k)
                        alerts_sent.append(f"usb_door:{name}")

            if up.get('is_paper_out'):
                alert_k = 'usb_paper_out'
                if self.state.can_send(ip_key, alert_k, cooldown_usb):
                    subject = f"⚠️ Bandeja sin Papel — Impresora USB {name} ({port})"
                    body = build_alert_email('paper_out', port, name, {
                        'Estado': 'Bandeja de alimentación de papel vacía',
                        'Puerto': port,
                        'Acción': 'Cargar papel en la bandeja correspondiente.',
                    }, client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip_key, alert_k)
                        alerts_sent.append(f"usb_paper_out:{name}")

        return alerts_sent

    def check_queue_alerts(self, queue_jobs):
        """
        Recibe lista de trabajos de cola (obtenida vía PowerShell).
        Detecta trabajos atascados > X minutos.
        """
        alerts_sent = []
        minutes = self.cfg.get('alert_queue_stuck_minutes', 30)
        cooldown = self.cfg.get('cooldown_queue_stuck_hours', 8)
        client = self.cfg.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')

        for job in queue_jobs:
            printer = job.get('PrinterName', '')
            job_id  = str(job.get('Id', ''))
            ip_key  = printer  # usamos nombre de impresora como clave

            saved = self.state.get_queue_job(ip_key)
            if saved.get('job_id') == job_id:
                first = datetime.fromisoformat(saved['since'])
                elapsed = (datetime.now() - first).total_seconds() / 60
                if elapsed >= minutes:
                    alert_key = f'queue_stuck_{job_id}'
                    if self.state.can_send(ip_key, alert_key, cooldown):
                        subject = f"⏳ Trabajo atascado — {printer}"
                        body    = build_alert_email('queue_stuck', ip_key,
                                                    printer, {
                            'Trabajo ID': job_id,
                            'Documento': job.get('DocumentName', '')[:60],
                            'Usuario':   job.get('UserName', ''),
                            'En cola desde': first.strftime('%H:%M del %d/%m'),
                            'Tiempo en cola': f"{int(elapsed)} minutos",
                        }, client_name=client)
                        if send_email(self.cfg, subject, body):
                            self.state.set_last_sent(ip_key, alert_key)
                            alerts_sent.append(f"queue_stuck:{printer}:{job_id}")
                            if alerts_history_db:
                                try:
                                    alerts_history_db.log_alert(
                                        ip=printer, alert_type='queue_stuck', model=printer, serial='',
                                        detail=f"Trabajo #{job_id} ({job.get('DocumentName', '')[:40]}) trabado {int(elapsed)} min",
                                        email_sent=True
                                    )
                                except Exception: pass

                            # Notificación push nativa de Windows (Toast)
                            if notifications:
                                try:
                                    notifications.notify_stuck_queue(printer, 1, int(elapsed))
                                except Exception: pass
            else:
                self.state.set_queue_job(ip_key, job_id)

        return alerts_sent

    def check_volume_alerts(self, counters):
        """Alerta si el volumen mensual supera el umbral."""
        alerts_sent = []
        limit   = self.cfg.get('alert_monthly_pages_limit', 5000)
        cooldown = 720  # 30 días en horas
        client = self.cfg.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')

        for ip, info in counters.items():
            delta = info.get('delta', 0)
            if delta >= limit:
                if self.state.can_send(ip, 'volume_alert', cooldown):
                    subject = f"📊 Volumen alto — {info.get('model', ip)}"
                    body    = build_alert_email('volume', ip, info.get('model', ''), {
                        'Páginas este mes': f"{delta:,}",
                        'Límite configurado': f"{limit:,}",
                        'Total acumulado': f"{info.get('total', 0):,}",
                    }, serial=info.get('serial', '—'), page_count=info.get('total', 0), client_name=client)
                    if send_email(self.cfg, subject, body):
                        self.state.set_last_sent(ip, 'volume_alert')
                        alerts_sent.append(f"volume:{ip}")
                        if alerts_history_db:
                            try:
                                alerts_history_db.log_alert(
                                    ip=ip, alert_type='volume', model=info.get('model', ip), serial=info.get('serial', ''),
                                    detail=f"Volumen mensual: +{delta:,} págs (límite: {limit:,})", email_sent=True
                                )
                            except Exception: pass
        return alerts_sent


# ============================================================================
# OBTENER COLA VÍA POWERSHELL
# ============================================================================
def get_print_queue():
    try:
        cmd_p = ('powershell -NoProfile -Command '
                 '"Get-Printer | Select-Object -ExpandProperty Name | ConvertTo-Json"')
        res_p = subprocess.run(cmd_p, capture_output=True, text=True, shell=True, timeout=10)
        printer_names = []
        if res_p.stdout.strip():
            raw_p = json.loads(res_p.stdout)
            printer_names = raw_p if isinstance(raw_p, list) else [raw_p]

        jobs = []
        for pname in printer_names:
            pe = pname.replace("'", "''")
            script = (f"$j=Get-PrintJob -PrinterName '{pe}' -ErrorAction SilentlyContinue;"
                      f"if($j){{$j|Select-Object Id,DocumentName,UserName,"
                      f"@{{N='PrinterName';E={{'{pe}'}}}},"
                      f"JobStatus,TotalPages,Size|ConvertTo-Json -Depth 2}}")
            cmd_j = f'powershell -NoProfile -Command "{script}"'
            res_j = subprocess.run(cmd_j, capture_output=True, text=True, shell=True, timeout=8)
            if res_j.stdout.strip():
                raw_j = json.loads(res_j.stdout)
                if isinstance(raw_j, dict): raw_j = [raw_j]
                jobs.extend(raw_j)
        return jobs
    except Exception as e:
        log.error(f"Error al obtener cola: {e}")
        return []


# ============================================================================
# SCHEDULER — lógica de "¿toca ejecutar ahora?"
# ============================================================================
def should_run_monthly(config, state):
    """
    Retorna True si toca enviar el reporte mensual.
    Lógica resiliente (Catch-up con respeto de días hábiles):
    - Si el reporte mensual está deshabilitado -> False.
    - Si ya se envió en el mes y año actual -> False.
    - Si hoy es fin de semana (sábado=5 o domingo=6) -> pospone al próximo día hábil (lunes).
    - Si hoy es día hábil (lunes a viernes):
      * Si es exactamente el día programado en el mes: se envía cuando se alcance la hora fijada (now.hour >= target_hour).
      * Si ya pasó el día programado (porque cayó domingo/fin de semana, la máquina estuvo apagada, etc.):
        se despacha en la primera ejecución hábil disponible.
    """
    if not config.get('monthly_report_enabled', True):
        return False

    now = datetime.now()

    # 1. Comprobar si ya fue enviado en el mes y año actual
    last = state.get_last_sent('SYSTEM', 'monthly_report')
    if last:
        if last.year == now.year and last.month == now.month:
            return False  # Ya enviado este mes
        if (now - last).days < 20:
            return False  # Margen de seguridad entre ciclos

    # 2. Respetar días hábiles: No enviar fines de semana (Sábado=5, Domingo=6)
    # Esperará al próximo día hábil (Lunes=0)
    if now.weekday() >= 5:
        return False

    # 3. Día y hora objetivo
    target_day = int(config.get('monthly_report_day', 1))
    target_hour = int(config.get('monthly_report_hour', 8))

    # Ajustar si el mes tiene menos días que target_day (ej. febrero o meses de 30 días)
    _, max_days = calendar.monthrange(now.year, now.month)
    effective_target_day = min(target_day, max_days)

    # Si aún no llegamos al día programado del mes
    if now.day < effective_target_day:
        return False

    # Si hoy es exactamente el día programado, esperar a alcanzar la hora fijada
    if now.day == effective_target_day and now.hour < target_hour:
        return False

    # Cumple todas las condiciones: día hábil, reporte pendiente del mes actual y fecha/hora alcanzada
    return True


# ============================================================================
# RESOLUCIÓN DE CONFIGURACIÓN
# ============================================================================
def load_full_config():
    """Carga la configuración completa del agente resolviendo prioridades de ubicación."""
    config = DEFAULT_CONFIG.copy()

    # Directorios candidatos ordenados de MENOR a MAYOR prioridad:
    # 1. ~/.printer_repair (Directorio de usuario/legacy)
    # 2. CWD (Directorio de trabajo actual)
    # 3. Directorio del ejecutable (sys.executable)
    # 4. C:\PrinterTools-Agente (Directorio oficial de instalación del servicio)
    candidate_dirs = []
    if BASE_DIR not in candidate_dirs:
        candidate_dirs.append(BASE_DIR)

    cwd_dir = Path.cwd()
    if cwd_dir not in candidate_dirs:
        candidate_dirs.append(cwd_dir)

    try:
        exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
        if exe_dir not in candidate_dirs:
            candidate_dirs.append(exe_dir)
    except Exception:
        pass

    try:
        inst_dir = Path(r'C:\PrinterTools-Agente')
        if inst_dir.exists() and inst_dir not in candidate_dirs:
            candidate_dirs.append(inst_dir)
    except Exception:
        pass

    # FASE 1: Cargar todas las configuraciones base 'config.json' (valores de arranque / fallbacks locales)
    for c_dir in candidate_dirs:
        cfg_base = c_dir / 'config.json'
        if cfg_base.exists():
            try:
                with open(cfg_base, 'r', encoding='utf-8-sig') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        config.update(loaded)
            except Exception as e:
                log.error(f"Error al leer {cfg_base}: {e}")

    # FASE 2: Cargar todas las configuraciones dedicadas del agente 'agent_config.json' CON PRIORIDAD ABSOLUTA
    # Garantiza que los parámetros de Sede, Token y Cliente del Agente NUNCA sean pisados por config.json de la app técnica
    for c_dir in candidate_dirs:
        agent_cfg = c_dir / 'agent_config.json'
        if agent_cfg.exists():
            try:
                with open(agent_cfg, 'r', encoding='utf-8-sig') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        config.update(loaded)
                        log.debug(f"Configuración específica de agente cargada con prioridad desde: {agent_cfg}")
            except Exception as e:
                log.error(f"Error al leer {agent_cfg}: {e}")

    # Descifrar credenciales y tokens protegidos con DPAPI
    if crypto_utils:
        try:
            config = crypto_utils.unprotect_config(config)
        except Exception as e_crypto:
            log.warning(f"Error al descifrar valores protegidos de configuración: {e_crypto}")

    # Normalizar sección multisite si viene anidada
    ms = config.get('multisite', {})
    if isinstance(ms, dict) and ms:
        if 'enabled' in ms:
            config['multisite_enabled'] = bool(ms.get('enabled'))
        if 'server_url' in ms:
            config['multisite_server_url'] = ms.get('server_url')
        if 'auth_token' in ms or 'token' in ms:
            config['multisite_token'] = ms.get('token') or ms.get('auth_token')
        if 'client_id' in ms:
            config['multisite_client_id'] = ms.get('client_id')
        if 'site_name' in ms:
            config['multisite_site_name'] = ms.get('site_name')

    # AUTO-MIGRACIÓN TRANSPARENTE: Si el agente tiene guardado el dominio viejo noc.centralprint.com.ar,
    # migrarlo automáticamente a https://printmonitor.com.ar y persistir el cambio en disco
    url_actual = (config.get('multisite_server_url') or '').strip()
    if 'noc.centralprint.com.ar' in url_actual:
        nueva_url = url_actual.replace('noc.centralprint.com.ar', 'printmonitor.com.ar')
        config['multisite_server_url'] = nueva_url
        if isinstance(config.get('multisite'), dict):
            config['multisite']['server_url'] = nueva_url
        try:
            for c_dir in candidate_dirs:
                ac_file = c_dir / 'agent_config.json'
                if ac_file.exists():
                    with open(ac_file, 'r', encoding='utf-8-sig') as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        if 'multisite_server_url' in data:
                            data['multisite_server_url'] = nueva_url
                        if isinstance(data.get('multisite'), dict) and 'server_url' in data['multisite']:
                            data['multisite']['server_url'] = data['multisite']['server_url'].replace('noc.centralprint.com.ar', 'printmonitor.com.ar')
                        with open(ac_file, 'w', encoding='utf-8') as f:
                            json.dump(data, f, indent=2, ensure_ascii=False)
                        log.info(f"Dominio migrado transparentemente a {nueva_url} en {ac_file}")
        except Exception as e_mig:
            log.warning(f"No se pudo reescribir agent_config.json durante migración de dominio: {e_mig}")


    # Resolver credenciales SMTP por defecto institucionales si no se especificaron
    smtp_keys = ['smtp_server', 'smtp_port', 'smtp_user', 'smtp_password',
                 'smtp_ssl', 'email_from', 'email_to', 'email_cc', 'client_name']

    for key in smtp_keys:
        if not config.get(key) and key in SMTP_DEFAULT:
            config[key] = SMTP_DEFAULT.get(key, '')

    return config


def generate_monthly_report_now(preview=True, send_mail=False, on_progress=None):
    """
    Genera el reporte mensual inmediatamente bajo demanda:
    - Escanea las impresoras en red por SNMP
    - Registra/obtiene contadores
    - Construye el HTML estilizado sin fondos oscuros y con Cliente visible
    - Si preview=True, guarda en temp y lo abre en el navegador web predeterminado
    - Si send_mail=True, lo envía a los correos configurados
    Retorna (ok: bool, mensaje: str, path_html: str)
    """
    try:
        if on_progress:
            on_progress("Cargando configuración del agente...")
        config = load_full_config()
        client = config.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')
        log.info(f"Generando reporte mensual bajo demanda para: {client}")

        if on_progress:
            on_progress("Escaneando red en busca de impresoras SNMP...")

        printers = scan_network_printers(
            config.get('network_range', 'auto'),
            config.get('snmp_community', 'public'),
            config.get('snmp_timeout', 0.8),
            config.get('snmp_port', 161),
        )

        if on_progress:
            on_progress(f"Auditadas {len(printers)} impresora(s). Registrando contadores...")

        efficient_io = bool(config.get('efficient_io', True))
        counters = CountersHistory(auto_save=not efficient_io)
        for p in printers:
            counters.record(
                p['ip'],
                p.get('model', ''),
                p.get('page_count', 0),
                p.get('serial', ''),
                p.get('hostname', ''),
                p.get('mac', ''),
                toners=p.get('toners') or p.get('supplies') or p.get('toner_levels'),
                tech=p.get('tech', 'laser'),
                status=p.get('status', 'OK'),
                error_detail=p.get('error_detail', ''),
                is_online=True
            )
        counters.flush()
        all_deltas = counters.get_all_deltas()

        if on_progress:
            on_progress("Generando plantilla HTML del reporte...")

        html = build_monthly_report(printers, all_deltas, client_name=client)
        month = datetime.now().strftime('%B %Y').capitalize()

        html_path = ""
        if preview:
            if on_progress:
                on_progress("Abriendo reporte en el navegador...")
            temp_dir = Path(tempfile.gettempdir())
            temp_file = temp_dir / f"reporte_mensual_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(html)
            html_path = str(temp_file)
            webbrowser.open(temp_file.as_uri())
            log.info(f"Reporte mensual abierto en el navegador: {temp_file}")

        mail_sent = False
        if send_mail:
            to_email = config.get('email_to', '').strip()
            if not to_email:
                return False, "No hay dirección en 'Email destino' configurada en el tab Agente.", html_path

            if on_progress:
                on_progress(f"Conectando a SMTP y enviando reporte a {to_email}...")

            subj = f"📊 Reporte mensual de impresoras — {month} — {client}"
            mail_sent = send_email(config, subj, html)
            if mail_sent:
                state = AlertsState(auto_save=not efficient_io)
                state.set_last_sent('SYSTEM', 'monthly_report')
                state.flush()
                log.info("Reporte mensual enviado correctamente por correo.")
            else:
                return False, "Error al enviar el reporte por correo (verifique SMTP y conexión).", html_path

        msg = "Reporte mensual generado y abierto en el navegador con éxito."
        if send_mail and mail_sent:
            msg = f"Reporte mensual generado y enviado exitosamente a {config.get('email_to')}."
        return True, msg, html_path
    except Exception as e:
        log.error(f"Error al generar reporte mensual: {e}", exc_info=True)
        return False, str(e), ""



# ============================================================================
# PROTOCOLO MULTI-SEDE: EJECUCIÓN REMOTA Y TELEMETRÍA PUSH SALIENTE
# ============================================================================
def _is_printer_ignored(p: dict, ignored_list: list) -> bool:
    """Verifica si una impresora coincide con alguna regla de la lista negra / exclusión."""
    if not ignored_list or not isinstance(ignored_list, list):
        return False
    p_ip = str(p.get('ip') or '').strip().lower()
    p_ser = str(p.get('serial') or '').strip().lower()
    p_port = str(p.get('port') or '').strip().lower()
    p_mac = str(p.get('mac') or '').strip().lower()
    p_host = str(p.get('hostname') or '').strip().lower()
    p_mod = str(p.get('model') or p.get('printer_name') or p.get('name') or '').strip().lower()

    for ign in ignored_list:
        if not isinstance(ign, dict):
            continue
        ign_ip = str(ign.get('ip') or '').strip().lower()
        ign_ser = str(ign.get('serial') or '').strip().lower()
        ign_port = str(ign.get('port') or '').strip().lower()
        ign_mac = str(ign.get('mac') or '').strip().lower()
        ign_host = str(ign.get('hostname') or '').strip().lower()
        ign_mod = str(ign.get('model') or '').strip().lower()

        # Coincidencia por IP
        if ign_ip and ign_ip not in ('', '0.0.0.0', '127.0.0.1', 'none', 'unknown') and ign_ip == p_ip:
            return True
        # Coincidencia por Serial
        if ign_ser and ign_ser not in ('', 'n/d', '—', '0', 'none', 'unknown') and ign_ser == p_ser:
            return True
        # Coincidencia por Puerto (ej: USB004)
        if ign_port and ign_port not in ('', 'none') and ign_port == p_port:
            return True
        # Coincidencia por MAC
        if ign_mac and ign_mac not in ('', 'none') and ign_mac == p_mac:
            return True
        # Coincidencia por Hostname
        if ign_host and ign_host not in ('', 'none', 'unknown') and ign_host == p_host:
            return True
        # Coincidencia por Modelo
        if ign_mod and ign_mod not in ('', 'impresora', 'n/d') and (ign_mod == p_mod or ign_mod in p_mod or p_mod in ign_mod):
            return True
    return False


def _cmd_restart_spooler(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    try:
        subprocess.run(['net', 'stop', 'spooler'], capture_output=True, text=True, errors='replace', creationflags=cflags)
        time.sleep(1.5)
        r = subprocess.run(['net', 'start', 'spooler'], capture_output=True, text=True, errors='replace', creationflags=cflags)
        ok = r.returncode == 0
        res = (r.stdout or r.stderr).strip()
        log.info(f"Comando restart_spooler resultado: {res}")
        return ok, res
    except Exception as e:
        return False, str(e)


def _cmd_clear_queue(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    try:
        subprocess.run(['net', 'stop', 'spooler'], capture_output=True, text=True, errors='replace', creationflags=cflags)
        time.sleep(1)
        spool_dir = Path(os.environ.get('WINDIR', 'C:\\Windows')) / 'System32' / 'spool' / 'PRINTERS'
        deleted = 0
        if spool_dir.exists():
            for f in spool_dir.glob('*'):
                try:
                    f.unlink()
                    deleted += 1
                except Exception:
                    pass
        r = subprocess.run(['net', 'start', 'spooler'], capture_output=True, text=True, errors='replace', creationflags=cflags)
        msg = f"Cola purgada con éxito. {deleted} archivo(s) eliminados del spooler."
        log.info(msg)
        return True, msg
    except Exception as e:
        return False, str(e)


def _cmd_run_agent_scan(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    try:
        if getattr(sys, 'frozen', False):
            cmd_line = f'timeout /t 2 /nobreak >nul & "{sys.executable}" --run'
        else:
            cmd_line = f'timeout /t 2 /nobreak >nul & "{sys.executable}" "{Path(__file__).resolve()}" --run'
        subprocess.Popen(
            ["cmd.exe", "/c", cmd_line],
            creationflags=cflags
        )
        return True, "Orden de escaneo ejecutada. El Agente actualizará la telemetría en instantes."
    except Exception as e:
        return False, f"Error al disparar escaneo: {e}"


def _cmd_print_test_page(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_print_test_page(params, cfg_actual)


def _cmd_ping_host(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    host = params.get('host', '127.0.0.1')
    try:
        r = subprocess.run(['ping', '-n', '2', '-w', '1000', host], capture_output=True, text=True, errors='replace', creationflags=cflags)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _cmd_decommission_printer(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    target_ip = str(params.get('ip', '')).strip()
    target_serial = str(params.get('serial', '')).strip()
    target_port = str(params.get('port', '')).strip()
    target_model = str(params.get('model', '')).strip()
    target_mac = str(params.get('mac', '')).strip().lower()
    target_host = str(params.get('hostname', '')).strip().lower()

    cnt = counters if (counters and hasattr(counters, 'data')) else CountersHistory(auto_save=False)
    to_delete = []
    for h_ip, h_info in list(cnt.data.items()):
        s = str(h_info.get('serial', '')).strip()
        m = str(h_info.get('mac', '')).strip().lower()
        h = str(h_info.get('hostname', '')).strip().lower()
        mod = str(h_info.get('model', '')).strip().lower()

        matches = (
            (target_ip and h_ip == target_ip) or
            (target_serial and target_serial not in ('', 'N/D', '—', '0', 'none') and s == target_serial) or
            (target_mac and m == target_mac) or
            (target_host and target_host not in ('', 'none', 'unknown') and h == target_host) or
            (target_model and target_model.lower() == mod)
        )
        if matches:
            to_delete.append(h_ip)

    for d_ip in to_delete:
        cnt.data.pop(d_ip, None)

    if to_delete:
        cnt.save()

    # Guardar en lista negra local permanente del agente
    try:
        ignored_file = BASE_DIR / 'ignored_printers.json'
        ignored_list = []
        if ignored_file.exists():
            with open(ignored_file, 'r', encoding='utf-8') as f_ign:
                ignored_list = json.load(f_ign)
        already_in = False
        for ex in ignored_list:
            if (target_ip and ex.get('ip') == target_ip) or \
               (target_port and ex.get('port') == target_port) or \
               (target_serial and target_serial not in ('', 'N/D', '—', '0', 'none') and ex.get('serial') == target_serial):
                already_in = True
                break
        if not already_in:
            ignored_list.append({
                'ip': target_ip,
                'serial': target_serial,
                'port': target_port,
                'model': target_model,
                'mac': target_mac,
                'hostname': target_host
            })
            with open(ignored_file, 'w', encoding='utf-8') as f_ign:
                json.dump(ignored_list, f_ign, indent=2, ensure_ascii=False)
    except Exception as e_ign_save:
        log.warning(f"Error guardando impresora ignorada: {e_ign_save}")

    msg = f"Impresora excluida del monitoreo: {target_model or target_ip or target_serial or target_port}"
    log.info(msg)
    return True, msg


def _cmd_restore_printer(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    target_ip = str(params.get('ip', '')).strip()
    target_serial = str(params.get('serial', '')).strip()
    target_port = str(params.get('port', '')).strip()
    target_model = str(params.get('model', '')).strip()
    target_mac = str(params.get('mac', '')).strip().lower()
    target_host = str(params.get('hostname', '')).strip().lower()

    try:
        ignored_file = BASE_DIR / 'ignored_printers.json'
        if ignored_file.exists():
            with open(ignored_file, 'r', encoding='utf-8') as f_ign:
                curr_ign = json.load(f_ign)
            new_ign = [
                x for x in curr_ign
                if not _is_printer_ignored(x, [{
                    'ip': target_ip, 'serial': target_serial, 'port': target_port,
                    'model': target_model, 'mac': target_mac, 'hostname': target_host
                }])
            ]
            with open(ignored_file, 'w', encoding='utf-8') as f_ign:
                json.dump(new_ign, f_ign, indent=2, ensure_ascii=False)
    except Exception as e_ign_res:
        log.warning(f"Error actualizando ignored_printers.json al restaurar: {e_ign_res}")

    msg = f"Impresora restaurada al monitoreo: {target_model or target_ip or target_serial or target_port}"
    log.info(msg)
    return True, msg


def _cmd_ecoprint_set(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_ecoprint_set(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_ecoprint_get(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_ecoprint_get(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_device_reboot(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_device_reboot(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_device_purge_memory(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_device_purge_memory(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_device_print_status(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_device_print_status(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_device_configure_hardware(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    cfg_actual = config if config else load_full_config()
    return _execute_device_configure_hardware(params, cfg_actual, live_printers=live_printers, counters=counters)


def _cmd_update_agent(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    log.info("Orden de actualización forzada del agente recibida desde el panel NOC.")
    ok, msg = check_and_apply_agent_auto_update(force=True)
    log.info(f"Resultado actualización remota del agente: ok={ok}, msg={msg}")
    if ok:
        try:
            if getattr(sys, 'frozen', False):
                cmd_line = f'timeout /t 3 /nobreak >nul & "{sys.executable}" --run'
            else:
                cmd_line = f'timeout /t 3 /nobreak >nul & "{sys.executable}" "{Path(__file__).resolve()}" --run'
            subprocess.Popen(
                ["cmd.exe", "/c", cmd_line],
                creationflags=cflags
            )
        except Exception as e_launch:
            log.debug(f"No se pudo programar reinicio inmediato del agente: {e_launch}")
        return True, f"Agente actualizado con éxito a la última versión disponible ({msg})"
    else:
        return False, f"Fallo en la actualización del agente: {msg}"


def _cmd_reset_monthly_counters(params: dict, config: dict, live_printers: list, counters, cflags: int) -> tuple:
    """
    Calibra y reinicia los contadores mensuales a 0 fijando la lectura actual como nueva línea de base ('start').
    Útil para calibrar sedes tras pruebas iniciales o capturas erróneas.
    """
    if not counters or not getattr(counters, 'data', None):
        return False, "No hay historial de contadores cargado en el agente."

    cur_month = datetime.now().strftime('%Y-%m')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M')
    target_ip = (params or {}).get('ip', '').strip()
    updated_count = 0

    # 1. Calibrar equipos en live_printers
    for lp in (live_printers or []):
        ip = str(lp.get('ip', '')).strip()
        if not ip or (target_ip and ip != target_ip):
            continue
        cnt = lp.get('page_count') or lp.get('total')
        if cnt is not None:
            if ip not in counters.data:
                counters.data[ip] = {
                    'model': lp.get('model', ''),
                    'serial': lp.get('serial', ''),
                    'hostname': lp.get('hostname', ''),
                    'mac': lp.get('mac', ''),
                    'tech': lp.get('tech', 'laser'),
                    'last_page_count': cnt,
                    'months': {}
                }
            months = counters.data[ip].setdefault('months', {})
            months[cur_month] = {
                'start': cnt,
                'end': cnt,
                'start_date': now_str,
                'end_date': now_str
            }
            lp['initial_counter'] = cnt
            lp['monthly_pages'] = 0
            lp['month_start_date'] = now_str
            updated_count += 1

    # 2. Calibrar entradas restantes en counters.data
    for ip, info in counters.data.items():
        if target_ip and ip != target_ip:
            continue
        months = info.setdefault('months', {})
        if cur_month in months and months[cur_month].get('start_date') == now_str:
            continue
        curr_cnt = info.get('last_page_count')
        m_data = months.get(cur_month)
        if m_data and m_data.get('end'):
            curr_cnt = m_data.get('end')
        if curr_cnt is not None:
            months[cur_month] = {
                'start': curr_cnt,
                'end': curr_cnt,
                'start_date': now_str,
                'end_date': now_str
            }
            updated_count += 1

    counters.save()
    counters.flush()
    log.info(f"🎯 Contadores calibrados con éxito para {updated_count} equipo(s). Base mensual fijada a {now_str}.")
    return True, f"Contadores calibrados: {updated_count} equipo(s) con base inicial fijada y delta = 0 a partir de hoy ({now_str})."


REMOTE_COMMAND_HANDLERS = {
    'restart_spooler': _cmd_restart_spooler,
    'clear_queue': _cmd_clear_queue,
    'run_agent_scan': _cmd_run_agent_scan,
    'print_test_page': _cmd_print_test_page,
    'ping_host': _cmd_ping_host,
    'decommission_printer': _cmd_decommission_printer,
    'restore_printer': _cmd_restore_printer,
    'ecoprint_set': _cmd_ecoprint_set,
    'ecoprint_get': _cmd_ecoprint_get,
    'device_reboot': _cmd_device_reboot,
    'device_purge_memory': _cmd_device_purge_memory,
    'device_print_status': _cmd_device_print_status,
    'device_configure_hardware': _cmd_device_configure_hardware,
    'update_agent': _cmd_update_agent,
    'force_update_agent': _cmd_update_agent,
    'reset_monthly_counters': _cmd_reset_monthly_counters,
}


def execute_remote_command(cmd_dict: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Ejecuta un comando recibido del Panel Central del Técnico utilizando una tabla de dispatch.
    Retorna (éxito: bool, salida/error: str).
    """
    action = cmd_dict.get('action', '')
    params = cmd_dict.get('params', {})
    cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0

    log.info(f"Ejecutando comando remoto: '{action}' con parámetros: {params}")

    handler = REMOTE_COMMAND_HANDLERS.get(action)
    if handler:
        return handler(params, config, live_printers, counters, cflags)

    return False, f"Acción remota '{action}' desconocida"


def _execute_print_test_page(params: dict, config: dict = None) -> tuple:
    """
    Envía una página de prueba a una impresora:
    1. Busca en las colas de impresión locales de Windows (por nombre exacto, coincidencia parcial, o IP en PortName).
    2. Si no se encuentra en las colas de Windows o falla, y se cuenta con una IP de red válida,
       intenta el envío directo por socket TCP al puerto RAW 9100 del hardware.
    """
    cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    pname = str(params.get('printer_name') or params.get('model') or '').strip()
    ip = str(params.get('ip') or params.get('host') or '').strip()
    port = str(params.get('port') or params.get('puerto') or '').strip()
    cfg_data = config or {}
    site_name = str(params.get('site_name') or cfg_data.get('client_name') or cfg_data.get('multisite_site_name') or '').strip()

    if pname.lower() in ('general sede', 'sede general', 'general', 'sin especificar', 'n/d'):
        pname = ''

    # Intentar primero por cola de impresión de Windows si hay nombre, IP o puerto USB
    ps_safe_pname = pname.replace("'", "''")
    ps_safe_ip = ip.replace("'", "''")
    ps_safe_port = port.replace("'", "''")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$target = $null
$printers = Get-CimInstance -ClassName Win32_Printer -ErrorAction SilentlyContinue

if ($printers) {{
    if ('{ps_safe_ip}') {{
        $target = $printers | Where-Object {{ $_.PortName -and ($_.PortName -like '*{ps_safe_ip}*') }} | Select-Object -First 1
    }}
    if (-not $target -and '{ps_safe_port}') {{
        $target = $printers | Where-Object {{ $_.PortName -and ($_.PortName -eq '{ps_safe_port}') }} | Select-Object -First 1
    }}
    if (-not $target -and '{ps_safe_pname}') {{
        $target = $printers | Where-Object {{ $_.Name -eq '{ps_safe_pname}' }} | Select-Object -First 1
    }}
    if (-not $target -and '{ps_safe_pname}') {{
        $target = $printers | Where-Object {{ $_.Name -like '*{ps_safe_pname}*' -or $_.ShareName -like '*{ps_safe_pname}*' -or $_.DriverName -like '*{ps_safe_pname}*' }} | Select-Object -First 1
    }}
    # Coincidencia estricta: Jamas hacer fallback por marcas genericas (ej: 'Kyocera' o 'HP')
    # porque causaria que otra impresora de diferente modelo imprima por error.
    if (-not $target -and '{ps_safe_pname}') {{
        $model_nums = [regex]::Matches('{ps_safe_pname}', '\\d{{3,5}}') | ForEach-Object {{ $_.Value }}
        if ($model_nums -and $model_nums.Count -gt 0) {{
            foreach ($mn in $model_nums) {{
                $matched = $printers | Where-Object {{ $_.Name -like "*$mn*" }} | Select-Object -First 1
                if ($matched) {{
                    $target = $matched
                    break
                }}
            }}
        }}
    }}
    # Si la orden era para un equipo de red con IP especifica y no se encontro la cola local,
    # no permitir que caiga en una impresora USB local por error.
    if ($target -and '{ps_safe_ip}' -and $target.PortName -match '^USB\\d+') {{
        $target = $null
    }}
}}


if ($target) {{
    try {{
        $res = Invoke-CimMethod -InputObject $target -MethodName PrintTestPage -ErrorAction Stop
        if ($res.ReturnValue -eq 0) {{
            Write-Output "WIN_OK:$($target.Name)|$($target.PortName)"
        }} else {{
            Write-Output "WIN_ERR:Codigo de retorno $($res.ReturnValue)"
        }}
    }} catch {{
        try {{
            $wtarget = Get-WmiObject -Class Win32_Printer -Filter "Name = '$($target.Name.Replace("'", "''"))'" -ErrorAction Stop
            $ret = $wtarget.PrintTestPage()
            Write-Output "WIN_OK:$($target.Name)|$($target.PortName)"
        }} catch {{
            Write-Output "WIN_ERR:$($_.Exception.Message)"
        }}
    }}
}} else {{
    Write-Output "WIN_NOT_FOUND"
}}
"""
    try:
        cmd = ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps_script]
        r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', creationflags=cflags)
        out = (r.stdout or '').strip()

        if out.startswith("WIN_OK:"):
            parts = out.split(":", 1)[1].split("|")
            q_name = parts[0]
            q_port = parts[1] if len(parts) > 1 else ""
            msg = f"Página de prueba enviada con éxito a través de la cola de Windows '{q_name}' (Puerto: {q_port or 'N/D'})."
            log.info(msg)
            return True, msg
        elif out.startswith("WIN_ERR:"):
            err_detail = out.split(":", 1)[1]
            log.warning(f"Error de Windows PrintTestPage: {err_detail}. Intentando canal alternativo RAW 9100...")
    except Exception as e:
        log.warning(f"Excepción al consultar WMI/CIM de impresoras: {e}")

    # Si no está en las colas de Windows o falló el spooler local, y tenemos IP de red, enviar directo a Port 9100 (RAW)
    if ip and ip not in ('127.0.0.1', 'localhost', '0.0.0.0'):
        try:
            now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            eq_name = pname or 'Impresora de Red'
            site_disp = site_name or 'Sede Remota'

            banner_lines = [
                b"\x1b%-12345X@PJL\r\n",
                b"@PJL JOB NAME = \"PRINTERTOOLS TEST PAGE\"\r\n",
                b"@PJL SET PAPER = A4\r\n",
                b"@PJL ENTER LANGUAGE = PCL\r\n",
                b"\x1bE",
                b"\x1b&l26A",  # Tamaño de papel: A4
                b"\x1b&l7H",   # Selección automática de bandeja (Auto Tray)
                b"\x1b&l0O",   # Orientación: Vertical (Portrait)
                b"\r\n\r\n",
                b"  ========================================================================\r\n",
                b"               PRINTERTOOLS V3.0 - HOJA DE PRUEBA REMOTA (NOC)          \r\n",
                b"  ========================================================================\r\n\r\n",
                f"    * Equipo / Modelo:  {eq_name}\r\n".encode('ascii', errors='replace'),
                f"    * Direccion IP:     {ip}\r\n".encode('ascii', errors='replace'),
                f"    * Sede de Origen:   {site_disp}\r\n".encode('ascii', errors='replace'),
                f"    * Fecha de Envio:   {now_str}\r\n".encode('ascii', errors='replace'),
                b"    * Canal de Envio:   Impresion Directa Socket RAW TCP:9100\r\n\r\n",
                b"  ------------------------------------------------------------------------\r\n",
                b"    [DIAGNOSTICO DE COMUNICACION]:\r\n",
                b"    - Conectividad LAN Agente <-> Impresora:   EXITOSA\r\n",
                b"    - Puerto Hardware Spooler (RAW 9100):      RESPONDIENDO OK\r\n",
                b"    - Sincronizacion y Monitoreo Central NOC:  ACTIVO\r\n",
                b"  ------------------------------------------------------------------------\r\n\r\n",
                b"  ========================================================================\r\n",
                b"\x0c",  # Form Feed
                b"\x1bE",  # PCL Reset
                b"\x1b%-12345X" # PJL Exit
            ]
            raw_data = b"".join(banner_lines)

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.5)
            s.connect((ip, 9100))
            s.sendall(raw_data)
            s.close()

            msg = f"Página de prueba enviada exitosamente vía puerto directo RAW 9100 hacia {ip} ({eq_name})."
            log.info(msg)
            return True, msg
        except Exception as e_sock:
            err_msg = (
                f"No se pudo enviar página de prueba a '{pname or ip}'.\n"
                f"- Cola de Windows local: No encontrada o inaccesible.\n"
                f"- Conexión directa a puerto RAW 9100 ({ip}): {e_sock}."
            )
            log.warning(err_msg)
            return False, err_msg

    return False, f"No se encontró la impresora '{pname or 'desconocida'}' en las colas de Windows locales ni se especificó una dirección IP de red válida."


def _execute_ecoprint_set(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Ajusta la densidad de impresión y modo EcoPrint de las impresoras vía SNMP SET.
    Parámetros:
    - level: int (1 a 5, default 3)
    - targets: list de IPs o ['all']
    - printers: list de dicts con {'ip', 'model', 'brand'} (enviados por el NOC)
    - brand: str (opcional, ej 'KYOCERA')
    - fallback: 'skip' | 'error'
    - snmp_write_community: str (opcional, de lo contrario lee de config protegida DPAPI)
    """
    level = int(params.get('level', 3))
    targets = params.get('targets', ['all'])
    brand_filter = str(params.get('brand', '')).strip().upper()
    fallback = params.get('fallback', 'skip')

    cfg_data = config or {}
    write_community = params.get('snmp_write_community')
    if not write_community:
        write_community = cfg_data.get('snmp_write_community', 'private')
    if crypto_utils and crypto_utils.is_encrypted(write_community):
        try:
            write_community = crypto_utils.decrypt_value(write_community)
        except Exception:
            pass

    read_community = cfg_data.get('snmp_community') or cfg_data.get('snmp_read_community') or 'public'
    port = int(cfg_data.get('snmp_port', 161))

    # 1. Recopilar mapa de impresoras conocidas {ip: model}
    known_map = {}
    if live_printers and isinstance(live_printers, list):
        for p in live_printers:
            if isinstance(p, dict) and p.get('ip'):
                known_map[p['ip']] = p.get('model', '')

    for p in params.get('printers', []):
        if isinstance(p, dict) and p.get('ip'):
            known_map[p['ip']] = p.get('model', '') or known_map.get(p['ip'], '')

    if counters and hasattr(counters, 'data'):
        for c_ip, c_info in counters.data.items():
            if c_ip not in known_map or not known_map[c_ip]:
                known_map[c_ip] = c_info.get('model', '')
    else:
        try:
            ch = CountersHistory(auto_save=False)
            for c_ip, c_info in ch.data.items():
                if c_ip not in known_map or not known_map[c_ip]:
                    known_map[c_ip] = c_info.get('model', '')
        except Exception:
            pass

    # 2. Armar lista de candidatas
    candidate_printers = []
    if targets != ['all'] and isinstance(targets, list):
        for tip in targets:
            candidate_printers.append({'ip': tip, 'model': known_map.get(tip, '')})
    elif known_map:
        for k_ip, k_model in known_map.items():
            candidate_printers.append({'ip': k_ip, 'model': k_model})
    else:
        try:
            scanned = scan_network_printers(
                network_range=cfg_data.get('network_range', 'auto'),
                community=read_community,
                timeout=float(cfg_data.get('snmp_timeout', 0.8))
            )
            candidate_printers = scanned or []
        except Exception as e:
            log.warning(f"Error escaneando red para ecoprint_set: {e}")

    if not candidate_printers:
        return False, "No se detectaron impresoras en la red local para configurar EcoPrint"

    results = []
    for p in candidate_printers:
        ip = p.get('ip')
        if not ip:
            continue
        model = p.get('model', '')
        cl = SNMPClient(ip, community=read_community, port=port, timeout=1.8)
        if not model:
            try:
                model = cl.get_model() or cl.get(OID_KYOCERA_MODEL) or cl.get(OID_DEVICE_MODEL) or 'Impresora'
            except Exception:
                model = 'Impresora'

        brand = detect_brand(model)

        # Si la marca es GENERIC, comprobar si responde al árbol Enterprise de Kyocera
        if brand == 'GENERIC':
            try:
                t_probe = cl.get_int('1.3.6.1.4.1.1347.43.5.2.1.1.1.1')
                if t_probe is not None:
                    brand = 'KYOCERA'
                    if not model or model == 'Impresora':
                        model = 'Kyocera ECOSYS'
            except Exception:
                pass

        if targets != ['all'] and ip not in targets:
            continue

        if brand_filter and brand != brand_filter and brand != 'GENERIC':
            continue

        if brand not in ECOPRINT_OIDS:
            if fallback == 'skip':
                results.append({
                    'ip': ip, 'model': model, 'brand': brand,
                    'status': 'skipped',
                    'reason': f"{brand} no soporta EcoPrint vía SNMP"
                })
            else:
                results.append({
                    'ip': ip, 'model': model, 'brand': brand,
                    'status': 'error',
                    'reason': f"Marca {brand} no homologada para EcoPrint SNMP"
                })
            continue

        b_cfg = ECOPRINT_OIDS[brand]
        target_oid = b_cfg.get('density_oid')
        if not target_oid:
            results.append({
                'ip': ip, 'model': model, 'brand': brand,
                'status': 'skipped',
                'reason': f"{brand} no soporta ajuste de densidad vía SNMP SET"
            })
            continue

        val_to_set = level
        if not b_cfg.get('supports_levels'):
            val_to_set = 1 if level <= 3 else 2

        # Intentar escribir con community configurada, con fallback cruzado ('private' <-> 'public')
        comm_attempts = [write_community]
        if read_community and read_community not in comm_attempts:
            comm_attempts.append(read_community)
        for alt in ('private', 'public'):
            if alt not in comm_attempts:
                comm_attempts.append(alt)

        success = False
        working_comm = None
        for comm in comm_attempts:
            writer = SNMPWriter(ip, community=comm, port=port, timeout=2.5)
            if writer.set_int(target_oid, val_to_set):
                success = True
                working_comm = comm
                break

        # Si se logró escribir la densidad y el equipo tiene OID de EcoPrint ON/OFF, aplicarlo también
        # Opción B: EcoPrint ON solo en niveles 1 y 2 (ahorro). En nivel 3 (estándar normal) y superiores queda EcoPrint OFF
        if success and b_cfg.get('ecoprint_oid'):
            eco_val = 1 if val_to_set <= 2 else 2
            try:
                w_eco = SNMPWriter(ip, community=working_comm or write_community, port=port, timeout=2.0)
                w_eco.set_int(b_cfg['ecoprint_oid'], eco_val)
            except Exception:
                pass

        # Pausa para que el firmware del equipo procese el cambio
        time.sleep(0.4)

        verified = None
        try:
            reader = SNMPClient(ip, community=read_community, port=port, timeout=2.0)
            verified = reader.get_int(target_oid)
            if verified is not None:
                if verified == val_to_set:
                    success = True
                elif not success:
                    success = False
        except Exception:
            pass

        savings = b_cfg.get('estimated_savings', {}).get(val_to_set, 15)
        lvl_desc = b_cfg.get('level_names', {}).get(val_to_set, f"Nivel {val_to_set}")

        if success:
            msg = f"Nivel {val_to_set} aplicado con éxito ({savings}% ahorro est.)"
            if verified is not None:
                msg += f" [Verificado SNMP: {verified}]"
        else:
            msg = f"Error SNMP SET en {ip}. Verifique que la community ('{write_community}') tenga permisos de escritura en la impresora."

        results.append({
            'ip': ip,
            'model': model,
            'brand': brand,
            'status': 'ok' if success else 'error',
            'level': val_to_set,
            'level_desc': lvl_desc,
            'verified': verified,
            'savings_pct': savings,
            'message': msg
        })
        log.info(f"EcoPrint SET {ip} ({model}): nivel {val_to_set} → {'✅' if success else '❌'}")

    ok_count = sum(1 for r in results if r.get('status') == 'ok')
    total = len(results)
    summary = f"{ok_count}/{total} impresoras configuradas"
    summary_lines = [f"{summary}:"]
    for r in results:
        status_icon = "✅" if r.get('status') == 'ok' else ("⏭️" if r.get('status') == 'skipped' else "❌")
        summary_lines.append(f"{status_icon} {r['ip']} ({r['model']}): {r.get('message', r.get('reason', ''))}")
    summary_text = "\n".join(summary_lines)

    return True, {
        'summary': summary,
        'text': summary_text,
        'results': results
    }


def _execute_ecoprint_get(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """Lee el nivel actual de EcoPrint y densidad de las impresoras de la red."""
    targets = params.get('targets', ['all'])
    cfg_data = config or {}
    read_community = cfg_data.get('snmp_community') or cfg_data.get('snmp_read_community') or 'public'
    port = int(cfg_data.get('snmp_port', 161))

    # 1. Recopilar mapa de impresoras conocidas {ip: model}
    known_map = {}
    if live_printers and isinstance(live_printers, list):
        for p in live_printers:
            if isinstance(p, dict) and p.get('ip'):
                known_map[p['ip']] = p.get('model', '')

    for p in params.get('printers', []):
        if isinstance(p, dict) and p.get('ip'):
            known_map[p['ip']] = p.get('model', '') or known_map.get(p['ip'], '')

    if counters and hasattr(counters, 'data'):
        for c_ip, c_info in counters.data.items():
            if c_ip not in known_map or not known_map[c_ip]:
                known_map[c_ip] = c_info.get('model', '')
    else:
        try:
            ch = CountersHistory(auto_save=False)
            for c_ip, c_info in ch.data.items():
                if c_ip not in known_map or not known_map[c_ip]:
                    known_map[c_ip] = c_info.get('model', '')
        except Exception:
            pass

    candidate_printers = []
    if targets != ['all'] and isinstance(targets, list):
        for tip in targets:
            candidate_printers.append({'ip': tip, 'model': known_map.get(tip, '')})
    elif known_map:
        for k_ip, k_model in known_map.items():
            candidate_printers.append({'ip': k_ip, 'model': k_model})
    else:
        try:
            scanned = scan_network_printers(
                network_range=cfg_data.get('network_range', 'auto'),
                community=read_community,
                timeout=float(cfg_data.get('snmp_timeout', 0.8))
            )
            candidate_printers = scanned or []
        except Exception as e:
            log.warning(f"Error escaneando red para ecoprint_get: {e}")

    results = []
    for p in candidate_printers:
        ip = p.get('ip')
        if not ip:
            continue
        model = p.get('model', '')
        cl = SNMPClient(ip, community=read_community, port=port, timeout=1.8)
        if not model:
            try:
                model = cl.get_model() or cl.get(OID_KYOCERA_MODEL) or cl.get(OID_DEVICE_MODEL) or 'Impresora'
            except Exception:
                model = 'Impresora'

        brand = detect_brand(model)
        # Sonda Kyocera si brand es GENERIC
        if brand == 'GENERIC':
            try:
                t_val = cl.get_int('1.3.6.1.4.1.1347.43.5.2.1.1.1.1')
                if t_val is not None:
                    brand = 'KYOCERA'
                    if not model or model == 'Impresora':
                        model = 'Kyocera ECOSYS'
            except Exception:
                pass

        b_cfg = ECOPRINT_OIDS.get(brand)
        if not b_cfg:
            continue

        density_oid = b_cfg.get('density_oid')
        ecoprint_oid = b_cfg.get('ecoprint_oid')

        cur_density = cl.get_int(density_oid) if density_oid else None
        cur_eco = cl.get_int(ecoprint_oid) if ecoprint_oid else None

        eco_desc = 'Desconocido'
        if cur_eco == 1:
            eco_desc = 'Activado (Ahorro)'
        elif cur_eco == 2:
            eco_desc = 'Desactivado (Estándar)'

        results.append({
            'ip': ip,
            'model': model,
            'brand': brand,
            'current_level': cur_density,
            'density': cur_density,
            'ecoprint_mode': eco_desc,
            'level_desc': b_cfg.get('level_names', {}).get(cur_density, f"Nivel {cur_density}") if cur_density else 'No consultado / Error SNMP',
            'status': 'ok' if (cur_density is not None or cur_eco is not None) else 'unreachable'
        })

    summary_lines = []
    for r in results:
        eco_str = r.get('ecoprint_mode', '')
        summary_lines.append(f"• {r['ip']} ({r['model']}): {r.get('level_desc', 'N/D')}" + (f" | EcoPrint: {eco_str}" if eco_str != 'Desconocido' else ""))
    summary_text = "\n".join(summary_lines) if summary_lines else "No se detectaron impresoras consultables en la sede"

    return True, {
        'summary': f"{len(results)} impresora(s) consultadas",
        'text': summary_text,
        'results': results
    }


def _send_raw_socket_cmd(ip: str, data: bytes, timeout: float = 4.0, retries: int = 2) -> bool:
    """Envía una secuencia de bytes directa a un socket TCP RAW (puerto 9100) con reintentos limpios."""
    import struct
    for attempt in range(max(1, retries)):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 3))
            except Exception:
                pass
            s.connect((ip, 9100))
            s.sendall(data)
            try:
                s.shutdown(socket.SHUT_WR)
            except Exception:
                pass
            time.sleep(0.15)
            return True
        except Exception as e:
            log.warning(f"Intento {attempt+1}/{retries} fallo al enviar comando RAW TCP:9100 a {ip}: {e}")
            if attempt < retries - 1:
                time.sleep(0.8)
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
    return False



def _reboot_via_web_admin(ip: str, model: str = '', brand: str = '', serial: str = '',
                          web_user: str = '', web_pass: str = '', timeout: float = 3.5) -> tuple:
    """
    Intenta un reinicio físico/sistema por HTTP/HTTPS autenticando en el servidor web del equipo:
    - Kyocera (Command Center RX): Autenticación con Admin/Admin (o serial) y llamada a /dvcset/rstset/set.cgi o /start.cgi.
    - HP (Embedded Web Server): DeviceReset / reboot endpoints con Basic Auth admin/admin.
    - Brother / Ricoh / Genérico: Endpoints web de reinicio con autenticación estándar.
    """
    import urllib.request
    import urllib.parse
    import urllib.error
    import http.cookiejar
    import http.client
    import ssl
    import base64

    if not ip:
        return False, "IP no especificada"

    # Verificación ultra-rápida de conectividad previa (evita demoras si los puertos web están cerrados)
    open_schemes = []
    for port, scheme in [(80, 'http'), (443, 'https')]:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.6)
                if sock.connect_ex((ip, port)) == 0:
                    open_schemes.append((scheme, port))
        except Exception:
            pass

    if not open_schemes:
        return False, "Puertos web (80/443) cerrados o inaccesibles"

    brand = (brand or detect_brand(model)).upper()

    # Armado de lista de credenciales ordenadas por prioridad
    cred_list = []
    if web_user and web_pass:
        cred_list.append((web_user, web_pass))

    clean_serial = str(serial or '').strip()
    if brand == 'KYOCERA':
        cred_list.append(('Admin', 'Admin'))
        if clean_serial:
            cred_list.append(('Admin', clean_serial))
            cred_list.append(('Admin', clean_serial.upper()))
        cred_list.extend([('admin', 'admin'), ('admin00', 'admin00')])
    elif brand == 'HP':
        cred_list.extend([('admin', ''), ('admin', 'admin'), ('Admin', 'Admin')])
        if clean_serial:
            cred_list.append(('admin', clean_serial))
    elif brand == 'BROTHER':
        cred_list.extend([('admin', 'access'), ('admin', 'admin')])
        if clean_serial:
            cred_list.append(('admin', clean_serial))
    else:
        cred_list.extend([('admin', 'admin'), ('Admin', 'Admin'), ('admin', '')])

    seen = set()
    clean_creds = []
    for u, p in cred_list:
        pair = (u, p)
        if pair not in seen:
            seen.add(pair)
            clean_creds.append(pair)

    # SSL context sin verificación de certificado (para certs autofirmados de impresoras)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    for scheme, port in open_schemes:
        port_suffix = f":{port}" if (scheme == 'http' and port != 80) or (scheme == 'https' and port != 443) else ""
        base_url = f"{scheme}://{ip}{port_suffix}"

        for u, p in clean_creds:
            try:
                cj = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(cj),
                    urllib.request.HTTPSHandler(context=ctx)
                )

                if brand == 'KYOCERA':
                    # 1. Login Command Center RX
                    login_urls = [
                        f"{base_url}/start.cgi",
                        f"{base_url}/dvcset/sysset/set.cgi",
                        f"{base_url}/login/login.htm"
                    ]
                    login_payloads = [
                        urllib.parse.urlencode({'arg01': u, 'arg02': p, 'wlm_login': '1'}).encode('utf-8'),
                        urllib.parse.urlencode({'user': u, 'password': p}).encode('utf-8')
                    ]
                    logged_in = False
                    for l_url in login_urls:
                        for l_data in login_payloads:
                            try:
                                req = urllib.request.Request(l_url, data=l_data, headers={
                                    'User-Agent': 'Mozilla/5.0 (PrinterTools-Pro)',
                                    'Referer': f"{base_url}/"
                                })
                                with opener.open(req, timeout=timeout) as resp:
                                    if resp.status in (200, 302):
                                        logged_in = True
                                        break
                            except Exception:
                                pass
                        if logged_in:
                            break

                    # 2. Trigger de Reinicio en Command Center RX
                    reset_targets = [
                        (f"{base_url}/dvcset/rstset/set.cgi", urllib.parse.urlencode({'arg01': '1', 'wlm_reset': '1'}).encode('utf-8')),
                        (f"{base_url}/start.cgi", urllib.parse.urlencode({'arg01': 'reset', 'arg02': 'reboot'}).encode('utf-8')),
                        (f"{base_url}/management/restart_device.htm", urllib.parse.urlencode({'action': 'reboot'}).encode('utf-8')),
                    ]
                    for r_url, r_data in reset_targets:
                        try:
                            req = urllib.request.Request(r_url, data=r_data, headers={
                                'User-Agent': 'Mozilla/5.0 (PrinterTools-Pro)',
                                'Referer': f"{base_url}/"
                            })
                            with opener.open(req, timeout=timeout) as resp:
                                if resp.status in (200, 302):
                                    return True, f"Command Center RX ({u}@{scheme.upper()})"
                        except (http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
                            # Al comenzar a reiniciar, la placa de red corta la conexión HTTP inmediatamente
                            return True, f"Command Center RX ({u}@{scheme.upper()})"
                        except Exception:
                            pass

                elif brand == 'HP':
                    # HP EWS Reset
                    auth_header = base64.b64encode(f"{u}:{p}".encode('utf-8')).decode('ascii')
                    hp_targets = [
                        (f"{base_url}/hp/device/DeviceReset/Reboot", b"reboot=true"),
                        (f"{base_url}/DevMgmt/ProductConfigDyn.xml", b"<ProductConfig><Reboot>true</Reboot></ProductConfig>")
                    ]
                    for h_url, h_data in hp_targets:
                        try:
                            req = urllib.request.Request(h_url, data=h_data, headers={
                                'Authorization': f"Basic {auth_header}",
                                'User-Agent': 'Mozilla/5.0 (PrinterTools-Pro)'
                            })
                            with opener.open(req, timeout=timeout) as resp:
                                if resp.status in (200, 302):
                                    return True, f"HP EWS ({u}@{scheme.upper()})"
                        except (http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
                            return True, f"HP EWS ({u}@{scheme.upper()})"
                        except Exception:
                            pass

                elif brand == 'BROTHER':
                    auth_header = base64.b64encode(f"{u}:{p}".encode('utf-8')).decode('ascii')
                    b_targets = [
                        (f"{base_url}/admin/reboot.html", b"reboot=yes"),
                        (f"{base_url}/etc/mnt_reboot.html", b"reboot=1")
                    ]
                    for b_url, b_data in b_targets:
                        try:
                            req = urllib.request.Request(b_url, data=b_data, headers={
                                'Authorization': f"Basic {auth_header}",
                                'User-Agent': 'Mozilla/5.0 (PrinterTools-Pro)'
                            })
                            with opener.open(req, timeout=timeout) as resp:
                                if resp.status in (200, 302):
                                    return True, f"Brother Web ({u}@{scheme.upper()})"
                        except (http.client.RemoteDisconnected, ConnectionResetError, urllib.error.URLError):
                            return True, f"Brother Web ({u}@{scheme.upper()})"
                        except Exception:
                            pass
            except Exception:
                continue

    return False, "No se pudo autenticar o reiniciar por Web Admin"


def _execute_device_reboot(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Envía una orden de reinicio al hardware de la impresora según el protocolo de cada fabricante.

    ⚠️ SEGURIDAD CRÍTICA: En modo 'hybrid' (por defecto), se detiene al PRIMER método exitoso
    para evitar el "triple golpe" (Web + Socket + SNMP simultáneos) que puede colapsar
    la controladora y dejar el equipo en estado de error requiriendo apagado físico.

    Protocolos soportados (en orden de preferencia):
    - WEB ADMIN (HTTP/HTTPS): Login Admin/Admin (o serial) contra Command Center RX (Kyocera), EWS (HP), Brother.
    - KYOCERA: Comando PRESCRIBE '!R! RES; EXIT;' por socket RAW 9100.
    - HP/BROTHER: Comando PJL '@PJL RESET / @PJL INITIALIZE' por socket RAW 9100.
    - SNMP SET prtGeneralReset(5) = resetPrinter (reinicio seguro, sin tocar NVRAM).

    NOTA: Se usa resetPrinter(5) en lugar de resetToNVRAM(3) para evitar resets a fábrica
    que pueden brickear la controladora si se interrumpen durante la escritura en flash.
    """
    ip = str(params.get('ip') or params.get('host') or '').strip()
    model = str(params.get('model') or params.get('printer_name') or '').strip()
    serial = str(params.get('serial') or '').strip()
    web_user = str(params.get('web_user') or '').strip()
    web_pass = str(params.get('web_pass') or '').strip()
    if not ip:
        return False, "No se especificó la dirección IP del equipo para reiniciar"

    brand = detect_brand(model)
    cfg_data = config or {}
    write_community = params.get('snmp_write_community') or cfg_data.get('snmp_write_community', 'private')
    if crypto_utils and crypto_utils.is_encrypted(write_community):
        try:
            write_community = crypto_utils.decrypt_value(write_community)
        except Exception:
            pass
    read_community = cfg_data.get('snmp_community') or cfg_data.get('snmp_read_community') or 'public'
    port = int(cfg_data.get('snmp_port', 161))

    # OID estándar RFC 3805 prtGeneralReset
    # Valor 5 = resetPrinter (reinicio seguro de la controladora, preserva NVRAM)
    # Valor 3 = resetToNVRAM (reset a valores de fábrica — PELIGROSO, puede brickear)
    OID_RESET = '1.3.6.1.2.1.43.5.1.1.3.1'

    mode = str(params.get('mode') or 'hybrid').lower().strip()

    # ── Pre-verificación: verificar que la impresora responde antes de enviar reinicio ──
    try:
        pre_check = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        pre_check.settimeout(1.5)
        reachable = pre_check.connect_ex((ip, 9100)) == 0
        pre_check.close()
        if not reachable:
            # Intentar por puerto web antes de abortar
            pre_check2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            pre_check2.settimeout(1.5)
            reachable = pre_check2.connect_ex((ip, 80)) == 0
            pre_check2.close()
        if not reachable:
            return False, f"El equipo {model or ip} no responde en los puertos de administración (9100/80). Verifique que esté encendido y conectado a la red."
    except Exception:
        pass

    # ═══════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════
    # REINICIO SEGURO DE HARDWARE (Socket RAW 9100 PRESCRIBE / PJL y SNMP SET)
    # ⚠️ Web Admin fue removido porque los endpoints CGI en Kyocera colapsaban la
    # memoria de la controladora generando error de sistema F245.
    # ═══════════════════════════════════════════════════════════════════════

    if mode == 'web_only':
        return False, "La modalidad de reinicio Web Admin ha sido desactivada para prevenir excepciones de firmware (error 245 en Kyocera). Utilice el reinicio directo seguro por PRESCRIBE/PJL."

    # 1. Socket RAW 9100 (PRESCRIBE nativo para Kyocera, PJL para HP/Brother)
    if mode in ('hybrid', 'all', 'raw_only', ''):
        raw_ok = False
        raw_method = ""
        if brand == 'KYOCERA':
            prescribe_cmd = b"!R! RES; EXIT;\r\n"
            if _send_raw_socket_cmd(ip, prescribe_cmd, timeout=3.0, retries=1):
                raw_ok = True
                raw_method = "PRESCRIBE RES (TCP 9100)"
        elif brand in ('HP', 'BROTHER'):
            pjl_cmd = b"\x1b%-12345X@PJL\r\n@PJL RESET\r\n@PJL INITIALIZE\r\n\x1b%-12345X\r\n"
            if _send_raw_socket_cmd(ip, pjl_cmd, timeout=3.0, retries=1):
                raw_ok = True
                raw_method = "PJL (TCP 9100)"

        if raw_ok:
            msg = f"Orden de reinicio enviada exitosamente a {model or 'equipo'} ({ip}) mediante [{raw_method}]. El equipo reiniciará su controladora en ~15 segundos."
            log.info(msg)
            # ⚡ En modo hybrid: PARAR AQUÍ — no enviar SNMP encima del socket
            if mode in ('hybrid', 'raw_only', ''):
                return True, msg

    # 3. Último recurso: SNMP SET prtGeneralReset(5) — reinicio seguro sin tocar NVRAM
    if mode in ('hybrid', 'all', 'snmp_only', ''):
        comm_attempts = [write_community, read_community, 'private', 'public']
        snmp_ok = False
        for comm in comm_attempts:
            if not comm:
                continue
            writer = SNMPWriter(ip, community=comm, port=port, timeout=2.0)
            # ⚠️ Primero valor 5 (resetPrinter) = reinicio seguro, preserva configuración
            if writer.set_int(OID_RESET, 5):
                snmp_ok = True
                break
            # Solo como último recurso absoluto: valor 3 (resetToNVRAM) — puede ser destructivo
            # DESHABILITADO por seguridad: este valor causó brickeo de controladoras
            # if writer.set_int(OID_RESET, 3):
            #     snmp_ok = True
            #     break

        if snmp_ok:
            msg = f"Orden de reinicio enviada exitosamente a {model or 'equipo'} ({ip}) mediante [SNMP SET prtGeneralReset(5) - resetPrinter]. El equipo reiniciará en ~20 segundos."
            log.info(msg)
            return True, msg

    return False, f"No se pudo enviar la orden de reinicio a {model or ip}. El equipo no respondió por Web Admin, puerto RAW 9100 ni por SNMP SET."


def _execute_device_purge_memory(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Purga la memoria interna y buffers de impresión del equipo por hardware para destrabar trabajos bloqueados:
    - KYOCERA: Comando PRESCRIBE '!R! DCLR; EXIT;\r\n' (Device Clear).
    - HP / LEXMARK / XEROX / SAMSUNG: Comando PJL EOJ (End of Job) + PCL Reset.
    - BROTHER / GENÉRICA: Secuencia PCL FormFeed / Reset (\\x1bE\\x0c).
    """
    ip = str(params.get('ip') or params.get('host') or '').strip()
    model = str(params.get('model') or params.get('printer_name') or '').strip()
    if not ip:
        return False, "No se especificó la dirección IP del equipo para purgar memoria"

    brand = detect_brand(model)

    if brand == 'KYOCERA':
        cmd_data = b"\x1bE\x0c!R! RES; EXIT;\r\n"
        desc = "PCL Reset + FormFeed + PRESCRIBE RES (Kyocera Buffer Flush)"
    elif brand in ('HP', 'LEXMARK', 'XEROX', 'SAMSUNG'):
        cmd_data = b"\x1b%-12345X@PJL\r\n@PJL JOB NAME = \"PURGE_MEMORY\"\r\n@PJL EOJ\r\n\x1b%-12345X\x1bE\r\n"
        desc = "PJL EOJ & PCL Reset"
    else:
        cmd_data = b"\x1bE\x0c\x1b%-12345X\r\n"
        desc = "PCL FormFeed & Buffer Reset"

    if _send_raw_socket_cmd(ip, cmd_data, timeout=3.5):
        msg = f"Memoria de impresión purgada con éxito en {model or 'impresora'} ({ip}) mediante [{desc}]."
        log.info(msg)
        return True, msg
    else:
        return False, f"No se pudo conectar al puerto RAW 9100 de {ip} para purgar la memoria del equipo."


def _execute_device_print_status(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Ordena al hardware imprimir su informe de estado y configuración nativo:
    - KYOCERA: PRESCRIBE '!R! STAT 1; EXIT;\r\n'
    - HP: PJL INFO CONFIG
    - Para marcas sin comando de estado seguro por RAW 9100, avisa y previene desperdicio de hojas.
    """
    ip = str(params.get('ip') or params.get('host') or '').strip()
    model = str(params.get('model') or params.get('printer_name') or '').strip()
    if not ip:
        return False, "No se especificó la dirección IP del equipo para imprimir estado"

    brand = detect_brand(model)

    if brand == 'KYOCERA':
        cmd_data = b"!R! STAT 1; EXIT;\r\n"
        if _send_raw_socket_cmd(ip, cmd_data, timeout=3.5):
            msg = f"Orden de Hoja de Estado (PRESCRIBE STAT 1) enviada con éxito a Kyocera {model or ip}. El equipo imprimirá su informe técnico."
            log.info(msg)
            return True, msg
        else:
            return False, f"No se pudo conectar al puerto 9100 de Kyocera ({ip}) para solicitar la hoja de estado."

    elif brand == 'HP':
        cmd_data = b"\x1b%-12345X@PJL\r\n@PJL INFO CONFIG\r\n\x1b%-12345X\r\n"
        if _send_raw_socket_cmd(ip, cmd_data, timeout=3.5):
            msg = f"Orden de Reporte de Configuración (PJL) enviada con éxito a HP {model or ip}."
            log.info(msg)
            return True, msg
        else:
            return False, f"No se pudo conectar al puerto 9100 de HP ({ip}) para solicitar el reporte."

    else:
        return False, f"La marca {brand} no cuenta con comando de reporte de estado seguro por RAW 9100. Utilice la interfaz web del equipo ({ip}) o la Hoja de Prueba del NOC."


def _execute_device_configure_hardware(params: dict, config: dict = None, live_printers: list = None, counters = None) -> tuple:
    """
    Configura parámetros de hardware avanzados remotamente por PRESCRIBE, PJL y SNMP SET:
    1. Temporizador de Reposo (Sleep Timer en minutos).
    2. Bandeja de Entrada Predeterminada ('auto', 'tray1', 'tray2', 'mp').
    3. Modo Dúplex por Defecto ('simplex', 'duplex_long', 'duplex_short').
    4. Identificación MIB-II / Command Center RX (sysLocation, sysContact, sysName).
    """
    ip = str(params.get('ip') or params.get('host') or '').strip()
    model = str(params.get('model') or params.get('printer_name') or '').strip()
    if not ip:
        return False, "No se especificó la dirección IP del equipo para configurar hardware"

    brand = params.get('brand') or detect_brand(model)
    cfg_data = config or {}
    write_community = params.get('snmp_write_community') or cfg_data.get('snmp_write_community', 'private')
    if crypto_utils and crypto_utils.is_encrypted(write_community):
        try:
            write_community = crypto_utils.decrypt_value(write_community)
        except Exception:
            pass
    read_community = cfg_data.get('snmp_community') or cfg_data.get('snmp_read_community') or 'public'
    port = int(cfg_data.get('snmp_port', 161))

    applied_items = []
    errors = []

    # ── Parámetros de Motor y Manejo de Papel (Kyocera PRESCRIBE Atómico) ──
    if brand == 'KYOCERA':
        frpo_cmds = []
        pending_labels = []

        if params.get('sleep_timer') is not None:
            try:
                st_val = int(params['sleep_timer'])
                if st_val > 0:
                    # En Kyocera ECOSYS modernos (M3550, M2040, etc.), FRPO N5 toma los minutos directamente (1 a 120/240 min)
                    # ⚠️ PRESCRIBE FRPO: SIN ESPACIO después de la coma (N5,valor no N5, valor)
                    frpo_cmds.append(f"FRPO N5,{st_val};")
                    pending_labels.append(f"⏱️ Sleep Timer: {st_val} min (PRESCRIBE FRPO N5,{st_val})")
            except Exception as e_st:
                errors.append(f"Error Sleep Timer: {e_st}")

        if params.get('default_tray'):
            tray_val = str(params['default_tray']).strip().lower()
            t_map = {'auto': 0, 'mp': 0, 'bypass': 0, 'tray1': 1, 'cassette1': 1, 'tray2': 2, 'cassette2': 2}
            code = t_map.get(tray_val, 0)
            frpo_cmds.append(f"FRPO R4,{code};")
            pending_labels.append(f"📥 Bandeja Predeterminada: {tray_val.upper()} (PRESCRIBE FRPO R4,{code})")

        if params.get('duplex'):
            duplex_val = str(params['duplex']).strip().lower()
            d_map = {'simplex': 0, 'duplex_long': 1, 'duplex': 1, 'duplex_short': 2}
            d_code = d_map.get(duplex_val, 0)
            frpo_cmds.append(f"FRPO N4,{d_code};")
            pending_labels.append(f"📄 Dúplex: {duplex_val} (PRESCRIBE FRPO N4,{d_code})")

        if frpo_cmds:
            # Enviar FRPO primero SIN RES para asegurar que los valores se graben en NVRAM
            cmd_write = f"!R! {' '.join(frpo_cmds)} EXIT;\r\n".encode('ascii')
            if _send_raw_socket_cmd(ip, cmd_write, timeout=4.5, retries=2):
                applied_items.extend(pending_labels)
                # Pequeña pausa y luego enviar RES separado para reiniciar la controladora
                import time as _t
                _t.sleep(0.5)
                cmd_res = b"!R! RES; EXIT;\r\n"
                _send_raw_socket_cmd(ip, cmd_res, timeout=3.0, retries=1)
            else:
                # Fallback: intentar envío atómico con todo junto
                cmd_all = f"!R! {' '.join(frpo_cmds)} RES; EXIT;\r\n".encode('ascii')
                if _send_raw_socket_cmd(ip, cmd_all, timeout=4.5, retries=1):
                    applied_items.extend(pending_labels)
                else:
                    errors.append("Fallo envío PRESCRIBE FRPO por socket 9100")

    elif brand == 'HP':
        pjl_cmds = []
        pending_labels = []

        if params.get('sleep_timer') is not None:
            try:
                st_val = int(params['sleep_timer'])
                if st_val > 0:
                    pjl_cmds.append(f"@PJL DEFAULT POWERSAVE = ON\r\n@PJL DEFAULT POWERSAVETIME = {st_val}")
                    pending_labels.append(f"⏱️ Sleep Timer: {st_val} min (PJL POWERSAVE)")
            except Exception as e_st:
                errors.append(f"Error Sleep Timer: {e_st}")

        if params.get('default_tray'):
            tray_val = str(params['default_tray']).strip().lower()
            pjl_t_map = {'mp': 'MANUALFEED', 'bypass': 'MANUALFEED', 'tray1': 'TRAY1', 'tray2': 'TRAY2', 'auto': 'AUTO'}
            p_code = pjl_t_map.get(tray_val, 'AUTO')
            pjl_cmds.append(f"@PJL DEFAULT INTRAY = {p_code}")
            pending_labels.append(f"📥 Bandeja Predeterminada: {p_code} (PJL)")

        if params.get('duplex'):
            duplex_val = str(params['duplex']).strip().lower()
            if duplex_val == 'simplex':
                pjl_cmds.append("@PJL DEFAULT DUPLEX = OFF")
            elif duplex_val == 'duplex_short':
                pjl_cmds.append("@PJL DEFAULT DUPLEX = ON\r\n@PJL DEFAULT BINDING = SHORTEDGE")
            else:
                pjl_cmds.append("@PJL DEFAULT DUPLEX = ON\r\n@PJL DEFAULT BINDING = LONGEDGE")
            pending_labels.append(f"📄 Dúplex: {duplex_val} (PJL)")

        if pjl_cmds:
            cmd = ("\x1b%-12345X@PJL\r\n" + "\r\n".join(pjl_cmds) + "\r\n\x1b%-12345X\r\n").encode('ascii')
            if _send_raw_socket_cmd(ip, cmd, timeout=4.5, retries=2):
                applied_items.extend(pending_labels)
            else:
                errors.append("Fallo envío PJL por socket 9100")

    else:
        # Soporte genérico para Brother / Ricoh / Lexmark / Samsung: intentar PJL estándar
        pjl_cmds = []
        pending_labels = []

        if params.get('sleep_timer') is not None:
            try:
                st_val = int(params['sleep_timer'])
                if st_val > 0:
                    pjl_cmds.append(f"@PJL DEFAULT POWERSAVETIME = {st_val}")
                    pending_labels.append(f"⏱️ Sleep Timer: {st_val} min (PJL genérico)")
            except Exception:
                pass

        if params.get('duplex'):
            duplex_val = str(params['duplex']).strip().lower()
            if duplex_val == 'simplex':
                pjl_cmds.append("@PJL DEFAULT DUPLEX = OFF")
            else:
                pjl_cmds.append("@PJL DEFAULT DUPLEX = ON")
            pending_labels.append(f"📄 Dúplex: {duplex_val} (PJL genérico)")

        if pjl_cmds:
            cmd = ("\x1b%-12345X@PJL\r\n" + "\r\n".join(pjl_cmds) + "\r\n\x1b%-12345X\r\n").encode('ascii')
            if _send_raw_socket_cmd(ip, cmd, timeout=4.5, retries=2):
                applied_items.extend(pending_labels)
            else:
                errors.append(f"Fallo envío PJL genérico a {brand} por socket 9100")


    # 4. Datos de Gestión y Localización (SNMP MIB-II Universal: sysLocation, sysContact, sysName)
    snmp_fields = [
        ('location', '1.3.6.1.2.1.1.6.0', 'Ubicación (sysLocation)'),
        ('contact', '1.3.6.1.2.1.1.4.0', 'Contacto (sysContact)'),
        ('device_name', '1.3.6.1.2.1.1.5.0', 'Nombre Dispositivo (sysName)')
    ]
    comm_attempts = [write_community, read_community, 'private', 'public']

    for param_key, oid_str, label_disp in snmp_fields:
        val_str = params.get(param_key)
        if val_str is not None and str(val_str).strip():
            clean_str = str(val_str).strip()
            ok_field = False
            for comm in comm_attempts:
                if not comm:
                    continue
                writer = SNMPWriter(ip, community=comm, port=port, timeout=2.0)
                if writer.set_str(oid_str, clean_str):
                    ok_field = True
                    applied_items.append(f"🏢 {label_disp}: '{clean_str}' (SNMP SET)")
                    break
            if not ok_field:
                errors.append(f"No se pudo escribir {label_disp} vía SNMP SET")

    if applied_items:
        res_summary = " ✅ " + " | ".join(applied_items)
        if errors:
            res_summary += f" (Advertencias: {'; '.join(errors)})"
        log.info(f"Parámetros de hardware aplicados a {ip} ({model}): {res_summary}")
        return True, res_summary
    else:
        err_str = "; ".join(errors) if errors else "Ningún parámetro fue modificado o el equipo no respondió"
        return False, f"No se pudieron aplicar los parámetros a {ip}: {err_str}"



def sync_multisite_telemetry(config: dict, printers: list, usb_printers: list, counters: CountersHistory = None, net_scan_enabled: bool = True):
    """
    Empaqueta la telemetría actual y la envía vía HTTPS PUSH al Servidor Relay Central.
    Descarga y despacha cualquier orden en cola (reiniciar spooler, purga, etc.) y envía ACK.
    """
    ms_section = config.get('multisite', {}) if isinstance(config.get('multisite'), dict) else {}
    server_url = (config.get('multisite_server_url') or ms_section.get('server_url') or 'https://printmonitor.com.ar').strip().rstrip('/')
    token = (config.get('multisite_token') or ms_section.get('token') or ms_section.get('auth_token') or config.get('multisite_admin_token') or ms_section.get('admin_token') or '').strip()
    client_id = (config.get('multisite_client_id') or ms_section.get('client_id') or config.get('client_name') or os.getenv('COMPUTERNAME', 'CLIENTE')).strip()
    site_name = (config.get('multisite_site_name') or ms_section.get('site_name') or config.get('client_name') or client_id).strip()

    # Si tiene token y servidor configurados, o si está explícitamente habilitado:
    enabled = bool(
        config.get('multisite_enabled')
        or ms_section.get('enabled')
        or (server_url and token)
    )

    if not enabled:
        log.info("ℹ️ Sincronización Multi-Sede omitida (no configurada).")
        return

    if not server_url or not token:
        log.warning("⚠️ Sincronización Multi-Sede habilitada pero falta server_url o token.")
        print("[AGENTE AVISO] Falta server_url o token para reportar al NOC.")
        return

    # Cargar lista negra local de equipos excluidos del monitoreo
    ignored_printers = []
    ignored_file = BASE_DIR / 'ignored_printers.json'
    if ignored_file.exists():
        try:
            with open(ignored_file, 'r', encoding='utf-8') as f_ign:
                ignored_printers = json.load(f_ign)
        except Exception as e_ign:
            log.warning(f"Error al leer ignored_printers.json: {e_ign}")

    # Consolidar impresoras y resúmenes de flota con soporte Multi-Agente
    all_printers = []
    laser_c = 0
    ink_c = 0
    therm_c = 0
    usb_c = 0
    warn_c = 0
    crit_c = 0

    agent_mode = str(config.get('agent_mode') or 'full').strip().lower()
    host_computer_name = os.getenv('COMPUTERNAME', '')
    if agent_mode == 'usb_only' or str(config.get('network_range', '')).strip().lower() in ('none', 'disabled', 'off', 'usb_only', 'solo_usb', 'usb', 'no'):
        net_scan_enabled = False
    else:
        net_scan_enabled = True

    # 1. Procesar impresoras SNMP de red y recolectar seriales/MACs para deduplicación local
    snmp_serials = set()
    snmp_macs = set()

    for p in printers:
        if _is_printer_ignored(p, ignored_printers):
            log.debug(f"Impresora {p.get('model')} ({p.get('ip')}) omitida de telemetría (en ignored_printers.json)")
            continue

        p.setdefault('host_pc', host_computer_name)
        s = str(p.get('serial') or '').strip().upper()
        if s and s not in ('0', 'ERR', 'NONE', 'N/D', '—'):
            snmp_serials.add(s)
        m = str(p.get('mac') or '').replace(':', '').replace('-', '').strip().upper()
        if m:
            snmp_macs.add(m)

        tech = p.get('tech', 'laser')
        if tech == 'thermal' or p.get('is_thermal'):
            therm_c += 1
        elif tech == 'inkjet':
            ink_c += 1
        else:
            laser_c += 1

        st = str(p.get('status') or '').lower()
        if 'error' in st or 'jam' in st or 'atasco' in st or p.get('paper_out') or p.get('ribbon_out') or p.get('head_open'):
            crit_c += 1
        elif 'warn' in st or 'low' in st or 'bajo' in st:
            warn_c += 1

        p.setdefault('is_online', True)
        all_printers.append(p)

    # 2. Procesar impresoras USB locales evitando duplicar las que ya se detectaron por SNMP
    for up in usb_printers:
        if _is_printer_ignored(up, ignored_printers):
            log.debug(f"Impresora USB {up.get('model')} ({up.get('port')}) omitida de telemetría (en ignored_printers.json)")
            continue

        up_s = str(up.get('serial') or '').strip().upper()
        up_m = str(up.get('mac') or '').replace(':', '').replace('-', '').strip().upper()
        if (up_s and up_s in snmp_serials) or (up_m and up_m in snmp_macs):
            log.info(f"Deduplicación local: Impresora USB {up.get('model')} ({up.get('port')}) omitida por coincidir con equipo de red SNMP (Serial: {up_s}).")
            continue

        up.setdefault('host_pc', host_computer_name)
        usb_c += 1
        tech = up.get('tech', 'usb')
        if tech == 'inkjet':
            ink_c += 1
        elif tech == 'thermal':
            therm_c += 1
        else:
            laser_c += 1

        st = str(up.get('status') or '').lower()
        if 'error' in st or 'jam' in st:
            crit_c += 1
        elif 'warn' in st:
            warn_c += 1
        up.setdefault('is_online', True)
        all_printers.append(up)

    # Relevar e incluir impresoras históricas que no respondieron en este escaneo (evita que desaparezcan del NOC)
    # AISLAMIENTO MULTI-AGENTE: Si este agente opera en modo satélite ('usb_only'), nunca debe reportar IPs de red como offline.
    if counters and getattr(counters, 'data', None):
        seen_keys = set()
        for p in all_printers:
            s = str(p.get('serial', '')).strip()
            m = str(p.get('mac', '')).strip().lower()
            h = str(p.get('hostname', '')).strip().lower()
            ip = str(p.get('ip', '')).strip()
            if s and s not in ('', 'N/D', '—', '0'): seen_keys.add(f"S:{s}")
            if m: seen_keys.add(f"M:{m}")
            if h and h not in ('', 'none', 'unknown'): seen_keys.add(f"H:{h}")
            if ip: seen_keys.add(f"IP:{ip}")

        expired_ips = []
        for h_ip, h_info in list(counters.data.items()):
            # Si este agente no escaneó la red (modo solo USB o sin escaneo), NUNCA reportar IPs de red como offline
            if (agent_mode == 'usb_only' or not net_scan_enabled) and not str(h_ip).startswith('USB:'):
                continue

            h_info_check = dict(h_info)
            h_info_check['ip'] = h_ip
            if _is_printer_ignored(h_info_check, ignored_printers):
                continue

            s = str(h_info.get('serial', '')).strip()
            m = str(h_info.get('mac', '')).strip().lower()
            h = str(h_info.get('hostname', '')).strip().lower()
            is_present = (
                (s and s not in ('', 'N/D', '—', '0') and f"S:{s}" in seen_keys)
                or (m and f"M:{m}" in seen_keys)
                or (h and h not in ('', 'none', 'unknown') and f"H:{h}" in seen_keys)
                or f"IP:{h_ip}" in seen_keys
            )
            if not is_present:
                last_m = sorted(h_info.get('months', {}).keys())[-1] if h_info.get('months') else None
                last_cnt = h_info.get('last_page_count') or (h_info['months'][last_m]['end'] if last_m else 0)
                last_dt = h_info.get('last_seen') or (h_info['months'][last_m].get('end_date') if last_m else 'Desconocida')

                # Si superó los 45 días sin conexión, se elimina del registro histórico
                if is_offline_expired(h_info, max_days=OFFLINE_RETENTION_DAYS) or is_offline_expired(last_dt, max_days=OFFLINE_RETENTION_DAYS):
                    expired_ips.append(h_ip)
                    continue

                h_tech = h_info.get('tech', 'laser')
                if h_tech == 'thermal': therm_c += 1
                elif h_tech == 'inkjet': ink_c += 1
                else: laser_c += 1

                offline_p = {
                    'ip': h_ip,
                    'model': h_info.get('model', 'Impresora'),
                    'serial': s,
                    'hostname': h,
                    'mac': m,
                    'tech': h_tech,
                    'page_count': last_cnt,
                    'toners': h_info.get('toners', {}),
                    'status': f"🔴 Desconectada ({last_dt})",
                    'is_online': False,
                    'is_ok': False,
                    'last_seen': last_dt,
                    'error_detail': f"Equipo apagado o fuera de red (Último reporte: {last_dt})"
                }
                all_printers.append(offline_p)

        if expired_ips:
            for exp_ip in expired_ips:
                counters.data.pop(exp_ip, None)
            counters.flush()
            log.info(f"🗑️ Eliminadas {len(expired_ips)} impresoras inactivas > {OFFLINE_RETENTION_DAYS} días de contadores: {expired_ips}")

    # Enriquecer cada impresora con el contador inicial del mes y páginas consumidas
    cur_month = datetime.now().strftime('%Y-%m')
    if counters and getattr(counters, 'data', None):
        for p in all_printers:
            ip = str(p.get('ip', '')).strip()
            s = str(p.get('serial', '')).strip()
            h_info = counters.data.get(ip)
            if not h_info and s and s not in ('', 'N/D', '—', '0'):
                for hip, hdata in counters.data.items():
                    if str(hdata.get('serial', '')).strip() == s:
                        h_info = hdata
                        break
            if h_info:
                m_data = h_info.get('months', {}).get(cur_month)
                if m_data:
                    start_val = m_data.get('start')
                    if start_val is not None:
                        p['initial_counter'] = start_val
                        curr_val = p.get('page_count') or m_data.get('end', start_val)
                        try:
                            p['monthly_pages'] = max(0, int(curr_val) - int(start_val))
                        except Exception:
                            p['monthly_pages'] = 0
                        p['month_start_date'] = m_data.get('start_date')

    summary = {
        "total": len(all_printers),
        "laser": laser_c,
        "inkjet": ink_c,
        "thermal": therm_c,
        "usb": usb_c,
        "warning": warn_c,
        "critical": crit_c
    }

    # Alertas activas de la base de datos de telemetría si existe
    active_alerts = []
    benign_terms = (
        'preparad', 'list', 'ready', 'en line', 'online', 'sleep', 'repos',
        'ahorro', 'bajo consumo', 'modo de reposo', 'energy saver', 'powersave',
        'imprim', 'print', 'proces', 'copi', 'standby', 'espera', 'ok',
        'calent', 'warming', 'auto', 'cassette', 'bandeja', 'operativ'
    )
    if alerts_history_db:
        try:
            raw_unresolved = alerts_history_db.get_unresolved_alerts(limit=20)
            active_alerts = [
                a for a in raw_unresolved
                if not any(b in str(a.get('message', '') or a.get('detail', '')).strip(" .!,;:").lower() for b in benign_terms)
            ]
        except Exception:
            pass

    # Incluir incidencias activas de la pasada actual (tóner no original, avisos en pantalla)
    seen_msgs = {str(a.get('message', '') or a.get('detail', '')) for a in active_alerts}
    for p in all_printers:
        if not p.get('is_ok', True) and p.get('error_detail'):
            e_msg = str(p['error_detail']).strip()
            clean_m = e_msg.strip(" .!,;:").lower()
            if any(b in clean_m for b in benign_terms):
                continue
            if e_msg and e_msg not in seen_msgs:
                active_alerts.append({
                    'ts': datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'printer_name': f"{p.get('model', 'Impresora')} ({p.get('ip', '')})",
                    'severity': 'WARN',
                    'message': e_msg,
                    'detail': e_msg
                })
                seen_msgs.add(e_msg)

    # Deduplicar impresoras para evitar envíos duplicados por IP o Serial
    seen_ips = set()
    seen_serials = set()
    deduped_all = []
    for p in sorted(all_printers, key=lambda x: (1 if x.get('is_online', True) else 0, 1 if str(x.get('serial','')).strip() not in ('', 'N/D', '—', '0') else 0), reverse=True):
        p_ip = str(p.get('ip', '')).strip().lower()
        p_s = str(p.get('serial', '')).strip().lower()
        if p_ip and p_ip not in ('', '0.0.0.0', '127.0.0.1') and p_ip in seen_ips:
            continue
        if p_s and p_s not in ('', 'n/d', '—', '0') and p_s in seen_serials:
            continue
        if p_ip and p_ip not in ('', '0.0.0.0', '127.0.0.1'):
            seen_ips.add(p_ip)
        if p_s and p_s not in ('', 'n/d', '—', '0'):
            seen_serials.add(p_s)

        if not p.get('site_name'):
            p['site_name'] = site_name
        if not p.get('client_id'):
            p['client_id'] = client_id
        deduped_all.append(p)

    all_printers = deduped_all
    summary['total'] = len(all_printers)

    import urllib.request
    import ssl

    # Verificación SSL: configurable via multisite_ssl_verify (por defecto False para soportar IPs locales/autofirmados)
    ssl_verify = bool(config.get('multisite_ssl_verify', False)) if config else False
    ctx = ssl.create_default_context()
    if not ssl_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    payload = {
        "protocol_version": "1.1",
        "client_id": client_id,
        "site_name": site_name,
        "agent_version": AGENT_VERSION,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_unix": int(time.time()),
        "system_info": {
            "computer_name": host_computer_name,
            "user_name": os.getenv('USERNAME', ''),
            "os": sys.platform,
            "agent_mode": agent_mode
        },
        "summary": summary,
        "printers": all_printers,
        "alerts": active_alerts
    }

    push_url = f"{server_url}/v1/telemetry/push"
    log.info(f"Enviando telemetría PUSH a {push_url} ({len(all_printers)} impresoras)...")

    import gzip as _gzip
    req_data_raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req_data = _gzip.compress(req_data_raw, compresslevel=6)
    compression_pct = round((1 - len(req_data) / max(len(req_data_raw), 1)) * 100, 1)
    log.info(f"Payload comprimido GZIP: {len(req_data_raw)} → {len(req_data)} bytes ({compression_pct}% reducción)")

    req = urllib.request.Request(
        push_url,
        data=req_data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Content-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"PrinterAgent/{AGENT_VERSION}"
        },
        method="POST"
    )

    MAX_RETRIES = 3
    resp_json = None
    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
                if resp.status == 200:
                    resp_bytes = resp.read()
                    resp_json = json.loads(resp_bytes.decode('utf-8'))
                    log.info(f"Telemetría Multi-Sede enviada y confirmada con éxito (intento {attempt}/{MAX_RETRIES}).")
                    print(f"[AGENTE] Telemetría enviada y confirmada con éxito ({len(all_printers)} impresoras reportadas).")
                    break
                else:
                    log.warning(f"Servidor Relay respondió con código {resp.status} (intento {attempt}/{MAX_RETRIES})")
                    last_err = f"HTTP {resp.status}"
        except Exception as e_req:
            last_err = e_req
            log.warning(f"Intento {attempt}/{MAX_RETRIES} falló al conectar con Servidor Relay: {e_req}")
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)

    if not resp_json:
        log.error(f"Error de conexión con Servidor Relay Multi-Sede tras {MAX_RETRIES} intentos ({push_url}): {last_err}")
        print(f"[AGENTE ERROR] Fallo de conexión persistente con {push_url}: {last_err}")
        # Guardar en cola offline con persistencia atómica para garantizar que no se pierdan datos
        try:
            tmp_q = OFFLINE_QUEUE_FILE.with_suffix('.tmp')
            with open(tmp_q, 'w', encoding='utf-8') as fq:
                json.dump(payload, fq, indent=2, ensure_ascii=False)
                fq.flush()
                os.fsync(fq.fileno())
            os.replace(tmp_q, OFFLINE_QUEUE_FILE)
            log.info("Telemetría guardada en cola offline local para sincronización posterior.")
        except Exception as ex_q:
            log.error(f"No se pudo guardar telemetría en cola offline: {ex_q}")

        # Alerta por webhook si hay fallo de conectividad persistente
        try:
            if config.get("webhook_enabled", False) and config.get("webhook_connectivity_error", True):
                from printer_tools.notifications.webhook_dispatcher import WebhookDispatcher, AlertEvent
                dispatcher = WebhookDispatcher(config)
                dispatcher.dispatch(AlertEvent(
                    event_type="connectivity",
                    severity="critical",
                    title="Fallo de Conexión WAN del Agente",
                    message=f"La sede {site_name} ({client_id}) no puede contactar al Servidor Relay tras {MAX_RETRIES} intentos: {last_err}",
                    site_id=client_id,
                    site_name=site_name
                ))
        except Exception as ex_wh_err:
            log.debug(f"Error al despachar alerta de conectividad: {ex_wh_err}")
        return

    # Evaluar y despachar alertas webhook de consumibles y estado de flota
    try:
        if config.get("webhook_enabled", False):
            from printer_tools.notifications.webhook_dispatcher import WebhookDispatcher
            dispatcher = WebhookDispatcher(config)
            dispatcher.evaluate_telemetry(
                site_id=client_id,
                site_name=site_name,
                printers=all_printers,
                thresholds=config
            )
    except Exception as ex_wh:
        log.warning(f"Error al evaluar alertas webhook en agente: {ex_wh}")

    # Si había telemetría acumulada en cola offline, purgarla al confirmarse la conectividad
    if OFFLINE_QUEUE_FILE.exists():
        try:
            OFFLINE_QUEUE_FILE.unlink()
            log.info("Conectividad restablecida: cola offline de telemetría purgada con éxito.")
        except Exception:
            pass

    # Procesar lista de seriales purgados por el usuario en el panel SaaS
    # (impresoras que el agente sigue reportando offline pero que el usuario ya eliminó)
    purge_serials = resp_json.get('purge_serials', [])
    if purge_serials and counters and getattr(counters, 'data', None):
        purged_count = 0
        for ps in purge_serials:
            ps_upper = str(ps).strip().upper()
            # Buscar por serial en los valores de counters_history (keyed by IP)
            ips_to_remove = []
            for h_ip, h_info in list(counters.data.items()):
                h_serial = str(h_info.get('serial', '')).strip().upper()
                if h_serial == ps_upper:
                    ips_to_remove.append(h_ip)
            for ip_rem in ips_to_remove:
                counters.data.pop(ip_rem, None)
                purged_count += 1
        if purged_count:
            counters.flush()
            log.info(f"🗑️ Purgados {purged_count} equipo(s) eliminados por el usuario del panel: {purge_serials}")
            print(f"[AGENTE] {purged_count} equipo(s) eliminado(s) del historial local por indicación del NOC: {purge_serials}")

        # Registrar en lista negra permanente local para evitar que vuelva a ser descubierta por escaneo
        try:
            ignored_file = BASE_DIR / 'ignored_printers.json'
            ignored_list = []
            if ignored_file.exists():
                with open(ignored_file, 'r', encoding='utf-8') as f_ign:
                    ignored_list = json.load(f_ign)
            added_any = False
            for ps in purge_serials:
                ps_str = str(ps).strip()
                if ps_str and not any(isinstance(ex, dict) and ex.get('serial') == ps_str for ex in ignored_list):
                    ignored_list.append({'serial': ps_str, 'reason': 'purged_by_noc', 'date': datetime.now().isoformat()})
                    added_any = True
            if added_any:
                with open(ignored_file, 'w', encoding='utf-8') as f_ign:
                    json.dump(ignored_list, f_ign, indent=2)
                log.info(f"🚫 Registrados {len(purge_serials)} equipos en ignored_printers.json local.")
        except Exception as e_ign_save:
            log.warning(f"Error guardando ignored_printers.json tras purga: {e_ign_save}")

    # Procesar estado de coordinación multi-agente emitido por el NOC
    ma_info = resp_json.get('multi_agent')
    if isinstance(ma_info, dict):
        save_multi_agent_state(ma_info)
        is_ldr = ma_info.get('is_lan_leader', True)
        ldr_pc = ma_info.get('leader_pc', '')
        tot_ag = ma_info.get('total_site_agents', 1)
        if tot_ag > 1:
            log.info(f"👥 Multi-Agente: {tot_ag} terminales activas en la sede. Líder LAN: '{ldr_pc}' (¿Esta terminal es líder?: {'SÍ' if is_ldr else 'NO'})")

    # Procesar órdenes remotas pendientes despachadas por el servidor
    pending_cmds = resp_json.get('pending_commands', [])
    if pending_cmds:
        log.info(f"Recibidos {len(pending_cmds)} comando(s) remoto(s) en cola.")
        print(f"[AGENTE] Ejecutando {len(pending_cmds)} comando(s) remoto(s) pendientes...")
        for cmd in pending_cmds:
            cmd_id = cmd.get('command_id')
            ok, out = execute_remote_command(cmd, config=config, live_printers=all_printers, counters=counters)
            ack_payload = {
                "command_id": cmd_id,
                "client_id": client_id,
                "status": "ok" if ok else "error",
                "output": out if ok else "",
                "error": out if not ok else "",
                "executed_at": int(time.time())
            }
            ack_req = urllib.request.Request(
                f"{server_url}/v1/commands/ack",
                data=json.dumps(ack_payload).encode('utf-8'),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": f"PrinterAgent/{AGENT_VERSION}"
                },
                method="POST"
            )
            # Reintento para el ACK del comando
            for ack_attempt in range(1, 3):
                try:
                    urllib.request.urlopen(ack_req, timeout=8, context=ctx)
                    log.info(f"ACK enviado para comando {cmd_id}: {'OK' if ok else 'ERROR'}")
                    break
                except Exception as e_ack:
                    log.warning(f"Error enviando ACK para {cmd_id} (intento {ack_attempt}/2): {e_ack}")
                    if ack_attempt < 2:
                        time.sleep(1)


def check_and_apply_agent_auto_update(force: bool = False, verbose: bool = False) -> tuple:
    """Consulta GitHub Releases y actualiza PrinterAgent.exe si hay versión nueva (o si force=True).
    Retorna (éxito: bool, mensaje: str).
    """
    try:
        cur_exe = Path(sys.executable).resolve()
        if cur_exe.name.upper().startswith('PRINTE~') and cur_exe.name.upper().endswith('.EXE'):
            cur_exe = cur_exe.parent / 'PrinterAgent.exe'
        # Limpiar residuos 8.3 previos si quedaron en la carpeta
        for orphan in cur_exe.parent.glob('PRINTE~*'):
            try:
                orphan.unlink()
            except Exception:
                pass
        if not getattr(sys, 'frozen', False):
            # === MODO SCRIPT .PY: Actualización de archivos fuente ===
            # Para agentes desplegados como scripts Python (no empaquetados con PyInstaller),
            # descargamos PrinterAgent.py y snmp_utils.py actualizados directamente del repositorio GitHub.
            try:
                script_path = Path(__file__).resolve()
                script_dir = script_path.parent

                import urllib.request
                import ssl
                ctx_py = ssl.create_default_context()
                try:
                    # Intentar con verificación SSL normal
                    urllib.request.urlopen(urllib.request.Request(
                        f"https://api.github.com/repos/{AGENT_GITHUB_REPO}/releases/latest",
                        headers={"User-Agent": f"PrinterAgent/{AGENT_VERSION}", "Accept": "application/vnd.github.v3+json"}
                    ), timeout=10, context=ctx_py)
                except (ssl.SSLError, urllib.error.URLError):
                    ctx_py = ssl.create_default_context()
                    ctx_py.check_hostname = False
                    ctx_py.verify_mode = ssl.CERT_NONE

                server_target = str(config.get('multisite_server_url') or config.get('server_url') or 'https://printmonitor.com.ar').rstrip('/')
                # Descargar archivos fuente crudos directamente desde el servidor SaaS o GitHub
                files_to_update = ["PrinterAgent.py", "snmp_utils.py"]
                updated_files = []

                for fname in files_to_update:
                    download_urls = [
                        f"{server_target}/api/agent/source/{fname}",
                        f"{server_target}/v1/agent/source/{fname}",
                        f"https://printmonitor.com.ar/api/agent/source/{fname}",
                        f"https://raw.githubusercontent.com/{AGENT_GITHUB_REPO}/main/{fname}"
                    ]
                    new_content = None
                    for raw_url in download_urls:
                        dl_req = urllib.request.Request(raw_url, headers={"User-Agent": f"PrinterAgent/{AGENT_VERSION}"})
                        try:
                            with urllib.request.urlopen(dl_req, timeout=15, context=ctx_py) as resp:
                                if resp.status == 200:
                                    data_read = resp.read()
                                    if len(data_read) >= (10000 if fname == "PrinterAgent.py" else 1000):
                                        new_content = data_read
                                        break
                        except Exception:
                            continue

                    if not new_content:
                        log.warning(f"No se pudo descargar {fname} desde ningún origen disponible.")
                        continue

                    try:
                        target_file = script_dir / fname
                        backup_file = script_dir / f"{fname}.bak"

                        # Crear backup del archivo actual
                        if target_file.exists():
                            try:
                                import shutil
                                shutil.copy2(target_file, backup_file)
                            except Exception:
                                pass

                        # Escritura atómica: tmp → fsync → replace
                        tmp_file = script_dir / f"{fname}.tmp"
                        with open(tmp_file, 'wb') as f_out:
                            f_out.write(new_content)
                            f_out.flush()
                            os.fsync(f_out.fileno())
                        os.replace(tmp_file, target_file)
                        updated_files.append(fname)
                        log.info(f"✅ Script {fname} actualizado correctamente ({len(new_content)} bytes)")

                    except Exception as e_dl:
                        log.warning(f"No se pudo guardar {fname}: {e_dl}")

                if updated_files:
                    msg = f"Scripts actualizados: {', '.join(updated_files)}. Los cambios se aplicarán en el próximo ciclo."
                    log.info(msg)
                    if verbose:
                        print(f"[OK] {msg}")
                    return True, msg
                else:
                    msg = "No se pudieron actualizar los scripts .py desde GitHub."
                    if verbose:
                        print(f"[AVISO] {msg}")
                    return False, msg

            except Exception as e_py_upd:
                msg = f"Error al actualizar scripts .py: {e_py_upd}"
                log.warning(msg)
                if verbose:
                    print(f"[ERROR] {msg}")
                return False, msg

        # Limpiar residuos de actualización previa (.old)
        old_exe = cur_exe.with_suffix('.old')
        if old_exe.exists():
            try:
                old_exe.unlink()
            except Exception:
                pass

        # Control de frecuencia: chequear máximo una vez cada 1 hora (salvo si force=True)
        state_file = cur_exe.parent / 'update_state.json'
        now_ts = int(time.time())
        if not force and state_file.exists():
            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    st_data = json.load(f)
                if now_ts - st_data.get('last_check', 0) < 3600:  # 1 hora
                    return False, "Chequeo omitido: período de gracia de 1 hora activo."
            except Exception:
                pass

        # Guardar timestamp de chequeo
        try:
            with open(state_file, 'w', encoding='utf-8') as f:
                json.dump({'last_check': now_ts, 'version': AGENT_VERSION}, f)
        except Exception:
            pass

        import urllib.request
        import ssl
        api_url = f"https://api.github.com/repos/{AGENT_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(
            api_url,
            headers={
                "User-Agent": f"PrinterTools-Agent/{AGENT_VERSION}",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        ctx = ssl.create_default_context()
        try:
            resp_cm = urllib.request.urlopen(req, timeout=12, context=ctx)
        except (ssl.SSLError, urllib.error.URLError) as e_ssl:
            if "CERTIFICATE_VERIFY_FAILED" in str(e_ssl) or isinstance(e_ssl, ssl.SSLCertVerificationError):
                log.warning(f"Certificados del sistema no validaron GitHub ({e_ssl}). Reintentando con contexto de contingencia...")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                resp_cm = urllib.request.urlopen(req, timeout=12, context=ctx)
            else:
                raise

        with resp_cm as resp:
            if resp.status != 200:
                msg = f"HTTP Error {resp.status} al consultar releases."
                if verbose:
                    print(f"[ERROR] {msg}")
                return False, msg
            rel_data = json.loads(resp.read().decode('utf-8'))

        tag_name = rel_data.get('tag_name', '')
        def normalize_v(val):
            parts = [int(x) for x in re.findall(r'\d+', str(val))]
            while len(parts) < 3:
                parts.append(0)
            return parts

        remote_v = normalize_v(tag_name)
        local_v = normalize_v(AGENT_VERSION)

        if not force and (not remote_v or remote_v <= local_v):
            msg = f"El agente ya está al día (v{AGENT_VERSION})."
            if verbose:
                print(f"[OK] {msg}")
            return True, msg

        log.info(f"{'Forzando' if force else 'Nueva versión detectada:'} actualización de Agente a {tag_name} (actual: v{AGENT_VERSION}). Descargando...")
        if verbose:
            print(f"[*] Descargando actualización de Agente ({tag_name})...")

        # Buscar asset ejecutable
        download_url = None
        for asset in rel_data.get('assets', []):
            name = asset.get('name', '').lower()
            if name.endswith('.exe') and ('agent' in name or 'agente' in name):
                download_url = asset.get('browser_download_url')
                break
        if not download_url:
            for asset in rel_data.get('assets', []):
                if asset.get('name', '').lower().endswith('.exe'):
                    download_url = asset.get('browser_download_url')
                    break

        if not download_url:
            msg = f"No se encontró binario .exe en el release {tag_name}"
            log.warning(msg)
            if verbose:
                print(f"[ERROR] {msg}")
            return False, msg

        # Descargar a archivo temporal
        tmp_exe = cur_exe.parent / f"PrinterAgent_update_{now_ts}.tmp"
        dl_req = urllib.request.Request(download_url, headers={"User-Agent": f"PrinterTools-Agent/{AGENT_VERSION}"})
        with urllib.request.urlopen(dl_req, timeout=60, context=ctx) as dl_resp:
            with open(tmp_exe, 'wb') as out_f:
                out_f.write(dl_resp.read())

        # Validar tamaño mínimo (> 5 MB)
        if tmp_exe.stat().st_size < 5 * 1024 * 1024:
            msg = "Descarga incompleta del nuevo agente (< 5 MB), cancelando actualización."
            log.warning(msg)
            try:
                tmp_exe.unlink()
            except Exception:
                pass
            if verbose:
                print(f"[ERROR] {msg}")
            return False, msg

        # Reemplazo atómico en Windows:
        target_old = old_exe
        if target_old.exists():
            try:
                target_old.unlink()
            except Exception:
                target_old = cur_exe.with_suffix(f'.old_{now_ts}')

        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        # En Windows, os.replace() de Python falla con WinError 32 si cur_exe está en ejecución.
        # 'cmd.exe /c move /y' sí permite mover el binario activo a .old y ubicar el nuevo de forma atómica.
        r_move1 = subprocess.run(
            ['cmd.exe', '/c', 'move', '/y', str(cur_exe), str(target_old)],
            capture_output=True, text=True, creationflags=cflags
        )
        if r_move1.returncode != 0:
            try:
                os.replace(cur_exe, target_old)
            except Exception:
                pass

        r_move2 = subprocess.run(
            ['cmd.exe', '/c', 'move', '/y', str(tmp_exe), str(cur_exe)],
            capture_output=True, text=True, creationflags=cflags
        )
        if r_move2.returncode != 0:
            os.replace(tmp_exe, cur_exe)

        msg = f"✅ Agente actualizado con éxito a {tag_name}. En el siguiente ciclo correrá la nueva versión."
        log.info(msg)
        if verbose:
            print(f"[OK] {msg}")
        return True, msg

    except Exception as e_upd:
        msg = f"Error en actualización del agente: {e_upd}"
        log.warning(msg)
        if verbose:
            print(f"[ERROR] {msg}")
        return False, msg


# ============================================================================
# CICLO PRINCIPAL DEL AGENTE
# ============================================================================
def run_agent():
    log.info("=" * 50)
    log.info("PrinterAgent iniciado")

    config = load_full_config()
    client = config.get('client_name') or os.getenv('COMPUTERNAME', 'Cliente')

    has_smtp = bool(config.get('email_to') and config.get('smtp_server'))
    if not has_smtp:
        log.info("ℹ️ Email de destino (email_to) no configurado aún.")
        log.info("ℹ️ El agente ejecutará el escaneo y registro de contadores. Las notificaciones por correo se habilitarán al configurar el email en el tab Agente.")

    efficient_io = bool(config.get('efficient_io', True))
    state    = AlertsState(auto_save=not efficient_io)
    counters = CountersHistory(auto_save=not efficient_io)
    engine   = AlertEngine(config, state)

    agent_mode = str(config.get('agent_mode') or 'full').strip().lower()
    net_scan_enabled = bool(config.get('network_scan_enabled', True))

    # MODO 1 AGENTE POR SEDE:
    # Todo agente escanea la red local LAN salvo que esté configurado explícitamente como 'usb_only'.
    if agent_mode == 'usb_only' or str(config.get('network_range', '')).strip().lower() in ('none', 'disabled', 'off', 'usb_only', 'solo_usb', 'usb', 'no'):
        net_scan_enabled = False
    else:
        net_scan_enabled = True

    # --- Escanear red SNMP (omitido en modo satélite / solo USB)
    printers = []
    if net_scan_enabled:
        known_ips = list(counters.data.keys()) if counters and getattr(counters, 'data', None) else None
        try:
            printers = scan_network_printers(
                config.get('network_range', 'auto'),
                config.get('snmp_community', 'public'),
                config.get('snmp_timeout', 0.8),
                config.get('snmp_port', 161),
                priority_ips=known_ips,
            )
        except Exception as e_snmp:
            log.error(f"Error en escaneo SNMP de red: {e_snmp}")
            printers = []
    else:
        log.info(f"ℹ️ Modo Satélite (Solo USB local) activo en {os.getenv('COMPUTERNAME')}: escaneo de red SNMP omitido.")

    # --- Escanear impresoras USB locales
    usb_printers = []
    if usb_monitor:
        try:
            usb_printers = usb_monitor.get_usb_and_local_printers()
        except Exception as e:
            log.warning(f"Error escaneando impresoras USB en PrinterAgent: {e}")

    # --- Guardar contadores de Red y USB locales
    for p in printers:
        if p.get('ip'):
            counters.record(
                p['ip'],
                p.get('model', ''),
                p.get('page_count', 0),
                p.get('serial', ''),
                p.get('hostname', ''),
                p.get('mac', ''),
                toners=p.get('toners') or p.get('supplies') or p.get('toner_levels'),
                tech=p.get('tech', 'laser'),
                status=p.get('status', 'OK'),
                error_detail=p.get('error_detail', ''),
                is_online=True
            )

    for up in usb_printers:
        usb_key = f"USB:{up.get('serial') or up.get('port') or up.get('name')}"
        counters.record(
            usb_key,
            up.get('model', ''),
            up.get('page_count', 0),
            serial=up.get('serial', ''),
            hostname=up.get('host_computer', os.getenv('COMPUTERNAME', '')),
            mac='',
            toners=up.get('toners') or up.get('supplies'),
            tech=up.get('tech', 'usb'),
            status=up.get('status_text', 'OK'),
            error_detail=up.get('detected_error_text', ''),
            is_online=up.get('online', True)
        )

    all_deltas = counters.get_all_deltas()
    log.info(f"Escaneo completado: {len(printers)} impresora(s) de red y {len(usb_printers)} USB locales auditadas.")

    # --- Alertas si hay SMTP
    if has_smtp:
        sent = engine.check_all(printers)
        if sent:
            log.info(f"Alertas enviadas: {sent}")

        if usb_printers:
            sent_u = engine.check_usb_alerts(usb_printers)
            if sent_u:
                log.info(f"Alertas USB enviadas: {sent_u}")

        # --- Alertas de cola
        queue_jobs = get_print_queue()
        sent_q = engine.check_queue_alerts(queue_jobs)
        if sent_q:
            log.info(f"Alertas de cola enviadas: {sent_q}")

        # --- Alertas de volumen
        sent_v = engine.check_volume_alerts(all_deltas)
        if sent_v:
            log.info(f"Alertas de volumen enviadas: {sent_v}")

        # --- Reporte mensual
        if should_run_monthly(config, state):
            log.info("Generando reporte mensual...")
            html  = build_monthly_report(printers, all_deltas, client_name=client)
            month = datetime.now().strftime('%B %Y').capitalize()
            subj  = f"📊 Reporte mensual de impresoras — {month} — {client}"
            if send_email(config, subj, html):
                state.set_last_sent('SYSTEM', 'monthly_report')
                log.info("Reporte mensual enviado correctamente.")

        # --- Aviso de vencimiento de licencia PRO (1 semana antes)
        try:
            from license_manager import LicenseManager
            l_data, _ = LicenseManager.validate_license()
            if l_data and l_data.get('license_type', '').upper() == 'PRO' and not l_data.get('is_perpetual', False):
                days_left = l_data.get('days_remaining', 999)
                if days_left <= 7:
                    exp_date = l_data.get('expiration_date', '')
                    warning_key = f"lic_warning_{exp_date}"
                    if not state.get_last_sent('SYSTEM', warning_key):
                        subj_lic = f"⚠️ Aviso de Vencimiento: Licencia Printer Tools PRO ({days_left} días restantes)"
                        html_lic = f"""
                        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: auto; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
                            <div style="background-color: #0f172a; padding: 20px; color: white;">
                                <h2 style="margin: 0; color: #38bdf8;">SolutionsDev · Printer Tools PRO</h2>
                                <p style="margin: 5px 0 0; color: #94a3b8; font-size: 13px;">Aviso Automático de Renovación de Servicio</p>
                            </div>
                            <div style="padding: 24px; color: #1e293b; line-height: 1.6;">
                                <h3 style="color: #ea580c; margin-top: 0;">⚠️ Su licencia PRO vencerá en {days_left} días</h3>
                                <p>Estimado/a <strong>{client}</strong>,</p>
                                <p>Le informamos que su suscripción a <strong>Printer Tools PRO</strong> en el equipo <code>{os.getenv('COMPUTERNAME', 'Equipo')}</code> está próxima a vencer.</p>
                                <table style="width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px;">
                                    <tr style="background-color: #f8fafc;"><td style="padding: 8px; border: 1px solid #cbd5e1;"><strong>Tipo de Licencia:</strong></td><td style="padding: 8px; border: 1px solid #cbd5e1;">Printer Tools PRO</td></tr>
                                    <tr><td style="padding: 8px; border: 1px solid #cbd5e1;"><strong>Fecha de Vencimiento:</strong></td><td style="padding: 8px; border: 1px solid #cbd5e1; color: #dc2626; font-weight: bold;">{exp_date}</td></tr>
                                    <tr style="background-color: #f8fafc;"><td style="padding: 8px; border: 1px solid #cbd5e1;"><strong>Días Restantes:</strong></td><td style="padding: 8px; border: 1px solid #cbd5e1; font-weight: bold;">{days_left} días</td></tr>
                                </table>
                                <p>Para evitar la interrupción del servicio de monitoreo autónomo y reportes mensuales, por favor coordine la renovación con su proveedor de servicios.</p>
                                <p style="margin-top: 25px;"><strong>Contacto Soporte SolutionsDev:</strong><br>
                                WhatsApp: <a href="https://wa.me/5491126029914">+54 9 11 2602-9914</a><br>
                                Email: printertools@solutionsdev.com.ar</p>
                            </div>
                            <div style="background-color: #f1f5f9; padding: 12px; text-align: center; color: #64748b; font-size: 11px;">
                                Mensaje generado automáticamente por PrinterAgent · SolutionsDev
                            </div>
                        </div>
                        """
                        if send_email(config, subj_lic, html_lic):
                            state.set_last_sent('SYSTEM', warning_key)
                            log.info(f"Email de aviso de vencimiento de licencia enviado ({days_left} días restantes).")
        except Exception as e_lic_warn:
            log.error(f"Error al verificar aviso de expiración de licencia: {e_lic_warn}")
    else:
        log.info("Alertas por correo omitidas (configure 'Email destino' en el tab Agente para recibirlas).")

    # --- Sincronización Multi-Sede NOC (PUSH Saliente y recepción de órdenes)
    try:
        sync_multisite_telemetry(config, printers, usb_printers, counters=counters, net_scan_enabled=net_scan_enabled)
    except Exception as e_ms:
        log.error(f"Error en sincronización Multi-Sede: {e_ms}")

    state.flush()
    counters.flush()
    log.info("Ciclo del agente completado con éxito.")

    # --- Auto-actualización desatendida desde GitHub Releases
    try:
        check_and_apply_agent_auto_update()
    except Exception as e_auto_upd:
        log.debug(f"Auto-actualización de agente: {e_auto_upd}")


# ============================================================================
# INTERFAZ GRÁFICA DE CONFIGURACIÓN RÁPIDA (ASISTENTE MULTI-ESCENARIO)
# ============================================================================
def show_agent_config_gui(initial_config=None):
    """Interfaz gráfica ligera para configurar el Agente según el escenario deseado."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
        import urllib.request
    except ImportError as e_tk:
        print(f"[ERROR] No se pudo iniciar el entorno gráfico: {e_tk}")
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"No se pudo iniciar la interfaz gráfica del configurador:\n{e_tk}\n\nPuede editar el archivo config.json directamente.",
                "Configuración de Agente",
                0x10
            )
        except Exception:
            pass
        return

    cfg = initial_config or load_full_config()

    root = tk.Tk()
    root.title(f"Configuración de Agente de Monitoreo v{AGENT_VERSION} — SolutionsDev")
    root.configure(bg="#0f172a")

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    W, H = 840, 520
    x = max(0, (sw - W) // 2)
    y = max(0, (sh - H) // 2)
    root.geometry(f"{W}x{H}+{x}+{y}")
    root.minsize(780, 480)
    root.resizable(True, True)

    # 1. Header institucional
    hdr = tk.Frame(root, bg="#1e293b", padx=18, pady=10)
    hdr.pack(side='top', fill='x')

    hdr_left = tk.Frame(hdr, bg="#1e293b")
    hdr_left.pack(side='left', fill='x', expand=True)
    tk.Label(hdr_left, text=f"🤖 CONFIGURADOR DE AGENTE DE IMPRESORAS (v{AGENT_VERSION})",
             font=("Segoe UI", 12, "bold"), fg="#38bdf8", bg="#1e293b").pack(anchor='w')
    tk.Label(hdr_left, text="Supervisión técnica y contable pasiva de impresoras y multifuncionales en red local y USB.",
             font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b").pack(anchor='w')

    # Badge de estado de servicio en Windows
    hdr_badge = tk.Frame(hdr, bg="#0f172a", padx=10, pady=4, highlightthickness=1, highlightbackground="#334155")
    hdr_badge.pack(side='right')
    lbl_svc_status = tk.Label(hdr_badge, text="⚪ Servicio: Verificando...", font=("Segoe UI", 8, "bold"),
                              fg="#94a3b8", bg="#0f172a")
    lbl_svc_status.pack()

    def refresh_service_badge():
        try:
            cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            r = subprocess.run(['schtasks', '/query', '/tn', 'PrinterAgent_SolutionsDev'],
                               capture_output=True, text=True, errors='replace', creationflags=cflags)
            if r.returncode == 0:
                lbl_svc_status.config(text="🟢 Servicio: Activo (SYSTEM)", fg="#10b981")
            else:
                lbl_svc_status.config(text="⚪ Servicio: No instalado", fg="#94a3b8")
        except Exception:
            lbl_svc_status.config(text="⚪ Servicio: No instalado", fg="#94a3b8")

    # 2. FOOTER FIJO AL FONDO (Garantiza visibilidad permanente en cualquier resolución)
    footer = tk.Frame(root, bg="#0f172a", padx=16, pady=8)
    footer.pack(side='bottom', fill='x')

    btn_bar1 = tk.Frame(footer, bg="#0f172a")
    btn_bar1.pack(fill='x', pady=(0, 5))

    btn_bar2 = tk.Frame(footer, bg="#0f172a")
    btn_bar2.pack(fill='x')

    # 3. CONTENEDOR CENTRAL DE 2 COLUMNAS
    body = tk.Frame(root, bg="#0f172a", padx=16, pady=6)
    body.pack(side='top', fill='both', expand=True)

    col_left = tk.Frame(body, bg="#0f172a")
    col_left.pack(side='left', fill='both', expand=True, padx=(0, 6))

    col_right = tk.Frame(body, bg="#0f172a")
    col_right.pack(side='right', fill='both', expand=True, padx=(6, 0))

    # --- COLUMNA IZQUIERDA ---
    # Card 1: Identificación de Sede
    f_id = tk.LabelFrame(col_left, text=" 🏢 Identificación de la Empresa / Sede ",
                         font=("Segoe UI", 8, "bold"), fg="#38bdf8", bg="#1e293b", padx=12, pady=8)
    f_id.pack(fill='x', pady=(0, 8))

    tk.Label(f_id, text="Nombre de Empresa / Cliente:", font=("Segoe UI", 8, "bold"), fg="#f8fafc", bg="#1e293b").pack(anchor='w', pady=(2, 2))
    e_client = tk.Entry(f_id, font=("Segoe UI", 9), bg="#0f172a", fg="#f8fafc", insertbackground="white",
                        relief='flat', highlightthickness=1, highlightbackground="#334155")
    e_client.pack(fill='x', pady=(0, 6))
    e_client.insert(0, cfg.get('client_name') or cfg.get('multisite_site_name') or '')

    tk.Label(f_id, text="ID de Sede / Sucursal:", font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b").pack(anchor='w', pady=(2, 2))
    e_cid = tk.Entry(f_id, font=("Segoe UI", 9), bg="#0f172a", fg="#f8fafc", insertbackground="white",
                     relief='flat', highlightthickness=1, highlightbackground="#334155")
    e_cid.pack(fill='x', pady=(0, 2))
    e_cid.insert(0, cfg.get('multisite_client_id') or os.getenv('COMPUTERNAME', 'CLI-01'))

    # Card 2: Escenario de Operación
    f_mode = tk.LabelFrame(col_left, text=" 🎯 Escenario de Operación ",
                          font=("Segoe UI", 8, "bold"), fg="#38bdf8", bg="#1e293b", padx=12, pady=6)
    f_mode.pack(fill='x', pady=(0, 6))

    scenario_var = tk.StringVar(value="noc" if cfg.get('multisite_enabled') else ("email" if cfg.get('email_to') else "noc"))

    # Card 3: Rol del Agente en esta PC (Multi-Agente en misma Sede)
    f_role = tk.LabelFrame(col_left, text=" 💻 Rol en la Sede (Multi-Agente) ",
                           font=("Segoe UI", 8, "bold"), fg="#38bdf8", bg="#1e293b", padx=12, pady=6)
    f_role.pack(fill='both', expand=True)

    cur_mode = str(cfg.get('agent_mode') or 'auto').lower()
    if cur_mode not in ('auto', 'full', 'usb_only'):
        cur_mode = "usb_only" if (cfg.get('network_scan_enabled') is False or cfg.get('network_range') in ('none', 'usb_only')) else "auto"
    agent_role_var = tk.StringVar(value=cur_mode)

    r_auto = tk.Radiobutton(f_role, text="🤖 Automático (Coordinado por el Servidor NOC)",
                            variable=agent_role_var, value="auto", font=("Segoe UI", 8, "bold"),
                            fg="#38bdf8", bg="#1e293b", activebackground="#1e293b", selectcolor="#0f172a")
    r_auto.pack(anchor='w', pady=(2, 2))
    tk.Label(f_role, text="    El NOC elige el líder LAN de la sede; los demás auditan sus USB locales.", font=("Segoe UI", 7), fg="#94a3b8", bg="#1e293b").pack(anchor='w', pady=(0, 3))

    r_full = tk.Radiobutton(f_role, text="🌐 Agente Principal (Forzar Red LAN + USB)",
                            variable=agent_role_var, value="full", font=("Segoe UI", 8, "bold"),
                            fg="#f8fafc", bg="#1e293b", activebackground="#1e293b", selectcolor="#0f172a")
    r_full.pack(anchor='w', pady=(2, 2))
    tk.Label(f_role, text="    Escanea impresoras en red local y puertos USB de esta PC.", font=("Segoe UI", 7), fg="#94a3b8", bg="#1e293b").pack(anchor='w', pady=(0, 3))

    r_usb = tk.Radiobutton(f_role, text="💻 Agente Satélite (Solo USB local)",
                           variable=agent_role_var, value="usb_only", font=("Segoe UI", 8, "bold"),
                           fg="#f8fafc", bg="#1e293b", activebackground="#1e293b", selectcolor="#0f172a")
    r_usb.pack(anchor='w', pady=(2, 2))
    tk.Label(f_role, text="    Solo impresoras conectadas físicamente a esta PC (sin red).", font=("Segoe UI", 7), fg="#94a3b8", bg="#1e293b").pack(anchor='w')

    # --- COLUMNA DERECHA ---
    f_cards = tk.Frame(col_right, bg="#0f172a")
    f_cards.pack(fill='both', expand=True)

    # Card Modo NOC
    card_noc = tk.LabelFrame(f_cards, text=" ☁️ Modo Servidor Central NOC (Flotas / Alquileres) ",
                             font=("Segoe UI", 8, "bold"), fg="#10b981", bg="#1e293b", padx=12, pady=8)

    tk.Label(card_noc, text="URL Servidor Central NOC:", font=("Segoe UI", 8, "bold"), fg="#f8fafc", bg="#1e293b").pack(anchor='w', pady=(2, 2))
    e_noc_url = tk.Entry(card_noc, font=("Segoe UI", 9), bg="#0f172a", fg="#f8fafc", insertbackground="white",
                         relief='flat', highlightthickness=1, highlightbackground="#334155")
    e_noc_url.pack(fill='x', pady=(0, 6))
    e_noc_url.insert(0, cfg.get('multisite_server_url') or 'https://printmonitor.com.ar')

    tk.Label(card_noc, text="Token de Seguridad de la Sede:", font=("Segoe UI", 8, "bold"), fg="#f8fafc", bg="#1e293b").pack(anchor='w', pady=(2, 2))

    f_token = tk.Frame(card_noc, bg="#1e293b")
    f_token.pack(fill='x', pady=(0, 6))
    e_noc_token = tk.Entry(f_token, font=("Segoe UI", 9), show="*", bg="#0f172a", fg="#f8fafc", insertbackground="white",
                           relief='flat', highlightthickness=1, highlightbackground="#334155")
    e_noc_token.pack(side='left', fill='x', expand=True)
    e_noc_token.insert(0, cfg.get('multisite_token') or '')

    def toggle_show_token():
        if e_noc_token.cget('show') == '*':
            e_noc_token.config(show='')
            btn_show_token.config(text="🙈")
        else:
            e_noc_token.config(show='*')
            btn_show_token.config(text="👁️")

    btn_show_token = tk.Button(f_token, text="👁️", font=("Segoe UI", 8), bg="#334155", fg="white",
                               relief='flat', padx=6, pady=1, cursor='hand2', command=toggle_show_token)
    btn_show_token.pack(side='left', padx=(4, 0))

    f_int = tk.Frame(card_noc, bg="#1e293b")
    f_int.pack(fill='x', pady=(0, 6))
    tk.Label(f_int, text="Intervalo de Envío:", font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b").pack(side='left', pady=2)
    cb_noc_interval = ttk.Combobox(f_int, values=["5 minutos (Recomendado)", "10 minutos", "15 minutos", "30 minutos", "60 minutos"], state="readonly", width=22)
    cb_noc_interval.pack(side='right')
    cur_mins = cfg.get('check_interval_minutes') or 5
    cb_noc_interval.set(f"{cur_mins} minutos (Recomendado)" if cur_mins == 5 else f"{cur_mins} minutos")

    # Feedback de prueba de conexión
    f_test = tk.Frame(card_noc, bg="#1e293b")
    f_test.pack(fill='x', pady=(4, 0))

    def test_noc_conn():
        u = e_noc_url.get().strip().rstrip('/')
        if not u:
            lbl_test_res.config(text="❌ Ingrese una URL", fg="#ef4444")
            return
        lbl_test_res.config(text="⏳ Conectando...", fg="#38bdf8")
        root.update_idletasks()
        try:
            req = urllib.request.Request(f"{u}/health", headers={'User-Agent': 'PrinterAgent'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    lbl_test_res.config(text="✅ Conexión Exitosa con Servidor", fg="#10b981")
                else:
                    lbl_test_res.config(text=f"⚠️ Código HTTP {resp.status}", fg="#f59e0b")
        except Exception as ex:
            lbl_test_res.config(text=f"❌ Fallo: {ex}", fg="#ef4444")

    btn_test_noc = tk.Button(f_test, text="🧪 Probar Conexión Servidor", font=("Segoe UI", 8, "bold"),
                             bg="#0f172a", activebackground="#334155", fg="#38bdf8", activeforeground="#38bdf8",
                             relief='groove', padx=10, pady=3, cursor='hand2', command=test_noc_conn)
    btn_test_noc.pack(side='left')
    lbl_test_res = tk.Label(f_test, text="", font=("Segoe UI", 8), bg="#1e293b")
    lbl_test_res.pack(side='left', padx=(8, 0))

    # Card Modo Email
    card_email = tk.LabelFrame(f_cards, text=" 📧 Modo Autónomo por Email (Reportes Directos) ",
                               font=("Segoe UI", 8, "bold"), fg="#f59e0b", bg="#1e293b", padx=12, pady=8)

    tk.Label(card_email, text="Enviar Alertas y Reportes a:", font=("Segoe UI", 8, "bold"), fg="#f8fafc", bg="#1e293b").pack(anchor='w', pady=(2, 2))
    e_email_to = tk.Entry(card_email, font=("Segoe UI", 9), bg="#0f172a", fg="#f8fafc", insertbackground="white",
                          relief='flat', highlightthickness=1, highlightbackground="#334155")
    e_email_to.pack(fill='x', pady=(0, 6))
    e_email_to.insert(0, cfg.get('email_to') or '')

    tk.Label(card_email, text="Servidor SMTP:", font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b").pack(anchor='w', pady=(2, 2))
    cb_smtp = ttk.Combobox(card_email, values=["SolutionsDev (Predeterminado)", "Personalizado (Gmail / Outlook / Propio)"], state="readonly")
    cb_smtp.pack(fill='x', pady=(0, 6))
    cb_smtp.set("SolutionsDev (Predeterminado)")

    f_toner = tk.Frame(card_email, bg="#1e293b")
    f_toner.pack(fill='x', pady=(0, 6))
    tk.Label(f_toner, text="Avisar con Tóner Menor a:", font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b").pack(side='left', pady=2)
    cb_toner = ttk.Combobox(f_toner, values=["5% (Recomendado)", "10%", "15%", "20%", "25%"], state="readonly", width=18)
    cb_toner.pack(side='right')
    cb_toner.set("5% (Recomendado)")

    def update_scenario_view():
        sc = scenario_var.get()
        if sc == "noc":
            card_email.pack_forget()
            card_noc.pack(fill='both', expand=True)
        else:
            card_noc.pack_forget()
            card_email.pack(fill='both', expand=True)

    r_noc = tk.Radiobutton(f_mode, text="☁️ Servidor NOC (Alquiler / Flotas) — Recomendado",
                           variable=scenario_var, value="noc", font=("Segoe UI", 8, "bold"),
                           fg="#f8fafc", bg="#1e293b", activebackground="#1e293b", selectcolor="#0f172a",
                           command=update_scenario_view)
    r_noc.pack(anchor='w', pady=(2, 4))
    tk.Label(f_mode, text="    Telemetría continua, consumibles en vivo y soporte remoto.", font=("Segoe UI", 7), fg="#94a3b8", bg="#1e293b").pack(anchor='w', pady=(0, 8))

    r_email = tk.Radiobutton(f_mode, text="📧 Autónomo por Email (Reportes Mensuales)",
                             variable=scenario_var, value="email", font=("Segoe UI", 8, "bold"),
                             fg="#f8fafc", bg="#1e293b", activebackground="#1e293b", selectcolor="#0f172a",
                             command=update_scenario_view)
    r_email.pack(anchor='w', pady=(2, 4))
    tk.Label(f_mode, text="    Envío periódico de correos al técnico sin servidor central.", font=("Segoe UI", 7), fg="#94a3b8", bg="#1e293b").pack(anchor='w')

    update_scenario_view()

    # --- CALLBACKS DE ACCIÓN ---
    def on_save_config():
        sc = scenario_var.get()
        cli_name = e_client.get().strip()
        cid = e_cid.get().strip() or os.getenv('COMPUTERNAME', 'CLI-01')

        new_cfg = cfg.copy()
        new_cfg['client_name'] = cli_name
        new_cfg['multisite_client_id'] = cid
        new_cfg['multisite_site_name'] = cli_name or cid

        # Configuración Multi-Agente
        role = agent_role_var.get()
        new_cfg['agent_mode'] = role
        if role == 'auto':
            new_cfg['network_scan_enabled'] = True
            if new_cfg.get('network_range') in ('none', 'usb_only'):
                new_cfg['network_range'] = 'auto'
        elif role == 'usb_only':
            new_cfg['network_scan_enabled'] = False
            if new_cfg.get('network_range') == 'auto':
                new_cfg['network_range'] = 'none'
        elif role == 'full':
            new_cfg['network_scan_enabled'] = True
            if new_cfg.get('network_range') in ('none', 'usb_only'):
                new_cfg['network_range'] = 'auto'

        if sc == "noc":
            new_cfg['multisite_enabled'] = True
            new_cfg['multisite_server_url'] = e_noc_url.get().strip()
            new_cfg['multisite_token'] = e_noc_token.get().strip()
            mins = 5
            try:
                mins = int(cb_noc_interval.get().split()[0])
            except Exception:
                pass
            new_cfg['check_interval_minutes'] = mins
            new_cfg['multisite'] = {
                'enabled': True,
                'server_url': new_cfg['multisite_server_url'],
                'client_id': cid,
                'site_name': cli_name or cid,
                'auth_token': new_cfg['multisite_token'],
                'push_interval_sec': mins * 60
            }
        else:
            new_cfg['multisite_enabled'] = False
            new_cfg['email_to'] = e_email_to.get().strip()
            try:
                pct = int(cb_toner.get().replace('%', '').split()[0])
                new_cfg['alert_toner_low_pct'] = pct
            except Exception:
                pass

        target_dirs = [BASE_DIR]
        try:
            exe_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
            if exe_dir not in target_dirs:
                target_dirs.insert(0, exe_dir)
        except Exception:
            pass

        save_cfg = crypto_utils.protect_config(new_cfg) if crypto_utils else new_cfg
        saved_paths = []
        for d in target_dirs:
            try:
                d.mkdir(exist_ok=True)
                for fname in ['config.json', 'agent_config.json']:
                    p = d / fname
                    with open(p, 'w', encoding='utf-8') as f:
                        json.dump(save_cfg, f, indent=2, ensure_ascii=False)
                saved_paths.append(str(d / 'config.json'))
            except Exception as ex_s:
                log.error(f"Error al guardar en {d}: {ex_s}")

        messagebox.showinfo("Configuración Guardada", f"Configuración guardada exitosamente en:\n{saved_paths[0]}", parent=root)

    def on_install_task():
        on_save_config()
        exe = sys.executable if sys.executable.endswith('.exe') else str(Path(sys.argv[0]).resolve())
        short_exe = get_windows_short_path(exe)
        target_tr = f'"{short_exe}" --run' if ' ' in short_exe else f'{short_exe} --run'
        interval = 5
        try:
            interval = int(cb_noc_interval.get().split()[0])
        except Exception:
            pass
        cmds = [
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/ru', 'SYSTEM', '/f'],
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/rl', 'HIGHEST', '/f'],
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/f']
        ]
        ok = False
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace', creationflags=cflags)
            if r.returncode == 0:
                ok = True
                break
        refresh_service_badge()
        if ok:
            messagebox.showinfo("Servicio Instalado", f"¡Tarea Programada instalada exitosamente!\nEl agente ahora iniciará automáticamente con Windows cada {interval} minutos.", parent=root)
        else:
            messagebox.showwarning("Atención", "No se pudo instalar automáticamente. Asegúrese de ejecutar este configurador como Administrador.", parent=root)

    def on_uninstall_task():
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        subprocess.run(['schtasks', '/delete', '/tn', 'PrinterAgent_SolutionsDev', '/f'],
                       capture_output=True, creationflags=cflags)
        refresh_service_badge()
        messagebox.showinfo("Servicio Desinstalado", "La tarea programada de Windows ha sido eliminada.", parent=root)

    def on_test_scan():
        btn_scan.config(state='disabled', text="⏳ Escaneando...")
        root.config(cursor="wait")

        def _worker():
            try:
                net_printers = scan_network_printers('auto', cfg.get('snmp_community', 'public'), 0.8, 161)
                usb_p = []
                if usb_monitor:
                    usb_p = usb_monitor.get_usb_and_local_printers()
                tot = len(net_printers) + len(usb_p)
                det = f"Total detectadas: {tot}\n- Impresoras de Red (SNMP): {len(net_printers)}\n- Impresoras USB / Locales: {len(usb_p)}\n\n"
                for p in net_printers:
                    det += f"• [{p.get('ip')}] {p.get('model', 'Desconocido')} - Tóner: {p.get('toner_pct', '?')}%\n"
                for u in usb_p:
                    det += f"• [USB] {u.get('name', 'USB')} (S/N: {u.get('serial', 'N/D')}) - Páginas: {u.get('page_count', 0)} - Estado: {u.get('status_text', 'OK')}\n"
                root.after(0, lambda: messagebox.showinfo("Resultado del Escaneo", det, parent=root))
            except Exception as ex_sc:
                root.after(0, lambda: messagebox.showerror("Error", f"Fallo al escanear: {ex_sc}", parent=root))
            finally:
                root.after(0, lambda: (btn_scan.config(state='normal', text="📡 Probar Escaneo LAN/USB"), root.config(cursor="")))

        threading.Thread(target=_worker, daemon=True).start()

    def on_gui_force_update():
        if not messagebox.askyesno(
            "Actualizar Agente",
            f"¿Desea consultar y forzar la actualización a la última versión disponible en GitHub?\n\nVersión actual instalada: v{AGENT_VERSION}",
            parent=root
        ):
            return
        root.config(cursor="wait")
        root.update_idletasks()
        try:
            ok, msg = check_and_apply_agent_auto_update(force=True, verbose=True)
            root.config(cursor="")
            if ok:
                messagebox.showinfo("Actualización Exitosa", f"¡El agente se actualizó exitosamente!\n\n{msg}\n\nReinicie la aplicación para reflejar la nueva versión.", parent=root)
            else:
                messagebox.showwarning("Aviso de Actualización", f"No se pudo completar la actualización:\n\n{msg}", parent=root)
        except Exception as ex_u:
            root.config(cursor="")
            messagebox.showerror("Error", f"Fallo al actualizar: {ex_u}", parent=root)

    # Poblado de botones en FOOTER
    btn_save = tk.Button(btn_bar1, text="💾 Guardar Configuración", font=("Segoe UI", 9, "bold"),
                         bg="#0284c7", activebackground="#0369a1", fg="white", activeforeground="white",
                         relief='flat', padx=16, pady=6, cursor='hand2', command=on_save_config)
    btn_save.pack(side='left', fill='x', expand=True, padx=(0, 4))

    btn_install = tk.Button(btn_bar1, text="⚡ Instalar Servicio Windows (Automático)", font=("Segoe UI", 9, "bold"),
                            bg="#059669", activebackground="#047857", fg="white", activeforeground="white",
                            relief='flat', padx=16, pady=6, cursor='hand2', command=on_install_task)
    btn_install.pack(side='left', fill='x', expand=True, padx=(4, 0))

    btn_scan = tk.Button(btn_bar2, text="📡 Probar Escaneo LAN/USB", font=("Segoe UI", 8),
                         bg="#1e293b", activebackground="#334155", fg="#f8fafc", activeforeground="#f8fafc",
                         relief='flat', padx=10, pady=4, cursor='hand2', command=on_test_scan)
    btn_scan.pack(side='left', padx=(0, 4))

    btn_update = tk.Button(btn_bar2, text="🚀 Actualizar a Última Versión", font=("Segoe UI", 8, "bold"),
                           bg="#6366f1", activebackground="#4f46e5", fg="white", activeforeground="white",
                           relief='flat', padx=12, pady=4, cursor='hand2', command=on_gui_force_update)
    btn_update.pack(side='left', padx=4)

    btn_uninst = tk.Button(btn_bar2, text="⏹️ Desinstalar Servicio", font=("Segoe UI", 8),
                           bg="#1e293b", activebackground="#334155", fg="#ef4444", activeforeground="#ef4444",
                           relief='flat', padx=10, pady=4, cursor='hand2', command=on_uninstall_task)
    btn_uninst.pack(side='left', padx=4)

    btn_close = tk.Button(btn_bar2, text="Cerrar", font=("Segoe UI", 8),
                          bg="#1e293b", activebackground="#334155", fg="#94a3b8", activeforeground="white",
                          relief='flat', padx=14, pady=4, cursor='hand2', command=root.destroy)
    btn_close.pack(side='right')

    refresh_service_badge()
    root.mainloop()


# ============================================================================
# INSTALADOR AUTO-EXTRAÍBLE (Self-Extracting Installer)
# Cuando el NOC genera un .exe para un cliente, le concatena al final del
# ejecutable un marcador mágico seguido de un JSON de configuración.
# Al ejecutar el .exe resultante, este bloque lo detecta y realiza la
# instalación automática en C:\PrinterTools-Agente.
# ============================================================================
AGENT_CONFIG_MARKER = b'__AGENT_CONFIG__='
INSTALL_DIR = Path(r'C:\PrinterTools-Agente')


def _check_embedded_config() -> dict | None:
    """Lee el propio ejecutable buscando configuración embebida por el NOC."""
    try:
        exe_path = Path(sys.executable)
        if not exe_path.exists() or exe_path.stat().st_size < 1000:
            return None
        # Leer los últimos 64KB donde estará el payload
        with open(exe_path, 'rb') as f:
            f.seek(max(0, exe_path.stat().st_size - 65536))
            tail = f.read()
        idx = tail.rfind(AGENT_CONFIG_MARKER)
        if idx < 0:
            return None
        json_bytes = tail[idx + len(AGENT_CONFIG_MARKER):]
        return json.loads(json_bytes.decode('utf-8'))
    except Exception as e:
        log.error(f"Error leyendo config embebida: {e}")
        return None


def _run_self_install(embedded_cfg: dict):
    """Ejecuta la instalación automática con interfaz gráfica y manejo seguro de procesos."""
    import shutil
    import ctypes
    import time
    import stat
    import threading

    cid = embedded_cfg.get('multisite_client_id') or embedded_cfg.get('client_id', 'Sede')
    sname = embedded_cfg.get('client_name') or embedded_cfg.get('site_name', cid)
    server_url = embedded_cfg.get('multisite_server_url') or 'https://printmonitor.com.ar'
    interval = embedded_cfg.get('check_interval_minutes', 5)

    # 1. Asegurar privilegios de administrador
    is_admin_user = False
    try:
        is_admin_user = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        pass

    if not is_admin_user:
        try:
            # Re-lanzar con elevación UAC pasando solo argumentos después del exe
            params = " ".join(f'"{a}"' for a in sys.argv[1:])
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            sys.exit(0)
        except Exception as e_elev:
            try:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Se requieren privilegios de Administrador para instalar el agente.\n\nError: {e_elev}",
                    "Instalador de Agente",
                    0x10
                )
            except Exception:
                pass
            sys.exit(1)

    # 2. Configurar entorno gráfico Tkinter para el instalador
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox
    except Exception:
        tk = None

    if not tk:
        # Fallback si no hubiera Tkinter
        return

    root = tk.Tk()
    root.title("Instalador de Agente de Monitoreo")
    root.geometry("490x300")
    root.resizable(False, False)
    root.configure(bg="#0f172a")

    # Centrar ventana en pantalla
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = (sw - 490) // 2
    y = (sh - 300) // 2
    root.geometry(f"+{x}+{y}")

    # Header Card
    hdr = tk.Frame(root, bg="#1e293b", padx=16, pady=12)
    hdr.pack(fill='x')

    lbl_title = tk.Label(
        hdr, text=f"Instalando Agente: {sname}",
        font=("Segoe UI", 11, "bold"), fg="#f8fafc", bg="#1e293b"
    )
    lbl_title.pack(anchor='w')

    lbl_sub = tk.Label(
        hdr, text=f"ID Sede: {cid}  •  Reporte cada {interval} min",
        font=("Segoe UI", 8), fg="#94a3b8", bg="#1e293b"
    )
    lbl_sub.pack(anchor='w', pady=(2, 0))

    # Body
    body = tk.Frame(root, bg="#0f172a", padx=20, pady=16)
    body.pack(fill='both', expand=True)

    lbl_step = tk.Label(
        body, text="Iniciando instalación...",
        font=("Segoe UI", 9, "bold"), fg="#38bdf8", bg="#0f172a"
    )
    lbl_step.pack(anchor='w')

    lbl_detail = tk.Label(
        body, text="Preparando archivos del sistema...",
        font=("Segoe UI", 8), fg="#94a3b8", bg="#0f172a", wraplength=450, justify='left'
    )
    lbl_detail.pack(anchor='w', pady=(4, 12))

    pbar = ttk.Progressbar(body, mode='determinate', maximum=100)
    pbar.pack(fill='x', pady=(0, 16))
    pbar['value'] = 10

    # Botón acción inferior
    btn_box = tk.Frame(body, bg="#0f172a")
    btn_box.pack(fill='x', side='bottom')

    btn_close = tk.Button(
        btn_box, text="Cerrar", font=("Segoe UI", 9),
        bg="#334155", fg="#94a3b8", state='disabled', relief='flat', padx=16, pady=4,
        command=root.destroy
    )
    btn_close.pack(side='right')

    def update_ui(step_text, detail_text, progress_val):
        lbl_step.config(text=step_text)
        lbl_detail.config(text=detail_text)
        pbar['value'] = progress_val

    def worker():
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        cur_pid = os.getpid()

        try:
            # Paso 1: Crear directorio
            root.after(0, lambda: update_ui("[1/4] Creando directorio de instalación...", f"Carpeta: {INSTALL_DIR}", 25))
            INSTALL_DIR.mkdir(parents=True, exist_ok=True)
            time.sleep(0.3)

            # Paso 2: Detener procesos previos y copiar ejecutable limpio
            root.after(0, lambda: update_ui("[2/4] Liberando archivos y copiando ejecutable...", "Deteniendo agentes previos y servicios activos...", 50))

            # Detener tarea programada si estaba en ejecución
            try:
                subprocess.run(['schtasks', '/end', '/tn', 'PrinterAgent_SolutionsDev'],
                               capture_output=True, creationflags=cflags)
            except Exception:
                pass

            # Terminar cualquier PrinterAgent.exe activo (excepto este instalador)
            try:
                subprocess.run(
                    ['taskkill', '/F', '/FI', f'PID ne {cur_pid}', '/IM', 'PrinterAgent.exe'],
                    capture_output=True, creationflags=cflags
                )
            except Exception:
                pass
            time.sleep(1.0)

            src_exe = Path(sys.executable)
            dst_exe = INSTALL_DIR / 'PrinterAgent.exe'

            try:
                same_file = src_exe.resolve() == dst_exe.resolve()
            except Exception:
                same_file = False

            if not same_file:
                # Extraer bytes limpios del ejecutable original
                with open(src_exe, 'rb') as f:
                    all_bytes = f.read()
                marker_pos = all_bytes.rfind(AGENT_CONFIG_MARKER)
                clean_bytes = all_bytes[:marker_pos] if marker_pos > 0 else all_bytes

                # Quitar atributos de sólo lectura si ya existía el archivo destino
                if dst_exe.exists():
                    try:
                        os.chmod(dst_exe, stat.S_IWRITE | stat.S_IREAD)
                    except Exception:
                        pass

                # Escritura segura con reintentos y rotación de archivo bloqueado
                written = False
                last_write_err = None
                for attempt in range(5):
                    try:
                        with open(dst_exe, 'wb') as f:
                            f.write(clean_bytes)
                        written = True
                        break
                    except PermissionError as pe:
                        last_write_err = pe
                        try:
                            # Si Windows bloquea sobrescritura, rotar el nombre del archivo
                            old_bak = INSTALL_DIR / f'PrinterAgent_old_{attempt}.tmp'
                            if dst_exe.exists():
                                os.replace(dst_exe, old_bak)
                                with open(dst_exe, 'wb') as f:
                                    f.write(clean_bytes)
                                written = True
                                try:
                                    old_bak.unlink()
                                except Exception:
                                    pass
                                break
                        except Exception:
                            pass
                        time.sleep(1.0)
                    except Exception as ex:
                        last_write_err = ex
                        time.sleep(1.0)

                if not written:
                    raise PermissionError(
                        f"No se pudo escribir en '{dst_exe}'.\n"
                        f"El archivo está bloqueado por el sistema o por una instancia activa.\n"
                        f"Detalle: {last_write_err}"
                    )

            # Paso 3: Guardar config.json
            root.after(0, lambda: update_ui("[3/4] Escribiendo configuración de la sede...", "Guardando credenciales y parámetros de telemetría...", 75))
            save_cfg = crypto_utils.protect_config(embedded_cfg) if crypto_utils else embedded_cfg
            for cfg_name in ['agent_config.json', 'config.json']:
                try:
                    with open(INSTALL_DIR / cfg_name, 'w', encoding='utf-8') as f:
                        json.dump(save_cfg, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass

            try:
                agent_cfg_path = Path.home() / '.printer_repair'
                agent_cfg_path.mkdir(exist_ok=True)
                for cfg_name in ['agent_config.json', 'config.json']:
                    with open(agent_cfg_path / cfg_name, 'w', encoding='utf-8') as f:
                        json.dump(save_cfg, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            time.sleep(0.3)

            # Paso 4: Registrar servicio y lanzar primera sincronización
            root.after(0, lambda: update_ui("[4/4] Registrando tarea e iniciando sincronización...", "Configurando tarea programada en Windows...", 90))

            target_exe = dst_exe if dst_exe.exists() else src_exe
            subprocess.run(
                [str(target_exe), '--install'],
                capture_output=True, text=True, errors='replace', creationflags=cflags
            )

            # Lanzar primera sincronización en segundo plano de forma no bloqueante
            try:
                subprocess.Popen([str(target_exe), '--run'], creationflags=cflags)
            except Exception:
                pass

            # Completado con éxito
            def on_success():
                lbl_step.config(text="✅ ¡Instalación Completada con Éxito!", fg="#22c55e")
                lbl_detail.config(
                    text=f"El Agente quedó instalado en {INSTALL_DIR}\n"
                         f"y reportará automáticamente al NOC cada {interval} minutos.",
                    fg="#f8fafc"
                )
                pbar['value'] = 100
                btn_close.config(text="Listo", bg="#16a34a", fg="white", state='normal')

            root.after(0, on_success)

        except Exception as e_err:
            def on_error(err=e_err):
                lbl_step.config(text="❌ Error durante la instalación", fg="#ef4444")
                lbl_detail.config(text=str(err), fg="#fca5a5")
                btn_close.config(text="Cerrar", bg="#ef4444", fg="white", state='normal')
                messagebox.showerror(
                    "Error de Instalación",
                    f"Ocurrió un error al instalar el agente:\n\n{err}",
                    parent=root
                )
            root.after(0, on_error)

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()
    sys.exit(0)


# ============================================================================
if __name__ == '__main__':
    if sys.platform == 'win32':
        try:
            import ctypes
            # Conectar salida con la consola que invocó el ejecutable (si existe)
            ctypes.windll.kernel32.AttachConsole(-1)
        except Exception:
            pass
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    # MATAR AGENTES VIEJOS ANTES DE CORRER PARA EVITAR CONFLICTOS DE OTA/PYTHON
    import subprocess
    _curr_pid = os.getpid()
    try:
        subprocess.run(f'taskkill /F /IM PrinterAgent.exe /FI "PID ne {_curr_pid}"', shell=True, capture_output=True)
        _r = subprocess.run('wmic process where "name=\'python.exe\' or name=\'pythonw.exe\'" get ProcessId,CommandLine', shell=True, capture_output=True, text=True, errors='replace')
        for _line in _r.stdout.splitlines():
            _line = _line.strip()
            if not _line or 'ProcessId' in _line:
                continue
            if 'printeragent' in _line.lower():
                _parts = _line.split()
                if _parts:
                    _pid_str = _parts[-1]
                    if _pid_str.isdigit():
                        _pid = int(_pid_str)
                        if _pid != _curr_pid:
                            subprocess.run(f'taskkill /F /PID {_pid}', shell=True, capture_output=True)
    except Exception:
        pass

    # ── PASO 0: Detectar si este .exe es un instalador auto-extraíble ──
    # Limpiar residuo .old de actualización previa si existe
    try:
        _cur_exe = Path(sys.executable)
        _old_exe = _cur_exe.with_suffix('.old')
        if _old_exe.exists():
            _old_exe.unlink()
    except Exception:
        pass

    _embedded = _check_embedded_config()
    if _embedded:
        _run_self_install(_embedded)
        # _run_self_install nunca retorna (sys.exit), pero por seguridad:
        sys.exit(0)

    # Mostrar versión por consola si se solicita
    if any(arg in sys.argv for arg in ('--version', '-v', '/version', '/v')):
        print(f"PrinterAgent v{AGENT_VERSION}")
        sys.exit(0)

    # Detectar si se pide modo de configuración visual
    is_gui_req = any(arg in sys.argv for arg in ('--config', '-c', '--gui', '--wizard'))

    # Cargar configuración para determinar si es un nodo de telemetría Multi-Sede
    curr_cfg = load_full_config()
    is_multisite = bool(
        curr_cfg.get('multisite_enabled')
        or curr_cfg.get('multisite', {}).get('enabled')
        or curr_cfg.get('multisite_token')
        or curr_cfg.get('multisite', {}).get('auth_token')
    )

    # 0. Verificación de Licencia PRO:
    # Si el agente está en modo Multi-Sede (cliente de flota/alquiler reportando al NOC),
    # se valida contra el servidor central mediante su Token de Sede.
    # No requiere una licencia comercial PRO local en la PC de cada cliente de alquiler.
    # Tampoco se bloquea el modo GUI (doble clic sin argumentos) para permitir configuración.
    if not is_multisite and '--uninstall' not in sys.argv and not is_gui_req and len(sys.argv) > 1:
        try:
            from license_manager import LicenseManager
            lic_data, lic_err = LicenseManager.validate_license()
            if lic_err or not lic_data or lic_data.get('license_type', '').upper() != 'PRO':
                msg = f"[BLOQUEO AGENTE] Se requiere licencia PRO activa. {lic_err or 'La versión DEMO no incluye el Agente de Monitoreo local.'}"
                log.warning(msg)
                print(msg)
                sys.exit(0)
        except Exception as ex_lic:
            log.warning(f"[AVISO LICENCIA AGENTE] {ex_lic}")

    # Evitar múltiples instancias concurrentes del ciclo de escaneo del agente
    skip_lock = (
        is_gui_req
        or '--install' in sys.argv
        or '--uninstall' in sys.argv
        or '--version' in sys.argv
        or '-v' in sys.argv
        or '--help' in sys.argv
        or '-h' in sys.argv
        or '--update' in sys.argv
        or '--force-update' in sys.argv
        or '/update' in sys.argv
    )
    if not skip_lock:
        try:
            import msvcrt, tempfile
            lock_path = Path(tempfile.gettempdir()) / "printer_agent_running.lock"
            _lock_file = open(lock_path, 'a')
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except Exception:
            sys.exit(0)

    if '--version' in sys.argv or '-v' in sys.argv:
        print(f"PrinterAgent v{AGENT_VERSION}")
        sys.exit(0)

    # Modo Asistente / Configuración Visual
    if is_gui_req:
        show_agent_config_gui()
        sys.exit(0)

    # Si se llama con --install, registra la tarea programada en Windows
    elif '--install' in sys.argv:
        exe = sys.executable if sys.executable.endswith('.exe') else \
              str(Path(sys.argv[0]).resolve())
        short_exe = get_windows_short_path(exe)
        curr_cfg = load_full_config()
        interval = int(curr_cfg.get('check_interval_minutes') or DEFAULT_CONFIG['check_interval_minutes'])
        if interval < 1:
            interval = 5
        target_tr = f'"{short_exe}" --run' if ' ' in short_exe else f'{short_exe} --run'
        cmds = [
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/ru', 'SYSTEM', '/f'],
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/rl', 'HIGHEST', '/f'],
            ['schtasks', '/create', '/tn', 'PrinterAgent_SolutionsDev', '/tr', target_tr, '/sc', 'MINUTE', '/mo', str(interval), '/f']
        ]
        ok = False
        last_err = ""
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        for cmd in cmds:
            r = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                               creationflags=cflags)
            if r.returncode == 0:
                ok = True
                break
            last_err = (r.stderr or r.stdout or "").strip()

        if ok:
            print("[OK] Tarea programada instalada exitosamente.")
            sys.exit(0)
        else:
            print(f"[ERROR] Error al instalar tarea: {last_err}")
            sys.exit(1)

    elif '--uninstall' in sys.argv:
        subprocess.run(['schtasks', '/delete', '/tn', 'PrinterAgent_SolutionsDev', '/f'],
                       capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
        print("[OK] Tarea programada eliminada.")

    elif '--show-report' in sys.argv or '--report-now' in sys.argv:
        ok, msg, path = generate_monthly_report_now(preview=True, send_mail=False)
        print(f"[{'OK' if ok else 'ERROR'}] {msg}")

    elif '--send-report' in sys.argv:
        ok, msg, path = generate_monthly_report_now(preview=False, send_mail=True)
        print(f"[{'OK' if ok else 'ERROR'}] {msg}")

    elif '--update' in sys.argv or '/update' in sys.argv or '--force-update' in sys.argv:
        ok, msg = check_and_apply_agent_auto_update(force=True, verbose=True)
        sys.exit(0 if ok else 1)

    elif len(sys.argv) == 1:
        # Si se hace doble clic directo (sin argumentos) desde el Explorador de Windows,
        # abrir siempre la interfaz gráfica del configurador para que el usuario interactúe.
        curr_cfg = load_full_config()
        show_agent_config_gui(curr_cfg)
        sys.exit(0)

    elif '--run' in sys.argv:
        run_agent()

