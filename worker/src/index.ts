/**
 * Telegram webhook + decision engine.
 *
 * Integrates:
 * - Telegram callbacks + edit text flow
 * - GitHub Contents API (posts.json as source of truth)
 * - LinkedIn publish API
 *
 * See low-level-design.md for expected behavior.
 */

import {
  ERROR_CODES,
  MAX_DRAFT_PREVIEW_LENGTH,
  MAX_TELEGRAM_POST_LENGTH,
  RATE_LIMIT_MAX_REQUESTS_PER_MINUTE,
  RESPONSE_MESSAGES,
} from "./constants";
import { findPostIndex, mutatePostsWithRetry, readPosts } from "./github_posts";
import { publishToLinkedIn } from "./linkedin_client";
import { logEvent } from "./logger";
import {
  answerCallbackQuery,
  clearInlineKeyboard,
  inlineApproveEditReject,
  inlineConfirmReenter,
  inlineRetry,
  parseCallbackAction,
  sendPostBotMessage,
} from "./telegram_client";
import { Env, JsonObject, TelegramCallbackQuery, TelegramMessage, TelegramUpdate } from "./types";
import { asNumber, asString, nowIso } from "./utils";

function statusOf(post: JsonObject): string {
  return asString(post.status) ?? "";
}

function tokenOf(post: JsonObject): string {
  return asString(post.approval_token) ?? "";
}

function composedTextOf(post: JsonObject): string {
  return asString(post.composed_text) ?? "";
}

function topicOf(post: JsonObject): string {
  return asString(post.topic) ?? "Unknown";
}

function riskFlagsOf(post: JsonObject): string[] {
  const raw = post.risk_flags;
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw.filter((item): item is string => typeof item === "string");
}

function postIdOf(post: JsonObject): string {
  return asString(post.id) ?? "";
}

function telegramMessageIdOf(post: JsonObject): number | null {
  return asNumber(post.telegram_message_id);
}

function draftMessage(post: JsonObject): string {
  const composed = composedTextOf(post);
  const preview = composed.length > MAX_DRAFT_PREVIEW_LENGTH ? `${composed.slice(0, MAX_DRAFT_PREVIEW_LENGTH)}...` : composed;
  const flags = riskFlagsOf(post);
  const flagsText = flags.length > 0 ? flags.join(", ") : "None";
  return `📝 New LinkedIn Draft\n\nTopic: ${topicOf(post)}\n\n---\n${preview}\n---\n\nRisk Flags: ${flagsText}`;
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
  return posts.find((post) => statusOf(post) === "editing") ?? null;
}

async function setPostMessageId(env: Env, postId: string, messageId: number | null): Promise<void> {
  if (messageId === null) {
    return;
  }
  await mutatePostsWithRetry(env, `chore(worker): track telegram msg for ${postId}`, (posts) => {
    const idx = findPostIndex(posts, postId);
    if (idx >= 0) {
      posts[idx].telegram_message_id = messageId;
    }
  });
}

async function resendApprovalMessage(env: Env, postId: string): Promise<void> {
  const { posts } = await readPosts(env);
  const idx = findPostIndex(posts, postId);
  if (idx < 0) {
    return;
  }
  const post = posts[idx];
  const approvalToken = tokenOf(post);
  const msgId = await sendPostBotMessage(env, draftMessage(post), inlineApproveEditReject(postId, approvalToken));
  await setPostMessageId(env, postId, msgId);
}

async function runPublishFlow(env: Env, postId: string): Promise<void> {
  const { posts } = await readPosts(env);
  const idx = findPostIndex(posts, postId);
  if (idx < 0) {
    return;
  }
  const post = posts[idx];
  const composed = composedTextOf(post);
  const approvalToken = tokenOf(post);
  const publish = await publishToLinkedIn(env, composed);

  if (publish.ok) {
    await mutatePostsWithRetry(env, `chore(worker): mark posted ${postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].status = "posted";
      writePosts[writeIdx].posted_at = nowIso();
      writePosts[writeIdx].linkedin_post_id = publish.linkedinPostId ?? null;
      writePosts[writeIdx].error = null;
    });

    await sendPostBotMessage(env, `✅ Post Published!\n\nTopic: ${topicOf(post)}\nPosted at: ${nowIso()}`);
    await logEvent(env, `publish_success post_id=${postId}`);
    return;
  }

  await mutatePostsWithRetry(env, `chore(worker): mark failed ${postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].status = "failed";
    writePosts[writeIdx].error = publish.error;
  });
  const text =
    `⚠️ Publishing Failed\n\n` +
    `Topic: ${topicOf(post)}\n` +
    `Error: ${publish.error}\n\n` +
    `⚠️ Post may have been published if this was a timeout.\n` +
    `Check your LinkedIn profile before retrying.`;
  const retryMarkup = inlineRetry(postId, approvalToken);
  const msgId = await sendPostBotMessage(env, text, retryMarkup);
  await setPostMessageId(env, postId, msgId);
  await logEvent(env, `publish_failed post_id=${postId} error=${publish.error}`);
}

