"""
Static file server สำหรับ serve 4Squared.html และไฟล์อื่น ๆ ในโฟลเดอร์นี้ - port 8000
พร้อมทั้งทำหน้าที่เป็น proxy ดึงลิงก์ HLS (m3u8) ของ X (Twitter) broadcast
ผ่าน guest token flow ของ X เอง (ไม่ต้อง login) แทนการใช้ X embed/widgets.js
ที่มักโดนบล็อกจาก third-party cookies

เริ่ม:  python server.py
หยุด:   กด Ctrl+C
"""
import functools
import json
import os
import re
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests

# รองรับการแสดงผลภาษาไทยบน Windows Terminal
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

STATIC_PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Bearer token สาธารณะที่ x.com/twitter.com ใช้ฝั่ง web client เองสำหรับ guest request
# (ฝังอยู่ใน JS bundle ของหน้าเว็บ ไม่ใช่ credential ส่วนตัวของใคร) ใช้คู่กับ guest token
# เพื่อเรียก endpoint สาธารณะของ broadcast/live_video_stream เท่านั้น
X_BEARER_TOKEN = (
    'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs='
    '1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA'
)
X_API_BASE = 'https://api.x.com/1.1'
X_REQUEST_TIMEOUT = 10
X_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# โฮสต์ปลายทางที่อนุญาตให้ /api/x-proxy ดึงข้อมูลให้ได้ (กันไม่ให้ server ถูกใช้เป็น open proxy)
ALLOWED_PROXY_HOST_SUFFIXES = (
    'twimg.com', 'pscp.tv', 'x.com', 'twitter.com',
    'fastly.net', 'akamaized.net', 'cloudfront.net', 'llnwd.net', 'edgesuite.net',
)

BROADCAST_ID_RE = re.compile(r'^[a-zA-Z0-9]{5,40}$')
M3U8_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')

_guest_token_lock = threading.Lock()
_guest_token_cache = {'token': None, 'ts': 0}
GUEST_TOKEN_TTL = 3300  # วินาที (~55 นาที) กันชนกับอายุจริงของ guest token ฝั่ง X


def get_guest_token(force_refresh=False):
    """ขอ guest token จาก X (ไม่ต้อง login) แล้ว cache ไว้ใช้ซ้ำจนกว่าจะหมดอายุ/ถูก reject"""
    with _guest_token_lock:
        now = time.time()
        if not force_refresh and _guest_token_cache['token'] and now - _guest_token_cache['ts'] < GUEST_TOKEN_TTL:
            return _guest_token_cache['token']
        r = requests.post(
            f'{X_API_BASE}/guest/activate.json',
            headers={'Authorization': f'Bearer {X_BEARER_TOKEN}', 'User-Agent': X_USER_AGENT},
            timeout=X_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        token = r.json()['guest_token']
        _guest_token_cache['token'] = token
        _guest_token_cache['ts'] = now
        return token


def x_api_get(path, params=None, retry_on_auth_fail=True):
    token = get_guest_token()
    headers = {
        'Authorization': f'Bearer {X_BEARER_TOKEN}',
        'x-guest-token': token,
        'User-Agent': X_USER_AGENT,
    }
    r = requests.get(f'{X_API_BASE}{path}', params=params, headers=headers, timeout=X_REQUEST_TIMEOUT)
    if r.status_code in (401, 403) and retry_on_auth_fail:
        get_guest_token(force_refresh=True)
        return x_api_get(path, params=params, retry_on_auth_fail=False)
    r.raise_for_status()
    return r.json()


def get_x_hls_url(broadcast_id):
    """broadcast id -> (guest token) -> media_key -> playback status -> m3u8 URL จริงจาก X"""
    data = x_api_get('/broadcasts/show.json', {'ids': broadcast_id, 'include_events': 'true'})
    broadcast = (data.get('broadcasts') or {}).get(broadcast_id)
    if not broadcast:
        raise ValueError('ไม่พบ broadcast นี้ (อาจจบไปแล้วหรือ id ไม่ถูกต้อง)')

    media_key = broadcast.get('media_key')
    if not media_key:
        raise ValueError('ไม่พบ media_key ของ broadcast นี้')

    status = x_api_get(f'/live_video_stream/status/{media_key}')
    source = status.get('source') or {}
    hls_url = source.get('noRedirectPlaybackUrl') or source.get('location')
    if not hls_url:
        raise ValueError('ยังไม่พบลิงก์สตรีม (broadcast อาจยังไม่เริ่มหรือจบไปแล้ว)')
    return hls_url


def is_allowed_proxy_url(url):
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == suf or host.endswith('.' + suf) for suf in ALLOWED_PROXY_HOST_SUFFIXES)


