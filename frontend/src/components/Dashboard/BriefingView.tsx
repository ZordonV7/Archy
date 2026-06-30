import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { API_BASE } from "../../lib/apiConfig";

interface BriefingData {
  greeting: string;
  intro: string;
  date: string;
  schedule_summary: {
    total_tasks: number;
    scheduled_today: number;
    pending: number;
    focus_blocks: number;
    meetings: number;
    deadlines_today: number;
    deadlines_tomorrow: number;
  };
  top_priority: {
    title: string;
    priority: number;
    suggested_start: string;
    duration: number;
    category: string;
  } | null;
  biggest_risk: {
    task_title: string;
    risk_score: number;
    risk_label: string;
    reason: string;
    recovery_actions: string[];
  } | null;
  current_state: {
    mood_score: number;
    mood_label: string;
    energy_score: number;
    energy_label: string;
  };
  insights: string[];
  schedule: Array<{
    time: string;
    title: string;
    duration: number;
    category: string;
    priority: number;
    is_meeting: boolean;
  }>;
}

const CATEGORY_ICONS: Record<string, string> = {
  finance: "💰",
  academics: "📚",
  work: "💼",
  personal: "🏠",
  health: "🏥",
  general: "📋",
};

const RISK_COLORS: Record<string, string> = {
  critical: "#ef4444",
  high: "#f97316",
  moderate: "#f59e0b",
  low: "#10b981",
};

const MOOD_EMOJIS: Record<string, string> = {
  deep_focus: "🎯",
  watchful: "👀",
  drift_alert: "⚠️",
  critical_panic: "🚨",
};

const ENERGY_EMOJIS: Record<string, string> = {
  full: "⚡",
  steady: "🔋",
  low: "📉",
  depleted: "🪫",
};

// Circular progress bar component
function CircularProgress({ value, label, color, emoji }: { value: number; label: string; color: string; emoji: string }) {
  const radius = 32;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (value / 100) * circumference;

  return (
    <div className="circular-bar">
      <svg width="80" height="80" viewBox="0 0 80 80">
        {/* Background circle */}
        <circle
          cx="40"
          cy="40"
          r={radius}
          fill="none"
          stroke="rgba(255,255,255,0.08)"
          strokeWidth="6"
        />
        {/* Progress circle */}
        <motion.circle
          cx="40"
          cy="40"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={circumference}
          initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 1, ease: "easeOut" }}
          transform="rotate(-90 40 40)"
          style={{ filter: `drop-shadow(0 0 6px ${color})` }}
        />
        {/* Center text */}
        <text x="40" y="38" textAnchor="middle" fontSize="16" fontWeight="700" fill={color}>
          {value}
        </text>
        <text x="40" y="50" textAnchor="middle" fontSize="8" fill="rgba(255,255,255,0.5)">
          /100
        </text>
      </svg>
      <div className="circular-label">{emoji} {label}</div>
    </div>
  );
}

