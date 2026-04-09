# whisper-dictate: IBus-only redesign plan for branch `ibus-input`

> Historical document: the design plan for the IBus-only redesign that shipped
> in PR #2 (when the project was still named `whisper-dictate`, before the
> rename to KDictate). Retained for context; not a current reference.

This document is for an agentic coding LLM team implementing a clean redesign of `whisper-dictate` so that **IBus / input methods are the only text placement engine**.

## Hard requirements

- Create and work on a new git branch named **`ibus-input`**.
- Treat **IBus as the sole output/insertion path**.
- Do **not** keep or add any fallback text insertion via:
  - `ydotool`
  - `dotool`
  - `wtype`
  - `wl-copy` + paste
  - `xdotool`
  - KWin scripting-based injection
  - portal RemoteDesktop / EIS-based synthetic typing
- The dictation core may still use the existing microphone / VAD / Whisper pipeline, but it must **not** type, paste, or synthesize key events.
- The output model must be:
  - **partial transcript -> IBus preedit**
  - **final transcript -> IBus commit**
- Every workstream must include:
  - robust error handling
  - inline documentation / docstrings
  - structured logging
  - good tests

---

# 1. Goal

Convert the current project into a two-part architecture:

1. **Core dictation daemon**
   - owns audio capture, VAD, segmentation, transcription, runtime state
   - exports transcript/state events over a local session IPC interface
   - never inserts text directly

2. **IBus engine frontend**
   - the **only** component allowed to place text into applications
   - receives transcript events from the daemon
   - uses IBus APIs for:
     - preedit updates for partial text
     - commit for finalized text
     - focus-aware behavior
     - surrounding-text-aware future improvements

This should produce a clean, future-facing Wayland-native design centered on input methods rather than fake keyboard injection.

---

# 2. Existing repo assumptions

The current repo already contains useful pieces that should be preserved where possible:

- `dictate.py`
  - long-lived warm daemon
  - microphone / VAD / decode lifecycle
  - GLib main loop
- `desktop_actions.py`
  - currently contains notification helpers and `ydotool`-based typing
- `dictate_runtime.py`
  - runtime paths and state persistence helpers
- `dictatectl.py`
  - current signal-driven CLI helper
- `kglobal_hotkey.py`
  - current KWin accessibility hotkey integration
- `install.sh`
  - currently installs and configures `ydotool`

The redesign should **reuse the daemon/transcription core** and **remove all direct typing responsibilities**.

---

# 3. Final desired architecture

## 3.1 High-level design

```text
+-------------------------+
| whisper-dictate daemon  |
|-------------------------|
| audio capture           |
| VAD segmentation        |
| Whisper decode          |
| runtime state           |
| session D-Bus service   |
+-----------+-------------+
            |
            | transcript/state signals
            v
+-------------------------+
| IBus engine frontend    |
|-------------------------|
| enable/disable          |
| focus in/out            |
| preedit update          |
| final commit            |
| engine state handling   |
+-------------------------+
            |
            v
      Focused text field
```

## 3.2 Important rule

The daemon emits **text events**, not keystrokes.

The IBus engine commits text, not the daemon.

## 3.3 Non-goals

- No synthetic key injection fallback
- No clipboard-based fallback
- No mixed “IBus + ydotool” design
- No hidden legacy path that still types directly

---

# 4. Target branch workflow

## 4.1 Branch

Create:

```bash
git checkout -b ibus-input
```

## 4.2 Suggested sub-branches for parallel agent work

Each sub-agent should work on a dedicated topic branch branched from `ibus-input`.

Recommended names:

- `ibus-input-agent1-core-service`
- `ibus-input-agent2-ibus-engine`
- `ibus-input-agent3-packaging-integration`
- `ibus-input-agent4-tests-observability`

Merge order should be:

1. shared contracts / scaffolding
2. daemon service + IBus engine
3. packaging / install / docs
4. final observability + integration test pass

---

# 5. Shared contract so agents can work in parallel

The biggest risk in parallel work is interface drift. Freeze the following contract first.

