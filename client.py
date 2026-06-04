import socket
import threading
import mss
import io
from PIL import Image
import tkinter as tk
from tkinter import font
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key
import time
import ssl
import pyautogui
import keyboard

SERVER_IP = "192.168.68.53" 
CMD_PORT = 1337
SCREEN_PORT = 1338

CLIENT_CONTEXT = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
CLIENT_CONTEXT.check_hostname = False
CLIENT_CONTEXT.verify_mode = ssl.CERT_NONE

class OmniClient:
    def __init__(self, server_ip, cmd_port, screen_port):
        self.server_ip = server_ip
        self.cmd_port = cmd_port
        self.screen_port = screen_port
        self.cmd_sock = None
        self.screen_sock = None
        self.running = False
        self.control_paused = False
        
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        
        self.sct = mss.mss()
        self.monitor = self.sct.monitors[1]
        self.screen_width = self.monitor["width"]
        self.screen_height = self.monitor["height"]
        
        self.gui_chat_callback = None

        keyboard.add_hotkey('F12', self.toggle_control)
    def connect(self):
        try:
            raw_cmd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_sock = CLIENT_CONTEXT.wrap_socket(raw_cmd, server_hostname=self.server_ip)
            self.cmd_sock.connect((self.server_ip, self.cmd_port))
            
            raw_screen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.screen_sock = CLIENT_CONTEXT.wrap_socket(raw_screen, server_hostname=self.server_ip)
            self.screen_sock.connect((self.server_ip, self.screen_port))
            
            self.running = True
            threading.Thread(target=self.receive_commands, daemon=True).start()
            threading.Thread(target=self.send_screen, daemon=True).start()
            print("[Client] SECURE connection established!")
            return True
        except Exception as e:
            print(f"[Client] SECURE Connection failed: {e}")
            return False

    def receive_commands(self):
        buffer = ""
        while self.running:
            try:
                data = self.cmd_sock.recv(1024).decode()
                if not data: break
                buffer += data
                
                while '\n' in buffer:
                    cmd, buffer = buffer.split('\n', 1)
                    self.process_command(cmd)
            except:
                break
        self.disconnect()

    def process_command(self, cmd):
        # 1. פקודות מערכת וצ'אט - תמיד עוברות (כדי שהצ'אט יעבוד גם בהשהיה)
        if cmd.startswith("CHAT:"):
            msg = cmd[5:].strip()
            clean_msg = msg.strip("\u202B\u202C\u200F")
            if self.gui_chat_callback:
                self.gui_chat_callback(clean_msg)
            return
            
        elif cmd.startswith("CMD:KICK"):
            print("[Client] הטכנאי סגר את הטיפול. התוכנה נסגרת.")
            import os
            os._exit(0) 

        # 2. חסימה הרמטית! אם הלקוח השהה את השליטה - מתעלמים מכל שאר הפקודות
        if getattr(self, 'control_paused', False):
            return

        # 3. פקודות השתלטות (עכבר ומקלדת) - מתבצעות רק אם לא בהשהיה
        if cmd.startswith("MOVE:"):
            _, coords = cmd.split(":")
            nx, ny = map(float, coords.split(","))
            self.mouse.position = (int(nx * self.screen_width), int(ny * self.screen_height))
        elif cmd.startswith("CLICK:"):
            _, params = cmd.split(":")
            nx, ny, btn_str, action = params.split(",")
            self.mouse.position = (int(float(nx) * self.screen_width), int(float(ny) * self.screen_height))
            btn = Button.left if btn_str == "left" else Button.middle if btn_str == "middle" else Button.right
            if action == "DOWN":
                self.mouse.press(btn)
            elif action == "UP":
                self.mouse.release(btn)
        elif cmd.startswith("SCROLL:"):
            direction = cmd.split(":")[1]
            clicks = 100 if direction == "up" else -100
            pyautogui.scroll(clicks)
        else:
            self.handle_keypress(cmd)

    def handle_keypress(self, key_str):
        try:
            if key_str.startswith("Key."):
                key = getattr(Key, key_str.split(".")[1])
                self.keyboard.press(key)
                self.keyboard.release(key)
            else:
                self.keyboard.press(key_str)
                self.keyboard.release(key_str)
        except Exception as e:
            pass

    def send_screen(self):
        while self.running:
            try:
                sct_img = self.sct.grab(self.monitor)
                img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                
                img = img.resize((int(img.width * 0.7), int(img.height * 0.7)), Image.LANCZOS)
                
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG', quality=60)
                img_bytes = img_byte_arr.getvalue()
                
                size_bytes = len(img_bytes).to_bytes(4, byteorder='big')
                self.screen_sock.sendall(size_bytes + img_bytes)
                time.sleep(0.08)
            except Exception:
                break
        self.disconnect()

    def send_chat(self, msg):
        if self.running and self.cmd_sock:
            try:
                self.cmd_sock.sendall(f"CHAT:\u202B{msg}\u200F\u202C\n".encode())
            except:
                self.disconnect()

    def send_help_request(self):
        if self.running and self.cmd_sock:
            try:
                self.cmd_sock.sendall(b"CMD:HELP_REQ\n")
            except:
                self.disconnect()
    
    def toggle_control(self):
        if not self.running or not self.cmd_sock:
            return
            
        import time
        # מנגנון Cooldown למניעת לחיצות כפולות בטעות (Bouncing)
        current_time = time.time()
        if not hasattr(self, 'last_toggle_time'):
            self.last_toggle_time = 0
            
        # אם לא עברה לפחות שנייה מאז הלחיצה הקודמת, התעלם
        if current_time - self.last_toggle_time < 1.0:
            return
            
        self.last_toggle_time = current_time

        try:
            if not getattr(self, 'control_paused', False):
                # הפסקת שליטה
                self.control_paused = True # עוצר מיד בצד הלקוח את כל התנועות!
                self.cmd_sock.sendall(b"CMD:REVOKE_CONTROL\n")
                print("[Client] שליטה הופסקה על ידי הלקוח.")
                if self.gui_chat_callback:
                    self.gui_chat_callback("מערכת: עצרת את שליטת הטכנאי. לחץ F12 שוב כדי לאפשר לו לחזור.")
            else:
                # חידוש שליטה
                self.control_paused = False # פותח חזרה את התנועות
                self.cmd_sock.sendall(b"CMD:HELP_REQ\n")
                print("[Client] נשלחה בקשת חידוש שליטה.")
                if self.gui_chat_callback:
                    self.gui_chat_callback("מערכת: אישרת לטכנאי לחזור לשלוט. ממתין...")
        except Exception as e:
            print(f"Error toggling control: {e}")

    def disconnect(self):
        self.running = False
        try:
            if self.cmd_sock: self.cmd_sock.close()
            if self.screen_sock: self.screen_sock.close()
        except:
            pass
        print("[Client] Disconnected.")