def proxy_url_for(absolute_url):
    return '/api/x-proxy?url=' + quote(absolute_url, safe='')


def rewrite_m3u8(body_text, base_url):
    """แทนที่ URI ทุกตัวใน m3u8 (ทั้ง sub-playlist และ segment) ให้วิ่งผ่าน /api/x-proxy
    เพื่อเลี่ยงปัญหา CORS/hotlink-protection ตอนเบราว์เซอร์เล่นจริงผ่าน hls.js"""
    out_lines = []
    for line in body_text.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith('#'):
            if stripped:
                out_lines.append(proxy_url_for(urljoin(base_url, stripped)))
            else:
                out_lines.append(line)
        elif 'URI="' in stripped:
            def _sub(m):
                return 'URI="' + proxy_url_for(urljoin(base_url, m.group(1))) + '"'
            out_lines.append(M3U8_URI_ATTR_RE.sub(_sub, line))
        else:
            out_lines.append(line)
    return '\n'.join(out_lines)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/x-stream':
            return self._handle_x_stream(parsed)
        if parsed.path == '/api/x-proxy':
            return self._handle_x_proxy(parsed)
        return super().do_GET()

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_x_stream(self, parsed):
        qs = parse_qs(parsed.query)
        broadcast_id = (qs.get('broadcast_id') or [''])[0].strip()
        if not broadcast_id or not BROADCAST_ID_RE.match(broadcast_id):
            return self._send_json(400, {'error': 'broadcast_id ไม่ถูกต้อง'})
        try:
            hls_url = get_x_hls_url(broadcast_id)
        except Exception as e:
            print(f'[x-stream] error for {broadcast_id}: {e}')
            return self._send_json(502, {'error': str(e)})
        return self._send_json(200, {'hls_url': proxy_url_for(hls_url)})

    def _handle_x_proxy(self, parsed):
        qs = parse_qs(parsed.query)
        target = unquote((qs.get('url') or [''])[0])
        if not is_allowed_proxy_url(target):
            return self._send_json(403, {'error': 'โฮสต์ปลายทางไม่ได้รับอนุญาต'})

        req_headers = {'User-Agent': X_USER_AGENT, 'Referer': 'https://x.com/'}
        range_header = self.headers.get('Range')
        if range_header:
            req_headers['Range'] = range_header

        try:
            upstream = requests.get(target, headers=req_headers, timeout=X_REQUEST_TIMEOUT, stream=True)
        except requests.RequestException as e:
            return self._send_json(502, {'error': f'proxy fetch failed: {e}'})

        content_type = upstream.headers.get('Content-Type', '')
        is_manifest = target.endswith('.m3u8') or 'mpegurl' in content_type.lower()

        if is_manifest:
            body = rewrite_m3u8(upstream.text, target).encode('utf-8')
            self.send_response(upstream.status_code)
            self.send_header('Content-Type', 'application/vnd.apple.mpegurl')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            upstream.close()
            return

        self.send_response(upstream.status_code)
        self.send_header('Content-Type', content_type or 'application/octet-stream')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        for h in ('Content-Range', 'Accept-Ranges', 'Content-Length'):
            if h in upstream.headers:
                self.send_header(h, upstream.headers[h])
        self.end_headers()
        try:
            for chunk in upstream.iter_content(chunk_size=65536):
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            upstream.close()


def main():
    static_server = ThreadingHTTPServer(("0.0.0.0", STATIC_PORT), Handler)

    print("=" * 60)
    print("4Squared Live - Local Server")
    print("=" * 60)
    print(f"หน้าเว็บหลัก  : http://localhost:{STATIC_PORT}/index.html")
    print("กด Ctrl+C เพื่อหยุด server")
    print("=" * 60)

    try:
        static_server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] กำลังปิด server...")
        static_server.shutdown()
        static_server.server_close()
        print("[server] ปิดเรียบร้อยแล้ว")


if __name__ == "__main__":
    main()