## 5.1 IPC mechanism

Use **session D-Bus** via GLib/Gio / PyGObject.

## 5.2 Bus identity

Use a stable reverse-DNS style identity.

Recommended:

- Bus name: `io.github.pizzimenti.WhisperDictate1`
- Object path: `/io/github/pizzimenti/WhisperDictate1`
- Interface: `io.github.pizzimenti.WhisperDictate1`

## 5.3 Required D-Bus methods

- `Start()`
- `Stop()`
- `Toggle()`
- `GetState() -> s`
- `GetLastText() -> s`
- `Ping() -> s`

## 5.4 Required D-Bus signals

- `StateChanged(s state)`
- `PartialTranscript(s text)`
- `FinalTranscript(s text)`
- `ErrorOccurred(s code, s message)`

## 5.5 Canonical state names

- `idle`
- `recording`
- `transcribing`
- `error`

## 5.6 Logging format

Use Python `logging`, not ad hoc `print` except in CLI entry points.

Every long-lived process should log:

- startup/shutdown
- configuration summary
- state transitions
- D-Bus connection events
- IBus enable/disable/focus events
- partial/final transcript publication
- exception paths with context

Preferred logger names:

- `whisper_dictate.core`
- `whisper_dictate.dbus`
- `whisper_dictate.ibus`
- `whisper_dictate.install`
- `whisper_dictate.tests`

## 5.7 Error model

Every boundary must fail cleanly.

Use explicit exception types where appropriate, for example:

- `ConfigurationError`
- `DbusServiceError`
- `IbusEngineError`
- `AudioInputError`
- `TranscriptionError`
- `FocusContextError`

Never swallow exceptions silently. Log them with context and either recover explicitly or surface a controlled error signal.

---

# 6. Proposed repo shape after redesign

A suggested target layout:

```text
whisper_dictate/
  __init__.py
  config.py
  logging_utils.py
  exceptions.py

  core/
    __init__.py
    daemon.py
    audio.py
    vad.py
    decode.py
    runtime.py
    notifications.py

  service/
    __init__.py
    dbus_api.py
    dbus_service.py

  ibus_engine/
    __init__.py
    engine.py
    component.py
    main.py

  cli/
    __init__.py
    dictatectl.py

  tests/
    unit/
    integration/
    fixtures/

packaging/
  io.github.pizzimenti.WhisperDictate.xml
  io.github.pizzimenti.WhisperDictate.service
  io.github.pizzimenti.WhisperDictate.desktop

systemd/
  whisper-dictate-core.service

scripts/
  install.sh
```

This exact layout may be adjusted, but the separation of concerns should remain.

---

# 7. Four-agent implementation plan

## Agent 1: Core daemon + D-Bus service

### Mission

Refactor the existing daemon so it owns transcription only, then expose a robust session D-Bus service that publishes transcript/state events.

### Scope

- Refactor `dictate.py` into a reusable daemon module
- Remove direct output calls from the daemon
- Create the canonical D-Bus API and implementation
- Preserve current state/runtime behavior where sensible
- Replace `print`-driven internal flow with structured logging

### Required deliverables

1. **Daemon refactor**
   - move warm model / record / stop / transcribe lifecycle into `whisper_dictate.core.daemon`
   - remove any direct call that types or inserts text
   - preserve notifications if useful, but decouple them from text placement

2. **D-Bus service**
   - implement the method/signal contract from section 5
   - own the session bus name
   - publish `PartialTranscript` and `FinalTranscript`
   - emit `StateChanged`
   - emit `ErrorOccurred` on recoverable failures

3. **CLI compatibility**
   - rework `dictatectl.py` so it calls D-Bus methods instead of sending UNIX signals
   - preserve a similar user-facing CLI where practical

4. **Runtime hygiene**
   - keep runtime state files if they are useful for debugging / compatibility
   - they must become observers/cache, not the primary control plane

### Engineering requirements

- Add docstrings to all public classes/functions
- Add typed function signatures
- Centralize config loading and validation
- Use dedicated exception classes
- Ensure all threads and stream resources shut down cleanly

