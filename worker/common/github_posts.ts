import {
  API_URLS,
  DEFAULT_AUTOMATION_BRANCH,
  DEFAULT_BASE_BRANCH,
  ERROR_CODES,
  POSTS_PATH,
  WORKER_SYNC_PR_BODY,
  WORKER_SYNC_PR_TITLE,
} from "./constants";
import { Env, GithubContentsResponse, JsonArray, JsonObject, ReadPostsResult, WritePostsResult } from "./types";
import { asString, decodeBase64Utf8, encodeBase64Utf8 } from "./utils";

function ghHeaders(env: Env): HeadersInit {
  return {
    Authorization: `Bearer ${env.GH_FINE_GRAINED_PAT}`,
    Accept: "application/vnd.github+json",
    "Content-Type": "application/json",
    "User-Agent": "linkedin-post-webhook",
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

function stateBranch(env: Env): string {
  const branch = env.GH_STATE_BRANCH?.trim();
  if (branch) {
    return branch;
  }
  return DEFAULT_AUTOMATION_BRANCH;
}

function baseBranch(env: Env): string {
  const branch = env.GH_BASE_BRANCH?.trim();
  if (branch) {
    return branch;
  }
  return DEFAULT_BASE_BRANCH;
}

function githubRepoApiBase(env: Env): string {
  return `${API_URLS.GITHUB_REPO_API_BASE}/${env.GH_REPO}`;
}

function githubContentsUrl(env: Env, path: string, ref: string): string {
  const query = new URLSearchParams({ ref }).toString();
  return `${githubRepoApiBase(env)}/contents/${path}?${query}`;
}

function asObject(value: unknown): JsonObject | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  return value as JsonObject;
}

async function branchHeadSha(env: Env, branch: string): Promise<string | null> {
  const encoded = encodeURIComponent(branch);
  const response = await fetch(`${githubRepoApiBase(env)}/git/ref/heads/${encoded}`, {
    method: "GET",
    headers: ghHeaders(env),
  });
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`github_ref_read_failed branch=${branch} status=${response.status} body=${body}`);
  }

  const data = (await response.json()) as unknown;
  const dataObj = asObject(data);
  const objectObj = asObject(dataObj?.object);
  const sha = asString(objectObj?.sha);
  if (!sha) {
    throw new Error(`github_ref_read_failed branch=${branch} reason=missing_sha`);
  }
  return sha;
}

async function ensureStateBranch(env: Env): Promise<string> {
  const branch = stateBranch(env);
  const base = baseBranch(env);
  if (branch === base) {
    return branch;
  }

  const existingSha = await branchHeadSha(env, branch);
  if (existingSha) {
    return branch;
  }

  const baseSha = await branchHeadSha(env, base);
  if (!baseSha) {
    throw new Error(`github_ref_read_failed base_branch_missing=${base}`);
  }

  const createResponse = await fetch(`${githubRepoApiBase(env)}/git/refs`, {
    method: "POST",
    headers: ghHeaders(env),
    body: JSON.stringify({
      ref: `refs/heads/${branch}`,
      sha: baseSha,
    }),
  });

  // Branch already exists race (422) is benign.
  if (createResponse.ok || createResponse.status === 422) {
    return branch;
  }
  const body = await createResponse.text();
  throw new Error(`github_ref_create_failed branch=${branch} status=${createResponse.status} body=${body}`);
}

async function ensureOpenPullRequest(env: Env): Promise<void> {
  const headBranch = stateBranch(env);
  const base = baseBranch(env);
  if (headBranch === base) {
    return;
  }

  const repoParts = env.GH_REPO.split("/");
  if (repoParts.length !== 2 || !repoParts[0]) {
    throw new Error(`invalid_repo_name GH_REPO=${env.GH_REPO}`);
  }
  const owner = repoParts[0];

  const query = new URLSearchParams({
    state: "open",
    head: `${owner}:${headBranch}`,
    base,
  }).toString();
  const listResponse = await fetch(`${githubRepoApiBase(env)}/pulls?${query}`, {
    method: "GET",
    headers: ghHeaders(env),
  });
  if (!listResponse.ok) {
    const body = await listResponse.text();
    throw new Error(`github_pull_list_failed status=${listResponse.status} body=${body}`);
  }

  const existing = (await listResponse.json()) as unknown;
  if (Array.isArray(existing) && existing.length > 0) {
    return;
  }

  const createResponse = await fetch(`${githubRepoApiBase(env)}/pulls`, {
    method: "POST",
    headers: ghHeaders(env),
    body: JSON.stringify({
      title: WORKER_SYNC_PR_TITLE,
      head: headBranch,
      base,
      body: WORKER_SYNC_PR_BODY,
    }),
  });
  if (createResponse.ok) {
    return;
  }
  const body = await createResponse.text();
  throw new Error(`github_pull_create_failed status=${createResponse.status} body=${body}`);
}

export async function readPosts(env: Env): Promise<ReadPostsResult> {
  const branch = await ensureStateBranch(env);
  const response = await fetch(githubContentsUrl(env, POSTS_PATH, branch), {
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
  const branch = await ensureStateBranch(env);
  const content = encodeBase64Utf8(`${JSON.stringify(posts, null, 2)}\n`);
  const response = await fetch(githubContentsUrl(env, POSTS_PATH, branch), {
    method: "PUT",
    headers: ghHeaders(env),
    body: JSON.stringify({
      message,
      content,
      sha,
      branch,
    }),
  });

  if (response.ok) {
    try {
      await ensureOpenPullRequest(env);
    } catch (err) {
      // PR creation is best-effort; state write already succeeded.
      console.error("ensure_open_pr_failed", err);
    }
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

