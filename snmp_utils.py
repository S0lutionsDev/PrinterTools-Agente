"""
snmp_utils.py — Módulo compartido de funciones de red y cliente SNMP mínimo
SolutionsDev · Printer Tools PRO v3

Provee:
- Resolución y parseo de subredes (get_local_subnet, parse_target_ips)
- OIDs estándar MIB y de fabricantes (Kyocera, HP, Ricoh, etc.)
- SNMPClient (cliente UDP ASN.1 con parser hacia adelante)
- scan_network_printers (escáner concurrente con ThreadPoolExecutor)
"""

import subprocess
import socket
import struct
import logging
import ipaddress
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import concurrent.futures

log = logging.getLogger('snmp_utils')

# ============================================================================
# RESOLUCIÓN Y PARSEO DE REDES
# ============================================================================
def get_local_subnet():
    """Detecta la subred local del equipo de forma resiliente."""
    # 1. Socket UDP saliente (determina la interfaz activa según el kernel)
    for target in [("8.8.8.8", 80), ("1.1.1.1", 80)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith('127.') and not ip.startswith('169.254.'):
                return ".".join(ip.split(".")[:-1]) + ".0/24"
        except Exception:
            pass

    # 2. Hostname local
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith('127.') and not host_ip.startswith('169.254.'):
            return ".".join(host_ip.split(".")[:-1]) + ".0/24"
    except Exception:
        pass

    # 3. Enumeración de interfaces de red activas en Windows
    try:
        ps_cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias 'Ethernet*','Wi-Fi*','Local*' | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress"
        cflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        r = subprocess.run(['powershell', '-NoProfile', '-Command', ps_cmd], capture_output=True, text=True, timeout=2, creationflags=cflags)
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        for cand_ip in lines:
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', cand_ip):
                return ".".join(cand_ip.split(".")[:-1]) + ".0/24"
    except Exception:
        pass

    return "192.168.1.0/24"


def parse_target_ips(network_range):
    """
    Parsea rangos en múltiples formatos:
    - 'none', 'disabled', 'usb_only', 'solo_usb': escaneo desactivado (retorna [])
    - 'auto' o vacío: subred local del equipo (ej. 192.168.1.0/24)
    - CIDR: '192.168.1.0/24' o '10.0.0.0/24'
    - Prefijo: '192.168.1' (se expande a .1 - .254)
    - Rango: '192.168.1.10-50' o '192.168.1.1-254'
    - IP única: '192.168.1.100'
    - Múltiples separados por coma: '192.168.1.0/24, 192.168.2.0/24'
    """
    nr = (network_range or '').strip()
    if nr.lower() in ('none', 'disabled', 'off', 'usb_only', 'solo_usb', 'usb', 'no'):
        return []
    if not nr or nr.lower() == 'auto':
        nr = get_local_subnet()

    tokens = [t.strip() for t in nr.split(',') if t.strip()]
    ips = []
    for token in tokens:
        try:
            if "/" in token:
                net = ipaddress.ip_network(token, strict=False)
                ips.extend(str(h) for h in net.hosts())
            elif "-" in token:
                base, rng = token.rsplit(".", 1)
                st, en = map(int, rng.split("-"))
                ips.extend(f"{base}.{i}" for i in range(st, en + 1))
            elif re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}$', token):
                ips.extend(f"{token}.{i}" for i in range(1, 255))
            else:
                ipaddress.ip_address(token)
                ips.append(token)
        except Exception as e:
            log.warning(f"Error parseando rango de red '{token}': {e}")
    # Retornar lista ordenada y sin duplicados
    return list(dict.fromkeys(ips))


# ============================================================================
# OIDs SNMP ESTÁNDAR Y POR FABRICANTE (Kyocera, HP, Ricoh, Brother, etc.)
# ============================================================================
OID_PRINTER_STATUS      = '1.3.6.1.2.1.25.3.5.1.1.1'       # estado general
OID_PAGE_COUNT          = '1.3.6.1.2.1.43.10.2.1.4.1.1'     # contador total estándar (prtMarkerLifeCount)
OID_KYOCERA_TOTAL_COUNT = '1.3.6.1.4.1.1347.43.10.1.1.12.1.1' # Kyocera contador total de páginas impresas (Panel / Informe de Estado)
OID_KYOCERA_COUNT_1     = '1.3.6.1.4.1.1347.42.3.1.1.1.1.1' # Kyocera impresiones
OID_KYOCERA_COUNT_2     = '1.3.6.1.4.1.1347.42.3.1.1.1.1.2' # Kyocera copias
OID_KYOCERA_COUNT_FAX   = '1.3.6.1.4.1.1347.42.3.1.1.1.1.3' # Kyocera fax
OID_SERIAL_1            = '1.3.6.1.2.1.43.5.1.1.17.1'       # serial estándar 1
OID_SERIAL_0            = '1.3.6.1.2.1.43.5.1.1.17.0'       # serial estándar 0
OID_KYOCERA_SERIAL      = '1.3.6.1.4.1.1347.43.5.1.1.28.1'  # serial Kyocera
OID_DEVICE_MODEL        = '1.3.6.1.2.1.25.3.2.1.3.1'        # hrDeviceDescr.1
OID_KYOCERA_MODEL       = '1.3.6.1.4.1.1347.40.35.1.1.2.1'  # Kyocera modelo
OID_SYS_DESCR           = '1.3.6.1.2.1.1.1.0'               # sysDescr
OID_SYS_NAME            = '1.3.6.1.2.1.1.5.0'               # sysName (RFC 1213)
OID_SUPPLY_TYPE_BASE    = '1.3.6.1.2.1.43.11.1.1.5.1.'      # tipo consumible RFC 3805 (15=tinta, 16=wasteInk, 3=toner)
OID_TONER_DESC_BASE     = '1.3.6.1.2.1.43.11.1.1.6.1.'      # descripción consumible (RFC 3805)
OID_TONER_MAX_BASE      = '1.3.6.1.2.1.43.11.1.1.8.1.'      # capacidad máx tóner/tinta (RFC 3805)
OID_TONER_CUR_BASE      = '1.3.6.1.2.1.43.11.1.1.9.1.'      # nivel actual tóner/tinta (RFC 3805)
OID_JAM_COUNT           = '1.3.6.1.2.1.43.18.1.1.8.1.1'     # atascos

TONER_SLOTS = {1: 'Negro', 2: 'Cian', 3: 'Magenta', 4: 'Amarillo'}