### Tests required

- unit tests for state transitions
- unit tests for D-Bus API methods with mocks/fakes
- unit tests for error propagation
- integration test that starts the service and verifies signal emission

### Explicit acceptance criteria

- starting/stopping dictation works without any typing code present
- partial/final transcript signals are observable over session D-Bus
- CLI can call `Start`, `Stop`, `Toggle`, `GetState`, `GetLastText`
- no internal component relies on `ydotool`

---

## Agent 2: IBus engine frontend

### Mission

Build the IBus engine that becomes the **sole text placement mechanism**.

### Scope

- Python IBus engine using PyGObject
- consume transcript/state signals from Agent 1 service
- own preedit + commit behavior
- handle enable/disable/focus lifecycle robustly

### Required deliverables

1. **IBus engine implementation**
   - create a subclass of `IBus.Engine`
   - implement lifecycle hooks such as:
     - `enable`
     - `disable`
     - `focus_in`
     - `focus_out`
     - `reset`
   - subscribe to the daemon D-Bus service

2. **Text placement behavior**
   - on `PartialTranscript(text)`: update preedit text
   - on `FinalTranscript(text)`: clear preedit then commit text
   - on `StateChanged(idle)`: ensure stale preedit is cleared

3. **Focus awareness**
   - track whether the engine has an active input context
   - do not commit text if no valid focus context exists
   - log focus changes and ignored commits clearly

4. **Optional surrounding-text groundwork**
   - request/track surrounding text where appropriate
   - do not overbuild editing logic yet, but structure code so future context-aware formatting is easy

### Engineering requirements

- No direct injection fallback of any kind
- Defensive handling when the daemon is unavailable
- Defensive handling when the engine loses focus mid-session
- Clear engine state model, separate from daemon state
- Rich inline documentation explaining why preedit/commit are used the way they are

### Tests required

- unit tests for state handling around enable/disable/focus
- unit tests for preedit update behavior
- unit tests for final commit behavior
- unit tests for service disconnect / reconnect behavior
- integration-style test harness with mocked daemon signals

### Explicit acceptance criteria

- all committed dictation text enters the focused field through IBus commit APIs only
- partial transcript is shown through preedit APIs only
- no code path exists that types or pastes text outside IBus
- focus-loss behavior is deterministic and tested

---

## Agent 3: Packaging, install, integration, cleanup

### Mission

Turn the redesign into a coherent installable project by removing legacy injector assumptions and adding IBus/system integration assets.

### Scope

- remove `ydotool` install/runtime assumptions
- create IBus component packaging assets
- create/update systemd user service(s)
- update install/uninstall flow
- document how to enable the engine in an IBus-based Plasma session

### Required deliverables

1. **Remove legacy injector dependency chain**
   - delete `ydotool` installation logic from `install.sh`
   - remove input-group membership logic
   - remove user-service assumptions tied to `ydotool`
   - remove docs/config paths that mention injection backends

2. **IBus integration assets**
   - add IBus component XML / metadata
   - add any required launcher / entrypoint files
   - ensure the engine can be discovered by IBus

3. **Systemd integration**
   - install a clean user service for the core daemon
   - ensure the service name and app identity are consistent
   - choose a stable reverse-DNS-style app naming convention

4. **Installer / bootstrap**
   - make install path straightforward for a development checkout
   - validate required dependencies early and emit good errors
   - support repeatable reinstall/update behavior

5. **Documentation**
   - update README and any user docs to explain:
     - project architecture
     - IBus-only placement model
     - how to enable/select the engine
     - how to operate the daemon and troubleshoot

### Engineering requirements

- clear shell logging in install scripts
- fail-fast dependency checks with actionable errors
- idempotent install steps where possible
- comments in scripts for non-obvious system integration steps

### Tests required

- shellcheck-clean scripts if shell is used
- tests for config/template rendering where practical
- smoke tests for installable file generation
- validation test that legacy `ydotool` references are removed from active paths

