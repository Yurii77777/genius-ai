# genius-ai

A control layer over every project and service on your Mac.
You open Claude Code in this folder, and Claude becomes the monitor: it knows your projects, watches their state, and talks to you by text and, optionally, by voice.

## What it does

- **Monitors projects.** For each local project: branch, uncommitted work, ahead/behind upstream, open PRs, failed and running CI workflows.
- **Monitors services.** GitHub, prod URLs (HTTP status, response time, TLS), Vercel deployments, RunPod pods.
- **Keeps project rules.** Each project gets a short rules file (`projects/<name>/AGENTS.md`): what it is, how it deploys, release windows, where to look for details. Claude reads it before any work with the project.
- **Live control.** While the session is open, Claude runs `bin/status` every 15 minutes and reports changes at once.
- **Pending operations.** Long operations (CI, release, deploy) are written to `state/operations.json` and survive the end of a session.
- **Persona.** You choose the language (Ukrainian or English), the grammatical gender, and the tone (`peer`, `business`, `warm`, or your own words).
- **Voice (optional).** Local text-to-speech for replies and work statuses; local dictation with F5. No cloud, no account.

Monitoring is read-only. Claude changes nothing in a monitored project or service unless you ask.

## How it works

```
genius-ai/
├── AGENTS.md                 all rules for the agent (CLAUDE.md only imports it)
├── .claude/
│   ├── settings.json         hooks: SessionStart, UserPromptSubmit, PreToolUse, Stop
│   └── hooks/session_start.py
├── bin/
│   ├── genius                start a session; save / restore / new / adopt / where
│   └── status                live state of all projects and services
├── persona/tones.json        tone presets
├── projects/_template/       copy of the rules for a new project
├── voice/                    everything for the voice (see «Voice»)
│
├── genius.config.json        ← personal, gitignored: language, persona, projects, services
├── projects/<name>/          ← personal, gitignored: rules of each project
└── state/                    ← personal, gitignored: pending operations, handoff
```

1. **SessionStart hook** reads `genius.config.json` and injects the persona and one mode:
   - **ONBOARDING** — the config is empty or incomplete. Claude leads you through the setup step by step.
   - **MONITORING** — the config is full. Claude reports the snapshot (problems first), checks every service, and lists what needs your attention.
2. **Every reply** ends with one status line per project (`bin/status --line`) and one 🔊 line — the short spoken essence.
3. **Voice hooks**: the Stop hook speaks the 🔊 line; the PreToolUse hook speaks important work statuses; the UserPromptSubmit hook catches «sound on» / «sound off».

### Product and personal data

The repository holds only instructions, scripts, and templates. Your data never goes into it.

| product (in git) | personal (gitignored, created on your Mac) |
|---|---|
| `AGENTS.md`, `CLAUDE.md`, `.claude/`, `bin/`, `persona/`, `voice/bin`, `voice/lib`, `voice/sounds`, `voice/*.json`, `projects/_template/` | `genius.config.json`, `projects/<name>/`, `state/`, `backup/`, `voice/.venv`, `voice/models`, `voice/run`, `voice/build`, `voice/tools` |

The config is bound to its folder and Mac. If you copy or move the folder, Claude asks one question: continue with this config, or start fresh.

## Requirements

