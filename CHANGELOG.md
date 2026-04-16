# Changelog

## 0.9.2 — 2026-04-16

### Fixed

- **Drop the RMS gate on hallucination suppression.** The first two
  drops were conditional on the utterance's RMS falling below a low-
  energy ceiling, on the theory that known hallucination phrases only
  appear during near-silence. Real-world testing showed ambient mic
  noise (HVAC, fans, keyboards) produces RMS well above any useful
  ceiling, so the gate leaked hallucinations through. Filtering is now
  unconditional whenever the phrase matches.
- **Fix 3 pre-existing test failures** surfaced during PR #9 review.
  Updated `test_install` regex to match current error messages; updated
  `test_daemon` to patch the functions `main()` actually calls after
  the GPU backend refactor.

## 0.9.1 — 2026-04-16

### Fixed

- **Review-round fixes for the hallucination filter.** Addressed
  feedback from CodeRabbit, Codex, and a manual code-review pass:
  tighter phrase matching, improved normalization of punctuation and
  whitespace before comparison, and clearer logging when a transcript
  is suppressed so users understand why their dictation produced no
  output.

## 0.9.0 — 2026-04-16

### Added

- **Suppress Whisper hallucination phrases.** Whisper models hallucinate
  short phrases like "Thank you", "you", "Bye", and "Okay" when the
  microphone captures ambient noise but no speech. A post-transcription
  filter now suppresses known hallucination phrases when they are the
  entire transcript output (PR #9).

## 0.8.2 — 2026-04-12

### Fixed

- **Chromium/Wayland text insertion regression.** Dictated text was not
  inserted into Chrome text fields; the preedit status animation
  ("Transcribing...") was left in the field instead.
  - Swap commit/clear ordering so `commit_text` arrives before the preedit
    clear. On the Wayland text-input-v3 path each IBus call becomes a
    separate protocol batch; clearing first caused Chrome to finalize the
    animation text.
  - Stop discarding deferred text when the daemon transitions to idle.
    Chrome on Wayland sends spurious focus-out events during transcription;
    the final transcript was deferred correctly but then thrown away before
    focus returned.
  - Always commit deferred text on focus return regardless of daemon state.
  - Remove redundant `hide_preedit_text()` call from the render adapter;
    `update_preedit_text_with_mode(visible=False)` is sufficient and avoids
    an extra signal that Chrome could misinterpret.
