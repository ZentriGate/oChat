#!/usr/bin/env python3
import threading
import requests
import base64
import os
import json
from datetime import datetime
from tkinter import (Tk, Text, Button, END, LEFT, RIGHT, BOTH, X, Frame, ttk,
                     Scrollbar, VERTICAL, Y, StringVar, filedialog, messagebox,
                     Menu, Toplevel)
from PIL import Image, ImageTk

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama2"  # or any Ollama model installed locally
DEFAULT_SYSTEM_PROMPT = ""  # empty = no system prompt; set one via the Session menu

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")  # chat history is saved here


def _now_iso():
    """Current local time as a sortable, human-readable string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _session_path(session_id):
    """Return the file path for a session id."""
    return os.path.join(SESSIONS_DIR, f"{session_id}.json")

class oChatGUI:
    def __init__(self, root):
        self.root = root
        root.title("oChat - Image Support")
        root.geometry("900x750")  # Slightly larger
        root.minsize(500, 500)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Session / history state
        self._session_meta = []
        self.session_id = None
        self.session_title = "New Chat"
        self.session_created_at = ""
        self.system_prompt = DEFAULT_SYSTEM_PROMPT

        # Menu bar: File (export) and Session (history / system prompt)
        menubar = Menu(root)
        file_menu = Menu(menubar, tearoff=0)
        file_menu.add_command(label="Export as Markdown…", command=lambda: self.export_chat("markdown"))
        file_menu.add_command(label="Export as JSON…", command=lambda: self.export_chat("json"))
        file_menu.add_command(label="Export as Text…", command=lambda: self.export_chat("text"))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        session_menu = Menu(menubar, tearoff=0)
        session_menu.add_command(label="New Session", command=self.new_session)
        session_menu.add_command(label="Set System Prompt…", command=self.set_system_prompt)
        session_menu.add_separator()
        session_menu.add_command(label="Delete Current Session…", command=self.delete_session)
        menubar.add_cascade(label="Session", menu=session_menu)
        root.config(menu=menubar)

        # Configure ttk style for better visibility
        style = ttk.Style()
        style.theme_use('clam')  # Use clam theme for better contrast

        # Main container frames
        top_frame = Frame(root)
        top_frame.pack(fill=X, padx=10, pady=5)
        
        # Model selection frame
        model_frame = Frame(top_frame)
        model_frame.pack(fill=X, pady=2)
        
        ttk.Label(model_frame, text="Model:", font=("Arial", 10, "bold")).pack(side=LEFT, padx=(0, 5))
        
        self.model_var = StringVar(value=DEFAULT_MODEL)
        self.model_combo = ttk.Combobox(model_frame, textvariable=self.model_var, state="readonly", width=30)
        # Populated by refresh_models() after the UI is fully built (see end of __init__).
        # Calling get_models() here used to crash startup whenever Ollama wasn't running,
        # because its error path calls update_status(), and status_label doesn't exist yet.
        self.model_combo['values'] = [DEFAULT_MODEL]
        self.model_combo.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
        
        # Refresh models button
        refresh_button = Button(model_frame, text="↻ Refresh", command=self.refresh_models)
        refresh_button.pack(side=RIGHT)

        # Session management row
        session_frame = Frame(top_frame)
        session_frame.pack(fill=X, pady=2)

        ttk.Label(session_frame, text="Session:", font=("Arial", 9)).pack(side=LEFT, padx=(0, 5))
        self.session_title_var = StringVar(value="New Chat")
        self.session_combo = ttk.Combobox(session_frame, textvariable=self.session_title_var,
                                          state="readonly", width=28)
        self.session_combo.pack(side=LEFT, fill=X, expand=True, padx=(0, 5))
        self.session_combo.bind("<<ComboboxSelected>>", self._on_session_selected)

        new_session_button = Button(session_frame, text="New", command=self.new_session, width=6)
        new_session_button.pack(side=LEFT, padx=(0, 5))

        delete_session_button = Button(session_frame, text="Delete", command=self.delete_session, width=8)
        delete_session_button.pack(side=LEFT)

        self.refresh_session_list()  # populate the dropdown from the sessions/ folder

        # Image attachment frame - made more compact
        image_frame = Frame(top_frame)
        image_frame.pack(fill=X, pady=2)
        
        ttk.Label(image_frame, text="📎 Image:", font=("Arial", 9)).pack(side=LEFT, padx=(0, 5))
        
        self.image_path_var = StringVar()
        self.image_path_var.set("None")
        
        self.image_label = ttk.Label(image_frame, textvariable=self.image_path_var, font=("Arial", 9), foreground="gray", width=30)
        self.image_label.pack(side=LEFT, padx=(0, 5))
        
        attach_button = Button(image_frame, text="Attach", command=self.attach_image, width=8)
        attach_button.pack(side=LEFT, padx=(0, 5))
        
        clear_image_button = Button(image_frame, text="Clear", command=self.clear_image, width=8)
        clear_image_button.pack(side=LEFT)
        
        # Image preview (small thumbnail) - hidden by default
        self.preview_frame = Frame(root, height=0)
        self.preview_frame.pack(fill=X, padx=10, pady=0)
        self.preview_label = ttk.Label(self.preview_frame, text="")
        self.preview_label.pack(side=LEFT)
        self.preview_visible = False

        # Conversation display area with scrollbar
        chat_frame = Frame(root)
        chat_frame.pack(fill=BOTH, expand=True, padx=10, pady=5)
        
        self.chat_text = Text(chat_frame, wrap='word', state='disabled', font=("Arial", 10), bg="#f5f5f5", fg="#000000")  # Added fg color
        self.scrollbar = Scrollbar(chat_frame, orient=VERTICAL, command=self.chat_text.yview)
        self.chat_text.configure(yscrollcommand=self.scrollbar.set)
        
        self.scrollbar.pack(side=RIGHT, fill=Y)
        self.chat_text.pack(side=LEFT, fill=BOTH, expand=True)

        # Input area with multi-line support - larger with proper colors
        input_container = Frame(root)
        input_container.pack(fill=X, padx=10, pady=5)
        
        # Input text area (multi-line) with proper colors
        self.input_text = Text(
            input_container, 
            font=("Arial", 10), 
            height=4, 
            wrap='word', 
            bg="#ffffff",      # White background
            fg="#000000",      # Black text
            insertbackground="#000000",  # Black cursor
            selectbackground="#b3d9ff",  # Light blue selection
            relief="solid",    # Add border
            borderwidth=1
        )
        self.input_text.pack(side=LEFT, fill=BOTH, expand=True, padx=(0, 10))
        
        # Add scrollbar for input
        input_scrollbar = Scrollbar(input_container, orient=VERTICAL, command=self.input_text.yview)
        self.input_text.configure(yscrollcommand=input_scrollbar.set)
        input_scrollbar.pack(side=LEFT, fill=Y)
        
        # Button frame (right side)
        button_frame = Frame(input_container)
        button_frame.pack(side=RIGHT, fill=Y)
        
        send_button = Button(button_frame, text="Send", command=self.on_send, width=10, height=2, bg="#4CAF50", fg="white")
        send_button.pack(pady=(0, 5))
        
        clear_input_button = Button(button_frame, text="Clear", command=self.clear_input, width=10)
        clear_input_button.pack()

        # Status bar
        self.status_frame = Frame(root)
        self.status_frame.pack(fill=X, padx=10, pady=(0, 5))
        self.status_label = ttk.Label(self.status_frame, text="Ready", font=("Arial", 9), foreground="#333333")
        self.status_label.pack(side=LEFT)

        # Bind keyboard shortcuts. A single handler is used because Tk's modifier-less
        # "<Return>" pattern also matches Shift+Return; checking event.state lets us
        # route Enter (send) vs Shift+Enter (newline) without double-firing either.
        self.input_text.bind('<Return>', self.handle_return)

        # Conversation history (list of dicts with role/message)
        self.conversation = []
        
        # Store image data
        self.current_image_base64 = None
        self.current_image_path = None
        
        # Set focus to input
        self.input_text.focus_set()

        # Fetch available models once the UI is fully constructed. Doing this in
        # __init__ before self.status_label existed crashed the app with
        # AttributeError whenever Ollama wasn't running yet.
        self.root.after(0, self.refresh_models)

    def handle_return(self, event):
        """Enter sends the message; Shift+Enter inserts a newline instead."""
        if event.state & 0x0001:  # Shift key is held down
            self.input_text.insert("insert", "\n")
        else:
            self.on_send()
        return "break"  # Prevent the default Text-widget behavior

    def clear_input(self):
        """Clear the input text area"""
        self.input_text.delete('1.0', END)

    def get_models(self):
        """Fetch available models from Ollama"""
        try:
            r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
            if r.status_code == 200:
                data = r.json()
                models = [model["name"] for model in data.get("models", [])]
                if models:
                    return models
                else:
                    self.update_status("No models found. Please pull a model with 'ollama pull <model>'")
                    return [DEFAULT_MODEL]
        except requests.exceptions.ConnectionError:
            self.update_status("⚠️ Cannot connect to Ollama. Make sure it's running.")
            print("Error: Cannot connect to Ollama. Make sure Ollama is running.")
        except Exception as e:
            self.update_status(f"⚠️ Error: {str(e)[:50]}")
            print("Error fetching models:", e)
        return [DEFAULT_MODEL]

    def refresh_models(self):
        """Refresh the list of available models"""
        self.update_status("Refreshing models...")
        models = self.get_models()
        self.model_combo['values'] = models
        if self.model_var.get() not in models and models:
            self.model_var.set(models[0])
        self.update_status(f"Loaded {len(models)} model(s)")

    def attach_image(self):
        """Open file dialog to select an image"""
        filetypes = (
            ('Image files', '*.png *.jpg *.jpeg *.gif *.bmp *.webp'),
            ('All files', '*.*')
        )
        filename = filedialog.askopenfilename(
            title='Select an image',
            filetypes=filetypes
        )
        
        if filename:
            try:
                # Open and convert image
                img = Image.open(filename)
                
                # Resize for preview (max 150x150)
                img.thumbnail((150, 150))
                
                # Convert to base64 for Ollama
                with open(filename, 'rb') as f:
                    image_data = f.read()
                    self.current_image_base64 = base64.b64encode(image_data).decode('utf-8')
                
                self.current_image_path = filename
                self.image_path_var.set(os.path.basename(filename))
                
                # Show preview with proper sizing
                photo = ImageTk.PhotoImage(img)
                self.preview_label.config(image=photo, text="")
                self.preview_label.image = photo  # Keep a reference
                
                # Show preview frame
                if not self.preview_visible:
                    self.preview_frame.config(height=160)
                    self.preview_visible = True
                    self.preview_frame.pack(fill=X, padx=10, pady=2)
                else:
                    self.preview_frame.config(height=160)
                
                self.update_status(f"✅ Image attached: {os.path.basename(filename)}")
                
                # Auto-switch to LLaVA if available
                model_list = self.model_combo['values']
                if isinstance(model_list, tuple):
                    model_list = list(model_list)
                
                # Check for vision models
                vision_models = [m for m in model_list if "llava" in m.lower() or "vision" in m.lower()]
                if vision_models:
                    self.model_var.set(vision_models[0])
                    self.update_status(f"💡 Switched to {vision_models[0]} for image support")
                else:
                    self.update_status("⚠️ No vision model found (LLaVA recommended)")
                
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load image: {str(e)}")

    def _reset_attachment(self):
        """Reset the attached-image state and UI (safe to schedule from a thread)."""
        self.current_image_base64 = None
        self.current_image_path = None
        self.image_path_var.set("None")
        self.preview_label.config(image="", text="")
        self.preview_label.image = None

        # Hide preview frame
        if self.preview_visible:
            self.preview_frame.config(height=0)
            self.preview_visible = False
            self.preview_frame.pack_forget()

    def clear_image(self):
        """Clear the attached image"""
        self._reset_attachment()
        self.update_status("🗑️ Image cleared")

    def update_status(self, message):
        """Update status bar message"""
        # Guarded: this may be called from error paths before the status bar is built
        if hasattr(self, "status_label"):
            self.status_label.config(text=message)
        self.root.update_idletasks()

    def _schedule(self, callback):
        """Run a UI callback on the main thread (safe to call from worker threads)."""
        try:
            self.root.after(0, callback)
        except Exception:
            pass  # Window was closed while the background request was in flight

    # ------------------------------------------------------------------ #
    # Session / history management
    # ------------------------------------------------------------------ #
    def _new_session_id(self):
        """Return a timestamp-based id for a new session file."""
        return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]

    def _ensure_sessions_dir(self):
        os.makedirs(SESSIONS_DIR, exist_ok=True)

    def list_sessions(self):
        """Return session metadata (id/title/updated_at), newest first."""
        self._ensure_sessions_dir()
        sessions = []
        for fname in os.listdir(SESSIONS_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(SESSIONS_DIR, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            sessions.append({
                "id": data.get("id", fname[:-5]),
                "title": data.get("title", "Untitled session"),
                "updated_at": data.get("updated_at", ""),
            })
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions

    def refresh_session_list(self):
        """Refresh the session dropdown from disk (main thread only)."""
        self._session_meta = self.list_sessions()
        self.session_combo['values'] = [s["title"] for s in self._session_meta]

    def _save_session(self):
        """Persist the current conversation to disk (main thread only)."""
        if not self.conversation:
            return
        self._ensure_sessions_dir()
        if not self.session_id:
            self.session_id = self._new_session_id()
        if not self.session_created_at:
            self.session_created_at = _now_iso()
        data = {
            "id": self.session_id,
            "title": self.session_title,
            "created_at": self.session_created_at,
            "updated_at": _now_iso(),
            "system_prompt": self.system_prompt,
            "messages": self.conversation,
        }
        try:
            with open(_session_path(self.session_id), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.update_status(f"⚠️ Could not save session: {e}")
            print("Error saving session:", e)
            return
        self.refresh_session_list()

    def load_session(self, session_id):
        """Load a saved session, replacing the current conversation."""
        try:
            with open(_session_path(session_id), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            messagebox.showerror("Error", f"Could not load session:\n{e}")
            return

        self._save_session()  # persist whatever was in the chat area first
        self.session_id = data.get("id", session_id)
        self.session_title = data.get("title", "Untitled session")
        self.session_created_at = data.get("created_at", "")
        self.system_prompt = data.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        self.conversation = data.get("messages", [])

        # Rebuild the on-screen transcript
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', END)
        for msg in self.conversation:
            sender = {"user": "You", "assistant": "oChat"}.get(
                msg.get("role"), msg.get("role") or "?")
            self.append_chat(sender, msg.get("content", ""))
        self.chat_text.configure(state='disabled')

        self.session_title_var.set(self.session_title)
        self.refresh_session_list()
        self._update_sysprompt_status()
        self.update_status(f"📂 Session loaded: {self.session_title}")

    def new_session(self, save_current=True):
        """Start a fresh conversation (optionally autosaving the active one)."""
        if save_current:
            self._save_session()
        self.session_id = None
        self.session_title = "New Chat"
        self.session_created_at = ""
        self.conversation = []
        self.chat_text.configure(state='normal')
        self.chat_text.delete('1.0', END)
        self.chat_text.configure(state='disabled')
        self._reset_attachment()
        self.session_title_var.set("New Chat")
        self.refresh_session_list()
        self._update_sysprompt_status()
        self.update_status("✨ New session started")
        self.input_text.focus_set()

    def delete_session(self):
        """Delete the current session's file and start fresh."""
        if not self.session_id:
            self.update_status("ℹ No saved session to delete")
            return
        if not messagebox.askyesno("Delete session",
                                   f"Delete the saved session '{self.session_title}'?\n"
                                   "This cannot be undone."):
            return
        try:
            os.remove(_session_path(self.session_id))
        except OSError as e:
            messagebox.showerror("Error", f"Could not delete session file:\n{e}")
            return
        self.new_session(save_current=False)
        self.update_status("🗑️ Session deleted")

    def _on_session_selected(self, event=None):
        """Dropdown selection -> load the chosen saved session."""
        title = self.session_title_var.get()
        for meta in self._session_meta:  # newest first
            if meta["title"] == title:
                self.load_session(meta["id"])
                return

    def _on_close(self):
        """Autosave the active session, then close the window."""
        self._save_session()
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # System prompt customization
    # ------------------------------------------------------------------ #
    def set_system_prompt(self):
        """Dialog to edit the system prompt sent with every request."""
        dialog = Toplevel(self.root)
        dialog.title("System Prompt")
        dialog.geometry("520x280")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="System prompt (sent before every message):",
                  font=("Arial", 9, "bold")).pack(anchor='w', padx=10, pady=(8, 4))
        prompt_text = Text(dialog, wrap='word', height=8, font=("Arial", 10))
        prompt_text.pack(fill=BOTH, expand=True, padx=10, pady=(0, 8))
        prompt_text.insert("1.0", self.system_prompt)
        prompt_text.focus_set()

        def save():
            self.system_prompt = prompt_text.get("1.0", "end-1c").strip()
            self._save_session()
            self._update_sysprompt_status()
            dialog.destroy()

        def clear():
            self.system_prompt = ""
            self._save_session()
            self._update_sysprompt_status()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        button_frame = Frame(dialog)
        button_frame.pack(fill=X, padx=10, pady=(0, 8))
        Button(button_frame, text="Save", command=save, width=10).pack(side=RIGHT, padx=5)
        Button(button_frame, text="Clear", command=clear, width=10).pack(side=RIGHT)
        Button(button_frame, text="Cancel", command=cancel, width=10).pack(side=LEFT, padx=5)

        dialog.bind("<Control-Return>", lambda e: save())
        dialog.bind("<Escape>", lambda e: cancel())

    def _update_sysprompt_status(self):
        """Show the active system prompt (or none) in the status bar."""
        preview = self.system_prompt if self.system_prompt else "none"
        if len(preview) > 60:
            preview = preview[:60] + "…"
        self.update_status(f"⚙ System prompt: {preview}")

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #
    def export_chat(self, fmt="markdown"):
        """Export the current conversation to a file chosen by the user."""
        if not self.conversation:
            messagebox.showinfo("Nothing to export", "The current conversation is empty.")
            return

        if fmt == "json":
            filetypes = [("JSON files", "*.json"), ("All files", "*.*")]
            default_ext = ".json"
            build = self._build_json_export
        elif fmt == "text":
            filetypes = [("Text files", "*.txt"), ("All files", "*.*")]
            default_ext = ".txt"
            build = self._build_text_export
        else:
            filetypes = [("Markdown files", "*.md"), ("All files", "*.*")]
            default_ext = ".md"
            build = self._build_markdown_export

        filename = filedialog.asksaveasfilename(
            title="Export chat",
            defaultextension=default_ext,
            filetypes=filetypes,
            initialfile=f"oChat-{datetime.now().strftime('%Y%m%d-%H%M%S')}{default_ext}",
        )
        if not filename:
            return
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(build())
        except OSError as e:
            messagebox.showerror("Error", f"Could not write file:\n{e}")
            return
        self.update_status(f"📤 Exported conversation to {os.path.basename(filename)}")

    def _build_markdown_export(self):
        lines = [f"# {self.session_title}", "",
                 f"- **Model:** {self.model_var.get()}",
                 f"- **Exported:** {_now_iso()}"]
        if self.system_prompt:
            lines.append(f"- **System prompt:** {self.system_prompt}")
        lines += ["", "---", ""]
        for msg in self.conversation:
            sender = {"user": "🧑 User", "assistant": "🤖 oChat"}.get(
                msg.get("role"), msg.get("role") or "?")
            lines += [f"## {sender}", "", str(msg.get("content", "")), ""]
        return "\n".join(lines)

    def _build_json_export(self):
        return json.dumps({
            "id": self.session_id,
            "title": self.session_title,
            "created_at": self.session_created_at,
            "updated_at": _now_iso(),
            "system_prompt": self.system_prompt,
            "model": self.model_var.get(),
            "messages": self.conversation,
        }, ensure_ascii=False, indent=2)

    def _build_text_export(self):
        lines = [f"oChat session: {self.session_title}",
                 f"Model: {self.model_var.get()}", ""]
        if self.system_prompt:
            lines += [f"System prompt: {self.system_prompt}", ""]
        lines.append("=" * 60)
        for msg in self.conversation:
            sender = {"user": "You", "assistant": "oChat", "system": "System"}.get(
                msg.get("role"), msg.get("role") or "?")
            lines += ["", f"[{sender}]", str(msg.get("content", ""))]
        lines.append("")
        return "\n".join(lines)

    def on_send(self):
        user_input = self.input_text.get('1.0', END).strip()
        if not user_input and not self.current_image_base64:
            return
        
        # Check if Ollama is running before trying to send
        try:
            requests.get(f"{OLLAMA_HOST}/api/tags", timeout=2)
        except:
            self.append_chat("Error", "⚠️ Cannot connect to Ollama. Please make sure Ollama is running.")
            self.update_status("⚠️ Ollama not running")
            return
        
        # Build user message with image info if present
        user_message = user_input if user_input else "[Image analysis]"
        if self.current_image_path and user_input:
            user_message = f"{user_input}\n[📷 Image: {os.path.basename(self.current_image_path)}]"
        elif self.current_image_path:
            user_message = f"[📷 Image: {os.path.basename(self.current_image_path)}]"
        
        self.append_chat("You", user_message)
        
        # Store image in conversation
        message_content = user_input if user_input else "Analyze this image"
        if self.current_image_base64:
            self.conversation.append({
                "role": "user", 
                "content": message_content,
                "images": [self.current_image_base64]
            })
        else:
            self.conversation.append({"role": "user", "content": message_content})
        
        # Auto-name the session from the first user message
        if len(self.conversation) == 1 and self.session_title == "New Chat":
            title = (user_message or "Image analysis").replace("\n", " ")[:40].strip()
            if title:
                self.session_title = title
                self.session_title_var.set(title)

        # Persist history immediately (also covers closing mid-request)
        self._save_session()

        # Clear input after sending
        self.input_text.delete('1.0', END)
        self.update_status("⏳ Thinking...")
        
        # Disable input while processing
        self.input_text.config(state='disabled')
        # Snapshot Tk state on the main thread, then hand the request to a worker
        model = self.model_var.get()
        threading.Thread(target=self.send_and_receive, args=(model,), daemon=True).start()

    def append_chat(self, sender, message):
        """Append a message to the chat display"""
        self.chat_text.configure(state='normal')
        
        # Format differently based on sender
        if sender == "You":
            self.chat_text.insert(END, f"🧑 {sender}: {message}\n\n", "user")
        elif sender == "oChat" or sender == "Assistant":
            self.chat_text.insert(END, f"🤖 {sender}: {message}\n\n", "assistant")
        elif sender == "Error":
            self.chat_text.insert(END, f"❌ {sender}: {message}\n\n", "error")
        else:
            self.chat_text.insert(END, f"📌 {sender}: {message}\n\n", "system")
        
        self.chat_text.see(END)
        self.chat_text.configure(state='disabled')
        
        # Configure tags for colors
        self.chat_text.tag_config("user", foreground="#1a73e8")
        self.chat_text.tag_config("assistant", foreground="#34a853")
        self.chat_text.tag_config("system", foreground="#ea4335")
        self.chat_text.tag_config("error", foreground="#ff0000")

    def send_and_receive(self, model):
        """Send request to Ollama and handle response (runs on a worker thread).

        `model` is snapshotted by the caller on the main thread; worker threads
        must never touch Tk objects directly.
        """
        
        # Prepare messages for Ollama API (system prompt first, then chat history)
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        for msg in self.conversation:
            # Skip images in messages (they're handled separately)
            clean_msg = {k: v for k, v in msg.items() if k != "images"}
            if clean_msg.get("role") in ["user", "assistant"]:
                messages.append(clean_msg)
        
        # Build payload
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9
            }
        }
        
        # Add images if present (only for the latest user message)
        if self.current_image_base64 and messages:
            # Find the last user message and add images
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    msg["images"] = [self.current_image_base64]
                    break

        # The image has now been captured into the payload; clear the attachment so
        # it isn't silently re-attached to a follow-up message.
        self._schedule(self._reset_attachment)
        
        try:
            self._schedule(lambda: self.update_status(f"⏳ Sending to {model}..."))
            r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload, timeout=120)
            
            if r.status_code == 200:
                data = r.json()
                assistant_msg = data.get("message", {}).get("content", "")
                if assistant_msg:
                    self.conversation.append({"role": "assistant", "content": assistant_msg})
                    self._schedule(self._save_session)  # persist history
                    self._schedule(lambda: self.append_chat("oChat", assistant_msg))
                    self._schedule(lambda: self.update_status("✅ Done"))
                else:
                    self._schedule(lambda: self.append_chat("Error", "❌ No response from Ollama"))
                    self._schedule(lambda: self.update_status("⚠️ Empty response"))
            else:
                error_text = f"Error {r.status_code}: {r.text[:200]}"
                self._schedule(lambda: self.append_chat("Error", f"❌ {error_text}"))
                self._schedule(lambda: self.update_status(f"⚠️ Error {r.status_code}"))
                
        except requests.exceptions.Timeout:
            self._schedule(lambda: self.append_chat("Error", "⏰ Request timed out. Please try again."))
            self._schedule(lambda: self.update_status("⏰ Timeout"))
        except requests.exceptions.ConnectionError:
            self._schedule(lambda: self.append_chat("Error", "🔌 Cannot connect to Ollama. Please check if it's running."))
            self._schedule(lambda: self.update_status("🔌 Connection error"))
        except Exception as e:
            self._schedule(lambda: self.append_chat("Error", f"❌ Error: {str(e)}"))
            self._schedule(lambda: self.update_status("⚠️ Error"))
        finally:
            # Re-enable input after response
            self._schedule(lambda: self.input_text.config(state='normal'))
            self._schedule(lambda: self.input_text.focus_set())

if __name__ == "__main__":
    root = Tk()
    gui = oChatGUI(root)
    
    # Add a welcome message
    gui.append_chat("System", "🚀 Welcome to oChat with Image Support!")
    gui.append_chat("System", f"📦 Using model: {gui.model_var.get()}")
    gui.append_chat("System", "🖼️ Click 'Attach' to add an image")
    gui.append_chat("System", "💡 Tip: Use LLaVA model for best image understanding")
    gui.append_chat("System", "⌨️  Press Enter to send, Shift+Enter for new line")
    gui.append_chat("System", "💾 Chat history autosaves into the 'sessions' folder")
    gui.append_chat("System", "⚙ Session menu: new/open/delete chats + system prompt")
    gui.append_chat("System", "📤 Export history: File → Export as…")
    gui.update_status("Ready")
    
    root.mainloop()
