import { useMemo, useState, useEffect } from "react";
import { useEventStore, type MoodLabel, type EnergyLabel } from "../../stores/eventStore";
import type { RetroMood } from "./RetroTVMascot";

/**
 * Derives which of the 7 retro TV moods to show, based on the Archy
 * system state: WebSocket connection, mood label, energy label, risk level,
 * and whether Archy is currently speaking.
 *
 * Priority order (highest first):
 *  1. WebSocket disconnected          → panic    (error state — red, shaking, static)
 *  2. Loading (initial mount)         → waking   (yellow, yawning, booting)
 *  3. mood = critical_panic           → panic    (red, X eyes, static)
 *  4. risk label = "critical"         → panic    (deadline about to blow)
 *  5. energy = depleted               → sleeping (blue, closed eyes, Zzz)
 *  6. mood = drift_alert + low energy → upset    (orange, angry eyes, tilted)
 *  7. risk label = "high"             → upset    (deadline pressure)
 *  8. mood = drift_alert              → sad      (blue, droopy, tears)
 *  9. energy = low                    → sad      (tired, melancholic)
 * 10. Archy speaking                 → happy    (green, smiling, stars — encouraging)
 * 11. Default (deep_focus / watchful) → happy    (green, smiling, stars)
 *
 * This hook also tracks "isTalking" — when a new assistantMessage arrives,
 * the mascot briefly shows the happy mood (Archy is encouraging the user).
 */
export function useRetroMascotMood(): RetroMood {
  const connected = useEventStore((s) => s.connected);
  const moodLabel = useEventStore((s) => s.mood?.label ?? null);
  const energyLabel = useEventStore((s) => s.energy?.label ?? null);
  const riskLabel = useEventStore((s) => s.risk?.label ?? null);
  const assistantMessage = useEventStore((s) => s.assistantMessage);

  // "Talking" state — Archy just sent a message, show happy for 8s
  const [isTalking, setIsTalking] = useState(false);
  useEffect(() => {
    if (assistantMessage?.text) {
      setIsTalking(true);
      const t = setTimeout(() => setIsTalking(false), 8000);
      return () => clearTimeout(t);
    }
  }, [assistantMessage]);

  return useMemo<RetroMood>(() => {
    return deriveRetroMood({
      connected,
      loading: false,
      moodLabel,
      energyLabel,
      riskLabel,
      isTalking,
    });
  }, [connected, moodLabel, energyLabel, riskLabel, isTalking]);
}

/** Pure derivation function — exported for testing. */
export function deriveRetroMood(params: {
  connected: boolean;
  loading: boolean;
  moodLabel: MoodLabel | null;
  energyLabel: EnergyLabel | null;
  riskLabel: string | null;
  isTalking: boolean;
}): RetroMood {
  // 1. Error state — WebSocket disconnected
  if (!params.connected) return "panic";
  // 2. Booting
  if (params.loading) return "waking";
  // 3. Critical mood or critical risk
  if (params.moodLabel === "critical_panic") return "panic";
  if (params.riskLabel === "critical") return "panic";
  // 4. Depleted energy
  if (params.energyLabel === "depleted") return "sleeping";
  // 5. Drift alert + low energy = upset (frustrated)
  // (Note: "depleted" already returned above, so only "low" can reach here)
  if (params.moodLabel === "drift_alert" && params.energyLabel === "low")
    return "upset";
  // 6. High risk = upset (deadline pressure)
  if (params.riskLabel === "high") return "upset";
  // 7. Drift alert = sad (things slipping)
  if (params.moodLabel === "drift_alert") return "sad";
  // 8. Low energy = sad (tired)
  if (params.energyLabel === "low") return "sad";
  // 9. Archy speaking = happy (encouraging)
  if (params.isTalking) return "happy";
  // 10. Default
  return "happy";
}
