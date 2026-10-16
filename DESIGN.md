# Design: conversation projects, subsessions and archival

Telegram is the only supported chat platform. Source references (`file:line`) point into
Hermes Agent v0.21.x and are indicative; they drift between Hermes releases.

## Identity, storage and authority

A project is identified by authenticated gateway route plus native conversation identity.
`pre_llm_call` or a tool's trusted host `session_id` creates it implicitly. Tool arguments
cannot supply a session ID or route. Existing native compression ancestry is read from
state.db in read-only mode, using bounded ancestry checks with the native reset-boundary and branch/delegate/reset
exclusions. Every intervening parent must have end_reason=compression. Checks stop at a trusted generation: compression after /new remains in its
new project without traversing into earlier reset ancestry. Worker resume validation
remains unchanged. If lineage evidence is
unavailable, a new identity gets a separate project conservatively. A persisted alias
maps each known (route, session ID) to its project. `/new` never rewrites old projects.

All tools require Telegram platform and an admitted or previously persisted project
route. Explicit project_id only accesses that profile's projects on that exact route;
default is the current host session's project. Old-project status, results, cancellation,
acknowledgments and approval decisions remain available after /new on that route.
New thread routes do not gain authority over old projects. Tool handlers additionally
require trusted caller session identity to match the target project for create/submit/restore.
Old project IDs from a different/newer conversation may inspect results/cards/status, cancel,
approve and acknowledge; they cannot start new work or restore old subsessions. The safe error
suggests starting a new subsession in the current conversation. The host session ID, never
model arguments, determines this gate; proven compression continues to match the project.

Schema 5 removes cwd from active projects and coordination_roots from active workstreams;
legacy declarations survive in audit-only tables. It adds conversation aliases, stream
name/card, per-task workspace override and append-only terminal-task card updates. Existing
project/stream/task/run/event IDs and native session IDs remain. Migrations are under the
existing singleton schema lock and transactional SQLite migration protocol. SQLite must
support DROP COLUMN (3.35+). Schema 6 adds stream created/idle_since/last_used/archived_at timestamps, initialized from
project creation and recorded task finishes/submissions, preserving all prior evidence.
Schema 7 persists roster_state(project_id, session_id, digest). Both pre-6 upgrades and
schema-6 upgrades seed active idle_since to the upgrade time; last_used retains historical
finish/submission evidence for LRU. Already archived streams remain archived. The first
age sweep gets a full grace period; capacity reconciliation still applies independently.
The grace lives in 007 only; shipped migrations (006 included) are never edited, since
databases that applied them do not re-run them. Schema 8 disambiguates legacy duplicate
stream names (SQLite 3.25+ window functions). Schema 9 adds inline-creation intent metadata
without changing prior task/session/name/roster/archive evidence. Shipped migrations are unchanged.
Registry remains profile-owned, 0600, WAL, with 0700 data dirs.
Legacy multiple directory projects on the same route/session are preserved separately;
the earliest becomes the implicit project. Other legacy IDs remain explicitly queryable;
no histories are merged without evidence or silently moved.

New streams get `plugin-data/project-sessions/workspaces/<stream-id>` (0700). Explicit
workspaces must exist locally; no ancestor/project-root restriction applies. Submit's
workspace applies only to that task and participates in request idempotency. At claim,
use override or persistent stream workspace. One active/uncertain run per stream plus
ancestor/descendant workspace overlap locks preserves FIFO and safe cooperative scheduling.
Locks coordinate working directories, not every file a full-permission worker can touch.

## Responsive main, roster and cards

The static Telegram-only section explains conversation projects and background sessions
that retain history. Routing is decided first: searches, several steps, commands or files go to a related
or new subsession; immediate replies can stay direct. When unsure, use a subsession. Returned results are relayed, and follow-ups
reuse the same subsession. Four short examples and one-line submit/status call shapes
make this usable without discovery round-trips. There is no main tool gate or rule
precedence list. Operational constraints stay in tool descriptions, the brief reference
skill, actionable errors and code. The section is byte-stable with no roster/config data.

Nine tools: project_submit only creates with new_workstream {scope, name?, workspace?}, plus request_key/task/user_request/criteria/authorization.
project_id defaults to the current conversation; workspace at the top level remains a
per-task override. New topic creation and task insertion share one BEGIN IMMEDIATE:
capacity/LRU archive changes, unique folded name assignment, task insertion and persisted
creation intent commit together. delegation_requests(task_id, spec) in schema 10 stores
normalized scope/name/persistent workspace intent. tasks has UNIQUE(project_id, request_key).
A matching retry returns the same task/stream without another create or name suffix;
Changed intent, task data or target mode within that project is refused. Another project
can reuse a key independently. Schema 10 rebuilds the two idempotency tables, preserving
all task IDs and child references, with foreign-key validation before commit. Current-project host identity gating runs before both new/existing
submission modes, so inline creation cannot bypass the old-project rule. Existing
submissions still enforce stream ownership/archived state and FIFO/resume semantics.

