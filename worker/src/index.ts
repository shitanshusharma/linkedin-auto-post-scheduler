/**
 * Telegram webhook + decision engine.
 *
 * Integrates:
 * - Telegram callbacks + edit text flow
 * - GitHub Contents API (posts.json as source of truth)
 * - LinkedIn publish API
 *
 * See docs/ARCHITECTURE.md for expected behavior.
 */

import {
  ERROR_CODES,
  IDEMPOTENCY_TTL_SECONDS,
  MAX_TELEGRAM_POST_LENGTH,
  POST_STATUSES,
  RATE_LIMIT_MAX_REQUESTS_PER_MINUTE,
  RESPONSE_MESSAGES,
} from "../common/constants";
import {
  handleApprove,
  handleConfirmEdit,
  handleEdit,
  handleReenterEdit,
  handleReject,
  handleRetry,
  resendApprovalMessage,
  setPostMessageId,
} from "./decision_handlers";
import { findPostIndex, mutatePostsWithRetry, readPosts } from "../common/github_posts";
import { logEvent } from "../common/logger";
import {
  answerCallbackQuery,
  inlineConfirmReenter,
  parseCallbackAction,
  sendPostBotMessage,
} from "../common/telegram_client";
import { Env, JsonObject, TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "../common/types";
import { asNumber, asString } from "../common/utils";

function statusOf(post: JsonObject): string {
  return asString(post.status) ?? "";
}

function tokenOf(post: JsonObject): string {
  return asString(post.approval_token) ?? "";
}

function postIdOf(post: JsonObject): string {
  return asString(post.id) ?? "";
}

function telegramMessageIdOf(post: JsonObject): number | null {
  return asNumber(post.telegram_message_id);
}

async function checkRateLimit(env: Env, userId: string): Promise<"ok" | "hit" | "error"> {
  if (!env.RATE_LIMIT_KV) {
    return "ok";
  }
  const key = `ratelimit:${userId}`;
  try {
    const raw = await env.RATE_LIMIT_KV.get(key);
    const count = Number.parseInt(raw ?? "0", 10);
    const current = Number.isFinite(count) && count > 0 ? count : 0;
    if (current >= RATE_LIMIT_MAX_REQUESTS_PER_MINUTE) {
      return "hit";
    }
    await env.RATE_LIMIT_KV.put(key, String(current + 1), { expirationTtl: 60 });
    return "ok";
  } catch {
    return "error";
  }
}

function isAuthorizedUser(userId: number | undefined, env: Env): boolean {
  if (typeof userId !== "number") {
    return false;
  }
  return String(userId) === env.TELEGRAM_USER_ID;
}

function isAuthorizedChat(chatId: number | undefined, env: Env): boolean {
  if (typeof chatId !== "number") {
    return false;
  }
  return String(chatId) === env.TELEGRAM_CHAT_ID;
}

function activeEditingPost(posts: JsonObject[]): JsonObject | null {
  return posts.find((post) => statusOf(post) === POST_STATUSES.EDITING) ?? null;
}

function idempotencyKeyFromUpdate(update: TelegramUpdate): string | null {
  const updateId = asNumber(update.update_id);
  if (updateId !== null) {
    return `update:${updateId}`;
  }

  const callbackId = update.callback_query?.id;
  if (callbackId) {
    return `callback:${callbackId}`;
  }

  const messageId = asNumber(update.message?.message_id);
  const chatId = asNumber(update.message?.chat?.id);
  if (messageId !== null && chatId !== null) {
    return `message:${chatId}:${messageId}`;
  }
  return null;
}

async function isDuplicateUpdate(env: Env, key: string): Promise<boolean> {
  if (!env.IDEMPOTENCY_KV) {
    return false;
  }
  try {
    const existing = await env.IDEMPOTENCY_KV.get(key);
    if (existing) {
      return true;
    }
    await env.IDEMPOTENCY_KV.put(key, "1", { expirationTtl: IDEMPOTENCY_TTL_SECONDS });
    return false;
  } catch {
    await logEvent(env, "idempotency_kv_error");
    return false;
  }
}

async function handleCallback(
  update: TelegramCallbackQuery,
  env: Env,
  idempotencyKey: string | null,
): Promise<Response> {
  const callbackId = update.id;
  const callerUserId = update.from?.id;
  const callerChatId = update.message?.chat?.id;

  if (!isAuthorizedUser(callerUserId, env) || !isAuthorizedChat(callerChatId, env)) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_UNAUTHORIZED);
    await logEvent(env, "webhook_security_violation reason=unauthorized_callback");
    return jsonOk();
  }

  if (idempotencyKey) {
    const duplicate = await isDuplicateUpdate(env, idempotencyKey);
    if (duplicate) {
      await logEvent(env, `duplicate_update_ignored key=${idempotencyKey}`);
      return jsonOk();
    }
  }

  const rateLimit = await checkRateLimit(env, String(callerUserId));
  if (rateLimit === "hit") {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_RATE_LIMITED);
    await logEvent(env, `rate_limit_hit user_id=${callerUserId}`);
    return jsonOk();
  }
  if (rateLimit === "error") {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_TRY_AGAIN);
    await logEvent(env, "rate_limit_kv_error");
    return jsonOk();
  }

  const parsed = parseCallbackAction(update.data);
  if (!parsed) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_INVALID_ACTION);
    await logEvent(env, "webhook_invalid_action reason=bad_callback_data");
    return jsonOk();
  }

  const { posts } = await readPosts(env);
  const idx = findPostIndex(posts, parsed.postId);
  if (idx < 0) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_POST_NOT_FOUND);
    await logEvent(env, `webhook_invalid_action reason=post_not_found post_id=${parsed.postId}`);
    return jsonOk();
  }
  const post = posts[idx];
  const status = statusOf(post);
  const postToken = tokenOf(post);
  if (!postToken || postToken !== parsed.approvalToken) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_INVALID_TOKEN);
    await logEvent(env, `webhook_security_violation reason=invalid_token post_id=${parsed.postId}`);
    return jsonOk();
  }

  const currentMsgId = telegramMessageIdOf(post);

  if (parsed.action === "a") {
    await handleApprove({
      env,
      callbackId,
      parsed,
      status,
      post,
      currentMsgId,
    });
    return jsonOk();
  }

  if (parsed.action === "e") {
    await handleEdit({
      env,
      callbackId,
      parsed,
      status,
      post,
      currentMsgId,
    });
    return jsonOk();
  }

  if (parsed.action === "r") {
    await handleReject({
      env,
      callbackId,
      parsed,
      status,
      post,
      currentMsgId,
    });
    return jsonOk();
  }

  if (parsed.action === "y") {
    await handleConfirmEdit({
      env,
      callbackId,
      parsed,
      status,
      post,
      currentMsgId,
    });
    return jsonOk();
  }

  if (parsed.action === "n") {
    await handleReenterEdit({
      env,
      callbackId,
      parsed,
      status,
      post,
      currentMsgId,
    });
    return jsonOk();
  }

  await handleRetry({
    env,
    callbackId,
    parsed,
    status,
    post,
    currentMsgId,
  });
  return jsonOk();
}

