# oChat

oChat is a lightweight graphical chat client for [Ollama](https://ollama.com). It lets you chat with any LLM model served by a local Ollama instance — including vision models such as LLaVA (attach an image and the model can describe it).

Works on Linux, macOS, and Windows. All you need is Python 3 and a running Ollama server.

## Prerequisites

- **Python 3** with `pip`
- **Ollama** — download and install from <https://ollama.com/download> for your platform
- The Ollama server running on its default address:

  ```bash
  ollama serve   # listens on http://localhost:11434
  ```

  On macOS and Windows the Ollama app usually starts the server automatically after install.
- At least one model pulled, e.g.:

  ```bash
  ollama pull llama2
  ```

## Installation

```bash
cd oChat
python3 -m venv .venv          # optional, but recommended
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running the GUI

```bash
python3 main.py
```

Make sure Ollama is running first — otherwise oChat shows a connection error in the status bar.

The **model dropdown** lists all locally installed models (use **↻ Refresh** to reload it). Type a message in the text box and press **Send** or hit **Enter** (**Shift+Enter** inserts a newline). Click **Attach** to add an image — a vision model such as LLaVA is recommended for image understanding.

## Features

- Lists all local Ollama models (refreshable).
- In-session chat history.
- Image attachment for vision models (LLaVA and similar).
- Simple, responsive UI built with Tkinter (part of the Python standard library).
- Threaded network requests keep the UI responsive while waiting for the model.

## Configuration

- **`OLLAMA_HOST`** in `main.py` — base URL of your Ollama server (default: `http://localhost:11434`).
- **`DEFAULT_MODEL`** in `main.py` — model preselected in the dropdown (default: `llama2`).

## Troubleshooting

- **"Cannot connect to Ollama"** → make sure the Ollama server is running (`ollama serve`).
- **`Error 5xx`** → the selected model is missing or the request is malformed; check locally installed models with `ollama list`.
- **Image not understood** → use a vision model such as LLaVA (`ollama pull llava`).

## License

MIT. See [LICENSE](LICENSE).

---
Happy chatting! 🤖
