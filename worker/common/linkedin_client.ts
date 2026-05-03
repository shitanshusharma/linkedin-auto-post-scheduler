import { API_URLS, ERROR_CODES, LINKEDIN_VERSION, MAX_TELEGRAM_POST_LENGTH } from "./constants";
import { Env, JsonObject } from "./types";
import { asString } from "./utils";

export interface PublishResult {
  ok: boolean;
  linkedinPostId?: string;
  error: string;
  detail?: string;
}

function sanitizeForLinkedIn(value: string): string {
  const noAngles = value.replace(/[<>]/g, "");
  return noAngles.replace(/\r\n/g, "\n").trim();
}

export async function publishToLinkedIn(env: Env, composedText: string): Promise<PublishResult> {
  const commentary = sanitizeForLinkedIn(composedText);
  if (!commentary) {
    return { ok: false, error: ERROR_CODES.EMPTY_POST_AFTER_SANITIZATION };
  }
  if (commentary.length > MAX_TELEGRAM_POST_LENGTH) {
    return { ok: false, error: ERROR_CODES.POST_EXCEEDS_MAX_LENGTH };
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
    response = await fetch(API_URLS.LINKEDIN_POSTS, {
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
      return { ok: false, error: ERROR_CODES.TIMEOUT };
    }
    return { ok: false, error: ERROR_CODES.NETWORK_ERROR };
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
    return {
      ok: false,
      error: ERROR_CODES.LINKEDIN_REAUTH_REQUIRED,
      detail: bodyText.slice(0, 500),
    };
  }
  if (response.status === 429) {
    return {
      ok: false,
      error: ERROR_CODES.LINKEDIN_RATE_LIMITED,
      detail: bodyText.slice(0, 500),
    };
  }
  return {
    ok: false,
    error: `linkedin_status_${response.status}`,
    detail: bodyText.slice(0, 500),
  };
}

