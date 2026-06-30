import { useEffect, useState, useRef } from "react";
import { motion } from "framer-motion";
import { useEventStore } from "../../stores/eventStore";
import { useArchyApi } from "../../hooks/useArchyApi";
import { useDraggable } from "../../hooks/useDraggable";
import { hideDashboard, quitApp } from "../../hooks/useTauriWindow";
import { API_BASE } from "../../lib/apiConfig";
import TaskList from "./TaskList";
import SidebarGauges from "./SidebarGauges";
import QuickIngest from "./QuickIngest";
import WorkSessionWindow from "./WorkSessionWindow";
import SimulationView from "./SimulationView";
import AnalyticsView from "./AnalyticsView";
import BriefingView from "./BriefingView";
import SettingsPanel from "../Settings/SettingsPanel";

export default function DashboardWindow() {
  const api = useArchyApi();
  const tasks = useEventStore((s) => s.tasks);
  const archivedTasks = useEventStore((s) => s.archivedTasks);
  const restoreTask = useEventStore((s) => s.restoreTask);
  const removeArchivedTask = useEventStore((s) => s.removeArchivedTask);
  const emptyTrash = useEventStore((s) => s.emptyTrash);
  const slots = useEventStore((s) => s.slots);
  const { dragHandleProps } = useDraggable();
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<"briefing" | "tasks" | "schedule" | "simulate" | "analytics" | "trash" | "settings">("briefing");
  const [error, setError] = useState<string | null>(null);
  const isDragging = useRef(false);

  // Mouse hover state — used to prevent auto-hide
  const isMouseOver = useRef(false);
  const hideTimerRef = useRef<number | null>(null);

  useEffect(() => {
    api.refreshTasks()
      .catch((e) => setError(`Failed to load tasks: ${e.message}`))
      .finally(() => setLoading(false));
  }, []);

  // Auto-hide dashboard when user clicks another app.
  // Uses BOTH focus events AND polling as fallback (WebView2 sometimes
  // doesn't fire focus events reliably on Windows).
  useEffect(() => {
    const mountTime = Date.now();
    const GRACE_PERIOD_MS = 3000;
    const HIDE_DELAY_MS = 1500;
    const POLL_INTERVAL_MS = 1000; // check every 1 second
    let cleanup: (() => void) | null = null;
    let pollTimer: number | null = null;

    (async () => {
      try {
        const { getAllWindows } = await import("@tauri-apps/api/window");
        const windows = await getAllWindows();
        const dashboard = windows.find((w) => w.label === "dashboard");
        const mascot = windows.find((w) => w.label === "mascot");

        if (!dashboard) return;

        // Method 1: Focus event listener
        const unlistenDash = await dashboard.onFocusChanged(async ({ event }) => {
          if (Date.now() - mountTime < GRACE_PERIOD_MS) return;

          if (event === "focus") {
            if (hideTimerRef.current) {
              clearTimeout(hideTimerRef.current);
              hideTimerRef.current = null;
            }
          } else {
            if (isMouseOver.current) return;
            if (hideTimerRef.current) clearTimeout(hideTimerRef.current);

            hideTimerRef.current = window.setTimeout(async () => {
              if (isMouseOver.current) return;
              try {
                if (mascot) {
                  const mascotFocused = await mascot.isFocused();
                  if (mascotFocused) return;
                }
                hideDashboard();
              } catch {}
            }, HIDE_DELAY_MS);
          }
        });
        cleanup = () => unlistenDash();

        // Method 2: Polling fallback — check every 1s if dashboard is focused
        pollTimer = window.setInterval(async () => {
          if (Date.now() - mountTime < GRACE_PERIOD_MS) return;
          if (isMouseOver.current) return;
          if (hideTimerRef.current) return; // already counting down

          try {
            const dashFocused = await dashboard.isFocused();
            if (!dashFocused) {
              if (mascot) {
                const mascotFocused = await mascot.isFocused();
                if (mascotFocused) return;
              }
              hideTimerRef.current = window.setTimeout(async () => {
                if (isMouseOver.current) return;
                try {
                  if (mascot) {
                    const mf = await mascot.isFocused();
                    if (mf) return;
                  }
                  hideDashboard();
                } catch {}
              }, HIDE_DELAY_MS);
            }
          } catch {}
        }, POLL_INTERVAL_MS);

      } catch {
        // Not in Tauri
      }
    })();

    return () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
      if (cleanup) cleanup();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, []);

  // Mouse handlers — attached directly to the dashboard DOM element
  const handleMouseEnter = () => {
    isMouseOver.current = true;
    // Cancel any pending hide immediately
    if (hideTimerRef.current) {
      clearTimeout(hideTimerRef.current);
      hideTimerRef.current = null;
    }
  };

  const handleMouseLeave = () => {
    isMouseOver.current = false;
    // Don't start hide here — let the focus listener handle it
  };

  // Safe action wrapper — prevents white screen on error
  const safeAction = async (fn: () => Promise<any>, label: string) => {
    try {
      setError(null);
      await fn();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(`${label} failed: ${msg}`);
      console.error(`[Dashboard] ${label} failed:`, e);
    }
  };

  // Prevent drag from causing re-renders — mark dragging state
  const handleDragStart = (e: React.MouseEvent) => {
    isDragging.current = true;
    dragHandleProps.onMouseDown(e);
  };

  return (
    <motion.div
      className="dashboard-window"
      initial={{ x: "100%", opacity: 0.5 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: "100%", opacity: 0.3 }}
      transition={{
        type: "spring",
        stiffness: 400,
        damping: 35,
        mass: 0.8,
      }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {/* Custom title bar — draggable. NO stopPropagation so drag works. */}
      <div className="titlebar" onMouseDown={handleDragStart} style={{ cursor: "grab" }}>
        <div className="titlebar-left">
          <span className="titlebar-title">Archy</span>
          <span className="titlebar-subtitle">Dashboard</span>
        </div>
        <div className="titlebar-controls">
          {/* All three buttons just hide the dashboard — quit is in Settings */}
          <button
            className="win-btn"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={() => hideDashboard()}
            title="Hide Dashboard"
          >
            <svg width="12" height="12" viewBox="0 0 12 12"><rect y="5" width="12" height="2" fill="currentColor" /></svg>
          </button>
          <button
            className="win-btn danger"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={() => hideDashboard()}
            title="Close Dashboard (mascot stays)"
          >
            <svg width="12" height="12" viewBox="0 0 12 12"><path d="M2 2 L10 10 M10 2 L2 10" stroke="currentColor" strokeWidth="1.5" /></svg>
          </button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div className="error-banner">
          <span>⚠️ {error}</span>
          <button onMouseDown={(e) => e.stopPropagation()} onClick={() => setError(null)}>×</button>
        </div>
      )}

      {/* Tabs */}
      <nav className="dashboard-tabs">
        <TabButton active={activeTab === "briefing"} onClick={() => setActiveTab("briefing")}>🏠 Home</TabButton>
        <TabButton active={activeTab === "tasks"} onClick={() => setActiveTab("tasks")}>Tasks</TabButton>
        <TabButton active={activeTab === "schedule"} onClick={() => setActiveTab("schedule")}>Schedule</TabButton>
        <TabButton active={activeTab === "simulate"} onClick={() => setActiveTab("simulate")}>🔬 Simulate</TabButton>
        <TabButton active={activeTab === "analytics"} onClick={() => setActiveTab("analytics")}>📊 Stats</TabButton>
        <TabButton active={activeTab === "trash"} onClick={() => setActiveTab("trash")}>🗑 Trash</TabButton>
        <TabButton active={activeTab === "settings"} onClick={() => setActiveTab("settings")}>Settings</TabButton>
      </nav>

      {/* Content — sidebar layout: circular gauges on top, main below */}
      <main className="dashboard-content-sidebar">
        <div className="dashboard-widgets-row">
          <SidebarGauges />
        </div>
        <div className="dashboard-main">
          {loading ? (
            <p className="empty-state">Loading tasks...</p>
          ) : (
            <>
              {/* Work session appears at top when active */}
              <WorkSessionWindow />

              {activeTab === "briefing" && <BriefingView />}
              {activeTab === "tasks" && (
                <>
                  <QuickIngest onIngest={(t) => safeAction(() => api.ingestText(t), "Ingest")} />
                  <TaskList
                    tasks={tasks}
                    onStart={(id) => safeAction(() => api.startTask(id), "Start task")}
                    onComplete={(id) => safeAction(() => api.completeTask(id), "Complete task")}
                    onCancel={(id) => safeAction(() => api.cancelTask(id), "Cancel task")}
                    onDelete={async (id) => {
                      // Permanently delete from database — NO mood effect
                      try {
                        await fetch(`${API_BASE}/tasks/${id}`, { method: "DELETE", credentials: "include" });
                        // FIX: previously this called archiveTask(id) which
                        // only works for ACTIVE tasks. If the user deletes a
                        // task that's already in the trash, archiveTask is a
                        // no-op and the UI never updates. Use the right
                        // action for each list.
                        const store = useEventStore.getState();
                        if (store.tasks.some((t) => t.id === id)) {
                          store.archiveTask(id);
                        } else if (store.archivedTasks.some((t) => t.id === id)) {
                          store.removeArchivedTask(id);
                        }
                      } catch (e) {
                        setError(`Delete failed: ${e}`);
                      }
                    }}
                  />
                </>
              )}
              {activeTab === "schedule" && (
                <ScheduleView
                  slots={slots}
                  onReschedule={() => safeAction(() => api.getSchedule(), "Reschedule")}
                  tasks={tasks}
                />
              )}
              {activeTab === "simulate" && <SimulationView />}
              {activeTab === "analytics" && <AnalyticsView />}
              {activeTab === "trash" && (
                <TrashView
                  archivedTasks={archivedTasks}
                  onRestore={async (id) => {
                    // FIX: previously this only flipped local state, so the
                    // backend row stayed cancelled/missed. Call the real
                    // /tasks/{id}/restore endpoint, and only update local
                    // state if the server confirms it.
                    try {
                      const resp = await fetch(`${API_BASE}/tasks/${id}/restore`, {
                        method: "POST",
                        credentials: "include",
                      });
                      if (resp.ok) {
                        restoreTask(id);
                      } else {
                        const data = await resp.json().catch(() => ({}));
                        setError(data.error || `Restore failed (HTTP ${resp.status})`);
                      }
                    } catch (e) {
                      setError(`Restore failed: ${e}`);
                    }
                  }}
                  onDelete={async (id) => {
                    // Permanently delete a single trashed task from the database
                    try {
                      const resp = await fetch(`${API_BASE}/tasks/${id}`, {
                        method: "DELETE",
                        credentials: "include",
                      });
                      if (resp.ok) {
                        removeArchivedTask(id);
                      } else {
                        setError(`Delete failed (HTTP ${resp.status})`);
                      }
                    } catch (e) {
                      setError(`Delete failed: ${e}`);
                    }
                  }}
                  onEmpty={async () => {
                    // Permanently delete all from database
                    try {
                      await fetch(`${API_BASE}/trash/empty`, { method: "DELETE", credentials: "include" });
                      emptyTrash();
                    } catch (e) {
                      setError(`Empty trash failed: ${e}`);
                    }
                  }}
                />
              )}
              {activeTab === "settings" && <SettingsPanel onConnectGoogle={api.triggerAuth} />}
            </>
          )}
        </div>
      </main>
    </motion.div>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return <button className={`tab-btn ${active ? "active" : ""}`} onClick={onClick}>{children}</button>;
}

function ScheduleView({ slots, onReschedule, tasks }: { slots: any[] | undefined | null; onReschedule: () => void; tasks: any[] }) {
  const safeSlots = Array.isArray(slots) ? slots : [];
  // FIX: previously the schedule list rendered slot.task_id.slice(0,8) which
  // showed a truncated UUID instead of the actual task title. Build a lookup
  // map so we can show the human-readable title.
  const taskTitleById = new Map<string, string>(
    (tasks ?? []).map((t) => [String(t.id), t.title || "Untitled"])
  );
  return (
    <div className="schedule-view">
      <div className="schedule-header">
        <h2>Today's Schedule</h2>
        <button className="action-btn" onClick={onReschedule}>Replan</button>
      </div>
      {safeSlots.length === 0 ? (
        <p className="empty-state">No scheduled tasks yet. Add a task to get started.</p>
      ) : (
        <div className="schedule-list">
          {safeSlots.map((slot, i) => (
            <motion.div
              key={i}
              className={`schedule-item ${slot.is_break ? "break" : ""} ${slot.at_risk ? "at-risk" : ""}`}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.05 }}
            >
              <div className="schedule-time">{formatTime(slot.start)} - {formatTime(slot.end)}</div>
              <div className="schedule-title">
                {slot.is_break
                  ? "☕ Break"
                  : (taskTitleById.get(String(slot.task_id)) || `Task ${String(slot.task_id).slice(0, 8)}`)}
                {slot.at_risk && <span className="at-risk-badge">AT RISK</span>}
              </div>
              {!slot.is_break && <div className="schedule-category">{slot.category}</div>}
            </motion.div>
          ))}
        </div>
      )}
    </div>
  );
}

function formatTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return iso; }
}

function TrashView({
  archivedTasks,
  onRestore,
  onEmpty,
  onDelete,
}: {
  archivedTasks: any[] | undefined | null;
  onRestore: (id: string) => void;
  onEmpty: () => void;
  onDelete: (id: string) => void;
}) {
  const safeTasks = Array.isArray(archivedTasks) ? archivedTasks : [];
  return (
    <div className="trash-view">
      <div className="trash-header">
        <h2>🗑 Trash ({safeTasks.length})</h2>
        {safeTasks.length > 0 && (
          <button className="action-btn danger" onClick={onEmpty}>Empty Trash</button>
        )}
      </div>
      {safeTasks.length === 0 ? (
        <p className="empty-state">Trash is empty. Completed/cancelled tasks will appear here.</p>
      ) : (
        <div className="trash-list">
          {safeTasks.map((task) => (
            <div key={task.id} className="task-card archived">
              <div className="task-header">
                <h3 className="task-title">{task.title}</h3>
                <span className="task-status" style={{ background: "#9ca3af" }}>
                  {task.status}
                </span>
              </div>
              <div className="task-meta">
                <span>📁 {task.category}</span>
                <span>⚡ {task.priority}/100</span>
              </div>
              <div className="task-actions">
                <button className="action-btn primary" onClick={() => onRestore(task.id)}>
                  ↩ Restore
                </button>
                <button
                  className="action-btn"
                  style={{ background: "rgba(239,68,68,0.2)", borderColor: "var(--danger)", color: "var(--danger)" }}
                  onClick={() => onDelete(task.id)}
                >
                  🗑 Delete
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
