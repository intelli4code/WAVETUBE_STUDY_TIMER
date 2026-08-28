import os
import sys
import time
import json
import sqlite3
import socket
import threading
import webbrowser
import subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Directory containing this script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "wavetube.db")
PORT = 8765
last_heartbeat = time.time()
server_instance = None
shutdown_requested = False

# ==============================================================================
# SQLite Database Setup & Migrations
# ==============================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 1. Playlist Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS playlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                youtube_id TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                thumbnail TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 2. Profiles Table (Custom & Default Presets)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                speed REAL NOT NULL DEFAULT 1.0,
                bass_boost REAL NOT NULL DEFAULT 0.0,
                visualizer_style TEXT NOT NULL DEFAULT 'bars',
                ambient_intensity REAL NOT NULL DEFAULT 0.7,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 3. Settings Key-Value Store
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        
        # Seed default profiles if none exist
        cursor.execute("SELECT COUNT(*) FROM profiles")
        if cursor.fetchone()[0] == 0:
            default_profiles = [
                ("Studio Flat", 1.0, 0.0, "bars", 0.6, 1),
                ("Bass Boosted", 1.0, 6.0, "bars", 0.9, 1),
                ("Nightcore Energy", 1.35, 2.0, "wave", 0.8, 1),
                ("Slowed & Reverb", 0.8, 4.0, "radial", 0.5, 1),
                ("Analog Matrix VU", 1.0, 3.0, "vu", 0.7, 1)
            ]
            cursor.executemany("""
                INSERT OR IGNORE INTO profiles (name, speed, bass_boost, visualizer_style, ambient_intensity, is_default)
                VALUES (?, ?, ?, ?, ?, ?)
            """, default_profiles)

        # Seed initial curated demo playlist if empty
        cursor.execute("SELECT COUNT(*) FROM playlist")
        if cursor.fetchone()[0] == 0:
            demo_tracks = [
                ("jfKfPfyJRdk", "https://www.youtube.com/watch?v=jfKfPfyJRdk", "lofi hip hop radio - beats to relax/study to", "Lofi Girl", "https://img.youtube.com/vi/jfKfPfyJRdk/mqdefault.jpg", 0),
                ("5qap5aO4i9A", "https://www.youtube.com/watch?v=5qap5aO4i9A", "Lofi Hip Hop - Chill Beats for Sleeping / Studying", "Lofi Coffee", "https://img.youtube.com/vi/5qap5aO4i9A/mqdefault.jpg", 1),
                ("rUxyKA_-grg", "https://www.youtube.com/watch?v=rUxyKA_-grg", "Synthwave Radio - Chill Synth / Retro Beats", "Lofi Girl Synthwave", "https://img.youtube.com/vi/rUxyKA_-grg/mqdefault.jpg", 2),
                ("4xDzrJKXOOY", "https://www.youtube.com/watch?v=4xDzrJKXOOY", "synthwave radio - chill synth / retro / electro beats", "Lofi Girl", "https://img.youtube.com/vi/4xDzrJKXOOY/mqdefault.jpg", 3)
            ]
            cursor.executemany("""
                INSERT OR IGNORE INTO playlist (youtube_id, url, title, author, thumbnail, position)
                VALUES (?, ?, ?, ?, ?, ?)
            """, demo_tracks)

        # Default Settings
        default_settings = [
            ("volume", "80"),
            ("muted", "false"),
            ("loop_mode", "playlist"),
            ("shuffle", "false"),
            ("active_profile", "Studio Flat"),
            ("active_index", "0")
        ]
        cursor.executemany("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", default_settings)

        conn.commit()

