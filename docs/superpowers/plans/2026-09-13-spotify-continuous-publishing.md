# Spotify Continuous Publishing Implementation Plan

> Execution is not authorized by this document. The current task is planning and independent review only. A future implementer should execute the tasks sequentially, using the repository's test and review requirements. Do not commit, push, publish, or call Spotify without the corresponding user authorization.

**Goal:** Let the standalone Spotify publisher finish a large append run through ordinary rate-limit interruptions, while reducing request bursts, renewing expired authentication, and reporting quota exhaustion accurately.

**Architecture:** Keep the existing matcher, caches, append planner, and batch writer. Add a small request-control component and a refreshable session to the existing client. A controller in the publisher resumes checkpointed passes after rate limits; it rebuilds playlist state rather than replaying stale positional writes.

**Tech stack:** Python 3.11 standard library, unittest, existing locked Pyright tooling. No new dependency.

**Spec:** The design and acceptance criteria in this document are the specification. Keep them with the implementation tasks rather than creating a second normative document.

**Baseline:** Local `main`, `334fe8574463758d69b2a6f8ba160b3628ccf225`, clean at the start of planning. Remote refs were not refreshed because this plan describes the installed local command. Revalidate the source and instructions before implementation; adapt the plan if they have changed.

**Review status:** Ready for implementation planning handoff. Independent review found no remaining material findings, and the user confirmed both policy choices on 2026-09-14. Implementation has not been authorized or started.

## Confirmed user decisions

The user confirmed both behaviors on 2026-09-14:

1. Add opt-in `--continuous` and preserve the existing bounded command as the default.
2. In continuous mode, wait through any valid Spotify-provided cooldown, including hours, until the user interrupts.

These choices confirm the behavior evaluated in the independent review; they do not change its design. Explicit quota exhaustion without a usable retry time still requires checkpointing and stopping for user action; do not invent a quota reset schedule. This confirmation finalizes the plan and does not authorize implementation or Spotify calls.

## Evidence and scope

The current command defaults to 500 new searches per invocation. It can exit with a partial report and exit code 0. Passing `--max-new-searches-per-run 0` removes that cap but does not change rate-limit retries. `client.py` retries a 429 three times and rejects waits over 480 seconds. It has no request pacing and obtains no new token while an invocation is running. The CLI fetches one access-token string before calling `publish_spotify_playlists`.

The previous diagnostic check ran 124 client, publisher, and session unit tests successfully against this baseline. That result establishes existing behavior, not implementation verification for this proposal. Recent local report summaries show both a search-budget stop and a rate-limit stop. No private collection rows or credentials belong in this plan or its fixtures.

Preserve the useful optimizations already in the code: durable positive/negative match decisions, authoritative manual matches, album lookup reuse across selected playlists, candidate validation, 100-URI append batches, and fresh reads of current playlist contents before planning writes. Positional restorations can legitimately use smaller requests; do not change their grouping as part of this work.

The first continuous mode covers the standalone publisher's **append** path and its dry-run equivalent. Existing replace mode, configured release playlist publishing, and dedupe keep their current bounded behavior. Reject `--continuous --publisher-sync-mode replace` before authentication or network calls. Repeated automatic replacement after a partial write needs a separate design; it is not required for the user's default append command.

Do not change matching rules, search result limits, collection data, playlist ownership rules, public/private checks, source ordering, URI identity, negative-cache validity, or existing persisted cache schemas. Do not add concurrent requests, a daemon, scheduling, an external queue, app rotation, or a new remote provider.

## Current primary sources

Checked on 2026-09-13 local time:

