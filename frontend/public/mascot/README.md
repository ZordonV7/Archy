# Archy Mascot Assets

Drop your `archy.riv` file in THIS directory to enable the Rive-powered mascot.

## When this file is used

`frontend/src/components/Mascot/RiveMascot.tsx` looks for `/mascot/archy.riv`
served from the public root. If the file is present, it loads the Rive runtime
and renders the state machine. If the file is missing, Rive is never
initialised and the canvas-based `RetroTVMascot.tsx` is used instead — so the
app works out of the box even without a `.riv` file.

## Required state machine contract

The Rive file MUST expose:

- A state machine named **`MoodMachine`**
- A **text** input on that state machine named **`mood`**

The frontend writes one of these 7 strings to the `mood` input:

| Value        | When it's shown                                              |
|--------------|---------------------------------------------------------------|
| `happy`      | Default / Archy just sent a message (encouraging)            |
| `sad`        | Mood is `drift_alert` (things slipping) or energy is `low`   |
| `panic`      | Mood is `critical_panic`, or WebSocket disconnected          |
| `upset`      | Mood is `drift_alert` AND energy is low, or risk is `high`   |
| `thinking`   | (reserved for future use; classifier running)                |
| `sleeping`   | Energy is `depleted`                                          |
| `waking`     | Initial boot / loading                                        |

These match the 7 moods in `RetroTVMascot.tsx` exactly, so designers and the
fallback renderer stay in sync.

## Producing a .riv file

1. Open <https://rive.app>
2. Create a new file with a state machine named `MoodMachine`
3. Add a text input named `mood` to that state machine
4. Wire 7 states to the matching string values (use Listener nodes)
5. File → Export → save as `archy.riv`
6. Drop the file in this directory and reload — no code changes needed.
