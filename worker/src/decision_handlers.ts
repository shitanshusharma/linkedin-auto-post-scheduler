import { MAX_TELEGRAM_POST_LENGTH, POST_STATUSES, RESPONSE_MESSAGES } from "../common/constants";
import { findPostIndex, mutatePostsWithRetry, readPosts } from "../common/github_posts";
import { publishToLinkedIn } from "../common/linkedin_client";
import { logEvent } from "../common/logger";
import {
  answerCallbackQuery,
  clearInlineKeyboard,
  inlineApproveEditReject,
  inlineRetry,
  sendPostBotMessage,
} from "../common/telegram_client";
import { CallbackAction, Env, JsonObject } from "../common/types";
import { asString, nowIso } from "../common/utils";

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

function draftMessage(post: JsonObject): string {
  const flags = riskFlagsOf(post);
  const flagsText = flags.length > 0 ? flags.join(", ") : "None";
  return `📝 New LinkedIn Draft\n\nTopic: ${topicOf(post)}\n\n---\n${composedTextOf(post)}\n---\n\nRisk Flags: ${flagsText}`;
}

export async function setPostMessageId(env: Env, postId: string, messageId: number | null): Promise<void> {
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

export async function resendApprovalMessage(env: Env, postId: string): Promise<void> {
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
      writePosts[writeIdx].status = POST_STATUSES.POSTED;
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
    writePosts[writeIdx].status = POST_STATUSES.FAILED;
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

interface HandlerContext {
  env: Env;
  callbackId: string;
  parsed: CallbackAction;
  status: string;
  post: JsonObject;
  currentMsgId: number | null;
}

export async function handleApprove({
  env,
  callbackId,
  parsed,
  status,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.PENDING) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=a status=${status} post_id=${parsed.postId}`);
    return;
  }

  const approved = await mutatePostsWithRetry(env, `chore(worker): approve ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return false;
    }
    if (asString(writePosts[writeIdx].status) !== POST_STATUSES.PENDING) {
      return false;
    }
    writePosts[writeIdx].status = POST_STATUSES.APPROVED;
    writePosts[writeIdx].approved_at = nowIso();
    writePosts[writeIdx].publish_attempted_at = nowIso();
    writePosts[writeIdx].error = null;
    return true;
  });

  if (!approved) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=a race_blocked post_id=${parsed.postId}`);
    return;
  }

  await clearInlineKeyboard(env, currentMsgId);
  await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_PUBLISHING);
  await logEvent(env, `approval_received post_id=${parsed.postId}`);
  await runPublishFlow(env, parsed.postId);
}

export async function handleEdit({
  env,
  callbackId,
  parsed,
  status,
  post,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.PENDING) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=e status=${status} post_id=${parsed.postId}`);
    return;
  }
  await clearInlineKeyboard(env, currentMsgId);
  await mutatePostsWithRetry(env, `chore(worker): edit start ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].status = POST_STATUSES.EDITING;
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
}

export async function handleReject({
  env,
  callbackId,
  parsed,
  status,
  post,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.PENDING) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=r status=${status} post_id=${parsed.postId}`);
    return;
  }
  await clearInlineKeyboard(env, currentMsgId);
  await mutatePostsWithRetry(env, `chore(worker): reject ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].status = POST_STATUSES.REJECTED;
    writePosts[writeIdx].error = null;
  });
  await sendPostBotMessage(env, `❌ Draft rejected.\n\nTopic: ${topicOf(post)}`);
  await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_REJECTED);
  await logEvent(env, `rejection_received post_id=${parsed.postId}`);
}

export async function handleRetry({
  env,
  callbackId,
  parsed,
  status,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.FAILED) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=rt status=${status} post_id=${parsed.postId}`);
    return;
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
}

export async function handleConfirmEdit({
  env,
  callbackId,
  parsed,
  status,
  post,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.CONFIRMING_EDIT) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=y status=${status} post_id=${parsed.postId}`);
    return;
  }

  const proposed = asString(post.proposed_edit) ?? "";
  if (!proposed || proposed.length > MAX_TELEGRAM_POST_LENGTH) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_INVALID_EDIT_TEXT);
    await logEvent(env, `webhook_invalid_action action=y invalid_proposed_edit post_id=${parsed.postId}`);
    return;
  }

  await clearInlineKeyboard(env, currentMsgId);
  await mutatePostsWithRetry(env, `chore(worker): confirm edit ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].composed_text = proposed;
    writePosts[writeIdx].proposed_edit = null;
    writePosts[writeIdx].status = POST_STATUSES.PENDING;
    writePosts[writeIdx].error = null;
  });
  await resendApprovalMessage(env, parsed.postId);
  await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_EDIT_APPLIED);
  await logEvent(env, `edit_confirmed post_id=${parsed.postId}`);
}

export async function handleReenterEdit({
  env,
  callbackId,
  parsed,
  status,
  currentMsgId,
}: HandlerContext): Promise<void> {
  if (status !== POST_STATUSES.CONFIRMING_EDIT) {
    await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_ACTION_NOT_ALLOWED);
    await logEvent(env, `webhook_invalid_action action=n status=${status} post_id=${parsed.postId}`);
    return;
  }

  await clearInlineKeyboard(env, currentMsgId);
  await mutatePostsWithRetry(env, `chore(worker): re-enter edit ${parsed.postId}`, (writePosts) => {
    const writeIdx = findPostIndex(writePosts, parsed.postId);
    if (writeIdx < 0) {
      return;
    }
    writePosts[writeIdx].status = POST_STATUSES.EDITING;
    writePosts[writeIdx].proposed_edit = null;
  });
  await sendPostBotMessage(env, RESPONSE_MESSAGES.MESSAGE_EDIT_REENTER_PROMPT);
  await answerCallbackQuery(env, callbackId, RESPONSE_MESSAGES.CALLBACK_EDIT_REENTER);
  await logEvent(env, `edit_re_entered post_id=${parsed.postId}`);
}