- [Spotify rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits): short-window throttling, `Retry-After`, and reducing unnecessary calls. Spotify does not publish a universal safe requests-per-second value.
- [Development-mode quotas](https://developer.spotify.com/documentation/web-api/concepts/quota-modes) and [July 2026 quota update](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates): quotas are shared across a developer account's development-mode apps; `error.reason == "QUOTA_EXCEEDED"` distinguishes this condition. Bucket details and limits can change.
- [Access tokens](https://developer.spotify.com/documentation/web-api/concepts/access-token) and [refreshing tokens](https://developer.spotify.com/documentation/web-api/tutorials/refreshing-tokens): tokens expire, refresh responses can rotate the refresh token, and an omitted replacement means retain the current refresh token. `invalid_grant` requires reauthorization, not repeated refresh attempts.
- [Add playlist items](https://developer.spotify.com/documentation/web-api/reference/add-items-to-playlist): positional insertion and a maximum of 100 items per request. The API does not give this script an exactly-once write guarantee.

The interval and fallback values below are application policy, not claims about Spotify's thresholds. Local deterministic tests cannot prove freedom from live throttling.

## Design

### 1. Command behavior and compatibility

Add two standalone publisher options:

| Option | Contract |
| --- | --- |
| `--continuous` | Keep running across completed search-budget passes and resumable 429 pauses. Default false, as confirmed by the user. |
| `--request-interval-seconds N` | Minimum spacing between API request starts. Finite nonnegative float. When omitted: 1.0 in continuous mode, 0.0 otherwise. Continuous mode requires a positive value. |

Retain `--max-new-searches-per-run` with its existing name, default 500, and meaning for ordinary runs. In continuous mode it is the search budget **per checkpointed pass**; the controller proceeds to the next pass. Value 0 remains an uncapped pass. A budget boundary never means overall success while inputs remain unprocessed.

Reject continuous mode combined with `--refresh-match-cache`: restarting bounded passes with refresh enabled would repeatedly invalidate progress. The user can do a separate bounded refresh first. Also reject continuous mode with the hidden `--access-token` override, since a raw token cannot be renewed. A continuous run needs the normal cached authorization flow. Preserve these options unchanged for ordinary runs.

`--publishing-dry-run` retains its current meaning: Spotify reads and local match/report writes are allowed; creating or changing Spotify playlists is not. `--reauthorize` runs at most once before the first pass. It must not reopen authorization on every pass or after each cooldown.

Update both `build_spotify_publisher_argv` and `build_spotify_publisher_namespace` with optional parameters/defaults for the new flags. Existing callers must keep existing behavior without adding flags to `discogs_make_playlists.py`, `discogs_release_playlist.py`, or dedupe. Tolerate older test/in-process namespaces using `getattr` defaults or normalize them in one existing argument-default function.

Future user command, after implementation:

```bash
rtk proxy python3 scripts/publishers/spotify/publish_playlist.py --continuous
```

This remains an explicit Spotify-writing command unless dry-run is selected. It is a foreground process. It is not restarted automatically after a reboot, terminal termination, or process kill. Document one publisher process per account/cache context; cross-process coordination and distributed quota tracking are outside this increment.

### 2. Central request pacing and typed rate-limit information

Create `scripts/publishers/spotify/request_control.py` for timing and rate-limit parsing only. Keep HTTP transport and Spotify endpoint methods in `client.py`; keep OAuth in `session.py`.

Use injected monotonic time and sleep for deterministic testing. Track the last actual transport-start time in one control object shared across all passes. Separate waiting from recording dispatch: wait for the request slot, obtain/refresh the token, rebuild Authorization, then record the current monotonic time immediately before transport. A slow OAuth refresh must not leave an obsolete pacing timestamp. Include searches, album-track pages, playlist inventory/pages, writes, and retried requests. Cache hits do not consume a request slot. Count actual transport attempts separately from the existing search count.

For continuous mode, on each 429 double the current interval, up to `max(base_interval, 30.0)` seconds. Do not automatically accelerate again during that invocation. This simple conservative adjustment avoids a second control algorithm. It survives pass restarts because the controller reuses the same object. An explicit server cooldown always takes precedence over this interval; 30 seconds is **not** a cap on `Retry-After`.

Parse the response JSON defensively. Only the exact nested string `error.reason == "QUOTA_EXCEEDED"` identifies quota exhaustion. Invalid JSON, scalar/array bodies, or unknown reasons remain ordinary/unknown 429 responses. Do not retain or print the raw response body.

Accept a case-insensitive `Retry-After` header only when it parses as a finite positive numeric count of seconds. Missing, zero, negative, `NaN`, infinity, and malformed values are unusable. Spotify specifies seconds; do not add an HTTP-date parser without evidence it is needed.

Represent a 429 using a frozen `SpotifyRateLimitInfo` dataclass in `request_control.py`: `kind: Literal["rate_limit", "quota_exceeded"]`, `retry_after_seconds: float | None`, `received_at_monotonic: float`, `received_at_unix: float`, and `operation_name: str`. Add `SpotifyRateLimitPauseError(info: SpotifyRateLimitInfo)` as a `SpotifyApiError` subtype in `client.py`. It holds `info` and a sanitized diagnostic. It is a run-control condition, never a track-match error or cacheable negative match. The controller passes `pause.info` to the timing component, avoiding a circular import.

Keep the legacy bounded retry policy for clients that do not enable continuous control, including dedupe and injected test clients. Add finite-value validation to the legacy header path too; this prevents invalid sleeps. Preserve the legacy 3 retries, 60-second fallback, and 480-second limit.

### 3. Response and retry policy

| Event | Continuous-mode behavior |
| --- | --- |
| 429 with usable delay, including explicit quota exhaustion | Stop the pass through a typed pause, checkpoint, wait until the full server deadline, then build a fresh pass. No API probing before the deadline. |
| Ordinary/unknown 429 without usable delay | Checkpoint and use fallback delays 60, 120, 240, 480, then 900 seconds maximum between attempts. Reset this escalation only after a pass makes retained matching progress, not after unrelated successful inventory calls. |
| Explicit quota exhaustion without usable delay | Checkpoint and exit 1 with an actionable message that the reset time was not provided. Do not retry on an invented hourly/daily schedule. |
| Access token nearing expiry | Refresh before the next Web API call, using the cached expiry and existing 60-second leeway. |
| First 401 for a logical request | Force one refresh and retry that request once with a rebuilt Authorization header. Count/pace the retry. |
| Second 401, 403, revoked/missing refresh token, or lost required scopes | Save progress and stop with an authentication/action-required error. No per-row search storm. |
| 400 query-length rejection | Preserve the existing narrow search-ladder fallback behavior. |
| Other API/transport failure, including 5xx or malformed successful response | Stop and preserve progress; do not introduce blanket transient retries in this increment. Never retry a write whose outcome is uncertain. |
| Ctrl-C | Save available local progress, stop without additional Spotify writes, and return 130. |

A terminal error must not be caught by the track-search fallback and converted into a match decision. Add a default-false `fail_on_api_error` argument to `search_spotify_track` and supply true only for continuous passes: after checking the existing narrow query-length exception, rethrow any other `SpotifyApiError`. Pause/authentication errors always propagate before that check. Keep ordinary-mode row-error handling compatible; renewable-session authentication failures propagate because continuing with revoked credentials cannot produce useful matches.

The continuous client raises the typed pause on the **first** 429 rather than sleeping/replaying inside an endpoint method. Its controller owns cooldown waiting. This ensures a positional insertion planned before a hours-long pause is not blindly resubmitted afterward.

Waits use a monotonic deadline calculated at response receipt. Checkpoint/report time counts toward the wait. Sleep in slices no longer than 60 seconds and recompute remaining time. Print the cause, remaining duration, and estimated UTC resume time at pause start and at most once per minute. If an exceptionally large finite server delay cannot be formatted as a calendar date, display the duration without a date; do not overflow or shorten it. Keep these notices visible with `--no-progress`; they are not progress-bar output. Stop signals must be responsive between slices. Delays are interruptible; no busy polling and no background timers.

For quota exhaustion without a usable reset time, and for a user interruption during a known cooldown, print and write the available reason/deadline in the report. A manually restarted process has no durable scheduler; the user must honor the reported cooldown. This limitation is explicit, not a claim of automatic resume across process death.

### 4. Refreshable session

Add `SpotifySession` in `session.py`, reusing `SpotifyToken`, `load_spotify_token`, `save_spotify_token`, `refresh_access_token`, and scope validation. Introduce only an optional token-provider protocol on `SpotifyClient`; do not alter every endpoint signature or the fake-client planning/publishing protocols.

The normal startup authorization flow remains responsible for interactive consent. Construct the live session from the authorized cached token once, then obtain a token through `get_access_token(force_refresh=False)` immediately before each transport attempt, after any pacing/cooldown wait. This avoids refreshing too early and sending an expired token after a long sleep. Endpoint methods may still accept `access_token` for compatibility; a configured provider overrides that value by rebuilding only the Authorization header.

Preserve an omitted refresh token and omitted scopes as the existing refresh logic does. Persist a rotated token atomically before using it. If persistence fails, stop; do not continue with a token that would be lost on restart. Check required scopes after refresh. Never log tokens, authorization codes, request headers, or token response bodies.

Startup may authorize interactively once. Mid-run `invalid_grant`, missing refresh token, repeated 401, or missing scopes produces a typed `SpotifyAuthenticationError` that propagates to the publisher's outer checkpoint/error handler. Do not launch a browser from a sleeping or restarted pass. Reuse existing invalid-grant cleanup semantics without deleting an unrelated token file. OAuth endpoint failures are not Web API quotas; do not feed them into the Web API 429 loop.

Keep `SpotifyClient` token-provider default `None`, preserving explicit-token clients, dedupe, and current tests. Use the provider for normal standalone publisher sessions, including long bounded runs. The hidden explicit-token path stays bounded and bypasses renewal as it does today.

### 5. Checkpointed continuation and completion

Keep one controller in `run_spotify_publish_from_args`, not a shell loop and not a new scheduler. It owns one session, one request-control object, the resolved original input selection, one report path, and cumulative request/wait counters. Each pass calls the existing publisher with the same chosen sync mode and normal caches.

Add a structured `stop_reason` field to `SpotifyPublishSummary`, with a backward-compatible final default: `complete` or `search_budget`, followed by `resume_progress_made: bool = False`. The existing human-readable `run_status` remains report text. Mark every budget return with `search_budget`; do not infer control flow from wording, exit code 0, or `search_count == 500` (a started search ladder can exceed the nominal budget).

Retain completed album-attempt keys in a controller-owned set across continuous passes. Define frozen `SpotifyAlbumAttemptKey` in `publish_types.py` from the exact ordered `tuple[PlaylistTrack, ...]`, full `tuple[SpotifyTrackCandidate, ...]` passed as fallback candidates, `search_limit: int`, and `matcher_version: int`. These frozen existing types are hashable. Add an optional `completed_album_attempts: set[SpotifyAlbumAttemptKey] | None = None` parameter to the publisher. With a supplied set, skip an album attempt only when the full key matches. Add its key only after `resolve_release_with_album` returns successfully, including a no-candidate/no-decision result; never retain an interrupted, failed, or rate-limited attempt. Save returned match decisions normally. Changed source values, fallback candidates, search limit, or matcher version invalidate reuse automatically. This is invocation-local lookup work, not a durable match verdict, and is not used by ordinary single-pass callers.

Set `resume_progress_made` only when the pass completes a new retained album attempt or caches a fresh cacheable row decision. Cache hits, refreshed `last_seen_at` values, API request counts, and failed attempts do not count. This closes the budget-1 loop: a no-candidate album lookup may consume pass one, but pass two remembers the completed lookup and advances to the track fallback instead of repeating the same album-only pass. If a budget return makes neither kind of progress, stop with an actionable error rather than loop. Resumable 429 waits remain governed by their cooldown policy, not by this budget-progress check.

Controller algorithm:

```python
# Control-flow sketch; names are the contracts defined in the tasks below.
while True:
    try:
        summary = publish_spotify_playlists(...)
    except SpotifyRateLimitPauseError as pause:
        # The publisher has saved local state before this exception reaches here.
        request_control.wait_for_pause(pause.info)
        continue  # Reload source rows and caches, and fetch current remote contents.
    if summary.error_count:
        return summary  # CLI exits 1; do not loop over uncacheable errors forever.
    if summary.stop_reason == "complete":
        return summary
    if summary.stop_reason != "search_budget" or not summary.resume_progress_made:
        raise RuntimeError("Publisher stopped without a resumable progress boundary")
    request_control.reset_fallback_after_progress()
    # The same request-control object continues pacing the next pass.
```

In ordinary mode execute one pass and preserve its current budget exit behavior. In continuous mode exit 0 only after a full pass over all selected source rows without API/search errors. Ambiguous or unmatched music still needs manual review and does not become a retry loop; display those counts and call the result "processing complete; matches need review" when applicable. This is not a guarantee that every record exists on Spotify.

Keep the selected master paths fixed for an invocation; reload their contents for a resumed pass. If a selected input disappears or becomes invalid, stop before new writes. Do not silently switch to a different playlist set or include newly discovered files after a pause. Preserve source order and the current ownership/public/private checks on every resume. Account identity must stay the same throughout a continuous invocation; a changed identity requires a stop, not a new target inventory.

Move startup inventory/current-user calls inside the protected reporting/checkpoint lifetime so a 429 before the first row also produces a clear waiting/error report. Add outer handling for the new pause/authentication errors and cancellation without converting them to row errors. When a nested append writer has already emitted a more specific partial-write report, preserve its confirmed/uncertain write information; the outer handler must not replace it with "no writes attempted".

On every resumable pause, save completed match decisions and confirmed publish-state updates before waiting. Pending matches may be cached, but pending writes are not flushed merely because the run is stopping. Resume by fetching actual playlist contents, then use existing append planning to skip confirmed tracks and restore positions. Do not persist or replay a queue of HTTP write requests.

Save after every completed release-level album resolution, every 25 completed row decisions, and existing append checkpoints. This bounds progress loss before a crash even if few rows match. Reuse atomic JSON writers; do not change cache schemas or write source CSVs. Report summaries need updates on pass boundaries, writes, pauses, cancellation, and completion, not on every transport call.

If a POST/create request times out or returns an unreadable success body, its remote result can be uncertain. Stop with that uncertainty and no automatic retry. A later manually started append run rereads current remote state. Exactly-once recovery from arbitrary crashes, simultaneous external edits, and transactional rollback of Spotify changes are not promised.

### 6. Observability and request reduction

Keep the existing per-pass match/search figures. Add clearly labeled invocation totals for actual Web API attempts, 429s, explicit quota responses, token refresh attempts, completed passes, and wait seconds. Do not sum per-pass "already present" or "tracks processed" into a fake unique-track total. Final unique/source coverage comes from the final full pass.

Use sanitized operation names and status codes in logs, never query strings, CSV values, token material, or raw API bodies. Report run state (`processing`, `waiting`, `complete`, `needs_action`, `interrupted`, `failed`) and a reason/resume timestamp where relevant; these are presentation fields separate from the two-value pass stop reason.

Do not reduce the existing search result limit, drop safety checks, or weaken matching to claim fewer requests. Do not add snapshot caches yet: a checkpointed restart must recheck live playlist contents for correctness, and the current scope has no measured evidence that an additional persistent snapshot cache beats its invalidation cost. The request counters make later decisions evidence-based.

## Implementation tasks

Implement sequentially; shared client/session/publisher code must not be edited concurrently. For each task add the specified observable regression tests first, run the focused tests to see the expected failure, implement the smallest change, then rerun that focused selection. Do not commit automatically.

### Task 1: Request control and 429 classification

**Files:** Create `scripts/publishers/spotify/request_control.py` and `tests/spotify/test_request_control.py`; modify `client.py` and `tests/spotify/test_client.py`.

**Interfaces:** `SpotifyRequestControl` takes `minimum_interval_seconds`, `continuous`, injected `monotonic`, `wall_time`, `sleep`, and sanitized notice callback. It provides `before_request() -> None` (wait only), `record_request_start() -> None` (record actual dispatch and increment request count), `on_response(status, headers, body, operation_name) -> SpotifyRateLimitInfo | None`, `wait_for_pause(info: SpotifyRateLimitInfo) -> None`, and `reset_fallback_after_progress() -> None`. A no-delay quota pause is rejected as non-resumable by the controller before calling the waiter. Keep existing transport/request dataclasses and default retry policy interfaces. The client records dispatch after obtaining the fresh token, immediately before transport.

- [ ] Add fake-clock tests proving request starts are spaced, cached decisions cause no requests, pagination and mutation calls are paced, and no actual sleep/network is used. Include token refresh consuming two seconds with a one-second interval; the next actual Web API dispatch must still start at least one second after the previous actual dispatch.
- [ ] Add response-sequence tests: 429 with 4 seconds followed by 200; more than three valid cooldowns; delay 7,200 seconds; checkpoint work already consumed two seconds; exact nested quota reason; unrelated/malformed JSON; missing/zero/negative/NaN/infinite header values.
- [ ] Verify typed continuous pauses occur before in-client retry; ordinary default behavior still matches the existing retry tests.
- [ ] Add adaptive interval/fallback reset tests and assert a new pass reuses the existing pacing state. Successful inventory calls before another throttled search must not reset fallback escalation; a pass with retained matching progress must reset it.
- [ ] Implement the timing and parsing rules above without logging raw payloads. A quota response without a usable retry delay remains a typed non-resumable outcome.

Example deterministic invariant, expressed using the test's local clock fixture:

```python
clock = FakeClock()
control = SpotifyRequestControl(
    minimum_interval_seconds=1.0,
    continuous=True,
    monotonic=clock.monotonic,
    wall_time=clock.wall_time,
    sleep=clock.sleep,
)
control.before_request()
control.record_request_start()
first_start = clock.monotonic()
control.before_request()
control.record_request_start()
self.assertGreaterEqual(clock.monotonic() - first_start, 1.0)
```

Focused command: `rtk proxy python3 -m unittest tests.spotify.test_request_control tests.spotify.test_client`.

### Task 2: Refresh tokens before requests and after 401

**Files:** Modify `session.py`, `client.py`, `tests/spotify/test_session.py`, and `tests/spotify/test_client.py`. Reuse `auth.py`/`token_cache.py`; change them only if a concrete contract test requires it.

**Interfaces:** Optional `AccessTokenProvider` protocol in `client.py`: `get_access_token(force_refresh: bool = False) -> str`. `SpotifySession` implements that method and owns `settings`, current `SpotifyToken`, required scopes, injected wall clock, and token transport. `SpotifyAuthenticationError` is a typed `SpotifyApiError` subtype. Keep existing endpoint method signatures and `get_spotify_access_token` compatibility.

- [ ] Test a token valid now but expired after a simulated long wait; the next request must carry the refreshed token, and a fresh token must not trigger refresh on every call.
- [ ] Test forced refresh on first 401 and a stop on the second. Cover a rejected POST as well as GET; count/pace the retry and send the body only again after explicit 401, never after timeout/5xx.
- [ ] Test refresh-token rotation, omitted refresh token/scopes, required-scope loss, invalid grant, token-save failure, and a sanitized error that contains no secret sentinels.
- [ ] Test that no browser authorization is attempted mid-run and that the explicit-token/no-provider client remains unchanged.
- [ ] Implement the session and rebuild Authorization immediately before transport, after waits. Wrap refresh failures in the typed authentication error so track matching cannot swallow them.

Focused command: `rtk proxy python3 -m unittest tests.spotify.test_session tests.spotify.test_auth tests.spotify.test_token_cache tests.spotify.test_client`.

### Task 3: Safe pause, interruption, and typed pass completion

**Files:** Modify `publish_playlist.py`, `publish_types.py`, `publish_reports.py`, and their existing Spotify tests. Keep `matching.py`, `release_matching.py`, `match_cache.py`, and `publish_state.py` behavior intact except for directly necessary checkpoint call sites in the publisher.

**Interfaces:** Add defaulted final `stop_reason: Literal["complete", "search_budget"] = "complete"` and `resume_progress_made: bool = False` fields on `SpotifyPublishSummary`; carry them through `write_publish_summary` and `build_publish_summary`. Add `SpotifyAlbumAttemptKey` and the optional retained attempt set as specified above. Add explicit pause/auth exception passthrough and default-false `fail_on_api_error` in `search_spotify_track`. The publisher must save/report and rethrow typed pauses; the controller alone waits.

- [ ] Cover both search-budget return sites and the exact-last-row boundary. Started ladders may exceed the cap; a fully processed pass is complete.
- [ ] Cover budget 1 with an eligible three-track release whose album lookup returns no candidates: repeated passes must finish the track fallbacks, and the same successful album attempt must occur only once per unchanged key. Include an already-cached first row, changed fallback candidates/source inputs, and an interrupted album attempt that must be retried.
- [ ] Inject 429 before inventory completes, in track search, during album pages, on playlist creation, and on append batch two. Assert report/state preservation and that pending writes do not run after the pause.
- [ ] Inject expired/revoked authentication on many source rows; assert one typed failure, not one failed search per remaining row.
- [ ] Inject Ctrl-C during search, after a completed batch, and during checkpoint persistence; preserve the original exception, best available state, and an honest report if persistence also fails.
- [ ] Add crash-bound tests for release completion and 25-row checkpoints without real kills. Confirm atomic write failures do not cause an automatic continuation.
- [ ] Preserve a successful earlier batch when a later batch fails or its outcome is uncertain; avoid overwriting a nested detailed report with an inaccurate outer summary.

Focused command: `rtk proxy python3 -m unittest tests.spotify.test_publish_playlist tests.spotify.test_publish_reports tests.spotify.test_publish_state`.

### Task 4: Continuous controller and CLI wiring

**Files:** Modify `publish_playlist.py`, `tests/spotify/test_publish_playlist.py`, and `tests/spotify/test_publish_structure.py` only if its supported ownership checks need updating. Exercise existing wrapper tests without widening their public CLI.

**Interfaces:** Extend the two existing argv/namespace helpers with `continuous: bool = False` and `request_interval_seconds: float | None = None`. Normalize values before any authentication/network operation. `run_spotify_publish_from_args` owns the same live session/control objects across passes; injected clients remain supported for deterministic tests.

- [ ] Test ordinary defaults and builder parity; invalid combinations fail before settings/token lookup or network use.
- [ ] Use synthetic CSVs and fake Spotify state to run at least three capped passes to completion. Verify each automatic match is reused, all expected URIs are present in source order, and the process returns only at a full pass.
- [ ] Pause on a positional append, change fake remote contents during the wait, then verify fresh planning respects the changed contents rather than resending the old insertion position.
- [ ] Test a two-hour pause, token expiry during that pause, continuing past three 429s, and ordinary/unknown 429 fallback. Assert no API calls before the deadline.
- [ ] Test explicit quota with no usable delay exits 1 once, missing input/account identity changes stop, and non-cacheable search errors or budget returns without retained progress do not produce an infinite loop. Initialize/pin the user ID before playlist inventory and compare it before resumed playlist reads/writes.
- [ ] Test continuous dry-run never calls playlist creation/add/replace; continuous replace and continuous refresh-cache are rejected.
- [ ] Test exit 0 for a full processed pass with disclosed ambiguous/unmatched counts, exit 1 for API/search errors, and exit 130 on interruption. Ordinary partial-budget exit behavior remains compatible.
- [ ] Implement the controller using typed fields and existing caches; preserve timing state between passes and authorize only once.

Focused command: `rtk proxy python3 -m unittest tests.spotify.test_publish_playlist tests.spotify.test_publish_structure tests.test_discogs_make_playlists tests.test_discogs_release_playlist tests.test_configured_release_playlists`.

### Task 5: Reports, documentation, and coherent verification

**Files:** Modify `README.md`, the project-purpose section of `AGENTS.md` only to describe the new supported publisher behavior, `publish_reports.py`/`publish_types.py` for the named presentation fields, and `tests/spotify/test_publish_reports.py`.

- [ ] Assert waiting/needs-action/interrupted reports, exact known resume time, pass versus invocation counters, no inflated unique-track totals, and no raw credential/query/source-row leakage in operational notices.
- [ ] Update practical examples for `--continuous`, optional request spacing, cache preservation, dry-run, quota reset uncertainty, token reauthorization, terminal/process lifetime, and the append-only scope. Explain that processing completion can leave unmatched music for manual review.
- [ ] State that a killed process requires a manual restart after any reported cooldown; a fixed-delay shell loop is not the supported scheduler.
- [ ] Apply the Humanizer plugin to prose while preserving identifiers, policy values, examples, and technical qualifications.
- [ ] Make one focused requirement/DRY pass; remove new options or abstractions that do not implement a named requirement.
- [ ] Run the full relevant suite once after the coherent batch: `rtk proxy python3 -m unittest discover -s tests`.
- [ ] Run `rtk npm run typecheck`. If locked tooling is absent, install using `rtk npm ci --ignore-scripts` with the normal environment permissions; do not change dependency versions.
- [ ] Run `rtk git diff --check`; inspect the complete diff and verify no private files/cache data were staged or changed as fixtures.
- [ ] Request one bounded independent implementation review. Repair material findings and rerun only affected checks until the reviewed implementation is ready. No automatic commit, push, PR, or live Spotify trial.

## Release criteria and limits

The implementation is ready for local use when all four requested improvements have observable tests, required suites/typecheck pass, ordinary defaults and protected paths remain compatible, and independent review has no unresolved material finding.

A user-authorized live trial is a separate activity: select one known append playlist, capture request/429/wait totals, and verify its expected ordered contents after completion. Do not claim a throughput improvement or absence of throttling from mocks. A personal development-mode account may still exhaust its quota; continuous operation can wait only when enough information exists to do so safely.

Omitting `--continuous` restores bounded operation; omit the interval option to retain existing pacing defaults outside continuous mode. Existing match/token/publish-state schemas remain readable. This is a local behavior rollback, not a reversal of Spotify writes already completed.

## Independent plan review record

Reuse one independent reviewer for changed drafts. Do not repeat reviews of an unchanged draft or treat a lack of findings as proof of live correctness. For subsequent review, the normative-content fingerprint is SHA256 of the UTF-8 bytes before this section heading; review-record additions therefore do not invalidate the reviewed design.

### Draft 1

Full-file SHA256: `017ae9498d338e9046498163e571686dffbc947161829390385998f96c04d296`.

- R1 (high), independently reproduced against current source: with budget 1, an unsuccessful album lookup can repeat forever without a row decision. Draft 2 retains successful album-attempt keys across passes and requires actual retained progress at budget boundaries; it adds the exact regression and invalidation/failure cases.
- R2 (medium): a token refresh after pacing can make the stored request-start timestamp stale. Draft 2 separates waiting from actual dispatch recording and adds a slow-refresh spacing regression.
- Controller self-review also clarified typed interfaces, propagation of continuous-mode API failures, fallback escalation reset, and overflow-safe cooldown notices.

### Draft 2

Reviewed normative SHA256: `466d3e41eab58fa7a89b42c945dd0c8895f5a6e9d7721bd993cd1f68eab4d4b8`.

Independent verdict: **No material improvements recommended.** The reviewer confirmed R1 and R2 are resolved and found no additional material defects in continuation, authentication, interruption, reporting, or compatibility. The review did not validate an implementation or live Spotify behavior.

After that review, the review-status sentence and this record were updated. On 2026-09-14 the user confirmed the reviewed opt-in behavior and waiting through valid Spotify cooldowns, including hours. The decision section and option table now record that confirmation. The technical design and task requirements are unchanged, so an additional review of unchanged requirements is unnecessary. No material findings or user decisions remain unresolved. The plan is ready for a separately authorized implementation.
