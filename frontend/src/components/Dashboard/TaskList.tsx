import { motion, AnimatePresence } from "framer-motion";
import type { Task } from "../../stores/eventStore";

interface TaskListProps {
  tasks: Task[] | undefined | null;
  onStart: (id: string) => Promise<any>;
  onComplete: (id: string) => Promise<any>;
  onCancel: (id: string) => Promise<any>;
  onDelete?: (id: string) => Promise<any>;
}

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pending: { label: "Pending", color: "#6b7280" },
  scheduled: { label: "Scheduled", color: "#3b82f6" },
  in_progress: { label: "In Progress", color: "#f59e0b" },
  done: { label: "Done", color: "#10b981" },
  missed: { label: "Missed", color: "#ef4444" },
  cancelled: { label: "Cancelled", color: "#9ca3af" },
  created: { label: "Created", color: "#6b7280" },
  classified: { label: "Classified", color: "#3b82f6" },
  active: { label: "Active", color: "#f59e0b" },
  completed: { label: "Completed", color: "#10b981" },
  archived: { label: "Archived", color: "#9ca3af" },
};

export default function TaskList({ tasks, onStart, onComplete, onCancel, onDelete }: TaskListProps) {
  // CRITICAL: defensive default — never crash on undefined tasks
  const safeTasks: Task[] = Array.isArray(tasks) ? tasks : [];

  if (safeTasks.length === 0) {
    return <p className="empty-state">No tasks yet. Tell Archy what you need to do above.</p>;
  }

  return (
    <div className="task-list">
      <h2>Tasks ({safeTasks.length})</h2>
      <AnimatePresence>
        {safeTasks.map((task) => {
          const status = STATUS_LABELS[task?.status] || STATUS_LABELS.pending;
          return (
            <motion.div
              key={task.id}
              className="task-card"
              layout
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, x: -50 }}
              whileHover={{ scale: 1.01 }}
            >
              <div className="task-header">
                <h3 className="task-title">{task.title || "Untitled"}</h3>
                <span className="task-status" style={{ background: status.color }}>
                  {status.label}
                </span>
              </div>
              <div className="task-meta">
                <span className="meta-item">📁 {task.category || "general"}</span>
                <span className="meta-item">⚡ {task.priority ?? 50}/100</span>
                <span className="meta-item">🧠 {task.complexity ?? 50}/100</span>
                <span className="meta-item">⏱ {task.duration_minutes ?? 30}min</span>
                {task.deadline && (
                  <span className="meta-item deadline">📅 {formatDate(task.deadline)}</span>
                )}
              </div>
              {task.scheduled_start && (
                <div className="task-schedule">
                  Scheduled: {formatDateTime(task.scheduled_start)} → {formatDateTime(task.scheduled_end)}
                </div>
              )}
              <div className="task-actions">
                {task.status === "scheduled" && (
                  <button className="action-btn primary" onClick={() => onStart(task.id)}>
                    Start
                  </button>
                )}
                {(task.status === "scheduled" || task.status === "in_progress") && (
                  <button className="action-btn success" onClick={() => onComplete(task.id)}>
                    Complete
                  </button>
                )}
                {task.status !== "done" && task.status !== "cancelled" && (
                  <button className="action-btn danger" onClick={() => onCancel(task.id)}>
                    Cancel
                  </button>
                )}
                {(task.status === "done" || task.status === "cancelled" || task.status === "missed") && onDelete && (
                  <button
                    className="action-btn"
                    style={{ background: "rgba(239,68,68,0.2)", borderColor: "var(--danger)", color: "var(--danger)" }}
                    onClick={() => onDelete(task.id)}
                  >
                    🗑 Delete
                  </button>
                )}
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function formatDateTime(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString([], { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}
