import { useCallback, useRef } from "react";
import { isTauri } from "./useTauriWindow";

/// Hook for making elements BOTH draggable AND clickable.
///
/// KEY INSIGHT: In Tauri, startDragging() captures the mouse at OS level.
/// Once called, no further JS events fire. So we must:
/// 1. On mouseDown: record start position, DON'T call startDragging yet
/// 2. On mouseMove (document): if moved > 5px, call startDragging() — it's a drag
/// 3. On mouseUp (document): if didn't move, do nothing — let onClick fire naturally
///
/// This way: click works (no drag started), drag works (drag started after 5px).
/// DO NOT use stopPropagation on child elements that need both behaviors.
export function useDraggable() {
  const dragRef = useRef<HTMLElement | null>(null);
  const stateRef = useRef({
    pendingDrag: false,
    startX: 0,
    startY: 0,
    startedDragging: false,
    // Browser drag state
    browserDragging: false,
    browserOffsetX: 0,
    browserOffsetY: 0,
  });

  const onDragStart = useCallback(async (e: React.MouseEvent) => {
    // Only start on left mouse button
    if (e.button !== 0) return;

    const inTauri = await isTauri();
    const st = stateRef.current;
    st.pendingDrag = true;
    st.startedDragging = false;
    st.startX = e.clientX;
    st.startY = e.clientY;

    if (inTauri) {
      // Tauri: defer startDragging until movement confirmed
      const onMove = async (ev: MouseEvent) => {
        if (!st.pendingDrag) return;
        const dx = Math.abs(ev.clientX - st.startX);
        const dy = Math.abs(ev.clientY - st.startY);

        // Start drag only after 5px of movement
        if (dx > 5 || dy > 5) {
          st.pendingDrag = false;
          st.startedDragging = true;
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          try {
            const { getCurrentWindow } = await import("@tauri-apps/api/window");
            await getCurrentWindow().startDragging();
          } catch (err) {
            console.error("[Drag] Tauri startDragging failed:", err);
          }
        }
      };

      const onUp = () => {
        // If didn't drag, it's a click — let onClick fire naturally
        st.pendingDrag = false;
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    } else {
      // Browser: manual drag via CSS transform
      st.browserDragging = false;
      const el = dragRef.current;
      if (el) {
        const transform = el.style.transform || "";
        const match = transform.match(/translate\(([-\d.]+)px,\s*([-\d.]+)px\)/);
        st.browserOffsetX = match ? parseFloat(match[1]) : 0;
        st.browserOffsetY = match ? parseFloat(match[2]) : 0;
      }

      const onMove = (ev: MouseEvent) => {
        const dx = ev.clientX - st.startX;
        const dy = ev.clientY - st.startY;
        if (Math.abs(dx) > 5 || Math.abs(dy) > 5) {
          st.browserDragging = true;
          st.pendingDrag = false;
        }
        if (st.browserDragging) {
          const el = dragRef.current;
          if (el) {
            el.style.transform = `translate(${st.browserOffsetX + dx}px, ${st.browserOffsetY + dy}px)`;
          }
        }
      };

      const onUp = () => {
        st.browserDragging = false;
        st.pendingDrag = false;
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }
  }, []);

  return {
    dragRef,
    dragHandleProps: {
      onMouseDown: onDragStart,
      style: { cursor: "grab" },
    },
  };
}