async function handleCallback(update: TelegramCallbackQuery, env: Env): Promise<Response> {
  const callbackId = update.id;
  const callerUserId = update.from?.id;
  const callerChatId = update.message?.chat?.id;

  if (!isAuthorizedUser(callerUserId, env) || !isAuthorizedChat(callerChatId, env)) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_UNAUTHORIZED);
    await logEvent(env, "webhook_security_violation reason=unauthorized_callback");
    return jsonOk();
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
    if (status !== "pending") {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
      await logEvent(env, `webhook_invalid_action action=a status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_PUBLISHING);
    await mutatePostsWithRetry(env, `chore(worker): approve ${parsed.postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, parsed.postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].status = "approved";
      writePosts[writeIdx].approved_at = nowIso();
      writePosts[writeIdx].publish_attempted_at = nowIso();
      writePosts[writeIdx].error = null;
    });
    await logEvent(env, `approval_received post_id=${parsed.postId}`);
    await runPublishFlow(env, parsed.postId);
    return jsonOk();
  }

  if (parsed.action === "e") {
    if (status !== "pending") {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
      await logEvent(env, `webhook_invalid_action action=e status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await mutatePostsWithRetry(env, `chore(worker): edit start ${parsed.postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, parsed.postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].status = "editing";
      writePosts[writeIdx].proposed_edit = null;
      writePosts[writeIdx].error = null;
    });
    const editPrompt =
      `✏️ Edit Mode\n\n` +
      `Current post:\n` +
      `───────────\n${composedTextOf(post)}\n───────────\n\n` +
      `Reply with your corrected post text.\n` +
      `Send "cancel" to exit edit mode.`;
    await sendPostBotMessage(env, editPrompt);
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_EDIT_MODE_ENABLED);
    await logEvent(env, `edit_started post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "r") {
    if (status !== "pending") {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
      await logEvent(env, `webhook_invalid_action action=r status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await mutatePostsWithRetry(env, `chore(worker): reject ${parsed.postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, parsed.postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].status = "rejected";
      writePosts[writeIdx].error = null;
    });
    await sendPostBotMessage(env, `❌ Draft rejected.\n\nTopic: ${topicOf(post)}`);
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_REJECTED);
    await logEvent(env, `rejection_received post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "y") {
    if (status !== "confirming_edit") {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
      await logEvent(env, `webhook_invalid_action action=y status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    const proposed = asString(post.proposed_edit) ?? "";
    if (!proposed || proposed.length > MAX_TELEGRAM_POST_LENGTH) {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_INVALID_EDIT_TEXT);
      await logEvent(env, `webhook_invalid_action action=y invalid_proposed_edit post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await mutatePostsWithRetry(env, `chore(worker): confirm edit ${parsed.postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, parsed.postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].composed_text = proposed;
      writePosts[writeIdx].proposed_edit = null;
      writePosts[writeIdx].status = "pending";
      writePosts[writeIdx].error = null;
    });
    await resendApprovalMessage(env, parsed.postId);
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_EDIT_APPLIED);
    await logEvent(env, `edit_confirmed post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "n") {
    if (status !== "confirming_edit") {
      await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
      await logEvent(env, `webhook_invalid_action action=n status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await mutatePostsWithRetry(env, `chore(worker): re-enter edit ${parsed.postId}`, (writePosts) => {
      const writeIdx = findPostIndex(writePosts, parsed.postId);
      if (writeIdx < 0) {
        return;
      }
      writePosts[writeIdx].status = "editing";
      writePosts[writeIdx].proposed_edit = null;
    });
    await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_EDIT_REENTER_PROMPT);
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_EDIT_REENTER);
    await logEvent(env, `edit_re_entered post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (status !== "failed") {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=rt status=${status} post_id=${parsed.postId}`);
    return jsonOk();
  }
  await clearInlineKeyboard(env, currentMsgId);
  await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_RETRYING_PUBLISH);
  await mutatePostsWithRetry(env, `chore(worker): retry publish ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].publish_attempted_at = nowIso();
    writePosts[writeIdx].error = null;
  });
  await logEvent(env, `retry_attempted post_id=${parsed.postId}`);
  await runPublishFlow(env, parsed.postId);
  return jsonOk();
}

async function handleMessage(message: TelegramMessage, env: Env): Promise<Response> {
  const callerUserId = message.from?.id;
  const callerChatId = message.chat?.id;

  if (!isAuthorizedUser(callerUserId, env) || !isAuthorizedChat(callerChatId, env)) {
    await logEvent(env, "webhook_security_violation reason=unauthorized_message");
    return jsonOk();
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
      writePosts[idx].status = "pending";
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
    writePosts[idx].status = "confirming_edit";
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

    try {
      if (update.callback_query) {
        return await handleCallback(update.callback_query, env);
      }
      if (update.message) {
        return await handleMessage(update.message, env);
      }
      return jsonOk();
    } catch (error) {
      const msg = error instanceof Error ? error.message : ERROR_CODES.UNKNOWN_WORKER_ERROR;
      await logEvent(env, `worker_error ${msg}`);
      return new Response(RESPONSE_MESSAGES.HTTP_INTERNAL_SERVER_ERROR, { status: 500 });
    }
  },
};