The shared creation helper applies name folding/deduplication and the capacity cap.
If the transaction fails, its newly created empty default workspace is removed with
rmdir; user files/directories are never deleted. Existing explicit workspaces are untouched.
The reply includes id/state/workstream_id/name/default_workspace, so no create/status call
is needed to acknowledge and route the next follow-up. Standalone stream creation remains
available for management.

## Deferred-tool fast path (verified host)

Hermes defers plugin-toolset tools (tools/tool_search.py:149–177); current config has no
per-plugin eager opt-out. The actual advertised bridge is tool_call({"calls":[{"name":
"project_submit","arguments":{...}}]}), exactly one local entry. The same shape calls
project_status with empty arguments; project_id defaults to the admitted main conversation.
No tool_describe/search state is required: model_tools.py:703–749 resolves the named call,
checks the session-scoped pre-assembly catalog, validates against the concrete schema and
redispatches it. tools/tool_search_validation.py:74–135 explicitly supports unseen schemas;
:139–188 accepts calls-array and legacy-single shapes. The prompt advertises the actual
calls-array form and all required fields, allowing routine delegation in one tool round.
If deferral is off, direct invocation uses the same arguments. Invalid targets are rejected
by the plugin even if native schema validation is unavailable; schema oneOf expresses the
mutually exclusive targets for the native bridge. A model-free installed-host test exercises
the bridge resolution/scope/validation without invoking tool_describe or calling a model.

Only after_memory is accepted for sections (hermes_cli/plugins_dispatch.py:70;
plugins.py:957–977); moving the section before SOUL/core text is unavailable. This change
front-loads the rule inside its supported section. Model-free tests cover prompt
freeze/restore. Earlier versions needed several tool rounds (search/describe/skill load)
before the first delegation; first-round model adherence is not covered by tests.

`pre_llm_call` returns `{'context': roster}` only on changed/first-session snapshots. It renders a JSON-escaped line per stream:
ID/name/scope/workspace, live state/queue count and a short context summary. JSON escaping
keeps embedded newlines on one line. Archived streams are omitted, as are results of old projects.
Hermes stores each turn's hook context in the user message's api_content sidecar and replays
it on later turns (agent/turn_context.py:933-977, :1298-1305), and spills hook text over
hooks.output_spill.max_chars (default 10,000) to a file. The roster therefore folds
whitespace/control characters, truncates fields, and stops at 8,000 characters with an
"N more ... omitted; use project_status" line. Full cards include all completed task
IDs/outcomes/artifacts and the latest 20 stored updates (updates_omitted counts the rest). States
are computed from task/approval records: idle/running/queued/awaiting approval, plus
cancel_requested/uncertain when applicable. Stored context reflects the latest worker
update; task conclusion/artifact history is retained independently.

The worker query requests a delimited `<workstream-card>` JSON trailer with cumulative
context, this task's conclusions and artifact paths. A linear scan (no regex backtracking)
runs before the finish transaction and picks the last valid trailer. Fields must be text and
pass the credential check on their full value. They are then truncated: context to 1,500
characters, at most 10 conclusions and 10 artifacts of 300 characters each. Unknown keys are
ignored, and invalid or deeply nested JSON falls back.
Malformed/missing updates fall back to a bounded final-result excerpt and result artifact;
failed/cancelled/interrupted outcomes also append a fallback at finish. The transaction
stores task outcome, update, latest card and completion event together, exactly once per
finish. Uncertain live runs have no terminal update until reconciled. Cards are untrusted
worker claims. There is no mandatory review state/worker or automatic verification.

Roster change suppression uses a SHA-256 hash of the bounded rendered snapshot plus native
session ID, persisted under the project. Emissions carry a session/hash marker in user context.
On unchanged turns the hook examines the most recent roster-bearing `api_content` in the host's
conversation_history and emits nothing only if that snapshot/session matches. Missing host
persistence causes a safe resend; new native IDs (including compression) force a full snapshot.
No system text or tool schemas are mutated. Marker + roster remains under the default 10k
host spill threshold. The state records emission, not provider receipt; history evidence
provides the best available confirmation that the snapshot entered replay. Persisted older
rosters still cost history tokens, but unchanged turns do not accumulate extra copies.

