# IBus-only architecture

`kdictate` is structured around a strict split:

- the core daemon owns audio capture, VAD, transcription, runtime state, and a session D-Bus API
- the IBus engine is the only component allowed to place text into applications

The daemon publishes:

- `StateChanged(state)`
- `PartialTranscript(text)`
- `FinalTranscript(text)`
- `ErrorOccurred(code, message)`

The IBus engine consumes those events and maps them to:

- partial transcript -> IBus preedit
- final transcript -> IBus commit

No synthetic typing, clipboard paste fallback, or mixed insertion backend is allowed in the redesign.
