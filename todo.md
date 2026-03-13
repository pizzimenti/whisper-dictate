# ptt-realtime branch TODO

## Done but untested
- [ ] kglobal_hotkey.py: removed `type_text` import, `NO_TRANSCRIPT_SENTINEL` still to remove, `_stop_dictation` now fires `stop --no-wait` and doesn't type (uncommitted diff on branch)
- [ ] Restart whisper-dictate-hotkey.service after committing kglobal_hotkey.py changes

## Not yet started

### Critical: Separation of Concerns / DRY
- [ ] Extract shared `VADSegmenter` class from duplicated logic in `dictate.py:242-326` and `mic_realtime.py:526-576`
- [ ] Extract shared `transcribe_pcm()` into a common module (duplicated in `dictate.py:170-188` and `mic_realtime.py:166-199`)
- [ ] Extract shared model-loading helper (duplicated across `dictate.py`, `transcribe.py`, `benchmark.py`, `eval/sweep.py`)
- [ ] Remove `NO_TRANSCRIPT_SENTINEL` constant from `kglobal_hotkey.py` (dead code)

### Critical: Safety
- [ ] Fix race condition in `DictationDaemon.start_recording` — lock released before thread startup; `stop_and_transcribe` can interleave (dictate.py:351-398)
- [ ] Add `timeout` to all `thread.join()` calls in `mic_realtime.py:580-585`
- [ ] Document lock semantics in `DictationDaemon.__init__` — which fields `_lock` protects

### Medium: Bug Fix
- [ ] `_transcribe_pcm` hardcodes `condition_on_previous_text=False` and `vad_filter=False` instead of passing through CLI args (`dictate.py:182-185`)

### Medium: Dead Code
- [ ] `eval/evaluate.py:73` — unreachable return referencing undefined `samples`
- [ ] `eval/evaluate.py` — unused imports: `soundfile`, `numpy`, `hf_hub_download`

### Medium: Documentation
- [ ] Add inline comments explaining VAD state machine in shared module
- [ ] Add inline comments on threading model and queue drop behavior in `dictate.py`
- [ ] Update `README.md`: document new streaming args (`--block-ms`, `--energy-threshold`, `--silence-ms`, `--min-speech-ms`, `--start-speech-ms`, `--max-utterance-s`)
- [ ] Update `README.md`: add install/uninstall instructions, service file location (`~/.config/systemd/user/`)
- [ ] Update `README.md`: note that daemon now streams transcription in real-time during PTT