async function handleMessage(
  message: TelegramMessage,
  env: Env,
  idempotencyKey: string | null,
): Promise<Response> {
  const callerUserId = message.from?.id;
  const callerChatId = message.chat?.id;

  if (!isAuthorizedUser(callerUserId, env) || !isAuthorizedChat(callerChatId, env)) {
    await logEvent(env, "webhook_security_violation reason=unauthorized_message");
    return jsonOk();
  }

  if (idempotencyKey) {
    const duplicate = await isDuplicateUpdate(env, idempotencyKey);
    if (duplicate) {
      await logEvent(env, `duplicate_update_ignored key=${idempotencyKey}`);
      return jsonOk();
    }
  }

  const rateLimit = await checkRateLimit(env, String(callerUserId));
  if (rateLimit === "hit") {
    await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_RATE_LIMITED);
    await logEvent(env, `rate_limit_hit user_id=${callerUserId}`);
    return jsonOk();
  }
  if (rateLimit === "error") {
    await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_TRY_AGAIN);
    await logEvent(env, "rate_limit_kv_error");
    return jsonOk();
  }

  const { posts } = await readPosts(env);
  const editing = activeEditingPost(posts);
  if (!editing) {
    return jsonOk();
  }

  const postId = postIdOf(editing);
  if (!postId) {
    return jsonOk();
  }

  const text = message.text;
  if (typeof text !== "string") {
    await logEvent(env, `non_text_message_skipped post_id=${postId}`);
    return jsonOk();
  }

  const trimmed = text.trim();
  if (!trimmed) {
    await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_EDIT_EMPTY);
    return jsonOk();
  }

  if (trimmed.toLowerCase() === "cancel") {
    await mutatePostsWithRetry(env, `chore(worker): cancel edit ${postId}`, (writePosts) => {
      const idx = findPostIndex(writePosts, postId);
      if (idx < 0) {
        return;
      }
      writePosts[idx].status = POST_STATUSES.PENDING;
      writePosts[idx].proposed_edit = null;
    });
    await resendApprovalMessage(env, postId);
    await logEvent(env, `edit_cancelled post_id=${postId}`);
    return jsonOk();
  }

  if (trimmed.length > MAX_TELEGRAM_POST_LENGTH) {
    await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_EDIT_TOO_LONG);
    return jsonOk();
  }

  await mutatePostsWithRetry(env, `chore(worker): proposed edit ${postId}`, (writePosts) => {
    const idx = findPostIndex(writePosts, postId);
    if (idx < 0) {
      return;
    }
    writePosts[idx].status = POST_STATUSES.CONFIRMING_EDIT;
    writePosts[idx].proposed_edit = trimmed;
  });

  const { posts: refreshedPosts } = await readPosts(env);
  const refreshedIdx = findPostIndex(refreshedPosts, postId);
  if (refreshedIdx < 0) {
    return jsonOk();
  }
  const refreshed = refreshedPosts[refreshedIdx];
  const approvalToken = tokenOf(refreshed);
  const review =
    `📋 Review your edit:\n` +
    `───────────\n${trimmed}\n───────────\n\n` +
    `Do you confirm this change?`;
  const msgId = await sendPostBotMessage(env, review, inlineConfirmReenter(postId, approvalToken));
  await setPostMessageId(env, postId, msgId);
  return jsonOk();
}

