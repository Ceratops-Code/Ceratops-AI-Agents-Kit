# Retrieval and Helper Contract

## Choose the narrowest complete source

Inspect the current task-history tool schemas before use. Use `list_threads`,
`list_archived_threads` and `read_thread` when available; preserve returned task
titles verbatim. Enumerate selected hosts and paginate archives. A capped active
list is not a complete inventory. Keep ChatGPT coverage separate when its
archive enumeration is unavailable.

Use `scripts/audit.py` from the installed skill directory. It makes no network
or model calls. Its local adapter enumerates the versioned Codex SQLite index
read-only, including archived entries, and scans actual rollout messages without
excluding tasks by title, update time or file modification time. This can require
substantial disk reads in a large history; use only for an executed history audit.

The existing credit-analysis collector targets execution and usage records,
not a complete cross-format misunderstanding inventory. Do not invoke its model
pipeline for retrieval. Reuse the maintained app reader for legacy reconstruction
and compacted history rather than treating the local adapter as a replacement.

Export exact `read_thread` payloads, individually or as a page array, to a
task-temp file through the available file tool. Remove tool outputs and reasoning
before export; retain complete `userMessage` and visible `agentMessage` items,
task metadata, timestamps and pagination evidence. Inside one `functions.exec`
call, select subsequent cursor calls from returned data without extra model
decisions; yield for user updates at meaningful boundaries. Do not infer that
the first page or an empty recent index proves no earlier messages exist.

Page through the frozen window plus enough older context to find the challenged
answer. Follow quoted message IDs or text to earlier pages if necessary. A known
What example must be found or recorded as a coverage failure, never silently
dismissed. Capture missing records, truncated content and unreturned attachments
as gaps; inspect relevant attachments when accessible. Do not mark a source
complete until pagination and required context were actually checked.

## Collect

Create a UTF-8 JSON request. Only caller-selected new output paths are written.
Use portable paths in reusable prompts; actual request paths resolve locally.
The task-temp root must be `<repo-parent>/tmp/<repo-name>/<task-name>`.

```json
{
  "version": 1,
  "mode": "history",
  "days": 14,
  "end": "2026-09-07T12:00:00+03:00",
  "timezone": "Asia/Jerusalem",
  "task_temp_root": "<task-temp-root>",
  "request_disposable": true,
  "local": true,
  "hosts": ["local"],
  "sources": [],
  "coverage_notes": ["Only the selected local Codex host is covered."]
}
```

`days` is positive elapsed 24-hour days; the end defaults to invocation time.
The helper freezes UTC bounds `[start, end)` and display timestamps. Omit `end`
to use now, never copy the illustrative timestamp above into a real audit.
`codex_home` optionally selects the local index root; otherwise use `CODEX_HOME`
or the standard user Codex directory. `task_ids` narrows to exact task IDs or
Codex task links. `project` matches an exact indexed project ID or working path.
Host selection constrains inputs; it does not connect to or discover remote hosts.

`assistant_scope: "final"` selects user messages and assistant answers with
`final` or `final_answer` phase evidence. In rollouts, an exact match to the
turn's completion answer supplies that evidence when its phase is absent.
Unmarked assistant messages are omitted with a coverage gap. The default,
`"visible"`, also retains assistant commentary. Both modes exclude reasoning;
tool records are never conversation messages. Reader content-reference
placeholders are reported as unavailable bodies. The packet and ledger record
the selected scope.

Each explicit source names `format`, `path` and `task` with `id`, `title`,
`host`, `kind` (`codex` or `chatgpt`) and available `archived`, `project` or `url`.
Formats are `app` for exported reader pages, `rollout` for a named JSONL session,
and `exchange` for a JSON array of pasted messages with `role`, `text`, optional
`id`, `turn_id` and timezone-bearing `timestamp`. An app source also supplies
`complete: true` only after its required pagination and context were verified.
Set `disposable: true` only for a task-created export beneath the task-temp root.
An exchange may supply `phase` or `channel` for final-answer selection.

For `mode: case`, omit days, restrict sources to one task or pasted exchange,
and supply `selector` with `item_id`, `timestamp` or `quote`. All supplied fields
must match one user message; an ambiguous quote requires a more exact selector.
Local case retrieval requires exactly one `task_ids` entry and never enumerates
other tasks. Context defaults to six preceding and twelve following visible
message records; use `context_before` and `context_after` only to obtain missing
case context. Only the selected message is counted; later clarification messages
can be retained in its chain without becoming a history-wide audit.