### Explicit acceptance criteria

- a fresh install does not install or configure any injector backend
- the daemon can start as a user service
- the IBus engine assets are installed/discoverable
- docs match the actual behavior of the redesigned app

---

## Agent 4: Test architecture, observability, CI-quality hardening

### Mission

Provide the cross-cutting quality layer: logging, test infrastructure, fixtures, failure-path coverage, and branch-level release confidence.

### Scope

- standardize logging utilities
- standardize exception handling patterns
- provide reusable test fixtures/mocks for D-Bus and IBus
- expand integration coverage
- police regressions such as accidental reintroduction of injector tools

### Required deliverables

1. **Logging utilities**
   - central `logging_utils.py`
   - formatter/handler setup helpers
   - debug vs info defaults appropriate for daemon and engine processes

2. **Shared exception definitions**
   - create central exception module
   - ensure public layers translate low-level exceptions into domain errors

3. **Reusable test fixtures**
   - fake/mocked D-Bus service and signal emitter
   - fake/mocked IBus engine context for preedit/commit assertions
   - fixtures for transcript event streams
   - fixtures for config and temporary runtime dirs

4. **Integration and regression tests**
   - end-to-end happy path:
     - daemon emits partial/final
     - engine receives partial/final
     - preedit/commit methods are invoked correctly
   - error path tests:
     - daemon unavailable
     - D-Bus reconnect needed
     - focus lost before final commit
     - malformed config
   - regression tests ensuring no forbidden injector backends remain active

5. **Quality checks**
   - expand lint/type/test commands in repo docs and scripts
   - make test runs deterministic and suitable for CI

### Engineering requirements

- write tests for both success and failure paths
- avoid fragile timing-dependent tests where possible
- use helper abstractions so other agents can plug into the same fixture set
- add logging assertions where useful

### Explicit acceptance criteria

- core happy path is covered by integration tests
- failure modes are logged and asserted
- accidental use/import of forbidden injector paths can be caught by tests
- developers can run a single documented test command locally

---

# 8. Parallel execution strategy

To keep four agents working effectively in parallel, follow this order.

## Step A: Contract freeze (very small initial PR or commit)

Before real implementation, create a tiny shared commit on `ibus-input` containing:

- `exceptions.py`
- `logging_utils.py`
- D-Bus interface contract docstring or XML/introspection spec
- state enum/constants
- a short `docs/architecture-ibus.md` summary

After that commit lands, all four agents branch from it.

## Step B: Parallel work

- Agent 1 works against the service contract
- Agent 2 works against mocked D-Bus transcript signals and mocked IBus contexts
- Agent 3 works against frozen names/paths and expected service/engine entrypoints
- Agent 4 builds the fixture layer early, then keeps rebasing and expanding coverage as Agents 1–3 land work

## Step C: Merge sequence

Recommended order:

1. Agent 4 fixture/logging scaffolding if it is low-risk
2. Agent 1 core daemon + D-Bus service
3. Agent 2 IBus engine
4. Agent 3 packaging/install/docs
5. Agent 4 final integration/regression hardening

---

# 9. Concrete task checklist

## 9.1 Remove/retire legacy files or behaviors

The redesign should remove or retire any active direct insertion logic.

Likely actions:

- remove or drastically shrink `desktop_actions.py`
  - keep notifications only
  - remove `type_text`
- remove active `ydotool` invocation code
- remove install-time `ydotool` setup
- remove documentation that frames the app as a fake typer

## 9.2 Preserve useful current pieces

Likely keep and adapt:

- Whisper model loading
- audio capture pipeline
- VAD segmentation
- GLib main loop integration
- runtime state persistence helpers if still useful
- optional notifications

## 9.3 Naming decisions

Choose one stable identity and use it everywhere:

- package/module name
- D-Bus service identity
- systemd service name
- IBus component id
- desktop metadata

Recommended root identity:

`io.github.pizzimenti.WhisperDictate`

---

# 10. Implementation details to follow

## 10.1 Code quality

