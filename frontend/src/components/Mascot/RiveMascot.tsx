import { Component, type ErrorInfo, type ReactNode, useEffect, useState } from "react";
import { useRive, useStateMachineInput } from "@rive-app/react-canvas";
import RetroTVMascot, { type RetroMood } from "./RetroTVMascot";

/**
 * RiveMascot — Archy's animated mascot, powered by Rive (`.riv`).
 *
 * Why this exists:
 *   The frontend's package.json description has always claimed "Tauri + React
 *   + Rive", but Rive was never installed and no .riv file was ever wired up.
 *   The mascot was actually rendered by `RetroTVMascot` — a hand-rolled
 *   canvas-based animation. That worked, but the marketing/code mismatch was
 *   misleading. This component finally makes Rive first-class:
 *
 *   - If a `.riv` asset is available (drop one at `public/mascot/archy.riv`),
 *     it is loaded via `@rive-app/react-canvas` and the mood state machine
 *     input is driven from the `mood` prop.
 *   - If the asset is missing, OR Rive fails to initialise (older browser,
 *     WebGL disabled, asset 404, etc.), we transparently fall back to the
 *     canvas-based RetroTVMascot. The user never sees a broken mascot.
 *
 * State machine contract (assumed in the .riv file):
 *   State machine name: "MoodMachine"
 *   Text input name: "mood"
 *   Valid values: "happy" | "sad" | "panic" | "upset" | "thinking"
 *                 | "sleeping" | "waking"
 *   These match the 7 moods in RetroTVMascot.ts exactly, so the same prop
 *   type is reused.
 *
 * To produce a .riv file:
 *   1. Open rive.app
 *   2. Create a state machine named "MoodMachine" with a text input "mood"
 *   3. Wire 7 mood states to the matching string values
 *   4. Export as `archy.riv` and drop into `public/mascot/`
 */

const RIVE_SRC = "/mascot/archy.riv";
const STATE_MACHINE = "MoodMachine";
const MOOD_INPUT = "mood";

export type { RetroMood } from "./RetroTVMascot";

export default function RiveMascot({ mood }: { mood: RetroMood }) {
  // Three modes:
  //   "loading"  — still probing for the .riv asset
  //   "rive"     — asset exists, render Rive
  //   "fallback" — asset missing OR Rive blew up at runtime
  const [mode, setMode] = useState<"loading" | "rive" | "fallback">("loading");

  // Probe for the .riv asset before trying to mount the Rive hook. A simple
  // HEAD request tells us whether the file exists, so we don't pay the cost
  // of initialising Rive (WebGL context, wasm runtime, etc.) for users who
  // haven't added one yet.
  useEffect(() => {
    let cancelled = false;
    fetch(RIVE_SRC, { method: "HEAD" })
      .then((r) => {
        if (cancelled) return;
        setMode(r.ok ? "rive" : "fallback");
      })
      .catch(() => {
        if (!cancelled) setMode("fallback");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (mode === "loading") {
    // Transparent placeholder sized like the mascot so layout doesn't shift
    // when we swap in Rive or the fallback.
    return <div style={{ width: 232, height: 174 }} aria-busy="true" />;
  }

  if (mode === "fallback") {
    return <RetroTVMascot mood={mood} />;
  }

  return (
    <RiveErrorBoundary fallback={<RetroTVMascot mood={mood} />}>
      <RiveRenderer src={RIVE_SRC} mood={mood} />
    </RiveErrorBoundary>
  );
}

/**
 * Inner renderer that actually calls useRive. Kept separate so the error
 * boundary above can catch any runtime error from the Rive wasm/gl layer.
 *
 * Hooks used:
 *   - useRive: loads the .riv file and exposes the React component to render
 *   - useStateMachineInput: returns a live reference to the named input so
 *     we can write to it when `mood` changes
 */
function RiveRenderer({ src, mood }: { src: string; mood: RetroMood }) {
  const { rive, RiveComponent } = useRive({
    src,
    stateMachines: STATE_MACHINE,
    autoplay: true,
  });

  const moodInput = useStateMachineInput(rive, STATE_MACHINE, MOOD_INPUT);

  // Drive the mood input whenever the prop changes. If the .riv file doesn't
  // actually have an input named "mood" (e.g. artist named it differently),
  // `moodInput` is null and this is a no-op rather than a crash — the
  // .riv file's default state will still play, just not mood-driven.
  useEffect(() => {
    if (moodInput) {
      try {
        // Text inputs accept any string; the .riv file's state machine is
        // responsible for routing the value to the right animation branch.
        (moodInput as any).value = mood;
      } catch {
        // Some Rive builds expose the input as a number-only input — ignore
        // write failures so we don't crash the whole mascot.
      }
    }
  }, [mood, moodInput]);

  if (!RiveComponent) {
    // useRive returns null RiveComponent while the file is still loading.
    // Show the canvas fallback in the meantime so there's no blank gap.
    return <RetroTVMascot mood={mood} />;
  }

  return (
    <div style={{ width: 232, height: 174 }}>
      <RiveComponent />
    </div>
  );
}

/**
 * Error boundary around the Rive renderer. If the wasm/gl layer throws
 * (older browser, WebGL disabled, malformed .riv), we fall back to the
 * canvas mascot so Archy is never broken.
 */
class RiveErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode },
  { hasError: boolean }
> {
  constructor(props: { children: ReactNode; fallback: ReactNode }) {
    super(props);
    this.state = { hasError: false };
  }
  static getDerivedStateFromError() {
    return { hasError: true };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.warn("[RiveMascot] Rive runtime failed, falling back to canvas mascot:", error, info);
  }
  render() {
    if (this.state.hasError) return this.props.fallback;
    return this.props.children;
  }
}
