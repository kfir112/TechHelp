import socket
import threading
import io
import sys
import signal
import os
import time
import tkinter as tk
from tkinter import ttk, font
from PIL import Image, ImageTk
import ssl
import sqlite3
import hashlib
from datetime import datetime

try:
    from google import genai
    from google.genai import types
except ImportError:
    print("[Error] Please install the new Gemini SDK using: pip install google-genai")
    sys.exit(1)

try:
    SERVER_CONTEXT = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    SERVER_CONTEXT.load_cert_chain(certfile="server.crt", keyfile="server.key")
except FileNotFoundError:
    print("[Error] TLS Certificates not found! Please ensure 'server.crt' and 'server.key' are in the directory.")
    sys.exit(1)


class OmniDatabase:
    def __init__(self, db_name="omnidesk.db"):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_name)

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Technicians (
                technician_id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(100) NOT NULL,
                salt VARCHAR(64) NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Clients (
                client_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address VARCHAR(50) UNIQUE NOT NULL,
                status VARCHAR(20),
                ai_summary TEXT
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ChatMessages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_type VARCHAR(10),
                sender_id INTEGER,
                receiver_id INTEGER,
                message_text TEXT,
                timestamp DATETIME
            )
        ''')
        
        cursor.execute("SELECT COUNT(*) FROM Technicians")
        if cursor.fetchone()[0] == 0:
            random_salt = os.urandom(16).hex()
            salted_password = "1234" + random_salt
            hashed_text = hashlib.sha256(salted_password.encode()).hexdigest()
            
            cursor.execute("INSERT INTO Technicians (username, password, salt) VALUES (?, ?, ?)", 
                           ("admin", hashed_text, random_salt))
            
        conn.commit()
        conn.close()

    def register_technician(self, username, password):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            random_salt = os.urandom(16).hex()
            salted_input = password + random_salt
            hashed_text = hashlib.sha256(salted_input.encode()).hexdigest()
            
            cursor.execute("INSERT INTO Technicians (username, password, salt) VALUES (?, ?, ?)", 
                           (username, hashed_text, random_salt))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

    def validate_technician(self, username, password):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT password, salt, technician_id FROM Technicians WHERE username = ?", (username,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            stored_hash, salt, technician_id = result
            salted_input = password + salt
            calculated_hash = hashlib.sha256(salted_input.encode()).hexdigest()
            
            if calculated_hash == stored_hash:
                return technician_id
                
        return None

    def log_client_connection(self, ip_address, status):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO Clients (ip_address, status, ai_summary) 
                VALUES (?, ?, '')
                ON CONFLICT(ip_address) DO UPDATE SET status=?
            ''', (ip_address, status, status))
            conn.commit()
            
            cursor.execute("SELECT client_id FROM Clients WHERE ip_address = ?", (ip_address,))
            client_id = cursor.fetchone()[0]
            return client_id
        finally:
            conn.close()

    def update_client_ai_summary(self, ip_address, summary):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE Clients SET ai_summary = ? WHERE ip_address = ?", (summary, ip_address))
        conn.commit()
        conn.close()

    def save_chat_message(self, sender_type, sender_id, receiver_id, text):
        conn = self.get_connection()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO ChatMessages (sender_type, sender_id, receiver_id, message_text, timestamp)
            VALUES (?, ?, ?, ?, ?)
        ''', (sender_type, sender_id, receiver_id, text, now))
        conn.commit()
        conn.close()

db = OmniDatabase()


class ClientHandler:
    def __init__(self, ip, cmd_sock, gui_callback, alert_callback):
        self.ip = ip
        self.cmd_sock = cmd_sock
        self.screen_sock = None
        
        self.latest_image = None
        self.lock = threading.Lock()
        self.is_active = True
        
        self.state = "AI_TALKING" 
        self.chat_history = []
        self.gui_callback = gui_callback 
        self.alert_callback = alert_callback

        self.db_id = db.log_client_connection(self.ip, "AI_TALKING")

        self.ai_client = None
        self.ai_chat = None
        self.init_gemini_ai()

        threading.Thread(target=self.listen_for_client_messages, daemon=True).start()

    def init_gemini_ai(self):
        try:
            with open("api_key.txt",'r') as file:
                the_api_key = file.read().strip()
                self.ai_client = genai.Client(api_key=the_api_key)
            
            system_prompt = (
                "You are an automated IT support triage assistant. "
                "Speak ONLY in Hebrew. Greet the user politely, ask them to describe the technical problem, "
                "and gather key details. "
                "CRITICAL INSTRUCTION: If the user explicitly asks you to DO something for them on their computer "
                "(e.g., 'תתקין לי אופיס', 'תוריד לי תוכנה', 'תעשה את זה בשבילי'), you MUST explain that you are an AI "
                "and cannot control their mouse. Tell them politely: 'הבנתי. אני רק עוזר וירטואלי ולכן לא יכול לבצע פעולות בעכבר. "
                "אני מעביר כעת את הפנייה לטכנאי אנושי שישתלט על המחשב ויעשה זאת עבורך.' "
                "Be brief and empathetic."
            )
            
            self.ai_chat = self.ai_client.chats.create(
                model="gemini-2.5-flash",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7
                )
            )
            
            welcome_msg = "עוזר הבינה המלאכותית: שלום! אני העוזר הווירטואלי. כיצד אוכל לעזור לך היום?"
            self.chat_history.append(welcome_msg)
            
            def send_delayed_welcome():
                time.sleep(0.5)
                self.send_command(f"CHAT:\u202B{welcome_msg}\u200F\u202C\n")

            threading.Thread(target=send_delayed_welcome, daemon=True).start()

        except Exception as e:
            print(f"[Client {self.ip}] Failed to initialize Gemini AI: {e}")
            self.chat_history.append(f"מערכת: הפעלת הבינה המלאכותית נכשלה. ממתין לטכנאי.")
            self.state = "NEEDS_HELP"
            db.log_client_connection(self.ip, "NEEDS_HELP")

    def listen_for_client_messages(self):
        buffer = ""
        try:
            while self.is_active and self.cmd_sock:
                data = self.cmd_sock.recv(1024).decode()
                if not data: break
                buffer += data
                
                while '\n' in buffer:
                    msg, buffer = buffer.split('\n', 1)
                    
                    if msg.startswith("CMD:HELP_REQ"):
                        if self.state != "TECH_CONTROL":
                            self.state = "NEEDS_HELP"
                            db.log_client_connection(self.ip, "NEEDS_HELP")
                        alert_msg = f"לקוח {self.ip} לחץ על כפתור 'בקשת עזרה'!"
                        self.chat_history.append(f"מערכת: הלקוח ביקש עזרת טכנאי.")
                        if self.alert_callback:
                            self.alert_callback(alert_msg)
                        
                        if hasattr(self, 'refresh_gui_callback') and self.refresh_gui_callback:
                            self.refresh_gui_callback()
                        continue

                    if msg.startswith("CMD:REVOKE_CONTROL"):
                        self.state = "NEEDS_HELP"
                        db.log_client_connection(self.ip, "NEEDS_HELP")
                        alert_msg = f"⚠️ הלקוח {self.ip} הפסיק לך את השליטה!"
                        self.chat_history.append("מערכת: הלקוח לחץ על מקש מצוקה והחזיר לעצמו את השליטה.")
                        if self.alert_callback:
                            self.alert_callback(alert_msg)
                            
                        if hasattr(self, 'revoke_gui_callback') and self.revoke_gui_callback:
                            self.revoke_gui_callback(self.ip)
                        continue

                    if msg.startswith("CHAT:"):
                        client_text = msg[5:].strip("\u202B\u202C\u200F") 
                        formatted_msg = f"לקוח: {client_text}"
                        
                        self.chat_history.append(formatted_msg)
                        db.save_chat_message("Client", self.db_id, 1, client_text)
                        
                        if self.gui_callback:
                            self.gui_callback(self.ip, formatted_msg)
                        
                        if self.state != "TECH_CONTROL" and self.ai_chat:
                            self.state = "AI_TALKING"
                            db.log_client_connection(self.ip, "AI_TALKING")
                            threading.Thread(target=self.generate_ai_response, args=(client_text,), daemon=True).start()
                    
        except Exception as e:
            print(f"[Client {self.ip}] Error reading from CMD socket: {e}")
        finally:
            self.disconnect()

    def generate_ai_response(self, user_text):
        try:
            response = self.ai_chat.send_message(user_text)
            ai_text = response.text.strip()
            
            if "טכנאי אנושי" in ai_text or "מעביר כעת" in ai_text:
                if self.state != "TECH_CONTROL":
                    self.state = "NEEDS_HELP"
                    db.log_client_connection(self.ip, "NEEDS_HELP")

            db.save_chat_message("AI", 0, self.db_id, ai_text)

            formatted_history_msg = f"עוזר הבינה המלאכותית: {ai_text}"
            self.chat_history.append(formatted_history_msg)
            
            if self.gui_callback:
                self.gui_callback(self.ip, formatted_history_msg)

            lines = [line.strip() for line in ai_text.split('\n') if line.strip()]
            first_line = True
            for clean_line in lines:
                if first_line:
                    self.send_command(f"CHAT:\u202Bעוזר הבינה המלאכותית: {clean_line}\u200F\u202C\n")
                    first_line = False
                else:
                    self.send_command(f"CHAT:\u202B{clean_line}\u200F\u202C\n")
                    
        except Exception as e:
            print(f"[Client {self.ip}] Gemini AI Error/Quota: {e}")
            error_msg = "מערכת: עוזר ה-AI אינו זמין כרגע. מעביר אותך אוטומטית לטכנאי אנושי..."
            self.chat_history.append(error_msg)
            if self.gui_callback:
                self.gui_callback(self.ip, error_msg)
            self.send_command(f"CHAT:\u202B{error_msg}\u200F\u202C\n")
            
            if self.state != "TECH_CONTROL":
                self.state = "NEEDS_HELP"
                db.log_client_connection(self.ip, "NEEDS_HELP")
            if self.alert_callback:
                self.alert_callback(f"🚨 ה-AI של לקוח {self.ip} נחסם! הועבר לטכנאי.")
            if hasattr(self, 'refresh_gui_callback') and self.refresh_gui_callback:
                self.refresh_gui_callback()

    def generate_incident_summary(self):
        if not self.ai_client: return "אין נתוני AI זמינים."
        try:
            history_str = "\n".join(self.chat_history)
            prompt = f"Based on the following conversation between a user and an AI assistant, provide a short, bulleted technical summary of the problem for a human technician in Hebrew:\n\n{history_str}"
            
            response = self.ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            summary_text = response.text.strip()
            db.update_client_ai_summary(self.ip, summary_text)
            return summary_text
        except Exception as e:
            return "⚠️ לא ניתן לייצר סיכום אוטומטי כרגע (חריגה ממכסת ה-AI או שגיאה)."

    def handle_screen_stream(self):
        try:
            while self.is_active and self.screen_sock:
                size_data = self.screen_sock.recv(4)
                if not size_data: break
                size = int.from_bytes(size_data, byteorder='big')
                
                buffer = b""
                while len(buffer) < size:
                    packet = self.screen_sock.recv(size - len(buffer))
                    if not packet: break
                    buffer += packet

                try:
                    image = Image.open(io.BytesIO(buffer))
                    with self.lock:
                        self.latest_image = image
                except:
                    continue
        except Exception:
            pass
        finally:
            self.disconnect()

    def send_command(self, msg):
        try:
            if self.is_active and self.cmd_sock:
                self.cmd_sock.sendall(msg.encode())
        except Exception:
            self.disconnect()

    def disconnect(self):
        if not self.is_active: return
        self.is_active = False
        print(f"[Client {self.ip}] Disconnected.")
        try:
            if self.cmd_sock: self.cmd_sock.close()
            if self.screen_sock: self.screen_sock.close()
        except:
            pass

class OmniServer:
    def __init__(self, host="0.0.0.0", cmd_port=1337, screen_port=1338):
        self.host = host
        self.cmd_port = cmd_port
        self.screen_port = screen_port
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.running = False
        
        self.gui_chat_callback = None
        self.gui_alert_callback = None
        self.gui_revoke_callback = None
        self.gui_refresh_callback = None

    def start(self):
        self.running = True
        threading.Thread(target=self.accept_cmd_connections, daemon=True).start()
        threading.Thread(target=self.accept_screen_connections, daemon=True).start()

    def accept_cmd_connections(self):
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) 
        raw_sock.bind((self.host, self.cmd_port))
        raw_sock.listen(5)
        
        secure_sock = SERVER_CONTEXT.wrap_socket(raw_sock, server_side=True)
        
        while self.running:
            try:
                conn, addr = secure_sock.accept()
                ip = addr[0]
                with self.clients_lock:
                    if ip in self.clients:
                        self.clients[ip].disconnect()
                    
                    handler = ClientHandler(ip, conn, self.on_chat_received, self.on_alert_received)
                    handler.revoke_gui_callback = self.on_revoke_received
                    handler.refresh_gui_callback = self.on_refresh_received
                    
                    self.clients[ip] = handler
            except Exception:
                pass

    def accept_screen_connections(self):
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw_sock.bind((self.host, self.screen_port))
        raw_sock.listen(5)
        
        secure_sock = SERVER_CONTEXT.wrap_socket(raw_sock, server_side=True)
        
        while self.running:
            try:
                conn, addr = secure_sock.accept()
                ip = addr[0]
                with self.clients_lock:
                    if ip in self.clients:
                        self.clients[ip].screen_sock = conn
                        threading.Thread(target=self.clients[ip].handle_screen_stream, daemon=True).start()
                    else:
                        conn.close()
            except Exception:
                pass
    
    def on_revoke_received(self, ip):
        if self.gui_revoke_callback:
            self.gui_revoke_callback(ip)

    def on_refresh_received(self):
        if self.gui_refresh_callback:
            self.gui_refresh_callback()

    def on_chat_received(self, ip, msg):
        if self.gui_chat_callback:
            self.gui_chat_callback(ip, msg)
            
    def on_alert_received(self, msg):
        if self.gui_alert_callback:
            self.gui_alert_callback(msg)

    def stop(self):
        self.running = False
        with self.clients_lock:
            for client in self.clients.values():
                client.disconnect()


class OmniDeskGUI:
    def __init__(self, root, server):
        self.root = root
        self.server = server
        self.selected_ip = None
        
        self.server.gui_chat_callback = self.handle_incoming_chat
        self.server.gui_alert_callback = self.show_alert_banner

        self.server.gui_revoke_callback = lambda ip: self.root.after(0, self.force_stop_takeover, ip)
        self.server.gui_refresh_callback = lambda: self.root.after(0, self.update_gui_controls_by_mode)
        
        self.bg_color = "#151e27"       
        self.sidebar_bg = "#1b2836"     
        self.panel_bg = "#253746"       
        self.text_color = "#ffffff"     
        self.accent_color = "#00a8ff"   
        self.red_btn = "#e84118"
        
        self.root.title("OmniDesk - Pro Technician Hub")
        self.root.geometry("1200x750")
        self.root.configure(bg=self.bg_color)
        
        self.main_font = font.Font(family="Helvetica", size=10)
        self.bold_font = font.Font(family="Helvetica", size=11, weight="bold")
        
        self.alert_banner = tk.Label(self.root, text="", bg="#c23616", fg="white", font=self.bold_font, pady=8)
        
        self.sidebar = tk.Frame(self.root, width=300, bg=self.sidebar_bg)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        
        self.main_area = tk.Frame(self.root, bg="#000000")
        self.main_area.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        tk.Label(self.sidebar, text="רשימת לקוחות וסטטוס", bg=self.sidebar_bg, fg=self.text_color, font=self.bold_font).pack(pady=(15,5))
        
        legend_frame = tk.Frame(self.sidebar, bg=self.sidebar_bg)
        legend_frame.pack(pady=(0,10))
        tk.Label(legend_frame, text="● בשיחה ", bg=self.sidebar_bg, fg="#fbc531", font=self.bold_font).pack(side=tk.RIGHT)
        tk.Label(legend_frame, text="● ממתין ", bg=self.sidebar_bg, fg="#e84118", font=self.bold_font).pack(side=tk.RIGHT)
        tk.Label(legend_frame, text="● בשליטה ", bg=self.sidebar_bg, fg="#4cd137", font=self.bold_font).pack(side=tk.RIGHT)

        self.clients_listbox = tk.Listbox(self.sidebar, bg=self.panel_bg, selectbackground=self.accent_color, height=8, font=self.bold_font, borderwidth=0, highlightthickness=1, highlightcolor=self.accent_color)
        self.clients_listbox.pack(fill=tk.X, padx=15, pady=5)
        self.clients_listbox.bind('<<ListboxSelect>>', self.on_client_select)
        
        self.status_label = tk.Label(self.sidebar, text="סטטוס: לא נבחר לקוח", bg=self.sidebar_bg, fg="#fbc531", font=self.bold_font)
        self.status_label.pack(anchor=tk.CENTER, pady=10)

        self.chat_frame = tk.Frame(self.sidebar, bg=self.sidebar_bg)
        self.chat_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        
        tk.Label(self.chat_frame, text="צ'אט", bg=self.sidebar_bg, fg=self.text_color, font=self.bold_font).pack(anchor=tk.E)
        
        self.chat_log = tk.Text(self.chat_frame, bg=self.panel_bg, fg=self.text_color, state=tk.DISABLED, font=self.main_font, height=12, borderwidth=0)
        self.chat_log.pack(fill=tk.BOTH, expand=True, pady=5)
        
        self.chat_log.tag_configure("rtl", justify="right") 
        self.chat_log.tag_configure("sender", underline=True, font=self.bold_font, foreground="#fbc531")

        self.takeover_btn = tk.Button(self.chat_frame, text="קח שליטה (נתק AI)", command=self.take_over_client, bg=self.red_btn, fg="white", font=self.bold_font, borderwidth=0, cursor="hand2", pady=5)
        self.takeover_btn.pack(fill=tk.X, pady=4)

        self.kick_btn = tk.Button(self.chat_frame, text="נתק וסגור טיפול", command=self.kick_selected_client, bg="#8e44ad", fg="white", font=self.bold_font, borderwidth=0, cursor="hand2", pady=5)
        self.kick_btn.pack(fill=tk.X, pady=4)
        
        self.is_controlling = False 
        
        self.toggle_ctrl_btn = tk.Button(self.chat_frame, text="אפשר שליטה בעכבר ומקלדת", command=self.toggle_control, bg=self.accent_color, fg="white", font=self.bold_font, borderwidth=0, cursor="hand2", pady=5)
        self.toggle_ctrl_btn.pack(fill=tk.X, pady=4)

        self.chat_entry = tk.Entry(self.chat_frame, font=self.main_font, bg=self.panel_bg, fg="white", borderwidth=1, justify="right")
        self.chat_entry.pack(fill=tk.X, pady=(5,15), ipady=5)
        self.chat_entry.bind("<Return>", self.send_chat_message)
        
        self.screen_canvas = tk.Canvas(self.main_area, bg="#000000", cursor="crosshair", highlightthickness=0)
        self.screen_canvas.pack(fill=tk.BOTH, expand=True)
        self.mouse_dot = self.screen_canvas.create_oval(-10, -10, -10, -10, fill="red", outline="white", tags="mouse_dot")
        
        self.screen_canvas.bind('<Motion>', self.on_mouse_move)
        self.screen_canvas.bind('<ButtonPress>', self.on_mouse_click_down)
        self.screen_canvas.bind('<ButtonRelease>', self.on_mouse_click_up)
        self.screen_canvas.bind("<MouseWheel>", self.on_mouse_scroll)
        self.root.bind('<KeyPress>', self.on_key_press)
        
        self.update_client_list()
        self.update_screen_loop()
        
        self.last_key = None
        self.last_key_time = 0

    def show_alert_banner(self, msg):
        self.alert_banner.config(text=f"🔔 {msg}")
        self.alert_banner.pack(fill=tk.X, side=tk.TOP, before=self.sidebar)
        self.root.after(5000, self.alert_banner.pack_forget)

    def on_client_select(self, event):
        selection = self.clients_listbox.curselection()
        if selection:
            item_text = self.clients_listbox.get(selection[0])
            new_ip = item_text.split(" ")[1] 
            
            if new_ip != self.selected_ip:
                if self.selected_ip:
                    with self.server.clients_lock:
                        old_client = self.server.clients.get(self.selected_ip)
                        if old_client:
                            old_client.state = "PAUSED"
                            db.log_client_connection(old_client.ip, "PAUSED")
                
                self.selected_ip = new_ip
                
                self.is_controlling = False
                self.toggle_ctrl_btn.config(text="אפשר שליטה בעכבר ומקלדת", bg=self.accent_color)
                self.screen_canvas.coords("mouse_dot", -10, -10, -10, -10)
                
                self.reload_chat_log()
                self.update_gui_controls_by_mode()

    def update_gui_controls_by_mode(self):
        client = self.get_selected_client()
        if client:
            if client.state == "TECH_CONTROL":
                self.kick_btn.config(state=tk.NORMAL)
                self.status_label.config(text="סטטוס: שליטת טכנאי", fg="#4cd137")
                self.takeover_btn.config(state=tk.DISABLED, bg="#7f8c8d")
                self.chat_entry.config(state=tk.NORMAL)
                self.toggle_ctrl_btn.config(state=tk.NORMAL, bg=self.accent_color)
            else:
                self.takeover_btn.config(state=tk.DISABLED, bg="#7f8c8d")
                self.kick_btn.config(state=tk.DISABLED, bg="#7f8c8d")
                
                if client.state == "NEEDS_HELP":
                    status_text = "סטטוס: הלקוח מבקש עזרה!"
                    color = "#e84118"
                elif client.state == "PAUSED":
                    status_text = "סטטוס: טיפול מושהה"
                    color = "#fbc531"
                else:
                    status_text = "סטטוס: בשיחה עם AI"
                    color = "#fbc531"
                
                self.status_label.config(text=status_text, fg=color)
                self.takeover_btn.config(state=tk.NORMAL, bg=self.red_btn)
                self.chat_entry.config(state=tk.DISABLED)
                
                self.is_controlling = False
                self.toggle_ctrl_btn.config(state=tk.DISABLED, text="אפשר שליטה בעכבר ומקלדת", bg="#7f8c8d")
        else:
            self.status_label.config(text="סטטוס: לא נבחר לקוח", fg="#7f8c8d")
            self.takeover_btn.config(state=tk.DISABLED, bg="#7f8c8d")
            self.kick_btn.config(state=tk.DISABLED, bg="#7f8c8d")
            self.chat_entry.config(state=tk.DISABLED)
            self.toggle_ctrl_btn.config(state=tk.DISABLED, bg="#7f8c8d")

    def take_over_client(self):
        client = self.get_selected_client()
        if client and client.state != "TECH_CONTROL":
            client.state = "TECH_CONTROL"
            db.log_client_connection(client.ip, "TECH_CONTROL")
            
            self.update_gui_controls_by_mode()
            
            client.send_command("CHAT:\u202Bמערכת: הבינה המלאכותית נותקה. טכנאי אנושי נכנס כעת לשיחה.\u200F\u202C\n")
            
            self.append_to_chat_log("\n" + "="*25)
            self.append_to_chat_log("מייצר סיכום שיחה מה-AI...")
            
            def fetch_summary_bg():
                summary = client.generate_incident_summary()
                self.root.after(0, lambda: (
                    self.append_to_chat_log(f"\n✨ סיכום הבעיה לטכנאי:\n{summary}"),
                    self.append_to_chat_log("="*25 + "\n")
                ))
            
            threading.Thread(target=fetch_summary_bg, daemon=True).start()
    
    def force_stop_takeover(self, client_ip):
        client = self.get_selected_client()
        if client and client.ip == client_ip:
            self.update_gui_controls_by_mode()
            self.append_to_chat_log("\n🛑 השליטה הופסקה על ידי הלקוח (לחיצה על F12).")
    
    def kick_selected_client(self):
        client = self.get_selected_client()
        if client:
            client.send_command("CMD:KICK\n")
            client.disconnect()
            self.selected_ip = None
            self.update_gui_controls_by_mode()
            self.show_alert_banner(f"לקוח {client.ip} נותק מהשרת.")

    def toggle_control(self):
        self.is_controlling = not self.is_controlling
        if self.is_controlling:
            self.toggle_ctrl_btn.config(text="הפסק שליטה 🛑", bg="#e84118")
        else:
            self.toggle_ctrl_btn.config(text="אפשר שליטה בעכבר ומקלדת", bg=self.accent_color)
            self.screen_canvas.coords("mouse_dot", -10, -10, -10, -10)

    def reload_chat_log(self):
        self.chat_log.config(state=tk.NORMAL)
        self.chat_log.delete("1.0", tk.END)
        if self.selected_ip:
            with self.server.clients_lock:
                client = self.server.clients.get(self.selected_ip)
                if client:
                    for msg in client.chat_history:
                        self.chat_log.insert(tk.END, msg + "\n", "rtl")
        self.chat_log.see(tk.END)
        self.chat_log.config(state=tk.DISABLED)

    def handle_incoming_chat(self, ip, msg):
        if ip == self.selected_ip:
            self.root.after(0, self.append_to_chat_log, msg)

    def append_to_chat_log(self, msg):
        self.chat_log.config(state=tk.NORMAL)
        
        clean_msg = msg.replace('\u202b', '').replace('\u202c', '').replace('\u200f', '').replace('\u200e', '')
        
        if ":" in clean_msg:
            parts = clean_msg.split(":", 1)
            sender = parts[0].strip() + ":"
            content = parts[1].strip()
            
            self.chat_log.insert(tk.END, "\u202B", "rtl")
            self.chat_log.insert(tk.END, sender, ("rtl", "sender"))
            self.chat_log.insert(tk.END, f" {content}\u202C\u200F\n", "rtl")
        else:
            self.chat_log.insert(tk.END, f"\u202B{clean_msg}\u202C\u200F\n", "rtl")

        self.chat_log.see(tk.END)
        self.chat_log.config(state=tk.DISABLED)

    def send_chat_message(self, event=None):
        msg = self.chat_entry.get()
        if msg and self.selected_ip:
            with self.server.clients_lock:
                client = self.server.clients.get(self.selected_ip)
            
            if client and client.is_active and client.state == "TECH_CONTROL":
                formatted_msg = f"טכנאי: {msg}"
                client.chat_history.append(formatted_msg)
                
                db.save_chat_message("Technician", 1, client.db_id, msg)
                
                client.send_command(f"CHAT:\u202Bטכנאי: {msg}\u200F\u202C\n")
                
                self.append_to_chat_log(formatted_msg)
                self.chat_entry.delete(0, tk.END)

    def update_client_list(self):
        current_selection = None
        sel = self.clients_listbox.curselection()
        if sel:
            current_selection = self.clients_listbox.get(sel[0]).split(" ")[1]
        elif self.selected_ip:
            current_selection = self.selected_ip

        self.clients_listbox.delete(0, tk.END)
        
        with self.server.clients_lock:
            active_clients = [c for c in self.server.clients.values() if c.is_active]
        
        idx_to_select = None
        for i, client in enumerate(active_clients):
            display_text = f"● {client.ip}"
            self.clients_listbox.insert(tk.END, display_text)
            
            if client.state == "AI_TALKING": 
                color = "#fbc531" 
                sel_bg = "#e1b12c" 
            elif client.state == "NEEDS_HELP": 
                color = "#e84118"
                sel_bg = "#c23616" 
            elif client.state == "PAUSED": 
                color = "#fbc531"
                sel_bg = "#d35400" 
            else: 
                color = "#4cd137"
                sel_bg = "#44bd32"
            
            self.clients_listbox.itemconfig(i, {
                'fg': color, 
                'selectbackground': sel_bg, 
                'selectforeground': 'white'
            })
            
            if client.ip == current_selection:
                idx_to_select = i
                
        if idx_to_select is not None:
            self.clients_listbox.select_set(idx_to_select)
        elif current_selection:
            self.selected_ip = None
            self.chat_log.config(state=tk.NORMAL)
            self.chat_log.delete("1.0", tk.END)
            self.chat_log.config(state=tk.DISABLED)
            self.screen_canvas.delete("screen")
            self.update_gui_controls_by_mode()
            
        self.root.after(2000, self.update_client_list)

    def update_screen_loop(self):
        if self.selected_ip:
            with self.server.clients_lock:
                client = self.server.clients.get(self.selected_ip)
            
            if client and client.is_active:
                with client.lock:
                    image = client.latest_image
                
                if image:
                    canvas_w = self.screen_canvas.winfo_width()
                    canvas_h = self.screen_canvas.winfo_height()
                    if canvas_w > 10 and canvas_h > 10:
                        
                        resized = image.resize((canvas_w, canvas_h), Image.LANCZOS)
                        photo = ImageTk.PhotoImage(resized)
                        
                        self.screen_canvas.delete("screen")
                        self.screen_canvas.create_image(0, 0, anchor=tk.NW, image=photo, tags="screen")
                        self.screen_canvas.image = photo
                        self.screen_canvas.lift("mouse_dot")
                        
        self.root.after(100, self.update_screen_loop)

    def get_selected_client(self):
        if not self.selected_ip: return None
        return self.server.clients.get(self.selected_ip)

    def grab_focus(self):
        self.root.focus_set()

    def on_mouse_move(self, event):
        if not self.is_controlling: return
        
        canvas_w = self.screen_canvas.winfo_width()
        canvas_h = self.screen_canvas.winfo_height()
        if canvas_w < 10 or canvas_h < 10: return

        r = 5
        self.screen_canvas.coords("mouse_dot", event.x - r, event.y - r, event.x + r, event.y + r)
        self.screen_canvas.lift("mouse_dot")

        client = self.get_selected_client()
        if client and client.state == "TECH_CONTROL":
            norm_x = event.x / canvas_w
            norm_y = event.y / canvas_h
            client.send_command(f"MOVE:{norm_x:.4f},{norm_y:.4f}\n")

    def on_mouse_click_down(self, event):
        self.grab_focus() 
        if not self.is_controlling: return
        client = self.get_selected_client()
        if client and client.state == "TECH_CONTROL":
            canvas_w = self.screen_canvas.winfo_width()
            canvas_h = self.screen_canvas.winfo_height()
            norm_x = event.x / canvas_w
            norm_y = event.y / canvas_h
            btn = "left" if event.num == 1 else "middle" if event.num == 2 else "right"
            client.send_command(f"CLICK:{norm_x:.4f},{norm_y:.4f},{btn},DOWN\n")

    def on_mouse_click_up(self, event):
        if not self.is_controlling: return
        client = self.get_selected_client()
        if client and client.state == "TECH_CONTROL":
            canvas_w = self.screen_canvas.winfo_width()
            canvas_h = self.screen_canvas.winfo_height()
            norm_x = event.x / canvas_w
            norm_y = event.y / canvas_h
            btn = "left" if event.num == 1 else "middle" if event.num == 2 else "right"
            client.send_command(f"CLICK:{norm_x:.4f},{norm_y:.4f},{btn},UP\n")

    def on_mouse_scroll(self, event):
        if self.is_controlling and self.selected_ip:
            direction = "up" if event.delta > 0 else "down"
            
            with self.server.clients_lock:
                client = self.server.clients.get(self.selected_ip)
                
                if client and client.is_active and client.cmd_sock:
                    try:
                        client.cmd_sock.sendall(f"SCROLL:{direction}\n".encode())
                    except:
                        pass

    def on_key_press(self, event):
        if not self.is_controlling: return
        if self.root.focus_get() == self.chat_entry: return 
        
        current_time = time.time()
        if event.keysym == self.last_key and (current_time - self.last_key_time) < 0.08:
            return
            
        self.last_key = event.keysym
        self.last_key_time = current_time

        client = self.get_selected_client()
        if client and self.selected_ip and client.state == "TECH_CONTROL":
            key_map = {"Return": "Key.enter", "BackSpace": "Key.backspace", "space": "Key.space"}
            key_str = key_map.get(event.keysym, event.char)
            if key_str: 
                client.send_command(f"{key_str}\n")

def handle_exit(sig, frame):
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)

if __name__ == "__main__":
    if "GEMINI_API_KEY" not in os.environ and not os.path.exists("api_key.txt"):
        print("[Warning] No API key method found. Please ensure 'api_key.txt' exists.")

    login_root = tk.Tk()
    login_root.title("OmniDesk - Auth Hub")
    login_root.geometry("340x260")
    login_root.resizable(False, False)
    
    tk.Label(login_root, text="שם משתמש:", font=("Arial", 11)).pack(pady=(15, 2))
    user_ent = tk.Entry(login_root, font=("Arial", 11), width=25, justify="center")
    user_ent.pack()
    
    tk.Label(login_root, text="סיסמה:", font=("Arial", 11)).pack(pady=(10, 2))
    pass_ent = tk.Entry(login_root, show="*", font=("Arial", 11), width=25, justify="center")
    pass_ent.pack()
    
    error_lbl = tk.Label(login_root, text="", font=("Arial", 10, "bold"))
    error_lbl.pack(pady=5)

    is_authenticated = [False] 

    def try_login():
        username = user_ent.get().strip()
        password = pass_ent.get().strip()
        
        if not username or not password:
            error_lbl.config(text="נא למלא את כל השדות!", fg="red")
            return
            
        tech_id = db.validate_technician(username, password)
        if tech_id:
            is_authenticated[0] = True
            login_root.destroy()
        else:
            error_lbl.config(text="שם משתמש או סיסמה שגויים!", fg="red")

    def try_signup():
        """ פונקציית רישום (Signup) של טכנאי חדש למערכת לצמיתות """
        username = user_ent.get().strip()
        password = pass_ent.get().strip()
        
        if not username or not password:
            error_lbl.config(text="נא להזין שם וסיסמה כדי להירשם!", fg="red")
            return
            
        if len(password) < 4:
            error_lbl.config(text="הסיסמה חייבת להכיל לפחות 4 תווים!", fg="red")
            return
            
        success = db.register_technician(username, password)
        if success:
            error_lbl.config(text=f"הטכנאי '{username}' נרשם! כעת לחץ התחבר", fg="green")
        else:
            error_lbl.config(text="שם משתמש תפוס! בחר שם אחר.", fg="red")

    btn_frame = tk.Frame(login_root)
    btn_frame.pack(pady=10)

    tk.Button(btn_frame, text="התחבר", command=try_login, width=12, bg="#2ecc71", fg="white", font=("Arial", 10, "bold"), cursor="hand2").grid(row=0, column=0, padx=5)
    tk.Button(btn_frame, text="הרשם כחדש", command=try_signup, width=12, bg="#3498db", fg="white", font=("Arial", 10, "bold"), cursor="hand2").grid(row=0, column=1, padx=5)

    login_root.mainloop()

    if is_authenticated[0]:
        server = OmniServer(host="0.0.0.0")
        server.start()

        root = tk.Tk()
        app = OmniDeskGUI(root, server)

        def on_closing():
            server.stop()
            root.destroy()
            sys.exit(0)

        root.protocol("WM_DELETE_WINDOW", on_closing)
        root.mainloop()
    else:
        print("[System] Auth failed or window closed. Exiting.")
        sys.exit(0)
