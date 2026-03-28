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

type JsonObject = Record<string, unknown>;
type JsonArray = unknown[];

interface KVNamespaceLike {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, options?: { expirationTtl?: number }): Promise<void>;
}

interface Env {
  GH_FINE_GRAINED_PAT: string;
  GH_REPO: string;
  TELEGRAM_POST_BOT_TOKEN: string;
  TELEGRAM_LOG_BOT_TOKEN?: string;
  TELEGRAM_LOG_CHAT_ID?: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  TELEGRAM_CHAT_ID: string;
  TELEGRAM_USER_ID: string;
  LINKEDIN_ACCESS_TOKEN: string;
  LINKEDIN_PERSON_ID: string;
  RATE_LIMIT_KV?: KVNamespaceLike;
}

interface TelegramUser {
  id: number;
}

interface TelegramChat {
  id: number;
}

interface TelegramMessage {
  message_id: number;
  chat: TelegramChat;
  from?: TelegramUser;
  text?: string;
}

interface TelegramCallbackQuery {
  id: string;
  from: TelegramUser;
  data?: string;
  message?: TelegramMessage;
}

interface TelegramUpdate {
  callback_query?: TelegramCallbackQuery;
  message?: TelegramMessage;
}

interface GithubContentsResponse {
  sha: string;
  content: string;
  encoding: string;
}

interface ReadPostsResult {
  posts: JsonObject[];
  sha: string;
}

interface WritePostsResult {
  ok: boolean;
  conflict: boolean;
}

interface CallbackAction {
  action: "a" | "e" | "r" | "y" | "n" | "rt";
  postId: string;
  approvalToken: string;
}

const POSTS_PATH = "posts.json";
const MAX_TELEGRAM_POST_LENGTH = 2000;
const MAX_DRAFT_PREVIEW_LENGTH = 500;
const RATE_LIMIT_MAX_REQUESTS_PER_MINUTE = 20;
const CALLBACK_DATA_MAX_BYTES = 64;
const LINKEDIN_VERSION = "202603";

function nowIso(): string {
  return new Date().toISOString();
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function encodeBase64Utf8(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }
  return btoa(binary);
}

function decodeBase64Utf8(value: string): string {
  const clean = value.replace(/\s+/g, "");
  const binary = atob(clean);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new TextDecoder().decode(bytes);
}

function ghHeaders(env: Env): HeadersInit {
  return {
    Authorization: `Bearer ${env.GH_FINE_GRAINED_PAT}`,
    Accept: "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": "linkedin-post-webhook",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

function githubContentsUrl(env: Env, path: string): string {
  return `https://api.github.com/repos/${env.GH_REPO}/contents/${path}`;
}

async function readPosts(env: Env): Promise<ReadPostsResult> {
  const response = await fetch(githubContentsUrl(env, POSTS_PATH), {
    method: "GET",
    headers: ghHeaders(env),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`github_read_failed status=${response.status} body=${body}`);
  }
  const data = (await response.json()) as GithubContentsResponse;
  const raw = decodeBase64Utf8(data.content);
  const parsed = JSON.parse(raw) as unknown;
  if (!Array.isArray(parsed)) {
    throw new Error("posts.json must be a JSON array");
  }
  const posts = parsed.filter((item): item is JsonObject => typeof item === "object" && item !== null);
  return { posts, sha: data.sha };
}

async function writePosts(env: Env, posts: JsonArray, sha: string, message: string): Promise<WritePostsResult> {
  const content = encodeBase64Utf8(`${JSON.stringify(posts, null, 2)}\n`);
  const response = await fetch(githubContentsUrl(env, POSTS_PATH), {
    method: "PUT",
    headers: ghHeaders(env),
    body: JSON.stringify({
      message,
      content,
      sha,
    }),
  });

  if (response.ok) {
    return { ok: true, conflict: false };
  }
  if (response.status === 409) {
    return { ok: false, conflict: true };
  }
  const body = await response.text();
  throw new Error(`github_write_failed status=${response.status} body=${body}`);
}

async function mutatePostsWithRetry<T>(
  env: Env,
  commitMessage: string,
  mutator: (posts: JsonObject[]) => T,
): Promise<T> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const { posts, sha } = await readPosts(env);
    const result = mutator(posts);
    const write = await writePosts(env, posts, sha, commitMessage);
    if (write.ok) {
      return result;
    }
    if (!write.conflict || attempt === 1) {
      throw new Error("github_conflict");
    }
  }
  throw new Error("unreachable");
}

function findPostIndex(posts: JsonObject[], postId: string): number {
  return posts.findIndex((p) => asString(p.id) === postId);
}

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

