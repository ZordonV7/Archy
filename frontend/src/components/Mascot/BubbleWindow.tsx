import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useState } from "react";
import { useEventStore } from "../../stores/eventStore";
import { hideBubble } from "../../hooks/useTauriWindow";

/// Separate window for Archy's speech bubble.
/// This window sits on top of the mascot window and displays messages
/// without being clipped by the mascot's small boundary.
export default function BubbleWindow() {
  const assistantMessage = useEventStore((s) => s.assistantMessage);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (assistantMessage?.text) {
      setVisible(true);
      // Auto-hide after 8 seconds
      const t = setTimeout(() => {
        setVisible(false);
        setTimeout(() => hideBubble(), 300); // wait for exit animation
      }, 8000);
      return () => clearTimeout(t);
    }
  }, [assistantMessage]);

  return (
    <div className="bubble-window">
      <AnimatePresence>
        {visible && assistantMessage?.text && (
          <motion.div
            className="bubble-content"
            initial={{ opacity: 0, y: 20, scale: 0.8 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 10, scale: 0.9 }}
            transition={{ type: "spring", stiffness: 400, damping: 25 }}
          >
            <p className="bubble-text">{assistantMessage.text}</p>
            <div className="bubble-tail" />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