| what | needed for |
|---|---|
| macOS | everything (the voice uses macOS audio, launchd, and a Swift key listener) |
| [Claude Code](https://claude.com/claude-code) | everything |
| Xcode Command Line Tools (`xcode-select --install`) | the voice |
| `gh` CLI, logged in | GitHub checks (PRs, CI runs) |
| `ffmpeg` (`brew install ffmpeg`) | dictation (F5) only |
| Vercel CLI / MCP, RunPod MCP | only if you use these services |

Voice download: about 2.4 GB for both languages, plus about 1.6 GB for dictation. Models are public (Hugging Face); no account or token is needed. `voice/bin/doctor` tells you if your Mac has enough RAM and disk.

## Setup

1. Clone the repository:

   ```sh
   git clone <repo-url> genius-ai
   cd genius-ai
   ```

2. Start a session:

   ```sh
   bin/genius        # or: claude
   ```

3. Answer the Claude Code prompts:
   - «Do you trust this folder?» → **Yes, I trust this folder**.
   - Permission for the setup scripts (`voice/bin/doctor`, `voice/bin/install`, `voice/bin/speak`, `afplay voice/sounds/…`) → **Yes, and don't ask again**. These are the project's own scripts; they delete nothing and send nothing anywhere.

4. Follow the onboarding. Claude asks one question at a time and shows the progress:

   | step | what happens |
   |---|---|
   | 1 · Start | `voice/bin/doctor` checks the Mac. You choose: voice and text, or text only. |
   | 2 · Voice | Claude installs the models into `voice/`, plays four voice samples (uk/en × female/male), and you pick one. |
   | 3 · Persona | Language and gender (text-only mode), then the tone and its dials (humor, directness, detail). |
   | 4 · Projects | You name the folders to look in. Claude lists what it finds; you pick the projects. Claude studies each one itself and writes its rules. |
   | 5 · Services | Claude detects GitHub accounts, remotes, Vercel, RunPod, and prod URLs itself. You only remove what you do not need. |
   | 6 · Finish | Read-only prod access per project, an optional private backup, and a final review of the config. |

5. After onboarding, each new session starts in MONITORING mode.

New hooks and settings work from the next session.

## Daily use

| command | what it does |
|---|---|
| `bin/genius` | start a session; Claude speaks first |
| `bin/status` | live state of all projects, problems first |
| `bin/status --line` | one line per project |
| `bin/status --json` | machine-readable state |
| `bin/genius save <folder>` | pack your personal data into a private archive outside genius-ai |
| `bin/genius restore <archive>` | unpack a save into a clean install and bind it to this Mac |
| `bin/genius new` | start fresh; the old personal data moves to `backup/<date>/` (nothing is deleted) |
| `bin/genius adopt` | bind a moved or copied config to this folder and Mac |
| `bin/genius where` | show where the config was made and where it runs now |

In the session, talk normally:

- «Work with `<project>`» — Claude reads the project rules and the current state.
- «Where do we go next?» — Claude gives a verdict: go on, blocked (and why), or one exact question.
- «See you tomorrow» — Claude writes `state/handoff.md`; the next session starts from it.
- «Change the tone to business», «more jokes», «shorter» — the tone changes at once.

### Add a project later

Ask Claude to connect a project. A project is connected only when all of this is done:

1. `{ "name", "path" }` in `genius.config.json` → `projects`.
2. Tag, spoken name, and priority in `projects/<name>/project.json`.
3. Summary, rules, release procedure, and «Where to dive» in `projects/<name>/AGENTS.md`.
4. Its prod URLs in `genius.config.json` → `services`.
5. Read-only prod access, if it has a prod.
6. Operations already in progress in `state/operations.json`.
7. `bin/status` shows the project with its tag, and its URLs pass.

## Voice

Everything for the voice lives in `voice/`. Nothing is installed globally: no `~/.local`, no `~/Library/LaunchAgents`.

| engine | language | use |
|---|---|---|
| StyleTTS2 | Ukrainian | spoken replies |
| Kokoro | English | spoken replies |
| Whisper large-v3-turbo | any | dictation (F5) |

| command | what it does |
|---|---|
| `voice/bin/doctor` | what this Mac has and what is missing; changes nothing |
| `voice/bin/install --lang all` | install the environment and models into `voice/` |
| `voice/bin/speak "text"` | say a text |
| `voice/bin/speak on \| off` | spoken replies on / off |
| `voice/bin/speak voice female \| male` | switch the voice gender |
| `voice/bin/speak test` | say a test line in each language |
| `voice/bin/dictate-setup start \| stop \| status` | the F5 listener |

- Type or say «sound on» / «sound off» (or «увімкни звук» / «вимкни звук») to toggle speech.
- **F5** (🎤) starts dictation; F5 again stops it. The text goes into the focused window.
- **⌃⇧5** toggles a screen recording with the microphone (saved to `~/Movies`).
- After a reboot, F5 works again once you open Claude in this folder.
- Dictation settings (env): `DICTATE_PAUSE`, `DICTATE_WAIT`, `DICTATE_MAX`, `DICTATE_IDLE`, `DICTATE_SEND`, `DICTATE_LANGS`.

## Rules for agents

All agent rules are in [`AGENTS.md`](AGENTS.md). Write new rules there, not in `CLAUDE.md`.
Project rules go to `projects/<name>/AGENTS.md`.
