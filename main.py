#!/usr/bin/env python3
import threading
import requests
import base64
import os
import json
import shutil
import subprocess
from datetime import datetime
from tkinter import (Tk, Text, Button, Entry, END, LEFT, RIGHT, BOTH, X, Frame,
                     ttk, Scrollbar, VERTICAL, Y, StringVar, filedialog,
                     messagebox, Menu, Toplevel)
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

# Per-user agent settings (workspace, policy, timeouts). Stored next to the app in
# config.json and gitignored, because workspace paths are operator-specific.
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DEFAULT_COMMAND_TIMEOUT = 120   # seconds for a shell tool (run_command)
DEFAULT_API_TIMEOUT = 600       # seconds of silence before a streaming request is dropped
MAX_TOOL_ITERATIONS = 12        # cap on tool-call rounds per request
MAX_AGENT_ROUNDS = 30           # total tool + narration rounds per agent request
TOOL_OUTPUT_LIMIT = 12000       # chars of a tool result returned to the model

POLICY_LABELS = {
    "off": "Off (plain chat)",
    "read": "Read-only",
    "write": "Read + Write",
    "shell": "Read + Write + Shell",
}
POLICY_KEYS = {label: key for key, label in POLICY_LABELS.items()}


def _load_config():
    """Load operator settings from config.json ({} if missing or corrupt)."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_config(config):
    """Persist operator settings to config.json. Returns True on success."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def _tool(name, description, properties, required):
    """Build one OpenAI-style tool/function schema for the Ollama API."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object",
                           "properties": properties,
                           "required": required},
        },
    }


def _tools_for_policy(policy):
    """Declare the tool set the model may call for the configured policy."""
    list_dir = _tool(
        "list_dir",
        "List the entries of a directory inside the workspace (relative paths "
        "start at the workspace root). One line per entry with type and size.",
        {"path": {"type": "string",
                  "description": "Directory path relative to the workspace (default '.')"}},
        [])
    read_file = _tool(
        "read_file",
        "Read a text file inside the workspace and return its full content.",
        {"path": {"type": "string", "description": "File path relative to the workspace"}},
        ["path"])
    write_file = _tool(
        "write_file",
        "Create or overwrite a file inside the workspace, creating missing "
        "parent directories automatically.",
        {"path": {"type": "string", "description": "File path relative to the workspace"},
         "content": {"type": "string", "description": "Full file content to write"}},
        ["path", "content"])
    edit_file = _tool(
        "edit_file",
        "Replace the first exact occurrence of old_string with new_string in a "
        "file inside the workspace. Use this instead of rewriting whole files.",
        {"path": {"type": "string", "description": "File path relative to the workspace"},
         "old_string": {"type": "string", "description": "Exact text to find (must appear at least once)"},
         "new_string": {"type": "string", "description": "Replacement text"}},
        ["path", "old_string", "new_string"])
    run_command = _tool(
        "run_command",
        "Run a shell command inside the workspace directory and return its "
        "output and exit code. Non-interactive; killed on timeout. Use for "
        "builds, tests, and quick checks.",
        {"command": {"type": "string", "description": "Shell command to run in the workspace"}},
        ["command"])
    mkdir = _tool(
        "mkdir",
        "Create a directory (and any missing parents) inside the workspace.",
        {"path": {"type": "string", "description": "Directory path relative to the workspace"}},
        ["path"])
    delete_file = _tool(
        "delete_file",
        "Delete a file inside the workspace (not a directory).",
        {"path": {"type": "string", "description": "File path relative to the workspace"}},
        ["path"])
    copy_file = _tool(
        "copy_file",
        "Copy a file inside the workspace (creating parent directories if needed).",
        {"source": {"type": "string", "description": "Source path relative to the workspace"},
         "destination": {"type": "string", "description": "Destination path relative to the workspace"}},
        ["source", "destination"])
    move_file = _tool(
        "move_file",
        "Move or rename a file inside the workspace (creating parent directories if needed).",
        {"source": {"type": "string", "description": "Source path relative to the workspace"},
         "destination": {"type": "string", "description": "Destination path relative to the workspace"}},
        ["source", "destination"])
    task_complete = _tool(
        "task_complete",
        "Call this ONLY when the entire user request is finished. Provide a "
        "short summary of everything that was done. Do NOT call it after "
        "single steps — the task is complete only when all steps are done.",
        {"summary": {"type": "string", "description": "Summary of everything completed"}},
        ["summary"])

    base = [list_dir, read_file, task_complete]
    if policy == "read":
        return base
    if policy == "write":
        return base + [write_file, edit_file, mkdir, delete_file, copy_file, move_file]
    if policy == "shell":
        return base + [write_file, edit_file, mkdir, delete_file, copy_file, move_file, run_command]
    return []  # "off"


def _resolve_in_workspace(workspace, path_arg):
    """Resolve a model-supplied path safely inside the workspace root.

    Returns the absolute real path, or raises ValueError with an explanation
    that is safe to send back to the model.
    """
    root = os.path.realpath(workspace)
    candidate = os.path.realpath(os.path.join(root, path_arg or "."))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise ValueError(
            f"Path '{path_arg}' resolves outside the workspace '{root}' and was refused."
        )
    return candidate


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

        # Agent (tool-calling) settings — operator-configurable, persisted in config.json
        self.agent_config = _load_config()
        # Migrate pre-split configs: the old "timeout" becomes the command timeout
        if "timeout" in self.agent_config and "command_timeout" not in self.agent_config:
            self.agent_config["command_timeout"] = self.agent_config.pop("timeout")
        self.agent_config.setdefault("workspace", "")
        self.agent_config.setdefault("policy", "off")
        self.agent_config.setdefault("command_timeout", DEFAULT_COMMAND_TIMEOUT)
        self.agent_config.setdefault("api_timeout", DEFAULT_API_TIMEOUT)

        # Ollama-reported model capabilities (name -> set of capability strings)
        self._model_caps = {}

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
        session_menu.add_command(label="Agent Settings…", command=self.agent_settings)
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
        """Fetch available models and their capabilities from Ollama."""
        caps = {}
        try:
            r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
            if r.status_code == 200:
                data = r.json()
                models = []
                for model in data.get("models", []):
                    name = model.get("name")
                    if not name:
                        continue
                    models.append(name)
                    cap_list = model.get("capabilities")
                    if isinstance(cap_list, list):
                        caps[name] = {str(c) for c in cap_list}
                if models:
                    self._model_caps = caps
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
        self._model_caps = caps
        return [DEFAULT_MODEL]

    def refresh_models(self):
        """Refresh the list of available models"""
        self.update_status("Refreshing models...")
        models = self.get_models()
        self.model_combo['values'] = models
        if self.model_var.get() not in models and models:
            self.model_var.set(models[0])
        self.update_status(f"Loaded {len(models)} model(s)")

    def _tools_capable_models(self):
        """Names of installed models that Ollama reports as tool-capable (best effort)."""
        return sorted(name for name, cap_set in self._model_caps.items() if "tools" in cap_set)

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
            if msg.get("role") in ("tool", None):  # tool results aren't transcript text
                continue
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
    # Agent tools (tool-calling for coding agents)
    # ------------------------------------------------------------------ #
    def agent_settings(self):
        """Dialog: workspace directory, policy level, and timeouts."""
        dialog = Toplevel(self.root)
        dialog.title("Agent Settings")
        dialog.geometry("520x270")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        workspace_var = StringVar(value=self.agent_config.get("workspace", ""))
        policy_var = StringVar(value=POLICY_LABELS.get(
            self.agent_config.get("policy", "off"), POLICY_LABELS["off"]))
        command_timeout_var = StringVar(
            value=str(self.agent_config.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)))
        api_timeout_var = StringVar(
            value=str(self.agent_config.get("api_timeout", DEFAULT_API_TIMEOUT)))

        row1 = Frame(dialog)
        row1.pack(fill=X, padx=10, pady=(8, 4))
        ttk.Label(row1, text="Workspace directory:", font=("Arial", 9)).pack(side=LEFT)
        Entry(row1, textvariable=workspace_var).pack(side=LEFT, fill=X, expand=True)
        Button(row1, text="Browse…",
               command=lambda: workspace_var.set(
                   filedialog.askdirectory(title="Choose the agent workspace") or workspace_var.get())
               ).pack(side=LEFT)

        row2 = Frame(dialog)
        row2.pack(fill=X, padx=10, pady=4)
        ttk.Label(row2, text="Policy:", font=("Arial", 9)).pack(side=LEFT, padx=(0, 10))
        ttk.Combobox(row2, textvariable=policy_var, state="readonly", width=22,
                     values=list(POLICY_LABELS.values())).pack(side=LEFT)

        row3 = Frame(dialog)
        row3.pack(fill=X, padx=10, pady=4)
        ttk.Label(row3, text="Command timeout (s):", font=("Arial", 9)).pack(side=LEFT, padx=(0, 10))
        Entry(row3, textvariable=command_timeout_var, width=8).pack(side=LEFT)
        ttk.Label(row3, text="for shell tools", font=("Arial", 8), foreground="#666666").pack(side=LEFT, padx=(4, 0))

        row4 = Frame(dialog)
        row4.pack(fill=X, padx=10, pady=4)
        ttk.Label(row4, text="API timeout (s):", font=("Arial", 9)).pack(side=LEFT, padx=(0, 10))
        Entry(row4, textvariable=api_timeout_var, width=8).pack(side=LEFT)
        ttk.Label(row4, text="of silence — streaming keeps the request alive",
                  font=("Arial", 8), foreground="#666666").pack(side=LEFT, padx=(4, 0))

        ttk.Label(dialog, foreground="#cc0000", justify=LEFT, wraplength=480,
                  font=("Arial", 8),
                  text="The model may only touch files inside the workspace. "
                       "'Read + Write + Shell' additionally allows running shell "
                       "commands — only enable it in a directory you trust.").pack(
            anchor='w', padx=10, pady=(4, 2))

        def save():
            ws = workspace_var.get().strip()
            pol = POLICY_KEYS.get(policy_var.get(), "off")
            try:
                cmd_to = int(command_timeout_var.get().strip() or DEFAULT_COMMAND_TIMEOUT)
                api_to = int(api_timeout_var.get().strip() or DEFAULT_API_TIMEOUT)
            except ValueError:
                messagebox.showerror("Invalid timeout", "Timeouts must be whole numbers of seconds.")
                return
            cmd_to = max(5, min(cmd_to, 3600))
            api_to = max(5, min(api_to, 3600))
            if pol != "off":
                if not os.path.isdir(ws):
                    messagebox.showerror("Invalid workspace",
                                         "The workspace directory does not exist.\n"
                                         "Create it first or pick an existing folder.")
                    return
                ws = os.path.realpath(ws)
            self.agent_config.update({"workspace": ws, "policy": pol,
                                      "command_timeout": cmd_to, "api_timeout": api_to})
            if not _save_config(self.agent_config):
                messagebox.showwarning("Warning",
                                       "Could not write config.json — settings will not survive a restart.")
            self._update_agent_status()
            dialog.destroy()

        def cancel():
            dialog.destroy()

        button_frame = Frame(dialog)
        button_frame.pack(fill=X, padx=10, pady=(0, 8))
        Button(button_frame, text="Save", command=save, width=10).pack(side=RIGHT, padx=5)
        Button(button_frame, text="Cancel", command=cancel, width=10).pack(side=LEFT, padx=5)

        dialog.bind("<Return>", lambda e: save())
        dialog.bind("<Escape>", lambda e: cancel())

    def _update_agent_status(self):
        """Show the active agent workspace/policy in the status bar."""
        pol = self.agent_config.get("policy", "off")
        if pol == "off":
            self.update_status("🔧 Agent: off")
            return
        ws = self.agent_config.get("workspace", "") or "(no workspace set)"
        self.update_status(f"🔧 Agent: {POLICY_LABELS.get(pol, pol)} — {ws}")

    def _stream_tail(self):
        """Terminate a live-streamed assistant reply with a blank line (main thread)."""
        self.chat_text.configure(state='normal')
        self.chat_text.insert(END, "\n\n")
        self.chat_text.see(END)
        self.chat_text.configure(state='disabled')

    def _handle_http_error(self, r, payload, model, tools):
        """Show a clear, actionable error for a failed /api/chat response."""
        # Ollama wraps errors in JSON: {"error": "..."} — extract it cleanly.
        raw_error = r.text[:400] if r else ""
        if r is not None:
            try:
                data = r.json()
                if isinstance(data, dict) and data.get("error"):
                    raw_error = str(data["error"])[:400]
            except Exception:
                pass

        if tools and "does not support tools" in raw_error.lower():
            note = (f"Model '{model}' does not support tool calling, so Agent mode "
                    "cannot call tools with it.\n\n"
                    "Options:\n")
            capable = self._tools_capable_models()
            if capable:
                note += ("• Tool-capable models you currently have:\n"
                         + "".join(f"    - {m}\n" for m in capable)
                         + "  Select one of those and send again.\n")
            else:
                note += ("• Pick a model known to support tool calling (check the "
                         "model card on ollama.com) and send again.\n")
            note += ("• Or turn agent mode off: Session → Agent Settings → Policy = "
                     "'Off (plain chat)' — it will answer like a normal chat.")
            self._schedule(lambda n=note: self.append_chat("Error", f"❌ {n}"))
            self._schedule(lambda: self.update_status("⚠️ Model has no tool support"))
            return

        error_text = f"Error {r.status_code}: {raw_error}"
        self._schedule(lambda e=error_text: self.append_chat("Error", f"❌ {e}"))
        self._schedule(lambda: self.update_status(f"⚠️ Error {r.status_code}"))

    def _execute_tool(self, name, args):
        """Execute one tool call inside the workspace. Returns a text result."""
        workspace = self.agent_config.get("workspace", "")
        if not workspace or not os.path.isdir(workspace):
            return ("Error: no valid agent workspace is configured. "
                    "Tell the user to set one (Session menu → Agent Settings…).")

        if name == "list_dir":
            try:
                path = _resolve_in_workspace(workspace, args.get("path", "."))
            except ValueError as e:
                return str(e)
            try:
                entries = sorted(os.listdir(path))
            except OSError as e:
                return f"Error listing {path}: {e}"
            if not entries:
                return "(empty directory)"
            lines = []
            for entry in entries:
                full = os.path.join(path, entry)
                try:
                    if os.path.isdir(full):
                        lines.append(f"dir  {entry}/")
                    else:
                        lines.append(f"file {entry} ({os.path.getsize(full)} B)")
                except OSError:
                    lines.append(f"??   {entry}")
            return "\n".join(lines)

        if name == "read_file":
            try:
                pth = _resolve_in_workspace(workspace, args.get("path", ""))
            except ValueError as e:
                return str(e)
            try:
                with open(pth, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError as e:
                return f"Error reading {pth}: {e}"
            return content if content else "(empty file)"

        if name == "write_file":
            try:
                pth = _resolve_in_workspace(workspace, args.get("path", ""))
            except ValueError as e:
                return str(e)
            content = args.get("content", "")
            if isinstance(content, list):  # some clients send content blocks
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            try:
                os.makedirs(os.path.dirname(pth) or workspace, exist_ok=True)
                with open(pth, "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError as e:
                return f"Error writing {pth}: {e}"
            return f"Wrote {len(content)} bytes to {pth}"

        if name == "edit_file":
            try:
                pth = _resolve_in_workspace(workspace, args.get("path", ""))
            except ValueError as e:
                return str(e)
            old_s = args.get("old_string", "")
            new_s = args.get("new_string", "")
            if not old_s:
                return "Error: edit_file requires a non-empty 'old_string'."
            try:
                with open(pth, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                return f"Error reading {pth}: {e}"
            if old_s not in content:
                return (f"Error: 'old_string' not found in {pth}. "
                        "Return the exact text to replace (must appear at least once).")
            try:
                with open(pth, "w", encoding="utf-8") as f:
                    f.write(content.replace(old_s, new_s, 1))
            except OSError as e:
                return f"Error writing {pth}: {e}"
            return f"Edited {pth}: replaced the first occurrence."

        if name == "mkdir":
            try:
                pth = _resolve_in_workspace(workspace, args.get("path", ""))
            except ValueError as e:
                return str(e)
            if os.path.exists(pth) and not os.path.isdir(pth):
                return f"Error: '{pth}' already exists and is not a directory."
            try:
                os.makedirs(pth, exist_ok=True)
            except OSError as e:
                return f"Error creating {pth}: {e}"
            return f"Created directory {pth}"

        if name == "delete_file":
            try:
                pth = _resolve_in_workspace(workspace, args.get("path", ""))
            except ValueError as e:
                return str(e)
            if not os.path.isfile(pth):
                return f"Error: '{pth}' is not a file."
            try:
                os.remove(pth)
            except OSError as e:
                return f"Error deleting {pth}: {e}"
            return f"Deleted {pth}"

        if name in ("copy_file", "move_file"):
            try:
                src = _resolve_in_workspace(workspace, args.get("source", ""))
                dst = _resolve_in_workspace(workspace, args.get("destination", ""))
            except ValueError as e:
                return str(e)
            if not os.path.isfile(src):
                return f"Error: source '{src}' is not a file."
            if os.path.isdir(dst):
                dst = os.path.join(dst, os.path.basename(src))
            try:
                os.makedirs(os.path.dirname(dst) or workspace, exist_ok=True)
                if name == "copy_file":
                    shutil.copy2(src, dst)
                else:
                    shutil.move(src, dst)
            except OSError as e:
                return f"Error {name} {src} -> {dst}: {e}"
            return f"{'Copied' if name == 'copy_file' else 'Moved'} {src} -> {dst}"

        if name == "task_complete":
            return "Task marked complete."

        if name == "run_command":
            cmd = args.get("command", "")
            if not cmd:
                return "Error: run_command requires a 'command' argument."
            command_timeout = int(self.agent_config.get("command_timeout", DEFAULT_COMMAND_TIMEOUT))
            try:
                result = subprocess.run(cmd, shell=True, cwd=workspace,
                                        capture_output=True, text=True, timeout=command_timeout)
            except subprocess.TimeoutExpired:
                return f"Error: command timed out after {command_timeout}s and was killed."
            except OSError as e:
                return f"Error running command: {e}"
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            if err:
                out = (out + ("\n" if out else "") + f"[stderr]\n{err}").strip()
            if not out:
                out = f"Command finished with exit code {result.returncode}."
            return (f"exit code: {result.returncode}\n{out}")[:TOOL_OUTPUT_LIMIT]

        return f"Error: unknown tool '{name}'."

    @staticmethod
    def _fmt_tool_args(args):
        """Short human-readable render of tool arguments for the chat log."""

        def short(v):
            s = str(v)
            return s[:80] + ("…" if len(s) > 80 else "")

        parts = []
        for key in ("path", "command", "old_string"):
            if key in args:
                parts.append(f"{key}={short(args[key])}")
        if not parts:
            parts = [f"{k}={short(v)}" for k, v in list(args.items())[:4]]
        return ", ".join(parts)

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
        
        # Agent mode: resolve tools up-front so the system prompt and loop can use it
        tools = _tools_for_policy(self.agent_config.get("policy", "off"))

        # Prepare messages for Ollama API (system prompt first, then chat history)
        messages = []
        system_parts = []
        if self.system_prompt:
            system_parts.append(self.system_prompt)
        if tools:
            system_parts.append(
                "You are an autonomous coding agent. Work through the user's "
                "request COMPLETELY, step by step, using the provided tools. "
                "Never stop after a single analysis step and never ask for "
                "permission between steps — keep using tools until the entire "
                "task is finished. Only when everything is done, call "
                "task_complete(summary) with a concise report of what changed.")
        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})
        for msg in self.conversation:
            # Skip images (handled separately); keep assistant tool-call narration
            # and tool results so follow-up turns have full context.
            clean_msg = {k: v for k, v in msg.items() if k != "images"}
            if clean_msg.get("role") in ["user", "assistant", "tool"]:
                messages.append(clean_msg)
        
        # Build payload
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": 0.7,
                "top_p": 0.9
            }
        }
        
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

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
            api_timeout = int(self.agent_config.get("api_timeout", DEFAULT_API_TIMEOUT))
            tool_round = 0
            tools_dropped = False
            agent_rounds = 0
            finished = False
            finish_summary = ""

            while True:
                if finished:
                    # task_complete was called — deliver its summary and stop
                    final_text = finish_summary or "(task complete)"
                    self.conversation.append({"role": "assistant", "content": final_text})
                    self._schedule(self._save_session)
                    self._schedule(lambda t=final_text: self.append_chat("oChat", t))
                    self._schedule(lambda: self.update_status("✅ Done"))
                    return

                self._schedule(lambda: self.update_status(f"⏳ Sending to {model}..."))
                # Streaming: tokens arrive progressively, so the API timeout only
                # bounds *silence* between tokens — long generations stay alive.
                chunk_parts = []
                seen_any = [False]
                retried = [False]

                def stream_chunk(text):
                    """Buffer a token and show it live in the chat (main thread via _schedule)."""
                    if not text:
                        return
                    # Capture 'first' at schedule time: by the time the callback runs,
                    # chunk_parts has grown, so we must not inspect it then.
                    first = not chunk_parts
                    chunk_parts.append(text)
                    seen_any[0] = True

                    def _show(first=first, text=text):
                        self.chat_text.configure(state='normal')
                        if first:  # first visible chunk -> header line
                            self.chat_text.insert(END, "🤖 oChat: ")
                        self.chat_text.insert(END, text)
                        self.chat_text.see(END)
                        self.chat_text.configure(state='disabled')

                    self._schedule(_show)

                try:
                    r = requests.post(f"{OLLAMA_HOST}/api/chat", json=payload,
                                      stream=True, timeout=(10, api_timeout))
                except requests.exceptions.Timeout:
                    if not retried[0] and not seen_any[0]:
                        # The round never started producing output — safe to retry once
                        retried[0] = True
                        self._schedule(lambda: self.append_chat("System", "⏳ Retrying…"))
                        self._schedule(lambda: self.update_status("⏳ Retrying…"))
                        continue
                    self._schedule(lambda: self.append_chat(
                        "Error", f"⏰ Could not reach Ollama within 10 s. "
                        "Please check that it is running."))
                    self._schedule(lambda: self.update_status("⏰ Connect timeout"))
                    return

                if r.status_code != 200:
                    self._handle_http_error(r, payload, model, tools)
                    r.close()
                    return

                # Consume the NDJSON stream: {"message": {"content": "..."}, "done": bool}
                final_message = {}
                streamed_tool_calls = None  # some servers send tool_calls pre-done
                try:
                    with r:
                        for raw in r.iter_lines(decode_unicode=True):
                            if not raw or not raw.strip():
                                continue
                            try:
                                obj = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            message = obj.get("message") or {}
                            piece = message.get("content", "")
                            stream_chunk(piece or "")
                            tc = message.get("tool_calls")
                            if tc:
                                streamed_tool_calls = tc
                            if obj.get("done"):
                                final_message = message
                                break
                except requests.exceptions.Timeout:
                    # Mid-stream silence: partial output is already on screen; do not
                    # re-run the round (would duplicate), just report clearly.
                    self._schedule(lambda t=api_timeout: self.append_chat(
                        "Error", f"⏰ No data received for {t}s mid-response — the "
                        f"partial answer was not saved. If this repeats, raise the "
                        f"API timeout in Agent Settings."))
                    self._schedule(lambda: self.update_status("⏰ Idle timeout"))
                    if seen_any[0]:
                        self._schedule(lambda: self._stream_tail())
                    return

                assistant_msg = "".join(chunk_parts)
                # Prefer tool calls seen on ANY stream line (some servers only send
                # them before the final done:true message).
                tool_calls = (final_message.get("tool_calls") or streamed_tool_calls or [])

                # Agent mode: the model asked to use tools — execute and loop
                if tools and tool_calls and tool_round < MAX_TOOL_ITERATIONS:
                    tool_round += 1
                    agent_rounds += 1
                    if seen_any[0]:
                        self._schedule(lambda: self._stream_tail())
                    messages.append({"role": "assistant", "content": assistant_msg,
                                     "tool_calls": tool_calls})
                    # Persist the round so a follow-up user message keeps full context
                    self.conversation.append({"role": "assistant", "content": assistant_msg,
                                              "tool_calls": tool_calls})
                    for tc in tool_calls:
                        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                        name = fn.get("name", "")
                        raw = fn.get("arguments", {})
                        if isinstance(raw, str):
                            try:
                                args = json.loads(raw) if raw.strip() else {}
                            except json.JSONDecodeError:
                                args = {"_raw": raw}
                        elif isinstance(raw, dict):
                            args = raw
                        else:
                            args = {}
                        self._schedule(lambda n=name, a=args: self.append_chat(
                            "Tool", f"🔧 {n}({self._fmt_tool_args(a)})"))
                        if name == "task_complete":
                            finished = True
                            finish_summary = str(args.get("summary", ""))
                            result = "Task marked complete."
                        else:
                            result = self._execute_tool(name, args)
                        self._schedule(lambda r_=result: self.append_chat(
                            "Tool result", f"{r_[:1200]}"))
                        # Persist a bounded copy so sessions stay small
                        limited = result[:TOOL_OUTPUT_LIMIT]
                        if len(result) > TOOL_OUTPUT_LIMIT:
                            limited += "\n...[result truncated]"
                        self.conversation.append({"role": "tool", "content": limited})
                        messages.append({"role": "tool", "content": result})
                    if agent_rounds >= MAX_AGENT_ROUNDS and not finished:
                        self._schedule(lambda: self.append_chat(
                            "Error", f"⏹ Reached the {MAX_AGENT_ROUNDS}-round agent "
                            "limit without task_complete — stopping. Send anything "
                            "to let the agent continue."))
                        self._schedule(lambda: self.update_status("⏹ Round limit"))
                        return
                    continue

                if not assistant_msg:
                    # Dead-end guard: with tools declared, the model produced neither
                    # a tool call nor any text — retry once WITHOUT tools so the
                    # conversation doesn't stall on an empty reply.
                    if tools and not tools_dropped:
                        tools_dropped = True
                        tools = []
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        self._schedule(lambda: self.append_chat(
                            "System", "⚠️ Model returned no tool call and no text — "
                            "retrying without tool calling."))
                        self._schedule(lambda: self.update_status("⏳ Retrying without tools…"))
                        continue

                    if tool_round:
                        msg = "❌ Agent stopped without a final text answer."
                    elif tools:
                        capable = self._tools_capable_models()
                        if capable:
                            msg = ("❌ The model returned nothing and made no tool call. "
                                   f"It may not support tool calling — try one of your "
                                   f"tool-capable models ({', '.join(capable)}) or set "
                                   f"Agent policy to 'Off'.")
                        else:
                            msg = ("❌ The model returned nothing and made no tool call. "
                                   "It may not support tool calling — switch to a "
                                   "tools-capable model or set Agent policy to 'Off'.")
                    else:
                        msg = "❌ No response from Ollama"
                    if seen_any[0]:
                        self._schedule(lambda: self._stream_tail())
                    self._schedule(lambda m=msg: self.append_chat("Error", m))
                    self._schedule(lambda: self.update_status("⚠️ Empty response"))
                    return

                # The model produced TEXT without a tool call.
                self.conversation.append({"role": "assistant", "content": assistant_msg})
                if seen_any[0]:
                    self._schedule(lambda: self._stream_tail())
                else:
                    self._schedule(lambda a=assistant_msg: self.append_chat("oChat", a))

                if tools:
                    # Agent mode: narration is not the end — keep going autonomously
                    # until the model calls task_complete or the round limit.
                    agent_rounds += 1
                    if agent_rounds >= MAX_AGENT_ROUNDS:
                        self._schedule(lambda: self.append_chat(
                            "Error", f"⏹ Reached the {MAX_AGENT_ROUNDS}-round agent "
                            "limit without task_complete — stopping. Send anything "
                            "to let the agent continue."))
                        self._schedule(lambda: self.update_status("⏹ Round limit"))
                        return
                    self._schedule(self._save_session)  # durable progress
                    self._schedule(lambda: self.append_chat(
                        "System", "⏭ Continuing autonomously…"))
                    messages.append({"role": "assistant", "content": assistant_msg})
                    messages.append({"role": "user",
                                     "content": "Continue. Work until the whole "
                                     "task is complete, then call task_complete(summary)."})
                    continue

                # Plain chat (no tools): this text IS the final answer.
                self._schedule(self._save_session)  # persist history
                self._schedule(lambda: self.update_status("✅ Done"))
                return
        except requests.exceptions.Timeout:
            self._schedule(lambda t=api_timeout: self.append_chat(
                "Error", f"⏰ Request timed out after {t}s of silence. "
                f"Raise the API timeout (Session → Agent Settings) and try again."))
            self._schedule(lambda: self.update_status("⏰ Timeout"))
        except requests.exceptions.ConnectionError:
            self._schedule(lambda: self.append_chat("Error", "🔌 Cannot connect to Ollama. Please check if it's running."))
            self._schedule(lambda: self.update_status("🔌 Connection error"))
        except Exception as e:
            # Capture e now — Python unbinds the except target when the block ends,
            # and lazy lambdas would otherwise raise NameError in the main loop.
            err_msg = str(e)
            self._schedule(lambda m=err_msg: self.append_chat("Error", f"❌ Error: {m}"))
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
    gui.append_chat("System", "🔧 Agent mode: Session → Agent Settings… (workspace + policy)")
    gui.update_status("Ready")
    
    root.mainloop()
