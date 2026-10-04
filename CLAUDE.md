# Process-to-Automation Advisor

A Streamlit app that interviews a business user about a manual process, scores how well it
automates, decides whether AI is needed, compares cost, checks compliance, and writes a design
document. Read `README.md` for what it does; this file is only the things that are easy to get
wrong and expensive to rediscover.

## Environment

- Prefer the explicit interpreter `.venv\Scripts\python.exe`. Bare `python` works only while the
  venv is activated in that shell; in a fresh shell it can hit the Microsoft Store stub instead.
- Tests: `.venv\Scripts\python.exe -m pytest -q` — currently 571, all passing. Keep it that way.
- Run the app: `.venv\Scripts\streamlit run app.py`
- **Not a git repository.** There is no history to consult and no safety net; be careful with
  destructive edits.
- `.env` is deny-listed in `.claude/settings.local.json`, so it cannot be read or edited. To check
  whether a key is configured, ask the user, or use `llm_available()` — never try to read the file.
- Optional extras are separate: `requirements-voice.txt` (faster-whisper, already installed).

## Hard constraints — do not break these

1. **The app must work completely without an API key.** Offline it uses `GuidedInterviewer` and
   `TemplateWriter`; with a key it switches to the LLM interviewer and writer automatically. Every
   feature except those two must behave identically either way.
2. **All scoring and cost maths lives in `advisor/analysis/`, in plain Python — never in the LLM.**
   Neither writer emits numbers; `report.py` substitutes computed tables into `{{PLACEHOLDER}}`
   tokens. This is the core design promise, not a preference.
3. **Model calls only ever go through `advisor/llm/base.py`.** The provider interface exists so an
   Ollama provider can be dropped in without touching agent logic. Do not call an SDK from agent code.
4. **No network call at import or startup.** Live prices are fetched only when the user clicks
   refresh. The test suite asserts this by making any socket use fail.

## Streamlit (1.65) — traps that cost real time

- **Streamlit exposes essentially no theme CSS variables.** `var(--primary-color)`,
  `var(--background-color)` and friends resolve to *nothing* and fail silently. Colour, type and
  radius belong in `.streamlit/config.toml`; keep `advisor/ui.py` CSS to layout only.
- **A custom `avatar=` collapses the chat message testids.** `stChatMessageAvatarUser` and
  `...Assistant` both become `stChatMessageAvatarCustom`, so role-based CSS stops matching. The
  left/right chat layout keys off `st.container(key="turn-<role>-N")` instead — the documented
  escape hatch, which emits an `st-key-...` class.
- **`use_container_width` is deprecated.** Use `width="stretch"`.
- **Editing an imported module does not reliably hot-reload.** Module-level constants such as
  `ui.CSS` are cached; restart the server after changing `advisor/ui.py`.
- Don't let CSS cap `stMainBlockContainer`'s width — it silently overrides `layout="wide"`.
- Version-matched reference docs are bundled in the installed package. Find them with:
  `python %USERPROFILE%\.claude\skills\developing-with-streamlit\scripts\discover.py --project-dir .`

## Working style

- Agents editing `app.py` while the app is running produce a "source code string cannot contain null
  bytes" error in the browser. Stop the app before a multi-agent run that touches it.
- Verify UI changes in a real browser, not only through `AppTest`. Tests pass on layouts that look
  broken.
- Prices in `advisor/data/pricing_assumptions.json` are planning placeholders. Keep the
  "validate before quoting" caveat visible in the UI and the document — it matters commercially.
