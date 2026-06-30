import { motion } from "framer-motion";
import { useEventStore, type MoodLabel, type EnergyLabel } from "../../stores/eventStore";

const MOOD_INFO: Record<MoodLabel, { emoji: string; label: string; color: string }> = {
  deep_focus: { emoji: "🎯", label: "Deep Focus", color: "#6366f1" },
  watchful: { emoji: "👀", label: "Watchful", color: "#10b981" },
  drift_alert: { emoji: "⚠️", label: "Drift Alert", color: "#f59e0b" },
  critical_panic: { emoji: "🚨", label: "Critical", color: "#ef4444" },
};

const ENERGY_INFO: Record<EnergyLabel, { emoji: string; label: string; color: string }> = {
  full: { emoji: "⚡", label: "Full", color: "#10b981" },
  steady: { emoji: "🔋", label: "Steady", color: "#3b82f6" },
  low: { emoji: "📉", label: "Low", color: "#f59e0b" },
  depleted: { emoji: "🪫", label: "Depleted", color: "#ef4444" },
};

function CircularGauge({ value, color, label, emoji }: { value: number; color: string; label: string; emoji: string }) {
  const radius = 26;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (value / 100) * circumference;

  return (
    <div className="sidebar-gauge">
      <svg width="64" height="64" viewBox="0 0 64 64">
        <circle cx="32" cy="32" r={radius} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="5" />
        <motion.circle
          cx="32"
          cy="32"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="5"
          strokeLinecap="round"
          strokeDasharray={circumference}
          initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 0.8, ease: "easeOut" }}
          transform="rotate(-90 32 32)"
          style={{ filter: `drop-shadow(0 0 4px ${color})` }}
        />
        <text x="32" y="30" textAnchor="middle" fontSize="14" fontWeight="700" fill={color}>
          {value}
        </text>
        <text x="32" y="40" textAnchor="middle" fontSize="7" fill="rgba(255,255,255,0.4)">
          /100
        </text>
      </svg>
      <div className="gauge-label">{emoji} {label}</div>
    </div>
  );
}

export default function SidebarGauges() {
  const mood = useEventStore((s) => s.mood);
  const energy = useEventStore((s) => s.energy);

  const moodInfo = mood ? (MOOD_INFO[mood.label] || MOOD_INFO.watchful) : MOOD_INFO.watchful;
  const energyInfo = energy ? (ENERGY_INFO[energy.label] || ENERGY_INFO.steady) : ENERGY_INFO.steady;
  const moodScore = mood?.score ?? 75;
  const energyScore = energy?.score ?? 80;

  return (
    <div className="sidebar-gauges">
      <CircularGauge
        value={moodScore}
        color={moodInfo.color}
        label="Mood"
        emoji={moodInfo.emoji}
      />
      <CircularGauge
        value={energyScore}
        color={energyInfo.color}
        label="Energy"
        emoji={energyInfo.emoji}
      />
    </div>
  );
}
