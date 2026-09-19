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
- Session history — every chat autosaves to `sessions/` and can be reopened from the **Session** dropdown.
- Custom **system prompts** per session (Session → Set System Prompt…).
- **Export** the current conversation to Markdown, JSON, or plain text (File → Export as…).
- Image attachment for vision models (LLaVA and similar).
- Simple, responsive UI built with Tkinter (part of the Python standard library).
- Threaded network requests keep the UI responsive while waiting for the model.

## Configuration

- **`OLLAMA_HOST`** in `main.py` — base URL of your Ollama server (default: `http://localhost:11434`).
- **`DEFAULT_MODEL`** in `main.py` — model preselected in the dropdown (default: `llama2`).
- **`sessions/`** — folder created next to `main.py`; each conversation is autosaved there as JSON and picks up the title from its first message.

## Troubleshooting

- **"Cannot connect to Ollama"** → make sure the Ollama server is running (`ollama serve`).
- **`Error 5xx`** → the selected model is missing or the request is malformed; check locally installed models with `ollama list`.
- **Image not understood** → use a vision model such as LLaVA (`ollama pull llava`).

## Agent mode (tool calling)

For coding agents, oChat supports Ollama function calling inside a **workspace folder you choose**:

- Open **Session → Agent Settings…** and pick:
  - **Workspace directory** — the folder the model may read/write (browse or type).
  - **Policy** — `Off` (plain chat), `Read-only`, `Read + Write`, or `Read + Write + Shell`.
  - **Command timeout** — seconds for shell tools (default 120).
  - **API timeout** — seconds of *silence* before a request is aborted (default 600).
- Responses are **streamed** token by token, so long generations keep the
  connection alive; the API timeout only triggers if the model goes silent.
- Settings are stored in `config.json`, which is **not** tracked by git.
- The model sees only the tools its policy allows, and every path is resolved
  against the workspace root — attempts to escape (`../`, symlinks, absolute
  paths) are refused.
- Tool activity is logged in the chat so you can always see what the agent did.
- Tools include `list_dir`, `read_file` (+ `write_file`, `edit_file`, `mkdir`,
  `delete_file`, `copy_file`, `move_file` for write/shell, `run_command` for
  shell) and a `task_complete(summary)` signal.
- The agent works **autonomously**: oChat keeps prompting it with *Continue*
  between steps until it calls `task_complete(summary)` (or a 30-round safety
  cap is hit), so a single analysis step no longer ends the task.
- **Context management:** huge file reads are truncated to a head+tail excerpt,
  and a configurable *Context budget* (default 80 000 chars ≈ 20–24k tokens)
  trims the oldest turns from each request — using only ~75% of the budget so
  reasoning models still have room to think and answer.
- **Reasoning models:** tick *Disable reasoning/thinking* in Agent Settings to
  send `thinking: disabled` for qwen3-style models (they otherwise burn the
  context window on internal thinking and can return empty replies).
- **Engine hiccups:** a stream that ends without a completion marker is retried
  once; if the model returns empty completions, oChat sends up to two targeted
  recovery nudges (keeping tools active, halving the context budget each time)
  before dropping tools — and if it repeatedly returns nothing, it pings the
  engine to tell *"model refusing / out of room"* apart from *"Ollama stalled"*
  (`ollama ps` / logs).
- `num_ctx` is pinned to the context budget so Ollama never silently
  mid-truncates the history oChat sends.
- **Stall detection:** if the model produces 3 consecutive narration replies
  without calling any tool, oChat stops and suggests a New Session / lower
  budget / larger model (the second nudge explicitly commands it to start
  using tools).
- **Resilience:** transient connection drops when Ollama restarts or OOMs are
  retried up to 2× with backoff before showing *"Cannot connect"*.
- **Model-aware windows:** the model's real context length is read once from
  `/api/show` (cached), and both `num_ctx` and the history budget are clamped to
  it — so oChat never sends more history than the model can hold. Stall stops
  remind you that progress is already saved ("send 'continue' to resume").
- **Safe model switches & resumes:** sending "continue" (or switching models
  mid-conversation) injects an **orientation recap** (session task, workspace,
  policy, previous/current model) so the model can't wander into other projects
  it only saw in old history; the switch prompt also offers starting a fresh
  session instead.

> ⚠️ **Security:** *Read + Write + Shell* lets the model run arbitrary shell
> commands in the workspace. Only enable it in a directory you trust.

## License

MIT. See [LICENSE](LICENSE).

---
Happy chatting! 🤖
