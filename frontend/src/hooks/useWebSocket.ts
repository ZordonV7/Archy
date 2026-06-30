import { useEffect, useRef } from "react";
import { useEventStore } from "../stores/eventStore";

export function useWebSocket(url: string) {
  const setConnected = useEventStore((s) => s.setConnected);
  const handleEvent = useEventStore((s) => s.handleEvent);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<number>();

  useEffect(() => {
    let mounted = true;

    const connect = () => {
      if (!mounted) return;
      try {
        const ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          console.log("[WS] Connected to Archy backend");
          setConnected(true);
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg && msg.type && msg.payload) {
              // CRITICAL: wrap handleEvent in try/catch so a store error
              // doesn't crash the WebSocket connection.
              try {
                handleEvent(msg.type, msg.payload);
              } catch (storeErr) {
                console.error("[WS] handleEvent threw (caught, not killing socket):", storeErr);
                console.error("[WS] Event was:", msg.type, msg.payload);
              }
            }
          } catch (parseErr) {
            console.error("[WS] Failed to parse message:", parseErr);
          }
        };

        ws.onclose = () => {
          console.log("[WS] Disconnected, will retry in 3s");
          setConnected(false);
          if (mounted) {
            reconnectTimer.current = window.setTimeout(connect, 3000);
          }
        };

        ws.onerror = (err) => {
          console.error("[WS] Error:", err);
          // Don't call ws.close() here — let onclose handle it.
          // Calling close() here can cause infinite reconnect loops.
        };
      } catch (connectErr) {
        console.error("[WS] Failed to connect:", connectErr);
        if (mounted) {
          reconnectTimer.current = window.setTimeout(connect, 3000);
        }
      }
    };

    connect();

    return () => {
      mounted = false;
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;  // prevent reconnect on unmount
        wsRef.current.close();
      }
    };
  }, [url, setConnected, handleEvent]);

  return wsRef;
}
