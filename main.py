#!/usr/bin/env python3
import threading
import requests
import base64
import os
from tkinter import Tk, Text, Button, END, LEFT, RIGHT, BOTH, X, Frame, ttk, Scrollbar, VERTICAL, Y, StringVar, filedialog, messagebox
from PIL import Image, ImageTk

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "llama2"  # or any Ollama model installed locally

class oChatGUI:
    def __init__(self, root):
        self.root = root
        root.title("oChat - Image Support")
        root.geometry("900x750")  # Slightly larger
        root.minsize(500, 500)

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
        
        # Clear input after sending
        self.input_text.delete('1.0', END)
        self.update_status("⏳ Thinking...")
        
        # Disable input while processing
        self.input_text.config(state='disabled')
        threading.Thread(target=self.send_and_receive, daemon=True).start()

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

    def send_and_receive(self):
        """Send request to Ollama and handle response"""
        model = self.model_var.get()
        
        # Prepare messages for Ollama API
        messages = []
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
    gui.update_status("Ready")
    
    root.mainloop()
