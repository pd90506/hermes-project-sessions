# hermes-project-sessions

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that treats a chat
conversation as a **project** and runs longer work in **background subsessions**.

- The main session stays responsive: searches, multi-step jobs, commands and file work are
  handed to a subsession, and the main keeps chatting.
- Each subsession is a real Hermes session that keeps its own history (and prompt cache), so
  follow-ups go back to the same subsession instead of starting over.
- Results are injected back into the conversation when they arrive; the main relays them.
- Command approvals raised inside a subsession are routed back to the chat as an
  Approve / Deny card (Telegram), following the profile's `approvals.mode`.
- Idle subsessions are archived automatically (never deleted) and can be restored.

## Status

**Experimental, unmaintained prototype.** It was built largely by AI coding agents and has
been tested on Hermes v0.21–0.22 only, with Telegram as the chat platform. It relies on
Hermes internals (deferred tool bridge, gateway injection, approval callbacks) that may
change between Hermes releases. Use at your own risk; issues and PRs may not be answered.

Workers run with the profile's full tools, credentials, network and filesystem access.
Only native command approvals are intercepted and sent to the chat.

## How it works (short)

The plugin registers nine tools (`project_submit`, `project_send`, `project_status`,
`project_result`, `project_cancel`, `project_workstream_card`, `project_workstream_create`,
`project_approval_decide`, `project_event_ack`), a short system-prompt section, a
`pre_llm_call` hook that adds a compact roster of the conversation's subsessions when it
changes, and a Telegram callback handler for approval buttons.

The gateway starts a separate executor process (as its own child) that claims queued tasks
and runs each one as `hermes chat` with `--resume` on the subsession's native session. State
lives in a SQLite registry under `$HERMES_HOME/plugin-data/project-sessions/`. See
[DESIGN.md](DESIGN.md) for details and known limits.

## Install

Requires Hermes ≥ 0.21.6 with the gateway and Telegram configured, and `hermes` on the
gateway's `PATH`. `HERMES_HOME` is the profile you want to install into.

```bash
export HERMES_HOME=~/.hermes            # or a profile dir, e.g. ~/.hermes/profiles/<name>
mkdir -p "$HERMES_HOME/plugins"
cp -a plugin/project-sessions "$HERMES_HOME/plugins/project-sessions"
hermes config set plugins.entries.project-sessions.allow_gateway_injection true
hermes plugins enable project-sessions
```

Restart the gateway, then start a new conversation (`/new`) so the prompt section and tool
schemas are picked up.

## Configuration

All keys live under `plugins.entries.project-sessions`:

| Key | Default | Meaning |
|---|---|---|
| `allow_gateway_injection` | — | Must be `true`: enables result injection, notices and approval cards. |
| `settings.archive_idle_days` | `3` | Archive a subsession after this many idle days. |
| `settings.max_active_workstreams` | `10` | Active subsessions per project; the least recently used idle one is archived to make room. |
| `settings.worker_reasoning_effort` | `high` | `--reasoning` passed to workers; empty string inherits the profile. |
| `settings.main_reasoning_effort` | empty | If set, overrides reasoning effort for the main Telegram route only (currently OpenCode Go `chat_completions` models; others unchanged). |
| `settings.worker_max_turns` | null | Worker turn budget; null inherits the profile's `agent.max_turns`. |

Example: `hermes config set plugins.entries.project-sessions.settings.archive_idle_days 7`
(numbers, not strings). Approval mode is inherited from the profile's `approvals.mode`;
approval requests expire (deny) after 30 minutes.

## Uninstall

Make sure nothing is queued or running (`project_status`), then:

```bash
hermes plugins disable project-sessions
rm -rf "$HERMES_HOME/plugins/project-sessions"
# optional: delete the registry, subsession workspaces and run data
rm -rf "$HERMES_HOME/plugin-data/project-sessions"
```

Remove the `plugins.entries.project-sessions` block and any `project_sessions` entries in
`platform_toolsets` from `config.yaml` if present, then restart the gateway. Native Hermes
session history of past subsessions stays in `state.db`.

## Tests

```bash
python3 -m unittest discover -s tests </dev/null
```

Most tests are model-free and need only Python ≥ 3.10. Native contract tests run small
snippets under a Hermes Agent checkout (with its `venv/`) to check the host APIs the plugin
uses; point `HERMES_AGENT_SRC` at it (default `~/.hermes/hermes-agent`). They are skipped
when it is absent. No test calls a model or Telegram.

## License

MIT — see [LICENSE](LICENSE).