export default function BriefingView() {
  const [briefing, setBriefing] = useState<BriefingData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchBriefing = async () => {
    try {
      const resp = await fetch(`${API_BASE}/briefing`, { credentials: "include" });
      if (resp.ok) {
        const data = await resp.json();
        setBriefing(data);
        setLoading(false);
      }
    } catch (e) {
      console.error("Failed to fetch briefing:", e);
      // On error, stop loading but keep whatever we have
      setLoading(false);
    }
  };

  useEffect(() => {
    // Try to load cached briefing from localStorage for instant display
    try {
      const cached = localStorage.getItem("archy_briefing_cache");
      if (cached) {
        const parsed = JSON.parse(cached);
        setBriefing(parsed);
        setLoading(false); // Show cached immediately — no loading screen
      }
    } catch {}

    // Then fetch fresh from backend
    fetchBriefing();
  }, []);

  // Save to localStorage whenever briefing updates
  useEffect(() => {
    if (briefing) {
      try {
        localStorage.setItem("archy_briefing_cache", JSON.stringify(briefing));
      } catch {}
    }
  }, [briefing]);

  if (loading) {
    return (
      <div className="briefing-view">
        <div className="briefing-hero" style={{ opacity: 0.5 }}>
          <div className="briefing-date">Loading...</div>
          <h1 className="briefing-greeting">Getting your briefing...</h1>
          <p className="briefing-intro">One moment — checking your schedule.</p>
        </div>
      </div>
    );
  }

  if (!briefing) {
    return (
      <div className="briefing-view">
        <p className="empty-state">Couldn't load briefing. Make sure the backend is running.</p>
      </div>
    );
  }

  const s = briefing.schedule_summary;
  const isCached = (briefing as any).cached === true;

  return (
    <div className="briefing-view">
      {/* Greeting + Archy's intro */}
      <motion.div
        className="briefing-hero"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 200, damping: 20 }}
      >
        <div className="briefing-date">
          {briefing.date}
          {isCached && <span className="cached-badge"> (cached)</span>}
        </div>
        <h1 className="briefing-greeting">{briefing.greeting}! 💜</h1>
        <p className="briefing-intro">{briefing.intro}</p>
      </motion.div>

      {/* Schedule summary cards */}
      <div className="briefing-summary">
        <SummaryCard icon="🎯" count={s.focus_blocks} label="Focus Blocks" color="#8b5cf6" />
        <SummaryCard icon="💬" count={s.meetings} label="Meetings" color="#3b82f6" />
        <SummaryCard icon="⏰" count={s.deadlines_today} label="Due Today" color="#ef4444" />
        <SummaryCard icon="📅" count={s.deadlines_tomorrow} label="Due Tomorrow" color="#f59e0b" />
      </div>

      {/* Top priority */}
      {briefing.top_priority && (
        <motion.div
          className="briefing-section briefing-priority"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.2 }}
        >
          <h3>⭐ Top Priority</h3>
          <div className="priority-card">
            <div className="priority-icon">
              {CATEGORY_ICONS[briefing.top_priority.category] || "📋"}
            </div>
            <div className="priority-info">
              <div className="priority-title">{briefing.top_priority.title}</div>
              <div className="priority-meta">
                <span>⚡ {briefing.top_priority.priority}/100</span>
                <span>⏱ {briefing.top_priority.duration}min</span>
                <span>🕐 Start: {briefing.top_priority.suggested_start}</span>
              </div>
            </div>
          </div>
        </motion.div>
      )}

      {/* Biggest risk */}
      {briefing.biggest_risk && briefing.biggest_risk.risk_score >= 35 && (
        <motion.div
          className="briefing-section briefing-risk"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.3 }}
        >
          <h3>⚠️ Biggest Risk</h3>
          <div
            className="risk-card"
            style={{ borderColor: RISK_COLORS[briefing.biggest_risk.risk_label] || "#f59e0b" }}
          >
            <div className="risk-task">{briefing.biggest_risk.task_title}</div>
            <div className="risk-score-row">
              <span
                className="risk-score-badge"
                style={{ background: RISK_COLORS[briefing.biggest_risk.risk_label] || "#f59e0b" }}
              >
                {briefing.biggest_risk.risk_score}% risk
              </span>
              <span className="risk-reason">{briefing.biggest_risk.reason}</span>
            </div>
            {briefing.biggest_risk.recovery_actions.length > 0 && (
              <div className="risk-recovery">
                <div className="recovery-label">💡 Recovery plan:</div>
                {briefing.biggest_risk.recovery_actions.slice(0, 2).map((action, i) => (
                  <div key={i} className="recovery-action">• {action}</div>
                ))}
              </div>
            )}
          </div>
        </motion.div>
      )}

      {/* Current state — circular bars */}
      <motion.div
        className="briefing-section briefing-state"
        initial={{ opacity: 0, x: -20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ delay: 0.4 }}
      >
        <h3>📊 Your State</h3>
        <div className="circular-row">
          <CircularProgress
            value={briefing.current_state.mood_score}
            label="Mood"
            color="#8b5cf6"
            emoji={MOOD_EMOJIS[briefing.current_state.mood_label] || "👀"}
          />
          <CircularProgress
            value={briefing.current_state.energy_score}
            label="Energy"
            color={
              briefing.current_state.energy_score >= 60 ? "#10b981"
              : briefing.current_state.energy_score >= 40 ? "#f59e0b" : "#ef4444"
            }
            emoji={ENERGY_EMOJIS[briefing.current_state.energy_label] || "🔋"}
          />
        </div>
      </motion.div>

      {/* Today's schedule */}
      {briefing.schedule.length > 0 && (
        <motion.div
          className="briefing-section briefing-schedule"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.5 }}
        >
          <h3>🗓 Today's Schedule</h3>
          {briefing.schedule.map((item, i) => (
            <div key={i} className="briefing-schedule-item">
              <div className="schedule-time">{item.time}</div>
              <div className="schedule-icon">
                {item.is_meeting ? "💬" : CATEGORY_ICONS[item.category] || "📋"}
              </div>
              <div className="schedule-info">
                <div className="schedule-title">{item.title}</div>
                <div className="schedule-duration">{item.duration}min</div>
              </div>
            </div>
          ))}
        </motion.div>
      )}

      {/* Insights */}
      {briefing.insights.length > 0 && (
        <motion.div
          className="briefing-section briefing-insights"
          initial={{ opacity: 0, x: -20 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: 0.6 }}
        >
          <h3>🧠 What I've Learned</h3>
          {briefing.insights.map((insight, i) => (
            <div key={i} className="insight-item">{insight}</div>
          ))}
        </motion.div>
      )}

      <button className="action-btn" onClick={fetchBriefing} style={{ marginTop: "12px", width: "100%" }}>
        🔄 Refresh Briefing
      </button>
    </div>
  );
}

function SummaryCard({ icon, count, label, color }: { icon: string; count: number; label: string; color: string }) {
  return (
    <motion.div
      className="summary-card"
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: "spring", stiffness: 300, damping: 20 }}
      style={{ borderColor: color }}
    >
      <div className="summary-icon">{icon}</div>
      <div className="summary-count" style={{ color }}>{count}</div>
      <div className="summary-label">{label}</div>
    </motion.div>
  );
}