class ClientGUI:
    def __init__(self, root, client):
        self.root = root
        self.client = client
        self.client.gui_chat_callback = self.append_chat
        
        self.bg_color = "#f4f6f9"     
        self.chat_bg = "#ffffff"      
        self.text_color = "#2c3e50"   
        self.accent_color = "#00a8ff" 
        self.red_btn = "#e84118"      
        
        self.root.title("OmniDesk - תמיכה מרחוק")
        self.root.geometry("450x650")
        self.root.configure(bg=self.bg_color)
        
        self.main_font = font.Font(family="Helvetica", size=11)
        self.bold_font = font.Font(family="Helvetica", size=12, weight="bold")

        header = tk.Frame(self.root, bg=self.accent_color, pady=15)
        header.pack(fill=tk.X)
        tk.Label(header, text="OmniDesk Support", bg=self.accent_color, fg="white", font=font.Font(family="Helvetica", size=16, weight="bold")).pack()

        content = tk.Frame(self.root, bg=self.bg_color, padx=20, pady=10)
        content.pack(fill=tk.BOTH, expand=True)

        self.status_lbl = tk.Label(content, text="סטטוס: ממתין לחיבור...", bg=self.bg_color, fg="#7f8c8d", font=self.bold_font)
        self.status_lbl.pack(pady=10)
        self.panic_info_lbl = tk.Label(
            content, 
            text="🔒 עצירת/חידוש שליטה: לחץ על מקש F12 במקלדת\nכדי לעצור את הטכנאי בכל שלב, ולחץ שוב כדי לאפשר לו להמשיך.", 
            bg=self.bg_color, 
            fg="#e67e22", 
            font=font.Font(family="Helvetica", size=10, weight="bold"),
            justify="center"
        )
        self.panic_info_lbl.pack(pady=5)
        self.panic_info_lbl.pack(pady=5)
        chat_frame = tk.Frame(content, bg=self.chat_bg, highlightbackground="#dcdde1", highlightthickness=1)
        chat_frame.pack(fill=tk.BOTH, expand=True, pady=10)

        self.chat_log = tk.Text(chat_frame, state=tk.DISABLED, bg=self.chat_bg, fg=self.text_color, font=self.main_font, borderwidth=0, padx=10, pady=10)
        self.chat_log.pack(fill=tk.BOTH, expand=True)
        self.chat_log.tag_configure("rtl", justify="right")
        self.chat_log.tag_configure("sender", underline=True, font=self.bold_font, foreground="#00a8ff")

        self.help_btn = tk.Button(content, text="אני צריך עזרת טכנאי (לחץ כאן)", bg=self.red_btn, fg="white", font=self.bold_font, borderwidth=0, cursor="hand2", pady=8, command=self.request_help)
        self.help_btn.pack(fill=tk.X, pady=5)

        input_frame = tk.Frame(content, bg=self.bg_color)
        input_frame.pack(fill=tk.X, pady=5)

        self.msg_entry = tk.Entry(input_frame, font=self.main_font, borderwidth=1, justify="right", relief="solid")
        self.msg_entry.pack(side=tk.RIGHT, fill=tk.X, expand=True, ipady=6, padx=(5, 0))
        self.msg_entry.bind("<Return>", self.send_msg)

        self.send_btn = tk.Button(input_frame, text="שלח", command=self.send_msg, bg=self.accent_color, fg="white", font=self.bold_font, borderwidth=0, cursor="hand2", padx=15)
        self.send_btn.pack(side=tk.LEFT, fill=tk.Y)

        self.connect_to_server()

    def connect_to_server(self):
        if self.client.connect():
            self.status_lbl.config(text="מחובר לשרת בהצלחה", fg="#4cd137")
        else:
            self.status_lbl.config(text="שגיאת חיבור. מנסה שוב...", fg="#e84118")
            self.root.after(3000, self.connect_to_server)

    def append_chat(self, msg):
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

    def send_msg(self, event=None):
        msg = self.msg_entry.get()
        if msg:
            self.client.send_chat(msg)
            self.append_chat(f"אני: {msg}")
            self.msg_entry.delete(0, tk.END)

    def request_help(self):
        self.client.send_help_request()
        self.append_chat("מערכת: בקשתך נשלחה לטכנאי. אנא המתן...")
        self.help_btn.config(state=tk.DISABLED, bg="#bdc3c7", text="הבקשה נשלחה")
        self.root.after(10000, lambda: self.help_btn.config(state=tk.NORMAL, bg=self.red_btn, text="אני צריך עזרת טכנאי (לחץ כאן)"))


if __name__ == "__main__":
    client = OmniClient(SERVER_IP, CMD_PORT, SCREEN_PORT)
    root = tk.Tk()
    app = ClientGUI(root, client)
    
    def on_closing():
        client.disconnect()
        root.destroy()
        
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()