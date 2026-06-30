import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { API_BASE } from "../../lib/apiConfig";

interface SimulationData {
  current_probability: number;
  optimized_probability: number;
  improvement: number;
  summary: string;
  tasks_analyzed: number;
  high_risk_count: number;
  changes: Array<{
    task_title: string;
    change_type: string;
    description: string;
    before_risk: number;
    after_risk: number;
  }>;
  current_assessments: Array<{
    task_title: string;
    risk_score: number;
    risk_label: string;
    hours_needed: number;
    hours_available: number;
    reasons: string[];
  }>;
  optimized_assessments: Array<{
    task_title: string;
    risk_score: number;
    risk_label: string;
  }>;
}

export default function SimulationView() {
  const [simulation, setSimulation] = useState<SimulationData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runSimulation = async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`${API_BASE}/simulation/run`, { method: "POST", credentials: "include" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      setSimulation(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Simulation failed");
    } finally {
      setLoading(false);
    }
  };

  const getProbColor = (prob: number) => {
    if (prob >= 80) return "#10b981"; // green
    if (prob >= 50) return "#f59e0b"; // yellow
    return "#ef4444"; // red
  };

  const getRiskColor = (risk: string) => {
    switch (risk) {
      case "critical": return "#ef4444";
      case "high": return "#f97316";
      case "moderate": return "#f59e0b";
      case "low": return "#10b981";
      default: return "#6b7280";
    }
  };

  return (
    <div className="simulation-view">
      <div className="simulation-header">
        <h2>🔬 Failure Simulation</h2>
        <button
          className="action-btn primary"
          onClick={runSimulation}
          disabled={loading}
        >
          {loading ? "Simulating..." : "Run Simulation"}
        </button>
      </div>

      {error && <p className="ingest-error">{error}</p>}

      <AnimatePresence>
        {simulation && (
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ type: "spring", stiffness: 200, damping: 20 }}
          >
            {/* Summary */}
            <div className="sim-summary">
              <p className="sim-summary-text">{simulation.summary}</p>
            </div>

            {/* Before vs After probability */}
            <div className="sim-comparison">
              <div className="sim-column">
                <div className="sim-label">Current</div>
                <div
                  className="sim-probability"
                  style={{ color: getProbColor(simulation.current_probability) }}
                >
                  {simulation.current_probability}%
                </div>
                <div className="sim-sublabel">success probability</div>
                {simulation.high_risk_count > 0 && (
                  <div className="sim-warning">
                    ⚠️ {simulation.high_risk_count} high-risk task(s)
                  </div>
                )}
              </div>

              <motion.div
                className="sim-arrow"
                animate={{ x: [0, 5, 0] }}
                transition={{ duration: 1.5, repeat: Infinity }}
              >
                →
              </motion.div>

              <div className="sim-column">
                <div className="sim-label">Optimized</div>
                <div
                  className="sim-probability"
                  style={{ color: getProbColor(simulation.optimized_probability) }}
                >
                  {simulation.optimized_probability}%
                </div>
                <div className="sim-sublabel">
                  {simulation.improvement >= 0 ? "+" : ""}{simulation.improvement}% improvement
                </div>
              </div>
            </div>

            {/* Current task risks */}
            {simulation.current_assessments && simulation.current_assessments.length > 0 && (
              <div className="sim-section">
                <h3>📊 Task Risk Breakdown</h3>
                {simulation.current_assessments.map((task, i) => {
                  const optimized = simulation.optimized_assessments[i];
                  return (
                    <div key={i} className="sim-task-row">
                      <div className="sim-task-title">{task.task_title}</div>
                      <div className="sim-task-risk">
                        <span
                          className="risk-badge"
                          style={{ background: getRiskColor(task.risk_label) }}
                        >
                          {task.risk_score}%
                        </span>
                        {optimized && optimized.risk_score !== task.risk_score && (
                          <>
                            <span className="risk-arrow">→</span>
                            <span
                              className="risk-badge"
                              style={{ background: getRiskColor(optimized.risk_label) }}
                            >
                              {optimized.risk_score}%
                            </span>
                          </>
                        )}
                      </div>
                      <div className="sim-task-hours">
                        {task.hours_needed}h needed / {task.hours_available}h available
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {/* Changes */}
            {simulation.changes && simulation.changes.length > 0 && (
              <div className="sim-section">
                <h3>🔧 Recommended Changes ({simulation.changes.length})</h3>
                {simulation.changes.map((change, i) => (
                  <div key={i} className="sim-change">
                    <span className={`change-type ${change.change_type}`}>
                      {change.change_type === "split" ? "✂️" : change.change_type === "moved" ? "📅" : "🔄"} {change.change_type}
                    </span>
                    <span className="change-desc">{change.description}</span>
                  </div>
                ))}
              </div>
            )}

            {/* Task analysis count */}
            <div className="sim-footer">
              Analyzed {simulation.tasks_analyzed} task(s) with deadlines
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {!simulation && !loading && (
        <p className="empty-state">
          Run a simulation to see your schedule's success probability and get optimization recommendations.
        </p>
      )}
    </div>
  );
}