function jsonOk(): Response {
  return new Response(JSON.stringify({ ok: true }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/health") {
      return jsonOk();
    }
    if (request.method !== "POST" || url.pathname !== "/webhook") {
      return new Response(RESPONSE_MESSAGES.HTTP_NOT_FOUND, { status: 404 });
    }

    const secret = request.headers.get("x-telegram-bot-api-secret-token") ?? "";
    if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      await logEvent(env, "webhook_security_violation reason=bad_secret");
      return new Response(RESPONSE_MESSAGES.HTTP_UNAUTHORIZED, { status: 401 });
    }

    let update: TelegramUpdate;
    try {
      update = (await request.json()) as TelegramUpdate;
    } catch {
      await logEvent(env, "webhook_invalid_action reason=invalid_json");
      return new Response(RESPONSE_MESSAGES.HTTP_BAD_REQUEST, { status: 400 });
    }

    const idempotencyKey = idempotencyKeyFromUpdate(update);

    try {
      if (update.callback_query) {
        return await handleCallback(update.callback_query, env, idempotencyKey);
      }
      if (update.message) {
        return await handleMessage(update.message, env, idempotencyKey);
      }
      return jsonOk();
    } catch (error) {
      const msg = error instanceof Error ? error.message : ERROR_CODES.UNKNOWN_WORKER_ERROR;
      await logEvent(env, `worker_error ${msg}`);
      return new Response(RESPONSE_MESSAGES.HTTP_INTERNAL_SERVER_ERROR, { status: 500 });
    }
  },
};
