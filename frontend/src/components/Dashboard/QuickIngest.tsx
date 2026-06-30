import { useState } from "react";
import { motion } from "framer-motion";

interface QuickIngestProps {
  onIngest: (text: string) => Promise<any>;
}

export default function QuickIngest({ onIngest }: QuickIngestProps) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!text.trim() || loading) return;
    setLoading(true);
    setError(null);
    try {
      await onIngest(text);
      setText("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Ingest failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <motion.form
      className="quick-ingest"
      onSubmit={handleSubmit}
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <input
        type="text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Tell Archy what you need to do... (e.g., 'I have to file taxes before midnight')"
        disabled={loading}
        className="ingest-input"
      />
      <motion.button
        type="submit"
        className="ingest-btn"
        disabled={loading || !text.trim()}
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.98 }}
      >
        {loading ? "Thinking..." : "Send to Archy"}
      </motion.button>
      {error && <p className="ingest-error">{error}</p>}
    </motion.form>
  );
}