An optional `control_example` contains `task_id`, `quote` and, when known,
`expected_count`. The packet records deterministic matching IDs and count;
explain any mismatch before drawing conclusions. A marker match is not semantic
confirmation. Copied UUID turns and repeated storage representations retain
their source appearances; generic IDs stay task-local and edits remain distinct.
Review uncertain lineage rather than merging equal What text across turns.
The maintained reader owns a current user message when its task and turn
identity match; rollout versions remain attached as evidence, not extra logical
messages. Reconstructed windows align only ordered multi-message copies. Their
capture time is never presented as the original message time.

```text
python scripts/audit.py collect --request REQUEST --output PACKET
```

The packet contains the frozen window, coverage manifest, literal patterns,
in-scope messages, bounded context, raw/visible text separation, source locations,
copied appearances and candidate IDs. Missing timestamps are coverage gaps, not
substituted task dates. Undated candidates remain reviewable when they could
belong to the window, but confirmed undated cases are counted separately from
confirmed cases in the window. Local compaction warnings require reconstruction
through the app reader. Combine its exports with the local source instead of
discarding legacy messages that the reader did not return. Add source exports
and recollect a new packet if necessary;
reuse already exported pages and the frozen end time. Keep a superseded packet
only while its evidence is needed, then remove its exact task-owned path.

## Review and finalize

The installed helper can page every eligible user message without a keyword
filter, then retrieve the selected task's conversation or source coverage:

```text
python scripts/audit.py inspect --packet PACKET --view users --offset 0 --limit 25
python scripts/audit.py inspect --packet PACKET --view conversation --task-id TASK_ID --offset 0 --limit 25
python scripts/audit.py inspect --packet PACKET --view coverage --offset 0 --limit 25
```

These commands use the installed skill directory and managed-runtime prefix
defined in `SKILL.md`. `PACKET` is the collector output; `TASK_ID` comes from
its message metadata. Each page returns `total`, `next_offset` and the packet
hash. A null `next_offset` ends that view. Inspection writes nothing, preserves
the selected assistant scope, and removes HTML annotation comments only from
the display; the packet retains the exact evidence.

Create a review JSON naming `packet_sha256`. `decisions` contains one object per
literal/variant candidate and each additional semantic candidate. Each object
has `id`, `status` (`confirmed`, `excluded`, `ambiguous`) and `reason`. Confirmed
cases also require `failure_type`, `missing_connection`, `better_answer` and
`excerpts`: objects with `message_id` and verbatim `text`, including the challenged
assistant evidence. Use `qualifying_item` for a mixed numbered user message.
Excerpts may include earlier user context or later clarifications as well.

Place ordinary signal-free messages in `swept_non_candidates` by ID after
semantic review. Every in-scope user message must appear in exactly one of these
two sets; the helper refuses missing, duplicate or unknown decisions. It cannot
prove that a semantic judgment or better answer is correct.
Inspect attached source versions when a user edited the same turn; identify the
qualifying version without counting one logical message repeatedly.

Optional `chains` contains disjoint ordered `message_ids` and an `assessment`
of the repair attempts and preservation of the original question. Context-only
user messages may extend a single-case chain. Supply `control_check` when a
control example was given. `common_causes`, `recommendations` and `uncertainties`
are arrays of report-ready prose; recommendations must identify evidence, owner,
alternative, regression boundary and recurring cost. Governance-generated exact
rule proposals may be included as prose without being applied.

```text
python scripts/audit.py finalize --packet PACKET --review REVIEW --ledger LEDGER --report REPORT --cleanup
```

Success emits `OK`; errors emit one actionable line and retain intermediate
evidence. Finalization checks the exact packet hash, decisions and excerpts,
derives counts, writes distinct new JSON and Markdown deliverables, and reopens
both before cleanup. The ledger preserves all candidate decisions, provenance,
source gaps and clarification chains without ordinary unrelated message bodies.

`--cleanup` removes only the unchanged packet, review and explicitly disposable
request/source exports inside the verified task-temp root. It never deletes
source sessions, user-owned exports or final deliverables. A cleanup error after
publication means deliverables exist with retained temporary files; report the
specific debt instead of rerunning against the same output paths.
