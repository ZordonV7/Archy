import { useEventStore, type MoodLabel, type EnergyLabel } from "../../stores/eventStore";
import { useMemo, useState, useEffect } from "react";

export type MascotState =
  | "idle"
  | "thinking"
  | "talking"
  | "happy"
  | "sleeping"
  | "worried"
  | "celebrating"
  | "focused";

export function deriveMascotState(
  mood: MoodLabel | null,
  energy: EnergyLabel | null,
  isTalking: boolean
): MascotState {
  if (isTalking) return "talking";
  if (mood === "critical_panic") return "worried";
  if (mood === "deep_focus" && energy && energy !== "depleted") return "focused";
  if (mood === "watchful") return "idle";
  if (energy === "depleted") return "sleeping";
  if (mood === "drift_alert") return "worried";
  return "idle";
}

// Color theme per mood
export const MOOD_COLORS: Record<MoodLabel, { primary: string; accent: string; bg: string }> = {
  deep_focus: { primary: "#6366f1", accent: "#818cf8", bg: "#1e1b4b" },
  watchful: { primary: "#10b981", accent: "#34d399", bg: "#064e3b" },
  drift_alert: { primary: "#f59e0b", accent: "#fbbf24", bg: "#451a03" },
  critical_panic: { primary: "#ef4444", accent: "#f87171", bg: "#450a0a" },
};

export const MASCOT_STATES: Record<MascotState, { emoji: string; label: string; animation: string }> = {
  idle: { emoji: "🐻", label: "Idle", animation: "idle-bob" },
  thinking: { emoji: "🐻", label: "Thinking", animation: "thinking-pulse" },
  talking: { emoji: "🐻", label: "Talking", animation: "talking-bounce" },
  happy: { emoji: "🐻", label: "Happy", animation: "happy-wiggle" },
  sleeping: { emoji: "😴", label: "Resting", animation: "sleep-breath" },
  worried: { emoji: "😟", label: "Concerned", animation: "worried-shake" },
  celebrating: { emoji: "🎉", label: "Celebrating", animation: "celebrate-jump" },
  focused: { emoji: "🎯", label: "Focused", animation: "focused-glow" },
};

export function useMascotState() {
  // Track BOTH label AND score so the component re-renders when score changes
  const moodLabel = useEventStore((s) => s.mood?.label ?? null);
  const moodScore = useEventStore((s) => s.mood?.score ?? 75);
  const energyLabel = useEventStore((s) => s.energy?.label ?? null);
  const energyScore = useEventStore((s) => s.energy?.score ?? 80);
  const assistantMessage = useEventStore((s) => s.assistantMessage);

  // Track talking state with a timer so it auto-resets after 10s
  const [isTalking, setIsTalking] = useState(false);

  useEffect(() => {
    if (assistantMessage?.text) {
      setIsTalking(true);
      const t = setTimeout(() => setIsTalking(false), 10000);
      return () => clearTimeout(t);
    }
  }, [assistantMessage]);

  return useMemo(() => {
    const state = deriveMascotState(moodLabel, energyLabel, isTalking);
    const colors = moodLabel ? MOOD_COLORS[moodLabel] : MOOD_COLORS.watchful;
    return {
      state,
      colors,
      mood: moodLabel,
      energy: energyLabel,
      moodScore,
      energyScore,
      isTalking,
    };
  }, [moodLabel, moodScore, energyLabel, energyScore, isTalking]);
}