## Current-conversation injection and /new routing

On the gateway loop, after trusted-origin recovery and exact route/profile/current
native authorization checks, delivery reads adapter.gateway_runner.async_session_store
.lookup_by_session_key(route). The native entry's session_id must equal the project's
conversation alias or be a proven compression descendant without crossing reset/fork
boundaries. The supported ctx.inject_message(content, role='system', origin=origin)
then schedules a current-conversation model turn. No await intervenes between this
last identity check and the injection call. Payload includes result_summary (up to 2000
characters), full card update and project/workstream/task/run/event IDs. Worker text is
data, never authorization. Main relays findings, links/artifacts and worker-stated caveats,
and sends follow-ups to the same subsession. The event header gives this brief relay
guidance with the short "as reported; don't redo or verify it here" clause. Completion metadata
states that the outcome is worker-reported, without demanding main verification.

If the native entry names an old project's successor (/new), delivery sends a plain
visible Telegram notice labelled with the originating conversation/project/stream/task;
large notices use a UTF-8 attachment. Unknown/missing native current-session evidence
leaves a result pending for retry rather than guessing old/current. Archive notices remain plain, with a conversation label for non-current projects.
Native approval requests are card-only: the outbox request waits for approval_cards.state=sent,
then consumes delivery without sending another message. Current approval_resolved events use
one short injected ID/decision/state summary, omitting raw JSON/command/reason. Old outcomes
with sent cards are consumed silently; without a sent card, emit a short labelled outcome.
Card callbacks/typed votes/native deadlines retain their authority and atomic decision path.
All SDK calls stay on the gateway event loop. No native store session is created by lookup.

**Accepted residual:** current-session lookup and host dispatch are not atomic.
/new between check and actual dispatch may inject an old result into the new context.
Origin injection explicitly follows whatever is current when dispatched. A checked read
cannot guarantee a generation-bound target; closing the gap requires a host API accepting
expected conversation generation, plus a durable receipt. Each event is checked separately
immediately before scheduling; no destination is supplied by model text. The same physical
Telegram thread cannot isolate old plain notices into its previous model history.

Only a gateway delivery host dispatches; CLI/cron do not consume attempts. Accepted
injections never resend, even if unacknowledged. False/exception retries 10–300 seconds;
no fallback plain send after a refused current injection. True means scheduled, not
actual dispatch/read; plain True means SDK send completed. Send/accept-before-DB crash
ambiguity and host async rejection can respectively duplicate or lose an accepted event.
Current authorization and canonical profile/source key checks remain mandatory. Missing
origins, deleted topics and profile routing changes do not cause target guessing.

## Subsession archive lifecycle

Gateway settings: archive_idle_days (positive number, default 3; max 3650),
max_active_workstreams (integer 1–1000, default 10). Each main plugin instance passes these
to its Registry. Executor/worker registries never sweep or create streams, so their defaults
do not affect lifecycle decisions. Only the gateway delivery loop sweeps, every 60 seconds
starting on registration; CLI/cron do not archive or consume notification attempts.

An eligible idle stream has no queued/running/cancel_requested/uncertain task and no
pending approval. idle_since initializes at creation and updates at task finish, failed
launch, queued cancellation. LRU last_used updates at submission, finish and restore;
passive inspection/search/roster does not prolong a stream's lifetime. Explicit restore
starts a fresh idle window for that new active period to prevent immediate rearchival
before the user can submit a follow-up. Creation/restoration transactions archive the
least-recently-used eligible idle streams needed to make capacity; if insufficient idle
slots exist, the transaction refuses with a safe actionable error. Busy work is never
archived. The periodic sweep first handles age and then a lowered cap; a previously busy
over-cap population waits until eligible rather than canceling user work.

Archive sets archived_at only; it never deletes native history, card updates, task/results,
or workspace. Roster hides it; status/full card retains archived state. workstream_card:
workstream_id defaults action to get; no ID defaults to list of archived cards, with an
optional case-insensitive query across name/scope/workspace/card/summary/update history.
action=archive/restore requires ID and exact project/route authority. Submit to archived
streams refuses until explicit restore. Restore keeps session_id/root_session/workspace;
native --resume continues the same tip and resolves its compression chain as before.

Automatic age/cap transitions and one archive notice per project/operation commit in the
same BEGIN IMMEDIATE transaction. Lifecycle notices reuse the durable outbox with kind
workstreams_archived; task/workstream/run fields are empty because this is a project event,
not worker output. Payload retains count/full names. SDK rendering emits one bounded plain
line, `Archived N idle subsession(s): A, B, C` (long lists abbreviated, full event retained).
These never inject/start a model turn; non-current projects add the originating conversation label after native-current-session evidence checks. Manual lifecycle changes return tool results without
an extra automatic notice. Repeated sweeps cannot produce duplicate notices for already
archived rows. Gateway restart resumes pending notices, with the same acceptance/crash limits.