# ============================================================================
# SNMP MÍNIMO (UDP RAW sin dependencias externas — Decodificador ASN.1)
# ============================================================================
class SNMPClient:
    """
    Cliente SNMP v1 GET vía sockets UDP con decodificación ASN.1 directa.
    No requiere librerías externas ni ejecutables auxiliares.
    """
    def __init__(self, host, community='public', port=161, timeout=0.8):
        self.host      = host
        self.community = community.encode('ascii', errors='ignore')
        self.port      = port
        self.timeout   = timeout

    def _encode_oid(self, oid_str):
        parts = [int(x) for x in oid_str.strip('.').split('.')]
        encoded = bytes([40 * parts[0] + parts[1]])
        for p in parts[2:]:
            if p < 128:
                encoded += bytes([p])
            else:
                segs = []
                while p:
                    segs.append(p & 0x7F)
                    p >>= 7
                segs.reverse()
                for i, seg in enumerate(segs):
                    encoded += bytes([seg | (0x80 if i < len(segs)-1 else 0)])
        return encoded

    def _build_get(self, oid_str):
        oid_enc = self._encode_oid(oid_str)
        oid_tlv = b'\x06' + bytes([len(oid_enc)]) + oid_enc
        varbind = b'\x30' + bytes([len(oid_tlv) + 2]) + oid_tlv + b'\x05\x00'
        varbind_list = b'\x30' + bytes([len(varbind)]) + varbind

        req_id = b'\x02\x04\x12\x34\x56\x78'
        err    = b'\x02\x01\x00'
        err_ix = b'\x02\x01\x00'
        pdu_body = req_id + err + err_ix + varbind_list
        pdu = b'\xa0' + bytes([len(pdu_body)]) + pdu_body

        comm_tlv = b'\x04' + bytes([len(self.community)]) + self.community
        ver_tlv  = b'\x02\x01\x00'  # v1
        msg_body = ver_tlv + comm_tlv + pdu
        return b'\x30' + bytes([len(msg_body)]) + msg_body

    def _read_length(self, data, offset):
        """Decodifica longitud ASN.1 BER (short-form <128 y long-form >=128).
        Retorna (length, data_start_offset)."""
        if offset >= len(data):
            return 0, offset
        first = data[offset]
        if first < 128:
            return first, offset + 1
        num_bytes = first & 0x7F
        if num_bytes == 0 or offset + 1 + num_bytes > len(data):
            return 0, offset + 1
        length = 0
        for b in data[offset + 1: offset + 1 + num_bytes]:
            length = (length << 8) | b
        return length, offset + 1 + num_bytes

    def _parse_response(self, response, as_raw_bytes: bool = False):
        """
        Parser hacia adelante para decodificar PDUs SNMP ASN.1 BER.
        Compatible con respuestas estándar y paquetes extendidos de Kyocera, HP, Ricoh, etc.
        Maneja correctamente respuestas NULL, noSuchObject, noSuchInstance y genError.
        """
        if not response or len(response) < 15:
            return None

        # Localizar el inicio del PDU GetResponse (0xa2) y avanzar hasta el VarBind
        pdu_idx = response.find(b'\xa2')
        if pdu_idx == -1:
            return None

        # Verificar error-status en el PDU (byte después del request-id)
        # Estructura: GetResponse → request-id(INT) → error-status(INT) → error-index(INT) → varbindlist
        j = pdu_idx + 2  # saltar tag + length del PDU
        # Saltar request-id
        if j < len(response) and response[j] == 0x02:
            rid_len = response[j + 1]
            j += 2 + rid_len
        # Leer error-status
        if j + 2 < len(response) and response[j] == 0x02:
            err_len = response[j + 1]
            if err_len == 1 and response[j + 2] != 0:
                # Error SNMP (genError=5, noSuchName=2, etc.) — OID no soportado
                return None

        # Localizar el OID del VarBind y posicionarse después para leer el valor
        oid_idx = response.find(b'\x06', pdu_idx)
        if oid_idx != -1 and oid_idx + 1 < len(response):
            oid_len = response[oid_idx + 1]
            i = oid_idx + 2 + oid_len
        else:
            return None

        # Leer el valor inmediatamente después del OID
        if i >= len(response):
            return None

        vtype = response[i]

        # NULL (0x05) — OID existe pero sin valor asignado
        if vtype == 0x05:
            return None

        # Errores SNMP v2c: noSuchObject(0x80), noSuchInstance(0x81), endOfMibView(0x82)
        if vtype in (0x80, 0x81, 0x82):
            return None

        # Integer, Counter32, Gauge32, TimeTicks, Counter64
        if vtype in (0x02, 0x41, 0x42, 0x43, 0x46):
            length, data_start = self._read_length(response, i + 1)
            if data_start + length <= len(response):
                val = 0
                for b in response[data_start:data_start + length]:
                    val = (val << 8) | b
                return val

        # OctetString
        if vtype == 0x04:
            length, data_start = self._read_length(response, i + 1)
            if data_start + length <= len(response):
                if as_raw_bytes:
                    return response[data_start:data_start + length]
                return response[data_start:data_start + length].decode('utf-8', errors='replace').strip('\x00')

        return None

    def get(self, oid_str):
        """Envía solicitud SNMP GET y retorna el valor decodificado vía _parse_response."""
        sock = None
        try:
            pkt = self._build_get(oid_str)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            sock.sendto(pkt, (self.host, self.port))
            data, _ = sock.recvfrom(4096)
            return self._parse_response(data)
        except socket.timeout:
            return None
        except Exception as e:
            log.debug(f"SNMP GET {self.host} OID {oid_str}: {e}")
            return None
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def get_value(self, oid_str):
        """Devuelve tupla (tipo, valor): ('INT', int), ('STR', str) o ('ERR', None)."""
        val = self.get(oid_str)
        if isinstance(val, int):
            return ('INT', val)
        elif isinstance(val, str):
            return ('STR', val)
        return ('ERR', None)

    def get_int(self, oid_str, default=None):
        t, v = self.get_value(oid_str)
        return v if t == 'INT' else default

    def get_str(self, oid_str, default=''):
        t, v = self.get_value(oid_str)
        if t == 'STR' and v:
            return str(v)
        if t == 'INT' and v is not None:
            return str(v)
        return default

    def get_page_counter(self):
        """
        Obtiene el contador total de páginas de la impresora.
        En Kyocera, prioriza el contador total oficial del panel/informe (kcpmTotalPageCounter)
        que incluye copias, impresiones, fax y reportes para coincidir 100% con la pantalla física.
        Fallback a la suma de desgloses privados (Impresiones + Copias + Fax + Reportes),
        y fallback final a MIB RFC 3805 (prtMarkerLifeCount) para Epson, HP, Ricoh, Brother, etc.
        """
        # 1. Kyocera: Suma de desgloses de trabajo reales (Impresiones + Copias + FAX + Reportes)
        # En multifuncionales Kyocera (M3550idn, M3655idn, TASKalfa), el contador total oficial físico
        # incluye obligatoriamente las páginas procesadas por el motor de FAX.
        ky_print = self.get_int(OID_KYOCERA_COUNT_1)
        ky_copy  = self.get_int(OID_KYOCERA_COUNT_2)
        ky_fax   = self.get_int(OID_KYOCERA_COUNT_FAX)
        ky_rep   = self.get_int('1.3.6.1.4.1.1347.42.3.1.1.1.1.4')
        ky_tot   = self.get_int(OID_KYOCERA_TOTAL_COUNT)

        if ky_print is not None or ky_copy is not None or (ky_fax is not None and ky_fax > 0):
            tot_ky = (ky_print or 0) + (ky_copy or 0) + (ky_fax or 0) + (ky_rep or 0)
            # Si el contador del motor (ky_tot) es mayor que print+copy, asegurar inclusión del FAX
            if ky_tot and ky_tot > ((ky_print or 0) + (ky_copy or 0)):
                tot_ky = max(tot_ky, ky_tot + (ky_fax or 0))
            if tot_ky > 0:
                return tot_ky

        # 2. Kyocera: Contador total de páginas del motor (fallback si el modelo no expone desgloses)
        if ky_tot and ky_tot > 0:
            return ky_tot

        # 3. Estándar MIB RFC 3805 (prtMarkerLifeCount) para Epson, HP, Ricoh, Brother, etc.
        cnt = self.get_int(OID_PAGE_COUNT)
        if cnt and cnt > 0:
            return cnt

        return 0

    def get_serial(self):
        """Obtiene el número de serie de la impresora."""
        for oid in (OID_SERIAL_1, OID_SERIAL_0, OID_KYOCERA_SERIAL):
            t, v = self.get_value(oid)
            if t == 'STR' and v and v.strip() not in ('0', 'ERR', 'No Such', 'None'):
                return v.strip()
            if t == 'INT' and v:
                return str(v)
        return ''

    def get_sys_name(self):
        """Obtiene el nombre de host de red de la impresora (RFC 1213 sysName)."""
        val = self.get_str(OID_SYS_NAME, '')
        return val.strip() if val else ''

    def get_mac_address(self):
        """
        Obtiene la dirección MAC física de la impresora vía MIB-II ifPhysAddress (RFC 1213 / RFC 2863).
        Permite identificar unívocamente el equipo incluso a través de routers y VLANs.
        """
        sock = None
        for idx in (1, 2, 3):
            oid_str = f"1.3.6.1.2.1.2.2.1.6.{idx}"
            try:
                pkt = self._build_get(oid_str)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(self.timeout)
                sock.sendto(pkt, (self.host, self.port))
                data, _ = sock.recvfrom(4096)
                raw_bytes = self._parse_response(data, as_raw_bytes=True)
                if raw_bytes and isinstance(raw_bytes, (bytes, bytearray)) and len(raw_bytes) == 6:
                    if any(b != 0 for b in raw_bytes):
                        return ":".join(f"{b:02X}" for b in raw_bytes)
            except Exception:
                pass
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
        return ''

    def get_model(self):
        """Obtiene marca y modelo del equipo vía SNMP."""
        for oid in (OID_DEVICE_MODEL, OID_KYOCERA_MODEL, OID_SYS_DESCR):
            m = self.get_str(oid)
            if m and len(m) >= 2 and m.lower() not in ('public', '0', 'err', 'unknown'):
                m = m.replace('\r', ' ').replace('\n', ' ').strip()
                m_low = m.lower()
                # Detección inteligente de marcas por nombres comerciales y familias
                brand_map = {
                    'Kyocera': ['kyocera', 'ecosys', 'taskalfa', 'km-'],
                    'HP': ['hp', 'laserjet', 'deskjet', 'pagewide', 'officejet', 'color laserjet'],
                    'Brother': ['brother', 'dcp-', 'mfc-', 'hl-'],
                    'Epson': ['epson', 'workforce', 'ecotank', 'stylus'],
                    'Ricoh': ['ricoh', 'aficio', 'sp c', 'mp c'],
                    'Lexmark': ['lexmark', 'optra'],
                    'Samsung': ['samsung', 'xpress', 'proxpress'],
                    'Xerox': ['xerox', 'phaser', 'versalink', 'altalink', 'workcentre'],
                    'Canon': ['canon', 'imageclass', 'imagerunner', 'pixma', 'lbp', 'ir-adv'],
                }
                for brand, keywords in brand_map.items():
                    if any(kw in m_low for kw in keywords):
                        if not m_low.startswith(brand.lower()):
                            m = f"{brand} {m}".strip()
                        break
                return m
        return ''

    def get_supplies(self):
        """
        Consulta universal y genérica de consumibles (tóner, tanques de tinta y almohadillas)
        según el estándar RFC 3805 (prtMarkerSuppliesTable) compatible con todas las marcas:
        HP, Brother, Epson (EcoTank), Kyocera, Canon (MegaTank), Ricoh, Lexmark, Xerox, Samsung.
        Retorna dict con porcentajes, valores actuales, máximos, tipo ('toner'/'ink'/'maintenance_box') y nombres.
        """
        raw_supplies = []
        for idx in range(1, 9):
            desc = self.get_str(f'{OID_TONER_DESC_BASE}{idx}', '')
            max_v = self.get_int(f'{OID_TONER_MAX_BASE}{idx}')
            cur_v = self.get_int(f'{OID_TONER_CUR_BASE}{idx}')
            stype = self.get_int(f'{OID_SUPPLY_TYPE_BASE}{idx}', 0)
            if desc or (max_v is not None and max_v > 0) or (cur_v is not None and cur_v >= 0):
                raw_supplies.append({'idx': idx, 'desc': desc.strip(), 'max': max_v, 'cur': cur_v, 'stype': stype})

        # Separar consumibles de impresión vs depósitos de residuo / mantenimiento
        supplies_print = []
        waste_supplies = []

        for s in raw_supplies:
            d_low = (s['desc'] or '').lower()
            is_waste = any(w in d_low for w in ['waste', 'residual', 'almohadilla', 'maintenance', 'caja de']) or s.get('stype') in (4, 16)
            is_mech = any(w in d_low for w in ['drum', 'tambor', 'opc', 'fuser', 'fusor', 'belt', 'transfer'])
            if is_waste:
                waste_supplies.append(s)
            elif not is_mech:
                supplies_print.append(s)

        is_monochrome = len(supplies_print) == 1
        toners = {}

        # Determinar si la impresora es de tinta por tipo RFC o descripción
        is_ink_device = any(
            s.get('stype') in (15, 17) or any(w in (s['desc'] or '').lower() for w in ['ink', 'tinta'])
            for s in supplies_print
        )

        for s in supplies_print:
            d_low = (s['desc'] or '').lower()
            max_v = s['max']
            cur_v = s['cur']

            d_raw = s['desc'] or ''
            d_low = d_raw.lower().strip()
            # Separar palabras y números para que códigos como 'TK-5242C' o 'TN-227C' generen tokens individuales ['tk', '5242', 'c']
            tokens = set(re.findall(r'[a-z]+|[0-9]+', d_low))

            # Colores fotográficos y específicos primero
            if any(p in d_low for p in ['light cyan', 'photo cyan', 'cian claro', 'photo-c', 'lc']):
                color = 'Cian Claro'
            elif any(p in d_low for p in ['light magenta', 'photo magenta', 'magenta claro', 'photo-m', 'lm']):
                color = 'Magenta Claro'
            elif any(p in d_low for p in ['photo black', 'negro foto', 'photo-k', 'pbk']):
                color = 'Negro Foto'
            elif any(p in d_low for p in ['gray', 'grey', 'gris']):
                color = 'Gris'
            # Cian / Cyan (verificar ANTES de negro para que modelos con sufijo 'c' como TK-5242C o 'c' aislado nunca caigan en fallback)
            elif (
                tokens.intersection({'cyan', 'cian', 'bleu', 'blau', 'c'})
                or any(p in d_low for p in ['cyan', 'cian', 'bleu', 'blau'])
                or bool(re.search(r'[-_\s\d]c\b', d_low))
                or d_low.endswith('c')
            ):
                color = 'Cian'
            # Magenta
            elif (
                tokens.intersection({'magenta', 'rot', 'm'})
                or any(p in d_low for p in ['magenta', 'rot'])
                or bool(re.search(r'[-_\s\d]m\b', d_low))
                or d_low.endswith('m')
            ):
                color = 'Magenta'
            # Amarillo / Yellow
            elif (
                tokens.intersection({'yellow', 'amarillo', 'gelb', 'jaune', 'y'})
                or any(p in d_low for p in ['yellow', 'amarillo', 'gelb'])
                or bool(re.search(r'[-_\s\d]y\b', d_low))
                or d_low.endswith('y')
            ):
                color = 'Amarillo'
            # Negro / Black (solo token 'k' exacto o palabras completas, NUNCA subcadena dentro de 'ink')
            elif (
                tokens.intersection({'black', 'negro', 'noir', 'schwarz', 'k', 'bk', 'blk'})
                or any(p in d_low for p in ['black', 'negro', 'noir', 'schwarz'])
                or bool(re.search(r'[-_\s\d](k|bk)\b', d_low))
                or d_low.endswith('k')
                or (is_monochrome and s['idx'] == 1)
            ):
                color = 'Negro'
            elif is_monochrome:
                color = 'Negro'
            else:
                idx_colors = {1: 'Negro', 2: 'Cian', 3: 'Magenta', 4: 'Amarillo'}
                color = idx_colors.get(s['idx'], f"Consumible {s['idx']}")

            is_discrete = False
            if (max_v == 254 and cur_v == 254):
                # Código discreto Kyocera / RFC 3805: Sensor de presencia OK (sin porcentaje analógico en tolva)
                pct = 100
                display_text = "OK"
                is_discrete = True
            elif max_v and max_v > 0 and cur_v is not None and cur_v >= 0:
                pct = max(0, min(100, round(cur_v * 100 / max_v)))
                display_text = f"{pct}%"
                is_discrete = False
            elif cur_v == -3:
                pct = 100  # RFC 3805: nivel operativo normal OK
                display_text = "OK"
                is_discrete = True
            elif cur_v == -2:
                pct = 0
                display_text = "Desconocido"
                is_discrete = True
            else:
                pct = 0
                display_text = "0% (Agotado)"
                is_discrete = False

            supply_type = 'ink' if (is_ink_device or s.get('stype') in (15, 17) or 'ink' in d_low or 'tinta' in d_low) else 'toner'
            
            # Etiqueta limpia: si la descripción solo repite el color/tipo (ej. "Yellow Ink Supply"), mostrar el color.
            # Si contiene número de parte o modelo específico (ej. "TK-3102", "T9481"), incluirlo entre paréntesis.
            has_part_number = bool(re.search(r'\d+', d_raw))
            if has_part_number:
                label = f"{color} ({d_raw})"
            else:
                label = color

            toners[color] = {
                'pct': pct,
                'cur': cur_v or 0,
                'max': max_v or 100,
                'desc': s['desc'] or color,
                'label': label,
                'type': supply_type,
                'is_discrete': is_discrete,
                'display_text': display_text
            }

        # Procesar Almohadilla Residual / Caja de Mantenimiento
        # SOLO para impresoras de tinta (inkjet). Los equipos láser no tienen
        # almohadillas; el stype=4 en láser corresponde a contenedores internos
        # de tóner residual que no son relevantes para el usuario.
        if waste_supplies and is_ink_device:
            ws = waste_supplies[0]
            w_max = ws['max']
            w_cur = ws['cur']
            w_pct = 0
            if w_max and w_max > 0 and w_cur is not None and w_cur >= 0:
                w_pct = max(0, min(100, round(w_cur * 100 / w_max)))
            toners['Almohadilla (Waste Ink)'] = {
                'pct': w_pct,
                'cur': w_cur or 0,
                'max': w_max or 100,
                'desc': ws['desc'] or 'Almohadilla de Tinta Residual',
                'label': 'Almohadilla (Waste Ink)',
                'type': 'maintenance_box'
            }

        return toners

    def get_operational_status(self):
        """
        Consulta estado de hardware y pantalla LCD de la impresora.
        Retorna (is_ok: bool, error_type: str, detail_msg: str).
        error_type puede ser: 'jam', 'door_open', 'no_paper', 'no_toner', 'service', 'other' o ''.
        """
        # 1. LCD Console Display Buffer (RFC 3805 / Kyocera / HP / Ricoh)
        raw_console = (self.get_str('1.3.6.1.2.1.43.16.5.1.2.1.1') or '').strip()
        console = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', raw_console).strip()
        c_low = console.lower()

        # 2. Detección de estados normales / benignos (NUNCA deben considerarse fallas)
        benign_terms = (
            'preparad', 'list', 'ready', 'en line', 'online', 'sleep', 'repos',
            'ahorro', 'bajo consumo', 'modo de reposo', 'energy saver', 'powersave',
            'imprim', 'print', 'proces', 'copi', 'standby', 'espera', 'ok',
            'calent', 'warming', 'auto', 'cassette', 'bandeja', 'operativ'
        )
        is_screen_benign = any(b in c_low for b in benign_terms)

        # 3. hrPrinterDetectedErrorState (RFC 2790)
        err_str = self.get_str('1.3.6.1.2.1.25.3.5.1.2.1')
        err_byte = ord(err_str[0]) if err_str and len(err_str) > 0 else 0

        # Atasco de papel: bit 5 (0x04) o texto explícito en pantalla
        is_jam = (err_byte & 0x04) != 0 or any(w in c_low for w in ['jam', 'atasco', 'traba', 'bourrage', 'papierstau'])
        if is_jam:
            msg = console if any(w in c_low for w in ['jam', 'atasco', 'traba']) else "Atasco de papel"
            return False, 'jam', msg

        # Puerta/tapa abierta: bit 4 (0x08) o texto en pantalla
        is_door = (err_byte & 0x08) != 0 or any(w in c_low for w in ['door open', 'puerta abierta', 'tapa abierta', 'cover open'])
        if is_door and not is_screen_benign:
            msg = console if any(w in c_low for w in ['door', 'puerta', 'tapa', 'cover']) else "Puerta o tapa abierta"
            return False, 'door_open', msg

        # Bandeja sin papel: bit 1 (0x40) o texto en pantalla
        is_no_paper = (err_byte & 0x40) != 0 or any(w in c_low for w in ['no paper', 'sin papel', 'load paper', 'cargar papel', 'paper empty'])
        if is_no_paper and not is_screen_benign:
            msg = console if any(w in c_low for w in ['paper', 'papel']) else "Bandeja sin papel"
            return False, 'no_paper', msg

        # Sin tóner: bit 3 (0x10)
        is_no_toner = (err_byte & 0x10) != 0 or any(w in c_low for w in ['no toner', 'sin toner', 'replace toner', 'cambiar toner'])
        if is_no_toner and not is_screen_benign:
            msg = console if any(w in c_low for w in ['toner', 'tóner']) else "Sin tóner"
            return False, 'no_toner', msg

        # Si la pantalla reporta un estado benigno (ej. "Preparado.", "Preparada", "Listo", etc.)
        if is_screen_benign:
            return True, '', ''

        # Mensajes adicionales de alerta o error explícito en pantalla LCD
        if console:
            # Solo alertar si contiene palabras inequívocas de problema o avería
            is_explicit_error = any(w in c_low for w in [
                'error', 'falla', 'fallo', 'averia', 'avería', 'fault', 'service',
                'servicio', 'llamar', 'call', 'solicitar', 'atencion', 'atención',
                'maintenance', 'mantenimiento', 'no original', 'non-genuine'
            ])
            if is_explicit_error:
                return False, 'other', console

        return True, '', ''

    def get_paper_trays(self) -> list:
        """
        Consulta el estado de las bandejas/casetes de papel según RFC 3805 (prtInputTable).
        Retorna lista de dicts: [{'name': 'Bandeja 1', 'media': 'A4', 'status': 'OK', 'is_empty': False}, ...]
        """
        trays = []
        for idx in range(1, 5):
            desc = self.get_str(f'1.3.6.1.2.1.43.8.2.1.13.1.{idx}', '')
            media = self.get_str(f'1.3.6.1.2.1.43.8.2.1.12.1.{idx}', '')
            cur_l = self.get_int(f'1.3.6.1.2.1.43.8.2.1.10.1.{idx}')
            if not desc and cur_l is None and not media:
                continue

            t_name = desc.strip() if desc else f"Bandeja {idx}"
            media_name = media.strip() if media else "A4"
            is_empty = (cur_l == 0)
            if is_empty:
                status_txt = "Vacía"
            elif cur_l == -3:
                status_txt = "Llena"
            elif cur_l == -1:
                status_txt = "Con papel"
            elif cur_l is not None and cur_l > 0:
                status_txt = f"{cur_l} págs"
            else:
                status_txt = "OK"

            trays.append({
                'tray_index': idx,
                'name': t_name,
                'media': media_name,
                'status': status_txt,
                'is_empty': is_empty
            })
        return trays


