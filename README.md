# oChat
oChat is a lightweight graphical chat client for Ollama on Debian. It allows you to interact with any locally installed LLM model exposed by the Ollama API.

## Prerequisites
- **Debian** (or any Debian-based distro)
- **Ollama** installed and running:
  ```bash
  curl -fsSL https://ollama.ai/install.sh | sh
  ollama serve   # Starts the Ollama server on http://localhost:11434
  ```
- Install at least one model, e.g.:
  ```bash
  ollama pull llama2
  ```
- Python3 (recommended) and pip.

## Installation
```bash
cd oChat
python3 -m venv .venv     # Optional: create a virtual environment
source .venv/bin/activate # Activate it
pip install -r requirements.txt
```

## Running the GUI
```bash
# Make sure Ollama is running first
ollama serve &

# Then start oChat
python3 main.py
```

The window will show a dropdown of available models. Type your message in the entry box and press **Send** or hit **Enter**.

## Features
- List all local Ollama models.
- Persistent chat history per session.
- Simple, responsive UI using Tkinter and PyQt5.
- Threaded network requests to keep UI responsive.

## Troubleshooting
- If you see errors like `Error 500: Internal Server Error`, make sure the model name is correct and exists locally (`ollama list`).
- The default Ollama host is `http://localhost:11434`. Change the `OLLAMA_HOST` constant in `main.py` if you run it elsewhere.

## License
MIT. See [LICENSE](LICENSE) for details.

---
Happy chatting! 🤖
