import { API_URLS } from "./constants";

interface WorkerLogEnv {
  TELEGRAM_LOG_BOT_TOKEN?: string;
  TELEGRAM_LOG_CHAT_ID?: string;
}

export async function logEvent(env: WorkerLogEnv, text: string): Promise<void> {
  const token = env.TELEGRAM_LOG_BOT_TOKEN?.trim();
  const chatId = env.TELEGRAM_LOG_CHAT_ID?.trim();
  if (!token || !chatId) {
    return;
  }
  try {
    await fetch(`${API_URLS.TELEGRAM_BOT_API_BASE}${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text }),
    });
  } catch {
    // Fire-and-forget by design.
  }
}