# ==============================================================================
# HTTP Request Handler & REST API Router
# ==============================================================================
class WaveTubeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, DELETE')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def send_json(self, data, status_code=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length <= 0:
            return {}
        try:
            body = self.rfile.read(content_length).decode('utf-8')
            return json.loads(body)
        except Exception:
            return {}

    def do_GET(self):
        global last_heartbeat
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path in ('/api/heartbeat', '/api/heartbeat/'):
            last_heartbeat = time.time()
            return self.send_json({"status": "ok", "time": time.time()})

        # --- GET /api/playlist ---
        if path == '/api/playlist':
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, youtube_id, url, title, author, thumbnail, position FROM playlist ORDER BY position ASC, id ASC")
                rows = [dict(row) for row in cursor.fetchall()]
            return self.send_json({"playlist": rows})

        # --- GET /api/profiles ---
        if path == '/api/profiles':
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, speed, bass_boost, visualizer_style, ambient_intensity, is_default FROM profiles ORDER BY is_default DESC, name ASC")
                rows = [dict(row) for row in cursor.fetchall()]
            return self.send_json({"profiles": rows})

        # --- GET /api/settings ---
        if path == '/api/settings':
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM settings")
                settings = {row['key']: row['value'] for row in cursor.fetchall()}
            return self.send_json({"settings": settings})

        # Static files serving
        if path in ('/', ''):
            self.path = '/wavetube_youtube_audio_player.html'
        
        return super().do_GET()

    def do_POST(self):
        global last_heartbeat, shutdown_requested
        parsed_url = urlparse(self.path)
        path = parsed_url.path

        if path in ('/api/heartbeat', '/api/heartbeat/'):
            last_heartbeat = time.time()
            return self.send_json({"status": "ok"})

        if path == '/api/shutdown':
            shutdown_requested = True
            self.send_json({"status": "shutting_down"})
            
            def delayed_kill():
                time.sleep(0.4)
                if server_instance:
                    server_instance.shutdown()
                os._exit(0)
            
            threading.Thread(target=delayed_kill, daemon=True).start()
            return

        # --- POST /api/playlist/add ---
        if path == '/api/playlist/add':
            data = self.read_json_body()
            youtube_id = str(data.get('youtube_id') or '').strip()
            if not youtube_id:
                return self.send_json({"error": "Missing youtube_id"}, 400)
            
            url = data.get('url') or f"https://www.youtube.com/watch?v={youtube_id}"
            title = data.get('title') or f"YouTube Track ({youtube_id})"
            author = data.get('author') or "YouTube"
            thumbnail = data.get('thumbnail') or f"https://img.youtube.com/vi/{youtube_id}/mqdefault.jpg"

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT MAX(position) FROM playlist")
                max_pos = cursor.fetchone()[0]
                new_pos = 0 if max_pos is None else max_pos + 1

                cursor.execute("""
                    INSERT INTO playlist (youtube_id, url, title, author, thumbnail, position)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(youtube_id) DO UPDATE SET
                        title = excluded.title,
                        author = excluded.author,
                        thumbnail = excluded.thumbnail
                """, (youtube_id, url, title, author, thumbnail, new_pos))
                conn.commit()

            return self.send_json({"status": "saved", "youtube_id": youtube_id})

        # --- POST /api/playlist/remove ---
        if path == '/api/playlist/remove':
            data = self.read_json_body()
            youtube_id = data.get('youtube_id')
            track_id = data.get('id')

            with get_db() as conn:
                cursor = conn.cursor()
                if youtube_id:
                    cursor.execute("DELETE FROM playlist WHERE youtube_id = ?", (youtube_id,))
                elif track_id:
                    cursor.execute("DELETE FROM playlist WHERE id = ?", (track_id,))
                conn.commit()

            return self.send_json({"status": "removed"})

        # --- POST /api/playlist/reorder ---
        if path == '/api/playlist/reorder':
            data = self.read_json_body()
            ordered_ids = data.get('ordered_ids', [])
            with get_db() as conn:
                cursor = conn.cursor()
                for pos, yid in enumerate(ordered_ids):
                    cursor.execute("UPDATE playlist SET position = ? WHERE youtube_id = ?", (pos, yid))
                conn.commit()
            return self.send_json({"status": "reordered"})

        # --- POST /api/playlist/clear ---
        if path == '/api/playlist/clear':
            with get_db() as conn:
                conn.cursor().execute("DELETE FROM playlist")
                conn.commit()
            return self.send_json({"status": "cleared"})

        # --- POST /api/profiles/save ---
        if path == '/api/profiles/save':
            data = self.read_json_body()
            name = str(data.get('name') or '').strip()
            if not name:
                return self.send_json({"error": "Profile name is required"}, 400)
            
            speed = float(data.get('speed', 1.0))
            bass_boost = float(data.get('bass_boost', 0.0))
            visualizer_style = str(data.get('visualizer_style', 'bars'))
            ambient_intensity = float(data.get('ambient_intensity', 0.7))

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO profiles (name, speed, bass_boost, visualizer_style, ambient_intensity, is_default)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(name) DO UPDATE SET
                        speed = excluded.speed,
                        bass_boost = excluded.bass_boost,
                        visualizer_style = excluded.visualizer_style,
                        ambient_intensity = excluded.ambient_intensity
                """, (name, speed, bass_boost, visualizer_style, ambient_intensity))
                conn.commit()

            return self.send_json({"status": "saved", "name": name})

        # --- POST /api/profiles/delete ---
        if path == '/api/profiles/delete':
            data = self.read_json_body()
            name = data.get('name')
            if not name:
                return self.send_json({"error": "Profile name required"}, 400)
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM profiles WHERE name = ? AND is_default = 0", (name,))
                conn.commit()
            return self.send_json({"status": "deleted"})

        # --- POST /api/settings ---
        if path == '/api/settings':
            data = self.read_json_body()
            with get_db() as conn:
                cursor = conn.cursor()
                for k, v in data.items():
                    cursor.execute("""
                        INSERT INTO settings (key, value) VALUES (?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """, (str(k), str(v)))
                conn.commit()
            return self.send_json({"status": "updated"})

        self.send_error(404, "Endpoint not found")

    def log_message(self, format, *args):
        # Silent windowless execution
        pass

# ==============================================================================
# Server Watchdog & App Lifecycle
# ==============================================================================
def watchdog():
    """Shuts down the server if no heartbeat is received from the browser for 10 seconds."""
    global last_heartbeat, shutdown_requested
    # Allow 25 seconds initial grace period for browser launch
    time.sleep(25)
    while not shutdown_requested:
        time.sleep(2)
        if time.time() - last_heartbeat > 10:
            if server_instance:
                server_instance.shutdown()
            os._exit(0)

def find_available_port(start_port=8765):
    for p in range(start_port, start_port + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', p)) != 0:
                return p
    return start_port

def launch_browser(url):
    """Launch as a standalone desktop app window using Edge/Chrome App mode or default browser."""
    edge_paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")
    ]
    for ep in edge_paths:
        if os.path.exists(ep):
            try:
                subprocess.Popen([ep, f"--app={url}", "--name=WaveTube"])
                return
            except Exception:
                pass

    chrome_paths = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe")
    ]
    for cp in chrome_paths:
        if os.path.exists(cp):
            try:
                subprocess.Popen([cp, f"--app={url}", "--name=WaveTube"])
                return
            except Exception:
                pass

    webbrowser.open(url)

def main():
    global server_instance, PORT
    
    # Initialize SQLite tables & seeds
    init_db()

    PORT = find_available_port(8765)
    server_address = ('127.0.0.1', PORT)
    server_instance = HTTPServer(server_address, WaveTubeHandler)
    
    # Start watchdog thread
    threading.Thread(target=watchdog, daemon=True).start()
    
    # Launch browser window after small delay
    url = f"http://127.0.0.1:{PORT}/wavetube_youtube_audio_player.html"
    threading.Timer(0.3, launch_browser, args=[url]).start()
    
    try:
        server_instance.serve_forever()
    except Exception:
        pass
    finally:
        if server_instance:
            server_instance.server_close()

if __name__ == '__main__':
    main()
