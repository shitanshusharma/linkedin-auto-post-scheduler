import { API_URLS, CALLBACK_DATA_MAX_BYTES } from "./constants";
import { asNumber } from "./utils";
import { CallbackAction, Env, JsonObject } from "./types";

function callbackData(prefix: string, postId: string, approvalToken: string): string {
  const data = `${prefix}:${postId}:${approvalToken}`;
  const bytes = new TextEncoder().encode(data).length;
  if (bytes > CALLBACK_DATA_MAX_BYTES) {
    throw new Error(`callback_data exceeds ${CALLBACK_DATA_MAX_BYTES} bytes (${bytes})`);
  }
  return data;
}

export function inlineApproveEditReject(postId: string, approvalToken: string): JsonObject {
  return {
    inline_keyboard: [
      [
        { text: "✅ Approve", callback_data: callbackData("a", postId, approvalToken) },
        { text: "✏️ Edit", callback_data: callbackData("e", postId, approvalToken) },
        { text: "❌ Reject", callback_data: callbackData("r", postId, approvalToken) },
      ],
    ],
  };
}

export function inlineConfirmReenter(postId: string, approvalToken: string): JsonObject {
  return {
    inline_keyboard: [
      [
        { text: "✅ Confirm Change", callback_data: callbackData("y", postId, approvalToken) },
        { text: "❌ Re-enter", callback_data: callbackData("n", postId, approvalToken) },
      ],
    ],
  };
}

export function inlineRetry(postId: string, approvalToken: string): JsonObject {
  return {
    inline_keyboard: [[{ text: "🔄 Retry Publish", callback_data: callbackData("rt", postId, approvalToken) }]],
  };
}

export function parseCallbackAction(value: string | undefined): CallbackAction | null {
  if (!value) {
    return null;
  }
  const match = value.match(/^(a|e|r|y|n|rt):([^:]+):([A-Za-z0-9]+)$/);
  if (!match) {
    return null;
  }
  const action = match[1];
  if (action !== "a" && action !== "e" && action !== "r" && action !== "y" && action !== "n" && action !== "rt") {
    return null;
  }
  return {
    action,
    postId: match[2],
    approvalToken: match[3],
  };
}

async function telegramApi(token: string, method: string, payload: JsonObject): Promise<JsonObject> {
  const response = await fetch(`${API_URLS.TELEGRAM_BOT_API_BASE}${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = (await response.json()) as JsonObject;
  if (!response.ok) {
    throw new Error(`telegram_${method}_failed status=${response.status} body=${JSON.stringify(data)}`);
  }
  return data;
}

export async function sendPostBotMessage(
  env: Env,
  text: string,
  replyMarkup?: JsonObject,
): Promise<number | null> {
  const payload: JsonObject = {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
  };
  if (replyMarkup) {
    payload.reply_markup = replyMarkup;
  }
  const data = await telegramApi(env.TELEGRAM_POST_BOT_TOKEN, "sendMessage", payload);
  const result = (data.result ?? null) as JsonObject | null;
  if (!result) {
    return null;
  }
  return asNumber(result.message_id);
}

export async function answerCallbackQuery(env: Env, callbackQueryId: string, text: string): Promise<void> {
  try {
    await telegramApi(env.TELEGRAM_POST_BOT_TOKEN, "answerCallbackQuery", {
      callback_query_id: callbackQueryId,
      text,
    });
  } catch {
    // Non-fatal; callback answer should not block core flow.
  }
}

export async function clearInlineKeyboard(env: Env, telegramMessageId: number | null): Promise<void> {
  if (telegramMessageId === null) {
    return;
  }
  try {
    await telegramApi(env.TELEGRAM_POST_BOT_TOKEN, "editMessageReplyMarkup", {
      chat_id: env.TELEGRAM_CHAT_ID,
      message_id: telegramMessageId,
      reply_markup: { inline_keyboard: [] },
    });
  } catch {
    // Best effort only; stale buttons are annoying but not fatal.
  }
}