## Approvals, workers and process lifecycle retained

Native buttons and typed authenticated fallback retain the previous request/decision bridge.
Cards use escaped HTML with bounded name/task/command/reason previews and local expiry,
in one message. Internal IDs and the typed fallback line are hidden; authenticated
project_status retains request IDs and full actions. Clicks, typed decisions, timeouts and
cancellations edit the sent card to its outcome without buttons (typed/timeout edits are best effort).
The request binds to durable random token and exact bot/chat/thread/message. Callbacks
canonicalize the presser's source and require literal native authorization True. Votes
resolve atomically, one winner; replay shows the prior outcome. Typed fallback records only
exact approve/deny ID intake; model decision requires that vote and the originating project.
Same route after /new can still resolve old requests; a Telegram session generation is not
a separate human identity. Buttons are the reliable path during busy/slash intake interception.
30-minute default deadlines keep running across outages; cancellation/expiry deny/withdraw.

Workers use the profile's full tools/credentials/network/filesystem. No tool allowlist,
Landlock or writable-root confinement. Per-run managed overlay profile-mode/deny + timeout,
empty command allowlist, builtin transport, yolo off. Existing admin managed scopes refuse
unreviewed composition. execute_code scripts require no per-script approval; nested terminal
commands still check original bytes before tracking wrappers and reuse native approval only
for identical command/reason in the same invocation. Middleware catches failures fail-closed;
thread-local native approval callbacks bridge waits through durable registry requests and
human_wait_window. Interrupt withdraws the request before a late vote can run an abandoned tool.
Configured sudo password only; no stdin/stdout prompt corruption. No persistent grants.

Launch scrubs all inherited HERMES_/TERMINAL_/PROJECT_SESSIONS_ variables and MESSAGING_CWD,
pins profile home/TERMINAL_CWD and per-run TMPDIR/TMP/TEMP, and uses native stream-json query
CLI with --resume/--no-restore-cwd. State.db lineage validates returned native identity.
Tool deltas/raw sensitive logs are discarded; results/actions use existing conservative filters.
Non-JSON stdout lines (Hermes prints quiet status such as "⟳ compacting context…" even in
stream-json mode) are discarded unstored, up to 100; malformed `{` lines remain protocol errors.
The per-run TMPDIR is removed once the worker group and every recorded session are gone: killed
workers leave Hermes terminal snapshots there, which contain environment values including
provider keys. Interrupted runs retain their TMPDIR inside the 0700 runs/ directory until recovery
confirms their processes stopped, then removes it before ending the run.
The worker's own session is recorded at launch with the terminal/descendant sessions, so helpers
that leave its process group but stay in its session (Hermes shell hooks use
Popen(process_group=0)) still count as live and are stopped.
Cancellation stops verified process group and recorded terminal/descendant sessions before
unlocking. Descendants orphaned between 0.5-second scans remain a limitation; remote lifecycle
is unverified. Restart cleanup stops proven survivors, marks the run interrupted and never requeues it automatically.

## Observed Hermes behaviour (documented, not changed)

- **Compression mode.** Hermes defaults to `compression.in_place: true`: compaction keeps the
  session id and soft-archives old rows. The compression-alias and lineage code is built for the
  rotation mode (`in_place: false`, new child, parent ended `compression`). Both were verified;
  with the default, the main conversation and workers simply keep their id, so no aliases appear.
