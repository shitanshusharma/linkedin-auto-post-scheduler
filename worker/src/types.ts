export type JsonObject = Record<string, unknown>;
export type JsonArray = unknown[];

export interface KVNamespaceLike {
  get(key: string): Promise<string | null>;
  put(key: string, value: string, options?: { expirationTtl?: number }): Promise<void>;
}

export interface Env {
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

export interface TelegramUser {
  id: number;
}

export interface TelegramChat {
  id: number;
}

export interface TelegramMessage {
  message_id: number;
  chat: TelegramChat;
  from?: TelegramUser;
  text?: string;
}

export interface TelegramCallbackQuery {
  id: string;
  from: TelegramUser;
  data?: string;
  message?: TelegramMessage;
}

export interface TelegramUpdate {
  callback_query?: TelegramCallbackQuery;
  message?: TelegramMessage;
}

export interface GithubContentsResponse {
  sha: string;
  content: string;
  encoding: string;
}

export interface ReadPostsResult {
  posts: JsonObject[];
  sha: string;
}

export interface WritePostsResult {
  ok: boolean;
  conflict: boolean;
}

export interface CallbackAction {
  action: "a" | "e" | "r" | "y" | "n" | "rt";
  postId: string;
  approvalToken: string;
}