function callbackData(prefix: string, postId: string, approvalToken: string): string {
  const data = `${prefix}:${postId}:${approvalToken}`;
  const bytes = new TextEncoder().encode(data).length;
  if (bytes > CALLBACK_DATA_MAX_BYTES) {
    throw new Error(`callback_data exceeds ${CALLBACK_DATA_MAX_BYTES} bytes (${bytes})`);
  }
  return data;
}

function inlineApproveEditReject(postId: string, approvalToken: string): JsonObject {
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

function inlineConfirmReenter(postId: string, approvalToken: string): JsonObject {
  return {
    inline_keyboard: [
      [
        { text: "✅ Confirm Change", callback_data: callbackData("y", postId, approvalToken) },
        { text: "❌ Re-enter", callback_data: callbackData("n", postId, approvalToken) },
      ],
    ],
  };
}

function inlineRetry(postId: string, approvalToken: string): JsonObject {
  return {
    inline_keyboard: [[{ text: "🔄 Retry Publish", callback_data: callbackData("rt", postId, approvalToken) }]],
  };
}

function draftMessage(post: JsonObject): string {
  const composed = composedTextOf(post);
  const preview = composed.length > MAX_DRAFT_PREVIEW_LENGTH ? `${composed.slice(0, MAX_DRAFT_PREVIEW_LENGTH)}...` : composed;
  const flags = riskFlagsOf(post);
  const flagsText = flags.length > 0 ? flags.join(", ") : "None";
  return `📝 New LinkedIn Draft\n\nTopic: ${topicOf(post)}\n\n---\n${preview}\n---\n\nRisk Flags: ${flagsText}`;
}

function sanitizeForLinkedIn(value: string): string {
  const noAngles = value.replace(/[<>]/g, "");
  const normalized = noAngles.replace(/\r\n/g, "\n").trim();
  return normalized.slice(0, MAX_TELEGRAM_POST_LENGTH);
}

function parseCallbackAction(value: string | undefined): CallbackAction | null {
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
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
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

async function sendPostBotMessage(
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

async function answerCallbackQuery(env: Env, callbackQueryId: string, text: string): Promise<void> {
  try {
    await telegramApi(env.TELEGRAM_POST_BOT_TOKEN, "answerCallbackQuery", {
      callback_query_id: callbackQueryId,
      text,
    });
  } catch {
    // Non-fatal; callback answer should not block core flow.
  }
}

async function clearInlineKeyboard(env: Env, telegramMessageId: number | null): Promise<void> {
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

async function logEvent(env: Env, text: string): Promise<void> {
  const token = env.TELEGRAM_LOG_BOT_TOKEN?.trim();
  const chatId = env.TELEGRAM_LOG_CHAT_ID?.trim();
  if (!token || !chatId) {
    return;
  }
  try {
    await telegramApi(token, "sendMessage", { chat_id: chatId, text });
  } catch {
    // Fire-and-forget by design.
  }
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

interface PublishResult {
  ok: boolean;
  linkedinPostId?: string;
  error: string;
}

async function publishToLinkedIn(env: Env, composedText: string): Promise<PublishResult> {
  const commentary = sanitizeForLinkedIn(composedText);
  if (!commentary) {
    return { ok: false, error: "empty_post_after_sanitization" };
  }

  const payload: JsonObject = {
    author: `urn:li:person:${env.LINKEDIN_PERSON_ID}`,
    lifecycleState: "PUBLISHED",
    visibility: "PUBLIC",
    commentary,
    distribution: {
      feedDistribution: "MAIN_FEED",
    },
  };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort("timeout"), 15000);
  let response: Response;
  try {
    response = await fetch("https://api.linkedin.com/rest/posts", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.LINKEDIN_ACCESS_TOKEN}`,
        "Content-Type": "application/json",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      return { ok: false, error: "timeout" };
    }
    return { ok: false, error: "network_error" };
  } finally {
    clearTimeout(timer);
  }

  const bodyText = await response.text();
  if (response.status === 201) {
    let linkedinPostId: string | undefined;
    try {
      const payloadJson = JSON.parse(bodyText) as JsonObject;
      linkedinPostId = asString(payloadJson.id) ?? undefined;
    } catch {
      linkedinPostId = undefined;
    }
    return { ok: true, linkedinPostId, error: "" };
  }
  if (response.status === 401) {
    return { ok: false, error: "linkedin_401_reauth_required" };
  }
  if (response.status === 429) {
    return { ok: false, error: "linkedin_429_rate_limited" };
  }
  return { ok: false, error: `linkedin_status_${response.status}: ${bodyText.slice(0, 500)}` };
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
    await answerCallbackQuery(env, callbackId, "Unauthorized");
    await logEvent(env, "webhook_security_violation reason=unauthorized_callback");
    return jsonOk();
  }

  const rateLimit = await checkRateLimit(env, String(callerUserId));
  if (rateLimit === "hit") {
    await answerCallbackQuery(env, callbackId, "Rate limited. Try again later.");
    await logEvent(env, `rate_limit_hit user_id=${callerUserId}`);
    return jsonOk();
  }
  if (rateLimit === "error") {
    await answerCallbackQuery(env, callbackId, "Try again.");
    await logEvent(env, "rate_limit_kv_error");
    return jsonOk();
  }

  const parsed = parseCallbackAction(update.data);
  if (!parsed) {
    await answerCallbackQuery(env, callbackId, "Invalid action");
    await logEvent(env, "webhook_invalid_action reason=bad_callback_data");
    return jsonOk();
  }

  const { posts } = await readPosts(env);
  const idx = findPostIndex(posts, parsed.postId);
  if (idx < 0) {
    await answerCallbackQuery(env, callbackId, "Post not found");
    await logEvent(env, `webhook_invalid_action reason=post_not_found post_id=${parsed.postId}`);
    return jsonOk();
  }
  const post = posts[idx];
  const status = statusOf(post);
  const postToken = tokenOf(post);
  if (!postToken || postToken !== parsed.approvalToken) {
    await answerCallbackQuery(env, callbackId, "Invalid token");
    await logEvent(env, `webhook_security_violation reason=invalid_token post_id=${parsed.postId}`);
    return jsonOk();
  }

  const currentMsgId = telegramMessageIdOf(post);

  if (parsed.action === "a") {
    if (status !== "pending") {
      await answerCallbackQuery(env, callbackId, "Action not allowed");
      await logEvent(env, `webhook_invalid_action action=a status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    await clearInlineKeyboard(env, currentMsgId);
    await answerCallbackQuery(env, callbackId, "Publishing...");
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
      await answerCallbackQuery(env, callbackId, "Action not allowed");
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
    await answerCallbackQuery(env, callbackId, "Edit mode enabled");
    await logEvent(env, `edit_started post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "r") {
    if (status !== "pending") {
      await answerCallbackQuery(env, callbackId, "Action not allowed");
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
    await answerCallbackQuery(env, callbackId, "Rejected");
    await logEvent(env, `rejection_received post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "y") {
    if (status !== "confirming_edit") {
      await answerCallbackQuery(env, callbackId, "Action not allowed");
      await logEvent(env, `webhook_invalid_action action=y status=${status} post_id=${parsed.postId}`);
      return jsonOk();
    }
    const proposed = asString(post.proposed_edit) ?? "";
    if (!proposed || proposed.length > MAX_TELEGRAM_POST_LENGTH) {
      await answerCallbackQuery(env, callbackId, "Invalid edited text");
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
    await answerCallbackQuery(env, callbackId, "Edit applied");
    await logEvent(env, `edit_confirmed post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (parsed.action === "n") {
    if (status !== "confirming_edit") {
      await answerCallbackQuery(env, callbackId, "Action not allowed");
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
    await sendPostBotMessage(env, "Send your corrected post text again.");
    await answerCallbackQuery(env, callbackId, "Please edit again");
    await logEvent(env, `edit_re_entered post_id=${parsed.postId}`);
    return jsonOk();
  }

  if (status !== "failed") {
    await answerCallbackQuery(env, callbackId, "Action not allowed");
    await logEvent(env, `webhook_invalid_action action=rt status=${status} post_id=${parsed.postId}`);
    return jsonOk();
  }
  await clearInlineKeyboard(env, currentMsgId);
  await answerCallbackQuery(env, callbackId, "Retrying publish...");
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
    await sendPostBotMessage(env, "Rate limited. Try again later.");
    await logEvent(env, `rate_limit_hit user_id=${callerUserId}`);
    return jsonOk();
  }
  if (rateLimit === "error") {
    await sendPostBotMessage(env, "Try again.");
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
    await sendPostBotMessage(env, "Edit cannot be empty. Send text (1-2000 chars) or 'cancel'.");
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
    await sendPostBotMessage(env, "Edit too long. Max 2000 characters. Send again or 'cancel'.");
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
      return new Response("Not found", { status: 404 });
    }

    const secret = request.headers.get("x-telegram-bot-api-secret-token") ?? "";
    if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      await logEvent(env, "webhook_security_violation reason=bad_secret");
      return new Response("Unauthorized", { status: 401 });
    }

    let update: TelegramUpdate;
    try {
      update = (await request.json()) as TelegramUpdate;
    } catch {
      await logEvent(env, "webhook_invalid_action reason=invalid_json");
      return new Response("Bad Request", { status: 400 });
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
      const msg = error instanceof Error ? error.message : "unknown_worker_error";
      await logEvent(env, `worker_error ${msg}`);
      return new Response("Internal Server Error", { status: 500 });
    }
  },
};