- **Compaction fragility (Hermes core).** One refused compaction ("compressed transcript would be
  larger") triggers an ineffective-compression backoff, and preflight compaction on `--resume`
  is skipped in a fresh process. A worker stream can therefore grow well past its threshold
  across several tasks until an in-turn trigger succeeds.
- **Git context in workers.** A workspace inside a git repository gives the worker that repo's
  status in its prompt, so cards may mention unrelated repository state.

## Optional reset-policy integration

Core timer resets are retired (hermes_cli/session_reset_retirement.py:1–8). The optional
hermes-session-reset-policy plugin restores idle/daily session_reset behavior. Its new native
session identity creates a new project, not a compression alias; old workers remain attached
to the former project and subsequent results use labelled plain notices. New-conversation
callers cannot submit/create/restore into it. This plugin neither installs nor configures it.

## Verified host interfaces (read-only source references)

| Interface | Source in installed Hermes | Use/limit |
|---|---|---|
| pre_llm_call kwargs session_id/platform; dict context or string result | agent/turn_context.py:793–846 | Called in turn prologue; no persist-disabled agents |
| User API context composition/stamp | agent/turn_context.py:111,1172,1284–1305 | Appended to current user message, system unchanged; sidecar replays historical wire bytes for prefix stability |
| Static callable prompt freeze/restore | agent/system_prompt.py:86–167 | Telegram-only role remains frozen; activation needs fresh conversation |
| Plugin hook registration | hermes_cli/plugins.py:933 | Existing supported host hook, no core patch |
| Admission source/session key | gateway/run_inbound_consumer.py:43–52 | Authorized non-internal intake; no conversation ID supplied here |
| Tool dispatch session_id | model_tools.py:_execute_tool; tools/registry.py:_kwargs_accepted_by | Trusted native ID, not model tool args |
| Native current route entry | gateway/session.py:773–781,1319–1325 | AsyncSessionStore wraps read-only existing-entry lookup |
| Native resume reset boundaries | hermes_state_messages.py:resolve_resume_session_id; hermes_state_common.py:178–183 | Compression continuity; reset/fork excluded |
| Injection current lookup and origin path | gateway/run_plugin_injection.py:128–152,218–245 | No expected-generation injection; used after native current-session check |
| Telegram handler factory and reconnect | hermes_cli/plugins.py:829–850; gateway/platforms/base.py:2322–2331 | Connected PTB Application/adapter on loop; hot reload limitation remains |
| Source/profile/authorization/source key | gateway/platforms/base.py:2412,2454,2561,4813 | Reused for cards and plain notices |
| Topic normalization | plugins/platforms/telegram/adapter.py:1092–1096,5872–5886,7073 | Group mapping; General topic ID 1 maps to no send thread |
| SDK send_message/send_document use | plugins/platforms/telegram/adapter.py:_send_exec_approval_prompt,send_document | Existing SDK; no new dependency |

Native/model-free tests cover these contracts, original approval guards, and cached prompt
freeze/restore, without Telegram or model calls. Not covered by tests: real
/new/new-thread and compression timing, busy typed fallback, pairing/allowlists,
reconnect/restart, provider caching, model card adherence and full cancellation.

## Gateway child executor lifecycle

Gateway registration and submit/restore ensure a live profile executor; the delivery loop
retries with persisted backoff. A launcher flock serializes probes and the executor flock
remains the single-instance authority. The executor is a plain child, preserving PATH
and staying in the gateway cgroup. Unload requests clean termination of this gateway's
Popen child (Hermes runs unload on plugin disable/reload, not on gateway stop); the
gateway unit's SIGKILL/cgroup reaper handles shutdown and the next start reconciles. A
cgroup-wide SIGTERM that kills a worker before the executor's stop flag still ends the run
interrupted. Backoff resets in a fresh gateway process, so a restart is not a crash loop.
No systemd-run/setsid path remains.

Gateway restart interrupts active work. Once surviving processes stop, reconciliation
ends the task/run as interrupted with a short reason through the existing finish/event
path. It preserves the latest validated native session, denies pending approvals and
releases locks. Repeated reconciliation is idempotent and emits at most one interruption
event. Tasks already queued remain queued; interrupted tasks never requeue automatically.
The main can submit a fresh task to the same workstream to continue its native history.
There is one run per task.
Cancellation wins; version handoff still drains active work before replacement. Failed
launches return offline notes while keeping queued work. No separate unit is managed.

## Create, send and steer

The public submit schema has no workstream_id; existing-subsession messages use send.
The internal submit transaction remains shared by creation and send queueing. Schema 11
adds steer_run_id and steer_accepted to tasks. A pending steer stays queued but reserved
from normal claiming until its target run finishes. The worker's post-tool hook uses the
context-bound native agent's steer method; Hermes adds its own standalone user row and
marker after the tool batch. Acceptance is reported, but final delivery requires matching
that message's unique ID header in native persisted user history (Hermes strips the text). Otherwise finish/interruption
clears the reservation and leaves it queued. Cancelled message tasks cannot be steered.
No new event transport, Hermes edits or model calls are required.

The managed overlay omits approvals.mode, inheriting the actual profile through Hermes
effective config. The guard checks normalized manual/smart/off against native mode; no global yolo or command
allowlist can bypass manual/smart. Native smart classification and the existing escalation
callback handle smart decisions. execute_code and nested command policy remain native.

Submit/send take a contextual `task` brief and verbatim `user_request`, shown with labels to the worker. Workers use Hermes’s native turn budget; empty timeout events state that no result was returned.
