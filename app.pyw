import os
import sys
import time
import json
import sqlite3
import socket
import threading
import webbrowser
import subprocess
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False

# Directory containing this script
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "wavetube.db")
PORT = 8765
last_heartbeat = time.time()
server_instance = None
shutdown_requested = False
stream_cache = {}

def proxy_audio_stream(handler, stream_url):
    """Proxy audio stream with HTTP Range support and CORS headers to enable full Web Audio API FFT."""
    _proxy_stream(handler, stream_url, content_type_fallback='audio/webm')

def proxy_video_stream(handler, stream_url):
    """Proxy video stream with HTTP Range support, proper MIME type for HTML5 video element."""
    _proxy_stream(handler, stream_url, content_type_fallback='video/mp4')

def _proxy_stream(handler, stream_url, content_type_fallback='audio/webm'):
    """Generic streaming proxy with Range support."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        
        # Forward Range header if browser requested a specific range (for seeking)
        range_header = handler.headers.get('Range')
        if range_header:
            headers['Range'] = range_header

        req = urllib.request.Request(stream_url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as upstream:
            status_code = upstream.status or 200
            handler.send_response(status_code)
            
            # Forward headers — override Content-Type to correct type
            content_type = upstream.headers.get('Content-Type', content_type_fallback)
            # Ensure video streams get proper video MIME type for HTML5 <video>
            if content_type_fallback.startswith('video') and 'audio' in content_type:
                content_type = content_type_fallback
            handler.send_header('Content-Type', content_type)
            handler.send_header('Accept-Ranges', 'bytes')
            handler.send_header('Access-Control-Allow-Origin', '*')
            handler.send_header('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS')
            handler.send_header('Access-Control-Allow-Headers', 'Range, Accept-Encoding, Origin')
            
            if 'Content-Length' in upstream.headers:
                handler.send_header('Content-Length', upstream.headers['Content-Length'])
            if 'Content-Range' in upstream.headers:
                handler.send_header('Content-Range', upstream.headers['Content-Range'])
                
            handler.end_headers()
            
            # Stream in 64KB chunks
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                try:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                except (ConnectionResetError, BrokenPipeError):
                    break
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:
        print("Stream proxy error:", e)

def extract_direct_audio(youtube_id):
    """Extract direct audio stream URL using yt-dlp to bypass embedding restrictions."""
    if not HAS_YTDLP or not youtube_id:
        return None
    
    # Check cache (15 minute TTL)
    cached = stream_cache.get(youtube_id)
    if cached and time.time() - cached.get('timestamp', 0) < 900:
        return cached

    url = f"https://www.youtube.com/watch?v={youtube_id}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': False,
        'skip_download': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            stream_url = info.get('url')
            
            # If no top-level URL, search formats list for the best audio stream
            if not stream_url and info.get('formats'):
                # 1. Search for audio-only stream
                audio_formats = [
                    f for f in info['formats']
                    if f.get('url') and (f.get('acodec') != 'none' or 'audio' in str(f.get('mimeType', '')))
                ]
                if audio_formats:
                    audio_formats.sort(key=lambda f: f.get('abr') or f.get('tbr') or 0, reverse=True)
                    stream_url = audio_formats[0]['url']
                else:
                    # 2. Fallback to any valid stream URL
                    for f in reversed(info['formats']):
                        if f.get('url'):
                            stream_url = f['url']
                            break

            # 3. Fallback to HLS manifest URL if live stream
            if not stream_url and info.get('manifest_url'):
                stream_url = info['manifest_url']

            if stream_url:
                result = {
                    'stream_url': stream_url,
                    'title': info.get('title', f"Track ({youtube_id})"),
                    'author': info.get('uploader') or info.get('channel') or "YouTube",
                    'duration': info.get('duration', 0),
                    'timestamp': time.time()
                }
                stream_cache[youtube_id] = result
                return result
    except Exception as e:
        print(f"Direct stream extraction error for {youtube_id}:", e)
    
    return None

video_stream_cache = {}

def extract_direct_video(youtube_id):
    """Extract direct video stream URL using yt-dlp to bypass embedding restrictions."""
    if not HAS_YTDLP or not youtube_id:
        return None
    
    cached = video_stream_cache.get(youtube_id)
    if cached and time.time() - cached.get('timestamp', 0) < 900:
        return cached

    url = f"https://www.youtube.com/watch?v={youtube_id}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'extract_flat': False,
        'skip_download': True
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            video_url = None
            
            # 1. Prefer H.264 (avc1/mp4v) stream up to 1080p (universally decoded by all browsers)
            h264_formats = [
                f for f in formats 
                if f.get('url') and f.get('vcodec') != 'none' 
                and ('avc1' in f.get('vcodec', '') or 'mp4v' in f.get('vcodec', ''))
                and (f.get('height') or 0) <= 1080
            ]
            if h264_formats:
                h264_formats.sort(key=lambda f: f.get('height') or 0, reverse=True)
                video_url = h264_formats[0]['url']
            
            # 2. Fallback to progressive MP4
            if not video_url:
                for f in reversed(formats):
                    if f.get('url') and f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                        video_url = f['url']
                        break
            
            # 3. Fallback to any high-res video stream
            if not video_url:
                for f in reversed(formats):
                    if f.get('url') and f.get('vcodec') != 'none':
                        video_url = f['url']
                        break
            
            if not video_url and info.get('url'):
                video_url = info['url']

            if video_url:
                result = {
                    'stream_url': video_url,
                    'title': info.get('title', f"Video ({youtube_id})"),
                    'author': info.get('uploader') or info.get('channel') or "YouTube",
                    'duration': info.get('duration', 0),
                    'timestamp': time.time()
                }
                video_stream_cache[youtube_id] = result
                return result
    except Exception as e:
        print(f"Direct video stream extraction error for {youtube_id}:", e)
    return None

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
                ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Rick Astley - Never Gonna Give You Up (Official Music Video)", "Rick Astley", "https://img.youtube.com/vi/dQw4w9WgXcQ/mqdefault.jpg", 0),
                ("kJQP7kiw5Fk", "https://www.youtube.com/watch?v=kJQP7kiw5Fk", "Luis Fonsi - Despacito ft. Daddy Yankee", "Luis Fonsi", "https://img.youtube.com/vi/kJQP7kiw5Fk/mqdefault.jpg", 1),
                ("4xDzrJKXOOY", "https://www.youtube.com/watch?v=4xDzrJKXOOY", "synthwave radio - chill synth / retro / electro beats", "Lofi Girl", "https://img.youtube.com/vi/4xDzrJKXOOY/mqdefault.jpg", 2),
                ("9bZkp7q19f0", "https://www.youtube.com/watch?v=9bZkp7q19f0", "PSY - GANGNAM STYLE(강남스타일) M/V", "officialpsy", "https://img.youtube.com/vi/9bZkp7q19f0/mqdefault.jpg", 3)
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

        # --- GET /api/stream (Bypass Embedding Restrictions) ---
        if path in ('/api/stream', '/api/stream/'):
            params = parse_qs(parsed_url.query)
            youtube_id = (params.get('v') or params.get('id') or [''])[0].strip()
            
            if not youtube_id:
                return self.send_json({"error": "Missing video ID (v=...)"}, status=400)
            
            result = extract_direct_audio(youtube_id)
            if result:
                return self.send_json({"success": True, **result})
            else:
                return self.send_json({"error": "Could not extract direct audio stream for this video."}, status=404)

        # --- GET /api/stream/audio (Direct Audio Stream Proxy with CORS & Range Support) ---
        if path.startswith('/api/stream/audio'):
            params = parse_qs(parsed_url.query)
            youtube_id = (params.get('v') or params.get('id') or [''])[0].strip()
            result = extract_direct_audio(youtube_id)
            if result and result.get('stream_url'):
                return proxy_audio_stream(self, result['stream_url'])
            else:
                return self.send_json({"error": "Audio stream unavailable."}, status=404)

        # --- GET /api/stream/video (Direct Video Stream Proxy with CORS & Range Support) ---
        if path.startswith('/api/stream/video'):
            params = parse_qs(parsed_url.query)
            youtube_id = (params.get('v') or params.get('id') or [''])[0].strip()
            result = extract_direct_video(youtube_id)
            if result and result.get('stream_url'):
                return proxy_video_stream(self, result['stream_url'])
            else:
                return self.send_json({"error": "Video stream unavailable."}, status=404)

        # Static files serving
        if path in ('/', ''):
            self.path = '/index.html'
        
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
    url = f"http://127.0.0.1:{PORT}/index.html"
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