class SNMPv2Client(SNMPClient):
    """
    Cliente SNMP v2c (hereda de SNMPClient pero especifica versión SNMPv2c 0x01 en la PDU).
    Permite aprovechar contadores Counter64 y respuesta extendida sin modificar SNMPClient v1.
    """
    def _build_get(self, oid_str):
        oid_enc = self._encode_oid(oid_str)
        oid_tlv = b'\x06' + bytes([len(oid_enc)]) + oid_enc
        varbind = b'\x30' + bytes([len(oid_tlv) + 2]) + oid_tlv + b'\x05\x00'
        varbind_list = b'\x30' + bytes([len(varbind)]) + varbind

        req_id = b'\x02\x04\x12\x34\x56\x78'
        err    = b'\x02\x01\x00'
        err_ix = b'\x02\x01\x00'
        pdu_body = req_id + err + err_ix + varbind_list
        pdu = b'\xa0' + bytes([len(pdu_body)]) + pdu_body

        comm_tlv = b'\x04' + bytes([len(self.community)]) + self.community
        ver_tlv  = b'\x02\x01\x01'  # v2c: versión 1 en ASN.1 INTEGER
        msg_body = ver_tlv + comm_tlv + pdu
        return b'\x30' + bytes([len(msg_body)]) + msg_body


