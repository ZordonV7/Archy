import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { API_BASE } from "../../lib/apiConfig";

interface AnalyticsData {
  total_sessions: number;
  completed_sessions: number;
  completion_rate: number;
  avg_interruptions: number;
  avg_estimated: number;
  avg_actual: number;
  duration_accuracy: number;
  category_stats: Record<string, {
    sessions: number;
    avg_estimated: number;
    avg_actual: number;
    avg_interruptions: number;
    accuracy: number;
  }>;
  productivity_by_hour: Record<string, number>;
  best_hour: number | null;
  message: string | null;
}

export default function AnalyticsView() {
  const [analytics, setAnalytics] = useState<AnalyticsData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchAnalytics = async () => {
    setLoading(true);
    try {
      const resp = await fetch(`${API_BASE}/sessions/analytics`, { credentials: "include" });
      if (resp.ok) {
        const data = await resp.json();
        setAnalytics(data);
      }
    } catch (e) {
      console.error("Failed to fetch analytics:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchAnalytics();
  }, []);

  if (loading) {
    return <p className="empty-state">Loading analytics...</p>;
  }

  if (!analytics || analytics.total_sessions === 0) {
    return (
      <div className="analytics-view">
        <h2>📊 Focus Session Analytics</h2>
        <p className="empty-state">
          {analytics?.message || "No completed sessions yet. Start a task to begin collecting analytics."}
        </p>
      </div>
    );
  }

  const getAccuracyColor = (accuracy: number) => {
    if (accuracy >= 80) return "#10b981";
    if (accuracy >= 60) return "#f59e0b";
    return "#ef4444";
  };

  const formatHour = (hour: number | null) => {
    if (hour === null) return "—";
    const h = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;
    const period = hour >= 12 ? "PM" : "AM";
    return `${h}:00 ${period}`;
  };

  return (
    <div className="analytics-view">
      <h2>📊 Focus Session Analytics</h2>

      {/* Overview stats */}
      <div className="analytics-overview">
        <motion.div
          className="stat-card"
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0 }}
        >
          <div className="stat-value">{analytics.completed_sessions}</div>
          <div className="stat-label">Sessions Completed</div>
        </motion.div>

        <motion.div
          className="stat-card"
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.1 }}
        >
          <div className="stat-value">{analytics.completion_rate}%</div>
          <div className="stat-label">Completion Rate</div>
        </motion.div>

        <motion.div
          className="stat-card"
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.2 }}
        >
          <div className="stat-value">{analytics.avg_interruptions}</div>
          <div className="stat-label">Avg Interruptions</div>
        </motion.div>

        <motion.div
          className="stat-card"
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.3 }}
        >
          <div className="stat-value" style={{ color: getAccuracyColor(analytics.duration_accuracy) }}>
            {analytics.duration_accuracy}%
          </div>
          <div className="stat-label">Duration Accuracy</div>
        </motion.div>
      </div>

      {/* Duration comparison */}
      <div className="analytics-section">
        <h3>⏱ Duration: Estimated vs Actual</h3>
        <div className="duration-comparison">
          <div className="duration-bar-container">
            <div className="duration-label">Estimated</div>
            <div className="duration-bar">
              <motion.div
                className="duration-fill estimated"
                initial={{ width: 0 }}
                animate={{ width: "100%" }}
                transition={{ duration: 0.8 }}
              />
            </div>
            <div className="duration-value">{analytics.avg_estimated} min avg</div>
          </div>

          <div className="duration-bar-container">
            <div className="duration-label">Actual</div>
            <div className="duration-bar">
              <motion.div
                className="duration-fill actual"
                initial={{ width: 0 }}
                animate={{
                  width: `${Math.min(150, (analytics.avg_actual / Math.max(analytics.avg_estimated, 1)) * 100)}%`,
                }}
                transition={{ duration: 0.8, delay: 0.3 }}
              />
            </div>
            <div className="duration-value">{analytics.avg_actual} min avg</div>
          </div>

          {analytics.avg_actual > analytics.avg_estimated && (
            <div className="duration-insight">
              ⚠️ You take {Math.round(((analytics.avg_actual - analytics.avg_estimated) / analytics.avg_estimated) * 100)}% longer than estimated on average.
            </div>
          )}
          {analytics.avg_actual <= analytics.avg_estimated && (
            <div className="duration-insight positive">
              ✓ You finish faster than estimated — great focus!
            </div>
          )}
        </div>
      </div>

      {/* Category breakdown */}
      {Object.keys(analytics.category_stats).length > 0 && (
        <div className="analytics-section">
          <h3>📁 By Category</h3>
          {Object.entries(analytics.category_stats).map(([cat, stats]) => (
            <div key={cat} className="category-row">
              <div className="category-name">{cat}</div>
              <div className="category-stats">
                <span className="cat-stat">{stats.sessions} sessions</span>
                <span className="cat-stat">{stats.avg_estimated}→{stats.avg_actual}min</span>
                <span className="cat-stat">{stats.avg_interruptions} interruptions</span>
                <span
                  className="cat-stat accuracy"
                  style={{ color: getAccuracyColor(stats.accuracy) }}
                >
                  {stats.accuracy}% accurate
                </span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Productivity by hour */}
      {Object.keys(analytics.productivity_by_hour).length > 0 && (
        <div className="analytics-section">
          <h3>🕐 Most Productive Hours</h3>
          <div className="hour-bars">
            {Object.entries(analytics.productivity_by_hour)
              .sort(([, a], [, b]) => Number(b) - Number(a))
              .map(([hour, count]) => {
                const maxCount = Math.max(...Object.values(analytics.productivity_by_hour));
                const pct = (Number(count) / maxCount) * 100;
                return (
                  <div key={hour} className="hour-bar-row">
                    <div className="hour-label">{formatHour(Number(hour))}</div>
                    <div className="hour-bar">
                      <motion.div
                        className="hour-bar-fill"
                        initial={{ width: 0 }}
                        animate={{ width: `${pct}%` }}
                        transition={{ duration: 0.6 }}
                      />
                    </div>
                    <div className="hour-count">{count}</div>
                  </div>
                );
              })}
          </div>
          {analytics.best_hour !== null && (
            <div className="duration-insight positive">
              ✓ You're most productive around {formatHour(analytics.best_hour)}.
            </div>
          )}
        </div>
      )}

      <button className="action-btn" onClick={fetchAnalytics} style={{ marginTop: "12px" }}>
        🔄 Refresh
      </button>
    </div>
  );
}
