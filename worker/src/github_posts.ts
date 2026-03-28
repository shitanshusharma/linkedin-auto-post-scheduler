import { API_URLS, ERROR_CODES, POSTS_PATH } from "./constants";
import { asString, decodeBase64Utf8, encodeBase64Utf8 } from "./utils";
import { Env, GithubContentsResponse, JsonArray, JsonObject, ReadPostsResult, WritePostsResult } from "./types";

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
  return `${API_URLS.GITHUB_REPO_API_BASE}/${env.GH_REPO}/contents/${path}`;
}

export async function readPosts(env: Env): Promise<ReadPostsResult> {
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

export async function mutatePostsWithRetry<T>(
  env: Env,
  commitMessage: string,
  mutator: (posts: JsonObject[]) => T,
): Promise<T> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const { posts, sha } = await readPosts(env);
    const before = JSON.stringify(posts);
    const result = mutator(posts);
    const after = JSON.stringify(posts);

    // Skip GitHub write on no-op mutations to avoid avoidable API errors.
    if (before === after) {
      return result;
    }

    const write = await writePosts(env, posts, sha, commitMessage);
    if (write.ok) {
      return result;
    }
    if (!write.conflict || attempt === 1) {
      throw new Error(ERROR_CODES.GITHUB_CONFLICT);
    }
  }
  throw new Error("unreachable");
}

export function findPostIndex(posts: JsonObject[], postId: string): number {
  return posts.findIndex((p) => asString(p.id) === postId);
}

