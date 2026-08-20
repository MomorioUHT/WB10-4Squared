"""
Static file server สำหรับ serve 4Squared.html และไฟล์อื่น ๆ ในโฟลเดอร์นี้ - port 8000

เริ่ม:  python server.py
หยุด:   กด Ctrl+C
"""
import functools
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# รองรับการแสดงผลภาษาไทยบน Windows Terminal
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

STATIC_PORT = 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    static_handler = functools.partial(SimpleHTTPRequestHandler, directory=BASE_DIR)
    static_server = ThreadingHTTPServer(("0.0.0.0", STATIC_PORT), static_handler)

    print("=" * 60)
    print("4Squared Live - Local Server")
    print("=" * 60)
    print(f"หน้าเว็บหลัก  : http://localhost:{STATIC_PORT}/4Squared.html")
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