- Python code should be type-annotated
- Public APIs need docstrings
- Non-obvious logic needs inline comments
- Avoid giant monolithic files
- Separate transport concerns from business logic

## 10.2 Logging expectations

At minimum, log these events:

### Core daemon
- daemon start
- model loaded
- microphone start/stop
- state transition
- partial transcript emitted
- final transcript emitted
- service method called
- recoverable failure
- fatal shutdown path

### IBus engine
- engine start
- service connect/disconnect
- enable/disable
- focus in/out
- preedit update
- final commit
- ignored commit due to missing focus
- reset/clear operations

### Installer / integration
- dependency detection
- file install/update
- service reload/restart
- ibus registration/discovery guidance

## 10.3 Error handling expectations

Do not leave the system in an ambiguous state.

Examples:

- if audio input cannot start:
  - log structured error
  - emit service error signal
  - return to `idle` or `error` deterministically
- if D-Bus bus acquisition fails:
  - fail startup with actionable log message
- if IBus engine is enabled but daemon is unreachable:
  - log warning/error
  - surface deterministic no-op behavior
- if final transcript arrives without focus:
  - do not commit blindly
  - log exactly what happened
  - clear stale preedit safely

---

# 11. Testing plan

## 11.1 Unit tests

Cover:

- daemon state machine
- D-Bus method behavior
- D-Bus signal publication
- engine focus lifecycle
- engine preedit/commit logic
- config validation
- exception translation

## 11.2 Integration tests

Must cover:

1. **Happy path**
   - start recording
   - receive partial transcript
   - engine updates preedit
   - receive final transcript
   - engine commits final text
   - state returns to idle

2. **Daemon unavailable**
   - engine starts without service
   - engine does not crash
   - logs meaningful error

3. **Focus lost mid-session**
   - partial shown
   - focus out occurs
   - final transcript does not commit blindly
   - state cleanup is deterministic

4. **Regression guard**
   - fail if code imports/calls forbidden injector tools in active codepaths

## 11.3 Useful regression test idea

Add a static or semi-static regression check that active Python modules do not call or shell out to:

- `ydotool`
- `dotool`
- `wtype`
- `wl-copy`
- `xdotool`

This is specifically to protect the IBus-only design.

---

# 12. Suggested milestones

## Milestone 1: core separation

- daemon no longer types
- D-Bus contract exists
- service starts and publishes state

## Milestone 2: basic IBus commit path

- IBus engine can receive `FinalTranscript`
- final text is committed through IBus only

## Milestone 3: streaming UX

- partial transcript shows as preedit
- final commit clears preedit correctly

## Milestone 4: installable package

- user service works
- IBus component is discoverable
- docs and scripts are updated

## Milestone 5: hardening

- integration tests pass
- logging is coherent
- failure modes are covered

---

# 13. Final acceptance checklist

The branch is ready only if all of the following are true:

- [ ] Branch name is `ibus-input`
- [ ] Direct text insertion code has been removed from the daemon
- [ ] IBus is the only active text placement engine
- [ ] D-Bus service methods and signals are implemented
- [ ] IBus engine handles partial preedit and final commit
- [ ] Focus lifecycle is handled deterministically
- [ ] No `ydotool` / `dotool` / `wtype` / clipboard injection fallback remains
- [ ] Installer and docs reflect the IBus-only architecture
- [ ] Structured logging exists across daemon, service, engine, and installer
- [ ] Tests cover happy path and failure paths
- [ ] Inline docs and comments explain the moving pieces clearly

---

# 14. Short instructions for the supervising coding agent

Use four sub-agents in parallel, but keep interfaces frozen.

1. Land the shared contract/scaffolding commit first.
2. Dispatch Agent 1–4 using the scopes above.
3. Require each agent to:
   - add robust error handling
   - add inline documentation
   - add tests for both success and failure paths
   - add structured logging
4. Merge only after rebasing onto the latest `ibus-input` tip.
5. Run the full test suite after every merge.
6. Reject any implementation that sneaks in non-IBus text insertion.