class SNMPAutoClient:
    """
    Wrapper inteligente que intenta SNMP v2c primero y conmuta de forma transparente
    a SNMP v1 si el dispositivo no soporta v2c.
    """
    def __init__(self, host, community='public', port=161, timeout=0.8):
        self.v2 = SNMPv2Client(host, community, port, timeout)
        self.v1 = SNMPClient(host, community, port, timeout)
        self._preferred = None

    def get(self, oid_str):
        if self._preferred == 'v1':
            return self.v1.get(oid_str)
        elif self._preferred == 'v2':
            val = self.v2.get(oid_str)
            return val if val is not None else self.v1.get(oid_str)

        val = self.v2.get(oid_str)
        if val is not None:
            self._preferred = 'v2'
            return val
        val = self.v1.get(oid_str)
        if val is not None:
            self._preferred = 'v1'
        return val

    def get_serial(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_serial() or self.v1.get_serial()

    def get_page_counter(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_page_counter() or self.v1.get_page_counter()

    def get_model(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_model() or self.v1.get_model()

    def get_supplies(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_supplies() or self.v1.get_supplies()

    def get_sys_name(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_sys_name() or self.v1.get_sys_name()

    def get_operational_status(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_operational_status()

    def get_paper_trays(self):
        target = self.v2 if self._preferred == 'v2' else self.v1
        return target.get_paper_trays()


def get_arp_table_ips():
    """
    Obtiene las direcciones IP activas registradas en la tabla ARP de Windows.
    Permite priorizar en el escaneo de red aquellas IPs que ya han tenido tráfico local.
    """
    arp_ips = []
    try:
        res = subprocess.run(['arp', '-a'], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            m = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', line)
            if m:
                ip = m.group(1)
                # Excluir broadcasts, multicasts y direcciones no utilizables
                if not (ip.endswith('.255') or ip.startswith('224.') or ip.startswith('239.') or ip.endswith('.0')):
                    if ip not in arp_ips:
                        arp_ips.append(ip)
    except Exception:
        pass
    return arp_ips


def get_arp_table_map():
    """
    Retorna un diccionario {ip: mac} obtenido de la tabla ARP de Windows.
    Permite vincular cada IP con su dirección MAC física permanente para evitar duplicados.
    """
    arp_map = {}
    try:
        res = subprocess.run(['arp', '-a'], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})\s+([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})', line)
            if m:
                ip = m.group(1)
                mac = m.group(2).replace('-', ':').lower()
                if not (ip.endswith('.255') or ip.startswith('224.') or ip.startswith('239.') or ip.endswith('.0')):
                    arp_map[ip] = mac
    except Exception:
        pass
    return arp_map


def resolve_printer_hostname(ip, snmp_sysname=''):
    """
    Determina el nombre de host de la impresora con fallback múltiple:
    1. sysName SNMP (RFC 1213)
    2. NetBIOS Node Status (UDP 137)
    3. DNS inverso / FQDN (socket.gethostbyaddr)
    """
    if snmp_sysname and snmp_sysname.strip():
        return snmp_sysname.strip()

    # Fallback 1: Consulta directa NetBIOS (UDP 137)
    try:
        pkt = (
            b'\x12\x34\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00'
            b'\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00'
            b'\x00\x21\x00\x01'
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.sendto(pkt, (ip, 137))
        data, _ = s.recvfrom(1024)
        s.close()
        if len(data) > 56:
            num_names = data[56]
            offset = 57
            for _ in range(num_names):
                if offset + 18 <= len(data):
                    nb_name = data[offset:offset+15].decode('ascii', errors='ignore').strip()
                    if nb_name and not nb_name.startswith('IS~') and not nb_name.startswith('\x01') and not nb_name.startswith('\x02'):
                        return nb_name
                offset += 18
    except Exception:
        pass

    # Fallback 2: Reverse DNS
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        if host and host != ip:
            return host.split('.')[0]
    except Exception:
        pass

    return ''


# ============================================================================
# SCANNER DE RED COMPLETA (Detecta instaladas y NO instaladas)
# ============================================================================
def scan_network_printers(network_range, community='public', timeout=0.6, snmp_port=161, priority_ips=None):
    """
    Escanea la subred buscando cualquier impresora de red que responda SNMP,
    esté o no instalada en la PC local.
    Prioriza las IPs indicadas y las presentes en la tabla ARP para detección inmediata.
    Retorna lista de dicts con modelo, serie, contador total y estado de tóner.
    """
    ips = parse_target_ips(network_range)
    arp_ips = get_arp_table_ips()
    arp_map = get_arp_table_map()

    # Reordenar: priority_ips -> arp_ips -> resto de la subred (sin descartar ninguna IP)
    ordered_ips = []
    seen = set()

    if priority_ips:
        for pip in priority_ips:
            pip = pip.strip()
            if pip and pip not in seen:
                try:
                    ipaddress.ip_address(pip)
                    ordered_ips.append(pip)
                    seen.add(pip)
                except ValueError:
                    pass

    for aip in arp_ips:
        if aip in ips and aip not in seen:
            ordered_ips.append(aip)
            seen.add(aip)

    for ip in ips:
        if ip not in seen:
            ordered_ips.append(ip)
            seen.add(ip)

    ips = ordered_ips

    log.info(f"Escaneando {len(ips)} IPs en red buscando impresoras (priorizadas {len(seen)} activas/ARP)...")
    found = []

    import threading
    lock = threading.Lock()

    def check_host(ip):
        client = SNMPClient(ip, community, snmp_port, timeout)
        # Sonda rápida inicial: si el host no responde a device_model ni sysDescr ni kyocera_model,
        # descartar de inmediato para no acumular timeouts innecesarios en hosts apagados
        probe = client.get_str(OID_DEVICE_MODEL) or client.get_str(OID_SYS_DESCR)
        if not probe:
            probe = client.get_str(OID_KYOCERA_MODEL)
            if not probe:
                return

        model = client.get_model() or probe
        page_count = client.get_page_counter()
        serial = client.get_serial()
        sys_name = client.get_sys_name()
        hostname = resolve_printer_hostname(ip, sys_name)
        mac = arp_map.get(ip, '')
        if not mac:
            mac = client.get_mac_address()
        clean_s = (serial or '').strip()
        device_id = clean_s if (clean_s and clean_s not in ('0', 'ERR', 'None', 'N/D', '—')) else (mac if mac else (hostname if hostname else ip))

        status = client.get_int(OID_PRINTER_STATUS, 0)
        if status in (-1, None):
            status = 0  # En línea (respondió a SNMP)

        toners = client.get_supplies()
        is_ok, err_type, err_detail = client.get_operational_status()

        # Detección de tecnología (Tinta vs Láser)
        model_str = model or 'Impresora de red'
        is_ink = any(s.get('type') == 'ink' for s in toners.values()) or any(
            w in model_str.lower() for w in ['ecotank', 'smart tank', 'megatank', 'ink tank', 'deskjet', 'pixma', 'inkbenefit', 'l31', 'l32', 'l41', 'l42', 'l51', 'l61', 'l80', 'g21', 'g31']
        )
        tech = 'inkjet' if is_ink else 'laser'

        info = {
            'ip':           ip,
            'hostname':     hostname,
            'mac':          mac,
            'device_id':    device_id,
            'model':        model_str,
            'serial':       serial,
            'status':       status,
            'is_online':    True,
            'page_count':   page_count or 0,
            'tech':         tech,
            'toners':       toners,
            'supplies':     toners,
            'is_ok':        is_ok,
            'error_type':   err_type,
            'error_detail': err_detail,
            'is_jammed':    (err_type == 'jam'),
            'timestamp':    datetime.now().isoformat(),
        }
        with lock:
            found.append(info)
            s_sn = f" [S/N: {serial}]" if serial else ""
            s_hn = f" [{hostname}]" if hostname else ""
            err_msg = f" — ⚠️ {err_detail}" if not is_ok and err_detail else ""
            log.info(f"  Encontrada: {ip}{s_hn} — {info['model']}{s_sn} — {page_count} pág.{err_msg}")

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(check_host, ip) for ip in ips]
        concurrent.futures.wait(futures, timeout=35)

    log.info(f"Escaneo completo: {len(found)} impresoras de red encontradas.")
    return found


def query_single_printer(ip, community='public', snmp_port=161, timeout=0.8):
    """
    Consulta directamente una única impresora por IP vía SNMP v1/v2c.
    Retorna dict con datos de modelo, serie, contador y consumibles, o None si no responde.
    """
    try:
        client = SNMPClient(ip, community, snmp_port, timeout)
        probe = client.get_str(OID_DEVICE_MODEL) or client.get_str(OID_SYS_DESCR) or client.get_str(OID_KYOCERA_MODEL)
        if not probe:
            return None
        model = client.get_model() or probe
        serial = client.get_serial()
        sys_name = client.get_sys_name()
        hostname = resolve_printer_hostname(ip, sys_name)
        arp_map = get_arp_table_map()
        mac = arp_map.get(ip, '')
        if not mac:
            mac = client.get_mac_address()
        clean_s = (serial or '').strip()
        device_id = clean_s if (clean_s and clean_s not in ('0', 'ERR', 'None', 'N/D', '—')) else (mac if mac else (hostname if hostname else ip))

        page_count = client.get_page_counter()
        status = client.get_int(OID_PRINTER_STATUS, 0)
        if status in (-1, None):
            status = 0
        toners = client.get_supplies()
        is_ok, err_type, err_detail = client.get_operational_status()

        model_str = model or 'Impresora de red'
        is_ink = any(s.get('type') == 'ink' for s in toners.values()) or any(
            w in model_str.lower() for w in ['ecotank', 'smart tank', 'megatank', 'ink tank', 'deskjet', 'pixma', 'inkbenefit', 'l31', 'l32', 'l41', 'l42', 'l51', 'l61', 'l80', 'g21', 'g31']
        )
        tech = 'inkjet' if is_ink else 'laser'

        return {
            'ip':           ip,
            'hostname':     hostname,
            'mac':          mac,
            'device_id':    device_id,
            'model':        model_str,
            'serial':       serial,
            'status':       status,
            'is_online':    True,
            'page_count':   page_count or 0,
            'tech':         tech,
            'toners':       toners,
            'supplies':     toners,
            'is_ok':        is_ok,
            'error_type':   err_type,
            'error_detail': err_detail,
            'is_jammed':    (err_type == 'jam'),
            'paper_trays':  client.get_paper_trays(),
            'timestamp':    datetime.now().isoformat(),
        }
    except Exception as e:
        log.warning(f"Error consultando impresora {ip}: {e}")
        return None


# ============================================================================
# HELPER DE LONGITUD ASN.1 BER
# ============================================================================
def _encode_asn1_length(length: int) -> bytes:
    """Codifica una longitud según estándar ASN.1 BER (short-form y long-form)."""
    if length < 128:
        return bytes([length])
    num_bytes = max(1, (length.bit_length() + 7) // 8)
    return bytes([0x80 | num_bytes]) + length.to_bytes(num_bytes, 'big')


# ============================================================================
# DETECCIÓN DE MARCAS DE IMPRESORAS
# ============================================================================
def detect_brand(model: str) -> str:
    """Detecta el fabricante del equipo a partir del modelo o descripción."""
    m = str(model or '').lower()
    if any(k in m for k in ('kyocera', 'ecosys', 'taskalfa', 'km-', 'fs-')):
        return 'KYOCERA'
    if any(k in m for k in ('hp', 'hewlett', 'laserjet', 'pagewide', 'colorjet', 'deskjet')):
        return 'HP'
    if any(k in m for k in ('brother', 'dcp', 'mfc', 'hl-')):
        return 'BROTHER'
    if any(k in m for k in ('ricoh', 'aficio', 'savin', 'gestetner', 'lanier', 'mp c', 'im c')):
        return 'RICOH'
    if any(k in m for k in ('lexmark', 'optra', 'ms3', 'ms4', 'ms5', 'mx3', 'mx4', 'mx5')):
        return 'LEXMARK'
    if any(k in m for k in ('xerox', 'phaser', 'workcentre', 'versalink', 'altalink')):
        return 'XEROX'
    if any(k in m for k in ('samsung', 'proxpress', 'multixpress')):
        return 'SAMSUNG'
    return 'GENERIC'


# ============================================================================
# TABLA DE OIDs DE ECOPRINT / DENSIDAD POR FABRICANTE
# ============================================================================
ECOPRINT_OIDS = {
    'KYOCERA': {
        'density_oid': '1.3.6.1.4.1.1347.43.5.2.1.1.1.1',   # Nivel de densidad (1 a 5)
        'ecoprint_oid': '1.3.6.1.4.1.1347.43.5.2.1.2.1.1',  # EcoPrint On(1) / Off(2)
        'supports_levels': True,
        'min_level': 1,
        'max_level': 5,
        'default_level': 3,
        'estimated_savings': {1: 30, 2: 20, 3: 0, 4: 0, 5: 0},
        'level_names': {
            1: 'Nivel 1 — Máximo ahorro (EcoPrint ON — Muy claro, ~30% ahorro)',
            2: 'Nivel 2 — Ahorro moderado (EcoPrint ON — Claro, ~20% ahorro)',
            3: 'Nivel 3 — Calidad Estándar de fábrica (EcoPrint OFF — Normal, recomendado)',
            4: 'Nivel 4 — Oscuro (EcoPrint OFF — Calidad comercial)',
            5: 'Nivel 5 — Máxima cobertura (EcoPrint OFF — Muy oscuro)'
        },
        'description': 'Kyocera EcoPrint & Densidad (1 a 5)'
    },
    'HP': {
        'density_oid': '1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.6.0',
        'ecoprint_oid': '1.3.6.1.4.1.11.2.3.9.4.2.1.4.1.2.5.0',
        'supports_levels': True,
        'min_level': 1,
        'max_level': 5,
        'default_level': 3,
        'estimated_savings': {1: 25, 2: 20, 3: 15, 4: 8, 5: 0},
        'level_names': {
            1: 'Nivel 1 (EconoMode Máx Ahorro ~25%)',
            2: 'Nivel 2 (Borrador Rápido ~20%)',
            3: 'Nivel 3 (Normal / Equilibrado ~15%)',
            4: 'Nivel 4 (Óptimo ~8%)',
            5: 'Nivel 5 (Pro / Máxima Cobertura)'
        },
        'description': 'HP EconoMode / Densidad (1 a 5)'
    },
    'BROTHER': {
        'density_oid': None,
        'ecoprint_oid': '1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0',
        'supports_levels': False,
        'min_level': 1,
        'max_level': 2,
        'default_level': 1,
        'estimated_savings': {1: 20, 2: 0},
        'level_names': {
            1: 'Activado (Ahorro de Tóner ~20%)',
            2: 'Desactivado (Estándar)'
        },
        'description': 'Brother Modo Ahorro de Tóner'
    },
    'RICOH': {
        'density_oid': '1.3.6.1.4.1.367.3.2.1.2.1.4.0',
        'ecoprint_oid': '1.3.6.1.4.1.367.3.2.1.2.1.4.0',
        'supports_levels': True,
        'min_level': 1,
        'max_level': 5,
        'default_level': 3,
        'estimated_savings': {1: 25, 2: 20, 3: 15, 4: 8, 5: 0},
        'level_names': {
            1: 'Nivel 1 (Ahorro ~25%)',
            2: 'Nivel 2 (Ahorro ~20%)',
            3: 'Nivel 3 (Equilibrado ~15%)',
            4: 'Nivel 4 (Estándar ~8%)',
            5: 'Nivel 5 (Alta densidad)'
        },
        'description': 'Ricoh Densidad de Impresión'
    }
}


# ============================================================================
# CLIENTE SNMP WRITER (SET vía UDP RAW ASN.1 BER sin librerías externas)
# ============================================================================
class SNMPWriter:
    """
    Cliente SNMP SET vía sockets UDP con ASN.1 BER nativo.
    Permite modificar OIDs de configuración remota (EcoPrint, densidades, etc.).
    """
    def __init__(self, host, community='private', port=161, timeout=2.0):
        self.host      = host
        self.community = str(community or 'private').encode('ascii', errors='ignore')
        self.port      = port
        self.timeout   = timeout

    def _encode_oid(self, oid_str):
        parts = [int(x) for x in oid_str.strip('.').split('.')]
        encoded = bytes([40 * parts[0] + parts[1]])
        for p in parts[2:]:
            if p < 128:
                encoded += bytes([p])
            else:
                segs = []
                while p:
                    segs.append(p & 0x7F)
                    p >>= 7
                segs.reverse()
                for i, seg in enumerate(segs):
                    encoded += bytes([seg | (0x80 if i < len(segs) - 1 else 0)])
        return encoded

    def _build_set_int(self, oid_str, int_value, version=0):
        oid_enc = self._encode_oid(oid_str)
        oid_tlv = b'\x06' + _encode_asn1_length(len(oid_enc)) + oid_enc

        int_val = int(int_value)
        nbytes = max(1, (int_val.bit_length() + 8) // 8)
        val_bytes = int_val.to_bytes(nbytes, 'big', signed=True)
        val_tlv = b'\x02' + _encode_asn1_length(len(val_bytes)) + val_bytes

        varbind_body = oid_tlv + val_tlv
        varbind = b'\x30' + _encode_asn1_length(len(varbind_body)) + varbind_body
        varbind_list = b'\x30' + _encode_asn1_length(len(varbind)) + varbind

        req_id = b'\x02\x04\x33\x44\x55\x66'
        err    = b'\x02\x01\x00'
        err_ix = b'\x02\x01\x00'
        pdu_body = req_id + err + err_ix + varbind_list
        pdu = b'\xa3' + _encode_asn1_length(len(pdu_body)) + pdu_body

        comm_tlv = b'\x04' + _encode_asn1_length(len(self.community)) + self.community
        ver_tlv  = b'\x02\x01' + bytes([version])
        msg_body = ver_tlv + comm_tlv + pdu
        return b'\x30' + _encode_asn1_length(len(msg_body)) + msg_body

    def set_int(self, oid_str, int_value: int) -> bool:
        """
        Envía un SNMP SetRequest para un valor entero.
        Intenta SNMPv1 primero (version=0) y luego SNMPv2c (version=1).
        Retorna True si el dispositivo respondió GetResponse con error-status == 0.
        """
        for ver in (0, 1):
            sock = None
            try:
                pkt = self._build_set_int(oid_str, int_value, version=ver)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(self.timeout)
                sock.sendto(pkt, (self.host, self.port))
                data, _ = sock.recvfrom(4096)
                if self._verify_response(data):
                    return True
            except socket.timeout:
                continue
            except Exception as e:
                log.debug(f"SNMP SET {self.host} OID {oid_str}={int_value} (v{ver}): {e}")
                continue
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
        return False

    def _build_set_str(self, oid_str, str_value: str, version=0):
        oid_enc = self._encode_oid(oid_str)
        oid_tlv = b'\x06' + _encode_asn1_length(len(oid_enc)) + oid_enc

        val_bytes = str(str_value or '').encode('utf-8', errors='replace')
        val_tlv = b'\x04' + _encode_asn1_length(len(val_bytes)) + val_bytes

        varbind_body = oid_tlv + val_tlv
        varbind = b'\x30' + _encode_asn1_length(len(varbind_body)) + varbind_body
        varbind_list = b'\x30' + _encode_asn1_length(len(varbind)) + varbind

        req_id = b'\x02\x04\x33\x44\x55\x66'
        err    = b'\x02\x01\x00'
        err_ix = b'\x02\x01\x00'
        pdu_body = req_id + err + err_ix + varbind_list
        pdu = b'\xa3' + _encode_asn1_length(len(pdu_body)) + pdu_body

        comm_tlv = b'\x04' + _encode_asn1_length(len(self.community)) + self.community
        ver_tlv  = b'\x02\x01' + bytes([version])
        msg_body = ver_tlv + comm_tlv + pdu
        return b'\x30' + _encode_asn1_length(len(msg_body)) + msg_body

    def set_str(self, oid_str, str_value: str) -> bool:
        """
        Envía un SNMP SetRequest para un valor de cadena (OctetString).
        Intenta SNMPv1 primero (version=0) y luego SNMPv2c (version=1).
        Retorna True si el dispositivo respondió GetResponse con error-status == 0.
        """
        for ver in (0, 1):
            sock = None
            try:
                pkt = self._build_set_str(oid_str, str_value, version=ver)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(self.timeout)
                sock.sendto(pkt, (self.host, self.port))
                data, _ = sock.recvfrom(4096)
                if self._verify_response(data):
                    return True
            except socket.timeout:
                continue
            except Exception as e:
                log.debug(f"SNMP SET STR {self.host} OID {oid_str}='{str_value}' (v{ver}): {e}")
                continue
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
        return False

    def _read_length_raw(self, data: bytes, offset: int) -> tuple:
        if offset >= len(data):
            return 0, offset
        first = data[offset]
        if first < 128:
            return first, offset + 1
        num_bytes = first & 0x7F
        if num_bytes == 0 or offset + 1 + num_bytes > len(data):
            return 0, offset + 1
        length = 0
        for b in data[offset + 1: offset + 1 + num_bytes]:
            length = (length << 8) | b
        return length, offset + 1 + num_bytes

    def _verify_response(self, response: bytes) -> bool:
        if not response or len(response) < 15:
            return False
        pdu_idx = response.find(b'\xa2')
        if pdu_idx == -1:
            return False
        # Decodificar longitud del PDU (GetResponse 0xa2)
        _, j = self._read_length_raw(response, pdu_idx + 1)
        # Saltar request-id (tag INTEGER 0x02)
        if j < len(response) and response[j] == 0x02:
            rid_len, rid_start = self._read_length_raw(response, j + 1)
            j = rid_start + rid_len
        # Leer error-status (tag INTEGER 0x02)
        if j < len(response) and response[j] == 0x02:
            err_len, err_start = self._read_length_raw(response, j + 1)
            if err_start + err_len <= len(response):
                val = 0
                for b in response[err_start:err_start + err_len]:
                    val = (val << 8) | b
                if val == 0:
                    return True
        return False


